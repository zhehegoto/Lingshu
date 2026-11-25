"""模型导入的纯函数部分：文件名白名单、同名副本反查。"""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from gateway.api import model_admin as ma


class SafeNameTest(unittest.TestCase):
    def test_accepts_plain_model_names(self):
        for raw in ("Gemma-4-E4B-Q5_K_M.gguf", "qwen_image_vae.safetensors",
                    "%E4%B8%AD%E6%96%87.gguf"):
            self.assertIsNotNone(ma._safe_name(raw), raw)

    def test_rejects_paths_and_wrong_ext(self):
        for raw in ("../../evil.gguf", r"C:\Windows\evil.gguf", "sub/dir/a.gguf",
                    "a.txt", "a.exe", "", ".gguf", ".."):
            self.assertIsNone(ma._safe_name(raw), raw)


class DuplicateTest(unittest.TestCase):
    def test_finds_same_name_and_size_only(self):
        with TemporaryDirectory() as td:
            base = Path(td)
            (base / "121" / "models").mkdir(parents=True)
            mine = base / "121" / "models" / "m.gguf"
            mine.write_bytes(b"x" * 1024)
            models = base / "app" / "models"
            models.mkdir(parents=True)
            (models / "m.gguf").write_bytes(b"x" * 1024)
            (base / "other.gguf").write_bytes(b"x" * 2048)

            old = ma._dup_roots
            ma._dup_roots = lambda: [base]
            try:
                found = ma._find_duplicates("m.gguf", 1024, models)
            finally:
                ma._dup_roots = old
            self.assertEqual([d["path"] for d in found], [str(mine)])

    def test_skips_models_dir_itself(self):
        with TemporaryDirectory() as td:
            base = Path(td)
            models = base / "models"
            models.mkdir()
            (models / "m.gguf").write_bytes(b"x" * 512)
            old = ma._dup_roots
            ma._dup_roots = lambda: [base]
            try:
                self.assertEqual(ma._find_duplicates("m.gguf", 512, models), [])
            finally:
                ma._dup_roots = old


if __name__ == "__main__":
    unittest.main()
