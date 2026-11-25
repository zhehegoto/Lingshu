"""进程内日志环形缓冲：给「运行」页的实时日志用。

为什么要它：gateway 的 stdout 在发布包（windowed exe）里没有控制台，日志文件也
只覆盖子服务（sd/chat 各一份），gateway 自己的运行信息没有可读出口。这里挂一个
logging.Handler，任何 logger 的输出都留最近 N 条，前端轮询就能看到"刚才发生了什么"。
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Any

_MAX = 500
_buf: deque[dict[str, Any]] = deque(maxlen=_MAX)
_lock = threading.Lock()
_seq = 0


class RingHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        global _seq
        # 标了 internal 的记录只进日志文件，不进界面：gateway 自己排障要看，
        # 但 killed=[] skipped=[] 这种话对用户没意义，刷在「运行日志」里只会像报错。
        if getattr(record, "internal", False):
            return
        try:
            msg = record.getMessage()
        except Exception:
            msg = str(record.msg)
        with _lock:
            _seq += 1
            _buf.append({
                "seq": _seq,
                "ts": round(record.created, 3),
                "time": time.strftime("%H:%M:%S", time.localtime(record.created)),
                "level": record.levelname,
                "logger": record.name,
                "msg": msg,
            })


def install() -> None:
    root = logging.getLogger()
    if any(isinstance(h, RingHandler) for h in root.handlers):
        return
    h = RingHandler()
    h.setLevel(logging.INFO)
    root.addHandler(h)


def tail(since: int = 0, limit: int = 200) -> dict[str, Any]:
    with _lock:
        items = [e for e in _buf if e["seq"] > since]
    if limit > 0:
        items = items[-limit:]
    last = _buf[-1]["seq"] if _buf else since
    return {"items": items, "last": last}
