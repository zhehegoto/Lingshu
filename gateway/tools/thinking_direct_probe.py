"""直连 llama-server（8099）试三种"关思考"写法，看哪种真能让 reasoning_content 消失。

只测对话，不动显存、不碰 sd。每次 max_tokens 300 控制时长。
"""

import json

import httpx

BASE = "http://127.0.0.1:8099/v1/chat/completions"
PROMPT = "用一句话说明什么是显存。"


def run(label: str, extra: dict) -> None:
    payload = {
        "messages": [{"role": "user", "content": PROMPT}],
        "max_tokens": 300,
        "stream": False,
        **extra,
    }
    r = httpx.post(BASE, json=payload, timeout=120)
    if r.status_code != 200:
        print(f"{label}: HTTP {r.status_code} {r.text[:120]}")
        return
    body = r.json()
    msg = (body.get("choices") or [{}])[0].get("message") or {}
    reason = msg.get("reasoning_content") or ""
    content = msg.get("content") or ""
    print(f"{label}: reasoning {len(reason)} 字 · content {len(content)} 字 · finish={body['choices'][0].get('finish_reason')}")
    print(f"    content 开头: {content[:50]!r}")


def main() -> int:
    run("① 基线（不传任何 kwargs）", {})
    run("② chat_template_kwargs.enable_thinking=False", {"chat_template_kwargs": {"enable_thinking": False}})
    run("③ chat_template_kwargs.thinking=False", {"chat_template_kwargs": {"thinking": False}})
    return 0


if __name__ == "__main__":
    main()
