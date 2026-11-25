"""三层配置。

Layer A  代码常量：护栏、超时、n_ctx 上限等，改代码才生效，不进 yaml。
Layer B  config.yaml 的 settings 段：5 个字段可被 /api/settings PUT 改。
Layer C  locked 段：n_ctx / kv_type / vae_tiling，PUT 一律 409。

另外有一组只读运行参数（gateway/sd/llama/models/queue），它们在 yaml 里，
但不进 /api/settings 的 GET/PUT——改了要重启 gateway。

**部署路径**（output.dir / logs.dir / chats.dir）是独立一块：可以改，但 effect 是
restart_gateway，且不与 settings 同层——路径校验失败会让整个工具写不出去，
不该和 steps 共享一个校验函数。改路径不搬迁旧文件（搬迁是独立的一次性操作）。

单一真源：启动时读 yaml（缺失则按默认生成）→ 内存副本 → PUT 改内存 + 原子回写。
派生参数只走 sd_args(cfg) / llama_args(cfg)，不要让启动参数和 settings 各说各话。
"""

from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]          # 应用根目录（exe / desktop.py 所在目录）
RUNTIME = ROOT / "runtime"
MODELS = ROOT / "models"

CONFIG_PATH = ROOT / "config.yaml"

# ---------------- 路径默认值（只读组的一部分，authoritative 在 yaml） ----------------
SD_EXE = RUNTIME / "sd" / "sd-server.exe"
LLAMA_EXE = RUNTIME / "llama" / "llama-server.exe"
OUTPUT = ROOT / "output"
LOGS = ROOT / "logs"
CHATS = ROOT / "chats"

SD_PORT = 8081
CHAT_PORT = 8099
GATEWAY_PORT = 8000
GATEWAY_HOST = "127.0.0.1"
QUEUE_MAX_SIZE = 4

MODEL_DIFFUSION = MODELS / "qwen-image-2.1-turbo-Q5_K_M.gguf"
MODEL_LLM = MODELS / "Qwen3VL-8B-Instruct-Q4_K_M.gguf"
MODEL_VAE = MODELS / "qwen_image_2.1_vae_bf16.safetensors"
MODEL_GEMMA = MODELS / "Gemma-4-E4B-Uncensored-HauhauCS-Aggressive-Q5_K_M.gguf"
MODEL_MMPROJ = MODELS / "mmproj-Qwen3VL-8B-Instruct-F16.gguf"

# ---------------- Layer A：代码常量，不进配置文件 ----------------
RES = 1024
NGL = 99
VRAM_TOTAL_MIB = 8188
PEAK_BUDGET_MIB = 6900
PEAK_GUARD_MIB = 7700
GUARD_CONSECUTIVE = 10                 # 100ms 采样 × 10 点 ≈ 1 秒窗口
SAMPLE_INTERVAL_MS = 100
VRAM_AFTER_JOB_MAX = 1800
VRAM_IDLE_SD = 1038
GEMMA_VRAM_MIB = 3681

CHAT_MAX_TOKENS = 1024
CHAT_TOOLCALL_RETRY_MAX_TOKENS = 2048
# 已废弃：曾用它在 thinking=false 时把预算压到 512。那条规则建立在"关思考其实没关"的错误前提上，
# 实测（thinking=false + 512）会把 400 字的问题截在"释放出氧气，同时"。留着只为标注历史，不要再用。
CHAT_MAX_TOKENS_NO_THINK = 512

N_CTX = 4096                           # 同时是 Layer C 的上限
KV_TYPE = "f16"
VAE_TILING = True

# 超时（秒）
# 这些值原来按本机（RTX 4060 Laptop / SSD）标定：一张 1024² 8 步约 32s、sd 首载约 20s。
# 换到 4–6 GB 卡、机械硬盘或带实时扫描的杀软机器上，原值会让"每一张都超时"，
# 所以统一放大到"慢机器也够用、坏掉时最多多等一会儿"的档位（2026-11 跨机器适配）。
JOB_TIMEOUT_PER_IMAGE_S = 300
TIMEOUTS = {"prepare": 30.0, "image": float(JOB_TIMEOUT_PER_IMAGE_S), "reload": 90.0, "cleanup": 180.0}
HTTP_TIMEOUT = JOB_TIMEOUT_PER_IMAGE_S + 30        # 330s，比监督层多留 30s

