from __future__ import annotations

import copy
import csv
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "benchmarks"))
from sdk_footprint import DATA, MANIFEST, PUBLIC, check


class SdkFootprintTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = json.loads(DATA.read_text())
        cls.manifest = json.loads(MANIFEST.read_text())

    def test_published_data_and_downloads_match(self):
        check(self.data, self.manifest, PUBLIC, public=True)
        self.assertEqual(self.data, json.loads((PUBLIC / "footprint.json").read_text()))
        with (PUBLIC / "footprint.csv").open() as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(
            [r["id"] for r in rows], [r["id"] for r in self.data["results"]]
        )
        for row, expected in zip(rows, self.data["results"], strict=True):
            for key, value in row.items():
                self.assertEqual(
                    float(value) if key.endswith("_bytes") else value, expected[key]
                )

    def test_rejects_different_driver_and_dependency_resolution(self):
        for key, value in [("sources", {}), ("rust_lock_sha256", "0" * 64)]:
            changed = copy.deepcopy(self.data)
            changed[key] = value
            with self.assertRaises(ValueError):
                check(changed, self.manifest, PUBLIC, public=True)

    def test_rpc_workers_cannot_replace_sdk_workers(self):
        rpc = json.loads((ROOT / "src/data/footprint.json").read_text())
        with self.assertRaises(ValueError):
            check(rpc, self.manifest, PUBLIC, public=True)
