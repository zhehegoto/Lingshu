"""关思考时的 512 预算够不够？用"要写长答案"的问题压一压。

判据是 finish_reason 和正文长度，不是"看着像写完了"：
  finish=length 且正文明显停在句子中间 → 512 不够，压预算这条规则要撤
  finish=stop   → 512 够
"""

import json
import sys

import httpx

GW = "http://127.0.0.1:8000"
LONG = "请分五个方面详细解释光合作用，每个方面写三到四句话，总共不少于 400 字。"


def run(prompt: str, label: str) -> dict:
    content = ""
    reasoning = 0
    done = {}
    with httpx.stream("POST", GW + "/api/chat", timeout=180,
                      json={"messages": [{"role": "user", "content": prompt}]}) as r:
        if r.status_code != 200:
            print(label, "HTTP", r.status_code, r.read()[:120])
            return {}
        for line in r.iter_lines():
            if not line.startswith("data:"):
                continue
            try:
                d = json.loads(line[5:].strip())
            except Exception:
                continue
            if d.get("type") == "reasoning":
                reasoning += len(d.get("text") or "")
            elif d.get("type") == "content":
                content += d.get("text") or ""
            elif d.get("type") == "done":
                done = d
    print(f"{label}: finish={done.get('finish_reason')} "
          f"completion={done.get('usage', {}).get('completion_tokens')} "
          f"max_tokens={done.get('max_tokens')} 正文 {len(content)} 字 reasoning {reasoning} 字")
    print(f"   尾巴: ...{content[-40:]!r}")
    return {"finish": done.get("finish_reason"), "len": len(content),
            "completion": (done.get("usage") or {}).get("completion_tokens"),
            "max_tokens": done.get("max_tokens")}


def set_thinking(v: bool, max_tokens: int | None = None) -> None:
    patch = {"thinking": v}
    if max_tokens is not None:
        patch["max_tokens"] = max_tokens
    r = httpx.put(GW + "/api/settings", json=patch, timeout=15)
    print("PUT", patch, "->", r.status_code, httpx.get(GW + "/api/settings", timeout=10).json()["values"])


def main() -> int:
    before = httpx.get(GW + "/api/settings", timeout=10).json()["values"]
    print("原始设置:", {k: before[k] for k in ("thinking", "max_tokens")})

    set_thinking(False)
    a = run(LONG, "关思考 · 当前预算")
    set_thinking(bool(before["thinking"]), before["max_tokens"])
    after = httpx.get(GW + "/api/settings", timeout=10).json()["values"]
    print("复原:", {k: after[k] for k in ("thinking", "max_tokens")}, "一致:", after["thinking"] == before["thinking"])
    if a.get("max_tokens") == 512 and a.get("finish") == "length":
        print("结论: 512 会截断（正文停在句中间）")
    elif a.get("finish") == "stop":
        print(f"结论: {a.get('max_tokens')} 够用（finish=stop，写完了）")
    else:
        print("结论: 仍被截断，预算还不够")
    return 0


if __name__ == "__main__":
    sys.exit(main())
