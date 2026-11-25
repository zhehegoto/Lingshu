"""模型导入：把拖进来的 gguf / safetensors 落到 models 目录，并顺手查本机有没有同名副本。

为什么不用 multipart：几 GB 的权重走 Form 解析要先写临时文件再整体读一遍，
内存和多一次拷贝都不划算。这里前端直接用原始 body 流式上传，服务端边收边写
`.part`，收完才原子改名——中途断线不会在 models 里留下半截模型。

删原文件这件事在浏览器侧做不到：拖拽只给文件名，不给路径。所以上传完成后由
后端在桌面/下载/文档里反查同名同大小的文件（范围有意收窄，最多 3 秒），
列给用户勾选；删除走回收站，可还原，且只允许删「models 里确实有同名同大小副本」的文件。
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import time
from pathlib import Path
from urllib.parse import unquote

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter()
log = logging.getLogger("api.models")

_ALLOWED_EXT = {".gguf", ".safetensors"}
_CHUNK = 1 << 20
_DUP_SECONDS = 3.0          # 反查原文件的时间预算，超时就返回已找到的
_DUP_LIMIT = 3
_DUP_DEPTH = 4
_DUP_NAMES = ("Desktop", "Downloads", "Documents", "桌面", "下载", "文档")
_SKIP_DIRS = {"node_modules", ".git", "$RECYCLE.BIN", "System Volume Information",
              "AppData", ".cache", ".venv", "venv"}


def _models_dir(request: Request) -> Path:
    d = Path(request.app.state.cfg.runtime.model_diffusion).parent
    d.mkdir(parents=True, exist_ok=True)
    return d.resolve()


def _safe_name(raw: str) -> str | None:
    """只认纯文件名：带路径成分的一律拒绝，扩展名必须在白名单里。"""
    name = unquote(str(raw or "")).strip()
    if not name or name in (".", "..") or name.startswith("."):
        return None
    if any(ch in name for ch in ("/", "\\", ":", "\0")):
        return None
    if Path(name).suffix.lower() not in _ALLOWED_EXT:
        return None
    return name


def _dup_roots() -> list[Path]:
    home = Path(os.environ.get("USERPROFILE") or Path.home())
    bases = [home / n for n in _DUP_NAMES]
    for key in ("OneDrive", "OneDriveCommercial", "OneDriveConsumer"):
        root = os.environ.get(key)
        if root:
            bases += [Path(root) / n for n in _DUP_NAMES]
    out: list[Path] = []
    for b in bases:
        try:
            rp = b.resolve()
        except OSError:
            continue
        if rp.is_dir() and rp not in out:
            out.append(rp)
    return out


def _find_duplicates(name: str, size: int, models: Path) -> list[dict]:
    """在桌面/下载/文档里找同名同大小的副本。找不到就返回空，不扫全盘。"""
    deadline = time.monotonic() + _DUP_SECONDS
    lower = name.lower()
    models_s = str(models).lower()
    found: list[dict] = []
    for base in _dup_roots():
        for root, dirs, files in os.walk(base):
            if time.monotonic() > deadline or len(found) >= _DUP_LIMIT:
                return found
            root_s = str(Path(root)).lower()
            if root_s == models_s or root_s.startswith(models_s + os.sep):
                dirs[:] = []
                continue
            depth = len(Path(root).relative_to(base).parts)
            dirs[:] = [d for d in dirs if d not in _SKIP_DIRS and not d.startswith(".")]
            if depth >= _DUP_DEPTH:
                dirs[:] = []
            for fn in files:
                if fn.lower() != lower:
                    continue
                p = Path(root) / fn
                try:
                    if p.stat().st_size != size:
                        continue
                except OSError:
                    continue
                found.append({"path": str(p), "size_mib": round(size / 1048576)})
                if len(found) >= _DUP_LIMIT:
                    return found
    return found


@router.post("/api/models/upload")
async def upload_model(request: Request) -> JSONResponse:
    """原始 body = 文件内容；文件名走 `X-Filename`（前端做了 URL 编码）。"""
    cfg = request.app.state.cfg
    models = _models_dir(request)
    name = _safe_name(request.headers.get("x-filename", ""))
    if not name:
        return JSONResponse({"error": "bad_name", "detail": "只接受 .gguf / .safetensors 文件"}, 400)
    target = models / name
    overwrite = request.query_params.get("overwrite") in ("1", "true", "yes")
    if target.exists() and not overwrite:
        return JSONResponse({
            "error": "exists",
            "detail": f"models 里已经有同名文件：{name}",
            "size_mib": round(target.stat().st_size / 1048576),
        }, 409)

    expect = 0
    raw_len = request.headers.get("content-length")
    if raw_len and raw_len.isdigit():
        expect = int(raw_len)
    # 留 5% 余量：写盘过程中系统还要临时用一点空间
    free = shutil.disk_usage(models).free
    if expect and free < expect * 1.05 + (64 << 20):
        return JSONResponse({
            "error": "no_space",
            "detail": f"磁盘不够：需要约 {expect / 1073741824:.1f} GB，"
                      f"models 所在盘只剩 {free / 1073741824:.1f} GB",
        }, 507)

    part = target.with_name(name + ".part")
    got = 0
    started = time.time()
    try:
        with open(part, "wb") as f:
            async for chunk in request.stream():
                if not chunk:
                    continue
                got += len(chunk)
                if expect and got > expect:
                    raise ValueError("收到的数据比声明的长度多")
                f.write(chunk)
            f.flush()
            os.fsync(f.fileno())
        if got == 0:
            raise ValueError("没有收到文件内容")
        os.replace(part, target)
    except Exception as exc:                        # 断线 / 磁盘满 / 文件被占用
        part.unlink(missing_ok=True)
        log.warning("导入模型失败 %s：%s", name, exc)
        return JSONResponse({
            "error": "upload_failed",
            "detail": f"写入 models 失败：{exc}。如果同名模型正在使用，先关闭对应服务再传。",
        }, 500)

    log.info("导入模型 %s（%.1f MB，%.1f 秒）", name, got / 1048576, time.time() - started)
    try:
        dups = await asyncio.to_thread(_find_duplicates, name, got, models)
    except Exception:
        dups = []
    return {
        "ok": True,
        "name": name,
        "path": str(target),
        "size_mib": round(got / 1048576),
        "duplicates": dups,
    }


@router.post("/api/models/delete")
async def delete_model(request: Request) -> JSONResponse:
    """删掉 models 里的一个模型文件（回收站）。

    只允许删 models 目录里的文件；config 正在引用的不许删——那会让下次启动直接缺文件。
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "bad_request", "detail": "请求体不是 JSON"}, 400)
    raw = str(body.get("path") or body.get("name") or "").strip()
    if not raw:
        return JSONResponse({"error": "bad_request", "detail": "缺少 path 或 name"}, 400)
    models = _models_dir(request)
    try:
        target = (models / Path(raw).name).resolve()
    except OSError:
        return JSONResponse({"error": "bad_path", "detail": "路径无法解析"}, 400)
    if target.parent != models:
        return JSONResponse({"error": "refused", "detail": "只能删 models 目录里的文件"}, 409)
    if not target.is_file():
        return JSONResponse({"error": "not_found", "detail": "文件不存在"}, 404)
    cfg = request.app.state.cfg
    rt = cfg.runtime
    current = {str(Path(p).resolve()) for p in (
        rt.model_diffusion, rt.model_llm, rt.model_vae, rt.model_gemma, rt.model_mmproj)}
    if str(target) in current:
        return JSONResponse({
            "error": "in_use",
            "detail": f"{target.name} 正被当前配置使用，先换掉它再删",
        }, 409)

    try:
        await asyncio.to_thread(_recycle, target)
    except Exception as exc:
        log.warning("删除模型失败 %s：%s", target, exc)
        return JSONResponse({
            "error": "delete_failed",
            "detail": f"{exc}。如果这个模型正在运行，先关闭对应服务再删。",
        }, 500)
    log.info("模型已移入回收站：%s", target.name)
    return {"ok": True, "name": target.name}


