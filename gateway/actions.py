"""真实动作实现：把 supervisor 抽象动作映射到子进程与 HTTP。

边界：本模块 import procs/backends，supervisor/ 不 import 它。
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import time
from pathlib import Path

from .backends.llama import LlamaBackend
from .backends.sd import SdBackend
from .config import CHAT_READY_TIMEOUT_S, NVIDIA_SMI, SD_START_TIMEOUT_S, VRAM_IDLE_SD
from .supervisor.machine import JobContext
from .supervisor.procs import ProcessManager

log = logging.getLogger("runtime")


def vram_used_mib() -> int | None:
    if NVIDIA_SMI is None:
        return None          # 没有采样器：所有"等显存回落"的等待都会直接超时，这是预期行为
    try:
        out = subprocess.run(
            [str(NVIDIA_SMI), "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=8, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout.strip().splitlines()
        return int(out[0]) if out else None
    except Exception:
        return None


def wait_vram_below(threshold: int, timeout: float = 6.0, interval: float = 0.4) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        used = vram_used_mib()
        if used is not None and used < threshold:
            return True
        time.sleep(interval)
    return False


class RealRuntime:
    def __init__(
        self,
        procs: ProcessManager,
        sd: SdBackend,
        llama: LlamaBackend,
        sd_exe: Path,
        llama_exe: Path,
        sd_args: list[str],
        llama_args: list[str],
        unload_vram_below: int = 2000,
        want: dict[str, bool] | None = None,
    ) -> None:
        self.procs = procs
        self.sd = sd
        self.llama = llama
        self.sd_exe = sd_exe
        self.llama_exe = llama_exe
        self.sd_args = sd_args
        self.llama_args = llama_args
        self.unload_vram_below = unload_vram_below
        # 用户意图（顶栏两个按钮写的）。手动关掉的模型，作业流程里不许偷偷拉起来：
        # 否则"我明明关了对话模型"，出完一张图它又回来了。
        self.want = want if want is not None else {}

    # ---------- 对话侧 ----------

    async def unload_chat(self) -> None:
        if "chat" in self.procs.procs and self.procs.procs["chat"].alive():
            await asyncio.to_thread(self.procs.kill, "chat", 5.0)
        ok = await asyncio.to_thread(wait_vram_below, self.unload_vram_below, 6.0)
        used = await asyncio.to_thread(vram_used_mib)
        log.info("Gemma 卸载完成 vram=%s MiB below=%s ok=%s", used, self.unload_vram_below, ok)

    async def load_chat(self) -> bool:
        if not self.want.get("chat", False):
            log.info("对话模型被手动关闭，跳过加载（状态机照常前进）")
            return True
        if "chat" in self.procs.procs and self.procs.procs["chat"].alive():
            if await asyncio.to_thread(self.llama.health):
                return True
            await asyncio.to_thread(self.procs.kill, "chat", 5.0)
        self.procs.spawn("chat", self.llama_exe, self.llama_args)
        ok = await asyncio.to_thread(self.llama.wait_ready, CHAT_READY_TIMEOUT_S)
        log.info("Gemma 加载完成 ok=%s vram=%s", ok, await asyncio.to_thread(vram_used_mib))
        return ok

    async def force_kill_chat(self) -> None:
        await asyncio.to_thread(self.procs.kill, "chat", 5.0)

    # ---------- 出图侧 ----------

    async def submit_image(self, ctx: JobContext) -> str:
        t0 = time.perf_counter()
        if ctx.kind == "mask" and ctx.init_b64 and ctx.mask_b64:
            res = await asyncio.to_thread(
                self.sd.img2img_mask,
                job_id=ctx.job_id,
                prompt=ctx.prompt,
                negative_prompt=ctx.negative,
                init_b64=ctx.init_b64,
                mask_b64=ctx.mask_b64,
                width=ctx.width,
                height=ctx.height,
                steps=ctx.steps,
                seed=ctx.seed,
                strength=ctx.strength,
            )
        else:
            res = await asyncio.to_thread(
                self.sd.txt2img,
                job_id=ctx.job_id,
                prompt=ctx.prompt,
                negative_prompt=ctx.negative,
                width=ctx.width,
                height=ctx.height,
                steps=ctx.steps,
                seed=ctx.seed,
                batch_count=1,
            )
        log.info("出图完成 %.1fs → %s", time.perf_counter() - t0, res["paths"])
        return res["paths"][0]

    async def kill_sd(self) -> None:
        """杀 sd-server 并（默认）立即重启（取消路径：契约要求 1s 级恢复）。

        用户手动关了出图模型时只杀不起——否则"取消作业"会把它又拉回来。
        """
        await asyncio.to_thread(self.procs.kill, "sd", 5.0)
        if not self.want.get("sd", False):
            log.info("出图模型被手动关闭，取消路径不再重启 sd-server")
            return
        await asyncio.sleep(0.2)
        self.procs.spawn("sd", self.sd_exe, self.sd_args)
        for _ in range(60):                  # 30s 上限：机械盘/杀软机器上冷启就是要这么久
            if await asyncio.to_thread(self.sd.probe):
                return
            await asyncio.sleep(0.5)
        raise RuntimeError("sd-server 重启后未就绪")

    async def stop_sd(self) -> None:
        """只杀不重启（超时/护栏路径：交给 cleanup_restart 统一拉起）。"""
        await asyncio.to_thread(self.procs.kill, "sd", 5.0)
        await asyncio.to_thread(wait_vram_below, 2000, 6.0)

    async def cleanup_restart(self) -> bool:
        """DEGRADED 的清理：两个服务全重启，返回是否都就绪。

        出图模型被手动关闭时只清不启，返回 True 让状态机回到 CHAT_READY
        （CHAT_READY 的语义是"可以接下一个作业"，不是"sd 在显存里"）。
        """
        log.warning("进入清理流程：重启 sd-server 与 llama-server")
        for name in ("sd", "chat"):
            try:
                await asyncio.to_thread(self.procs.kill, name, 5.0)
            except Exception:
                pass
        await asyncio.to_thread(wait_vram_below, 2000, 6.0)
        if not self.want.get("sd", False):
            log.info("出图模型被手动关闭，清理流程不再拉起 sd-server")
            return True
        self.procs.spawn("sd", self.sd_exe, self.sd_args)
        sd_ok = False
        for _ in range(120):                 # 60s 上限
            if await asyncio.to_thread(self.sd.probe):
                sd_ok = True
                break
            await asyncio.sleep(0.5)
        return sd_ok

    # ---------- 手动开关（顶栏按钮 → /api/services） ----------

    async def start_sd(self, timeout: float = SD_START_TIMEOUT_S) -> bool:
        """把 sd-server 拉起来并等它就绪。已经在跑就直接探活。"""
        if await asyncio.to_thread(self.sd.probe):
            return True
        if "sd" in self.procs.procs and self.procs.procs["sd"].alive():
            await asyncio.to_thread(self.procs.kill, "sd", 5.0)   # 进程在但没就绪：先清干净
        self.procs.spawn("sd", self.sd_exe, self.sd_args)
        deadline = time.time() + timeout
        while time.time() < deadline:
            if await asyncio.to_thread(self.sd.probe):
                log.info("sd-server 手动启动完成 vram=%s", await asyncio.to_thread(vram_used_mib))
                return True
            await asyncio.sleep(0.4)
        log.error("sd-server 手动启动 %.0fs 内未就绪", timeout)
        return False
