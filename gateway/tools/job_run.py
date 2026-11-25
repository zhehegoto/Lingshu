"""第 4 步联调：正常路径走完一次完整作业。

验收四项：
1. 状态轨迹完整（CHAT_READY → PREPARING → GENERATING → RELOADING → CHAT_READY）
2. vram.csv 完整曲线，峰值落在 6668–6900，无护栏触发
3. 落盘 output/{YYYYMMDD}/{job_id}_0.png + 同名 .json
4. Gemma 重载耗时 5–7s
"""

from __future__ import annotations

import asyncio
import json
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


async def wait_port(fn, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if await asyncio.to_thread(fn):
            return True
        await asyncio.sleep(0.4)
    return False


async def main() -> int:
    job_id = "j" + datetime.now().strftime("%H%M%S")
    csv_path = LOGS / f"job_{job_id}.vram.csv"
    procs = ProcessManager(LOGS)
    sd = SdBackend(SD_PORT, OUTPUT)
    llama = LlamaBackend(CHAT_PORT)

    print(f"[1/5] 启动服务 job_id={job_id} 基线显存={vram_used_mib()} MiB")
    procs.spawn("sd", SD_EXE, sd_args())
    procs.spawn("chat", LLAMA_EXE, llama_args())
    sd_ok = await wait_port(sd.probe, 30)
    chat_ok = await wait_port(llama.health, 40)
    print(f"[2/5] sd={sd_ok} chat={chat_ok} 显存={vram_used_mib()} MiB")
    if not (sd_ok and chat_ok):
        print("服务未就绪，终止")
        procs.shutdown()
        return 1

    sampler = subprocess.Popen(
        [str(NVIDIA_SMI), "--query-gpu=memory.used", "--format=csv,noheader,nounits", "-lms", "100"],
        stdout=open(csv_path, "w", encoding="utf-8"), stderr=subprocess.DEVNULL,
    )

    runtime = RealRuntime(
        procs=procs, sd=sd, llama=llama,
        sd_exe=SD_EXE, llama_exe=LLAMA_EXE,
        sd_args=sd_args(), llama_args=llama_args(),
    )
    sup = Supervisor(runtime=runtime, timeouts=TIMEOUTS)

    ctx = JobContext(job_id=job_id, prompt="a red apple on a wooden table, soft window light", total=1)
    t_start = time.perf_counter()
    print("[3/5] 提交作业")
    await sup.submit(ctx)

    state_seen = False
    deadline = time.time() + 240
    while time.time() < deadline:
        if sup.state is not None and sup.state != "CHAT_READY":
            state_seen = True
        if state_seen and str(sup.state) in ("CHAT_READY", "FAILED"):
            break
        await asyncio.sleep(0.4)

    total = round(time.perf_counter() - t_start, 1)
    print(f"[4/5] 结束 state={sup.state} 总耗时={total}s")
    await sup.shutdown()
    await asyncio.sleep(0.5)
    sampler.terminate()
    sampler.wait(timeout=5)
    procs.shutdown()

    # ---------- 验收 ----------
    print("\n=== 状态轨迹 ===")
    t0 = sup.timeline[0]["ts"] if sup.timeline else 0
    prev_ts = t0
    for item in sup.timeline:
        gap = round(item["ts"] - prev_ts, 2)
        print(f"  +{gap:>6.2f}s  {item['from']:<11} --{item['event']:<16}--> {item['to']:<11} [{item['action']}]")
        prev_ts = item["ts"]

    reload_s = None
    for a, b in zip(sup.timeline, sup.timeline[1:]):
        if a["to"] == "RELOADING":
            reload_s = round(b["ts"] - a["ts"], 2)
    print(f"\n  重载耗时: {reload_s}s  (期望 5–7s)")

    vals = [int(x.strip()) for x in csv_path.read_text(encoding="utf-8").splitlines() if x.strip().isdigit()]
    peak = max(vals) if vals else -1
    big = [v for v in vals if v > 7700]
    print(f"  显存采样 {len(vals)} 点  峰值={peak} MiB  >7700 的点={len(big)}")

    day = datetime.now().strftime("%Y%m%d")
    png = OUTPUT / day / f"{job_id}_0.png"
    meta = png.with_suffix(".json")
    print(f"  落盘: {png.exists()} {png}")
    if meta.exists():
        m = json.loads(meta.read_text(encoding="utf-8"))
        print(f"  元信息: prompt={m['prompt'][:30]}... steps={m['steps']} seed={m['seed']} "
              f"耗时={m['elapsed_seconds']}s 尺寸={m['width']}x{m['height']}")

    ok = (
        str(sup.state) == "CHAT_READY"
        and peak >= 6000 and peak <= 7200 and not big
        and png.exists() and meta.exists()
        and reload_s is not None and 4.5 <= reload_s <= 8.0
    )
    print(f"\n验收: {'通过' if ok else '不通过'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
