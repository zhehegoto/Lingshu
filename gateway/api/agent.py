"""Agent：计划 + 工作目录 + 三档权限 + 文件工具。

边界：
- 工作目录可以选整台电脑上的任意文件夹，不再锁死在「灵枢」目录；
- 不提供执行命令（shell）工具——本机完全访问下，模型幻觉一条 rm 的代价不可逆；
- 「仅限查看」= 只读当前工作目录；「仅限工作区」= 可读写当前工作目录；
  「完全权限」= 可读写整台电脑上的任意路径（用户明确选择才生效）。

模型用 llama-server 的 tool_call 能力。本地小模型的 tool_call 不可全信，所以每个
参数都要在服务端重新校验：路径按权限解析，写文件大小有上限。
"""

from __future__ import annotations

import html
import json
import logging
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from gateway.backends.llama import LlamaError

router = APIRouter()
log = logging.getLogger("api.agent")

MAX_TOOL_ROUNDS = 5
MAX_READ_CHARS = 4000
MAX_WRITE_BYTES = 200 * 1024
MAX_WEB_BYTES = 1_200_000
MAX_WEB_CHARS = 4000
_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "build", "dist"}
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
_BING_SEARCH = "https://cn.bing.com/search"

APP_ROOT = Path(__file__).resolve().parents[2]

TOOLS = [
    {"type": "function", "function": {
        "name": "list_dir",
        "description": "列出目录里的文件和子目录（只读）。path 用绝对路径或相对工作区。",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "目录路径，空 = 工作区根"}},
            "required": []}}},
    {"type": "function", "function": {
        "name": "read_file",
        "description": "读取一个文本文件（只读，最多返回 4000 字）。",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "文件路径"}},
            "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "write_file",
        "description": "新建或覆盖一个文本文件。权限不够会被拒绝。",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "文件路径"},
            "content": {"type": "string", "description": "完整文件内容"}},
            "required": ["path", "content"]}}},
    {"type": "function", "function": {
        "name": "web_search",
        "description": "联网搜索网页，返回标题、链接和摘要（只读）。",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "搜索关键词"}},
            "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "fetch_page",
        "description": "读取一个 http/https 网页的正文（只读，最多返回 4000 字）。",
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string", "description": "网页地址"}},
            "required": ["url"]}}},
]


def _inside(p: Path, root: Path) -> bool:
    try:
        p.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def _workdir(raw: str) -> Path | None:
    """任意绝对路径都可作为工作目录；相对路径按应用根解析。"""
    if not raw:
        return APP_ROOT
    p = Path(raw)
    if not p.is_absolute():
        p = APP_ROOT / p
    return p


def _resolve_target(raw: str, workdir: Path) -> Path | None:
    if not raw:
        return None
    p = Path(raw)
    if not p.is_absolute():
        p = workdir / p
    return p


def _perm_ok(permission: str, action: str, target: Path, workdir: Path) -> tuple[bool, str]:
    if permission not in ("view", "workspace", "full"):
        return False, "未知权限"
    # 完全权限 = 整台电脑任意路径；其余两档只在当前工作目录内活动
    if permission == "full":
        return True, ""
    if action in ("list", "read"):
        inside = _inside(target, workdir)
        return (inside, "" if inside else "「仅限查看/仅限工作区」只能读当前工作目录")
    if action == "write":
        if permission == "view":
            return False, "「仅限查看」不能写文件"
        inside = _inside(target, workdir)
        return (inside, "" if inside else "「仅限工作区」只能写当前工作目录")
    return False, "未知动作"


def _http_text(url: str) -> str:
    if not url.startswith(("http://", "https://")):
        raise ValueError("只支持 http/https 地址")
    with httpx.Client(timeout=15.0, follow_redirects=True, headers={"User-Agent": _UA}) as client:
        resp = client.get(url)
        resp.raise_for_status()
        ctype = resp.headers.get("content-type", "")
        if any(x in ctype for x in ("image/", "audio/", "video/", "application/octet-stream")):
            raise ValueError("这个地址不是网页正文")
        return resp.text[:MAX_WEB_BYTES]


def _plain(text: str) -> str:
    text = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _web_search(query: str) -> dict:
    """用 Bing 中国站（国内可直连）检索，返回最多 6 条标题/链接/摘要。

    不依赖 API key，也不把搜索结果当可信事实：模型只把它们当“网上说了什么”，
    最终答复仍要注明来源。DuckDuckGo 的 HTML 端点在部分网络下会连不通，所以
    这里不再作为主路径。
    """
    q = quote(query.strip())
    page = _http_text(f"{_BING_SEARCH}?q={q}&setlang=zh-CN&count=10")
    blocks = re.findall(r'(?is)<li class="b_algo".*?</li>', page)
    items = []
    for block in blocks[:6]:
        m = re.search(r'(?is)<h2[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', block)
        if not m:
            continue
        href, title = html.unescape(m.group(1)), _plain(m.group(2))
        sm = re.search(r'(?is)<p class="b_lineclamp[^"]*"[^>]*>(.*?)</p>', block)
        snippet = _plain(sm.group(1))[:220] if sm else ""
        if not title:
            continue
        items.append({"title": title[:100], "url": href, "snippet": snippet})
    if not items:
        return {"ok": False, "detail": "搜索没有返回结果（可能被网络或搜索引擎拦截）"}
    return {"ok": True, "detail": f"{len(items)} 条结果", "data": {"query": query, "items": items}}


