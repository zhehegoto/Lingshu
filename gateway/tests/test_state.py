import unittest

from gateway.supervisor.machine import JobContext, Supervisor
from gateway.supervisor.state import (
    TRANSITIONS,
    Action,
    Event,
    State,
    Timer,
    UnknownTransition,
    can_accept,
    transition,
)


class TestTransitionTable(unittest.TestCase):
    """纯同步，不碰 async、不碰进程。"""

    def test_table_has_exactly_thirteen_rows(self):
        self.assertEqual(len(TRANSITIONS), 13)

    def test_each_row_is_consistent(self):
        for (state, event), tr in TRANSITIONS.items():
            self.assertIsInstance(state, State)
            self.assertIsInstance(event, Event)
            self.assertIsInstance(tr.next_state, State)
            self.assertIsInstance(tr.action, Action)

    def test_key_transitions(self):
        cases = [
            (State.CHAT_READY, Event.JOB_SUBMITTED, State.PREPARING, Action.UNLOAD_CHAT, Timer.PREPARE),
            (State.PREPARING, Event.PREPARE_OK, State.GENERATING, Action.SUBMIT_IMAGE, Timer.IMAGE),
            (State.PREPARING, Event.PREPARE_TIMEOUT, State.DEGRADED, Action.FORCE_KILL_CHAT, Timer.CLEANUP),
            (State.GENERATING, Event.IMAGE_DONE_MORE, State.GENERATING, Action.AFTER_IMAGE_MORE, Timer.IMAGE),
            (State.GENERATING, Event.IMAGE_DONE_LAST, State.RELOADING, Action.AFTER_IMAGE_LAST, Timer.RELOAD),
            (State.GENERATING, Event.IMAGE_FAILED, State.DEGRADED, Action.KILL_SD, Timer.CLEANUP),
            (State.GENERATING, Event.IMAGE_TIMEOUT, State.DEGRADED, Action.KILL_SD, Timer.CLEANUP),
            (State.GENERATING, Event.CANCEL, State.RELOADING, Action.KILL_SD, Timer.RELOAD),
            (State.GENERATING, Event.GUARD_TRIPPED, State.DEGRADED, Action.ABORT_JOB, Timer.CLEANUP),
            (State.RELOADING, Event.RELOAD_OK, State.CHAT_READY, Action.LOAD_CHAT, None),
            (State.RELOADING, Event.RELOAD_TIMEOUT, State.DEGRADED, Action.FORCE_KILL_CHAT, Timer.CLEANUP),
            (State.DEGRADED, Event.CLEANUP_OK, State.RELOADING, Action.LOAD_CHAT, Timer.RELOAD),
            (State.DEGRADED, Event.CLEANUP_TIMEOUT, State.FAILED, Action.STOP_AUTO_RECOVERY, None),
        ]
        for state, event, next_state, action, timer in cases:
            tr = transition(state, event)
            self.assertEqual(tr.next_state, next_state, f"{state}+{event}")
            self.assertEqual(tr.action, action, f"{state}+{event}")
            self.assertEqual(tr.timer, timer, f"{state}+{event}")

    def test_chat_ready_rejects_image_events(self):
        for ev in (Event.PREPARE_OK, Event.IMAGE_DONE_LAST, Event.RELOAD_OK, Event.CLEANUP_OK):
            with self.assertRaises(UnknownTransition):
                transition(State.CHAT_READY, ev)

    def test_cancel_outside_generating_raises(self):
        for st in (State.CHAT_READY, State.PREPARING, State.RELOADING, State.DEGRADED, State.FAILED):
            self.assertFalse(can_accept(st, Event.CANCEL), f"{st} 不应接受 cancel")

    def test_failed_is_terminal(self):
        for ev in Event:
            self.assertFalse(can_accept(State.FAILED, ev), f"FAILED 不应接受 {ev}")


