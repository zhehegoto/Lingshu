"""模型服务的手动启停（顶栏按钮背后的三个端点）。

层级分工：
- gateway(8000) 常驻，不在这套开关里——关了它连界面都没了；
- 能被开关的只有两个推理服务：
    chat → llama-server(8099)  对话
    sd   → sd-server(8081)     出图

两条约定（都是产品决定，不是实现细节）：

1. **关闭前先取消作业。** 出图中直接杀 sd 会留半张图 + 一条 image_failed 记录，
   用户选的是"先取消再关"，所以走到这里先 cancel，再等状态机回稳定态，最后才停进程。

2. **意图要记住。** 关掉的模型不能被作业流程偷偷拉回来（出完图要重载对话模型、
   取消作业要重启 sd）——`app.state.model_want` 是唯一真相，actions.py 里
   load_chat / kill_sd / cleanup_restart 都查它。
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from gateway import config as cfgmod
from gateway import hardware
from gateway.supervisor.state import State
from gateway.supervisor.procs import file_tail

router = APIRouter()
log = logging.getLogger("api.services")

NAMES = {"chat": "对话模型", "sd": "画图模型"}
UNSTABLE = (State.PREPARING, State.GENERATING, State.RELOADING, State.DEGRADED)

# 每个服务起来之前必须"真的存在"的文件。原来只由 procs.spawn 检查 exe，模型缺失时
# sd-server 会启动然后静默读不到权重，用户等 30 秒只看到"未就绪"——原因在 logs 里。
def _required_files(cfg, key: str) -> list[tuple[str, Path]]:
    rt = cfg.runtime
    if key == "chat":
        return [("对话模型", Path(rt.model_gemma)), ("llama-server", Path(rt.llama_exe))]
    return [("出图主干", Path(rt.model_diffusion)),
            ("文本编码器", Path(rt.model_llm)),
            ("VAE", Path(rt.model_vae)),
            ("sd-server", Path(rt.sd_exe))]


def _missing_files(cfg, key: str) -> list[str]:
    out: list[str] = []
    for label, p in _required_files(cfg, key):
        try:
            ok = p.is_file() and p.stat().st_size > 0
        except OSError:
            ok = False
        if not ok:
            out.append(f"{label}：{p}")
    return out


def _spawn_error_detail(app, key: str) -> str:
    """启动失败时把子进程日志最后几行带上，别只回一句"未就绪"。"""
    logs = Path(app.state.cfg.runtime.logs_dir)
    tail = (file_tail(logs / f"{key}.err.log", 8) or file_tail(logs / f"{key}.log", 8) or "").strip()
    return tail[-800:] if tail else ""


def snapshot(app) -> dict[str, dict]:
    ready = getattr(app.state, "ready", {})
    procs = app.state.procs
    cfg = app.state.cfg
    want = getattr(app.state, "model_want", {})
    ports = {"chat": cfg.runtime.llama_port, "sd": cfg.runtime.sd_port}
    items = {}
    for key in ("chat", "sd"):
        proc = procs.procs.get(key)
        alive = bool(proc is not None and proc.popen.poll() is None)
        items[key] = {
            "name": NAMES[key],
            "ready": bool(ready.get(key)),
            "running": alive,
            "pid": proc.pid if alive else None,
            "port": ports[key],
            "want": bool(want.get(key, False)),
        }
    return items


async def _settle(sup, timeout: float) -> bool:
    """等状态机离开 PREPARING/GENERATING/RELOADING/DEGRADED。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if sup.state not in UNSTABLE:
            return True
        await asyncio.sleep(0.3)
    log.warning("关闭模型：等状态机稳定超时（当前 %s）", sup.state)
    return False


@router.get("/api/services")
async def list_services(request: Request) -> dict:
    return {"items": snapshot(request.app)}


