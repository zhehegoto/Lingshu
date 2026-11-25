"""部署路径 + 聊天记录的接口自检。

路径部分遵守"读→取反→写→读回→断言→写回原值"。
"""

import sys
import httpx

BASE = "http://127.0.0.1:8000"
FAILS = []


def check(name, cond, detail=""):
    print(("  ✓ " if cond else "  ✗ ") + name + (f"  {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


def main() -> int:
    c = httpx.Client(base_url=BASE, timeout=15)

    # ---- 部署路径 ----
    s = c.get("/api/settings").json()
    check("GET settings 有独立 paths 块", "paths" in s and set(s["paths"]["values"]) ==
          {"output.dir", "logs.dir", "chats.dir"}, str(list(s.get("paths", {}).keys())))
    orig = s["paths"]["values"]["logs.dir"]
    target = orig.replace("logs", "logs_probe") if orig.endswith("logs") else orig + "_probe"

    r = c.put("/api/settings/paths", json={"logs.dir": target})
    check("PUT 合法路径 → 200 restart_gateway", r.status_code == 200 and r.json()["restart_gateway"],
          str(r.json().get("changed")))
    back = c.get("/api/settings").json()["paths"]["values"]["logs.dir"]
    check("读回等于新值", back == target, back)
    c.put("/api/settings/paths", json={"logs.dir": orig})          # 写回原值
    check("已恢复原值", c.get("/api/settings").json()["paths"]["values"]["logs.dir"] == orig)

    r = c.put("/api/settings/paths", json={"logs.dir": r"C:\Windows\logs"})
    check("PUT 系统目录 → 400", r.status_code == 400, r.text[:70])
    r = c.put("/api/settings/paths", json={"logs.dir": "relative/logs"})
    check("PUT 相对路径 → 400", r.status_code == 400, r.text[:70])

    # ---- 聊天记录 ----
    doc = {"messages": [{"role": "user", "content": "自检：今天天气"},
                        {"role": "assistant", "content": "晴天。"}]}
    r = c.post("/api/chats", json=doc)
    check("POST /api/chats → 201", r.status_code == 201, str(r.json()))
    cid = r.json()["id"]
    g = c.get(f"/api/chats/{cid}")
    check("读回的 messages 条数一致", g.status_code == 200 and len(g.json()["messages"]) == 2,
          str(len(g.json()["messages"])) if g.status_code == 200 else g.text[:50])
    lst = c.get("/api/chats").json()["items"]
    check("列表里有刚存的那条", any(x["id"] == cid for x in lst), f"{len(lst)} 条")
    bad = c.get("/api/chats/..%2Fconfig")
    check("路径穿越 id → 400/404", bad.status_code in (400, 404), str(bad.status_code))

    c.close()
    print("\n路径/记录自检: " + ("通过" if not FAILS else f"不通过 {FAILS}"))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