class TestSupervisorTrajectory(unittest.IsolatedAsyncioTestCase):
    """无 runtime（runtime=None）时动作只记账，断言状态轨迹。"""

    def _sup(self) -> Supervisor:
        return Supervisor(runtime=None)

    async def test_happy_path_single_image(self):
        sup = self._sup()
        await sup.submit(JobContext(job_id="j1", total=1))
        self.assertEqual(sup.state, State.PREPARING)
        await sup.prepare_ok()
        self.assertEqual(sup.state, State.GENERATING)
        await sup.image_done("a.png")
        self.assertEqual(sup.state, State.RELOADING)
        await sup.reload_ok()
        self.assertEqual(sup.state, State.CHAT_READY)

        events = [e for _, e, _, _ in sup.trace]
        self.assertEqual(
            events,
            [Event.JOB_SUBMITTED, Event.PREPARE_OK, Event.IMAGE_DONE_LAST, Event.RELOAD_OK],
        )
        self.assertEqual(sup.ctx.images, ["a.png"])
        self.assertEqual(sup.ctx.status, "done")

    async def test_batch_self_loop(self):
        sup = self._sup()
        await sup.submit(JobContext(job_id="j2", total=3))
        await sup.prepare_ok()
        self.assertEqual(sup.state, State.GENERATING)
        await sup.image_done("1.png")
        self.assertEqual(sup.state, State.GENERATING, "2/3 张时应自环回 GENERATING")
        self.assertEqual(sup.ctx.done, 1)
        await sup.image_done("2.png")
        self.assertEqual(sup.state, State.GENERATING)
        self.assertEqual(sup.ctx.done, 2)
        await sup.image_done("3.png")
        self.assertEqual(sup.state, State.RELOADING, "最后一张应转 RELOADING")
        self.assertEqual(len(sup.ctx.images), 3)

    async def test_prepare_timeout_to_degraded_then_recover(self):
        sup = self._sup()
        await sup.submit(JobContext(job_id="j3"))
        await sup.emit(Event.PREPARE_TIMEOUT)
        self.assertEqual(sup.state, State.DEGRADED)
        await sup.emit(Event.CLEANUP_OK)
        self.assertEqual(sup.state, State.RELOADING)
        await sup.reload_ok()
        self.assertEqual(sup.state, State.CHAT_READY)

    async def test_image_timeout_marks_job_failed(self):
        sup = self._sup()
        await sup.submit(JobContext(job_id="j4"))
        await sup.prepare_ok()
        await sup.emit(Event.IMAGE_TIMEOUT)
        self.assertEqual(sup.state, State.DEGRADED)
        await sup.emit(Event.CLEANUP_OK)
        await sup.reload_ok()
        self.assertEqual(sup.state, State.CHAT_READY)

    async def test_cancel_goes_through_reloading_not_directly_chat_ready(self):
        sup = self._sup()
        await sup.submit(JobContext(job_id="j5"))
        await sup.prepare_ok()
        await sup.cancel()
        self.assertEqual(sup.state, State.RELOADING, "取消后必须先重载对话模型")
        self.assertEqual(sup.ctx.status, "cancelled")
        self.assertEqual(sup.ctx.error, "cancelled_by_user")
        await sup.reload_ok()
        self.assertEqual(sup.state, State.CHAT_READY)

    async def test_guard_tripped_aborts_job(self):
        sup = self._sup()
        await sup.submit(JobContext(job_id="j6"))
        await sup.prepare_ok()
        await sup.guard_tripped()
        self.assertEqual(sup.state, State.DEGRADED)
        self.assertEqual(sup.ctx.status, "failed")
        self.assertEqual(sup.ctx.error, "vram_guard_tripped")

    async def test_degraded_cleanup_timeout_reaches_failed_and_stays(self):
        sup = self._sup()
        await sup.submit(JobContext(job_id="j7"))
        await sup.prepare_ok()
        await sup.cancel()
        self.assertEqual(sup.state, State.RELOADING)
        await sup.emit(Event.RELOAD_TIMEOUT)
        self.assertEqual(sup.state, State.DEGRADED)
        await sup.emit(Event.CLEANUP_TIMEOUT)
        self.assertEqual(sup.state, State.FAILED)
        with self.assertRaises(UnknownTransition):
            await sup.emit(Event.RELOAD_OK)

    async def test_submit_without_ctx_raises(self):
        sup = self._sup()
        with self.assertRaises(ValueError):
            await sup.emit(Event.JOB_SUBMITTED)

    async def test_event_without_job_raises(self):
        sup = self._sup()
        sup.state = State.PREPARING
        sup.ctx = None
        with self.assertRaises(ValueError):
            await sup.prepare_ok()

    async def test_prepare_ok_in_idle_raises_unknown_transition(self):
        sup = self._sup()
        with self.assertRaises(UnknownTransition):
            await sup.prepare_ok()

    async def test_snapshot_shape(self):
        sup = self._sup()
        snap = sup.snapshot()
        self.assertEqual(snap["state"], "CHAT_READY")
        self.assertIsNone(snap["job"])
        await sup.submit(JobContext(job_id="j8", total=2, prompt="x"))
        self.assertEqual(sup.snapshot()["job"]["job_id"], "j8")


if __name__ == "__main__":
    unittest.main(verbosity=2)
