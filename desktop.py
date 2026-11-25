"""桌面壳入口：起 gateway + 内嵌浏览器，双击即用。

设计约束（和这个项目别处的规则一致）：
· **所有目录外置**。exe 只带 Python 运行时和 PySide6/依赖库，前端 `static/`、后端 `gateway/`、
  模型、`runtime/` 全部留在磁盘上，exe 从**自己所在目录**读它们。所以这里把 APP_DIR
  插进 `sys.path` 最前面并 `chdir` 过去——不是用 `sys._MEIPASS` 去包内找文件，恰恰相反：
  PyInstaller 单文件模式下 `sys.path[0]` 是临时解包目录，必须把它压下去，才能保证
  `import gateway` 命中的是根目录里那份**可读可改**的源码，而不是打进包里的副本。
· 前端代码不进包：`gateway.*` 用 `importlib.import_module("gateway.app")` 延迟导入，
  PyInstaller 的静态分析因此看不见它（不会被收进 exe）；但它的依赖（fastapi / uvicorn /
  yaml / httpx / PIL / pydantic）必须在本文件顶层 import，否则 exe 里没有这些库。
· 显存：`QTWEBENGINE_CHROMIUM_FLAGS` 必须**在 import PySide6 之前**设好。
  8GB 卡要留给 sd/llama，Chromium 一旦拿 GPU 上下文就会和它们抢显存。
"""

import os

# 必须在 PySide6 之前：关掉 Chromium 的 GPU（避免和出图/对话模型抢显存），
# 但保留软件光栅化——两个都关会让整页退到极慢的绘制路径，滚动明显卡。
os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = "--disable-gpu --num-raster-threads=4"

import importlib
import json
import socket
import sys
import threading
import time
import urllib.request
from pathlib import Path

# ---- 依赖：顶层 import 是为了让 PyInstaller 把它们打进包里（gateway 本身不进包） ----
import httpx                      # noqa: F401  gateway 用
import uvicorn
import yaml                       # noqa: F401  gateway 用
from fastapi import FastAPI       # noqa: F401  gateway 用
from fastapi.responses import JSONResponse, StreamingResponse  # noqa: F401
from fastapi.staticfiles import StaticFiles                     # noqa: F401
from PIL import Image, ImageChops, ImageStat                    # noqa: F401
from pydantic import BaseModel, Field                            # noqa: F401
import starlette                                                 # noqa: F401

# ---- 应用根目录 = exe 所在目录（冻结时） / desktop.py 所在目录（源码运行时） ----
FROZEN = bool(getattr(sys, "frozen", False))
APP_DIR = Path(sys.executable if FROZEN else __file__).resolve().parent
os.chdir(APP_DIR)
if str(APP_DIR) not in sys.path[:3]:
    sys.path.insert(0, str(APP_DIR))

LOG_PATH = APP_DIR / "logs" / "desktop.log"
LOG_PATH.parent.mkdir(parents=True, exist_ok=True)


def _read_gateway_port(default: int = 8000) -> int:
    """端口以 config.yaml 为准（和 start.ps1 / gateway 自己的配置保持一个真源）。

    原来这里写死 8000，而 yaml 里的 gateway.port 是可以改的：改了 yaml 之后壳还是去
    找 8000，永远等不到——等于把"端口可配置"变成了一个假的开关。
    """
    try:
        doc = yaml.safe_load((APP_DIR / "config.yaml").read_text(encoding="utf-8")) or {}
        return int((doc.get("gateway") or {}).get("port") or default)
    except Exception:
        return default


HOST = "127.0.0.1"
PORT = _read_gateway_port()
URL = f"http://{HOST}:{PORT}/"
READY_TIMEOUT_S = 180.0       # 单文件 exe 首次解包 + 杀软扫描 + infra_guard，慢机器要给足
POLL_MS = 200


class PortBusyError(RuntimeError):
    """端口开着，但那上面不是我们的 gateway。"""


