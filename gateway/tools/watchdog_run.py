"""第 5 步验收：看门狗 + vram_log + 护栏注入。

用法：
  python gateway/tools/watchdog_run.py                # 正常档，guard=7700
  python gateway/tools/watchdog_run.py --guard 5000   # 故障注入第 4 条
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from gateway.actions import RealRuntime  # noqa: E402
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
    deadline = time.time() + timeout
    while time.time() < deadline:
        if await asyncio.to_thread(fn):
            return True
        await asyncio.sleep(0.4)
    return False


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--guard", type=int, default=7700)
    args = ap.parse_args()

    job_id = "g" + datetime.now().strftime("%H%M%S")
    guard = args.guard

    procs = ProcessManager(LOGS)
    sd = SdBackend(SD_PORT, OUTPUT)
    llama = LlamaBackend(CHAT_PORT)

    procs.spawn("sd", SD_EXE, sd_args())
    procs.spawn("chat", LLAMA_EXE, llama_args())
    if not (await wait_any(sd.probe, 30) and await wait_any(llama.health, 40)):
        print("服务未就绪")
        procs.shutdown()
        return 1
    sd_pid_before = procs.procs["sd"].pid
    print(f"[1/4] 就绪 job={job_id} guard={guard} sd_pid={sd_pid_before}")

    vlog = VramLog(LOGS / "jobs" / f"{job_id}.vram.csv")
    await vlog.start()
    wd = VramWatchdog(NVIDIA_SMI, vlog, guard_mib=guard, consecutive=10)

    runtime = RealRuntime(procs=procs, sd=sd, llama=llama, sd_exe=SD_EXE,
                          llama_exe=LLAMA_EXE, sd_args=sd_args(), llama_args=llama_args())
    sup = Supervisor(runtime=runtime, timeouts=TIMEOUTS)

    # 事件循环卡顿监测：每 50ms 一次 sleep，测实际漂移
    lag_max = {"v": 0.0, "stop": False}

    async def lag_monitor():
        while not lag_max["stop"]:
            t0 = time.perf_counter()
            await asyncio.sleep(0.05)
            lag_max["v"] = max(lag_max["v"], (time.perf_counter() - t0 - 0.05) * 1000)
            await asyncio.sleep(0.01)

    async def on_trip(used: int):
        print(f"  !! 护栏触发 used={used} MiB")
        await sup.guard_tripped()

    wd_task = asyncio.create_task(wd.run(on_trip))
    lag_task = asyncio.create_task(lag_monitor())

    ctx = JobContext(job_id=job_id, prompt="a red apple on a wooden table, soft window light", total=1)
    t0 = time.perf_counter()
    print(f"[2/4] 提交（eta={ctx.eta_seconds}s first_after_reload={ctx.first_after_reload}）")
    await sup.submit(ctx)
    print(f"  submit 后 first_after_reload={ctx.first_after_reload}")

    # 等作业进入非 CHAT_READY，再等回到终态
    deadline = time.time() + 260
    entered = False
    while time.time() < deadline:
        if str(sup.state) != "CHAT_READY":
            entered = True
        if entered and str(sup.state) in ("CHAT_READY", "FAILED"):
            break
        await asyncio.sleep(0.3)

    total = round(time.perf_counter() - t0, 1)
    lag_max["stop"] = True
    await asyncio.sleep(0.1)
    await wd.stop()
    try:
        await asyncio.wait_for(wd_task, timeout=5)
    except Exception:
        pass
    lag_task.cancel()
    await vlog.close()

    sd_pid_after = procs.procs["sd"].pid if "sd" in procs.procs else -1
    sd_alive = procs.procs["sd"].alive() if "sd" in procs.procs else False
    await sup.shutdown()
    procs.shutdown()

    print(f"[3/4] 结束 state={sup.state} 总耗时={total}s")
    print("\n=== 轨迹 ===")
    prev = sup.timeline[0]["ts"] if sup.timeline else 0
    for it in sup.timeline:
        print(f"  +{it['ts'] - prev:>6.2f}s  {it['from']:<11} --{it['event']:<16}--> {it['to']:<11} [{it['action']}]")
        prev = it["ts"]

    rows = vlog.written
    csv_text = (LOGS / "jobs" / f"{job_id}.vram.csv").read_text(encoding="utf-8")
    flags = [ln.rsplit(",", 1)[-1] for ln in csv_text.strip().splitlines()[1:]]
    warn_idx = [i + 1 for i, f in enumerate(flags) if f == "warn"]
    trip_idx = [i + 1 for i, f in enumerate(flags) if f == "trip"]
    vals = [int(ln.split(",")[1]) for ln in csv_text.strip().splitlines()[1:]]
    peak = max(vals) if vals else -1

    # "放行 N 点后才触发"要看触发行之前连续多少个超限点，不是看行号
    run_before_trip = []
    if trip_idx:
        t = trip_idx[0] - 1
        while t >= 1 and flags[t - 1] == "warn" and vals[t - 1] > guard:
            run_before_trip.append(t)
            t -= 1

    print(f"\n=== csv ===")
    print(f"  行数={rows} dropped={vlog.dropped} 峰值={peak} MiB")
    print(f"  warn 行数={len(warn_idx)} 首个 warn={warn_idx[0] if warn_idx else None} "
          f"首个 trip={trip_idx[0] if trip_idx else None} 触发前连续超限点={len(run_before_trip)}")
    print(f"  作业状态={sup.ctx.status} error={sup.ctx.error}")
    print(f"  sd pid {sd_pid_before} → {sd_pid_after} 存活={sd_alive} 重启={'是' if sd_pid_after != sd_pid_before else '否'}")
    print(f"  事件循环最大漂移={lag_max['v']:.1f} ms")

    if guard >= 7000:
        ok = (rows > 0 and not warn_idx and not trip_idx and 6000 <= peak <= 7200
              and str(sup.ctx.status) == "done" and lag_max["v"] < 200)
    else:
        ok = (len(run_before_trip) == 9                       # 前 9 点放行
              and str(sup.ctx.status) == "failed"
              and sup.ctx.error == "vram_guard_tripped"        # 原因必须是护栏，不是连接重置
              and sd_pid_after != sd_pid_before                # sd 被 force kill 后重启
              and lag_max["v"] < 200)
    print(f"\n验收: {'通过' if ok else '不通过'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
