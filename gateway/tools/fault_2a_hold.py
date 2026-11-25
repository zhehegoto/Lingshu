"""故障注入 2a 的载体：起 Job Object + 两个子进程，然后挂住等外部强杀父进程。"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from gateway.config import LLAMA_EXE, LOGS, SD_EXE, llama_args, sd_args  # noqa: E402
from gateway.supervisor.procs import ProcessManager  # noqa: E402

procs = ProcessManager(LOGS)
procs.spawn("sd", SD_EXE, sd_args())
procs.spawn("chat", LLAMA_EXE, llama_args())
state = {"parent": None, "sd": procs.procs["sd"].pid, "chat": procs.procs["chat"].pid}
(LOGS / "fault_2a_state.json").write_text(json.dumps(state), encoding="utf-8")
print(f"已起 sd={state['sd']} chat={state['chat']}", flush=True)
time.sleep(200)
