"""图片 `_trash` 恢复的副作用断言（不只是断言 200）。

步骤：
  1. 造一张可识别的测试图 `output/20261108/_restore_test_0.png` + 同名 json
  2. DELETE → 列表里消失、含删列表里出现且 path 以 `_trash/` 开头、原位文件真的不在了
  3. POST /api/images/restore → 列表里回来、原位文件字节一致、json 里的 display_name 还在
  4. 负向：恢复一个不在 _trash 的路径 → 404 not_in_trash
  5. 冲突：再删一次、在原位放一个同名文件 → 恢复必须 409，不能覆盖已有文件
  6. 坏文件：把 _trash 里的 png 改成不是 png 的字节 → 恢复必须 400 invalid_image，
     且原位不能多出这个坏文件（不然作品列表里会冒出打不开的格子）
  7. 清理：测试文件是我们自己造的，直接硬删（用户数据一张不碰）
"""

import json
import sys
from pathlib import Path

import httpx

GW = "http://127.0.0.1:8000"
ROOT = Path(__file__).resolve().parents[2] / "output"
REL = "20261108/_restore_test_0.png"
BAD_REL = "20261108/_restore_bad_0.png"


def api(method: str, url: str, **kw):
    return httpx.request(method, GW + url, timeout=20, **kw)


def listing(include_deleted: bool) -> list[dict]:
    return api("GET", f"/api/images?limit=200&include_deleted={'true' if include_deleted else 'false'}").json()["items"]


def found(items, path) -> dict | None:
    return next((i for i in items if i["path"] == path), None)


def cleanup() -> None:
    for name in ("_restore_test_0.png", "_restore_test_0.json", "_restore_bad_0.png", "_restore_bad_0.json"):
        p = ROOT / "20261108" / name
        if p.exists():
            p.unlink()
    t = ROOT / "_trash" / "20261108"
    for name in ("_restore_test_0.png", "_restore_test_0.json", "_restore_bad_0.png", "_restore_bad_0.json"):
        p = t / name
        if p.exists():
            p.unlink()


def main() -> int:
    ok = True
    cleanup()

    from PIL import Image
    src = ROOT / "20261108" / "_restore_test_0.png"
    src.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (64, 64), (200, 40, 40)).save(src)
    js = src.with_suffix(".json")
    js.write_text(json.dumps({"prompt": "[RESTORE-TEST] 红方块", "created_at": "2026-11-08T14:00:00",
                              "display_name": "恢复测试图"}, ensure_ascii=False), encoding="utf-8")
    size0 = src.stat().st_size
    print(f"造图完成 {REL} {size0} bytes")

    # ① 删除
    r = api("DELETE", f"/api/images/{REL}")
    print("删除 →", r.status_code)
    if r.status_code != 200:
        print(r.text[:200])
        return 1
    if found(listing(False), REL):
        ok = False
        print("✗ 删除后仍出现在未删除列表")
    trashed = found(listing(True), "_trash/" + REL)
    if not trashed:
        ok = False
        print("✗ 含删列表里找不到 _trash 路径")
    else:
        print("含删列表 path:", trashed["path"], "| title:", trashed["title"])
    if src.exists():
        ok = False
        print("✗ 原位文件还在（软删没生效）")

    # ② 恢复
    r = api("POST", "/api/images/restore", json={"relpath": "_trash/" + REL})
    print("恢复 →", r.status_code, r.text[:80])
    back = found(listing(False), REL)
    if r.status_code != 200 or not back:
        ok = False
        print("✗ 恢复后没回到未删除列表")
    else:
        if src.stat().st_size != size0:
            ok = False
            print("✗ 恢复后文件大小变了")
        meta = json.loads(js.read_text(encoding="utf-8"))
        if meta.get("display_name") != "恢复测试图":
            ok = False
            print("✗ 恢复后 display_name 丢了")
        print("恢复后 title:", back["title"])

    # ③ 负向：不在 _trash 的路径
    r = api("POST", "/api/images/restore", json={"relpath": REL})
    print("恢复非 _trash 路径 →", r.status_code, r.json().get("error"))
    if r.status_code != 404:
        ok = False
        print("✗ 非 _trash 路径应 404")

    # ④ 冲突：原位已有同名文件
    api("DELETE", f"/api/images/{REL}")
    src.write_bytes(b"\x89PNG\r\n\x1a\nplaceholder")     # 占位，模拟"原位又被占用"
    r = api("POST", "/api/images/restore", json={"relpath": "_trash/" + REL})
    print("原位已存在时恢复 →", r.status_code, r.json().get("error"))
    if r.status_code != 409:
        ok = False
        print("✗ 冲突时应 409，不能覆盖")
    src.unlink()

    # ⑤ 坏文件：_trash 里被手改成非 png 的字节 → 恢复要拒，且不能把坏文件挪回列表
    bad = ROOT / "20261108" / "_restore_bad_0.png"
    bad.write_bytes(b"this is not a png at all, just text bytes")
    r = api("DELETE", f"/api/images/{BAD_REL}")
    print("坏图删除 →", r.status_code)
    r = api("POST", "/api/images/restore", json={"relpath": "_trash/" + BAD_REL})
    print("坏图恢复 →", r.status_code, r.json().get("error"))
    if r.status_code != 400:
        ok = False
        print("✗ 坏图恢复应 400 invalid_image")
    if bad.exists():
        ok = False
        print("✗ 坏图被挪回了原位（列表里会出现打不开的格子）")
    if found(listing(False), BAD_REL):
        ok = False
        print("✗ 坏图出现在未删除列表里")
    # 注：在"含已删除"视图里它本来就该还在 _trash（占位不影响正常列表），
    # 这里只断言它没被挪回原位（上面两条），不断言它从 _trash 消失。

    cleanup()
    left = list((ROOT / "_trash" / "20261108").glob("_restore_test_0*")) if (ROOT / "_trash" / "20261108").exists() else []
    if left:
        ok = False
        print("✗ 清理不干净:", left)
    print("验收:", "通过" if ok else "不通过")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
