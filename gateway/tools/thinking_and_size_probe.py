"""第二轮：尺寸指令换几种说法 + thinking 开关（这次按 SSE 真解析，不看 JSON）。

用法: python thinking_and_size_probe.py
"""

import json
import sys
import time

import httpx

GW = "http://127.0.0.1:8000"

TOOLS = [{
    "type": "function",
    "function": {
        "name": "generate_image",
        "description": "用户要求画图/出图/生成图片时调用。",
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "英文画面描述"},
                "width": {"type": "integer", "description": "图宽，像素"},
                "height": {"type": "integer", "description": "图高，像素"},
                "steps": {"type": "integer", "description": "采样步数，8-20，默认 8"},
                "seed": {"type": "integer", "description": "随机种子，-1 随机"},
            },
            "required": ["prompt"],
        },
    },
}]

SIZE_CASES = [
    "画一张竖版海报，比例 3:4",
    "生成一张宽高比 16:9 的风景图",
    "画一个竖图，高度 1024、宽度 768",
]


def tool_args(text: str) -> dict:
    t0 = time.time()
    r = httpx.post(GW + "/api/chat", timeout=120,
                   json={"messages": [{"role": "user", "content": text}],
                         "tools": TOOLS, "tool_choice": "auto"})
    body = r.json() if "json" in r.headers.get("content-type", "") else {}
    msg = body.get("message") or {}
    calls = msg.get("tool_calls") or []
    args = {}
    if calls:
        try:
            args = json.loads(calls[0]["function"]["arguments"])
        except Exception as e:
            args = {"__parse_error": str(e)}
    return {"dt": round(time.time() - t0, 1), "finish": body.get("finish_reason"),
            "args": args, "text": (msg.get("content") or "")[:120].replace("\n", " ")}


def stream_reasoning(payload: dict) -> tuple[int, str, float]:
    """返回 (reasoning 字符数, 正文前 60 字, 用时)"""
    t0 = time.time()
    reason = 0
    text = ""
    with httpx.stream("POST", GW + "/api/chat", json=payload, timeout=180) as resp:
        print("  HTTP", resp.status_code)
        if resp.status_code != 200:
            return -1, resp.read().decode()[:80], time.time() - t0
        for line in resp.iter_lines():
            if not line.startswith("data:"):
                continue
            raw = line[5:].strip()
            if not raw:
                continue
            try:
                d = json.loads(raw)
            except Exception:
                continue
            if d.get("type") == "reasoning":
                reason += len(d.get("text") or "")
            elif d.get("type") == "delta":
                text += d.get("text") or ""
    return reason, text[:60], time.time() - t0


def main() -> int:
    print("=== A'. 尺寸指令的三种说法 ===")
    for text in SIZE_CASES:
        r = tool_args(text)
        print(f"[{text}] {r['dt']}s finish={r['finish']}")
        print("  args:", json.dumps(r["args"], ensure_ascii=False)[:200])
        if not r["args"]:
            print("  文本回复:", r["text"])

    print("\n=== B'. thinking 开关（SSE 解析）===")
    base = {"messages": [{"role": "user", "content": "用一句话解释什么是显存。"}],
            "max_tokens": 512}
    n1, t1, d1 = stream_reasoning(dict(base))
    print(f"默认(thinking 开): reasoning {n1} 字 · {d1:.1f}s · {t1!r}")
    n2, t2, d2 = stream_reasoning({**base, "thinking": False})
    print(f"thinking=false:    reasoning {n2} 字 · {d2:.1f}s · {t2!r}")
    print("结论:", "开关有效（reasoning 明显减少）" if n2 >= 0 and n2 < n1 * 0.5
          else "开关无效（reasoning 没变化）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
