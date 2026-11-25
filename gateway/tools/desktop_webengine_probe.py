"""最小 WebEngine 验证：确认 QWebEngineView 能起、能渲染，与 gateway 无关。

用法：python gateway\tools\desktop_webengine_probe.py [flags]
  flags 默认为 desktop.py 用的那套；传 "none" 则不设 QTWEBENGINE_CHROMIUM_FLAGS。
"""
import os
import sys

flags = sys.argv[1] if len(sys.argv) > 1 else "--disable-gpu --disable-software-rasterizer"
if flags != "none":
    os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = flags

from PySide6.QtCore import QTimer, QUrl
from PySide6.QtWidgets import QApplication
from PySide6.QtWebEngineWidgets import QWebEngineView

app = QApplication([])
view = QWebEngineView()
state = {"ok": None}


def on_done(ok):
    state["ok"] = ok
    QTimer.singleShot(500, app.quit)


view.loadFinished.connect(on_done)
view.resize(800, 600)
view.show()
view.load(QUrl("http://127.0.0.1:8000/"))
QTimer.singleShot(12000, app.quit)
app.exec()
print(f"PROBE flags={flags!r} loadFinished={state['ok']}")
