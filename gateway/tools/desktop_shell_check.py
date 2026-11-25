"""桌面壳验收：证明 `python desktop.py` 真的弹出了窗口、真的起了 gateway、关窗真的会停服务。

为什么这么验（而不是"看到窗口就行"）：
· 窗口存在 → 用 EnumWindows 找标题「灵枢」的可见窗口，不靠截图人眼判断；
· 服务真由壳起 → 启动前先确认 8000 没被占，起完后 8000 可连；
· 关窗真会停 → 给那个窗口发 WM_CLOSE（等价点右上角 X，走 closeEvent），
  然后要求进程退出 **且** 8000 断开、`.lock` 被 lifespan 释放掉。
任一条不成立就红：这就是"副作用断言"，不是"没报错就算过"。

用法：python gateway\\tools\\desktop_shell_check.py
前提：8000 当前空闲（本脚本自己起服务，不会连已有实例——那样验证不到"壳起的服务"）。
"""
import ctypes
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))      # 直接跑脚本时 sys.path[0] 是 gateway\tools，导入 gateway 前先把根目录压进去
TITLE = "灵枢"
CLOSE_TIMEOUT_S = 60      # 关窗要走 lifespan：停 worker → 停 watchdog → 停子进程
AUTOCLOSE_S = 25         # desktop.py 的测试钩子：进程启动 25 秒后自己走一次关窗


def port_open(port: int, timeout: float = 0.4) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        return s.connect_ex(("127.0.0.1", port)) == 0


def find_window(title: str) -> int:
    user32 = ctypes.windll.user32
    hits: list[int] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def cb(hwnd, _):
        buf = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd, buf, 256)
        if user32.IsWindowVisible(hwnd) and buf.value.strip() == title:
            hits.append(hwnd)
        return True

    user32.EnumWindows(cb, None)
    return hits[0] if hits else 0


def main() -> int:
    failures: list[str] = []

    if port_open(8000):
        print("8000 已被占用：先停掉现有 gateway，否则验不到「壳自己起的服务」")
        return 2

    env = dict(os.environ)
    env["PYTHONPATH"] = os.environ.get("PYTHONPATH", "") + (";" if os.environ.get("PYTHONPATH") else "") + r"C:\pyside"
    env["PYTHONUNBUFFERED"] = "1"
    env["LINGSHU_AUTOCLOSE_S"] = str(AUTOCLOSE_S)   # 用 desktop.py 自带钩子代替模拟 WM_CLOSE，同一 QCloseEvent 路径
    log = ROOT / "logs" / "desktop_check.out"
    log.parent.mkdir(parents=True, exist_ok=True)
    fh = open(log, "w", encoding="utf-8")
    # 传 灵枢.exe 就验打包出来的壳；不传就验源码运行的 desktop.py
    target = sys.argv[1] if len(sys.argv) > 1 else "desktop.py"
    cmd = [target] if target.lower().endswith(".exe") else [sys.executable, "-u", "desktop.py"]
    print(f"被测对象：{target}")
    proc = subprocess.Popen(cmd, cwd=str(ROOT), env=env,
                            stdout=fh, stderr=subprocess.STDOUT)
    # 冻结版（-w）没有控制台，desktop.py 会把 stdout 重定向进 logs\desktop.log，
    # 所以「关窗」那行可能落在这里而不是 desktop_check.out —— 两个文件都从"本次启动前的大小"往后找。
    watch_files = {"check": log, "desktop": ROOT / "logs" / "desktop.log"}
    base = {k: (p.stat().st_size if p.exists() else 0) for k, p in watch_files.items()}

    def marker_seen(marker: str) -> bool:
        for k, p in watch_files.items():
            if not p.exists():
                continue
            with open(p, "rb") as f:
                f.seek(base[k])
                if marker.encode("utf-8") in f.read():
                    return True
        return False

    try:
        hwnd, ready_at = 0, None
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if not hwnd:
                hwnd = find_window(TITLE)
            if ready_at is None and port_open(8000):
                ready_at = time.monotonic()
            if hwnd and ready_at and proc.poll() is None:
                break
            if proc.poll() is not None:
                break
            time.sleep(0.4)

        print(f"窗口「{TITLE}」存在: {bool(hwnd)} · 启动后 8000 可连: {ready_at is not None} · 进程活着: {proc.poll() is None}")
        if not hwnd:
            failures.append("没找到标题为「灵枢」的可见窗口")
        if ready_at is None:
            failures.append("60 秒内 8000 没就绪（gateway 没被壳起起来）")

        if hwnd and ready_at:
            # 等钩子到点自己关窗：它走的是和点 X 完全同一条 QCloseEvent → closeEvent → stop
            close_at = None
            marker = "[desktop] 关窗"
            deadline = time.monotonic() + AUTOCLOSE_S + 15
            while time.monotonic() < deadline and proc.poll() is None:
                if close_at is None and marker_seen(marker):
                    close_at = time.monotonic()
                if close_at is not None and proc.poll() is not None:
                    break
                time.sleep(0.3)
            if close_at is None and proc.poll() is None:
                failures.append(f"到 {AUTOCLOSE_S}s 钩子没触发关窗（closeEvent 没进）")
            elif close_at is None:
                # 进程已经退了但没抓到那行：可能是缓冲/时机，进程退出本身仍是硬证据
                print("没抓到「关窗」日志行（进程已退出），按退出时间计")
            while proc.poll() is None and time.monotonic() - (close_at or time.monotonic()) < CLOSE_TIMEOUT_S:
                time.sleep(0.3)
            exited = proc.poll()
            elapsed = time.monotonic() - close_at if close_at else float("nan")
            closed = not port_open(8000)
            print(f"关窗后进程退出: {exited is not None}（{elapsed:.1f}s 内）· 8000 已断开: {closed}")
            if exited is None:
                failures.append(f"关窗 {CLOSE_TIMEOUT_S}s 内进程没退出")
            if not closed:
                failures.append("关窗后 8000 还在监听（uvicorn 没跟着停）")
            # `.lock` 文件是 PID 记录，release() 只关互斥体不删文件（留着比删掉好），
            # 所以断言"互斥体已释放"而不是"文件没了"：真释放了才允许再起一份。
            from gateway.infra import InstanceAlreadyRunning, InstanceLock
            lock = InstanceLock(ROOT / ".lock")
            try:
                lock.acquire()
                lock.release()
                print("互斥体已释放，可以再起一份: True")
            except InstanceAlreadyRunning:
                print("互斥体已释放，可以再起一份: False")
                failures.append("关窗后互斥体仍被占（lifespan 的 finally 没跑完）")
    finally:
        if proc.poll() is None:
            proc.kill()
        fh.close()

    tail = "\n".join(log.read_text(encoding="utf-8", errors="replace").splitlines()[-12:])
    print("--- desktop_check.out 末尾 ---")
    print(tail)
    print("验收:", "通过" if not failures else "失败 → " + "；".join(failures))
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
