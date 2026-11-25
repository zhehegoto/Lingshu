"""sd-server HTTP 客户端（第 4 步只做 txt2img，非流式）。

三个约束（见第 4 步约定）：
1. urlopen 必须显式 timeout，且大于监督层杀进程的超时——HTTP 层先失败，监督层再兜底，
   两层都有出口。
2. 同步函数，由调用方用 asyncio.to_thread 包一层；阻塞线程只能靠 socket 超时返回。
3. 返回 base64，落盘规则：output/{YYYYMMDD}/{job_id}_{index}.png + 同名 .json。
"""

from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any


class SdError(RuntimeError):
    pass


class SdBackend:
    def __init__(self, port: int, output_dir: Path, timeout: int = 140) -> None:
        self.base = f"http://127.0.0.1:{port}"
        self.output_dir = Path(output_dir)
        self.timeout = timeout

    # ---------- HTTP ----------

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base}{path}",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def probe(self, timeout: float = 3.0) -> bool:
        try:
            with urllib.request.urlopen(f"{self.base}/sdapi/v1/options", timeout=timeout) as r:
                return r.status == 200
        except Exception:
            return False

    def progress(self) -> dict[str, Any] | None:
        try:
            with urllib.request.urlopen(f"{self.base}/sdapi/v1/progress", timeout=3) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception:
            return None

    @staticmethod
    def _slim_params(params: Any) -> dict[str, Any]:
        """sd-server 会把 init_images / mask 原样回显，几 MB 的 base64 不进元数据。"""
        if not isinstance(params, dict):
            return {}
        return {k: v for k, v in params.items() if len(str(v)) <= 200}

    # ---------- 落盘 ----------

    def _save_images(self, images: list[str], *, job_id: str, meta_base: dict, elapsed: float) -> list[str]:
        day = datetime.now().strftime("%Y%m%d")
        out_dir = self.output_dir / day
        out_dir.mkdir(parents=True, exist_ok=True)

        paths: list[str] = []
        for i, item in enumerate(images):
            b64 = item.split(",", 1)[1] if item.startswith("data:") else item
            raw = base64.b64decode(b64)
            png = out_dir / f"{job_id}_{i}.png"
            png.write_bytes(raw)
            paths.append(str(png))

            meta = {
                **meta_base,
                "job_id": job_id,
                "index": i,
                "elapsed_seconds": elapsed,
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "backend": "sd-server",
            }
            png.with_suffix(".json").write_text(
                json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        return paths

    # ---------- txt2img ----------

    def txt2img(
        self,
        *,
        job_id: str,
        prompt: str,
        width: int = 1024,
        height: int = 1024,
        steps: int = 8,
        seed: int = -1,
        batch_count: int = 1,
        negative_prompt: str = "",
    ) -> dict[str, Any]:
        payload = {
            "prompt": prompt,
            "negative_prompt": negative_prompt,
            "width": width,
            "height": height,
            "steps": steps,
            "cfg_scale": 1.0,
            "seed": seed,
            "batch_count": batch_count,
        }
        t0 = time.perf_counter()
        resp = self._post("/sdapi/v1/txt2img", payload)
        elapsed = round(time.perf_counter() - t0, 2)

        images = resp.get("images") or []
        if not images:
            raise SdError(f"sd-server 未返回图片: keys={list(resp)}")

        meta_base = {"prompt": prompt, "negative_prompt": negative_prompt, "width": width,
                     "height": height, "steps": steps, "seed": seed, "batch_count": batch_count,
                     "mode": "txt2img", "returned_params": self._slim_params(resp.get("parameters"))}
        paths = self._save_images(images, job_id=job_id, meta_base=meta_base, elapsed=elapsed)
        return {"paths": paths, "elapsed": elapsed, "count": len(paths),
                "raw_params": self._slim_params(resp.get("parameters"))}

    # ---------- img2img + mask（画布模式） ----------

    def img2img_mask(
        self,
        *,
        job_id: str,
        prompt: str,
        init_b64: str,
        mask_b64: str,
        width: int = 1024,
        height: int = 1024,
        steps: int = 8,
        seed: int = -1,
        negative_prompt: str = "",
        strength: float = 0.95,
    ) -> dict[str, Any]:
        """白=重绘、黑=保留（二期 mask 实测语义）。strength 固定 0.95，不给前端改。

        原图另存 `{job_id}_src.png`，便于对照"框内改了、框外没动"。
        """
        def strip(s: str) -> str:
            return s.split(",", 1)[1] if s.startswith("data:") else s

        src_raw = base64.b64decode(strip(init_b64))
        day = datetime.now().strftime("%Y%m%d")
        out_dir = self.output_dir / day
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / f"{job_id}_src.png").write_bytes(src_raw)

        payload = {
            "prompt": prompt,
            "negative_prompt": negative_prompt,
            "init_images": [strip(init_b64)],
            "mask": strip(mask_b64),
            "denoising_strength": strength,
            "width": width,
            "height": height,
            "steps": steps,
            "cfg_scale": 1.0,
            "seed": seed,
            "resize_mode": 0,
        }
        t0 = time.perf_counter()
        resp = self._post("/sdapi/v1/img2img", payload)
        elapsed = round(time.perf_counter() - t0, 2)

        images = resp.get("images") or []
        if not images:
            raise SdError(f"sd-server 未返回图片: keys={list(resp)}")

        meta_base = {"prompt": prompt, "negative_prompt": negative_prompt, "width": width,
                     "height": height, "steps": steps, "seed": seed, "batch_count": 1,
                     "mode": "img2img_mask", "strength": strength,
                     "src": str(out_dir / f"{job_id}_src.png"),
                     "returned_params": self._slim_params(resp.get("parameters"))}
        paths = self._save_images(images, job_id=job_id, meta_base=meta_base, elapsed=elapsed)
        return {"paths": paths, "elapsed": elapsed, "count": len(paths),
                "raw_params": self._slim_params(resp.get("parameters"))}