def _fetch_page(url: str) -> dict:
    page = _http_text(url.strip())
    title = ""
    m = re.search(r"(?is)<title[^>]*>(.*?)</title>", page)
    if m:
        title = _plain(m.group(1))[:120]
    text = _plain(page)
    truncated = len(text) > MAX_WEB_CHARS
    text = text[:MAX_WEB_CHARS]
    return {"ok": True, "detail": f"{len(text)} 字" + ("（已截断）" if truncated else ""),
            "data": {"url": url, "title": title, "text": text, "truncated": truncated}}


def _run_tool(name: str, args: dict, permission: str, workdir: Path) -> dict:
    if name == "web_search":
        query = str(args.get("query") or "").strip()
        if not query:
            return {"ok": False, "detail": "缺少 query"}
        try:
            return _web_search(query)
        except Exception as e:
            return {"ok": False, "detail": f"搜索失败：{str(e)[:160]}"}
    if name == "fetch_page":
        url = str(args.get("url") or "").strip()
        if not url:
            return {"ok": False, "detail": "缺少 url"}
        try:
            return _fetch_page(url)
        except Exception as e:
            return {"ok": False, "detail": f"读取网页失败：{str(e)[:160]}"}
    if name == "list_dir":
        raw = str(args.get("path") or "").strip()
        target = _resolve_target(raw, workdir) if raw else workdir
        if target is None:
            return {"ok": False, "detail": "路径为空"}
        ok, why = _perm_ok(permission, "list", target, workdir)
        if not ok:
            return {"ok": False, "detail": why}
        if not target.is_dir():
            return {"ok": False, "detail": f"不是目录：{target}"}
        rows = []
        for p in sorted(target.iterdir(), key=lambda x: x.name.lower())[:120]:
            if p.name in _SKIP_DIRS:
                continue
            try:
                rows.append({"name": p.name, "dir": p.is_dir(), "size": p.stat().st_size})
            except OSError:
                rows.append({"name": p.name, "dir": p.is_dir(), "size": None})
        return {"ok": True, "detail": f"{len(rows)} 项",
                "data": {"path": str(target), "entries": rows}}

    if name == "read_file":
        target = _resolve_target(str(args.get("path") or "").strip(), workdir)
        if target is None:
            return {"ok": False, "detail": "缺少 path"}
        ok, why = _perm_ok(permission, "read", target, workdir)
        if not ok:
            return {"ok": False, "detail": why}
        if not target.is_file():
            return {"ok": False, "detail": f"不是文件：{target}"}
        if target.stat().st_size > 2 * 1024 * 1024:
            return {"ok": False, "detail": "文件超过 2 MB，不读"}
        try:
            text = target.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            return {"ok": False, "detail": f"读取失败：{e}"}
        truncated = len(text) > MAX_READ_CHARS
        text = text[:MAX_READ_CHARS]
        return {"ok": True, "detail": f"{len(text)} 字" + ("（已截断）" if truncated else ""),
                "data": {"path": str(target), "text": text, "truncated": truncated}}

    if name == "write_file":
        target = _resolve_target(str(args.get("path") or "").strip(), workdir)
        content = args.get("content")
        if target is None or not isinstance(content, str):
            return {"ok": False, "detail": "需要 path 和 content"}
        ok, why = _perm_ok(permission, "write", target, workdir)
        if not ok:
            return {"ok": False, "detail": why}
        raw = content.encode("utf-8")
        if len(raw) > MAX_WRITE_BYTES:
            return {"ok": False, "detail": "内容超过 200 KB，不写"}
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
        log.warning("agent 写入 %s（%s 字节，权限 %s）", target, len(raw), permission)
        return {"ok": True, "detail": f"已写入 {len(raw)} 字节", "data": {"path": str(target)}}

    return {"ok": False, "detail": f"未知工具 {name}"}


def _system_prompt(workdir: Path, permission: str) -> str:
    perm = {"view": "仅限查看（只读当前工作目录，不能写任何文件）",
            "workspace": "仅限工作区（可在当前工作目录内读写，不能碰目录外）",
            "full": "完全权限（可读写整台电脑上的任意路径）"}[permission]
    return (
        "你是灵枢（ZEROSOURCE 本机 AI 工作台）的 Agent。你可以调用工具查看和修改文件。\n"
        f"工作目录：{workdir}\n当前权限：{perm}\n"
        "规则：\n"
        "1. 先用 list_dir 了解目录，再按需要 read_file；不要凭想象编文件内容。\n"
        "2. 需要写文件时调用 write_file，content 必须是完整文件内容。\n"
        "3. 不要声称执行过未调用的工具；工具失败就如实说明失败原因。\n"
        "4. 最后用中文给结论：做了什么、改了哪些文件、结果如何。\n"
    )


