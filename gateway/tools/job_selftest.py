"""Job Object 手动验证的「父进程」：起两个 sd-server，写状态文件，然后睡等被外部杀掉。

外部（PowerShell）会在确认两个端口都起来后，对父 pid 执行 Stop-Process -Force，
然后检查两个子进程是否一起消失、端口是否释放。
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from gateway.supervisor.procs import ProcessManager, port_open  # noqa: E402

SD_EXE = ROOT / "runtime" / "sd" / "sd-server.exe"
MODELS = Path(r"F:\modiles")
STATE = ROOT / "logs" / "job_selftest_state.json"


def sd_args(port: int) -> list[str]:
    return [
        "--diffusion-model", str(MODELS / "qwen-image-2.1-turbo-Q5_K_M.gguf"),
        "--llm", str(MODELS / "Qwen3VL-8B-Instruct-Q4_K_M.gguf"),
        "--vae", str(MODELS / "qwen_image_2.1_vae_bf16.safetensors"),
        "--vae-tiling", "--vae-tile-size", "384",
        "--offload-to-cpu",
        "--listen-port", str(port),
    ]


def main() -> int:
    manager = ProcessManager(ROOT / "logs")
    a = manager.spawn("sd-a", SD_EXE, sd_args(8082))
    b = manager.spawn("sd-b", SD_EXE, sd_args(8083))

    deadline = time.time() + 40
    ready_a = ready_b = False
    while time.time() < deadline and not (ready_a and ready_b):
        ready_a = ready_a or port_open(8082)
        ready_b = ready_b or port_open(8083)
        time.sleep(0.4)

    STATE.write_text(
        json.dumps(
            {
                "parent_pid": __import__("os").getpid(),
                "children": {"sd-a": a.pid, "sd-b": b.pid},
                "ports": [8082, 8083],
                "ready": {"8082": ready_a, "8083": ready_b},
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    # 故意不走正常退出路径：等外部强杀。若被强杀，Python 不会 close job handle，
    # 只能靠内核 KILL_ON_JOB_CLOSE 兜底——这正是要验证的行为。
    time.sleep(150)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
