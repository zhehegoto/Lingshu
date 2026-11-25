"""验证 PUT vae_tile 真的重启 sd-server（副作用断言，不看返回码）。

断言方式：读 sd-server 进程的命令行，确认 --vae-tile-size 的值真的变了。
"""

import subprocess
import sys
import time

import httpx

BASE = "http://127.0.0.1:8000"
PS1 = str(Path(__file__).resolve().parents[2] / "gateway" / "tools" / "sd_cmdline.ps1")
PS_EXE = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"


def ps(args: list[str]) -> str:
    out = subprocess.run([PS_EXE, "-NoProfile", "-ExecutionPolicy", "Bypass", *args],
                         capture_output=True, text=True, timeout=40)
    return out.stdout.strip()


def sd_cmd() -> str:
    return " ".join(ps(["-File", PS1]).split())


def sd_pid() -> str:
    return " ".join(ps(["-Command", "(Get-Process sd-server).Id"]).split())


def tile_in_cmd(cmd: str) -> str | None:
    parts = cmd.split()
    for i, p in enumerate(parts):
        if p == "--vae-tile-size" and i + 1 < len(parts):
            return parts[i + 1]
    return None


def wait_ready(c: httpx.Client, timeout: float = 45) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            if c.get("/api/status").json()["ready"]["sd"]:
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def main() -> int:
    c = httpx.Client(base_url=BASE, timeout=30)
    ok = True
    print("ready:", wait_ready(c))
    start_tile = tile_in_cmd(sd_cmd())
    print("启动时 tile:", start_tile)

    for want in (256, int(start_tile or 384)):
        before_pid = sd_pid()
        r = c.put("/api/settings", json={"vae_tile": want})
        print(f"PUT vae_tile={want} → {r.status_code} {r.json().get('note')}")
        ok &= wait_ready(c)
        after = tile_in_cmd(sd_cmd())
        after_pid = sd_pid()
        print(f"  pid {before_pid} → {after_pid}，命令行 tile={after}")
        if after != str(want):
            ok = False
            print(f"  ✗ 命令行的 tile 不是 {want}")
        if after_pid == before_pid:
            ok = False
            print("  ✗ 进程 PID 没变，说明根本没重启")

    # 落盘也必须是最后的值
    cfg_tile = c.get("/api/settings").json()["values"]["vae_tile"]
    print("config 落盘 vae_tile:", cfg_tile)
    print("验收:", "通过" if ok else "不通过")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
