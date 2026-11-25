"""会话接口的副作用断言：列表 / 改名 / 软删 / 恢复 / 非法输入 / 备份。

跑完把两个文件的原始内容写回（不靠 API 恢复，避免留下 title_auto=false 这种语义残留）。
"""

import json
import shutil
import sys
from pathlib import Path

import httpx

BASE = "http://127.0.0.1:8000"
ROOT = Path(__file__).resolve().parents[2]
CHATS = ROOT / "chats"
BACKUP = ROOT / "chats_backup"


def snapshot() -> dict:
    return {f.name: f.read_bytes() for f in CHATS.glob("*.json")}


def restore(snap: dict) -> None:
    for name, raw in snap.items():
        (CHATS / name).write_bytes(raw)
    for f in CHATS.glob("*.json"):
        if f.name not in snap:
            f.unlink()


def main() -> int:
    c = httpx.Client(base_url=BASE, timeout=30)
    snap = snapshot()
    ok = True
    try:
        items = c.get("/api/chats?limit=200").json()["items"]
        print("列表:", [(i["id"], i["title"], i["messages"], i["title_auto"]) for i in items])
        if not items:
            print("✗ 列表为空"); ok = False
        for i in items:
            if not i["title"]:
                print("✗ 空标题:", i["id"]); ok = False
        if not all(i["title_auto"] for i in items):
            print("✗ 旧记录 title_auto 不为 True"); ok = False

        target = items[0]["id"]
        before = c.get(f"/api/chats/{target}").json()
        n_before = len(before.get("messages") or [])
        print(f"目标 {target}：{n_before} 条，标题「{before.get('title')}」")

        r = c.patch(f"/api/chats/{target}", json={"title": "改名测试会话"})
        print("PATCH →", r.status_code, r.json())
        if r.status_code != 200:
            ok = False
        after = c.get(f"/api/chats/{target}").json()
        print("读回:", after["title"], "| title_auto:", after.get("title_auto"),
              "| 消息数:", len(after.get("messages") or []))
        if after["title"] != "改名测试会话" or not after.get("title_auto") is False:
            ok = False
            print("✗ 标题没改或 title_auto 没置 False")
        if len(after.get("messages") or []) != n_before:
            ok = False
            print("✗ 改名把消息弄丢了")

        for val, why in [("", "空"), ("x" * 41, "41 字"), ("a/b", "含 /"), ("a:b", "含 :"),
                         ("a|b", "含 |"), ("bad\x01x", "控制字符")]:
            rr = c.patch(f"/api/chats/{target}", json={"title": val})
            print(f"  {why:8s} → {rr.status_code} {rr.json().get('error')}")
            if rr.status_code != 400:
                ok = False
                print("    ✗ 应拒绝")
        bad_id = c.patch("/api/chats/%2e%2e%2fconfig.yaml", json={"title": "x"})
        print("  非法 id →", bad_id.status_code, bad_id.json().get("error"))
        # 路由层可能把它当成别的路径直接 404；真正的要求是"没落盘、config 还在"
        if bad_id.status_code not in (400, 404):
            ok = False
        if not (ROOT / "config.yaml").exists():
            ok = False
            print("  ✗ config.yaml 不见了")

        print("备份目录存在:", BACKUP.exists())
        if not BACKUP.exists():
            ok = False
            print("✗ 改写前没有一次性备份")

        d = c.delete(f"/api/chats/{target}")
        print("DELETE →", d.status_code, d.json())
        gone = [i for i in c.get("/api/chats").json()["items"] if i["id"] == target]
        with_del = [i for i in c.get("/api/chats?include_deleted=true").json()["items"]
                    if i["id"] == target]
        trashed = (CHATS / "_trash" / f"{target}.json").exists()
        print("未删列表里还在吗:", bool(gone), "| 含删列表:", bool(with_del),
              "| _trash 里有文件:", trashed)
        if gone or not with_del or not trashed:
            ok = False
            print("✗ 软删除不符合预期")

        rr = c.post(f"/api/chats/{target}/restore")
        print("restore →", rr.status_code, rr.json())
        back = (CHATS / f"{target}.json").exists()
        still_trash = (CHATS / "_trash" / f"{target}.json").exists()
        print("回到原位:", back, "| _trash 里没了:", not still_trash)
        if not back or still_trash:
            ok = False

        dup = c.post(f"/api/chats/{target}/restore")
        print("重复恢复 →", dup.status_code, dup.json().get("error"))
        if dup.status_code != 404:          # 文件已在原位，_trash 里没有 → 404
            ok = False
    finally:
        restore(snap)
        print("已把原始文件写回")
        shutil.rmtree(BACKUP, ignore_errors=True) if "--keep-backup" not in sys.argv else None

    print("验收:", "通过" if ok else "不通过")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
