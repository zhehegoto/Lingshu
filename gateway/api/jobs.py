import asyncio
import json
import logging
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from gateway import draw_sizes
from gateway import hardware

router = APIRouter()
log = logging.getLogger("api.jobs")


class JobRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=2000)
    negative: str = Field(default="", max_length=1000)   # 反向提示词（是否真生效见 10.11 实测）
    width: int = 1024
    height: int = 1024
    steps: int | None = None
    seed: int = -1
    batch_count: int = 1
    kind: str = "txt2img"
    init_image: str | None = None      # mask 模式：画布快照，裸 base64 或 dataURL
    mask: str | None = None            # mask 模式：白=重绘、黑=保留


@router.get("/api/draw/capabilities")
async def capabilities() -> dict:
    """尺寸目录 + 按本机显存算出的推荐档。前端只按这里返回的组合显示按钮。"""
    vram = int((hardware.summary().get("gpu") or {}).get("vram_mib") or 0)
    return draw_sizes.caps(vram)


@router.post("/api/jobs")
async def create_job(request: Request, body: JobRequest) -> JSONResponse:
    app = request.app
    cfg = app.state.cfg
    hub = app.state.hub
    worker = app.state.worker

    steps = body.steps if body.steps is not None else cfg.settings.steps
    if steps not in range(8, 21):
        return JSONResponse({"error": "invalid_steps", "detail": "steps 只能是 8–20"}, status_code=400)
    if not draw_sizes.allowed_size(body.width, body.height):
        return JSONResponse({"error": "unsupported_resolution",
                             "detail": "只支持 1K/2K/4K 的 1:1、4:3、3:4、16:9、9:16；4K 需要足够的显存"},
                            status_code=400)
    if body.batch_count not in (1, 2, 3, 4):
        return JSONResponse({"error": "invalid_batch", "detail": "batch_count 只能是 1–4"}, status_code=400)

    # 出图模型被顶栏按钮关掉了：直接拒绝，别让作业跑进 PREPARING 再失败
    if not app.state.ready.get("sd"):
        return JSONResponse({"error": "sd_stopped",
                             "detail": "画图模型已关闭，去「设置」页点「启动画图模型」"}, status_code=409)

    if body.kind not in ("txt2img", "mask"):
        return JSONResponse({"error": "invalid_kind", "detail": "kind 只能是 txt2img / mask"}, status_code=400)
    if body.kind == "mask":
        if not body.init_image or not body.mask:
            return JSONResponse({"error": "invalid_mask",
                                 "detail": "mask 模式必须同时给 init_image 和 mask"}, status_code=400)
        if body.batch_count > 1:
            return JSONResponse({"error": "unverified_batch", "max_verified": 1,
                                 "detail": "mask 模式只支持单张"}, status_code=400)

    job = hub.create(prompt=body.prompt, negative=body.negative,
                     width=body.width, height=body.height,
                     steps=steps, seed=body.seed, batch_count=body.batch_count,
                     kind=body.kind, init_b64=body.init_image, mask_b64=body.mask)
    if not await worker.enqueue(job):
        job.status = "failed"
        job.error = "queue_full"
        return JSONResponse({"error": "queue_full", "detail": "队列已满（默认 4）"}, status_code=429)

    return JSONResponse({"id": job.id, "status": job.status,
                         "batch_count": body.batch_count}, status_code=202)


def _public(snap: dict[str, Any], output_dir) -> dict[str, Any]:
    """把绝对路径换成前端能取的 /output/... URL。"""
    from pathlib import Path as _P
    base = _P(output_dir)
    imgs = []
    for p in snap.get("images") or []:
        try:
            rel = _P(p).relative_to(base).as_posix()
        except ValueError:
            rel = _P(p).name
        imgs.append(f"/output/{rel}")
    snap["images"] = imgs
    return snap


@router.get("/api/jobs")
async def list_jobs(request: Request) -> dict:
    hub = request.app.state.hub
    out = request.app.state.cfg.runtime.output_dir
    items = [_public(j.snapshot(), out)
             for j in sorted(hub.jobs.values(), key=lambda j: j.created_at, reverse=True)]
    return {"items": items}


@router.get("/api/jobs/{job_id}")
async def get_job(request: Request, job_id: str) -> JSONResponse:
    job = request.app.state.hub.get(job_id)
    if job is None:
        return JSONResponse({"error": "job_not_found"}, status_code=404)
    return JSONResponse(_public(job.snapshot(), request.app.state.cfg.runtime.output_dir))


@router.get("/api/jobs/{job_id}/events")
async def job_events(request: Request, job_id: str) -> StreamingResponse:
    app = request.app
    hub = app.state.hub
    sd = app.state.sd
    out = app.state.cfg.runtime.output_dir

    async def gen():
        async for evt in hub.stream(job_id, sd_progress=sd.progress):
            yield f"data: {json.dumps(_public(evt, out), ensure_ascii=False)}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.post("/api/jobs/{job_id}/cancel")
async def cancel_job(request: Request, job_id: str) -> JSONResponse:
    """取消 = 杀 sd-server 并重启；SSE 会断，前端主动关 SSE 改轮询 GET /api/jobs/{id}。"""
    worker = request.app.state.worker
    result = await worker.cancel(job_id)
    if not result.get("ok"):
        code = 404 if result.get("reason") == "job_not_found" else 409
        return JSONResponse(result, status_code=code)
    return JSONResponse(result)
