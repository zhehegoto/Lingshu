"""清空回收站（{"all": true}）的副作用断言。

要点：
1. 不能拿用户已有的 `_trash` 做实验——`output/_trash` 里还躺着用户明确"不恢复但也没说删"的文件。
   所以先把已有内容整体挪到 `output/_trash_hold`，跑完（无论成败）再挪回去，并逐名核对。
2. 断言的是"活着的作品/会话一个没少"，不是"返回 200"。
3. 两侧各测：图片（png+json 一起删）、会话。
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import httpx
from PIL import Image

GW = "http://127.0.0.1:8000"
ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "output"
CHATS = ROOT / "chats"


def make_png(p: Path, color) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (64, 64), color).save(p)
    p.with_suffix(".json").write_text(
        json.dumps({"prompt": "[PURGE-ALL-TEST] square", "created_at": "2026-11-08T21:00:00"}),
        encoding="utf-8",
    )


def live_images() -> list[str]:
    return [i["path"] for i in httpx.get(GW + "/api/images?limit=500", timeout=20).json()["items"]]


def main() -> int:
    ok = True
    trash = OUT / "_trash"
    hold = OUT / "_trash_hold"

    if hold.exists():
        print("✗ 上一次的暂存目录还在，先人工确认再跑", hold)
        return 1

    # ① 把用户已有的回收站内容暂存（可逆）
    had = [p.relative_to(trash) for p in trash.rglob("*") if p.is_file()] if trash.exists() else []
    if had:
        shutil.move(str(trash), str(hold))
    print(f"原有回收站文件 {len(had)} 个，已暂存到 {hold.name}")

    created_imgs: list[str] = []
    created_chats: list[str] = []
    try:
        # ② 造 3 个作品再软删（2 个正常 png + 1 个坏 png，坏图本来就没法恢复）
        for i in range(2):
            rel = f"20261108/_purge_all_{i}.png"
            make_png(OUT / rel, (10 + i * 60, 120, 200))
            created_imgs.append(rel)
            r = httpx.delete(f"{GW}/api/images/{rel}", timeout=10)
            print(f"软删 {rel}:", r.status_code)
            if r.status_code != 200:
                ok = False
        bad = OUT / "20261108" / "_purge_all_bad.png"
        bad.parent.mkdir(parents=True, exist_ok=True)
        bad.write_bytes(b"not a png")
        created_imgs.append("20261108/_purge_all_bad.png")
        r = httpx.delete(f"{GW}/api/images/20261108/_purge_all_bad.png", timeout=10)
        print("软删坏图:", r.status_code)

        # ③ 造 2 个会话再软删
        for i in range(2):
            cid = f"c_purgeall{i}"
            r = httpx.post(GW + "/api/chats", json={"id": cid, "messages": [
                {"role": "user", "content": "[PURGE-ALL-TEST] 待清空的会话"}]}, timeout=10)
            print(f"建会话 {cid}:", r.status_code)
            created_chats.append(cid)
            r = httpx.delete(f"{GW}/api/chats/{cid}", timeout=10)
            print(f"软删会话 {cid}:", r.status_code)
            if r.status_code != 200:
                ok = False

        before_live = live_images()
        before_chat_ids = {i["id"] for i in
                           httpx.get(GW + "/api/chats?include_deleted=true&limit=200", timeout=20).json()["items"]
                           if not i["deleted"]}

        files_in_trash = sorted(p.relative_to(OUT).as_posix() for p in trash.rglob("*") if p.is_file())
        print("清空前回收站:", files_in_trash)

        # ④ 图片侧清空
        r = httpx.post(GW + "/api/images/purge", json={"all": True}, timeout=30)
        body = r.json() if r.status_code == 200 else {}
        print("图片清空:", r.status_code, "count =", body.get("count"))
        if r.status_code != 200 or body.get("count") != len(files_in_trash):
            ok = False
            print("  ✗ 期望 200 且 count 等于清空前文件数", len(files_in_trash))
        left = sorted(p.relative_to(OUT).as_posix() for p in trash.rglob("*") if p.is_file())
        print("清空后回收站:", left)
        if left:
            ok = False
            print("  ✗ 回收站里还有文件")

        # ⑤ 会话侧清空
        r = httpx.post(GW + "/api/chats/purge", json={"all": True}, timeout=30)
        body = r.json() if r.status_code == 200 else {}
        print("会话清空:", r.status_code, "count =", body.get("count"))
        if r.status_code != 200 or body.get("count") != len(created_chats):
            ok = False
            print("  ✗ 期望 200 且 count =", len(created_chats))
        left = [p.name for p in (CHATS / "_trash").glob("*.json")] if (CHATS / "_trash").exists() else []
        print("会话回收站剩余:", left)
        if left:
            ok = False

        # ⑥ 关键副作用断言：活着的作品与没被删的会话一个没少
        after_live = live_images()
        print("活着的作品数:", len(before_live), "→", len(after_live))
        if before_live != after_live:
            ok = False
            print("  ✗ 清空回收站动了活着的作品")
        after_chat_ids = {i["id"] for i in
                          httpx.get(GW + "/api/chats?include_deleted=true&limit=200", timeout=20).json()["items"]
                          if not i["deleted"]}
        print("活着的会话:", sorted(before_chat_ids), "→", sorted(after_chat_ids))
        if before_chat_ids != after_chat_ids:
            ok = False
            print("  ✗ 清空回收站动了活着的会话")

    finally:
        # ⑦ 把用户原有回收站内容原样搬回（这一步本身也要核对）
        for rel in created_imgs:
            p = OUT / rel
            if p.exists():
                p.unlink()
        if hold.exists():
            if trash.exists():
                shutil.rmtree(trash)
            shutil.move(str(hold), str(trash))
            back = sorted(p.relative_to(trash).as_posix() for p in trash.rglob("*") if p.is_file())
            print("暂存内容已搬回:", back)
            if sorted(str(x).replace("\\", "/") for x in had) != back:
                ok = False
                print("  ✗ 原有回收站内容没有原样回来")

    print("验收:", "通过" if ok else "不通过")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
