"""对话路由。

- 出图中（state != CHAT_READY）返回 503 {"error":"gpu_busy"}，前端排队等 CHAT_READY。
- max_tokens 先按 n_ctx 钳制：prompt_tokens 走 POST /tokenize（实测可用），失败才估算。
- tool_call 意图走非流式：finish_reason=length 且 content 为空时，用 2048 重试一次。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from gateway.backends.llama import LlamaError
from gateway.config import CHAT_TOOLCALL_RETRY_MAX_TOKENS, estimate_prompt_tokens
from gateway.supervisor.state import State

router = APIRouter()
log = logging.getLogger("api.chat")


def _flatten(messages: list[dict]) -> str:
    parts = []
    for m in messages:
        c = m.get("content")
        if isinstance(c, str):
            parts.append(c)
        elif isinstance(c, list):
            parts.append("".join(x.get("text", "") for x in c if isinstance(x, dict)))
    return "\n".join(parts)


def _is_toolcall(body: dict[str, Any]) -> bool:
    return bool(body.get("tools")) or body.get("tool_choice") not in (None, "none")


_SAMPLING_RANGES = {
    "temperature": (0.0, 2.0),
    "top_p": (0.0, 1.0),
    "top_k": (1, 200),
    "min_p": (0.0, 1.0),
    "repeat_penalty": (0.1, 3.0),
    "presence_penalty": (-2.0, 2.0),
    "seed": (0, 2**31 - 1),
}


def _sampling(body: dict[str, Any]) -> tuple[dict[str, Any], int | None]:
    """对话设置里的采样参数：只透传 llama-server 认的键，越界就地夹住。

    空值（null/""）表示"用模型默认"，不传；这样前端"留空=默认"才有意义。
    """
    src = body.get("params") if isinstance(body.get("params"), dict) else {}
    out: dict[str, Any] = {}
    for k, (lo, hi) in _SAMPLING_RANGES.items():
        v = src.get(k)
        if v is None or v == "":
            continue
        try:
            num = float(v)
        except (TypeError, ValueError):
            continue
        num = max(lo, min(hi, num))
        out[k] = int(num) if k in ("top_k", "seed") else num

    cap = None
    raw = body.get("max_tokens") if body.get("max_tokens") is not None else src.get("max_tokens")
    if raw not in (None, ""):
        try:
            cap = max(1, int(raw))
        except (TypeError, ValueError):
            cap = None
    return out, cap


@router.get("/api/chat/idle")
async def chat_idle(request: Request):
    """停止后的解禁依据：返回当前有多少路在生成，不是"猜一个 1.2s 够不够"。

    busy=None 表示探测失败（llama 没起来/超时），调用方按"还不确定"处理。
    """
    import asyncio
    llama = request.app.state.llama
    busy = await asyncio.to_thread(llama.busy_slots)
    return JSONResponse({"busy": busy, "idle": busy == 0})


@router.post("/api/chat")
async def chat(request: Request):
    app = request.app
    sup = app.state.sup
    llama = app.state.llama
    cfg = app.state.cfg

    if sup.state is not State.CHAT_READY:
        return JSONResponse({"error": "gpu_busy", "state": str(sup.state)}, status_code=503)
    # 顶栏把对话模型手动关了：状态机仍是 CHAT_READY（它表示"可以接作业"），
    # 但端口是空的，必须单独拦一道，否则 llama 连不上会以 500 形式暴露。
    if not app.state.ready.get("chat"):
        return JSONResponse({"error": "chat_stopped",
                             "detail": "对话模型已关闭，去「设置」页点「启动对话模型」"}, status_code=503)

    body: dict[str, Any] = await request.json()
    messages = body.get("messages") or []
    if not messages:
        return JSONResponse({"error": "empty_messages"}, status_code=400)

    prompt_text = _flatten(messages)
    prompt_tokens = await asyncio_count(llama, prompt_text)
    sampling, cap = _sampling(body)
    max_tokens = cfg.effective_max_tokens(prompt_tokens, cap=cap)
    extra = {k: body[k] for k in ("tools", "tool_choice", "chat_template_kwargs", "stop") if k in body}
    extra.update(sampling)

    # 思考开关是真的：Gemma 模板认 chat_template_kwargs 的 enable_thinking。
    if "chat_template_kwargs" not in extra and not cfg.settings.thinking:
        extra["chat_template_kwargs"] = {"enable_thinking": False}

    if _is_toolcall(body):
        return await _toolcall(llama, messages, max_tokens, extra, prompt_tokens)

    async def gen():
        usage: dict[str, Any] | None = None
        finish: str | None = None
        stopped = False
        content_len = 0
        reason_buf: list[str] = []
        degraded = None
        try:
            async for chunk in llama.chat_stream(messages, max_tokens, extra=extra or None):
                if chunk.get("usage"):
                    usage = chunk["usage"]
                for ch in chunk.get("choices") or []:
                    d = ch.get("delta") or {}
                    if d.get("reasoning_content"):
                        reason_buf.append(d["reasoning_content"])
                        yield sse({"type": "reasoning", "text": d["reasoning_content"]})
                    if d.get("content"):
                        content_len += len(d["content"])
                        yield sse({"type": "content", "text": d["content"]})
                    if ch.get("finish_reason"):
                        finish = ch["finish_reason"]
        except asyncio.CancelledError:
            # 客户端断开（前端"停止"就是断这条流）：generator 取消 → httpx 关连接 →
            # llama-server 侧发现连接断开后中止生成。这里不再 yield，只记账。
            stopped = True
            log.info("客户端断开，中止本次生成")
            raise
        except LlamaError as e:
            yield sse({"type": "error", "error": str(e)})
            return
        finally:
            if stopped:
                log.info("已停止：finish=%s", finish)
        # 契约 ② 的兜底：思考吃满预算时正文可能是"完全空"，也可能只剩半句。
        # 前者端上只会看到一个只有思考块的空气泡，必须回填；后者保留正文，只标降级。
        if not stopped and finish == "length":
            if content_len == 0 and reason_buf:
                fallback = "[思考被截断，以下是思考内容]\n" + "".join(reason_buf).strip()
                content_len = len(fallback)
                degraded = "thinking_truncated"
                log.warning("思考吃满预算、正文为空，回填 reasoning（%s 字）", len(fallback))
                yield sse({"type": "content", "text": fallback})
            elif content_len > 0:
                degraded = "truncated"
                log.warning("回答被 max_tokens=%s 截断（正文 %s 字）", max_tokens, content_len)

        # 一行 token 记账：以后要判断"能不能放开 8K"，grep 这行就够，不必另建 CSV
        u = usage or {}
        pt = u.get("prompt_tokens", prompt_tokens)
        ct = u.get("completion_tokens")
        log.info("对话 token: prompt=%s completion=%s total=%s finish=%s ctx=%s/%s",
                 pt, ct, u.get("total_tokens"), finish,
                 (pt or 0) + (ct or 0), cfg.settings.n_ctx)
        yield sse({"type": "done", "finish_reason": finish, "usage": usage, "max_tokens": max_tokens,
                   "prompt_tokens": prompt_tokens, "degraded": degraded})

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


async def asyncio_count(llama, text: str) -> int:
    import asyncio
    n = await asyncio.to_thread(llama.count_tokens, text)
    return n if n is not None else estimate_prompt_tokens(text)


async def _toolcall(llama, messages, max_tokens, extra, prompt_tokens) -> JSONResponse:
    resp = await llama.chat_once(messages, max_tokens, extra=extra or None)
    choice = (resp.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    finish = choice.get("finish_reason")

    if finish == "length" and not (msg.get("content") or "").strip():
        retry_cap = CHAT_TOOLCALL_RETRY_MAX_TOKENS - prompt_tokens - 16
        if retry_cap > max_tokens:
            log.warning("tool_call 被截断且无内容，用 %s 重试一次", retry_cap)
            resp = await llama.chat_once(messages, retry_cap, extra=extra or None)
            choice = (resp.get("choices") or [{}])[0]
            msg = choice.get("message") or {}
            finish = choice.get("finish_reason")
            max_tokens = retry_cap

    if finish == "length" and not (msg.get("content") or "").strip():
        return JSONResponse({"error": "toolcall_truncated", "detail": "重试后仍被截断，请手动填参",
                             "usage": resp.get("usage")}, status_code=502)

    return JSONResponse({"message": msg, "finish_reason": finish, "usage": resp.get("usage"),
                         "max_tokens": max_tokens, "prompt_tokens": prompt_tokens})


def sse(obj: dict[str, Any]) -> str:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"
