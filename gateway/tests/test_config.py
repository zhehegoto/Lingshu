"""config 三层的行为断言，包括"回写真的落盘"这种副作用，而不只是返回值。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import yaml

from gateway.config import (
    Config, ConfigError, FIELD_SPECS, LockedField, N_CTX,
    load_config, sd_args, llama_args,
)


class TempConfigCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "config.yaml"
        self.cfg = load_config(self.path)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def disk(self) -> dict:
        return yaml.safe_load(self.path.read_text(encoding="utf-8"))["settings"]


class TestDefaults(TempConfigCase):
    def test_missing_yaml_is_created(self):
        self.assertTrue(self.path.exists())

    def test_defaults_match_contract(self):
        self.assertEqual(self.cfg.settings.as_dict(), {
            "steps": 8, "max_tokens": 1024, "thinking": True,
            "n_ctx": N_CTX, "vae_tile": 384, "batch_count_default": 1,
        })

    def test_six_settable_fields_only(self):
        # n_ctx 从 Layer C 解锁成可改字段：用户要能自己填上下文长度
        self.assertEqual(set(FIELD_SPECS), {"steps", "max_tokens", "thinking",
                                            "n_ctx", "vae_tile", "batch_count_default"})


class TestValidation(TempConfigCase):
    def test_locked_field_rejected(self):
        # kv_type / vae_tiling 仍是硬锁；n_ctx 已解锁，不再进这一组
        for name in ("kv_type", "vae_tiling"):
            with self.assertRaises(LockedField):
                self.cfg.update({name: 8192})

    def test_n_ctx_is_settable_and_restarts_chat(self):
        r = self.cfg.update({"n_ctx": 8192})
        self.assertTrue(r["restart_chat"])
        self.assertEqual(self.disk()["n_ctx"], 8192)

    def test_out_of_range_rejected(self):
        for name, bad in (("steps", 4), ("steps", 21), ("max_tokens", 0),
                          ("n_ctx", 256), ("n_ctx", 40000),
                          ("vae_tile", 500), ("batch_count_default", 0),
                          ("batch_count_default", 5)):   # 1–4 可选，5 越界
            with self.assertRaises(ConfigError):
                self.cfg.update({name: bad})

    def test_max_tokens_above_2048_now_allowed(self):
        # 老规则 256–2048 太小：用户要多少存多少（生效时仍受 n_ctx 截）
        r = self.cfg.update({"max_tokens": 4096})
        self.assertEqual(r["changed"], {"max_tokens": 4096})
        self.assertEqual(self.disk()["max_tokens"], 4096)

    def test_more_vae_tiles_allowed(self):
        r = self.cfg.update({"vae_tile": 512})
        self.assertTrue(r["restart_sd"])
        self.assertEqual(self.disk()["vae_tile"], 512)

    def test_unknown_field_rejected(self):
        with self.assertRaises(ConfigError):
            self.cfg.update({"peak_guard_mib": 6000})

    def test_bool_is_not_int(self):
        with self.assertRaises(ConfigError):
            self.cfg.update({"steps": True})


class TestWriteBack(TempConfigCase):
    def test_hot_change_persists_to_disk(self):
        r = self.cfg.update({"steps": 12})
        self.assertEqual(r["changed"], {"steps": 12})
        self.assertEqual(r["effects"], {"steps": "hot"})
        self.assertFalse(r["restart_sd"])
        self.assertEqual(self.disk()["steps"], 12)          # 副作用断言

    def test_vae_tile_requires_sd_restart(self):
        r = self.cfg.update({"vae_tile": 256})
        self.assertTrue(r["restart_sd"])
        self.assertEqual(self.disk()["vae_tile"], 256)

    def test_no_change_does_not_write(self):
        before = self.path.stat().st_mtime_ns
        r = self.cfg.update({"steps": 8})
        self.assertEqual(r["changed"], {})
        self.assertEqual(self.path.stat().st_mtime_ns, before)

    def test_bad_value_leaves_memory_untouched(self):
        with self.assertRaises(ConfigError):
            self.cfg.update({"steps": 30})
        self.assertEqual(self.cfg.settings.steps, 8)
        self.assertEqual(self.disk()["steps"], 8)


class TestEffectiveMaxTokens(TempConfigCase):
    def test_think_on_uses_config(self):
        self.assertEqual(self.cfg.effective_max_tokens(300), 1024)

    def test_think_off_no_longer_caps(self):
        # 曾经压到 512，实测会把长答案截断（870 字停在半句），规则已撤销
        self.cfg.settings.thinking = False
        self.assertEqual(self.cfg.effective_max_tokens(300), 1024)

    def test_n_ctx_is_hard_boundary(self):
        self.cfg.settings.thinking = False
        self.assertEqual(self.cfg.effective_max_tokens(3600), N_CTX - 3600 - 16)

    def test_never_below_one(self):
        # 地板从 64 改成 1：ctx 现在用户可调，prompt 顶满时不能返回负数/0
        self.cfg.settings.thinking = False
        self.assertEqual(self.cfg.effective_max_tokens(4096), 1)
        self.assertEqual(self.cfg.effective_max_tokens(9999), 1)


class TestDerivedArgs(TempConfigCase):
    def test_sd_args_follows_vae_tile(self):
        self.cfg.update({"vae_tile": 256})
        args = sd_args(self.cfg)
        self.assertEqual(args[args.index("--vae-tile-size") + 1], "256")

    def test_sd_args_has_no_mmproj(self):
        self.assertNotIn("--mmproj", sd_args(self.cfg))

    def test_llama_args_pins_ctx_and_ngl(self):
        # 上下文取配置值（用户手填优先），不再是硬件推荐档
        self.cfg.update({"n_ctx": 8192})
        args = llama_args(self.cfg)
        self.assertEqual(args[args.index("--ctx-size") + 1], "8192")
        self.assertEqual(args[args.index("--n-gpu-layers") + 1], "99")


class TestRaggedYaml(TempConfigCase):
    def test_bad_value_falls_back_to_default(self):
        doc = yaml.safe_load(self.path.read_text(encoding="utf-8"))
        doc["settings"]["steps"] = 4                    # 手改成非法值
        self.path.write_text(yaml.safe_dump(doc, allow_unicode=True), encoding="utf-8")
        cfg = load_config(self.path)
        self.assertEqual(cfg.settings.steps, 8)         # 不因此起不来

    def test_locked_values_ignore_yaml(self):
        doc = yaml.safe_load(self.path.read_text(encoding="utf-8"))
        doc["locked"] = {"kv_type": "q8_0"}            # 用户手加了一段也不该生效
        self.path.write_text(yaml.safe_dump(doc, allow_unicode=True), encoding="utf-8")
        cfg = load_config(self.path)
        self.assertEqual(cfg.locked["kv_type"], "f16")

    def test_yaml_has_no_locked_section(self):
        doc = yaml.safe_load(self.path.read_text(encoding="utf-8"))
        self.assertNotIn("locked", doc)                # 少一段少一个困惑源


if __name__ == "__main__":
    unittest.main()
