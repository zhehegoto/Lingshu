"""硬件自动识别的对外入口。

只读：把"本机是什么 + 自动选了哪些参数"吐给设置页。
重新探测只重算参数，不会替你重启已在跑的服务 —— 重启要用户按顶栏的开关，
因为那是会中断作业的动作，不该由一次 GET/POST 探测顺手做掉。
"""
from __future__ import annotations

from fastapi import APIRouter

from gateway import config as cfgmod
from gateway import hardware

router = APIRouter(prefix="/api", tags=["hardware"])


def _payload(note: str | None = None) -> dict:
    body = hardware.summary()
    live = hardware.live_free()
    if body.get("gpu"):
        body["gpu"]["free_mib"] = live["gpu_free_mib"]
    if body.get("ram"):
        body["ram"]["avail_mib"] = live["ram_avail_mib"]
    body["live"] = live
    tile_cfg = cfgmod.load_config().settings.vae_tile
    body["applied"] = {
        "chat_args": hardware.chat_params(),
        "sd_tile": hardware.sd_tile(tile_cfg),
        "sd_tile_configured": tile_cfg,
        "guard_mib": hardware.guard_mib(),
        "budget_mib": hardware.budget_mib(),
    }
    if note:
        body["note"] = note
    return body


@router.get("/hardware")
def get_hardware() -> dict:
    """探测快照。设置页每次打开读它，不重新跑 nvidia-smi。"""
    return _payload()


@router.post("/hardware/redetect")
def redetect() -> dict:
    """强制重新探测（换显卡/插外接屏/改显存上限之后用）。"""
    hardware.detect(force=True)
    return _payload("已重新探测。正在跑的服务仍用旧参数，要生效请在顶栏重启对应模型。")
