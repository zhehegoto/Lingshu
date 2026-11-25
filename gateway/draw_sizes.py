"""出图尺寸目录（发布版）。

这台开发机只有 8 GB 显存，实测 2K 可用、4K 在权重准备阶段 OOM；但发布包要给
不同机器用，所以这里只定义"允许请求"的尺寸，是否跑得动由各自的显存和运行结果
决定。界面按本机显存给一档推荐，用户仍可自己换更高档。
"""

from __future__ import annotations

RATIOS = ["1:1", "4:3", "3:4", "16:9", "9:16"]
RESOLUTIONS = ["1K", "2K", "4K"]

SIZES: dict[str, dict[str, tuple[int, int]]] = {
    "1K": {
        "1:1": (1024, 1024),
        "4:3": (1152, 864),
        "3:4": (864, 1152),
        "16:9": (1344, 768),
        "9:16": (768, 1344),
    },
    "2K": {
        "1:1": (2048, 2048),
        "4:3": (2304, 1728),
        "3:4": (1728, 2304),
        "16:9": (2688, 1536),
        "9:16": (1536, 2688),
    },
    "4K": {
        "1:1": (3840, 3840),
        "4:3": (3840, 2880),
        "3:4": (2880, 3840),
        "16:9": (3840, 2160),
        "9:16": (2160, 3840),
    },
}

NOTES = {
    "4K": "4K 很吃显存：8 GB 显存可能放不下，12 GB 以上更稳妥；放不下时会直接报错",
}

ALLOWED = {(w, h) for group in SIZES.values() for (w, h) in group.values()}


def recommend(vram_mib: int | None) -> str:
    """按本机显存给一档推荐；没有 NVIDIA 显卡就退回 1K。"""
    v = int(vram_mib or 0)
    if v >= 16000:
        return "4K"
    if v >= 10000:
        return "2K"
    return "1K"


def caps(vram_mib: int | None = None) -> dict:
    rec = recommend(vram_mib)
    return {
        "ratios": RATIOS,
        "resolutions": RESOLUTIONS,
        "sizes": {r: {k: list(v) for k, v in m.items()} for r, m in SIZES.items()},
        "notes": NOTES,
        "recommended": rec,
        "vram_mib": int(vram_mib or 0),
        "batch": {"allowed": [1, 2, 3, 4], "note": "1 张最快；2–4 张连续出图，更占显存和时间"},
        "lora": {"allowed": False, "note": "未验证"},
    }


def allowed_size(width: int, height: int) -> bool:
    return (int(width), int(height)) in ALLOWED
