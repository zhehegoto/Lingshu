"""硬件自动识别的验活脚本。

跑法：python gateway\\tools\\hardware_check.py   （不需要服务在跑）
既验真机，也验"参数选错会变坏"的方向 —— 用假硬件跑 decide()，
看 6GB / 4GB / 无显卡 会不会真的降档，而不是永远返回同一组数。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from gateway import config as cfgmod          # noqa: E402
from gateway import hardware                  # noqa: E402

FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global FAIL
    if cond:
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


print("== A. 真机探测 ==")
plan = hardware.detect(force=True)
gpu = plan["gpu"]
check("检测到 NVIDIA 显卡", bool(gpu), str(gpu))
if gpu:
    check("显存 > 0", int(gpu["vram_mib"]) > 0, gpu["vram_mib"])
    check("驱动版本非空", bool(gpu["driver"]), gpu["driver"])
check("CPU 物理核 >= 4", plan["cpu"]["physical"] >= 4, plan["cpu"])
check("物理核 <= 逻辑线程", plan["cpu"]["physical"] <= plan["cpu"]["logical"], plan["cpu"])
check("内存总量 > 8 GB", plan["ram"]["total_mib"] > 8192, plan["ram"]["total_mib"])
check("磁盘余量被读到", plan["disk"]["total_gib"] > 0, plan["disk"])

print("== B. GGUF 元数据 + KV 估算 ==")
check("解析出 block_count", bool(plan["gguf"].get("block_count")), plan["gguf"])
check("KV 每 token 字节数 > 0", hardware.kv_bytes_per_token(
    {f"gemma4.{k}": v for k, v in plan["gguf"].items() if k != "arch"}) > 0
    or plan["kv_mib_at_ctx"] > 0, plan["kv_mib_at_ctx"])
check("4096 上下文 KV > 100 MB", plan["kv_mib_at_ctx"] > 100, plan["kv_mib_at_ctx"])

print("== C. 真机参数与常量脱钩（护栏/预算按显存算）==")
if gpu:
    vram = int(gpu["vram_mib"])
    check("护栏 = 94% 显存", plan["guard_mib"] == int(vram * 0.94),
          f"{plan['guard_mib']} vs {int(vram * 0.94)}")
    check("护栏不再等于写死的 7700", plan["guard_mib"] != cfgmod.PEAK_GUARD_MIB
          or vram == 8188, f"{plan['guard_mib']}")
    check("budget = 84% 显存", plan["budget_mib"] == int(vram * 0.84), plan["budget_mib"])

print("== D. 启动参数真的用了自动档 ==")
la = cfgmod.llama_args()
check("--threads 存在", "--threads" in la, la)
check("--batch-size 存在", "--batch-size" in la, la)
check("--ubatch-size 存在", "--ubatch-size" in la, la)
check("-fa 跟着计划走",
      (("-fa" in la) == bool(plan["flash_attn"])), la)
check("-ngl 等于计划值", la[la.index("--n-gpu-layers") + 1] == str(plan["ngl"]), la)
sa = cfgmod.sd_args()
check("tile 等于生效值", sa[sa.index("--vae-tile-size") + 1] == str(
      hardware.sd_tile(cfgmod.load_config().settings.vae_tile)), sa)

print("== E. 负向：假硬件必须降档 ==")
cpu = {"name": "fake", "physical": 14, "logical": 20}
big = hardware.decide({"name": "fake-8g", "vram_mib": 8188, "free_mib": 3000, "driver": "1"},
                      cpu, 5495, 172032)
check("8G 整块进显存", big["ngl"] == 99 and big["fits"], big)

small = hardware.decide({"name": "fake-6g", "vram_mib": 6144, "free_mib": 2000, "driver": "1"},
                        cpu, 5495, 172032)
check("6G 不再 99 层", small["ngl"] < 99, small["ngl"])
check("6G tile 仍是 384（>=6G）", small["vae_tile"] == 384, small["vae_tile"])
check("6G 护栏按自己的显存算", small["guard_mib"] == int(6144 * 0.94), small["guard_mib"])

tiny = hardware.decide({"name": "fake-4g", "vram_mib": 4096, "free_mib": 1000, "driver": "1"},
                       cpu, 5495, 172032)
check("4G tile 压到 256", tiny["vae_tile"] == 256, tiny["vae_tile"])
check("4G ngl 很小", tiny["ngl"] < small["ngl"], tiny["ngl"])

nocpu_gpu = hardware.decide(None, cpu, 5495, 172032)
check("无显卡 -ngl 0", nocpu_gpu["ngl"] == 0, nocpu_gpu)
check("无显卡关 FlashAttn", nocpu_gpu["flash_attn"] is False, nocpu_gpu)
check("无显卡 ctx 降到 2048", nocpu_gpu["ctx"] == 2048, nocpu_gpu["ctx"])
check("无显卡写清原因", any("显卡" in n for n in nocpu_gpu["notes"]), nocpu_gpu["notes"])

print("== F. 小 CPU 的线程数 ==")
check("4 核给 4 线程", hardware.decide(None, {"physical": 4, "logical": 4}, 100, 0)["threads"] == 4)
check("20 核封顶 12", hardware.decide(None, {"physical": 20, "logical": 20}, 100, 0)["threads"] == 12)

print()
if FAIL:
    print(f"FAILED: {FAIL} 项")
    sys.exit(1)
print("ALL PASS")