def _recycle(path: Path) -> None:
    """删到回收站而不是彻底抹掉：用户的文件，留一条后悔路。"""
    ps = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / \
        "v1.0" / "powershell.exe"
    script = ("Add-Type -AssemblyName Microsoft.VisualBasic;"
              "[Microsoft.VisualBasic.FileIO.FileSystem]::DeleteFile("
              f"'{str(path).replace(chr(39), chr(39) * 2)}','OnlyErrorDialogs','SendToRecycleBin')")
    subprocess.run([str(ps), "-NoProfile", "-NonInteractive", "-Command", script],
                   check=True, timeout=120,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


@router.post("/api/models/delete_source")
async def delete_source(request: Request) -> JSONResponse:
    """删掉用户本机那份原文件。只允许删「models 里有同名且同大小副本」的文件。"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "bad_request", "detail": "请求体不是 JSON"}, 400)
    raw = str(body.get("path") or "").strip()
    if not raw:
        return JSONResponse({"error": "bad_request", "detail": "缺少 path"}, 400)
    models = _models_dir(request)
    try:
        target = Path(raw).expanduser().resolve()
    except OSError:
        return JSONResponse({"error": "bad_path", "detail": "路径无法解析"}, 400)
    if target == models or models in target.parents:
        return JSONResponse({"error": "refused", "detail": "这是 models 目录里的文件，不能删"}, 409)
    if not target.is_file():
        return JSONResponse({"error": "not_found", "detail": "文件不存在"}, 404)

    twin = models / target.name
    try:
        size = target.stat().st_size
        same = twin.is_file() and twin.stat().st_size == size
    except OSError:
        return JSONResponse({"error": "bad_path", "detail": "读不到文件信息"}, 400)
    if not same:
        return JSONResponse({
            "error": "not_a_copy",
            "detail": "models 里没有同名且大小一致的副本，拒绝删除这个文件",
        }, 409)

    try:
        await asyncio.to_thread(_recycle, target)
    except Exception as exc:
        log.warning("删除原文件失败 %s：%s", target, exc)
        return JSONResponse({"error": "delete_failed", "detail": str(exc)}, 500)
    log.info("原文件已移入回收站：%s（%.1f MB）", target, size / 1048576)
    return {"ok": True, "path": str(target), "freed_mib": round(size / 1048576)}