def _plan_prompt(task: str, workdir: Path) -> list[dict]:
    return [
        {"role": "system", "content": (
            "你是一个任务规划器。把用户的任务拆成 3–6 个可执行步骤。"
            "只输出 JSON 数组，每项是一个短句字符串，不要解释、不要代码块。")},
        {"role": "user", "content": f"工作目录：{workdir}\n任务：{task}"},
    ]


def _parse_plan(text: str, task: str) -> list[str]:
    text = (text or "").strip()
    block = re.search(r"\[[\s\S]*\]", text)
    if block:
        try:
            data = json.loads(block.group(0))
            if isinstance(data, list):
                items = [str(x).strip() for x in data if str(x).strip()]
                if items:
                    return items[:8]
        except Exception:
            pass
    lines = []
    for line in text.splitlines():
        s = re.sub(r"^[\s\-*•]*(\d+[.、)])?\s*", "", line).strip()
        if s and not s.startswith(("```", "{", "[")):
            lines.append(s[:120])
    return lines[:8] or [f"理解任务：{task[:80]}", "检查工作目录", "执行并汇报结果"]


def _num(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _tool_args(raw: Any) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            v = json.loads(raw)
            return v if isinstance(v, dict) else {}
        except Exception:
            return {}
    return {}


@router.post("/api/agent/plan")
async def plan(request: Request, body: dict) -> JSONResponse:
    app = request.app
    if not app.state.ready.get("chat"):
        return JSONResponse({"error": "chat_stopped", "detail": "先启动对话模型"}, status_code=503)
    task = str(body.get("task") or "").strip()
    workdir = _workdir(str(body.get("workdir") or ""))
    if not task:
        return JSONResponse({"error": "empty_task"}, status_code=400)
    if not workdir.is_dir():
        return JSONResponse({"error": "invalid_workdir", "detail": "工作目录不存在或不是文件夹"},
                            status_code=400)
    llama = app.state.llama
    try:
        resp = await llama.chat_once(_plan_prompt(task, workdir), 512, temperature=0.2,
                                     read_timeout=240.0)
        msg = (resp.get("choices") or [{}])[0].get("message") or {}
        text = msg.get("content") or ""
        return JSONResponse({"plan": _parse_plan(text, task), "raw": text[:1200],
                             "workdir": str(workdir)})
    except LlamaError as e:
        return JSONResponse({"error": "plan_failed", "detail": str(e)[:200]}, status_code=502)


@router.post("/api/agent/run")
async def run(request: Request, body: dict) -> JSONResponse:
    app = request.app
    if not app.state.ready.get("chat"):
        return JSONResponse({"error": "chat_stopped", "detail": "先启动对话模型"}, status_code=503)
    task = str(body.get("task") or "").strip()
    permission = str(body.get("permission") or "workspace")
    workdir = _workdir(str(body.get("workdir") or ""))
    if not task:
        return JSONResponse({"error": "empty_task"}, status_code=400)
    if not workdir.is_dir():
        return JSONResponse({"error": "invalid_workdir", "detail": "工作目录不存在或不是文件夹"},
                            status_code=400)

    llama = app.state.llama
    cfg = app.state.cfg
    messages: list[dict] = [
        {"role": "system", "content": _system_prompt(workdir, permission)},
        {"role": "user", "content": task},
    ]
    trace: list[dict] = []
    answer = ""
    try:
        for _ in range(MAX_TOOL_ROUNDS + 1):
            resp = await llama.chat_once(messages, min(cfg.settings.max_tokens, 1536),
                                         temperature=0.3, extra={"tools": TOOLS},
                                         read_timeout=300.0)
            msg = (resp.get("choices") or [{}])[0].get("message") or {}
            content = msg.get("content") or ""
            calls = msg.get("tool_calls") or []
            if not calls:
                answer = content.strip()
                break
            messages.append({"role": "assistant", "content": content, "tool_calls": calls})
            for call in calls:
                fn = (call.get("function") or {})
                name = str(fn.get("name") or "")
                args = _tool_args(fn.get("arguments"))
                res = _run_tool(name, args, permission, workdir)
                trace.append({"tool": name, "args": args, "ok": res.get("ok"),
                              "detail": res.get("detail"), "data": res.get("data")})
                messages.append({"role": "tool", "tool_call_id": call.get("id") or name,
                                 "content": json.dumps(res, ensure_ascii=False)[:6000]})
            # 工具结果回灌后如果已经把上下文塞满，就别再跑下一轮
            if sum(len(str(m.get("content") or "")) for m in messages) > 12000:
                answer = "（工具调用轮次已达上限或上下文接近上限，先给结论）\n" + (content or "")
                break
        else:
            answer = "工具调用轮次达到上限，先停下。"
    except LlamaError as e:
        return JSONResponse({"error": "agent_failed", "detail": str(e)[:300],
                             "trace": trace}, status_code=502)

    if trace:
        log.info("agent 任务完成：%s 步工具调用", len(trace))
    return JSONResponse({"answer": answer or "（模型没有返回正文）", "trace": trace,
                         "workdir": str(workdir), "permission": permission})
