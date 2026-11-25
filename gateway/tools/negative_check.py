"""反向提示词到底生效没有：走真实部署路径（gateway → sd-server，cfg_scale=1.0）。

同一 seed / prompt / steps，只改 negative：
  A: negative=""
  B: negative="red apple, red, apple"
逐像素比较。差 ≈ 0 说明 negative 在 cfg=1.0 下是惰性的（传了但没作用）；
差很大说明真生效。
"""

import io
import sys

import httpx
from PIL import Image, ImageChops, ImageStat

GW = "http://127.0.0.1:8000"
SEED = 12345
PROMPT = "a red apple on a white table, product photo"


def run(negative: str) -> str:
    r = httpx.post(GW + "/api/jobs",
                   json={"prompt": PROMPT, "negative": negative, "seed": SEED, "steps": 8}, timeout=20)
    r.raise_for_status()
    job_id = r.json()["id"]
    print(f"  作业 {job_id} negative={negative!r}")
    for _ in range(120):
        s = httpx.get(f"{GW}/api/jobs/{job_id}", timeout=10).json()
        if s["status"] in ("done", "failed", "cancelled"):
            if s["status"] != "done":
                print("  作业失败:", s.get("error"))
                return ""
            return s["images"][0]
        httpx.get(f"{GW}/api/status", timeout=10)
        import time
        time.sleep(2)
    print("  超时")
    return ""


def pixels(url: str) -> Image.Image:
    b = httpx.get(GW + url, timeout=30).content
    return Image.open(io.BytesIO(b)).convert("RGB")


def main() -> int:
    print("A 无 negative")
    a_url = run("")
    print("B 有 negative")
    b_url = run("red apple, red, apple")
    if not (a_url and b_url):
        print("验收: 不通过（作业没出图）")
        return 1

    ia, ib = pixels(a_url), pixels(b_url)
    diff = ImageChops.difference(ia, ib)
    stat = ImageStat.Stat(diff)
    mean = sum(stat.mean) / 3
    lo, hi = diff.getextrema()[0][0], max(x[1] for x in diff.getextrema())
    print(f"A={a_url}\nB={b_url}")
    print(f"逐像素平均差 {mean:.2f}，最大通道差 {hi}")

    if mean < 2:
        print("结论：negative 无实际效果（cfg_scale=1.0 下 CFG 关闭，反向项不参与计算）")
    else:
        print("结论：negative 有可见影响")
    return 0


if __name__ == "__main__":
    sys.exit(main())
