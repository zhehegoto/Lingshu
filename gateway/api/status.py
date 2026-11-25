import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from gateway import config as cfgmod
from gateway import hardware
from gateway.api.services import snapshot as services_snapshot
from gateway.supervisor.state import State

router = APIRouter()
log = logging.getLogger("api.status")


def svc_status(ready: dict, key: str) -> str:
    return "ok" if ready.get(key) else "off"


@router.get("/health")
async def health(request: Request) -> JSONResponse:
    app = request.app
    ready = getattr(app.state, "ready", {})
    sup = app.state.sup
    body = {
        "gateway": "ok",
        "sd": svc_status(ready, "sd"),
        "chat": svc_status(ready, "chat"),
        "state": str(sup.state),
        "models_ready": bool(ready.get("sd") and ready.get("chat")),
    }
    # gateway 活着就是 200：模型默认关闭时 /health 不能报 503，
    # 否则外部编排/桌面壳会误以为服务没起来。模型是否就绪看 models_ready。
    return JSONResponse(body, status_code=200)


@router.get("/api/status")
async def status(request: Request) -> dict:
    app = request.app
    ready = getattr(app.state, "ready", {})
    sup = app.state.sup
    worker = app.state.worker
    cfg = app.state.cfg
    sd = app.state.sd

    # 跨页也要能看到进度：直接读 sd /progress，不依赖某个页面是否订阅了 SSE
    progress = None
    cur = worker.current
    if cur is not None and str(sup.state) == "GENERATING":
        try:
            import asyncio
            live = await asyncio.to_thread(sd.progress)
            st = (live or {}).get("state") or {}
            step = st.get("sampling_step")
            total = st.get("sampling_steps") or cur.steps
            if step is not None and total:
                progress = {"step": int(step), "total": int(total),
                            "percent": round(min(1.0, step / total) * 100),
                            "job_id": cur.id}
        except Exception:
            progress = None

    rep = getattr(app.state, "infra", None)
    conflicts = [
        {"port": o.port, "pid": o.pid, "name": o.name, "exe": o.exe_path}
        for o in (getattr(rep, "skipped", []) or [])
    ]
    return {
        "state": str(sup.state),                 # CHAT_READY 时前端才自动发排队消息
        "ready": {"sd": bool(ready.get("sd")), "chat": bool(ready.get("chat"))},
        "services": services_snapshot(app),      # 顶栏两个开关按钮看这个（含 pid / port / 意图）
        "ports": {"gateway": cfg.runtime.gateway_port,
                  "sd": cfg.runtime.sd_port, "chat": cfg.runtime.llama_port},
        "port_conflicts": conflicts,             # 启动时发现"端口被别人占着"，界面上要说出来
        "vram_guard": {"enabled": bool(getattr(app.state, "vram_guard_on", False)),
                       "nvidia_smi": str(cfgmod.NVIDIA_SMI) if cfgmod.NVIDIA_SMI else None},
        "guard": {"peak_guard_mib": hardware.guard_mib(),
                  "budget_mib": hardware.budget_mib(),
                  "consecutive": cfgmod.GUARD_CONSECUTIVE,
                  "interval_ms": cfgmod.SAMPLE_INTERVAL_MS},
        "queue": {"depth": worker.depth(), "current": worker.current.id if worker.current else None},
        "progress": progress,
        "pids": {k: p.pid for k, p in request.app.state.procs.procs.items()
                 if p is not None and p.popen.poll() is None},
        "can_accept_chat": sup.state is State.CHAT_READY,
        "vram_guard_tripped": bool(worker.current and (worker.current.error == "vram_guard_tripped")),
        "snapshot": sup.snapshot(),
        "settings": cfg.public_settings()["values"],
    }