def _is_our_gateway(host: str = HOST, port: int | None = None, timeout: float = 1.5) -> bool:
    """用 /health 认一下对面是谁。

    只看"端口能连上"是不够的：8000 是开发服务器/代理/其他 AI 工具的高频端口，
    别人占着时原实现会把它当成"自己的 gateway 已就绪"，直接把别人的网页显示出来。
    """
    try:
        url = f"http://{host}:{port or PORT}/health"
        with urllib.request.urlopen(url, timeout=timeout) as r:
            body = json.loads(r.read().decode("utf-8"))
        return isinstance(body, dict) and body.get("gateway") == "ok"
    except Exception:
        return False


def _redirect_output() -> None:
    """`-w` 打包后没有控制台，uvicorn/gateway 的 print 会落空甚至抛异常。统一写进 logs/desktop.log。"""
    if sys.stdout is not None and sys.stderr is not None and not FROZEN:
        return
    f = open(LOG_PATH, "a", encoding="utf-8", buffering=1)
    sys.stdout = f
    sys.stderr = f


def _port_open(host: str = HOST, port: int = PORT, timeout: float = 0.35) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        return s.connect_ex((host, port)) == 0


def start_gateway() -> tuple[object | None, threading.Thread | None]:
    """在后台线程起 uvicorn。已有实例占着端口就不重复起（gateway 的命名互斥体也会拒绝第二份）。"""
    if _port_open():
        if _is_our_gateway():
            print(f"[desktop] {PORT} 已是本软件的 gateway，直接连现有实例", flush=True)
            return None, None
        raise PortBusyError(
            f"端口 {PORT} 被其他程序占用了（不像是灵枢的 gateway）。\n\n"
            f"两条路：① 关掉占用该端口的程序；② 改 config.yaml 里的 gateway.port 后再启动。\n"
            f"查占用：Get-NetTCPConnection -State Listen -LocalPort {PORT}")

    mod = importlib.import_module("gateway.app")      # 延迟导入：外置目录里的源码，不进包
    config = uvicorn.Config(
        mod.app, host=HOST, port=PORT,
        log_level="info", log_config=None, access_log=False,
    )
    server = uvicorn.Server(config)                   # Server 本身不在主线程，不会接管信号
    thread = threading.Thread(target=server.run, name="gateway-uvicorn", daemon=True)
    thread.start()
    return server, thread


def stop_gateway(server, thread, timeout: float = 20.0) -> None:
    """窗口关掉时让 uvicorn 走正常 shutdown：lifespan 的 finally 才会停子进程、释放 .lock。"""
    if server is None:
        return
    server.should_exit = True
    deadline = time.monotonic() + timeout
    while thread and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.2)
    print(f"[desktop] gateway 线程已退出: {not (thread and thread.is_alive())}", flush=True)