@router.post("/api/services/{key}/start")
async def start_service(request: Request, key: str) -> JSONResponse:
    if key not in NAMES:
        return JSONResponse({"error": "unknown_service", "detail": f"未知服务 {key}"}, status_code=404)
    app = request.app
    rt = app.state.runtime
    if not hasattr(app.state, "model_lock"):
        app.state.model_lock = asyncio.Lock()

    async with app.state.model_lock:
        # 预检：缺文件/内存不够就直接说清楚，不进"启动→等 3 分钟→未就绪"的坑
        missing = _missing_files(app.state.cfg, key)
        if missing:
            log.error("启动 %s 失败：缺文件 %s", NAMES[key], missing)
            return JSONResponse({"error": "missing_files",
                                 "detail": "缺少文件，无法启动：\n" + "\n".join(missing),
                                 "items": snapshot(app)}, status_code=409)
        if key == "sd":
            plan = hardware.current()
            total_ram = int((plan.get("ram") or {}).get("total_mib") or 0)
            if total_ram and total_ram < cfgmod.SD_MIN_RAM_MIB:
                log.error("启动出图模型被拒：内存 %s MB < %s MB", total_ram, cfgmod.SD_MIN_RAM_MIB)
                return JSONResponse(
                    {"error": "ram_too_small",
                     "detail": f"本机内存 {total_ram} MB，低于出图模型要求的 "
                               f"{cfgmod.SD_MIN_RAM_MIB} MB（权重+编码器要常驻内存，会换页卡死）。",
                     "items": snapshot(app)}, status_code=409)

        app.state.model_want[key] = True
        app.state.grace_until = time.time() + 15.0     # 加载期间显存尖峰不算护栏越界
        log.warning("手动启动 %s", NAMES[key])
        if key == "chat":
            await rt.load_chat()
            ok = bool(await asyncio.to_thread(app.state.llama.health))
        else:
            ok = bool(await rt.start_sd())
        app.state.ready[key] = ok

    body = {"ok": ok, "name": NAMES[key], "state": str(app.state.sup.state),
            "items": snapshot(app)}
    if ok:
        return JSONResponse(body)
    tail = _spawn_error_detail(app, key)
    body["detail"] = f"{NAMES[key]} 启动失败，看 logs/{key}.err.log" + (f"\n\n{tail}" if tail else "")
    body["error"] = "start_failed"
    return JSONResponse(body, status_code=500)


@router.post("/api/services/{key}/stop")
async def stop_service(request: Request, key: str) -> JSONResponse:
    if key not in NAMES:
        return JSONResponse({"error": "unknown_service", "detail": f"未知服务 {key}"}, status_code=404)
    app = request.app
    rt = app.state.runtime
    worker, sup = app.state.worker, app.state.sup
    if not hasattr(app.state, "model_lock"):
        app.state.model_lock = asyncio.Lock()

    async with app.state.model_lock:
        app.state.model_want[key] = False          # 先改意图：取消路径里的重启会查它
        log.warning("手动关闭 %s", NAMES[key])

        current = worker.current
        if current is not None:
            # PREPARING 里 CANCEL 没有定义（转移表里只有 GENERATING + cancel），
            # 先等它进 GENERATING（最多 20s），进不去就按"没有可取消的作业"处理。
            deadline = time.time() + 20.0
            while sup.state is State.PREPARING and time.time() < deadline:
                await asyncio.sleep(0.3)
            if sup.state is State.GENERATING:
                log.warning("关闭 %s 前先取消作业 %s", NAMES[key], current.id)
                await worker.cancel(current.id)
            await _settle(sup, 90.0)

        # 只清出图队列：关掉 sd 之后这些作业必然失败，留着只会让前端看到"排队中"不动。
        # 关对话模型不清队列——出图只用 sd，恢复对话那一步会因 want=False 跳过。
        drained = worker.drain("sd_stopped") if key == "sd" else []
        if key == "chat":
            await rt.unload_chat()
        else:
            await rt.stop_sd()
        app.state.ready[key] = False

    body = {"ok": True, "name": NAMES[key], "state": str(sup.state),
            "drained": drained, "items": snapshot(app)}
    return JSONResponse(body)
