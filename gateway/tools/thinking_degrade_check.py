"""思考吃满预算时的降级：验的是不变量，不是某一次的具体形态。

实测发现"正文为空"不是必然：同一预算下这次正文 144 字、下次 0 字。
所以断言写成对用户有意义的那个不变量：
    finish=length 时 → degraded 必被标记，且**正文永远不为空**
    （为空时回填思考内容，非空时保留原样只标 truncated）

跑 3 次覆盖两种形态，跑完复原设置。
"""

import json
import sys

import httpx

GW = "http://127.0.0.1:8000"
LONG = "请分五个方面详细解释光合作用，每个方面写三到四句话，总共不少于 400 字。"


def run() -> dict:
    content, done, reason = "", {}, 0
    with httpx.stream("POST", GW + "/api/chat", timeout=180,
                      json={"messages": [{"role": "user", "content": LONG}]}) as r:
        if r.status_code != 200:
            print("  HTTP", r.status_code)
            return {}
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
                content += d.get("text") or ""
            elif d.get("type") == "done":
                done = d
    print(f"  finish={done.get('finish_reason')} max_tokens={done.get('max_tokens')} "
          f"degraded={done.get('degraded')} reasoning {reason} 字 正文 {len(content)} 字")
    return {"degraded": done.get("degraded"), "content": content,
            "finish": done.get("finish_reason"), "max_tokens": done.get("max_tokens")}


def main() -> int:
    before = httpx.get(GW + "/api/settings", timeout=10).json()["values"]
    print("原始:", {k: before[k] for k in ("thinking", "max_tokens")})
    httpx.put(GW + "/api/settings", json={"thinking": True, "max_tokens": 256}, timeout=15)
    print("临时:", httpx.get(GW + "/api/settings", timeout=10).json()["values"]["max_tokens"])

    ok = True
    seen = []
    for i in range(3):
        print(f"第 {i + 1} 次")
        a = run()
        seen.append(a.get("degraded"))
        if a.get("finish") == "length":
            if a.get("degraded") not in ("truncated", "thinking_truncated"):
                ok = False
                print("  ✗ finish=length 但没有标记降级")
            if not a.get("content"):
                ok = False
                print("  ✗ 正文为空（用户会看到只有思考块的空气泡）")
        elif a.get("finish") == "stop":
            print("  （这一次写完了，不算降级场景）")

    httpx.put(GW + "/api/settings",
              json={"thinking": before["thinking"], "max_tokens": before["max_tokens"]}, timeout=15)
    back = httpx.get(GW + "/api/settings", timeout=10).json()["values"]
    print("复原:", {k: back[k] for k in ("thinking", "max_tokens")})
    if back["max_tokens"] != before["max_tokens"] or back["thinking"] != before["thinking"]:
        ok = False
        print("  ✗ 设置没复原")

    print("出现过的降级标记:", seen)
    print("验收:", "通过" if ok else "不通过")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