# 首次加载/重启的等待上限（从磁盘读 5 GB 权重、机械盘可能要 1–2 分钟）
SD_START_TIMEOUT_S = 180.0
CHAT_READY_TIMEOUT_S = 90.0
GATEWAY_READY_TIMEOUT_S = 180.0                    # 单文件 exe 首次解包到 %TEMP% + 杀软扫描

# sd-server + 文本编码器都在 --offload-to-cpu 下要常驻内存，机器内存太小会换页到卡死
SD_MIN_RAM_MIB = 8192                              # 低于这个值：拒绝启动出图模型
SD_WARN_RAM_MIB = 12288                            # 低于这个值：只告警，不拦


def _find_nvidia_smi() -> Path | None:
    """nvidia-smi 不一定在 C:\\Windows\\System32。

    会落空的情况：Windows 装在别的盘、系统被精简、或 32 位解释器（System32 被重定向到
    SysWOW64，64 位的 nvidia-smi 根本看不到）。依次试 PATH → System32 → NVSMI 目录；
    全都没有就返回 None，上层必须按"探测不到"降级 —— 不能把它当成"没有显卡"，
    更不能让看门狗拿着一个不存在的路径去 create_subprocess_exec 崩掉。
    """
    found = shutil.which("nvidia-smi")
    if found:
        return Path(found)
    candidates = [
        Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "nvidia-smi.exe",
        Path(os.environ.get("ProgramW6432", r"C:\Program Files"))
        / "NVIDIA Corporation" / "NVSMI" / "nvidia-smi.exe",
        Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
        / "NVIDIA Corporation" / "NVSMI" / "nvidia-smi.exe",
    ]
    for c in candidates:
        if c.exists():
            return c
    return None


NVIDIA_SMI: Path | None = _find_nvidia_smi()


# ---------------- Layer B：字段规格 ----------------
class ConfigError(ValueError):
    pass


class LockedField(ConfigError):
    pass


class UnknownField(ConfigError):
    pass


@dataclass(frozen=True)
class FieldSpec:
    default: Any
    type_: type
    allowed: Any            # range 或 set；None 表示"不限定取值，只按 min/max 卡"
    effect: str             # hot / restart_sd / restart_chat / locked
    note: str = ""
    min: int | None = None  # allowed=None 时的下界（含）
    max: int | None = None  # allowed=None 时的上界（含）；None 表示不设上限

    def coerce(self, name: str, value: Any) -> Any:
        if self.type_ is bool:
            if not isinstance(value, bool):
                raise ConfigError(f"{name} 必须是布尔值")
            return value
        if self.type_ is int:
            if isinstance(value, bool) or not isinstance(value, int):
                raise ConfigError(f"{name} 必须是整数")
            if self.allowed is not None:
                ok = value in self.allowed if isinstance(self.allowed, set) else value in self.allowed
                if not ok:
                    rng = sorted(self.allowed)
                    raise ConfigError(f"{name} 只能是 {rng[0]}–{rng[-1]}" if isinstance(self.allowed, range)
                                      else f"{name} 只能是 {rng}")
                return value
            if self.min is not None and value < self.min:
                raise ConfigError(f"{name} 不能小于 {self.min}")
            if self.max is not None and value > self.max:
                raise ConfigError(f"{name} 不能大于 {self.max}")
            return value
        return value


