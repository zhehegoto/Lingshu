"""聊天记录：一条会话一个 JSON，文件名 = conversation_id（主键，白名单校验）。

title 只做显示，可改、可重复，不参与文件名——和图片重命名同一套原则。
删除软删到 _trash/，可恢复。第一次有新写操作之前，整目录一次性备份到 <chats>_backup/。
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import time
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

log = logging.getLogger("api.chats")

router = APIRouter()

_ID_OK = re.compile(r"^[A-Za-z0-9_-]{1,48}$")
TRASH = "_trash"
_BAD_TITLE = '/\\:*?"<>|'


def _dir(request: Request) -> Path:
    d = Path(request.app.state.cfg.runtime.chats_dir)
    if d.exists() and not d.is_dir():
        raise RuntimeError(f"chats.dir 指向的是文件而不是目录：{d}")
    d.mkdir(parents=True, exist_ok=True)
    return d


def _backup_dir(d: Path) -> Path:
    return d.parent / f"{d.name}_backup"


def _ensure_backup(d: Path) -> Path:
    """一次性备份：判据是备份目录是否存在，不是时间窗口。之后不再自动复制。"""
    b = _backup_dir(d)
    if not b.exists():
        shutil.copytree(d, b)
    return b


def _safe_id(raw: str) -> str | None:
    return raw if _ID_OK.match(raw or "") else None


def _auto_title(messages: list[dict]) -> str:
    for m in messages:
        if m.get("role") == "user":
            text = str(m.get("content") or "").strip()
            if text:
                return text[:20] + ("…" if len(text) > 20 else "")
    return f"对话 {time.strftime('%m-%d %H:%M')}"


def _read(f: Path) -> dict | None:
    try:
        doc = json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return None
    return doc if isinstance(doc, dict) else None


def _doc_view(f: Path, doc: dict, deleted: bool) -> dict:
    """列表项：title 缺失时按首条用户消息补显示（不写回文件）。"""
    msgs = doc.get("messages") or []
    title = str(doc.get("title") or "").strip() or _auto_title(msgs)
    return {
        "id": f.stem,
        "title": title,
        # 字段缺失视为自动标题：旧文件本来就没这个键，硬写回要多改一次文件
        "title_auto": bool(doc.get("title_auto", True)),
        "updated_at": doc.get("updated_at") or f.stat().st_mtime,
        "messages": len(msgs),
        "deleted": deleted,
    }


@router.post("/api/chats/{chat_id}/export")
async def export_chat(request: Request, chat_id: str) -> JSONResponse:
    """把一条会话导成 Markdown，落在 chats/exports/ 下。

    不弹浏览器的下载框：这个前端跑在 WebView 里，下载行为各家不一；写文件更可靠，
    导出完把路径告诉用户，并告诉他「打开 chats 文件夹」在哪。
    """
    cid = _safe_id(chat_id)
    if cid is None:
        return JSONResponse({"error": "invalid_id"}, status_code=400)
    d = _dir(request)
    src = d / f"{cid}.json"
    if not src.exists():
        return JSONResponse({"error": "chat_not_found"}, status_code=404)
    doc = _read(src) or {}
    msgs = doc.get("messages") or []
    title = str(doc.get("title") or "").strip() or _auto_title(msgs)

    lines = [f"# {title}", "", f"导出时间：{time.strftime('%Y-%m-%d %H:%M')}", ""]
    roles = {"user": "我", "assistant": "灵枢", "system": "系统设定"}
    for m in msgs:
        text = str(m.get("content") or "").strip()
        if not text:
            continue
        lines += [f"**{roles.get(str(m.get('role')), str(m.get('role') or '消息'))}**", "", text, ""]

    safe = "".join("_" if c in _BAD_TITLE else c for c in title).strip()[:40] or cid
    out_dir = d / "exports"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{time.strftime('%Y%m%d-%H%M')}-{safe}.md"
    try:
        out.write_text("\n".join(lines), encoding="utf-8")
    except OSError as exc:
        log.warning("导出会话失败 %s：%s", cid, exc)
        return JSONResponse({"error": "export_failed", "detail": str(exc)}, status_code=500)
    log.info("会话已导出：%s", out.name)
    return {"ok": True, "path": str(out), "name": out.name, "chats_dir": str(d),
            "messages": len(msgs)}


@router.get("/api/chats")
async def list_chats(request: Request, limit: int = 50, include_deleted: bool = False) -> dict:
    d = _dir(request)
    items: list[dict] = []
    for f in d.glob("*.json"):
        doc = _read(f)
        if doc:
            items.append(_doc_view(f, doc, False))
    if include_deleted:
        trash = d / TRASH
        if trash.exists():
            for f in trash.glob("*.json"):
                doc = _read(f)
                if doc:
                    items.append(_doc_view(f, doc, True))
    items.sort(key=lambda x: float(x["updated_at"]), reverse=True)
    return {"items": items[: max(1, min(limit, 200))]}


@router.get("/api/chats/{chat_id}")
async def get_chat(request: Request, chat_id: str) -> JSONResponse:
    cid = _safe_id(chat_id)
    if cid is None:
        return JSONResponse({"error": "invalid_id"}, status_code=400)
    f = _dir(request) / f"{cid}.json"
    if not f.exists():
        return JSONResponse({"error": "chat_not_found"}, status_code=404)
    doc = _read(f) or {}
    doc.setdefault("id", cid)
    doc.setdefault("title", _auto_title(doc.get("messages") or []))
    doc.setdefault("title_auto", True)
    return JSONResponse(doc)


@router.post("/api/chats")
async def save_chat(request: Request, body: dict) -> JSONResponse:
    messages = body.get("messages") or []
    if not isinstance(messages, list):
        return JSONResponse({"error": "invalid_messages"}, status_code=400)

    d = _dir(request)
    cid = _safe_id(body.get("id") or "") or f"c{int(time.time() * 1000) % 100000000:08d}"
    f = d / f"{cid}.json"
    old = _read(f) or {}
    given = str(body.get("title") or "").strip()

    if given:
        title, title_auto = given, False
    elif old.get("title") and not old.get("title_auto", True):
        title, title_auto = old["title"], False     # 用户改过的标题，后续保存不能覆盖
    else:
        title, title_auto = _auto_title(messages), True

    doc = {"id": cid, "title": title, "title_auto": title_auto,
           "updated_at": time.time(), "messages": messages,
           "settings": body.get("settings") if isinstance(body.get("settings"), dict)
                       else old.get("settings", {})}
    tmp = f.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(f)
    return JSONResponse({"id": cid, "messages": len(messages)}, status_code=201)


@router.patch("/api/chats/{chat_id}")
async def rename_chat(request: Request, chat_id: str, body: dict) -> JSONResponse:
    cid = _safe_id(chat_id)
    if cid is None:
        return JSONResponse({"error": "invalid_id"}, status_code=400)
    d = _dir(request)
    f = d / f"{cid}.json"
    if not f.exists():
        return JSONResponse({"error": "chat_not_found"}, status_code=404)

    raw = body.get("title")
    if not isinstance(raw, str):
        return JSONResponse({"error": "invalid_title", "detail": "title 必须是字符串"}, status_code=400)
    title = raw.strip()
    if not title:
        return JSONResponse({"error": "invalid_title", "detail": "标题不能为空"}, status_code=400)
    if len(title) > 40:
        return JSONResponse({"error": "invalid_title", "detail": "标题最长 40 字"}, status_code=400)
    if any(ch in title for ch in _BAD_TITLE) or any(ord(ch) < 32 for ch in title):
        return JSONResponse({"error": "invalid_title",
                             "detail": '标题不能包含 / \\ : * ? " < > | 或控制字符'}, status_code=400)

    _ensure_backup(d)
    doc = _read(f) or {"id": cid, "messages": []}
    doc["title"] = title
    doc["title_auto"] = False
    doc.setdefault("updated_at", time.time())
    tmp = f.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(f)
    return JSONResponse({"ok": True, "id": cid, "title": title, "title_auto": False})


@router.patch("/api/chats/{chat_id}/settings")
async def save_chat_settings(request: Request, chat_id: str, body: dict) -> JSONResponse:
    """对话设置（系统提示词 / 采样参数）跟着会话走，不跟全局配置混。

    这样换会话不用手动恢复上下文：每个会话有自己的角色设定和温度。
    """
    cid = _safe_id(chat_id)
    if cid is None:
        return JSONResponse({"error": "invalid_id"}, status_code=400)
    d = _dir(request)
    f = d / f"{cid}.json"
    if not f.exists():
        return JSONResponse({"error": "chat_not_found"}, status_code=404)
    settings = body.get("settings")
    if not isinstance(settings, dict):
        return JSONResponse({"error": "invalid_settings"}, status_code=400)
    _ensure_backup(d)
    doc = _read(f) or {"id": cid, "messages": []}
    doc["settings"] = settings
    doc["updated_at"] = doc.get("updated_at") or time.time()
    tmp = f.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(f)
    return JSONResponse({"ok": True, "id": cid, "settings": settings})


@router.delete("/api/chats/{chat_id}")
async def delete_chat(request: Request, chat_id: str) -> JSONResponse:
    cid = _safe_id(chat_id)
    if cid is None:
        return JSONResponse({"error": "invalid_id"}, status_code=400)
    d = _dir(request)
    f = d / f"{cid}.json"
    if not f.exists():
        return JSONResponse({"error": "chat_not_found"}, status_code=404)
    _ensure_backup(d)
    trash = d / TRASH
    trash.mkdir(parents=True, exist_ok=True)
    f.replace(trash / f.name)
    return JSONResponse({"ok": True, "id": cid, "moved": f"{TRASH}/{cid}.json"})


@router.post("/api/chats/purge")
async def purge_chat(request: Request, body: dict) -> JSONResponse:
    """彻底删除 `chats/_trash/` 下的某条会话（不进任何地方）。

    和图片侧同构：软删除必须有真删除的出口，否则回收站只进不出。
    """
    cid = _safe_id(body.get("id") if isinstance(body.get("id"), str) else "")
    d = _dir(request)

    # {"all": true} = 清空 chats/_trash（单个接口、两种粒度；标识由 body 给，不挂动作段）
    if body.get("all") is True:
        trash = d / TRASH
        removed = [p.stem for p in sorted(trash.glob("*.json"))] if trash.exists() else []
        for p in sorted(trash.glob("*.json")) if trash.exists() else []:
            p.unlink()
        log.warning("清空会话回收站 %s 条", len(removed))
        return JSONResponse({"ok": True, "removed": removed, "count": len(removed)})

    if cid is None:
        return JSONResponse({"error": "invalid_id"}, status_code=400)
    src = d / TRASH / f"{cid}.json"
    if not src.exists():
        return JSONResponse({"error": "not_in_trash"}, status_code=404)
    src.unlink()
    log.warning("彻底删除会话 %s", cid)
    return JSONResponse({"ok": True, "id": cid})


@router.post("/api/chats/{chat_id}/restore")
async def restore_chat(request: Request, chat_id: str) -> JSONResponse:
    cid = _safe_id(chat_id)
    if cid is None:
        return JSONResponse({"error": "invalid_id"}, status_code=400)
    d = _dir(request)
    src = d / TRASH / f"{cid}.json"
    if not src.exists():
        return JSONResponse({"error": "not_in_trash"}, status_code=404)
    dst = d / f"{cid}.json"
    if dst.exists():
        return JSONResponse({"error": "already_exists"}, status_code=409)
    src.replace(dst)
    return JSONResponse({"ok": True, "id": cid})
