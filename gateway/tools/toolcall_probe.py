"""先验证"今天的模型会不会调工具"，再决定要不要写"一句话出图"这条路径。

不发请求到 sd，只问对话模型：给一个 generate_image 的 tools schema，
用户说"画一个红苹果"，看它返回 tool_calls 还是纯文本。
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
        "description": "根据一句画面描述生成一张 1024x1024 的图片。用户要求画图/出图/生成图片时调用。",
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "英文画面描述，尽量具体"},
                "steps": {"type": "integer", "description": "采样步数，默认 8"},
                "seed": {"type": "integer", "description": "随机种子，-1 表示随机"},
            },
            "required": ["prompt"],
        },
    },
}]


def main() -> int:
    t0 = time.time()
    r = httpx.post(GW + "/api/chat", timeout=180, json={
        "messages": [{"role": "user", "content": "画一个红苹果放在木桌上"}],
        "tools": TOOLS,
        "tool_choice": "auto",
    })
    print("HTTP", r.status_code, f"{time.time()-t0:.1f}s")
    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    print("finish_reason:", body.get("finish_reason"), "| prompt_tokens:", body.get("prompt_tokens"),
          "| max_tokens:", body.get("max_tokens"))
    msg = body.get("message") or {}
    print("keys:", list(msg.keys()))
    print("tool_calls:", json.dumps(msg.get("tool_calls"), ensure_ascii=False)[:400])
    print("content:", (msg.get("content") or "")[:300].replace("\n", " "))
    print("reasoning:", (msg.get("reasoning_content") or "")[:120].replace("\n", " "))
    ok = bool(msg.get("tool_calls"))
    print("结论:", "模型会调用工具，可以做一句话出图" if ok else "模型没调工具（只会输出文本）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
