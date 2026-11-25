"""软删除之后必须有真删除的出口：_trash 的 purge 两侧（图片 / 会话）各验一遍。

断言不是"返回 200"，而是：
  · 非 `_trash/` 路径 → 404 not_in_trash（不能借 purge 删掉正常作品）
  · 不存在 → 404 not_found
  · 合法 → 200 且**文件真的没了**，含已删除列表里也不再有
  · 会话侧同构
测试数据用 `[PURGE-TEST]` 标记，跑完不留垃圾。
"""

import json
import sys
import uuid
from pathlib import Path

import httpx

GW = "http://127.0.0.1:8000"
ROOT = Path(__file__).resolve().parents[2]
out = ROOT / "output"
chats = ROOT / "chats"
IMG_REL = "20261108/_purge_test_0.png"


def make_png(path: Path) -> None:
    from PIL import Image
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (64, 64), (30, 90, 200)).save(path)


def main() -> int:
    ok = True

    # ---- 图片侧 ----
    p = out / IMG_REL
    js = p.with_suffix(".json")
    make_png(p)
    js.write_text(json.dumps({"prompt": "[PURGE-TEST] blue square", "created_at": "2026-11-08T20:00:00"},
                             ensure_ascii=False), encoding="utf-8")
    d = httpx.delete(GW + f"/api/images/{IMG_REL}", timeout=10)
    print("软删除:", d.status_code, "→ 在 _trash:", (out / "_trash" / IMG_REL).exists())

    r = httpx.post(GW + "/api/images/purge", json={"relpath": IMG_REL}, timeout=10)
    print("purge 非 _trash:", r.status_code, r.json().get("error"))
    if r.status_code != 404 or r.json().get("error") != "not_in_trash":
        ok = False

    r = httpx.post(GW + "/api/images/purge", json={"relpath": "_trash/20261108/_nope_0.png"}, timeout=10)
    print("purge 不存在:", r.status_code, r.json().get("error"))
    if r.status_code != 404 or r.json().get("error") != "not_found":
        ok = False

    r = httpx.post(GW + "/api/images/purge", json={"relpath": "_trash/" + IMG_REL}, timeout=10)
    print("purge 合法:", r.status_code, r.json().get("removed"))
    trash_png = out / "_trash" / IMG_REL
    trash_js = trash_png.with_suffix(".json")
    if not (r.status_code == 200 and not trash_png.exists() and not trash_js.exists()):
        ok = False
        print("  ✗ 期望 200 且 png/json 都不在 _trash 里")
    items = httpx.get(GW + "/api/images?include_deleted=true&limit=200", timeout=10).json()["items"]
    if any("_purge_test_0" in it["path"] for it in items):
        ok = False
        print("  ✗ 含已删除列表里还能看到它")
    print("  含已删除列表里还有它吗:", any("_purge_test_0" in it["path"] for it in items))

    # ---- 会话侧 ----
    cid = "c_purge" + uuid.uuid4().hex[:8]
    r = httpx.post(GW + "/api/chats", json={"id": cid, "messages": [
        {"role": "user", "content": "[PURGE-TEST] 会话"},
        {"role": "assistant", "content": "好"}]}, timeout=10)
    print("建会话:", r.status_code, cid)

    # 还在正常位置（没进 _trash）时必须拒绝：purge 不能顺手删掉活着的会话
    r = httpx.post(GW + "/api/chats/purge", json={"id": cid}, timeout=10)
    print("purge 未进 _trash:", r.status_code, r.json().get("error"))
    if r.status_code != 404 or r.json().get("error") != "not_in_trash":
        ok = False
        print("  ✗ 期望 404 not_in_trash（不能删活着的会话）")

    d = httpx.delete(GW + f"/api/chats/{cid}", timeout=10)
    print("软删除会话:", d.status_code, "→ 在 _trash:", (chats / "_trash" / f"{cid}.json").exists())

    r = httpx.post(GW + "/api/chats/purge", json={"id": "../../config"}, timeout=10)
    print("purge 非法 id:", r.status_code, r.json().get("error"))
    if r.status_code != 400:
        ok = False

    r = httpx.post(GW + "/api/chats/purge", json={"id": cid}, timeout=10)
    print("purge 合法:", r.status_code, r.json().get("removed", r.text[:40]))
    if not (r.status_code == 200 and not (chats / "_trash" / f"{cid}.json").exists()):
        ok = False
        print("  ✗ 期望 200 且文件已从 _trash 移除")
    lst = httpx.get(GW + "/api/chats?include_deleted=true&limit=200", timeout=10).json()["items"]
    print("  含已删除会话列表里还有它吗:", any(i["id"] == cid for i in lst))
    if any(i["id"] == cid for i in lst):
        ok = False

    print("验收:", "通过" if ok else "不通过")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
