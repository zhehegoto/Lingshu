import asyncio
import logging
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from gateway import config as cfgmod
from gateway import hardware
from gateway.config import ConfigError, LockedField

router = APIRouter()
log = logging.getLogger("api.settings")


async def _restart_sd(app) -> bool:
    """真正重启 sd-server 让 vae_tile 生效（之前只回 restart_sd: true 不动作，等于切了不生效）。"""
    procs, sd, ready, cfg = app.state.procs, app.state.sd, app.state.ready, app.state.cfg
    ready["sd"] = False
    app.state.grace_until = time.time() + 15.0        # 重启期间看门狗只记录不判失败
    try:
        await asyncio.to_thread(procs.kill, "sd", 8.0)
        procs.spawn("sd", cfg.runtime.sd_exe, cfgmod.sd_args(cfg))
        # 上限跟着"首次冷启"走：慢机器换 vae_tile 后也要能等到就绪，不能再按 20s 一刀切
        for _ in range(int(cfgmod.SD_START_TIMEOUT_S / 0.5)):
            if await asyncio.to_thread(sd.probe):
                ready["sd"] = True
                log.warning("sd-server 已用新参数重启（vae_tile=%s）", cfg.settings.vae_tile)
                return True
            await asyncio.sleep(0.5)
    except Exception as e:
        log.error("sd-server 重启失败: %s", e)
    log.error("sd-server 重启后未就绪（vae_tile=%s）", cfg.settings.vae_tile)
    return False


async def _restart_chat(app) -> bool:
    """n_ctx 改了必须重启 llama-server 才生效。

    只重启对话服务，不动 sd、不取消作业：n_ctx 是启动参数，改完的老进程还在用旧值，
    不重启等于"改了个数字给人看"。
    """
    procs, rt, ready, cfg = (app.state.procs, app.state.runtime,
                             app.state.ready, app.state.cfg)
    if not getattr(app.state, "model_want", {}).get("chat", False):
        # 用户手动关了对话模型：不要因为改设置就把它偷偷拉起来
        log.warning("对话模型处于手动关闭状态，跳过重启（下次启动用新 n_ctx=%s）", cfg.settings.n_ctx)
        return False
    ready["chat"] = False
    app.state.grace_until = time.time() + cfgmod.CHAT_READY_TIMEOUT_S   # 大上下文加载更慢
    try:
        await asyncio.to_thread(procs.kill, "chat", 8.0)
        rt.llama_args = cfgmod.llama_args(cfg)         # 后续状态机重启也拿新参数
        procs.spawn("chat", rt.llama_exe, rt.llama_args)
        ok = bool(await asyncio.to_thread(app.state.llama.wait_ready,
                                          cfgmod.CHAT_READY_TIMEOUT_S))
        ready["chat"] = ok
        if ok:
            log.warning("llama-server 已用新参数重启（n_ctx=%s max_tokens=%s）",
                        cfg.settings.n_ctx, cfg.settings.max_tokens)
            return True
    except Exception as e:
        log.error("对话服务重启失败: %s", e)
    log.error("对话服务重启后未就绪（n_ctx=%s）", cfg.settings.n_ctx)
    return False


def _settings_payload(cfg) -> dict:
    """GET/PUT 共用：配置值 + 本机推荐值 + 当前上下文的显存估算。"""
    pub = cfg.public_settings()
    pub["recommended"] = hardware.recommend()
    pub["ctx_estimate"] = hardware.ctx_estimate(pub["values"]["n_ctx"])
    return pub


@router.get("/api/settings")
async def get_settings(request: Request) -> dict:
    return _settings_payload(request.app.state.cfg)


@router.put("/api/settings")
async def put_settings(request: Request, patch: dict) -> JSONResponse:
    """只接受 B 层 5 个字段。locked → 409；非法值/未知字段 → 400。"""
    cfg = request.app.state.cfg
    try:
        result = cfg.update(patch)
    except LockedField as e:
        return JSONResponse({"error": "locked", "detail": str(e)}, status_code=409)
    except ConfigError as e:
        return JSONResponse({"error": "invalid", "detail": str(e)}, status_code=400)

    pub = _settings_payload(cfg)
    applied = result["changed"]
    body = {
        "ok": True,
        "changed": applied,
        "effects": result["effects"],
        "restart_sd": result["restart_sd"],
        "restart_chat": result.get("restart_chat", False),
        "values": pub["values"],                      # 这里只放字段值
        "fields": pub["fields"],                      # 与 values 平级
        "locked": pub["locked"],
        "max_tokens_effective_no_think": pub["max_tokens_effective_no_think"],
        "unverified_options": pub["unverified_options"],
        "recommended": pub["recommended"],
        "ctx_estimate": pub["ctx_estimate"],
    }
    if result["restart_sd"]:
        body["note"] = "vae_tile 已保存，正在重启出图服务"
        log.warning("vae_tile 变更，重启 sd-server")
        body["restart_sd_now"] = True
        request.app.state._sd_restart_task = asyncio.create_task(_restart_sd(request.app))
    if result.get("restart_chat"):
        if not getattr(request.app.state, "model_want", {}).get("chat", False):
            # 手动关掉的对话模型不因改设置被拉起，"重启"也就无从谈起
            body["note"] = "上下文长度已保存；对话模型当前是关闭状态，下次启动时生效"
            body["restart_chat_now"] = False
        else:
            body["note"] = "上下文长度已保存，正在重启对话服务（约 20s）"
            log.warning("n_ctx 变更，重启 llama-server")
            body["restart_chat_now"] = True
            request.app.state._chat_restart_task = asyncio.create_task(_restart_chat(request.app))
    return JSONResponse(body)


@router.put("/api/settings/paths")
async def put_paths(request: Request, patch: dict) -> JSONResponse:
    """部署路径：独立端点、独立校验（绝对路径/非盘符根/非系统目录/可写）。

    effect 恒为 restart_gateway：本次响应不会热切换目录，旧文件也不搬。
    """
    cfg = request.app.state.cfg
    try:
        result = cfg.update_paths(patch)
    except ConfigError as e:
        return JSONResponse({"error": "invalid_path", "detail": str(e)}, status_code=400)

    pub = cfg.public_settings()
    body = {
        "ok": True,
        "changed": result["changed"],
        "effects": result["effects"],
        "restart_gateway": result["restart_gateway"],
        "paths": pub["paths"],
    }
    if result["restart_gateway"]:
        body["note"] = "路径已写入 config.yaml，重启 gateway 后生效；旧目录里的文件不会自动搬迁"
        log.warning("部署路径变更（需重启）：%s", result["changed"])
    return JSONResponse(body)
