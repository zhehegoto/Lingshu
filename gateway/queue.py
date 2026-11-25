"""作业队列 + 事件扇出（pub-sub）。

要点：
- 每个作业维护"当前快照 + 订阅者列表"。SSE 重连先推快照、再订阅后续事件，
  不从头重放也不漏中间事件。
- 快照里的 sampling_step 建连时**实时读 sd.progress()**，不读缓存（缓存会落后 100–500ms）。
- 单队列单 worker：supervisor 一次只跑一个作业，这里不做并发。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("queue")


@dataclass
class Job:
    id: str
    prompt: str
    negative: str = ""
    width: int = 1024
    height: int = 1024
    steps: int = 8
    seed: int = -1
    batch_count: int = 1
    status: str = "queued"          # queued / preparing / generating / done / failed / cancelled
    error: str | None = None
    images: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    eta_seconds: int | None = None
    kind: str = "txt2img"           # txt2img / mask
    init_b64: str | None = None     # 不进 snapshot（几 MB，前端不需要）
    mask_b64: str | None = None

    def snapshot(self) -> dict[str, Any]:
        return {
            "id": self.id, "status": self.status, "error": self.error,
            "prompt": self.prompt, "negative": self.negative,
            "width": self.width, "height": self.height,
            "steps": self.steps, "seed": self.seed, "batch_count": self.batch_count,
            "kind": self.kind,
            "images": list(self.images), "eta_seconds": self.eta_seconds,
        }


class JobChannel:
    """一个作业的事件通道：快照 + 订阅者。"""

    def __init__(self) -> None:
        self.subscribers: list[asyncio.Queue] = []
        self.closed = False

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        self.subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        if q in self.subscribers:
            self.subscribers.remove(q)

    def publish(self, event: dict[str, Any]) -> None:
        for q in list(self.subscribers):
            if q.full():
                try:
                    q.get_nowait()           # 丢最旧，宁可少一条也不阻塞发布
                except asyncio.QueueEmpty:
                    pass
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass

    def close(self) -> None:
        self.closed = True
        for q in list(self.subscribers):
            try:
                q.put_nowait({"type": "end"})
            except asyncio.QueueFull:
                pass


class JobHub:
    def __init__(self, keep: int = 50) -> None:
        self.jobs: dict[str, Job] = {}
        self.channels: dict[str, JobChannel] = {}
        self.keep = keep
        self._seq = 0

    def new_id(self) -> str:
        self._seq += 1
        return f"j{int(time.time() * 1000) % 86400000:07d}{self._seq:03d}"

    def create(self, **kwargs: Any) -> Job:
        job = Job(id=kwargs.pop("id", None) or self.new_id(), **kwargs)
        self.jobs[job.id] = job
        self.channels[job.id] = JobChannel()
        self._trim()
        return job

    def get(self, job_id: str) -> Job | None:
        return self.jobs.get(job_id)

    def publish(self, job_id: str, etype: str, data: dict[str, Any] | None = None) -> None:
        ch = self.channels.get(job_id)
        if ch:
            ch.publish({"type": etype, **(data or {})})

    def finish(self, job_id: str) -> None:
        ch = self.channels.get(job_id)
        if ch:
            ch.close()

    def _trim(self) -> None:
        if len(self.jobs) <= self.keep:
            return
        for jid in sorted(self.jobs, key=lambda j: self.jobs[j].created_at)[: len(self.jobs) - self.keep]:
            if self.jobs[jid].status in ("done", "failed", "cancelled"):
                self.jobs.pop(jid, None)
                self.channels.pop(jid, None)

    async def stream(self, job_id: str, sd_progress=None):
        """先推快照（实时 progress），再订阅。"""
        job = self.jobs.get(job_id)
        ch = self.channels.get(job_id)
        if job is None or ch is None:
            yield {"type": "error", "error": "job_not_found"}
            return

        snap = job.snapshot()
        if job.status == "generating" and sd_progress is not None:
            try:
                live = await asyncio.to_thread(sd_progress)
                if live:
                    snap["progress"] = live
            except Exception:
                pass
        snap["type"] = "snapshot"
        yield snap

        if ch.closed:
            yield {"type": "end"}
            return

        q = ch.subscribe()
        try:
            while True:
                try:
                    evt = await asyncio.wait_for(q.get(), timeout=10.0)
                except asyncio.TimeoutError:
                    yield {"type": "ping", "ts": time.time()}   # 心跳，防中间层断
                    continue
                yield evt
                if evt.get("type") == "end":
                    return
        finally:
            ch.unsubscribe(q)


class JobWorker:
    def __init__(self, hub: JobHub, sup, sd, cfg, maxsize: int = 4) -> None:
        self.hub = hub
        self.sup = sup
        self.sd = sd
        self.cfg = cfg
        self.queue: asyncio.Queue[Job] = asyncio.Queue(maxsize=maxsize)
        self.current: Job | None = None
        self._task: asyncio.Task | None = None
        self._first_after_reload = True
        self._cancelled: set[str] = set()

    def start(self) -> None:
        self._task = asyncio.create_task(self.run())

    async def enqueue(self, job: Job) -> bool:
        try:
            self.queue.put_nowait(job)
            return True
        except asyncio.QueueFull:
            return False

    def depth(self) -> int:
        return self.queue.qsize()

    def drain(self, reason: str) -> list[str]:
        """把排队的作业全部置 failed 并推事件。

        sd-server 刚重启时它还在加载模型，这时候放过去的作业大概率再失败一次；
        留着队列会让前端看到"排队中 1"永远不动，被误判成前端 bug。
        """
        drained: list[str] = []
        while True:
            try:
                job = self.queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if job.status == "queued":
                job.status = "failed"
                job.error = reason
                self.hub.publish(job.id, "status",
                                 {"status": "failed", "error": reason, "requeue": False})
                self.hub.finish(job.id)
                drained.append(job.id)
            self.queue.task_done()
        if drained:
            log.warning("队列已清空（%s）：%s", reason, drained)
        return drained

    async def cancel(self, job_id: str) -> dict[str, Any]:
        job = self.hub.get(job_id)
        if job is None:
            return {"ok": False, "reason": "job_not_found"}
        if job.status in ("done", "failed", "cancelled"):
            return {"ok": False, "reason": f"already_{job.status}"}
        if job.status == "queued":
            job.status = "cancelled"
            job.error = "cancelled_by_user"
            self._cancelled.add(job.id)
            self.hub.publish(job.id, "status", {"status": "cancelled"})
            self.hub.finish(job.id)
            return {"ok": True, "scope": "queue"}
        if self.current is not None and self.current.id == job_id:
            from gateway.supervisor.state import can_accept, Event as Ev
            if can_accept(self.sup.state, Ev.CANCEL):
                await self.sup.cancel()
                self.hub.publish(job.id, "status", {"status": "cancelling"})
                return {"ok": True, "scope": "running"}
            return {"ok": False, "reason": f"state_{self.sup.state}"}
        return {"ok": False, "reason": "not_current"}

    async def run(self) -> None:
        from gateway.supervisor.machine import JobContext
        from gateway.supervisor.state import State

        while True:
            job = await self.queue.get()
            if job.status == "cancelled":
                self.queue.task_done()
                continue

            self.current = job
            job.status = "preparing"
            self.hub.publish(job.id, "status", {"status": "preparing"})

            ctx = JobContext(
                job_id=job.id, prompt=job.prompt, negative=job.negative,
                width=job.width, height=job.height,
                steps=job.steps, seed=job.seed, total=job.batch_count,
                first_after_reload=self._first_after_reload,
                kind=job.kind, init_b64=job.init_b64, mask_b64=job.mask_b64,
            )
            self._first_after_reload = False
            job.eta_seconds = ctx.eta_seconds

            await self.sup.submit(ctx)

            # 轮询把进度推给订阅者，直到回到稳定态
            while self.sup.state in (State.PREPARING, State.GENERATING, State.RELOADING, State.DEGRADED):
                self._sync_from_ctx(job)
                if job.status == "generating":
                    prog = await asyncio.to_thread(self._progress)
                    if prog:
                        job.eta_seconds = prog.get("eta_seconds", job.eta_seconds)
                        self.hub.publish(job.id, "progress", prog)
                await asyncio.sleep(0.3)

            self._sync_from_ctx(job)
            job.status = self.sup.ctx.status if self.sup.ctx else "failed"
            job.error = self.sup.ctx.error if self.sup.ctx else "unknown"

            # sd 连接断开：对外统一叫 sd_connection_lost，并清空队列
            if job.error == "image_failed":
                job.error = "sd_connection_lost"
            if job.error in ("sd_connection_lost", "sd_restarted"):
                self.drain("sd_restarted")

            if self.sup.state == State.CHAT_READY:
                self._first_after_reload = True

            self.hub.publish(job.id, "done", {"status": job.status, "error": job.error})
            self.hub.finish(job.id)
            self.current = None
            self.queue.task_done()

    def _progress(self) -> dict[str, Any] | None:
        try:
            return self.sd.progress()
        except Exception:
            return None

    def _sync_from_ctx(self, job: Job) -> None:
        ctx = self.sup.ctx
        if ctx is None or ctx.job_id != job.id:
            return
        job.images = list(ctx.images)
        job.status = ctx.status
