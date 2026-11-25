"""显存看门狗。

口径（第 5 步定案）：
- 采样：单进程 `nvidia-smi -lms 100` 流式读 stdout，**不 spawn-per-sample**（Windows 上单次
  spawn 50–150ms CPU，100ms 一次会把 CPU 打满并干扰出图）。
- 判定：**连续 10 点（≈1 秒）都 > PEAK_GUARD_MIB 才触发**，单点毛刺放行。
- 标记：`ok` / `warn`（超限但未达 10 点，或落在 7700–7900 观察带）/ `trip`（触发那一行）。
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import time
from pathlib import Path
from typing import Awaitable, Callable

from .procs import JobObject, JobObjectError
from .vram_log import VramLog

log = logging.getLogger("watchdog")


class VramWatchdog:
    def __init__(
        self,
        nvidia_smi: Path | None,
        vram_log: VramLog,
        guard_mib: int = 7700,
        consecutive: int = 10,
        warn_band: tuple[int, int] | None = None,
        interval_ms: int = 100,
    ) -> None:
        # 采样器路径可能是 None（Windows 不在 C 盘 / 精简系统 / 32 位解释器）。
        # 那种机器上没有显存数据可采：run() 直接不进循环，而不是让 task 抛异常死掉——
        # 死掉的 task 会让"护栏生效中"变成一个骗人的状态。
        self.nvidia_smi = Path(nvidia_smi) if nvidia_smi else None
        self.vram_log = vram_log
        self.guard_mib = guard_mib
        self.consecutive = consecutive
        # 观察带原来写死 7700–7900（按 8 GB 卡标定）。护栏已经是按实际显存算的，
        # 观察带跟着走：护栏下方 100 MiB 处开始只标记，不再参与触发。
        self.warn_band = warn_band or (max(0, guard_mib - 100), guard_mib)
        self.interval_ms = interval_ms

        self.tripped = False
        self.trip_sample: tuple[float, int] | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._job: JobObject | None = None
        self._task: asyncio.Task | None = None
        self._over_run = 0
        self.max_used = 0

    # ---------- 采样判定（纯逻辑，可单测） ----------

    def classify(self, used: int) -> str:
        over = used > self.guard_mib
        self._over_run = self._over_run + 1 if over else 0

        if self._over_run >= self.consecutive and not self.tripped:
            return "trip"
        if over:
            return "warn"
        lo, hi = self.warn_band
        if lo <= used <= hi:
            return "warn"                     # 观察带：只标记，不参与触发
        return "ok"

    # ---------- 运行 ----------

    def reset_trip(self) -> None:
        """把"已触发"状态清回去，让下一次作业仍能被护栏保护。

        为什么需要：护栏在空闲态（没有作业）超限时不会中止任何东西，只是别的 GPU 程序
        占了显存。若不清 `tripped`，这一个布尔值会一直为 True，等真开始出图时护栏反而不响。
        """
        self.tripped = False
        self.trip_sample = None
        self._over_run = 0

    async def run(self, on_trip: Callable[[int], Awaitable[None] | None]) -> None:
        started = time.time()
        if self.nvidia_smi is None:
            log.warning("没有可用的 nvidia-smi：显存采样停用，护栏在本机不生效（不是故障，是这台机器没装/找不到）")
            self.vram_log.put_nowait((started, 0, "off"))
            return
        # nvidia-smi -lms 是长驻子进程：也放进 KILL_ON_JOB_CLOSE 的 Job，
        # 这样 gateway 正常退出或被硬杀都不会留下一串 nvidia-smi 黑窗。
        try:
            self._job = JobObject()
        except JobObjectError as e:
            log.warning("显存采样 Job 创建失败（继续采样，但退出时可能留子进程）：%s", e)
        try:
            self._proc = await asyncio.create_subprocess_exec(
                str(self.nvidia_smi),
                "--query-gpu=memory.used",
                "--format=csv,noheader,nounits",
                "-lms", str(self.interval_ms),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, ValueError) as e:
            # 路径存在但执行不了（驱动装坏、文件被杀软隔离）：记录并停用护栏，别把 task 打死
            log.warning("nvidia-smi 启动失败，显存采样停用：%s", e)
            self.vram_log.put_nowait((time.time(), 0, "off"))
            return
        if self._job:
            try:
                self._job.assign(self._proc.pid)
            except JobObjectError as e:
                log.warning("显存采样进程加入 Job 失败：%s", e)
        assert self._proc.stdout is not None
        try:
            async for raw in self._proc.stdout:
                line = raw.decode("utf-8", "replace").strip()
                if not line.isdigit():
                    continue
                used = int(line)
                ts = time.time()
                self.max_used = max(self.max_used, used)
                flag = self.classify(used)
                self.vram_log.put_nowait((ts, used, flag))

                if flag == "trip" and not self.tripped:
                    self.tripped = True
                    self.trip_sample = (ts, used)
                    log.error("护栏触发：used=%d MiB > %d，连续 %d 点（t=%.1fs）",
                              used, self.guard_mib, self.consecutive, ts - started)
                    res = on_trip(used)
                    if asyncio.iscoroutine(res):
                        await res
        finally:
            await self.stop()

    async def stop(self) -> None:
        if self._proc and self._proc.returncode is None:
            try:
                self._proc.terminate()
                await asyncio.wait_for(self._proc.wait(), timeout=3)
            except Exception:
                pass
        if self._job:
            self._job.close()
            self._job = None
        self._proc = None
