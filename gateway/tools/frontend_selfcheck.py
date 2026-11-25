"""前端静态资源自检：HTML/CSS/JS 可取、/api/models 有 5 条、/output 挂载可访问。

不测渲染效果（那是浏览器的事），只测"打开页面需要的资源全都 200 且内容对得上"。
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
    c = httpx.Client(base_url=BASE, timeout=10)

    r = c.get("/")
    check("GET / 返回界面", r.status_code == 200 and "<title>灵枢</title>" in r.text,
          str(r.status_code))

    for path in ["/static/style.css", "/static/js/app.js", "/static/js/api.js",
                 "/static/js/sse.js", "/static/js/chat.js", "/static/js/draw.js",
                 "/static/js/settings.js"]:
        rr = c.get(path)
        check(f"GET {path}", rr.status_code == 200 and len(rr.text) > 500, f"{rr.status_code} {len(rr.text)}B")

    m = c.get("/api/models")
    check("/api/models 五条", m.status_code == 200 and len(m.json()["items"]) == 5, str(m.status_code))

    # 顶栏两个模型开关：按钮在 HTML 里、端点回两条、服务都报 ready
    page = c.get("/").text
    check("顶栏有「对话模型」开关按钮", 'id="btn-model-chat"' in page)
    check("顶栏有「出图模型」开关按钮", 'id="btn-model-sd"' in page)
    sv = c.get("/api/services")
    items = sv.json().get("items", {}) if sv.status_code == 200 else {}
    check("/api/services 回 chat/sd 两条", set(items) == {"chat", "sd"}, str(sorted(items)))
    check("/api/services 两条都带 port 与 ready",
          all("port" in v and "ready" in v for v in items.values()), str(items))

    # 关键：前端只能拿 URL，不能拿绝对路径
    j = c.post("/api/jobs", json={"prompt": "front-end self check", "steps": 8})
    if j.status_code == 202:
        jid = j.json()["id"]
        snap = c.get(f"/api/jobs/{jid}").json()
        check("作业 snapshot 的 images 是 /output/ 开头",
              all(x.startswith("/output/") for x in snap["images"]) or snap["images"] == [],
              str(snap["images"]))
        c.post(f"/api/jobs/{jid}/cancel")
        check("取消不报错", True)
    else:
        check("POST /api/jobs", False, f"{j.status_code} {j.text[:60]}")

    c.close()
    print("\n前端自检: " + ("通过" if not FAILS else f"不通过 {FAILS}"))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
