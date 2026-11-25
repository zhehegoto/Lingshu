"""作品列表 + 软删除的副作用断言。

规则同 PUT：删完必须挪回去，测试自己恢复原状，否则下一次跑就少了图。
"""

import sys
from pathlib import Path

import httpx

BASE = "http://127.0.0.1:8000"
ROOT = Path(__file__).resolve().parents[2] / "output"


def main() -> int:
    c = httpx.Client(base_url=BASE, timeout=30)
    ok = True

    live = c.get("/api/images", params={"limit": 500}).json()
    names = [i["name"] for i in live["items"]]
    src_leak = [n for n in names if n.endswith("_src.png")]
    print(f"作品数 {live['total']}，_src 混入 {len(src_leak)}")
    if src_leak:
        ok = False
        print("  ✗ _src.png 被当成作品列出来了")

    if not live["items"]:
        print("没有作品可测，退出")
        return 0

    target = live["items"][-1]                 # 取最老那张，改动影响最小
    src = ROOT / target["path"]
    print("待删:", target["path"], "存在:", src.exists())
    if not src.exists():
        return 1

    r = c.delete(f"/api/images/{target['path']}")
    print("DELETE →", r.status_code, r.json())
    if r.status_code != 200:
        return 1

    after = c.get("/api/images", params={"limit": 500, "include_deleted": "true"}).json()
    hit = [i for i in after["items"] if i["name"] == target["name"]]
    still_live = [i for i in c.get("/api/images", params={"limit": 500}).json()["items"]
                  if i["name"] == target["name"]]
    if not hit or not hit[0]["deleted"]:
        ok = False
        print("  ✗ 已删除的没出现在 include_deleted 列表里")
    else:
        print("  已删除项:", hit[0]["path"], "url:", hit[0]["url"])
        if "_trash" not in hit[0]["url"]:
            ok = False
            print("  ✗ 已删除项的 url 没带 _trash 前缀，缩略图会 404")
    if still_live:
        ok = False
        print("  ✗ 未删除列表里还能看到它")

    # 恢复原状：从 _trash 挪回原位
    trash = ROOT / "_trash" / target["path"]
    moved_back = []
    for p in (trash, trash.with_suffix(".json")):
        if p.exists():
            dst = ROOT / target["path"]
            if p.suffix == ".json":
                dst = dst.with_suffix(".json")
            dst.parent.mkdir(parents=True, exist_ok=True)
            p.replace(dst)
            moved_back.append(dst.name)
    back = c.get("/api/images", params={"limit": 500}).json()
    restored = [i for i in back["items"] if i["name"] == target["name"]]
    print("恢复:", moved_back, "回到列表:", bool(restored))
    if not restored or restored[0]["deleted"]:
        ok = False
        print("  ✗ 恢复失败")

    # 负向：路径穿越必须被拒
    bad = c.delete("/api/images/../config.yaml")
    print("DELETE ../config.yaml →", bad.status_code, bad.json())
    if bad.status_code not in (400, 404):
        ok = False
        print("  ✗ 路径穿越没被拒绝")
    if (ROOT.parent / "config.yaml").exists() and not (ROOT.parent / "config.yaml").exists():
        ok = False

    missing = c.delete("/api/images/20261108/__no_such__.png")
    print("DELETE 不存在 →", missing.status_code)
    if missing.status_code != 404:
        ok = False
        print("  ✗ 不存在的图没回 404")

    print("验收:", "通过" if ok else "不通过")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
