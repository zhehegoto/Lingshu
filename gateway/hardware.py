"""硬件自动识别 + 参数自动决定。

口径参考 LocModel（只看它的面板和 settings.json，不搬它的下载功能）：
  1. 诊断：NVIDIA 显卡（型号/显存/驱动）+ CPU（物理核/线程）+ 内存 + 磁盘余量；
  2. 算层：读 GGUF 元数据估 KV 缓存，判断"整个模型放不放得进显存"，放不下按比例算 -ngl；
  3. 定参：-ngl / --ctx-size / --threads / --batch-size / --ubatch-size / -fa / VAE tile / 显存护栏。

三条自我约束（这个项目的既有规矩）：
  * 只在本机验证过的档位里选：ctx ∈ {2048, 4096}，tile ∈ {256, 384}，KV 保持 f16，
    不因为"显卡更大"就擅自打开没测过的 8192 或 512 —— 那是拿用户的机器当试验台。
  * 探测失败要降级而不是崩：没有 nvidia-smi 就当纯 CPU 跑并写进 notes，让用户看得见原因。
  * 参数只在进程内生效，不偷偷回写 config.yaml；用户改过的配置永远是权威。
"""
from __future__ import annotations

import ctypes
import json
import mmap
import os
import shutil
import struct
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

from gateway import config as cfgmod

MIB = 1024 * 1024

# 显存里不属于模型的那部分：驱动上下文、桌面合成、CUDA 库热身。
# 512 是"干净桌面"的经验值；多显示器 / 4K / 壁纸引擎会占到 700–1500 MiB，
# 所以实际用的是 max(512, 开机实测已被占用)，见 detect()。
VRAM_OVERHEAD_MIB = 512

# 只在这两个档位里选（都实测过）
CTX_CHOICES = (4096, 2048)
TILE_SAFE = 384
TILE_SMALL = 256

# flash attention 从 Turing（compute capability 7.0）起才被这个 build 稳定支持；
# Pascal（GTX 10 系）及更老的卡上硬开 -fa on 可能直接被拒、llama-server 起不来。
FA_MIN_COMPUTE = 7.0

_PLAN: dict | None = None


# ---------------------------------------------------------------- 探测：GPU

def _run(cmd: list[str], timeout: int = 10) -> str | None:
    """跑一次外部命令。失败返回 None —— 调用方必须把 None 当成"探测不到"，
    不许当成"没有"。"""
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout,
                           encoding="utf-8", errors="replace",
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return None
    if p.returncode != 0:
        return None
    return p.stdout


def _compute_cap_from_name(name: str) -> float | None:
    """拿不到 compute_cap 时的兜底：按型号粗判。

    只区分"Turing 及以后"和"更老"两类——判错的代价是关掉 flash attention（慢一点），
    比硬开导致服务起不来划算。
    """
    low = (name or "").lower()
    if "rtx" in low or "gtx 16" in low or "tesla t4" in low or "a100" in low \
            or "h100" in low or "l4" in low or "a10" in low or "a40" in low:
        return 7.5
    if "gtx 10" in low or "gtx 9" in low or "gtx 7" in low or "mx" in low \
            or "quadro" in low or "p100" in low or "p4" in low:
        return 6.1
    return None


def gpu_info() -> dict | None:
    """型号 / 总显存 / 空闲显存 / 驱动 / 算力。没有 NVIDIA 卡（或 nvidia-smi 不可用）返回 None。"""
    if cfgmod.NVIDIA_SMI is None:
        return None
    out = _run([str(cfgmod.NVIDIA_SMI),
                "--query-gpu=name,memory.total,memory.free,driver_version",
                "--format=csv,noheader,nounits"])
    if not out:
        return None
    first = out.strip().splitlines()
    if not first:
        return None
    parts = [x.strip() for x in first[0].split(",")]
    if len(parts) < 4:
        return None
    try:
        total = int(float(parts[1]))
        free = int(float(parts[2]))
    except ValueError:
        return None
    if total <= 0:
        return None

    cap = _query_compute_cap()
    if cap is None:
        cap = _compute_cap_from_name(parts[0])
    return {"name": parts[0], "vram_mib": total, "free_mib": free,
            "driver": parts[3], "count": len(first), "compute_cap": cap}


