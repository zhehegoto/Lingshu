"""/api/chat 的思考开关是否真能关（标准 PUT 流程 + SSE 计量 + 复原）。

判据不是"看起来没思考"，是 reasoning 字符数：
  基线（thinking=true）  应 > 0
  thinking=false         应 ≈ 0 且正文非空
跑完必须把设置写回 true。
"""

import json
import sys
import time

import httpx

GW = "http://127.0.0.1:8000"
PROMPT = "用一句话解释什么是显存。"


def measure() -> tuple[int, int, float]:
    t0 = time.time()
    reason = 0
    text = ""
    with httpx.stream("POST", GW + "/api/chat", timeout=180,
                      json={"messages": [{"role": "user", "content": PROMPT}]}) as r:
        if r.status_code != 200:
            print("  HTTP", r.status_code, r.read()[:100])
            return -1, -1, 0.0
        for line in r.iter_lines():
            if not line.startswith("data:"):
                continue
            try:
                d = json.loads(line[5:].strip())
            except Exception:
                continue
            if d.get("type") == "reasoning":
                reason += len(d.get("text") or "")
            elif d.get("type") == "content":
                text += d.get("text") or ""
    return reason, len(text), time.time() - t0


def set_thinking(v: bool) -> bool:
    r = httpx.put(GW + "/api/settings", json={"thinking": v}, timeout=15)
    print(f"PUT thinking={v} →", r.status_code)
    back = httpx.get(GW + "/api/settings", timeout=10).json()
    got = back.get("values", back).get("thinking")
    print("  读回:", got)
    return r.status_code == 200 and got == v


def main() -> int:
    ok = True
    before = httpx.get(GW + "/api/settings", timeout=10).json()
    orig = (before.get("values", before)).get("thinking")
    print("原始 thinking =", orig)

    # ① 关掉
    if not set_thinking(False):
        ok = False
        print("✗ 写入/读回失败")
    r_off, c_off, d_off = measure()
    print(f"thinking=false: reasoning {r_off} 字 · 正文 {c_off} 字 · {d_off:.1f}s")
    if r_off >= 200 or c_off == 0:
        ok = False
        print("✗ 关不掉（reasoning 仍很多或正文为空）")

    # ② 打开
    if not set_thinking(bool(orig)):
        ok = False
        print("✗ 复原写入失败")
    r_on, c_on, d_on = measure()
    print(f"thinking=true : reasoning {r_on} 字 · 正文 {c_on} 字 · {d_on:.1f}s")
    if r_on <= 0:
        ok = False
        print("✗ 打开后 reasoning 仍为 0，说明 kwargs 被常开")

    back = httpx.get(GW + "/api/settings", timeout=10).json()
    assert (back.get("values", back)).get("thinking") == orig
    print("设置已复原:", orig)
    print("验收:", "通过" if ok else "不通过")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
