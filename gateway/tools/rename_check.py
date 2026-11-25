"""重命名（只改 display_name）的副作用断言。

两段式：
  1) 默认运行 —— 改名、断言列表读回、跑非法输入与路径穿越，并把值留在新名字上
  2) --verify  —— gateway 重启之后跑，断言新名字还在，然后改回原名（测试自己恢复）

文件名（relpath）必须始终不变，这是本测试的第二条断言。
"""

import sys
from pathlib import Path

import httpx

BASE = "http://127.0.0.1:8000"
ROOT = Path(__file__).resolve().parents[2]
MARK = ROOT / "logs" / "rename_check.json"


def get_items(c):
    return c.get("/api/images", params={"limit": 500}).json()["items"]


def main() -> int:
    c = httpx.Client(base_url=BASE, timeout=30)
    verify_only = "--verify" in sys.argv
    ok = True

    if verify_only:
        if not MARK.exists():
            print("没有上一段的标记文件，先跑一次默认模式")
            return 1
        import json
        m = json.loads(MARK.read_text(encoding="utf-8"))
        items = get_items(c)
        hit = [i for i in items if i["path"] == m["path"]]
        print("重启后读回:", hit[0]["title"] if hit else None)
        if not hit or hit[0]["title"] != m["new"]:
            ok = False
            print("  ✗ 重启后名字丢了")
        # 恢复原名：original 为空时 API 会 400（空名是非法输入），所以直接改 json 删掉这个键
        root = ROOT / "output"
        js = (root / m["path"]).with_suffix(".json")
        import json as _json
        meta = _json.loads(js.read_text(encoding="utf-8")) if js.exists() else {}
        if m["old"]:
            rr = c.patch(f"/api/images/{m['path']}", json={"display_name": m["old"]})
            print("恢复原名 →", rr.status_code, rr.json().get("display_name"))
        else:
            meta.pop("display_name", None)
            js.write_text(_json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
            print("恢复原名 → 已从 json 删除 display_name")
        got = [i for i in get_items(c) if i["path"] == m["path"]]
        print("恢复后 title:", got[0]["title"] if got else None)
        MARK.unlink()
        print("验收:", "通过" if ok else "不通过")
        return 0 if ok else 1

    items = get_items(c)
    if not items:
        print("没有作品")
        return 1
    it = items[0]
    path = it["path"]
    old = it["display_name"]
    new = "测试改名 苹果"
    print("目标:", path, "原名:", old or "(空)")

    r = c.patch(f"/api/images/{path}", json={"display_name": new})
    print("PATCH →", r.status_code, r.json())
    if r.status_code != 200:
        return 1

    after = [i for i in get_items(c) if i["path"] == path]
    print("读回 title:", after[0]["title"], "display_name:", after[0]["display_name"], "name:", after[0]["name"])
    if after[0]["title"] != new:
        ok = False
        print("  ✗ title 没变")
    if after[0]["name"] != it["name"]:
        ok = False
        print("  ✗ 文件名被改了（不该改）")

    cases = [("", "空字符串"), ("x" * 60, "超过 50 字"), ("a/b", "含 /"), ("a:b", "含 :"),
             ("a*", "含 *"), ("a|b", "含 |"), ("a<b", "含 <"), ("bad\x01name", "控制字符")]
    for val, why in cases:
        rr = c.patch(f"/api/images/{path}", json={"display_name": val})
        print(f"  {why:10s} → {rr.status_code} {rr.json().get('error')}")
        if rr.status_code != 400:
            ok = False
            print("    ✗ 应拒绝却通过了")

    name_after_bad = [i for i in get_items(c) if i["path"] == path][0]["title"]
    print("非法输入之后 title 仍是:", name_after_bad)
    if name_after_bad != new:
        ok = False
        print("  ✗ 非法输入把已有名字改坏了")

    for p in ("%2e%2e%2fconfig.yaml", "20261108%2f..%2f..%2fconfig.yaml"):
        rr = c.patch(f"/api/images/{p}", json={"display_name": "hack"})
        print("  穿越", p, "→", rr.status_code, rr.json().get("error"))
        if rr.status_code != 400:
            ok = False
            print("    ✗ 路径穿越没被拒")

    if not (ROOT / "config.yaml").exists():
        ok = False
        print("  ✗ config.yaml 不见了")

    import json
    MARK.write_text(json.dumps({"path": path, "old": old, "new": new}, ensure_ascii=False),
                    encoding="utf-8")
    print("（名字先留成新值，重启后再跑 --verify 断言持久化并改回）")
    print("验收（第一段）:", "通过" if ok else "不通过")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
