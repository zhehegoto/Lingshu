"""cfg_scale 实验：判断反向提示词"不支持"还是"支持但被 cfg=1.0 关掉"。

前提：gateway 已停（否则显存不够）。本脚本自己拉起一个 sd-server（端口 8082），跑两张：
  1. cfg 3.0 + negative="red apple, red"
  2. cfg 3.0 + negative=""
同 seed / prompt / steps。若两张差很大 → sd.cpp 支持 negative，只是 cfg=1.0 下 CFG 关闭；
差≈0 → 这个 build 的 negative 路径本身无效。
跑完自己关掉 sd-server，不留下进程。
"""

import io
import subprocess
import sys
import time
from pathlib import Path

import httpx
from PIL import Image, ImageChops, ImageStat

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from gateway.config import load_config                     # noqa: E402
from gateway.config import sd_args                        # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
EXE = ROOT / "runtime" / "sd" / "sd-server.exe"
PORT = 8082
SEED = 12345
PROMPT = "a red apple on a white table, product photo"
NEG = "red apple, red, apple"


def spawn() -> subprocess.Popen:
    cfg = load_config()
    args = sd_args(cfg, port=PORT)
    log = open(ROOT / "logs" / "neg_cfg.log", "w", encoding="utf-8")
    p = subprocess.Popen([str(EXE), *args], cwd=str(ROOT), stdout=log, stderr=subprocess.STDOUT,
                         creationflags=subprocess.CREATE_NO_WINDOW)
    for _ in range(60):
        try:
            if httpx.get(f"http://127.0.0.1:{PORT}/sdapi/v1/options", timeout=2).status_code == 200:
                return p
        except Exception:
            pass
        time.sleep(1)
    raise RuntimeError("sd-server 没起来")


def gen(cfg_scale: float, negative: str) -> Image.Image:
    payload = {"prompt": PROMPT, "negative_prompt": negative, "width": 1024, "height": 1024,
               "steps": 8, "cfg_scale": cfg_scale, "seed": SEED, "batch_count": 1}
    r = httpx.post(f"http://127.0.0.1:{PORT}/sdapi/v1/txt2img", json=payload, timeout=300)
    r.raise_for_status()
    imgs = r.json().get("images") or []
    b = imgs[0].split(",", 1)[1] if imgs[0].startswith("data:") else imgs[0]
    import base64
    return Image.open(io.BytesIO(base64.b64decode(b))).convert("RGB")


def main() -> int:
    p = spawn()
    try:
        print("cfg 3.0 + negative …")
        a = gen(3.0, NEG)
        a.save(ROOT / "output" / "_negcfg_neg.png")
        print("cfg 3.0 无 negative …")
        b = gen(3.0, "")
        b.save(ROOT / "output" / "_negcfg_none.png")
        d = ImageChops.difference(a, b)
        st = ImageStat.Stat(d)
        mean = sum(st.mean) / 3
        print(f"cfg3.0 有/无 negative 平均逐像素差 {mean:.2f}")
        print("结论:", "sd.cpp 支持 negative（cfg>1 时生效）" if mean > 5 else "cfg>1 时 negative 也无差别 → 该 build 不支持")
    finally:
        p.terminate()
        try:
            p.wait(timeout=10)
        except Exception:
            p.kill()
        print("sd-server(8082) 已关闭")
    return 0


if __name__ == "__main__":
    sys.exit(main())