def main() -> int:
    _redirect_output()
    print(f"[desktop] APP_DIR={APP_DIR} frozen={FROZEN}", flush=True)

    from PySide6.QtCore import Qt, QTimer, QUrl
    from PySide6.QtWidgets import QApplication, QLabel, QMainWindow, QMessageBox
    from PySide6.QtWebEngineWidgets import QWebEngineView

    QApplication.setAttribute(Qt.AA_ShareOpenGLContexts)     # WebEngine 需要，且必须在造 app 之前
    app = QApplication(sys.argv)
    app.setApplicationName("灵枢")
    # 图标同样外置：exe 所在目录的 static/favicon.ico。设成 app 级，窗口和任务栏一起生效。
    ico = APP_DIR / "static" / "favicon.ico"
    if ico.exists():
        from PySide6.QtGui import QIcon
        app.setWindowIcon(QIcon(str(ico)))
    else:
        print(f"[desktop] 没找到图标 {ico}，用默认图标", flush=True)

    try:
        server, thread = start_gateway()
    except PortBusyError as e:
        print(f"[desktop] 端口冲突：{e}", flush=True)
        QMessageBox.critical(None, "端口被占用", str(e))
        return 2

    class MainWindow(QMainWindow):
        def __init__(self) -> None:
            super().__init__()
            self.setWindowTitle("灵枢")
            self.resize(1280, 860)
            self.view = None
            self._closing = False
            self._close_deadline = 0.0
            self.splash = QLabel("正在启动本地服务…\n首次启动要拉起出图与对话服务，通常 5–20 秒。")
            self.splash.setAlignment(Qt.AlignCenter)
            self.setCentralWidget(self.splash)
            self._deadline = time.monotonic() + READY_TIMEOUT_S
            self._timer = QTimer(self)
            self._timer.timeout.connect(self._tick)
            self._timer.start(POLL_MS)

        # ---- 就绪轮询 ----
        def _tick(self) -> None:
            if self._closing:
                return
            if _port_open():
                self._timer.stop()
                if _is_our_gateway():
                    self._show_ui()
                else:
                    self._fail(f"端口 {PORT} 上有响应，但不是灵枢的 gateway——大概率被别的程序占了。\n"
                               f"关掉那个程序，或改 config.yaml 里的 gateway.port。")
                return
            if thread is not None and not thread.is_alive():
                self._timer.stop()
                self._fail("gateway 线程已退出，起不来。")
                return
            if time.monotonic() > self._deadline:
                self._timer.stop()
                self._fail(f"等了 {int(READY_TIMEOUT_S)} 秒，{PORT} 端口仍未就绪。")

        def _show_ui(self) -> None:
            self.view = QWebEngineView(self)
            self.view.loadFinished.connect(self._on_loaded)
            self.setCentralWidget(self.view)
            self.splash.deleteLater()
            self.view.load(QUrl(URL))

        def _on_loaded(self, ok: bool) -> None:
            print(f"[desktop] 页面加载 {'成功' if ok else '失败'}", flush=True)

        def _fail(self, msg: str) -> None:
            log_hint = f"\n\n日志：{LOG_PATH}"
            self.splash.setText(msg + log_hint)
            QMessageBox.critical(self, "启动失败", msg + log_hint)

        # ---- 关窗 = 关服务（不阻塞主线程，否则窗口会"未响应"） ----
        def closeEvent(self, event) -> None:      # noqa: N802 (Qt 命名)
            if server is None or not (thread and thread.is_alive()):
                event.accept()
                return
            if self._closing:
                return                            # 已经在关了，重复点 X 不重复发信号
            event.ignore()                        # 先留住 app，等 gateway 走完 lifespan 再退
            self._closing = True
            print("[desktop] 关窗：让 gateway 走正常 shutdown", flush=True)
            self.hide()
            server.should_exit = True             # 不在这里 sleep：主线程一阻塞，窗口就假死
            self._close_deadline = time.monotonic() + 30.0
            self._close_timer = QTimer(self)
            self._close_timer.timeout.connect(self._wait_closed)
            self._close_timer.start(POLL_MS)

        def _wait_closed(self) -> None:
            dead = not (thread and thread.is_alive())
            if not dead and time.monotonic() < self._close_deadline:
                return
            print(f"[desktop] 关窗等待结束，gateway 线程已退出: {dead}", flush=True)
            self._close_timer.stop()
            QApplication.quit()                   # app.exec() 返回后 main() 里还有一次兜底 stop

    win = MainWindow()
    win.show()
    win.raise_()
    win.activateWindow()
    # 测试钩子：LINGSHU_AUTOCLOSE_S=30 时 30 秒后自己走一遍关窗（和点 X 是同一条 QCloseEvent 路径），
    # 用来让验收脚本不必模拟系统消息就能验「关窗 = 停服务」。正常使用不受影响。
    auto = os.environ.get("LINGSHU_AUTOCLOSE_S", "").strip()
    if auto:
        try:
            QTimer.singleShot(max(1, int(float(auto) * 1000)), win.close)
            print(f"[desktop] 已装自动关窗钩子：{auto}s", flush=True)
        except ValueError:
            print(f"[desktop] LINGSHU_AUTOCLOSE_S 不是数字，忽略：{auto!r}", flush=True)
    code = app.exec()
    stop_gateway(server, thread)                  # 兜底：非关窗路径退出（比如 Ctrl+C）
    return code


if __name__ == "__main__":
    sys.exit(main())
