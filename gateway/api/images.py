"""作品列表、改名与删除。

删除 = **真删**（png + 同名 json 一起 unlink），没有回收站。
理由：`_trash` 需要三个动作才闭环（恢复 / 彻底删除 / 清空），而这些动作本身又是一批要维护、
要测、要写文案的路径；误删的风险改由前端「再点一次确认」挡住，比留一个只进不出的目录诚实。
单个删除用 `DELETE /api/images/{relpath}`（对资源本身），批量删除用
`POST /api/images/delete` + `{"relpaths": [...]}`（对多个资源做动作，按契约 7.1）。
"""

import json
import logging
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter()
log = logging.getLogger("api.images")

MAX_BATCH = 500


def _meta_for(png: Path) -> dict:
    """读同名 .json 的 created_at / prompt / display_name；读不到就用文件时间。"""
    meta = {}
    js = png.with_suffix(".json")
    if js.exists():
        try:
            meta = json.loads(js.read_text(encoding="utf-8"))
        except Exception:
            meta = {}
    created = meta.get("created_at") or meta.get("created") or ""
    if not created:
        created = png.stat().st_mtime
    return {
        "created_at": created,
        "prompt": meta.get("prompt", ""),
        "display_name": (meta.get("display_name") or "").strip(),
        # 「再来一张（同种子）」要复现同一张图，这些参数得能从作品本身读回来
        "negative": meta.get("negative_prompt", ""),
        "seed": meta.get("seed"),
        "steps": meta.get("steps"),
        "width": meta.get("width"),
        "height": meta.get("height"),
        "full_prompt": meta.get("prompt", ""),
    }


def _item(root: Path, png: Path) -> dict:
    rel = png.relative_to(root).as_posix()
    m = _meta_for(png)
    ts = m["created_at"]
    if isinstance(ts, (int, float)):
        import datetime
        ts = datetime.datetime.fromtimestamp(ts).isoformat(timespec="seconds")
    return {
        "name": png.name,
        "path": rel,
        "url": f"/output/{rel}",
        "created_at": ts,
        "prompt": (m.get("prompt") or "").strip()[:120],
        "display_name": m["display_name"],
        "title": m["display_name"] or png.name,   # 列表上显示的标题
        "seed": m["seed"],
        "steps": m["steps"],
        "width": m["width"],
        "height": m["height"],
        "negative": (m["negative"] or "").strip()[:200],
        "prompt_full": (m["full_prompt"] or "").strip()[:2000],
    }


def _is_output(png: Path, root: Path | None = None) -> bool:
    """_src.png 是 mask 模式的原图备份，算输入不是作品，不进列表。

    另外：output 下 `_` 前缀的子目录（历史遗留的 `_trash`、临时用的 `_hold`/`_tmp`）一律不算作品。
    实测教训：把回收站临时挪到 `_trash_hold` 做验证，列表立刻把它当"活着的作品"列出（42 张）。
    判据必须带 `len(parts) > 1`：根目录下的 `_probe_mask.png` 是**文件名**带下划线，不是目录前缀，
    第一版没加这个条件，丢了两张真实作品（40 → 38）。
    """
    if png.stem.endswith("_src"):
        return False
    if root is not None:
        parts = png.relative_to(root).parts
        if len(parts) > 1 and parts[0].startswith("_"):
            return False
    return True


@router.get("/api/images")
async def list_images(request: Request, limit: int = 80) -> JSONResponse:
    app = request.app
    root: Path = app.state.cfg.runtime.output_dir
    if not root.exists():
        return JSONResponse({"items": [], "total": 0})

    items = []
    for png in root.rglob("*.png"):
        if not _is_output(png, root):
            continue
        items.append(_item(root, png))

    items.sort(key=lambda x: str(x["created_at"]), reverse=True)
    return JSONResponse({"items": items[: max(1, min(limit, 500))], "total": len(items)})


def _safe_target(root: Path, relpath: str) -> Path | None:
    """把 relpath 解析成 root 下的绝对路径，拒绝越界与脚手架目录。"""
    if not isinstance(relpath, str):
        return None
    rel = relpath.strip()
    if not rel or rel.startswith("/") or ".." in Path(rel).parts:
        return None
    # `_` 前缀目录是脚手架（历史 `_trash`、临时目录），永远不是作品，也不允许从这里删文件。
    # 判据必须带 `len(parts) > 1`：根目录的 `_probe_mask.png` 是**文件名**带下划线，不是目录前缀，
    # 少了这个条件它就"列表里看得见、点删除必然 400"。同一个坑 _is_output 踩过一次，这里又踩一次。
    parts = Path(rel).parts
    if len(parts) > 1 and parts[0].startswith("_"):
        return None
    target = (root / rel).resolve()
    try:
        target.relative_to(root.resolve())
    except ValueError:
        return None
    return target


