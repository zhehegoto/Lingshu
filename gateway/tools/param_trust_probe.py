"""三条结构性问题一次测完（共 4 次调用，约 60s）：

A. 模型给的**尺寸/种子/描述**可不可信（不只步数）
B. thinking=false 时 reasoning_content 是不是真没了（设置里的开关是真是假）
C. 停止后撞"重载窗口"会返回什么（503 排队 or 连接错误）

只读，不产生作业、不写文件。
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
                "width": {"type": "integer", "description": "图宽像素"},
                "height": {"type": "integer", "description": "图高像素"},
                "steps": {"type": "integer", "description": "采样步数 8-20"},
                "seed": {"type": "integer", "description": "随机种子，-1 随机"},
            },
            "required": ["prompt"],
        },
    },
}]

CASES = [
    "画一个竖图，1024 高",
    "用种子 42 画一个苹果",
    "画一个红色苹果，背景是蓝色",
]


def call_args(text: str, tools=True) -> dict:
    payload = {"messages": [{"role": "user", "content": text}]}
    if tools:
        payload["tools"] = TOOLS
        payload["tool_choice"] = "auto"
    t0 = time.time()
    r = httpx.post(GW + "/api/chat", timeout=120, json=payload)
    dt = time.time() - t0
    body = r.json() if "json" in r.headers.get("content-type", "") else {}
    msg = body.get("message") or {}
    calls = msg.get("tool_calls") or []
    args = {}
    if calls:
        try:
            args = json.loads(calls[0]["function"]["arguments"])
        except Exception as e:
            args = {"__parse_error": str(e)}
    return {"dt": round(dt, 1), "status": r.status_code, "finish": body.get("finish_reason"),
            "args": args, "content": (msg.get("content") or "")[:80],
            "reasoning_len": len(msg.get("reasoning_content") or "")}


def main() -> int:
    print("=== A. 结构化参数可信度 ===")
    for text in CASES:
        r = call_args(text)
        print(f"[{text}] {r['dt']}s finish={r['finish']}")
        print("  args:", json.dumps(r["args"], ensure_ascii=False)[:220])

    print("\n=== B. thinking 开关 ===")
    r = call_args("说一句你好", tools=False)
    print(f"默认（thinking 开）: reasoning {r['reasoning_len']} 字 · content={r['content']!r}")
    payload = {"messages": [{"role": "user", "content": "说一句你好"}],
               "chat_template_kwargs": {"enable_thinking": False}}
    t0 = time.time()
    got_reason = 0
    text = ""
    with httpx.stream("POST", GW + "/api/chat", json=payload, timeout=120) as resp:
        for line in resp.iter_lines():
            if line.startswith("data:"):
                try:
                    d = json.loads(line[5:].strip())
                except Exception:
                    continue
                if d.get("type") == "reasoning":
                    got_reason += len(d.get("text") or "")
                elif d.get("type") == "delta":
                    text += d.get("text") or ""
    print(f"thinking=false: reasoning {got_reason} 字 · 用时 {time.time()-t0:.1f}s · content={text[:60]!r}")
    print("结论:", "关得掉" if got_reason < 200 else "关不掉（reasoning 仍在输出）")

    print("\n=== C. 停止后撞重载窗口 ===")
    st = httpx.get(GW + "/api/status", timeout=10).json()
    print("当前状态:", st["state"], "| can_accept_chat:", st.get("can_accept_chat"))
    r = httpx.post(GW + "/api/chat", timeout=30,
                   json={"messages": [{"role": "user", "content": "你好"}]})
    print("此刻发消息 →", r.status_code, r.text[:80].replace("\n", " "))

    print("\n=== D. 表单实际提交的尺寸 ===")
    print("（由前端固定 1024×1024，模型给的 width/height 不参与；见 draw.js submit）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
