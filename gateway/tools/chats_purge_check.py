"""会话侧 purge（软删除的出口）的副作用断言。

图片侧的软删除已经取消（作品现在一删就真删，见 tools/images_delete_check.py），
所以这里只验会话：`chats/_trash` 仍然存在，仍然需要"单个彻底删除 / 清空"两个出口。
"""

import json
import time
from pathlib import Path

import httpx

BASE = "http://127.0.0.1:8000"
CHATS = Path(__file__).resolve().parents[2] / "chats"
TRASH = CHATS / "_trash"


def alive_chat_ids() -> list[str]:
    return sorted(i["id"] for i in httpx.get(f"{BASE}/api/chats?limit=200", timeout=10).json()["items"])


def main() -> int:
    ok = True
    with httpx.Client(timeout=20) as c:
        before = alive_chat_ids()
        print("活着的会话（起始）:", before)

        # 造一个会话并软删
        r = c.post(f"{BASE}/api/chats", json={"title": "[CHAT-PURGE-TEST] 待彻底删除"})
        cid = r.json()["id"]
        c.delete(f"{BASE}/api/chats/{cid}")
        on_disk = (TRASH / f"{cid}.json").exists()
        print(f"建会话 {cid}，软删后 _trash 里在: {on_disk}")
        if not on_disk:
            print("  ✗ 软删除没把文件挪到 chats/_trash")
            ok = False

        # 单个 purge
        p = c.post(f"{BASE}/api/chats/purge", json={"id": cid})
        gone = not (TRASH / f"{cid}.json").exists()
        print(f"单个 purge: {p.status_code} · 文件已移除: {gone}")
        if p.status_code != 200 or not gone:
            ok = False

        # 负向：已不在 _trash 的会话再 purge → 404
        p2 = c.post(f"{BASE}/api/chats/purge", json={"id": cid})
        print(f"重复 purge: {p2.status_code} {p2.json().get('error')}")
        if p2.status_code != 404:
            ok = False

        # 负向：活着的会话不能 purge
        r3 = c.post(f"{BASE}/api/chats", json={"title": "[CHAT-PURGE-TEST] 活着"})
        cid3 = r3.json()["id"]
        p3 = c.post(f"{BASE}/api/chats/purge", json={"id": cid3})
        print(f"purge 活着的会话: {p3.status_code} {p3.json().get('error')}")
        if p3.status_code != 404:
            ok = False
        c.delete(f"{BASE}/api/chats/{cid3}")

        # 整批：此时 _trash 里只有上面那个 cid3
        files = sorted(p.name for p in TRASH.glob("*.json")) if TRASH.exists() else []
        print("清空前 _trash:", files)
        pa = c.post(f"{BASE}/api/chats/purge", json={"all": True})
        left = sorted(p.name for p in TRASH.glob("*.json")) if TRASH.exists() else []
        print(f"清空会话回收站: {pa.status_code} count={pa.json().get('count')} · 剩下 {left}")
        if pa.status_code != 200 or left:
            ok = False

        after = alive_chat_ids()
        print("活着的会话（结束）:", after)
        if after != before:
            print("  ✗ 活着的会话被顺手删了")
            ok = False

    print("验收:", "通过" if ok else "不通过")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
