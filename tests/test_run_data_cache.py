from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from models.run_data_cache import RunDataCache


def payload(index: int) -> dict:
    return {
        "time_s": float(index),
        "series_name": f"RUN {index}",
        "smu1": {"source_v": index, "values": {"Voltage": index, "Current": index}},
    }


class RunDataCacheTests(unittest.TestCase):
    def test_get_returns_copy_and_refreshes_lru_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = RunDataCache(max_runs=2, max_bytes=1024 * 1024)
            path = Path(tmp) / "workspace.keithley-workspace.sqlite"
            key_1 = cache.make_key(path, 1, 1, 101)
            key_2 = cache.make_key(path, 1, 2, 102)
            key_3 = cache.make_key(path, 1, 3, 103)

            cache.set(key_1, [payload(1)])
            cache.set(key_2, [payload(2)])
            cached = cache.get(key_1)
            self.assertIsNotNone(cached)
            cached[0]["time_s"] = 999

            self.assertEqual(cache.get(key_1)[0]["time_s"], 1.0)
            cache.set(key_3, [payload(3)])

            self.assertIsNotNone(cache.get(key_1))
            self.assertIsNone(cache.get(key_2))
            self.assertIsNotNone(cache.get(key_3))

    def test_memory_cap_evicts_oldest_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = RunDataCache(max_runs=10, max_bytes=900)
            path = Path(tmp) / "workspace.keithley-workspace.sqlite"
            for run_id in range(1, 6):
                cache.set(
                    cache.make_key(path, 1, run_id, 100 + run_id),
                    [{"blob": "x" * 120, "run": run_id}],
                )

            self.assertLessEqual(cache.total_bytes, cache.max_bytes)
            self.assertLess(cache.size, 5)
            self.assertIsNotNone(cache.get(cache.make_key(path, 1, 5, 105)))

    def test_append_and_invalidate_project(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = RunDataCache(max_runs=5, max_bytes=1024 * 1024)
            path = Path(tmp) / "workspace.keithley-workspace.sqlite"
            key_1 = cache.make_key(path, 1, 1, 101)
            key_2 = cache.make_key(path, 1, 2, 102)
            key_3 = cache.make_key(path, 2, 1, 201)

            cache.append(key_1, payload(1))
            cache.append(key_1, payload(2))
            cache.set(key_2, [payload(3)])
            cache.set(key_3, [payload(4)])

            self.assertEqual(len(cache.get(key_1)), 2)
            cache.invalidate_project(path, 1)

            self.assertIsNone(cache.get(key_1))
            self.assertIsNone(cache.get(key_2))
            self.assertIsNotNone(cache.get(key_3))

    def test_clear_workspace_only_removes_matching_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = RunDataCache(max_runs=5, max_bytes=1024 * 1024)
            path_1 = Path(tmp) / "one.keithley-workspace.sqlite"
            path_2 = Path(tmp) / "two.keithley-workspace.sqlite"
            key_1 = cache.make_key(path_1, 1, 1, 101)
            key_2 = cache.make_key(path_2, 1, 1, 101)

            cache.set(key_1, [payload(1)])
            cache.set(key_2, [payload(2)])
            cache.clear_workspace(path_1)

            self.assertIsNone(cache.get(key_1))
            self.assertIsNotNone(cache.get(key_2))


if __name__ == "__main__":
    unittest.main()
