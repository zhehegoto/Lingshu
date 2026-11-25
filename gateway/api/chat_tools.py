"""对话里的工具：文件读写 + 联网搜索 / 读取网页。

设计边界：
- 文件工具只能碰「灵枢」应用根目录，读写都走 agent._run_tool 的路径校验；
- 联网工具只读，不需要文件权限；
- 只在用户打开对应工具开关时，才把工具定义交给模型；
- 执行命令不在这里开放。
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from gateway.api.agent import APP_ROOT, TOOLS, _run_tool
from gateway.backends.llama import LlamaError
from gateway.supervisor.state import State

router = APIRouter()
log = logging.getLogger("api.chat_tools")

MAX_ROUNDS = 4


def _enabled_tools(flags: dict) -> list[dict]:
    names = set()
    if flags.get("folder"):
        names.add("list_dir")
    if flags.get("file"):
        names.add("read_file")
    if flags.get("write"):
        names.add("write_file")
    if flags.get("web"):
        names.add("web_search")
    if flags.get("page"):
        names.add("fetch_page")
    return [t for t in TOOLS if t["function"]["name"] in names]


def _tool_system(flags: dict) -> str:
    parts = ["工具已开启。文件工具的工作目录是灵枢应用根目录：%s" % APP_ROOT]
    if flags.get("folder"):
        parts.append("查看文件夹用 list_dir。")
    if flags.get("file"):
        parts.append("读取文件用 read_file（最多返回 4000 字）。")
    if flags.get("write"):
        parts.append("写入文件用 write_file；权限只允许写灵枢应用根目录内。")
    if flags.get("web"):
        parts.append("联网搜索用 web_search，结果里带链接和摘要；引用时说明来源。")
    if flags.get("page"):
        parts.append("读取网页用 fetch_page，只支持 http/https，最多返回 4000 字。")
    parts.append("不要编造没有调用工具得到的内容；工具失败就如实说明。")
    return "\n".join(parts)


@router.post("/api/chat/tools")
async def chat_with_tools(request: Request):
    app = request.app
    if app.state.sup.state is not State.CHAT_READY:
        return JSONResponse({"error": "gpu_busy", "state": str(app.state.sup.state) }, status_code=503)
    if not app.state.ready.get("chat"):
        return JSONResponse({"error": "chat_stopped",
                             "detail": "对话模型已关闭，去「设置」页点「启动对话模型」"}, status_code=503)

    body = await request.json()
    messages = body.get("messages") or []
    flags = body.get("tools") if isinstance(body.get("tools"), dict) else {}
    tools = _enabled_tools(flags)
    if not tools:
        return JSONResponse({"error": "no_tools", "detail": "没有打开可执行的工具"}, status_code=400)

    cfg = app.state.cfg
    max_tokens = int(body.get("max_tokens") or cfg.settings.max_tokens)
    max_tokens = max(64, min(max_tokens, 8192))
    extra: dict = {"tools": tools, "tool_choice": "auto"}
    params = body.get("params") if isinstance(body.get("params"), dict) else {}
    for k in ("temperature", "top_p", "top_k", "min_p", "repeat_penalty", "presence_penalty", "seed"):
        if params.get(k) not in (None, ""):
            extra[k] = params[k]

    msgs = list(messages)
    msgs.insert(0, {"role": "system", "content": _tool_system(flags)})
    trace: list[dict] = []

    try:
        for _ in range(MAX_ROUNDS):
            resp = await app.state.llama.chat_once(msgs, max_tokens, extra=extra)
            choice = (resp.get("choices") or [{}])[0]
            msg = choice.get("message") or {}
            calls = msg.get("tool_calls") or []
            if not calls:
                return {"content": msg.get("content") or "", "tool_trace": trace,
                        "finish_reason": choice.get("finish_reason")}
            msgs.append({"role": "assistant", "content": msg.get("content") or "",
                         "tool_calls": calls})
            for call in calls:
                fn = (call.get("function") or {})
                name = fn.get("name") or ""
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except (TypeError, ValueError):
                    args = {}
                result = _run_tool(name, args, "workspace", APP_ROOT)
                trace.append({"name": name, "args": args,
                              "ok": bool(result.get("ok")), "detail": result.get("detail")})
                msgs.append({"role": "tool", "tool_call_id": call.get("id") or name,
                             "name": name, "content": json.dumps(result, ensure_ascii=False)})
    except LlamaError as e:
        return JSONResponse({"error": "tool_failed", "detail": str(e)}, status_code=502)
    return JSONResponse({"error": "tool_round_limit", "detail": "工具调用轮数达到上限"}, status_code=502)
