"""FastAPI 入口。启停全部放在 lifespan 里，模块顶层不实例化、不起 task。

为什么必须这样：`uvicorn --reload` 会导入模块两次，顶层起 task 就是两份看门狗，
互相杀对方。lifespan 只在真正 serve 时执行一次。
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from gateway import config as cfgmod
from gateway import logbuf
from gateway.actions import RealRuntime
from gateway.api import (agent, chat, chat_tools, chats, hardware as hwapi, images, jobs, misc,
                         model_admin, services, settings, status)
from gateway.backends.llama import LlamaBackend
from gateway.backends.sd import SdBackend
from gateway.infra import InstanceLock, InstanceAlreadyRunning, infra_guard
from gateway.queue import JobHub, JobWorker
from gateway.supervisor.machine import Supervisor
from gateway.supervisor.procs import ProcessManager
from gateway.supervisor.state import Event, can_accept
from gateway.supervisor.vram_log import VramLog
from gateway.supervisor.watchdog import VramWatchdog

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("gateway")

GRACE_SECONDS = 30.0       # 加载期间的显存尖峰：慢机器冷启 sd 会更久，宽限期跟着放大


@asynccontextmanager
async def lifespan(app: FastAPI):
    logbuf.install()                  # 尽早挂上：之后所有 logger 都能被「运行」页看到
    app.state.started_at = time.time()
    cfg = cfgmod.load_config()

    # 硬件自动识别：只跑一次（nvidia-smi + GGUF 元数据 + 系统 API），结果喂给
    # sd_args / llama_args 与显存护栏。失败会降级成"纯 CPU + 更保守的参数"，
    # 不会拦住启动 —— 参数选错的代价是慢，起不来的代价是没法用。
    from gateway import hardware
    hw = hardware.detect(force=True)
    log.info("硬件：%s / %s / 内存 %s MB（可用 %s MB）/ 磁盘剩 %.0f GB",
             (hw["gpu"] or {}).get("name") or "无 NVIDIA 显卡",
             hw["cpu"]["name"], hw["ram"]["total_mib"], hw["ram"]["avail_mib"],
             hw["disk"]["free_gib"])
    # 参数与理由每次都一样，用户看不懂也不该在「运行日志」里占三行；
    # 留在日志文件里给排障用，界面只留上面那行硬件摘要
    log.info("自动参数：chat ngl=%s ctx=%s threads=%s batch=%s/%s fa=%s；sd tile=%s；护栏 %s MiB",
             hw["ngl"], hw["ctx"], hw["threads"], hw["batch"], hw["ubatch"],
             hw["flash_attn"], hw["vae_tile"], hw["guard_mib"], extra={"internal": True})
    for note in hw["notes"]:
        log.info("自动参数说明：%s", note, extra={"internal": True})

    lock = InstanceLock(cfgmod.ROOT / ".lock")
    try:
        lock.acquire()
    except InstanceAlreadyRunning as e:
        log.error("%s", e)
        raise                                  # 拒绝起第二份，不去抢端口

    rep = infra_guard({cfg.runtime.sd_port: cfg.runtime.sd_exe,
                       cfg.runtime.llama_port: cfg.runtime.llama_exe}, cfgmod.NVIDIA_SMI)
    # 什么都没发生时这行是纯噪声（killed=[] skipped=[] released=[]）。
    # 仍然写进 gateway 日志（排障要看），但标 internal：界面日志不显示。
    noise = not (rep.killed or rep.skipped or rep.release_timeout)
    log.info("infra_guard: %s", rep.summary(), extra={"internal": noise})

    procs = ProcessManager(cfg.runtime.logs_dir)
    # 发布版默认不拉起任何模型：先让用户看到硬件与推荐参数，再在「设置」页
    # 自己选模型、点启动。这样打开软件不会一上来就吃满显存，也不会出现
    # 用户没点过却突然在后台加载 16 GB 权重的情况。
    sd = SdBackend(cfg.runtime.sd_port, cfg.runtime.output_dir)
    llama = LlamaBackend(cfg.runtime.llama_port)
    model_want: dict[str, bool] = {"chat": False, "sd": False}
    runtime = RealRuntime(procs=procs, sd=sd, llama=llama,
                          sd_exe=cfg.runtime.sd_exe, llama_exe=cfg.runtime.llama_exe,
                          sd_args=cfgmod.sd_args(cfg), llama_args=cfgmod.llama_args(cfg),
                          want=model_want)
    sup = Supervisor(runtime=runtime, timeouts=cfgmod.TIMEOUTS)

    hub = JobHub()
    worker = JobWorker(hub, sup, sd, cfg, maxsize=cfg.runtime.queue_max_size)
    worker.start()

    ready: dict[str, bool] = {"sd": False, "chat": False}
    grace_until = time.time() + GRACE_SECONDS

    vlog = VramLog(cfg.runtime.logs_dir / "gateway.vram.csv")
    await vlog.start()

    wd = None
    wd_task = None
    vram_guard_on = cfgmod.NVIDIA_SMI is not None
    if not vram_guard_on:
        log.warning("找不到 nvidia-smi：显存采样与护栏在本机不生效（不影响使用，只是不再自动中止作业）")
    else:
        def on_trip(used: int) -> None:
            if time.time() < grace_until:
                log.warning("宽限期内显存 %s MiB 超限，只记录不中止", used)
                return
            # 护栏的唯一语义是"中止正在出图的作业"。没有作业在跑时（CHAT_READY 之类）超限，
            # 只可能是别的 GPU 程序占着显存——这里没有可中止的东西，发 GUARD_TRIPPED 会撞
            # 状态机里未定义的转移（CHAT_READY + guard_tripped），把 supervisor 任务直接打死。
            if not can_accept(sup.state, Event.GUARD_TRIPPED):
                log.warning("显存 %s MiB 超限，但当前状态 %s 无作业可中止：只记录，并重置护栏计数",
                            used, sup.state)
                wd.reset_trip()
                return
            log.error("显存护栏触发：%s MiB，中止作业", used)
            asyncio.create_task(sup.guard_tripped())

        wd = VramWatchdog(cfgmod.NVIDIA_SMI, vlog,
                          guard_mib=hardware.guard_mib(),
                          consecutive=cfgmod.GUARD_CONSECUTIVE)
        wd_task = asyncio.create_task(wd.run(on_trip))

    async def probe_ready() -> None:
        # 默认不启动模型，这里不再探测；模型由用户从「设置」页手动启动，
        # /api/services/{key}/start 会自己等待端口就绪。
        return

    ready_task = None

    app.state.cfg = cfg
    app.state.sup = sup
    app.state.hub = hub
    app.state.worker = worker
    app.state.sd = sd
    app.state.llama = llama
    app.state.procs = procs
    app.state.ready = ready
    app.state.lock = lock
    app.state.grace_until = grace_until
    app.state.runtime = runtime
    app.state.model_want = model_want
    app.state.model_lock = asyncio.Lock()          # 手动启停串行：两个按钮同时点也不打架
    app.state.hardware = hw                        # 探测快照，/api/hardware 读它
    app.state.infra = rep                          # 端口残留/冲突记录，/api/status 读它
    app.state.vram_guard_on = vram_guard_on

    log.info("gateway 启动完成 %s:%s（模型默认关闭，等待用户在「设置」页启动）",
             cfg.runtime.gateway_host, cfg.runtime.gateway_port)
    try:
        yield
    finally:
        log.info("gateway 关闭：停 worker / 看门狗 / supervisor / 子进程")
        worker._task and worker._task.cancel()
        if wd_task:
            wd_task.cancel()
        if ready_task:
            ready_task.cancel()
        for t in (worker._task, wd_task, ready_task):
            if t:
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass
        if wd:
            await wd.stop()
        await vlog.close()
        await sup.shutdown()
        procs.shutdown()
        lock.release()


app = FastAPI(title="灵枢", lifespan=lifespan)
app.include_router(status.router)
app.include_router(hwapi.router)
app.include_router(settings.router)
app.include_router(services.router)
app.include_router(jobs.router)
app.include_router(chat.router)
app.include_router(chat_tools.router)
app.include_router(chats.router)
app.include_router(images.router)
app.include_router(misc.router)
app.include_router(model_admin.router)
app.include_router(agent.router)

STATIC_DIR = cfgmod.ROOT / "static"


@app.get("/output/{relpath:path}")
def output_file(relpath: str, request: Request):
    """作品文件按当前配置的出图目录现算，不再挂静态目录。

    原来是模块级 `StaticFiles(directory=ROOT/output)`：用户把出图路径改到 D 盘（换机器后
    几乎必然要做，C 盘满、或者想放数据盘），改完列表能列出来、点开却 404——因为挂载点
    还指着旧目录。现在每次请求都按 cfg.runtime.output_dir 解析，改路径并重启后立刻正确。
    """
    from fastapi.responses import FileResponse

    root = Path(request.app.state.cfg.runtime.output_dir).resolve()
    try:
        target = (root / relpath).resolve()
        target.relative_to(root)         # ../ 越界一律拒绝
    except (ValueError, OSError):
        return JSONResponse({"error": "not_found"}, status_code=404)
    if not target.is_file():
        return JSONResponse({"error": "not_found"}, status_code=404)
    return FileResponse(target)


@app.get("/")
async def index():
    """根路径直接给界面；没有 static 时退回 JSON 自述。"""
    page = STATIC_DIR / "index.html"
    if page.exists():
        from fastapi.responses import HTMLResponse
        return HTMLResponse(page.read_text(encoding="utf-8"))
    return JSONResponse({"service": "local-ai-gateway", "docs": "/docs",
                         "health": "/health", "status": "/api/status"})


def _model_label(path: Path) -> str:
    """把文件名压成下拉能读的短标签：去掉扩展名，量化档单独用 · 隔开。"""
    raw = path.stem.strip()
    match = re.search(r"(?:^|[-_.])((?:Q\d[\w]*|F16|F32|BF16))$", raw, re.IGNORECASE)
    if match:
        base = raw[:match.start()].replace("_", " ").strip(" -_.")
        label = f"{base} · {match.group(1)}"
    else:
        label = raw.replace("_", " ").strip()
    if len(label) > 58:
        label = label[:55] + "…"
    return label


def _model_roots(cfg: cfgmod.Config) -> list[Path]:
    """模型目录以配置里的模型路径为准；再兜底应用根下的 models/。"""
    roots: list[Path] = []
    for p in (Path(cfg.runtime.model_diffusion).parent,
              Path(cfg.runtime.model_gemma).parent,
              Path(cfgmod.MODELS)):
        try:
            rp = p.resolve()
        except OSError:
            continue
        if rp.exists() and rp not in roots:
            roots.append(rp)
    return roots


def _scan_models(cfg: cfgmod.Config) -> tuple[list[dict], list[dict]]:
    """扫模型目录里的 gguf：名字像出图主干的归 draw，其余可对话的归 chat。

    只按文件名分类，不读 GGUF 元数据——扫目录是每次打开下拉都会做的事，
    读几十 GB 权重头会把界面拖住。mmproj / VAE / LoRA 不是可选主模型，跳过。
    """
    chat: list[dict] = []
    draw: list[dict] = []
    seen: set[str] = set()
    for root in _model_roots(cfg):
        for path in sorted(root.rglob("*.gguf"), key=lambda p: p.name.lower()):
            low = path.name.lower()
            if any(s in low for s in ("mmproj", "vae", "lora", "embedding", "clip")):
                continue
            resolved = str(path.resolve())
            if resolved in seen:
                continue
            seen.add(resolved)
            role = "draw" if any(s in low for s in (
                "qwen-image", "qwen_image", "diffusion", "flux", "sdxl", "stable-diffusion"
            )) else "chat"
            entry = {
                "key": f"{role}:{path.name}",
                "label": _model_label(path),
                "path": resolved,
                "role": role,
                "size_mib": round(path.stat().st_size / (1024 * 1024)),
            }
            (draw if role == "draw" else chat).append(entry)
    return chat, draw


def _selected_model(items: list[dict], path: Path) -> dict | None:
    target = str(path.resolve())
    for item in items:
        if item["path"] == target:
            return item
    return None


def _size_mib(path: Path) -> int:
    """文件多大；文件不在就返回 0（界面用它显示"5.5 GB"，不是判存在性的唯一依据）。"""
    try:
        return round(path.stat().st_size / (1024 * 1024))
    except OSError:
        return 0


_ALLOWED_MODEL_EXT = (".gguf", ".safetensors")


def _model_files(cfg: cfgmod.Config) -> list[dict]:
    """models 目录里所有模型文件（含 VAE / mmproj 这类不能选的），给「管理模型」用。

    in_use 指"config 正引用着它"，这种文件不给删——删了下次启动就直接起不来。
    """
    rt = cfg.runtime
    current = {str(Path(p).resolve()) for p in (
        rt.model_diffusion, rt.model_llm, rt.model_vae, rt.model_gemma, rt.model_mmproj)}
    rows: list[dict] = []
    seen: set[str] = set()
    for root in _model_roots(cfg):
        for path in sorted(root.rglob("*"), key=lambda p: p.name.lower()):
            try:
                if not path.is_file() or path.suffix.lower() not in _ALLOWED_MODEL_EXT:
                    continue
                rp = str(path.resolve())
                size = _size_mib(path)
            except OSError:
                continue
            if rp in seen:
                continue
            seen.add(rp)
            rows.append({"name": path.name, "path": rp, "size_mib": size,
                         "in_use": rp in current})
    return rows


@app.get("/api/models")
def models() -> dict:
    """模型清单：自动扫 models/，并告诉前端当前对话/画图各选了哪个。"""
    cfg = cfgmod.load_config()
    rt = cfg.runtime
    chat, draw = _scan_models(cfg)
    selected_chat = _selected_model(chat, Path(rt.model_gemma))
    selected_draw = _selected_model(draw, Path(rt.model_diffusion))
    return {
        "items": [
            {"key": "diffusion", "label": _model_label(Path(rt.model_diffusion)),
             "path": str(rt.model_diffusion), "role": "出图主干",
             "size_mib": _size_mib(Path(rt.model_diffusion))},
            {"key": "llm", "label": _model_label(Path(rt.model_llm)),
             "path": str(rt.model_llm), "role": "文本编码器 / 视觉",
             "size_mib": _size_mib(Path(rt.model_llm))},
            {"key": "mmproj", "label": _model_label(Path(rt.model_mmproj)),
             "path": str(rt.model_mmproj), "role": "图像投影",
             "size_mib": _size_mib(Path(rt.model_mmproj))},
            {"key": "vae", "label": Path(rt.model_vae).stem.replace("_", " "),
             "path": str(rt.model_vae), "role": "VAE 解码",
             "size_mib": _size_mib(Path(rt.model_vae))},
            {"key": "gemma", "label": _model_label(Path(rt.model_gemma)),
             "path": str(rt.model_gemma), "role": "对话",
             "size_mib": _size_mib(Path(rt.model_gemma))},
        ],
        "chat": chat,
        "draw": draw,
        "files": _model_files(cfg),
        "selected": {
            "chat": {"path": str(Path(rt.model_gemma).resolve()),
                     "label": (selected_chat or {}).get("label", _model_label(Path(rt.model_gemma)))},
            "draw": {"path": str(Path(rt.model_diffusion).resolve()),
                     "label": (selected_draw or {}).get("label", _model_label(Path(rt.model_diffusion)))},
        },
    }


@app.post("/api/models/select")
async def select_model(request: Request) -> JSONResponse:
    """切当前对话/画图模型；正在跑的服务会按新权重重启，关着的只记入配置。"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "bad_request", "detail": "请求体不是 JSON"},
                            status_code=400)
    role = str(body.get("role") or "")
    raw = str(body.get("path") or "")
    if role not in ("chat", "draw"):
        return JSONResponse({"error": "unknown_role", "detail": "role 只能是 chat 或 draw"},
                            status_code=400)
    target = Path(raw).resolve()
    if target.suffix.lower() != ".gguf" or not target.is_file():
        return JSONResponse({"error": "invalid_model", "detail": "只能选模型目录里的 gguf 文件"},
                            status_code=400)
    app_ = request.app
    chat, draw = _scan_models(app_.state.cfg)
    allowed = {m["path"] for m in (chat if role == "chat" else draw)}
    if str(target) not in allowed:
        return JSONResponse({"error": "invalid_model", "detail": "这个文件不是该用途的模型"},
                            status_code=400)
    if app_.state.worker.current is not None or str(app_.state.sup.state) in (
            "PREPARING", "GENERATING", "RELOADING", "DEGRADED"):
        return JSONResponse({"error": "busy", "detail": "正在出图或准备，等当前作业结束再换模型"},
                            status_code=409)
    if not hasattr(app_.state, "model_lock"):
        app_.state.model_lock = asyncio.Lock()
    async with app_.state.model_lock:
        cfg = app_.state.cfg
        runtime = app_.state.runtime
        restart = False
        ok = True
        try:
            if role == "chat":
                cfg.runtime.model_gemma = target
                cfg.save()
                runtime.llama_args = cfgmod.llama_args(cfg)
                if app_.state.model_want.get("chat"):
                    restart = True
                    app_.state.ready["chat"] = False
                    app_.state.grace_until = time.time() + cfgmod.CHAT_READY_TIMEOUT_S
                    await asyncio.to_thread(app_.state.procs.kill, "chat", 8.0)
                    app_.state.procs.spawn("chat", runtime.llama_exe, runtime.llama_args)
                    ok = bool(await asyncio.to_thread(app_.state.llama.wait_ready,
                                                      cfgmod.CHAT_READY_TIMEOUT_S))
                    app_.state.ready["chat"] = ok
            else:
                cfg.runtime.model_diffusion = target
                cfg.save()
                runtime.sd_args = cfgmod.sd_args(cfg)
                if app_.state.model_want.get("sd"):
                    restart = True
                    app_.state.ready["sd"] = False
                    app_.state.grace_until = time.time() + 60.0
                    await asyncio.to_thread(app_.state.procs.kill, "sd", 8.0)
                    app_.state.procs.spawn("sd", runtime.sd_exe, runtime.sd_args)
                    ok = False
                    for _ in range(240):          # 120s 上限：换权重是最慢的一次冷启
                        if await asyncio.to_thread(app_.state.sd.probe):
                            ok = True
                            break
                        await asyncio.sleep(0.5)
                    app_.state.ready["sd"] = ok
        except Exception as e:
            log.error("切换模型失败: %s", e)
            return JSONResponse({"error": "switch_failed", "detail": str(e)}, status_code=500)
    body = {"ok": ok, "role": role, "path": str(target), "restarted": restart,
            "items": services.snapshot(app_)}
    if not ok:
        return JSONResponse({**body, "error": "start_failed",
                             "detail": "新模型没起来，看 logs 里的服务日志"}, status_code=500)
    return JSONResponse(body)


if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