def _query_compute_cap() -> float | None:
    """单独查一次 compute_cap：老驱动不认这个字段会整条命令失败，
    所以不能塞进主查询里（那会让整个探测白跑）。"""
    if cfgmod.NVIDIA_SMI is None:
        return None
    out = _run([str(cfgmod.NVIDIA_SMI), "--query-gpu=compute_cap",
                "--format=csv,noheader,nounits"], timeout=5)
    if not out:
        return None
    try:
        return float(out.strip().splitlines()[0].strip())
    except (ValueError, IndexError):
        return None


# ---------------------------------------------------------------- 探测：CPU / 内存 / 磁盘

def cpu_info() -> dict:
    """物理核数 + 逻辑线程数 + 型号。

    物理核用 GetLogicalProcessorInformationEx 数 RelationProcessorCore，
    不靠"线程数除以 2"猜 —— 大小核（这个本子的 14 核 20 线程）会被猜错。
    """
    import winreg

    name = "未知 CPU"
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as k:
            name = str(winreg.QueryValueEx(k, "ProcessorNameString")[0]).strip()
    except OSError:
        pass

    logical = os.cpu_count() or 1
    physical = 0
    try:
        kernel32 = ctypes.windll.kernel32
        RELATION_PROCESSOR_CORE = 0
        length = wintypes.DWORD(0)
        if kernel32.GetLogicalProcessorInformationEx(RELATION_PROCESSOR_CORE, None,
                                                     ctypes.byref(length)) == 0 and length.value:
            buf = ctypes.create_string_buffer(length.value)
            if kernel32.GetLogicalProcessorInformationEx(
                    RELATION_PROCESSOR_CORE, buf, ctypes.byref(length)):
                off = 0
                while off < length.value:
                    relationship = ctypes.c_uint32.from_buffer(buf, off).value
                    size = ctypes.c_uint32.from_buffer(buf, off + 4).value
                    if relationship == RELATION_PROCESSOR_CORE:
                        physical += 1
                    if size <= 0:
                        break
                    off += size
    except (AttributeError, OSError, ValueError):
        physical = 0
    return {"name": name, "physical": physical or max(1, logical // 2),
            "logical": logical}


def ram_info() -> dict:
    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [("dwLength", wintypes.DWORD), ("dwMemoryLoad", wintypes.DWORD),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

    st = MEMORYSTATUSEX()
    st.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    try:
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st))
    except (AttributeError, OSError):
        return {"total_mib": 0, "avail_mib": 0}
    return {"total_mib": int(st.ullTotalPhys // MIB), "avail_mib": int(st.ullAvailPhys // MIB)}


def disk_info(path: Path) -> dict:
    try:
        u = shutil.disk_usage(str(path))
    except OSError:
        return {"free_gib": 0.0, "total_gib": 0.0}
    return {"free_gib": round(u.free / (MIB * 1024), 1),
            "total_gib": round(u.total / (MIB * 1024), 1)}


# ---------------------------------------------------------------- GGUF 元数据

_GGUF_STR, _GGUF_ARR = 8, 9
_GGUF_SCALAR = {0: ("<B", 1), 1: ("<b", 1), 2: ("<H", 2), 3: ("<h", 2), 4: ("<I", 4),
                5: ("<i", 4), 6: ("<f", 4), 7: ("<?", 1), 10: ("<Q", 8), 11: ("<q", 8),
                12: ("<d", 8)}

_WANTED = ("general.architecture", "block_count", "attention.head_count",
           "attention.head_count_kv", "attention.key_length", "attention.value_length",
           "embedding_length", "context_length")


def gguf_meta(path: Path) -> dict:
    """只读 GGUF 头部的 KV 段，取"算显存要用的几个数"。

    tokenizer 那种巨型数组会拖慢解析，所以边读边判断：拿到够用的键就走，
    读不懂（版本太新/结构异常）就返回空 dict，让上层按文件大小粗算。
    """
    meta: dict[str, object] = {}
    try:
        with open(path, "rb") as f:
            if f.read(4) != b"GGUF":
                return meta
            vraw = f.read(4)                       # 版本；后面是 8 字节的张量数 + 8 字节的 KV 数
            if len(vraw) < 4:
                return meta
            version = struct.unpack("<I", vraw)[0]
            if version not in (2, 3):
                return meta
            counts = f.read(16)
            if len(counts) < 16:
                return meta
            _n_tensors, n_kv = struct.unpack("<QQ", counts)

            def read_len() -> int:
                return struct.unpack("<Q", f.read(8))[0]

            for _ in range(min(int(n_kv), 512)):
                klen = read_len()
                if klen > 256:
                    return meta
                key = f.read(klen).decode("utf-8", "replace")
                vtype = struct.unpack("<I", f.read(4))[0]
                want = key in _WANTED or key.endswith(_WANTED[1:])
                if vtype == _GGUF_STR:
                    slen = read_len()
                    raw = f.read(slen)
                    if want:
                        meta[key] = raw.decode("utf-8", "replace")
                elif vtype == _GGUF_ARR:
                    itype = struct.unpack("<I", f.read(4))[0]
                    count = read_len()
                    if itype == _GGUF_STR:            # 字符串数组是变长的，必须逐个跳
                        for _i in range(int(count)):
                            f.seek(read_len(), os.SEEK_CUR)
                    elif itype == _GGUF_ARR:          # 嵌套数组：不啃 tokenizer，见好就收
                        return meta
                    else:
                        fmt = _GGUF_SCALAR.get(itype)
                        if fmt is None:
                            return meta
                        if want and itype in (4, 5) and 0 < count <= 1024:
                            vals = struct.unpack("<" + "i" * int(count), f.read(4 * int(count)))
                            meta[key] = list(vals)
                        else:
                            f.seek(fmt[1] * int(count), os.SEEK_CUR)
                else:
                    fmt = _GGUF_SCALAR.get(vtype)
                    if fmt is None:
                        return meta
                    raw = f.read(fmt[1])
                    val = struct.unpack(fmt[0], raw)[0]
                    if want:
                        meta[key] = bool(val) if vtype == 7 else val
                # 该有的都拿到了就不再往下啃 tokenizer（键名带 arch 前缀，按后缀比）
                if sum(1 for w in _WANTED[:7]
                       if any(k == w or k.endswith("." + w) for k in meta)) >= 6:
                    break
    except (OSError, struct.error, ValueError):
        return {}
    return meta


def _meta(meta: dict, arch_prefix: str, key: str):
    return meta.get(f"{arch_prefix}.{key}", meta.get(key))


def kv_bytes_per_token(meta: dict) -> int:
    """每 token 的 KV 缓存字节数（f16）。算不出来返回 0，上层退回经验值。"""
    if not meta:
        return 0
    arch = str(meta.get("general.architecture") or "")
    layers = _meta(meta, arch, "block_count")
    heads = _meta(meta, arch, "attention.head_count")
    kv_heads = _meta(meta, arch, "attention.head_count_kv")
    klen = _meta(meta, arch, "attention.key_length")
    vlen = _meta(meta, arch, "attention.value_length")
    embd = _meta(meta, arch, "embedding_length")
    try:
        layers = int(layers)
        heads = int(heads) if not isinstance(heads, list) else int(heads[0])
        # 三个键的覆盖关系：两个都给就用给的值；只给一个就认为 K/V 同宽；
        # 都不给才退回 embedding_length / head_count。别用"缺一个就把两个都覆盖"，
        # 那会把 key_length=512 的信息丢掉，KV 估算直接小一截。
        if not klen and not vlen:
            if not embd or not heads:
                return 0
            klen = vlen = int(embd) // heads
        elif not klen:
            klen = vlen
        elif not vlen:
            vlen = klen
        klen, vlen = int(klen), int(vlen)
        if isinstance(kv_heads, list):
            per_layer = sum(int(h) for h in kv_heads[:layers])
        else:
            per_layer = int(kv_heads or heads) * layers
    except (TypeError, ValueError, IndexError):
        return 0
    return per_layer * (klen + vlen) * 2          # f16，K 与 V 各一份


# ---------------------------------------------------------------- 决策

def _threads(cpu: dict) -> int:
    physical = int(cpu.get("physical") or 1)
    if physical <= 4:
        return physical
    return max(4, min(physical - 2, 12))          # 留两核给界面/系统，上限 12 免抢核


def _batch(vram_mib: int) -> tuple[int, int]:
    if vram_mib >= 8000:
        return 2048, 512
    return 1024, 256


def _ram_notes(ram: dict | None) -> list[str]:
    """内存门禁：sd-server 带 --offload-to-cpu，权重与文本编码器要同时常驻内存。

    磁盘上 17 GB 模型不等于运行时 17 GB，但 8 GB 内存机器上跑 4.9 GB 扩散权重 +
    5 GB 文本编码器一定会换页到卡死，而且表现为"整机假死"而不是一个明确的报错，
    所以这里必须提前说清楚。
    """
    total = int((ram or {}).get("total_mib") or 0)
    if not total:
        return []
    if total < cfgmod.SD_MIN_RAM_MIB:
        return [f"内存只有 {total} MB，低于出图模型需要的 {cfgmod.SD_MIN_RAM_MIB} MB："
                f"出图模型会被拒绝启动（换页会把整机拖死）。对话仍可用。"]
    if total < cfgmod.SD_WARN_RAM_MIB:
        return [f"内存 {total} MB 偏小：出图模型要常驻内存，同时开浏览器/IDE 可能换页变慢。"]
    return []


def decide(gpu: dict | None, cpu: dict, model_size_mib: int,
           kv_per_token: int, overhead_mib: int = VRAM_OVERHEAD_MIB,
           ram: dict | None = None) -> dict:
    """把"显卡 + 模型多大"折成一组启动参数。纯函数，方便用假硬件做对照测试。"""
    notes: list[str] = []
    overhead = max(int(VRAM_OVERHEAD_MIB), int(overhead_mib or 0))
    if not gpu:
        notes.append("未检测到 NVIDIA 显卡（或 nvidia-smi 不可用）：-ngl 0，纯 CPU 推理，"
                     "对话会明显变慢；GPU 相关的护栏也不再生效。")
        notes.extend(_ram_notes(ram))
        return {
            "gpu": None, "ngl": 0, "ctx": 2048, "threads": _threads(cpu),
            "batch": 1024, "ubatch": 256, "flash_attn": False,
            "vae_tile": TILE_SMALL, "steps": 8,
            "budget_mib": 0, "guard_mib": 0, "overhead_mib": overhead,
            "fits": False, "notes": notes,
        }

    vram = int(gpu["vram_mib"])
    weights = max(1, int(model_size_mib))
    kv_per_token = kv_per_token or 0
    ctx = CTX_CHOICES[-1]
    kv_mib = 0
    fits = False
    for cand in CTX_CHOICES:
        kv = int(kv_per_token * cand / MIB)
        if weights + kv + overhead <= vram * 0.96:
            ctx, kv_mib, fits = cand, kv, True
            break
    else:
        ctx = CTX_CHOICES[-1]
        kv_mib = int(kv_per_token * ctx / MIB)

    if fits:
        ngl = 99
        notes.append(
            f"模型 {weights} MB + KV {kv_mib} MB（{ctx} 上下文）+ 余量 {overhead} MB "
            f"= {weights + kv_mib + overhead} MB ≤ 显存 {vram} MB：整块进显存（-ngl 99）。")
    else:
        room = vram * 0.96 - overhead - kv_mib
        ratio = max(0.0, min(1.0, room / weights))
        ngl = int(ratio * 99)
        ngl = max(0, min(98, ngl - ngl % 2))       # 偶数层，留一层给 CPU
        notes.append(
            f"模型 {weights} MB + KV {kv_mib} MB + 余量 {overhead} MB 放不进 {vram} MB 显存："
            f"按比例只卸载 {ngl} 层到 GPU，其余层在内存里跑，速度会掉。想更快就换更小的量化。")

    vram_budget = int(vram * 0.84)
    guard = int(vram * 0.94)
    if vram >= 6144:
        tile, tile_note = TILE_SAFE, None
    else:
        tile, tile_note = TILE_SMALL, f"显存 {vram} MB < 6 GB：VAE tile 降到 {TILE_SMALL} 防止解码时爆显存。"
    if tile_note:
        notes.append(tile_note)

    cap = gpu.get("compute_cap")
    flash = True
    if cap is not None and float(cap) < FA_MIN_COMPUTE:
        flash = False
        notes.append(f"{gpu.get('name') or '显卡'} 算力 {cap} < {FA_MIN_COMPUTE}："
                     f"关掉 flash attention（老卡硬开会起不来）。")
    elif cap is None:
        notes.append("查不到显卡算力（驱动太老）：保守关掉 flash attention。")
        flash = False

    notes.extend(_ram_notes(ram))
    return {
        "gpu": gpu, "ngl": ngl, "ctx": ctx, "threads": _threads(cpu),
        "batch": _batch(vram)[0], "ubatch": _batch(vram)[1],
        "flash_attn": flash, "vae_tile": tile, "steps": 8,
        "budget_mib": vram_budget, "guard_mib": guard, "overhead_mib": overhead,
        "fits": fits, "notes": notes,
    }


def detect(force: bool = False) -> dict:
    """整套探测 + 决策。结果缓存在进程内；force=True 重新采一次。"""
    global _PLAN
    if _PLAN is not None and not force:
        return _PLAN

    gpu = gpu_info()
    cpu = cpu_info()
    ram = ram_info()
    model_path = cfgmod.load_config().runtime.model_gemma
    try:
        size_mib = int(Path(model_path).stat().st_size / MIB)
    except OSError:
        size_mib = 0
    meta = gguf_meta(Path(model_path)) if Path(model_path).exists() else {}
    kv = kv_bytes_per_token(meta)

    # 桌面/其他程序已经占用的显存当余量：多显示器、4K、壁纸引擎会让"512 MB 余量"变成
    # 一个过于乐观的假设，按开机实测值抬上去，宁可少卸载几层也别直接 OOM。
    baseline = 0
    overhead = VRAM_OVERHEAD_MIB
    if gpu:
        baseline = max(0, int(gpu["vram_mib"]) - int(gpu["free_mib"]))
        # 上限压到半张卡：防止"某个大程序吃了 7 GB"把 ngl 直接算成 0（那台机器本来就跑不了），
        # 也避免把上次残留的本项目进程误当成必须让出的空间。
        overhead = max(VRAM_OVERHEAD_MIB, min(baseline, int(gpu["vram_mib"] * 0.5)))

    plan = decide(gpu, cpu, size_mib, kv, overhead_mib=overhead, ram=ram)
    if baseline > 700:
        plan["notes"].append(
            f"开机时显存已被其他程序占用 {baseline} MB（多显示器/浏览器/壁纸引擎等），"
            f"余量按这个值算；想多留显存给模型就先关掉它们。")
    disk = disk_info(cfgmod.ROOT)
    if disk["free_gib"] and disk["free_gib"] < 5:
        plan["notes"].append(f"安装目录所在磁盘只剩 {disk['free_gib']} GB：出图会写 output，空间不足会失败。")
    # 非 ASCII 安装路径：模型路径会原样传给 sd-server / llama-server（C++ 侧按 ANSI 代码页
    # 打开文件），在英文系统或用户名含中文的机器上可能直接打不开。
    try:
        if not str(cfgmod.ROOT).isascii():
            plan["notes"].append(
                f"安装路径含非 ASCII 字符（{cfgmod.ROOT}）：部分后端可能打不开模型文件，"
                f"建议把整个文件夹放到纯英文路径下，例如 D:\\Lingshu。")
    except Exception:
        pass

    plan.update({
        "detected_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "cpu": cpu, "ram": ram,
        "disk": disk,
        "chat_model": str(model_path),
        "chat_model_mib": size_mib,
        "kv_mib_at_ctx": int(kv * plan["ctx"] / MIB),
        "baseline_used_mib": baseline,
        # 存一份每 token 的 KV 字节数：用户把上下文改大时，用它现算"还装不装得下"
        "kv_bytes_per_token": int(kv),
        "gguf": {k: _meta(meta, str(meta.get("general.architecture") or ""), k)
                 for k in ("block_count", "attention.head_count", "attention.head_count_kv",
                           "attention.key_length", "attention.value_length",
                           "embedding_length")} | {"arch": meta.get("general.architecture")},
    })
    if not kv:
        plan["notes"].append("GGUF 元数据没解析出来：KV 缓存按 0 估算，显存判断可能偏乐观。")
    _PLAN = plan
    try:
        (cfgmod.LOGS / "hardware.json").write_text(
            json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass
    return plan


def current() -> dict:
    return detect()


def live_free() -> dict:
    """界面打开时要看的两个"现在还剩多少"：空闲显存 / 可用内存。
    探测快照记的是启动时的值，模型加载完会差一大截，所以这里每次现采。"""
    gpu = gpu_info()
    ram = ram_info()
    return {"gpu_free_mib": int(gpu["free_mib"]) if gpu else 0,
            "ram_avail_mib": int(ram["avail_mib"])}


# ---------------------------------------------------------------- 给启动参数用的取值

def chat_params() -> dict:
    p = current()
    return {"ngl": p["ngl"], "ctx": p["ctx"], "threads": p["threads"],
            "batch": p["batch"], "ubatch": p["ubatch"], "flash_attn": p["flash_attn"]}


def sd_tile(configured: int) -> int:
    """settings.vae_tile 是用户可改的值，但低显存机器上要比它更保守。"""
    p = current()
    gpu = p.get("gpu")
    if gpu and int(gpu["vram_mib"]) < 6144:
        return min(int(configured), TILE_SMALL)
    return int(configured)


def guard_mib() -> int:
    p = current()
    return int(p["guard_mib"] or cfgmod.PEAK_GUARD_MIB)


def budget_mib() -> int:
    p = current()
    return int(p["budget_mib"] or cfgmod.PEAK_BUDGET_MIB)


def _sd_weights_mib() -> int:
    """出图侧三个文件（主干 + 文本编码器 + VAE）加起来多大。

    "启动全部模型"这个动作是否该给用户，取决于显存能不能同时装下对话与出图两条权重链。
    这里只读文件大小，不读元数据——判断的是"装不装得下"，不是"参数怎么选"。
    """
    cfg = cfgmod.load_config()
    total = 0
    for p in (cfg.runtime.model_diffusion, cfg.runtime.model_llm, cfg.runtime.model_vae):
        try:
            total += os.stat(p).st_size // MIB
        except OSError:
            continue
    return int(total)


def summary() -> dict:
    """状态条/设置页要的那一小撮，避免把整个 plan 塞给前端。"""
    p = current()
    sd_weights = _sd_weights_mib()
    vram = int((p["gpu"] or {}).get("vram_mib") or 0)
    need_both = int((p.get("chat_model_mib") or 0) + (p.get("kv_mib_at_ctx") or 0) + sd_weights)
    both_fits = bool(vram and need_both <= int(vram * 0.97))
    return {
        "gpu": p["gpu"], "cpu": p["cpu"], "ram": p["ram"], "disk": p["disk"],
        "chat": {"ngl": p["ngl"], "ctx": p["ctx"], "threads": p["threads"],
                 "batch": p["batch"], "ubatch": p["ubatch"], "flash_attn": p["flash_attn"],
                 "fits": p["fits"], "model_mib": p["chat_model_mib"],
                 "kv_mib": p["kv_mib_at_ctx"]},
        "sd": {"vae_tile": p["vae_tile"], "steps": p["steps"], "weights_mib": sd_weights},
        "both": {"fits": both_fits, "need_mib": need_both, "vram_mib": vram},
        "guard": {"budget_mib": p["budget_mib"], "guard_mib": p["guard_mib"]},
        "notes": p["notes"],
        "detected_at": p["detected_at"],
    }


# ---------------------------------------------------------------- 推荐值 + 显存估算
# 设置页先给一台机器该用的值（用户仍可手改）。推荐值只是"填进去的默认"，
# 不覆盖用户已经写进 config.yaml 的选择——那个永远优先。

def recommend() -> dict:
    p = current()
    return {
        "n_ctx": p["ctx"],
        "max_tokens": min(2048, max(256, int(p["ctx"]) // 4)),
        "steps": p["steps"],
        "vae_tile": p["vae_tile"],
        "batch_count_default": 1,
        "ngl": p["ngl"],
        "threads": p["threads"],
        "flash_attn": p["flash_attn"],
    }


def ctx_estimate(ctx: int) -> dict:
    """按候选上下文现算 KV/总占用，回答"这个 ctx 还装得下吗"。

    guard/budget 是显存护栏与预算（0.94 / 0.84 显存）。fits_guard 表示不越护栏，
    fits_budget 表示连常用预算都没超。没有 GPU 时两者都按 False、gpu 字段为 False。
    """
    p = current()
    gpu = p.get("gpu")
    kv = int(int(p.get("kv_bytes_per_token") or 0) * int(ctx) / MIB)
    total = int(p.get("chat_model_mib") or 0) + kv + int(p.get("overhead_mib") or VRAM_OVERHEAD_MIB)
    guard = guard_mib() or 0
    budget = budget_mib() or 0
    has_gpu = bool(gpu)
    return {
        "ctx": int(ctx), "kv_mib": kv, "total_mib": total,
        "guard_mib": guard, "budget_mib": budget,
        "gpu": has_gpu,
        "fits_guard": bool(has_gpu and guard and total <= guard),
        "fits_budget": bool(has_gpu and budget and total <= budget),
    }
