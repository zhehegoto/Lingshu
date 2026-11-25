"""概览 / 日志 / 附件 / 打开文件夹 等跨页面小接口。

不放进各自的功能路由，是因为它们服务的是「壳」：概览要一次拿全机器与状态，
运行页要日志，对话要附件。放在一起，前端只依赖一个模块。
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import subprocess
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse

from gateway import config as cfgmod
from gateway import hardware, logbuf

router = APIRouter()
log = logging.getLogger("api.misc")

_TEXT_EXT = {".txt", ".md", ".markdown", ".json", ".csv", ".log", ".py", ".js", ".ts",
             ".html", ".css", ".yaml", ".yml", ".toml", ".ini", ".xml", ".sql", ".bat", ".ps1"}
_IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
_MAX_BYTES = 10 * 1024 * 1024
_MAX_TEXT = 12000


def _att_dir(request: Request) -> Path:
    d = Path(request.app.state.cfg.runtime.chats_dir) / "_attachments"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _count_output(root: Path) -> int:
    n = 0
    if root.exists():
        for p in root.rglob("*.png"):
            n += 1
    return n


def _recent_images(root: Path, limit: int = 8) -> list[dict]:
    items: list[dict] = []
    if not root.exists():
        return items
    files = sorted(root.rglob("*.png"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]
    for p in files:
        try:
            rel = p.relative_to(root).as_posix()
        except ValueError:
            rel = p.name
        items.append({"url": f"/output/{rel}", "name": p.name,
                      "mtime": int(p.stat().st_mtime)})
    return items


@router.get("/api/overview")
async def overview(request: Request) -> dict:
    app = request.app
    cfg = app.state.cfg
    st = app.state.sup.state
    hw = getattr(app.state, "hardware", None) or hardware.summary()
    ready = app.state.ready
    services = {}
    for key, name, port in (("chat", "对话模型", cfg.runtime.llama_port),
                            ("sd", "画图模型", cfg.runtime.sd_port)):
        want = bool(app.state.model_want.get(key, False))
        is_ready = bool(ready.get(key))
        proc = app.state.procs.procs.get(key)
        services[key] = {
            "name": name, "ready": is_ready, "want": want, "port": port,
            "pid": proc.pid if proc and proc.alive() else None,
            "loading": want and not is_ready,
        }
    chats = len(list(Path(cfg.runtime.chats_dir).glob("*.json"))) \
        if Path(cfg.runtime.chats_dir).exists() else 0
    images = _count_output(Path(cfg.runtime.output_dir))
    rt = cfg.runtime
    return {
        "app": {"name": "灵枢", "state": str(st), "port": rt.gateway_port,
                "dir": str(cfgmod.ROOT),
                "uptime": int(time.time() - getattr(app.state, "started_at", time.time()))},
        "hardware": hw,
        # hardware 本身是完整 plan（字段扁平，给"对话参数"弹窗用）；
        # 状态条要的"两个模型能不能同时装下"另外给一份，别让前端自己凑显存
        "summary": hardware.summary(),
        "services": services,
        "settings": {
            "n_ctx": cfg.settings.n_ctx, "max_tokens": cfg.settings.max_tokens,
            "steps": cfg.settings.steps, "vae_tile": cfg.settings.vae_tile,
            "thinking": cfg.settings.thinking,
        },
        "paths": {"output": str(rt.output_dir), "chats": str(rt.chats_dir), "logs": str(rt.logs_dir)},
        "models": {
            "chat": {"name": Path(rt.model_gemma).stem.replace("_", " "), "path": str(rt.model_gemma)},
            "draw": {"name": Path(rt.model_diffusion).stem.replace("_", " "), "path": str(rt.model_diffusion)},
        },
        "counts": {"chats": chats, "images": images},
        "recent": _recent_images(Path(rt.output_dir)),
        "vram": hardware.live_free(),
        "estimate": hardware.ctx_estimate(cfg.settings.n_ctx),
    }


@router.get("/api/logs")
async def logs(since: int = 0, limit: int = 200) -> dict:
    return logbuf.tail(since=since, limit=max(1, min(limit, 500)))


@router.get("/api/vram")
async def vram(request: Request, seconds: int = 300, points: int = 120) -> dict:
    """显存曲线：直接读看门狗写的 gateway.vram.csv，取最近一段并降采样。"""
    path = Path(request.app.state.cfg.runtime.logs_dir) / "gateway.vram.csv"
    if not path.exists():
        return {"samples": [], "total_mib": 0}
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-4000:]
    now = time.time()
    rows = []
    for line in lines:
        parts = line.split(",")
        if len(parts) < 2:
            continue
        try:
            ts, used = float(parts[0]), int(float(parts[1]))
        except ValueError:
            continue
        if now - ts > seconds:
            continue
        rows.append({"t": round(ts, 1), "used": used})
    step = max(1, len(rows) // max(1, points))
    sampled = rows[::step]
    total = int((hardware.summary().get("gpu") or {}).get("vram_mib") or 0)
    return {"samples": sampled, "total_mib": total, "last": rows[-1] if rows else None}


@router.post("/api/open-folder")
async def open_folder(request: Request, body: dict) -> JSONResponse:
    kind = str(body.get("kind") or "")
    cfg = request.app.state.cfg
    target = {"output": Path(cfg.runtime.output_dir),
              "chats": Path(cfg.runtime.chats_dir),
              "logs": Path(cfg.runtime.logs_dir)}.get(kind)
    if target is None:
        return JSONResponse({"error": "invalid_kind", "detail": "kind 只能是 output/chats/logs"},
                            status_code=400)
    target.mkdir(parents=True, exist_ok=True)
    try:
        os.startfile(str(target))          # 只在本机桌面打开资源管理器，不碰远端
    except Exception as e:
        return JSONResponse({"error": "open_failed", "detail": str(e)}, status_code=500)
    return JSONResponse({"ok": True, "path": str(target)})


_PS_PICK_FOLDER = r'''
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Add-Type -AssemblyName System.Windows.Forms
$owner = New-Object System.Windows.Forms.Form
$owner.TopMost = $true
$owner.ShowInTaskbar = $false
$owner.WindowState = [System.Windows.Forms.FormWindowState]::Minimized
$owner.Show()
$dlg = New-Object System.Windows.Forms.FolderBrowserDialog
$dlg.Description = '选择 Agent 工作文件夹'
$dlg.ShowNewFolderButton = $true
$result = $dlg.ShowDialog($owner)
$owner.Close()
if ($result -eq [System.Windows.Forms.DialogResult]::OK) { [Console]::Out.Write($dlg.SelectedPath) }
'''


@router.post("/api/system/pick-folder")
async def pick_folder() -> JSONResponse:
    """弹出本机文件夹选择器。浏览器安全沙箱拿不到绝对路径，所以由桌面侧进程选。"""
    def _pick() -> str:
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        p = subprocess.run(
            ["powershell", "-NoProfile", "-STA", "-Command", _PS_PICK_FOLDER],
            capture_output=True, text=True, encoding="utf-8", timeout=120,
            creationflags=flags,
        )
        if p.returncode != 0:
            raise RuntimeError((p.stderr or p.stdout or "picker failed").strip()[:300])
        return (p.stdout or "").strip().lstrip("\ufeff")

    try:
        path = await asyncio.to_thread(_pick)
    except Exception as e:
        return JSONResponse({"error": "picker_failed", "detail": str(e)}, status_code=500)
    if not path:
        return JSONResponse({"canceled": True})
    return JSONResponse({"ok": True, "path": path})


def _safe_name(name: str) -> str:
    base = Path(name or "file").name
    return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff._-]", "_", base)[:80] or "file"


@router.post("/api/attachments")
async def upload_attachment(request: Request, body: dict) -> JSONResponse:
    """前端读成 base64 再传：不引 python-multipart，少一个安装依赖、也少一处 413 行为差异。"""
    name = _safe_name(str(body.get("name") or ""))
    raw = body.get("data") or ""
    if not isinstance(raw, str) or not raw:
        return JSONResponse({"error": "empty_file"}, status_code=400)
    try:
        b64 = raw.split(",", 1)[1] if raw.startswith("data:") else raw
        data = base64.b64decode(b64)
    except Exception:
        return JSONResponse({"error": "invalid_base64"}, status_code=400)
    if len(data) > _MAX_BYTES:
        return JSONResponse({"error": "too_large", "detail": "单个附件上限 10 MB"}, status_code=413)

    ext = Path(name).suffix.lower()
    aid = f"a{uuid.uuid4().hex[:12]}"
    f = _att_dir(request) / f"{aid}{ext}"
    f.write_bytes(data)

    kind = "image" if ext in _IMAGE_EXT else ("text" if ext in _TEXT_EXT else "binary")
    text = None
    if kind == "text":
        try:
            text = data.decode("utf-8", "replace")[:_MAX_TEXT]
        except Exception:
            text = None
    return JSONResponse({"id": aid, "name": name, "size": len(data), "kind": kind,
                         "url": f"/api/attachments/{aid}", "text": text}, status_code=201)


@router.get("/api/attachments/{aid}")
async def get_attachment(request: Request, aid: str):
    if not re.fullmatch(r"a[0-9a-f]{12}", aid):
        return JSONResponse({"error": "invalid_id"}, status_code=400)
    d = _att_dir(request)
    for f in d.glob(f"{aid}.*"):
        return FileResponse(f)
    return JSONResponse({"error": "not_found"}, status_code=404)