FIELD_SPECS: dict[str, FieldSpec] = {
    "steps": FieldSpec(8, int, range(8, 21), "hot", "采样步数；4 步会出双影，8 是实测下限"),
    # 上限不再卡 2048：用户填多少存多少。真正生效时仍被 n_ctx 截（那是物理边界，不是我们设的规矩）
    "max_tokens": FieldSpec(1024, int, None, "hot",
                            "回复上限（token）：填多少存多少，实际生成仍受上下文长度限制",
                            min=1),
    "thinking": FieldSpec(True, bool, None, "hot", "思考模式；关掉后更快（实测 11.3s → 1.6s）"),
    # 上下文长度从"锁定"改成可改：8K/16K 没实测不等于不许用户试，但要如实告诉他 KV 会长
    "n_ctx": FieldSpec(N_CTX, int, None, "restart_chat",
                       "上下文长度：越大记得越多，KV 缓存也越占显存；保存后自动重启对话服务（约 20s）",
                       min=512, max=32768),
    # 从两个选项拓宽成常见档；384 是实测默认，512 也跑过，其余为 64 的倍数
    "vae_tile": FieldSpec(384, int, {128, 192, 256, 320, 384, 448, 512, 640, 768, 1024},
                          "restart_sd",
                          "VAE tile：VAE 解码时每一块的大小。块越小越省显存但更慢，越大越快但更吃显存；384 是默认"),
    "batch_count_default": FieldSpec(1, int, {1, 2, 3, 4}, "hot",
                                     "默认张数：1 张最快；2–4 张连续出图，更占显存和时间，按自己机器情况选"),
}

# kv_type / vae_tiling 仍是硬锁：改它们会打破已经标定过的显存与画质口径
LOCKED_FIELDS: dict[str, Any] = {"kv_type": KV_TYPE, "vae_tiling": VAE_TILING}

# ---------------- 部署路径：独立一块，effect 恒为 restart_gateway ----------------
PATH_SPECS: dict[str, str] = {
    "output.dir": "出图路径",
    "logs.dir": "日志与显存 CSV 目录",
    "chats.dir": "聊天记录 JSON 目录",
}

# 这些地方写进去不是"用户的选择"，是"事后收拾不完的麻烦"
# 盘符不能写死 C：：Windows 装在 D: 的机器上，C:\Windows 只是个无关目录，
# 真正该拦的是系统目录本身（SystemRoot / ProgramFiles，按环境变量取）。
_FORBIDDEN_DIRS = [
    p for p in (
        Path(os.environ.get("SystemRoot", r"C:\Windows")),
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")),
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")),
        Path(os.environ.get("ProgramData", r"C:\ProgramData")),
    )
]


class PathError(ConfigError):
    pass


def validate_dir(key: str, value: Any) -> Path:
    """绝对路径、非盘符根、不在系统目录、确实可写。不搬迁旧文件。"""
    raw = str(value or "").strip()
    if not raw:
        raise PathError(f"{key} 不能为空")
    p = Path(raw)
    if not p.is_absolute():
        raise PathError(f"{key} 必须是绝对路径（收到 {raw}）")
    if p.parent == p:
        raise PathError(f"{key} 不能是盘符根目录（{p}）")
    for bad in _FORBIDDEN_DIRS:
        if p == bad or bad in p.parents:
            raise PathError(f"{key} 不能放在系统目录（{bad}）")
    if p.exists() and not p.is_dir():
        # 指向一个已存在的文件时不能静默 mkdir 失败或写进去，直接报出来
        raise PathError(f"{key} 指向的是一个文件而不是目录（{p}）")
    try:
        p.mkdir(parents=True, exist_ok=True)
        probe = p / ".write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except Exception as e:
        raise PathError(f"{key} 不可写：{e}")
    return p


@dataclass
class Settings:
    steps: int = 8
    max_tokens: int = 1024
    thinking: bool = True
    n_ctx: int = N_CTX
    vae_tile: int = 384
    batch_count_default: int = 1

    def as_dict(self) -> dict[str, Any]:
        return {k: getattr(self, k) for k in FIELD_SPECS}


