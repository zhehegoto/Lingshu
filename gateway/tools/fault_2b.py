"""故障注入第 2b 条：孤儿接管（正向 + 负向）。

正向：手动起本项目 sd-server(:8081) 与 llama-server(:8099) → infra_guard 应强杀并释放端口。
负向：用 junction 造一个"同名不同路径"的 sd-server（runtime\\_probe_sd）占着 :8081
      → infra_guard 应打印"跳过不杀"，进程存活。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from gateway.config import LLAMA_EXE, NVIDIA_SMI, SD_EXE, llama_args, sd_args  # noqa: E402
from gateway.infra import infra_guard, pids_listening, process_exe_path  # noqa: E402


def spawn(exe: Path, args: list[str]) -> subprocess.Popen:
    return subprocess.Popen([str(exe), *args], stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                            creationflags=0x08000000)


def wait_port(port: int, timeout: float = 30) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if pids_listening(port):
            return True
        time.sleep(0.5)
    return False


def main() -> int:
    ok = True

    # ---------- 正向 ----------
    print("[2b+] 手动起孤儿 sd(:8081) + llama(:8099)")
    p_sd = spawn(SD_EXE, sd_args(port=8081))
    p_ll = spawn(LLAMA_EXE, llama_args(port=8099))
    both = wait_port(8081) and wait_port(8099)
    print(f"  端口已占: {both}  pid sd={p_sd.pid} llama={p_ll.pid}")

    rep = infra_guard({8081: SD_EXE, 8099: LLAMA_EXE}, NVIDIA_SMI)
    print(f"  报告: {rep.summary()}")
    killed = len(rep.killed)
    released = sorted(rep.released)
    print(f"  killed={killed} released={released}")
    if not (both and killed == 2 and released == [8081, 8099]):
        ok = False
        print("  ✗ 正向未通过")
    else:
        print("  ✓ 正向通过")

    # ---------- 负向 ----------
    print("\n[2b-] 造同名不同路径的 sd-server 占 :8081（真实拷贝，不用 junction）")
    link = ROOT / "runtime" / "_probe_sd"
    try:
        # junction 不行：Windows 会把 junction 路径解析成目标路径，
        # QueryFullProcessImageNameW 返回的是 runtime\sd\sd-server.exe，负向就失真了。
        if link.exists():
            shutil.rmtree(link, ignore_errors=True)
        shutil.copytree(SD_EXE.parent, link)          # ~1.1GB，NVMe 上几秒
        probe_exe = link / "sd-server.exe"
        print(f"  拷贝到 {link}")

        p_bad = spawn(probe_exe, sd_args(port=8081))
        if not wait_port(8081):
            print("  ✗ 负向进程未起来")
            p_bad.kill()
            return 1
        actual = process_exe_path(p_bad.pid)
        print(f"  路径={actual}")

        rep2 = infra_guard({8081: SD_EXE}, NVIDIA_SMI)
        print(f"  报告: {rep2.summary()}")
        still_alive = p_bad.poll() is None
        print(f"  killed={len(rep2.killed)} skipped={len(rep2.skipped)} 进程存活={still_alive}")
        if len(rep2.killed) == 0 and len(rep2.skipped) == 1 and still_alive:
            print("  ✓ 负向通过（没误杀别人）")
        else:
            ok = False
            print("  ✗ 负向未通过")

        p_bad.kill()
        p_bad.wait(timeout=5)
        time.sleep(1)
    except Exception as e:
        print(f"  负向异常: {e}")
        ok = False
    finally:
        bound = (ROOT / "runtime" / "_probe_sd").resolve()
        if str(bound).startswith(str((ROOT / "runtime").resolve())) and bound.exists():
            shutil.rmtree(bound, ignore_errors=True)
            print(f"  已清理拷贝目录 {bound}")

    print(f"\n验收: {'通过' if ok else '不通过'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
