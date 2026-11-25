"""状态机定义：状态、事件、转移表。

设计约束（见 二期结构决定.md 第一节）：
- 所有状态变更只走 transition()，看门狗与超时器只发事件，不直接改状态。
- transition() 是纯函数：不碰进程、不碰 IO，只查表。
- 未定义的 (状态, 事件) 一律抛 UnknownTransition，不静默忽略——
  静默忽略会把"两次事件同时到达"这类 bug 藏到联调阶段。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class State(StrEnum):
    CHAT_READY = "CHAT_READY"
    PREPARING = "PREPARING"
    GENERATING = "GENERATING"
    RELOADING = "RELOADING"
    DEGRADED = "DEGRADED"
    FAILED = "FAILED"


class Event(StrEnum):
    JOB_SUBMITTED = "job_submitted"
    PREPARE_OK = "prepare_ok"
    PREPARE_TIMEOUT = "prepare_timeout"
    IMAGE_DONE_MORE = "image_done_more"      # 还有剩余张
    IMAGE_DONE_LAST = "image_done_last"      # 最后一张
    IMAGE_FAILED = "image_failed"            # 连接被重置 / HTTP 错误：sd 挂了，不是超时
    IMAGE_TIMEOUT = "image_timeout"
    CANCEL = "cancel"
    GUARD_TRIPPED = "guard_tripped"
    RELOAD_OK = "reload_ok"
    RELOAD_TIMEOUT = "reload_timeout"
    CLEANUP_OK = "cleanup_ok"
    CLEANUP_TIMEOUT = "cleanup_timeout"


class Action(StrEnum):
    UNLOAD_CHAT = "unload_chat"
    SUBMIT_IMAGE = "submit_image"
    FORCE_KILL_CHAT = "force_kill_chat"
    AFTER_IMAGE_MORE = "after_image_more"
    AFTER_IMAGE_LAST = "after_image_last"
    ABORT_JOB = "abort_job"
    KILL_SD = "kill_sd"
    LOAD_CHAT = "load_chat"
    CLEANUP_RESTART = "cleanup_restart"
    STOP_AUTO_RECOVERY = "stop_auto_recovery"


class Timer(StrEnum):
    PREPARE = "prepare_timer"
    IMAGE = "image_timer"
    RELOAD = "reload_timer"
    CLEANUP = "cleanup_timer"


@dataclass(frozen=True, slots=True)
class Transition:
    next_state: State
    action: Action
    timer: Timer | None = None


class UnknownTransition(RuntimeError):
    def __init__(self, state: State, event: Event):
        super().__init__(f"未定义的转移：状态={state} 事件={event}")
        self.state = state
        self.event = event


def _t(next_state: State, action: Action, timer: Timer | None = None) -> Transition:
    return Transition(next_state, action, timer)


# 12 条转移，与 二期结构决定.md 第一节的表格逐行对应
TRANSITIONS: dict[tuple[State, Event], Transition] = {
    (State.CHAT_READY, Event.JOB_SUBMITTED): _t(State.PREPARING, Action.UNLOAD_CHAT, Timer.PREPARE),
    (State.PREPARING, Event.PREPARE_OK): _t(State.GENERATING, Action.SUBMIT_IMAGE, Timer.IMAGE),
    (State.PREPARING, Event.PREPARE_TIMEOUT): _t(State.DEGRADED, Action.FORCE_KILL_CHAT, Timer.CLEANUP),
    (State.GENERATING, Event.IMAGE_DONE_MORE): _t(State.GENERATING, Action.AFTER_IMAGE_MORE, Timer.IMAGE),
    (State.GENERATING, Event.IMAGE_DONE_LAST): _t(State.RELOADING, Action.AFTER_IMAGE_LAST, Timer.RELOAD),
    (State.GENERATING, Event.IMAGE_FAILED): _t(State.DEGRADED, Action.KILL_SD, Timer.CLEANUP),
    (State.GENERATING, Event.IMAGE_TIMEOUT): _t(State.DEGRADED, Action.KILL_SD, Timer.CLEANUP),
    (State.GENERATING, Event.CANCEL): _t(State.RELOADING, Action.KILL_SD, Timer.RELOAD),
    (State.GENERATING, Event.GUARD_TRIPPED): _t(State.DEGRADED, Action.ABORT_JOB, Timer.CLEANUP),
    (State.RELOADING, Event.RELOAD_OK): _t(State.CHAT_READY, Action.LOAD_CHAT, None),
    (State.RELOADING, Event.RELOAD_TIMEOUT): _t(State.DEGRADED, Action.FORCE_KILL_CHAT, Timer.CLEANUP),
    (State.DEGRADED, Event.CLEANUP_OK): _t(State.RELOADING, Action.LOAD_CHAT, Timer.RELOAD),
    (State.DEGRADED, Event.CLEANUP_TIMEOUT): _t(State.FAILED, Action.STOP_AUTO_RECOVERY, None),
}

TERMINAL_STATES: frozenset[State] = frozenset({State.FAILED})


def transition(state: State, event: Event) -> Transition:
    """查表。未定义即抛，调用方不应 catch 后忽略。"""
    try:
        return TRANSITIONS[(state, event)]
    except KeyError:
        raise UnknownTransition(state, event) from None


def can_accept(state: State, event: Event) -> bool:
    return (state, event) in TRANSITIONS
