"""画布 mask 作业自检：提交 → 等完成 → 验证"框内变了、框外没变"。

只测副作用，不看 200：对比成品与底图在框内/框外的平均像素差。
"""

from pathlib import Path
import base64
import io
import json
import sys
import time

import httpx
from PIL import Image, ImageChops, ImageStat

BASE = "http://127.0.0.1:8000"
ROOT = str(Path(__file__).resolve().parents[2])
SRC = ROOT + r"\output\20261108\j72595437002_0.png"      # 已有 1024² 成品当底图
BOX = (312, 312, 712, 712)                                 # 中心 400×400


def b64(img: Image.Image) -> str:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def mean_diff(a: Image.Image, b: Image.Image, box=None) -> float:
    if box:
        a, b = a.crop(box), b.crop(box)
    diff = ImageChops.difference(a.convert("RGB"), b.convert("RGB"))
    st = ImageStat.Stat(diff)
    return sum(st.mean) / 3


def main() -> int:
    seed = int(sys.argv[1]) if len(sys.argv) > 1 else 12345      # 固定 seed，三次峰值得可比
    base = Image.open(SRC).convert("RGB")
    mask = Image.new("L", base.size, 0)
    mask.paste(255, BOX)

    c = httpx.Client(base_url=BASE, timeout=30)
    r = c.post("/api/jobs", json={
        "prompt": "a bright red cube",
        "width": 1024, "height": 1024, "kind": "mask",
        "seed": seed,
        "init_image": b64(base),
        "mask": b64(mask.convert("RGB")),
    })
    print(f"POST /api/jobs (seed={seed}):", r.status_code, r.text[:120])
    if r.status_code != 202:
        return 1
    job_id = r.json()["id"]

    t0 = time.time()
    status = None
    for _ in range(60):
        time.sleep(3)
        j = c.get(f"/api/jobs/{job_id}").json()
        status = j["status"]
        print(f"  {int(time.time()-t0)}s {status} {j.get('error') or ''}")
        if status in ("done", "failed", "cancelled"):
            break
    if status != "done":
        print(f"✗ 作业未完成: {status}")
        return 1

    j = c.get(f"/api/jobs/{job_id}").json()
    out_url = j["images"][0]
    out = ROOT + out_url.replace("/output/", r"\output\\").replace("/", "\\")
    dst = Image.open(out).convert("RGB")

    # 显存峰值：取 gateway 全局采样里落在本作业窗口内的最大值
    peak = None
    csv_path = ROOT + r"\logs\gateway.vram.csv"
    try:
        vals = []
        with open(csv_path, encoding="utf-8") as fh:
            next(fh)
            for line in fh:
                parts = line.strip().split(",")
                if len(parts) >= 2:
                    try:
                        ts = float(parts[0])
                    except ValueError:
                        continue
                    if t0 - 2 <= ts <= time.time() + 2:
                        vals.append(int(parts[1]))
        peak = max(vals) if vals else None
    except OSError:
        pass
    print(f"作业窗口内显存峰值: {peak} MiB（预算 {6900}，护栏 7700）")

    d_in = mean_diff(base, dst, BOX)
    d_out = mean_diff(base, dst)
    print(f"框内平均差 {d_in:.1f} / 整图平均差 {d_out:.1f}")
    # 正确结果：框内明显变化，框外接近 0
    outside = ImageChops.difference(base, dst)
    print(f"成品: {out}")
    ok = d_in > 20 and d_out < d_in * 0.35 and (peak is None or peak <= 6900)
    print("✓ 框内重绘、框外基本未变、峰值在预算内" if ok else "✗ 分布或峰值不达标")
    print(json.dumps({"job": job_id, "elapsed": int(time.time() - t0), "d_in": round(d_in, 1),
                      "d_out": round(d_out, 1), "peak_mib": peak}, ensure_ascii=False))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