@dataclass
class RuntimeGroup:
    gateway_host: str = GATEWAY_HOST
    gateway_port: int = GATEWAY_PORT
    sd_port: int = SD_PORT
    sd_exe: Path = SD_EXE
    llama_port: int = CHAT_PORT
    llama_exe: Path = LLAMA_EXE
    model_diffusion: Path = MODEL_DIFFUSION
    model_llm: Path = MODEL_LLM
    model_vae: Path = MODEL_VAE
    model_gemma: Path = MODEL_GEMMA
    model_mmproj: Path = MODEL_MMPROJ
    output_dir: Path = OUTPUT
    logs_dir: Path = LOGS
    chats_dir: Path = CHATS
    queue_max_size: int = QUEUE_MAX_SIZE


@dataclass
class Config:
    settings: Settings = field(default_factory=Settings)
    runtime: RuntimeGroup = field(default_factory=RuntimeGroup)
    path: Path = CONFIG_PATH
    locked: dict[str, Any] = field(default_factory=lambda: dict(LOCKED_FIELDS))

    # ---- 生效值 ----
    def effective_max_tokens(self, prompt_tokens: int = 0, toolcall: bool = False,
                             cap: int | None = None) -> int:
        """配置是上限，n_ctx 是硬边界。

        曾经在 thinking 关时压到 512，理由是"关思考其实没关，模型照样思考、只是更容易被截断"。
        那个理由随 `enable_thinking: false` 接上而失效，实测压预算会造成真截断：

            thinking=false · max_tokens 被压到 512 · 要 400 字的问题
            → finish=length，正文 870 字停在"释放出氧气，同时"

        所以不再按思考开关压预算：两种模式下都只受配置上限与 n_ctx 约束。
        """
        # 调用方可以本次指定 cap（对话设置的「最大回复长度」），但仍受 n_ctx 硬边界约束
        cap = self.settings.max_tokens if cap is None else max(1, int(cap))
        # n_ctx 现在是用户可改字段，硬边界跟着配置走，不再读代码常量
        room = self.settings.n_ctx - prompt_tokens - 16     # 留一点模板开销
        return max(1, min(cap, room))

    def update(self, patch: dict[str, Any]) -> dict[str, Any]:
        """校验并应用，返回 {"changed": {...}, "effects": {...}}。有 locked 就抛。"""
        locked = [k for k in patch if k in LOCKED_FIELDS]
        if locked:
            raise LockedField(f"{locked[0]} 已锁定（只能 {LOCKED_FIELDS[locked[0]]}）")
        unknown = [k for k in patch if k not in FIELD_SPECS]
        if unknown:
            raise UnknownField(f"未知字段: {unknown[0]}")

        coerced: dict[str, Any] = {}
        for k, v in patch.items():
            spec = FIELD_SPECS[k]
            coerced[k] = spec.coerce(k, v)

        changed = {k: v for k, v in coerced.items() if getattr(self.settings, k) != v}
        for k, v in changed.items():
            setattr(self.settings, k, v)

        effects = {k: FIELD_SPECS[k].effect for k in changed}
        if changed:
            self.save()
        return {"changed": changed, "effects": effects,
                "restart_sd": "restart_sd" in effects.values(),
                "restart_chat": "restart_chat" in effects.values()}

    def update_paths(self, patch: dict[str, Any]) -> dict[str, Any]:
        """部署路径：只在 PUT /api/settings/paths 用。生效需重启 gateway。"""
        if not patch:
            return {"changed": {}, "restart_gateway": False}
        unknown = [k for k in patch if k not in PATH_SPECS]
        if unknown:
            raise UnknownField(f"未知路径字段: {unknown[0]}")

        attr = {"output.dir": "output_dir", "logs.dir": "logs_dir", "chats.dir": "chats_dir"}
        validated: dict[str, Path] = {}
        for k, v in patch.items():
            validated[k] = validate_dir(k, v)

        changed: dict[str, str] = {}
        for k, p in validated.items():
            cur = getattr(self.runtime, attr[k])
            if Path(cur) != p:
                setattr(self.runtime, attr[k], p)
                changed[k] = str(p)
        if changed:
            self.save()
        return {"changed": changed, "effects": {k: "restart_gateway" for k in changed},
                "restart_gateway": bool(changed)}

    def save(self) -> None:
        atomic_write_yaml(self.path, self.to_doc())

    def to_doc(self) -> dict[str, Any]:
        rt = self.runtime
        return {
            "gateway": {"host": rt.gateway_host, "port": rt.gateway_port},
            "sd": {"port": rt.sd_port, "exe": _portable(rt.sd_exe)},
            "llama": {"port": rt.llama_port, "exe": _portable(rt.llama_exe)},
            "models": {
                "diffusion": _portable(rt.model_diffusion),
                "llm": _portable(rt.model_llm),
                "vae": _portable(rt.model_vae),
                "gemma": _portable(rt.model_gemma),
                "mmproj": _portable(rt.model_mmproj),
            },
            "output": {"dir": _portable(rt.output_dir)},
            "logs": {"dir": _portable(rt.logs_dir)},
            "chats": {"dir": _portable(rt.chats_dir)},
            "queue": {"max_size": rt.queue_max_size},
            "settings": self.settings.as_dict(),
        }

    # ---- 对外 ----
    def public_settings(self) -> dict[str, Any]:
        rt = self.runtime
        return {
            "values": self.settings.as_dict(),
            "fields": {
                k: {"default": s.default,
                    "allowed": sorted(s.allowed) if isinstance(s.allowed, (range, set)) else None,
                    "min": s.min, "max": s.max,
                    "effect": s.effect, "note": s.note}
                for k, s in FIELD_SPECS.items()
            },
            "locked": dict(self.locked),
            # 部署路径独立成一块：改它要重启 gateway，不搬迁旧文件
            "paths": {
                "values": {
                    "output.dir": str(rt.output_dir),
                    "logs.dir": str(rt.logs_dir),
                    "chats.dir": str(rt.chats_dir),
                },
                "fields": {k: {"effect": "restart_gateway", "note": note}
                           for k, note in PATH_SPECS.items()},
            },
            "max_tokens_effective_no_think": self.effective_max_tokens(),
            "unverified_options": {},
        }


