"""故障注入第 1 条：出图中外部 taskkill /F 杀 sd-server。

要证明的是"异常被检测到"，不是"110s 超时兜底"：
- 杀掉后应在极短时间内出现 GENERATING --image_failed--> DEGRADED
- 作业 error 必须是 image_failed
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from gateway.actions import RealRuntime, vram_used_mib  # noqa: E402
from gateway.backends.llama import LlamaBackend  # noqa: E402
from gateway.backends.sd import SdBackend  # noqa: E402
from gateway.config import (  # noqa: E402
    CHAT_PORT, LLAMA_EXE, LOGS, NVIDIA_SMI, OUTPUT, SD_EXE, SD_PORT,
    TIMEOUTS, llama_args, sd_args,
)
from gateway.supervisor.machine import JobContext, Supervisor  # noqa: E402
from gateway.supervisor.procs import ProcessManager  # noqa: E402
from gateway.supervisor.vram_log import VramLog  # noqa: E402
from gateway.supervisor.watchdog import VramWatchdog  # noqa: E402


async def wait_any(fn, timeout: float) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if await asyncio.to_thread(fn):
            return True
        await asyncio.sleep(0.4)
    return False


async def main() -> int:
    job_id = "f" + datetime.now().strftime("%H%M%S")
    procs = ProcessManager(LOGS)
    sd = SdBackend(SD_PORT, OUTPUT)
    llama = LlamaBackend(CHAT_PORT)
    procs.spawn("sd", SD_EXE, sd_args())
    procs.spawn("chat", LLAMA_EXE, llama_args())
    if not (await wait_any(sd.probe, 30) and await wait_any(llama.health, 40)):
        print("服务未就绪")
        procs.shutdown()
        return 1

    vlog = VramLog(LOGS / "jobs" / f"{job_id}.vram.csv")
    await vlog.start()
    wd = VramWatchdog(NVIDIA_SMI, vlog, guard_mib=7700, consecutive=10)
    wd_task = asyncio.create_task(wd.run(lambda used: None))

    runtime = RealRuntime(procs=procs, sd=sd, llama=llama, sd_exe=SD_EXE,
                          llama_exe=LLAMA_EXE, sd_args=sd_args(), llama_args=llama_args())
    sup = Supervisor(runtime=runtime, timeouts=TIMEOUTS)
    ctx = JobContext(job_id=job_id, prompt="a red apple on a wooden table, soft window light")
    print(f"[1/4] 提交 job={job_id} sd_pid={procs.procs['sd'].pid}")
    await sup.submit(ctx)

    # 等真正进入出图（采样已开始）再杀
    end = time.time() + 30
    while time.time() < end and str(sup.state) != "GENERATING":
        await asyncio.sleep(0.2)
    await asyncio.sleep(6)

    pid = procs.procs["sd"].pid
    t_kill = time.perf_counter()
    subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True, text=True, check=False)
    print(f"[2/4] 已外部强杀 sd pid={pid}")

    # 等它进 DEGRADED（不等到恢复完成，先看检测延迟）
    end = time.time() + 60
    while time.time() < end and str(sup.state) == "GENERATING":
        await asyncio.sleep(0.1)
    t_detect = time.perf_counter() - t_kill
    print(f"[3/4] 检测到异常用时 {t_detect:.2f}s state={sup.state}")

    # 等恢复正常
    end = time.time() + 120
    while time.time() < end and str(sup.state) not in ("CHAT_READY", "FAILED"):
        await asyncio.sleep(0.3)

    await wd.stop()
    try:
        await asyncio.wait_for(wd_task, timeout=5)
    except Exception:
        pass
    await vlog.close()
    await sup.shutdown()
    procs.shutdown()

    print("\n=== 轨迹 ===")
    prev = sup.timeline[0]["ts"] if sup.timeline else 0
    for it in sup.timeline:
        print(f"  +{it['ts'] - prev:>6.2f}s  {it['from']:<11} --{it['event']:<16}--> {it['to']:<11} [{it['action']}]")
        prev = it["ts"]

    events = [it["event"] for it in sup.timeline]
    guard_error = sup.ctx.error
    print(f"\n  作业 error={guard_error} status={sup.ctx.status}")
    print(f"  csv 行数={vlog.written} dropped={vlog.dropped}")

    ok = ("image_failed" in events and t_detect < 10 and guard_error == "image_failed"
          and str(sup.state) == "CHAT_READY" and vlog.written > 0 and vlog.dropped == 0)
    print(f"\n验收: {'通过' if ok else '不通过'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
