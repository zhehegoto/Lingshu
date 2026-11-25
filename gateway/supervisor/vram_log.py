"""vram.csv 异步写入。

两个约束（故障注入需要）：
1. 每行 flush——强杀进程那一刻，缓冲区里的行丢了正好是护栏触发前后最关键的数据。
2. 队列有背压且"丢最旧"——宁可丢点，不能让看门狗的 put 阻塞（阻塞等于采样停摆）。
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path


class VramLog:
    def __init__(self, path: Path, maxsize: int = 1000) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._q: asyncio.Queue[tuple[float, int, str]] = asyncio.Queue(maxsize=maxsize)
        self._task: asyncio.Task | None = None
        self.dropped = 0
        self.written = 0

    async def start(self) -> None:
        self.path.write_text("ts,used_mib,guard_flag\n", encoding="utf-8")
        self._task = asyncio.create_task(self._writer())

    def put_nowait(self, sample: tuple[float, int, str]) -> None:
        """看门狗专用：永不阻塞。队列满则丢最旧。"""
        try:
            self._q.put_nowait(sample)
        except asyncio.QueueFull:
            try:
                self._q.get_nowait()
            except asyncio.QueueEmpty:
                pass
            self.dropped += 1
            self._q.put_nowait(sample)

    async def _writer(self) -> None:
        with open(self.path, "a", encoding="utf-8") as f:
            while True:
                sample = await self._q.get()
                if sample is None:                       # 停止哨兵
                    f.flush()
                    return
                ts, used, flag = sample
                f.write(f"{ts:.3f},{used},{flag}\n")
                f.flush()
                self.written += 1

    async def close(self) -> None:
        await self._q.put(None)
        if self._task:
            await self._task
            self._task = None
