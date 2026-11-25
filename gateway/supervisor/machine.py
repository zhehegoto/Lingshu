"""Supervisor：同步状态机内核 + 异步动作调度。

分层约定（第 4 步）：
- transition()/self._apply() 保持同步纯逻辑 → 原有 17 条单测一行不改。
- async 只包在 emit() 外层：_apply 决定去哪，_dispatch 负责 await 真实动作。
- 动作实现不在本模块（supervisor/ 不 import backends/），由构造时注入的 runtime 提供。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from .state import (
    Action,
    Event,
    State,
    Timer,
    Transition,
    UnknownTransition,
    can_accept,
    transition,
)

log = logging.getLogger("supervisor")

TIMER_EVENTS: dict[Timer, Event] = {
    Timer.PREPARE: Event.PREPARE_TIMEOUT,
    Timer.IMAGE: Event.IMAGE_TIMEOUT,
    Timer.RELOAD: Event.RELOAD_TIMEOUT,
    Timer.CLEANUP: Event.CLEANUP_TIMEOUT,
}


@dataclass
class JobContext:
    """一次作业的全部上下文。动作只读 self.ctx。"""

    job_id: str
    prompt: str = ""
    negative: str = ""
    width: int = 1024
    height: int = 1024
    steps: int = 8
    seed: int = -1
    total: int = 1
    done: int = 0
    status: str = "queued"
    error: str | None = None
    images: list[str] = field(default_factory=list)
    first_after_reload: bool = False      # 对话刚恢复后的首图，eta 用 40s 而非 32s
    kind: str = "txt2img"                 # txt2img / mask（画布框选重绘，复用同一状态机）
    init_b64: str | None = None           # mask 模式的原图（画布快照）
    mask_b64: str | None = None           # mask 模式：白=重绘、黑=保留
    strength: float = 0.95                # 固定值，不给前端改

    @property
    def eta_seconds(self) -> int:
        """按档给 eta，不做滑动窗口（窗口会把 40 和 32 混成中间值，两档都不准）。"""
        base = 40 if self.first_after_reload else 32
        return base + 32 * (self.total - self.done - 1)

    @property
    def has_more(self) -> bool:
        return self.done < self.total


class Runtime(Protocol):
    """动作实现契约。supervisor 只认这几个方法。"""

    async def unload_chat(self) -> None: ...
    async def load_chat(self) -> bool: ...
    async def submit_image(self, ctx: JobContext) -> str: ...
    async def kill_sd(self) -> None: ...
    async def stop_sd(self) -> None: ...
    async def force_kill_chat(self) -> None: ...
    async def cleanup_restart(self) -> bool: ...


class Supervisor:
    def __init__(
        self,
        runtime: Runtime | None = None,
        timeouts: dict[str, float] | None = None,
    ) -> None:
        self.state: State = State.CHAT_READY
        self.ctx: JobContext | None = None
        self.timer: Timer | None = None
        self.runtime = runtime
        self.timeouts = timeouts or {"prepare": 10.0, "image": 110.0, "reload": 20.0, "cleanup": 60.0}

        self.trace: list[tuple[State, Event, State, Action]] = []
        self.timeline: list[dict[str, Any]] = []
        self.action_log: list[Action] = []
        self._timer_task: asyncio.Task | None = None
        self._tasks: set[asyncio.Task] = set()
        self._first_after_load = True      # 冷启动也算：sd 权重未热、Gemma 已上卡

    # ---------------- 同步内核 ----------------

    def _apply(self, event: Event, ctx: JobContext | None = None) -> Transition:
        tr = transition(self.state, event)
        prev = self.state

        if event is Event.JOB_SUBMITTED:
            if ctx is None:
                raise ValueError("job_submitted 必须携带 JobContext")
            self.ctx = ctx
            ctx.status = "preparing"
            ctx.first_after_reload = self._first_after_load
            self._first_after_load = False
        if self.ctx is None and event is not Event.JOB_SUBMITTED:
            raise ValueError(f"无作业上下文时收到了 {event}")

        # 作业状态在事件入口定，不在动作里猜
        if self.ctx is not None:
            if event is Event.IMAGE_TIMEOUT:
                self.ctx.status = "failed"
                self.ctx.error = self.ctx.error or "image_timeout"
            elif event is Event.IMAGE_FAILED:
                self.ctx.status = "failed"
                self.ctx.error = self.ctx.error or "image_failed"
            elif event is Event.PREPARE_TIMEOUT:
                self.ctx.status = "failed"
                self.ctx.error = self.ctx.error or "prepare_timeout"
            elif event is Event.CLEANUP_TIMEOUT:
                self.ctx.error = self.ctx.error or "degraded_cleanup_failed"

        self.state = tr.next_state
        if tr.next_state is State.CHAT_READY and prev is State.RELOADING:
            self._first_after_load = True        # 对话刚恢复，下一张图要按 40s 估
        self.timer = tr.timer
        self._cancel_timer()
        if tr.timer:
            self._arm_timer(tr.timer)

        self.trace.append((prev, event, self.state, tr.action))
        self.timeline.append(
            {
                "ts": round(time.time(), 3),
                "from": str(prev),
                "event": str(event),
                "to": str(self.state),
                "action": str(tr.action),
            }
        )
        log.info("迁移 %s --%s--> %s (动作 %s)", prev, event, self.state, tr.action)
        return tr

    # ---------------- 异步入口 ----------------

    async def emit(self, event: Event, ctx: JobContext | None = None) -> State:
        tr = self._apply(event, ctx)
        await self._dispatch(tr.action)
        return self.state

    async def _safe_emit(self, event: Event) -> State | None:
        """异步动作完成时状态可能已被超时改掉；此时丢弃该事件而不是让状态机崩。"""
        if not can_accept(self.state, event):
            log.warning("丢弃过期事件 %s（当前状态 %s）", event, self.state)
            return None
        return await self.emit(event)

    async def submit(self, ctx: JobContext) -> State:
        return await self.emit(Event.JOB_SUBMITTED, ctx=ctx)

    async def image_done(self, image_path: str | None = None) -> State:
        if self.ctx is None:
            raise ValueError("image_done 前没有作业")
        if image_path:
            self.ctx.images.append(image_path)
        self.ctx.done += 1
        ev = Event.IMAGE_DONE_MORE if self.ctx.has_more else Event.IMAGE_DONE_LAST
        return await self.emit(ev)

    async def cancel(self) -> State:
        if self.ctx:
            self.ctx.status = "cancelled"
            self.ctx.error = "cancelled_by_user"
        return await self.emit(Event.CANCEL)

    async def guard_tripped(self) -> State:
        return await self.emit(Event.GUARD_TRIPPED)

    async def prepare_ok(self) -> State:
        return await self.emit(Event.PREPARE_OK)

    async def reload_ok(self) -> State:
        return await self.emit(Event.RELOAD_OK)

    # ---------- 兼容同步单测的入口（生产请用 emit） ----------

    def _sync_only(self, name: str) -> None:
        if self.runtime is not None:
            raise RuntimeError(f"{name}() 是同步单测入口；生产请 await emit(Event.X)")

    def submit_sync(self, ctx: JobContext) -> State:
        self._sync_only("submit_sync")
        return self._apply(Event.JOB_SUBMITTED, ctx).next_state

    # ---------------- 动作分发 ----------------

    async def _dispatch(self, action: Action) -> None:
        self.action_log.append(action)
        rt = self.runtime

        if action is Action.UNLOAD_CHAT:
            if rt:
                await rt.unload_chat()
                await self._safe_emit(Event.PREPARE_OK)
            else:
                self._book_ctx("preparing")

        elif action is Action.SUBMIT_IMAGE:
            self._book_ctx("generating")           # 记账与是否有 runtime 无关
            if rt:
                self._spawn(self._do_image())

        elif action is Action.AFTER_IMAGE_MORE:
            self._book_ctx("generating")
            if rt:
                self._spawn(self._do_image())

        elif action is Action.AFTER_IMAGE_LAST:
            self._book_ctx("done")
            if rt:
                self._spawn(self._load_and_emit())

        elif action is Action.KILL_SD:
            if rt:
                if self.state is State.RELOADING:
                    await rt.kill_sd()          # 取消：立刻重启，1s 级恢复
                else:
                    await rt.stop_sd()          # 超时/异常/护栏：只杀
                    self._spawn(self._cleanup_and_emit())   # 重启统一交给 cleanup
            if self.state is State.RELOADING and rt:
                self._spawn(self._load_and_emit())

        elif action is Action.FORCE_KILL_CHAT:
            if rt:
                await rt.force_kill_chat()
                self._spawn(self._cleanup_and_emit())

        elif action is Action.ABORT_JOB:
            if self.ctx:
                self.ctx.status = "failed"
                self.ctx.error = "vram_guard_tripped"
            if rt:
                await rt.kill_sd()
                self._spawn(self._cleanup_and_emit())

        elif action is Action.LOAD_CHAT:
            if rt and self.state is State.RELOADING:
                self._spawn(self._load_and_emit())

        elif action is Action.CLEANUP_RESTART:
            if rt:
                self._spawn(self._cleanup_and_emit())

        elif action is Action.STOP_AUTO_RECOVERY:
            if self.ctx:
                self.ctx.error = self.ctx.error or "degraded_cleanup_failed"

    # ---------------- 异步子流程 ----------------

    async def _do_image(self) -> None:
        assert self.ctx is not None and self.runtime is not None
        try:
            path = await self.runtime.submit_image(self.ctx)
        except asyncio.CancelledError:
            raise
        except Exception as e:                                # 动作失败 → 交回状态机处理
            log.warning("出图失败: %s", e)
            if can_accept(self.state, Event.IMAGE_FAILED):
                self.ctx.status = "failed"
                await self.emit(Event.IMAGE_FAILED)          # 原因记为 image_failed
            elif not self.ctx.error:
                # 状态已被别的事件带走（如护栏），保留更具体的原因
                self.ctx.error = f"submit_failed: {e}"
            return
        await self.image_done(path)

    async def _load_and_emit(self) -> None:
        assert self.runtime is not None
        ok = await self.runtime.load_chat()
        if ok:
            await self._safe_emit(Event.RELOAD_OK)
        # 失败则等 RELOAD 计时器发 RELOAD_TIMEOUT，不在这里抢

    async def _cleanup_and_emit(self) -> None:
        assert self.runtime is not None
        ok = await self.runtime.cleanup_restart()
        if ok:
            await self._safe_emit(Event.CLEANUP_OK)

    # ---------------- 计时器 ----------------

    def _arm_timer(self, timer: Timer) -> None:
        if self.runtime is None:
            return                                          # 单测模式不起 task
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        seconds = self.timeouts.get(str(timer).replace("_timer", ""), 10.0)
        ev = TIMER_EVENTS[timer]
        self._timer_task = asyncio.create_task(self._fire(seconds, ev))

    async def _fire(self, seconds: float, ev: Event) -> None:
        try:
            await asyncio.sleep(seconds)
        except asyncio.CancelledError:
            return
        log.warning("超时 %.0fs → %s", seconds, ev)
        await self._safe_emit(ev)

    def _cancel_timer(self) -> None:
        if self._timer_task and not self._timer_task.done():
            self._timer_task.cancel()
        self._timer_task = None

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def shutdown(self) -> None:
        self._cancel_timer()
        for t in list(self._tasks):
            t.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    # ---------------- 只读视图 ----------------

    def _book_ctx(self, status: str) -> None:
        if self.ctx:
            self.ctx.status = status
        self._assert_ctx_complete()

    REQUIRED_CTX_FIELDS = ("job_id", "prompt", "width", "height", "steps", "seed", "total", "done", "status")

    def _assert_ctx_complete(self) -> None:
        """接口字段与 ctx 字段对齐的兜底：缺字段在测试期就炸，别等到前端拿空值。"""
        if self.ctx is None:
            return
        missing = [f for f in self.REQUIRED_CTX_FIELDS if getattr(self.ctx, f, None) is None]
        if missing:
            raise AssertionError(f"JobContext 缺字段: {missing}")

    def _book_job_cancel_or_fail(self) -> None:
        if not self.ctx:
            return
        if self.ctx.error:
            self.ctx.status = "failed"
        else:
            self.ctx.status = "cancelled"
            self.ctx.error = "cancelled_by_user"

    @property
    def can_chat(self) -> bool:
        return self.state is State.CHAT_READY

    def snapshot(self) -> dict[str, Any]:
        ctx = self.ctx
        return {
            "state": str(self.state),
            "timer": str(self.timer) if self.timer else None,
            "job": None
            if ctx is None
            else {
                "job_id": ctx.job_id,
                "status": ctx.status,
                "done": ctx.done,
                "total": ctx.total,
                "images": list(ctx.images),
                "error": ctx.error,
            },
        }
