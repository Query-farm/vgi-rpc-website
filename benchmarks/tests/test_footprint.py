from __future__ import annotations

import copy
import csv
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "benchmarks"))
from footprint import collect_files, validate, validate_inventories


class FootprintTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = json.loads(
            (ROOT / "benchmarks/footprint-manifest.json").read_text()
        )
        cls.data = json.loads((ROOT / "src/data/footprint.json").read_text())

    def test_published_snapshot_validates_and_download_matches(self):
        validate(self.data, self.manifest)
        validate_inventories(self.data, ROOT / "public/benchmarks", public=True)
        public = json.loads((ROOT / "public/benchmarks/footprint.json").read_text())
        self.assertEqual(self.data, public)

    def test_inventories_reject_changed_sizes(self):
        changed = copy.deepcopy(self.data)
        changed["results"][0]["app_bytes"] += 1
        with self.assertRaisesRegex(ValueError, "inventory"):
            validate_inventories(changed, ROOT / "public/benchmarks", public=True)

    def test_csv_download_matches_json_and_uses_lf(self):
        path = ROOT / "public/benchmarks/footprint.csv"
        self.assertNotIn(b"\r", path.read_bytes())
        with path.open() as handle:
            rows = list(csv.DictReader(handle))
        expected = {row["id"]: row for row in self.data["results"]}
        self.assertEqual({row["id"] for row in rows}, set(expected))
        for row in rows:
            for field in (
                "artifact_bytes",
                "app_bytes",
                "runtime_bytes",
                "total_bytes",
                "idle_rss_bytes",
                "peak_rss_bytes",
            ):
                self.assertEqual(float(row[field]), expected[row["id"]][field])

    def test_incomplete_language_set_cannot_publish(self):
        changed = copy.deepcopy(self.data)
        changed["results"].pop()
        with self.assertRaisesRegex(ValueError, "every implementation"):
            validate(changed, self.manifest)

    def test_wrong_source_revision_cannot_publish(self):
        changed = copy.deepcopy(self.data)
        changed["results"][0]["sha"] = "0" * 40
        with self.assertRaisesRegex(ValueError, "revision"):
            validate(changed, self.manifest)

    def test_missing_round_and_falsified_peak_cannot_publish(self):
        changed = copy.deepcopy(self.data)
        changed["results"][0]["rounds"].pop()
        with self.assertRaisesRegex(ValueError, "rounds"):
            validate(changed, self.manifest)
        changed = copy.deepcopy(self.data)
        changed["results"][0]["rounds"][0]["peak_rss_bytes"] += 1
        with self.assertRaisesRegex(ValueError, "observations"):
            validate(changed, self.manifest)

    def test_zero_rss_and_incomplete_correctness_checks_cannot_publish(self):
        changed = copy.deepcopy(self.data)
        changed["results"][0]["rounds"][0]["payload_samples"][0]["rss_bytes"] = 0
        with self.assertRaisesRegex(ValueError, "observation"):
            validate(changed, self.manifest)
        changed = copy.deepcopy(self.data)
        changed["results"][0]["rounds"][0]["validated_payload_calls"] -= 1
        with self.assertRaisesRegex(ValueError, "correctness"):
            validate(changed, self.manifest)

    def test_runtime_is_not_omitted_from_total(self):
        changed = copy.deepcopy(self.data)
        row = next(r for r in changed["results"] if r["runtime_bytes"] > 0)
        row["total_bytes"] = row["app_bytes"]
        with self.assertRaisesRegex(ValueError, "disk inventory"):
            validate(changed, self.manifest)

    def test_files_count_once_across_symlinks_hardlinks_and_roots(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "worker"
            source.write_bytes(b"worker bytes")
            (root / "symlink").symlink_to(source)
            os.link(source, root / "hardlink")
            (root / "__pycache__").mkdir()
            (root / "__pycache__/cached.pyc").write_bytes(b"ignored")
            files = collect_files([root, source], ("__pycache__",))
            self.assertEqual(len(files), 1)
            self.assertEqual(sum(p.stat().st_size for p in files), len(b"worker bytes"))

    def test_missing_artifact_is_an_error(self):
        with (
            tempfile.TemporaryDirectory() as temporary,
            self.assertRaisesRegex(ValueError, "Missing artifact"),
        ):
            collect_files([Path(temporary) / "absent"])


if __name__ == "__main__":
    unittest.main()