def estimate_prompt_tokens(text: str) -> int:
    """没有 /tokenize 时的兜底：CJK 约 1 token/字，其余约 0.25 token/字符，再乘 1.2 余量。

    **这是估算不是精确值**。本机 `POST /tokenize` 已实测可用，正常路径必须走它，
    只有调用失败时才回落，别把这里当准数。
    """
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff" or "\u3040" <= ch <= "\u30ff")
    other = len(text) - cjk
    return int((cjk + other * 0.25) * 1.2)


def _portable(p: Path) -> str:
    """写回 yaml 时，应用目录内的路径一律存相对路径。

    发布包会被拷到别的盘 / 别的用户名下；一旦把开发机的绝对路径写进 yaml，
    换台机器就全部失效。只有用户显式改到应用目录外的路径才保留绝对路径。
    """
    try:
        return str(p.resolve().relative_to(ROOT.resolve()))
    except (ValueError, OSError):
        return str(p)


def atomic_write_yaml(path: Path, doc: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = yaml.safe_dump(doc, allow_unicode=True, sort_keys=False)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".config-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)                 # 原子替换，不产生半截文件
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        raise


def _resolve(value, default: Path) -> Path:
    """yaml 里可以写相对路径，按"应用根目录"（ROOT）解析，不按调用者的 cwd。

    桌面壳 `灵枢.exe` 就放在这个根目录里，双击时 cwd 是根目录、`__file__` 也在根目录，
    两者一致；但从别处（比如右键"以管理员身份"或快捷方式设了工作目录）起时 cwd 可能不同，
    所以以 ROOT 为准更稳。外部目录全部外置：`runtime/`、`output/`、`logs/`、`chats/` 都在根目录下，
    打包成 exe 时不进包（不用 sys._MEIPASS）。
    """
    if value is None:
        return default
    p = Path(str(value).strip())
    if not str(p):
        return default
    return p if p.is_absolute() else (ROOT / p)


def load_config(path: Path | None = None) -> Config:
    """缺失则按默认生成。字段缺失/非法按默认补，不因为一个手改错字就起不来。"""
    p = Path(path) if path else CONFIG_PATH
    cfg = Config(path=p)
    if not p.exists():
        cfg.save()
        return cfg

    doc = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    rt = cfg.runtime
    g = doc.get("gateway") or {}
    rt.gateway_host = g.get("host", rt.gateway_host)
    rt.gateway_port = int(g.get("port", rt.gateway_port))
    sd = doc.get("sd") or {}
    rt.sd_port = int(sd.get("port", rt.sd_port))
    rt.sd_exe = _resolve(sd.get("exe"), rt.sd_exe)
    ll = doc.get("llama") or {}
    rt.llama_port = int(ll.get("port", rt.llama_port))
    rt.llama_exe = _resolve(ll.get("exe"), rt.llama_exe)
    m = doc.get("models") or {}
    rt.model_diffusion = _resolve(m.get("diffusion"), rt.model_diffusion)
    rt.model_llm = _resolve(m.get("llm"), rt.model_llm)
    rt.model_vae = _resolve(m.get("vae"), rt.model_vae)
    rt.model_gemma = _resolve(m.get("gemma"), rt.model_gemma)
    rt.model_mmproj = _resolve(m.get("mmproj"), rt.model_mmproj)
    rt.output_dir = _resolve((doc.get("output") or {}).get("dir"), rt.output_dir)
    rt.logs_dir = _resolve((doc.get("logs") or {}).get("dir"), rt.logs_dir)
    rt.chats_dir = _resolve((doc.get("chats") or {}).get("dir"), rt.chats_dir)
    rt.queue_max_size = int((doc.get("queue") or {}).get("max_size", rt.queue_max_size))

    saved = doc.get("settings") or {}
    for k, spec in FIELD_SPECS.items():
        if k in saved:
            try:
                setattr(cfg.settings, k, spec.coerce(k, saved[k]))
            except ConfigError:
                pass                          # 保留默认，运行时日志提示
    cfg.locked = dict(LOCKED_FIELDS)          # Layer C 不受 yaml 影响，手改也不生效
    return cfg


# ---------------- 派生参数（唯一入口） ----------------

def sd_args(cfg: Config | None = None, *, port: int | None = None, vae_tile: int | None = None) -> list[str]:
    from gateway import hardware          # 延迟导入：hardware 反过来要用 config，顶层互相 import 会成环

    cfg = cfg or load_config()
    rt, st = cfg.runtime, cfg.settings
    tile = hardware.sd_tile(vae_tile if vae_tile is not None else st.vae_tile)
    return [
        "--diffusion-model", str(rt.model_diffusion),
        "--llm", str(rt.model_llm),
        "--vae", str(rt.model_vae),
        "--vae-tiling",
        "--vae-tile-size", str(tile),
        "--offload-to-cpu",
        "--listen-port", str(port if port is not None else rt.sd_port),
    ]


def llama_args(cfg: Config | None = None, *, port: int | None = None, ngl: int | None = None,
               n_ctx: int | None = None) -> list[str]:
    """对话服务的启动参数。不传 ngl/n_ctx 时用硬件自动决定的档（见 gateway/hardware.py）；
    单测/诊断脚本可以显式传值绕过自动档。"""
    from gateway import hardware

    cfg = cfg or load_config()
    rt = cfg.runtime
    p = hardware.chat_params()
    args = [
        "--model", str(rt.model_gemma),
        "--host", "127.0.0.1",
        "--port", str(port if port is not None else rt.llama_port),
        "--n-gpu-layers", str(p["ngl"] if ngl is None else ngl),
        # 用户手填的上下文优先于自动档：自动档只是"推荐值"，不是命令
        "--ctx-size", str(cfg.settings.n_ctx if n_ctx is None else n_ctx),
        "--threads", str(p["threads"]),
        "--batch-size", str(p["batch"]),
        "--ubatch-size", str(p["ubatch"]),
    ]
    if p["flash_attn"]:
        # 显式写 on：这个 build 的 -fa 带可选参数（on/off/auto），裸写会被当成 auto
        args.extend(["-fa", "on"])
    return args
