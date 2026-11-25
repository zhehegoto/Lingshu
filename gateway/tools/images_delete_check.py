"""作品删除的副作用断言：DELETE 之后文件真的从磁盘上没了（不是挪走、不是进 _trash）。

为什么要逐字节核对文件系统：接口 200 只能证明"处理过"，软删除时代它返回的是 moved，
真删除时代如果只断言 200，把 unlink 写成 replace 到别处也会通过。

测试数据用 `[DELETE-TEST]` 命名，跑完在 finally 里清掉，不污染作品列表。
"""

import json
import shutil
import sys
import time
from pathlib import Path

import httpx

BASE = "http://127.0.0.1:8000"
ROOT = Path(__file__).resolve().parents[2] / "output"
def _pick_src() -> Path:
    """造测试图要一张能打开的 png：先用探针图，没有就退到 mask 原图备份（`_src` 不算作品）。"""
    probe = ROOT / "_probe_mask.png"
    if probe.exists():
        return probe
    return next(ROOT.rglob("*_src.png"))


SRC = _pick_src()
DAY = "20261108"

created = []


def make(n: int) -> Path:
    """造一张可识别的测试图（内容借用现成的 mask 探针图，只为能打开）。"""
    dst = ROOT / DAY / f"zz_del_test_{n}.png"
    shutil.copyfile(SRC, dst)
    meta = dst.with_suffix(".json")
    meta.write_text(json.dumps(
        {"created_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "prompt": "[DELETE-TEST] 删除测试"},
        ensure_ascii=False), encoding="utf-8")
    created.extend([dst, meta])
    return dst


def rel(p: Path) -> str:
    return p.relative_to(ROOT).as_posix()


def total(c: httpx.Client) -> int:
    return c.get(f"{BASE}/api/images", params={"limit": 500}).json()["total"]


def main() -> int:
    if not SRC.exists():
        print(f"✗ 缺少源图 {SRC}")
        return 1

    ok = True
    with httpx.Client(timeout=30) as c:
        before = total(c)

        # ---- 单张：DELETE 之后磁盘上必须没有文件 ----
        # 注意计数口径：before 是**造测试图之前**的数量，所以"删完"应该回到 before（而不是 before-1）
        p0 = make(0)
        r = c.delete(f"{BASE}/api/images/{rel(p0)}")
        gone_png = not p0.exists()
        gone_json = not p0.with_suffix(".json").exists()
        after = total(c)
        print(f"单张删除: {r.status_code} · png 还在? {not gone_png} · json 还在? {not gone_json} · total {before} → {after}")
        if r.status_code != 200 or not gone_png or not gone_json or after != before:
            print("  ✗ 删除没有真删掉文件，或列表数量没跟着变")
            ok = False
        removed = r.json().get("removed", [])
        if rel(p0) not in removed or rel(p0.with_suffix('.json')) not in removed:
            print(f"  ✗ 返回体 removed 里应该同时有 png 和 json，实际 {removed}")
            ok = False

        # 再删一次：文件已经不在了，必须是 404（不能静默 200）
        r2 = c.delete(f"{BASE}/api/images/{rel(p0)}")
        print(f"重复删除: {r2.status_code} {r2.json().get('error')}")
        if r2.status_code != 404:
            print("  ✗ 已经不存在的图，重复删除应返回 404")
            ok = False

        # ---- 批量：勾 2 张删，第 3 张必须还在 ----
        batch = [make(1), make(2), make(3)]
        before_batch = total(c)          # 三张都在的时候取一次
        rb = c.post(f"{BASE}/api/images/delete",
                    json={"relpaths": [rel(batch[0]), rel(batch[1])]})
        print(f"批量删除 2 张: {rb.status_code} removed={len(rb.json().get('removed', []))}")
        if rb.status_code != 200:
            ok = False
        for p in batch[:2]:
            if p.exists() or p.with_suffix(".json").exists():
                print(f"  ✗ {p.name} 还在磁盘上")
                ok = False
        if not batch[2].exists():
            print("  ✗ 没勾的第 3 张被顺手删掉了")
            ok = False
        after2 = total(c)
        print(f"  total {before_batch} → {after2}（应 −2）")
        if after2 != before_batch - 2:
            ok = False

        # 批量里混入不存在的路径：返回 missing，不整体失败
        rm = c.post(f"{BASE}/api/images/delete",
                    json={"relpaths": [rel(batch[2]), f"{DAY}/zz_del_test_999.png"]})
        miss = rm.json().get("missing", [])
        print(f"混合删除: {rm.status_code} removed={len(rm.json().get('removed', []))} missing={miss}")
        if rm.status_code != 200 or not miss:
            ok = False

        # ---- 根目录、文件名带下划线的图：必须能删 ----
        # 这是 `_safe_target` 漏 `len(parts) > 1` 时漏测的形状：只有一层路径 + 名字以 `_` 开头，
        # 旧判据把它当"`_` 前缀目录"拒成 400，而它在列表里是可见的（点删除必然报错）。
        pr = ROOT / "_zz_del_root.png"
        shutil.copyfile(SRC, pr)
        pr.with_suffix(".json").write_text(json.dumps(
            {"created_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "prompt": "[DELETE-TEST] 根目录下划线文件名"},
            ensure_ascii=False), encoding="utf-8")
        created.extend([pr, pr.with_suffix(".json")])
        rr = c.delete(f"{BASE}/api/images/{pr.name}")
        print(f"删根目录 _ 文件名: {rr.status_code} · 还在? {pr.exists()}")
        if rr.status_code != 200 or pr.exists():
            print("  ✗ 根目录下划线文件名被误判成脚手架目录")
            ok = False

        # ---- 负向：脚手架目录里的文件不允许通过作品接口删 ----
        rn = c.delete(f"{BASE}/api/images/_trash/test_512.png")
        print(f"删 _trash 里的文件: {rn.status_code} {rn.json().get('error')}")
        if rn.status_code != 400:
            print("  ✗ 脚手架目录（_ 前缀）应判 invalid_path")
            ok = False

        # 越界路径：URL 可能被客户端/服务端先归一化成 /config.yaml（那样是 404 路由不存在），
        # 所以断言"没被删掉"这件事本身，而不是纠结 400 还是 404
        cfg = Path(__file__).resolve().parents[2] / "config.yaml"
        rz = c.delete(f"{BASE}/api/images/%2e%2e%2fconfig.yaml")
        print(f"越界路径(编码): {rz.status_code} {rz.json().get('error')} · config.yaml 还在? {cfg.exists()}")
        if not cfg.exists() or rz.status_code not in (400, 404):
            ok = False

        print(f"最终 total = {total(c)}（起始 {before}）")
        if total(c) != before:
            print("  ✗ 收尾数量没回到起始值")
            ok = False

    print("验收:", "通过" if ok else "不通过")
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        for p in created:
            try:
                if p.exists():
                    p.unlink()
            except Exception as e:
                print(f"清理失败 {p}: {e}")
