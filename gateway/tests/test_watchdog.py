import unittest

from gateway.supervisor.watchdog import VramWatchdog
from gateway.supervisor.vram_log import VramLog
from pathlib import Path


class FakeLog(VramLog):
    def __init__(self):
        self.samples = []
        self.dropped = 0

    put_nowait = lambda self, s: self.samples.append(s)


def wd(guard=7700, consecutive=10):
    return VramWatchdog(nvidia_smi=Path("nvidia-smi"), vram_log=FakeLog(),
                        guard_mib=guard, consecutive=consecutive)


class TestClassify(unittest.TestCase):
    def test_normal_is_ok(self):
        w = wd()
        for _ in range(10):
            self.assertEqual(w.classify(6680), "ok")

    def test_single_spike_does_not_trip(self):
        w = wd()
        self.assertEqual(w.classify(7800), "warn")
        self.assertEqual(w.classify(6690), "ok", "单点毛刺后必须回到 ok")
        self.assertFalse(w.tripped)

    def test_nine_points_warn_tenth_trips(self):
        w = wd()
        for i in range(9):
            self.assertEqual(w.classify(7800), "warn", f"第 {i + 1} 点不应触发")
        self.assertEqual(w.classify(7800), "trip", "第 10 点必须触发")

    def test_warn_band_is_marked_but_not_tripped(self):
        # 观察带：7700–7900 只标记，不参与触发（这里把 guard 抬高到 7900 模拟"未超限"）
        w = VramWatchdog(nvidia_smi=Path("nvidia-smi"), vram_log=FakeLog(),
                         guard_mib=7900, consecutive=10, warn_band=(7700, 7900))
        for _ in range(20):
            self.assertEqual(w.classify(7800), "warn")
        self.assertFalse(w.tripped)

    def test_counter_resets_on_drop(self):
        w = wd()
        for _ in range(9):
            w.classify(7800)
        w.classify(6000)
        for i in range(9):
            self.assertEqual(w.classify(7800), "warn", f"重置后第 {i + 1} 点不应触发")
        self.assertEqual(w.classify(7800), "trip")


class TestGuardInjectionSemantics(unittest.TestCase):
    def test_guard_5000_trips_on_normal_load(self):
        # 故障注入第 4 条用的口径：护栏临时改 5000 时，6690 也算超限
        w = VramWatchdog(nvidia_smi=Path("nvidia-smi"), vram_log=FakeLog(),
                         guard_mib=5000, consecutive=10)
        flags = [w.classify(6690) for _ in range(10)]
        self.assertEqual(flags[:9], ["warn"] * 9)
        self.assertEqual(flags[9], "trip")


if __name__ == "__main__":
    unittest.main(verbosity=2)