def _unlink_pair(root: Path, target: Path) -> list[str]:
    """删 png 和它的同名 json，返回真正删掉的文件（相对路径）。"""
    removed = []
    for p in (target, target.with_suffix(".json")):
        if p.is_file():
            p.unlink()
            removed.append(p.relative_to(root).as_posix())
    return removed


@router.patch("/api/images/{relpath:path}")
async def rename_image(request: Request, relpath: str, body: dict) -> JSONResponse:
    """重命名只改显示名（写进同名 .json 的 display_name），文件名和 relpath 不动。

    文件名是主键：改它要让历史列表、作业引用、四格缓存全部跟着变，风险远大于收益。
    """
    root: Path = request.app.state.cfg.runtime.output_dir
    target = _safe_target(root, relpath)
    if target is None:
        return JSONResponse({"error": "invalid_path"}, status_code=400)
    if not target.exists() or target.suffix.lower() != ".png":
        return JSONResponse({"error": "not_found"}, status_code=404)

    raw = body.get("display_name")
    if not isinstance(raw, str):
        return JSONResponse({"error": "invalid_name", "detail": "display_name 必须是字符串"}, status_code=400)
    name = raw.strip()
    if not name:
        return JSONResponse({"error": "invalid_name", "detail": "名字不能为空"}, status_code=400)
    if len(name) > 50:
        return JSONResponse({"error": "invalid_name", "detail": "名字最长 50 字"}, status_code=400)
    if any(ch in name for ch in '/\\:*?"<>|') or any(ord(ch) < 32 for ch in name):
        return JSONResponse({"error": "invalid_name",
                             "detail": "名字不能包含 / \\ : * ? \" < > | 或控制字符"}, status_code=400)

    js = target.with_suffix(".json")
    meta = {}
    if js.exists():
        try:
            meta = json.loads(js.read_text(encoding="utf-8"))
        except Exception:
            meta = {}
    if not meta.get("created_at"):
        import datetime
        meta["created_at"] = datetime.datetime.fromtimestamp(
            target.stat().st_mtime).isoformat(timespec="seconds")
    meta["display_name"] = name

    tmp = js.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(js)
    log.warning("重命名 %s → %s", relpath, name)
    return JSONResponse({"ok": True, "path": relpath, "display_name": name, "title": name})


@router.post("/api/images/delete")
async def delete_images(request: Request, body: dict) -> JSONResponse:
    """批量真删。body: {"relpaths": ["20261108/a.png", ...]}。

    按契约 7.1：对多张图做同一件事属于"动作"，走 `POST /api/images/{action}` + body 传标识，
    不做 `/api/images/{relpath}/delete` 这类路径参数挂动作段（贪婪匹配会吞掉末尾的动作名）。
    """
    root: Path = request.app.state.cfg.runtime.output_dir
    raw = body.get("relpaths")
    if not isinstance(raw, list) or not raw:
        return JSONResponse({"error": "invalid_path", "detail": "relpaths 必须是非空列表"},
                            status_code=400)
    if len(raw) > MAX_BATCH:
        return JSONResponse({"error": "too_many", "detail": f"一次最多删 {MAX_BATCH} 张"},
                            status_code=400)

    removed, missing, rejected = [], [], []
    for rel in raw:
        target = _safe_target(root, rel if isinstance(rel, str) else "")
        if target is None:
            rejected.append(rel)
            continue
        if not target.is_file() or target.suffix.lower() != ".png":
            missing.append(rel)
            continue
        removed += _unlink_pair(root, target)

    log.warning("批量删除 %d 张 → %d 个文件（缺 %d，拒 %d）",
                len(raw), len(removed), len(missing), len(rejected))
    return JSONResponse({"ok": True, "removed": removed,
                         "count": len(removed), "missing": missing, "rejected": rejected})


@router.delete("/api/images/{relpath:path}")
async def delete_image(request: Request, relpath: str) -> JSONResponse:
    """删单张：png + 同名 json，直接 unlink。没有回收站，删了就没了。"""
    app = request.app
    root: Path = app.state.cfg.runtime.output_dir
    target = _safe_target(root, relpath)
    if target is None:
        return JSONResponse({"error": "invalid_path"}, status_code=400)
    if not target.is_file() or target.suffix.lower() != ".png":
        return JSONResponse({"error": "not_found"}, status_code=404)

    removed = _unlink_pair(root, target)
    log.warning("删除 %s → %s", relpath, removed)
    return JSONResponse({"ok": True, "removed": removed})
