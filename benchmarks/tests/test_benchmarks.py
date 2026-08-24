from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

BENCHMARKS = Path(__file__).resolve().parents[1]
ROOT = BENCHMARKS.parent
sys.path.insert(0, str(BENCHMARKS))

from lib import (  # noqa: E402
    BASE_WORKLOADS,
    LOAD_CONCURRENCY,
    LOAD_TRANSPORTS,
    LOAD_WORKLOADS,
    TRANSPORTS,
    ValidationError,
    coefficient_of_variation,
    percentile,
    summarize_rounds,
    validate_dataset,
    validate_manifest,
    validate_publishable_dataset,
)


class StatisticsTests(unittest.TestCase):
    def test_percentiles_use_linear_interpolation(self) -> None:
        self.assertEqual(percentile([1, 2, 3, 4, 5], 50), 3)
        self.assertAlmostEqual(percentile([10, 20], 95), 19.5)

    def test_coefficient_of_variation(self) -> None:
        self.assertEqual(coefficient_of_variation([5, 5, 5]), 0)
        self.assertGreater(coefficient_of_variation([5, 10, 15]), 0.3)

    def test_summary_counts_bidirectional_payload(self) -> None:
        summary = summarize_rounds([
            {"samples_ns": [100, 110, 120], "duration_ns": 330},
            {"samples_ns": [90, 100, 110], "duration_ns": 300},
        ], payload_bytes=1024)
        self.assertEqual(summary["sample_count"], 6)
        self.assertGreater(summary["payload_bytes_per_second"], 0)
        self.assertEqual(len(summary["rounds"]), 2)

    def test_noisy_rounds_are_flagged(self) -> None:
        summary = summarize_rounds([
            {"samples_ns": [100, 100], "duration_ns": 200},
            {"samples_ns": [200, 200], "duration_ns": 400},
        ])
        self.assertEqual(summary["status"], "noisy")


class ContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest = json.loads((BENCHMARKS / "manifest.json").read_text())
        cls.dataset = json.loads((ROOT / "src/data/benchmarks.json").read_text())

    def test_checked_in_contracts_validate(self) -> None:
        validate_manifest(self.manifest)
        validate_dataset(self.dataset, self.manifest)

    def test_unreleased_version_cannot_masquerade_as_rust_release(self) -> None:
        changed = json.loads(json.dumps(self.dataset))
        rust = next(item for item in changed["implementations"] if item["id"] == "rust")
        rust["version"] = "0.24.0"
        with self.assertRaisesRegex(ValidationError, "not pinned"):
            validate_dataset(changed, self.manifest)

    def test_controlled_layer_requires_rust_client(self) -> None:
        changed = json.loads(json.dumps(self.dataset))
        changed["results"] = [{
            "layer": "controlled",
            "client": "python",
            "server": "python",
            "transport": "stdio",
            "workload": "void_noop",
            "status": "failed",
        }]
        with self.assertRaisesRegex(ValidationError, "Rust client"):
            validate_dataset(changed, self.manifest)

    def test_unsupported_transport_cannot_have_measurements(self) -> None:
        changed = json.loads(json.dumps(self.dataset))
        changed["results"] = [{
            "layer": "controlled",
            "client": "rust",
            "server": "typescript",
            "transport": "stdio_shm",
            "workload": "void_noop",
            "status": "failed",
        }]
        with self.assertRaisesRegex(ValidationError, "unsupported transport"):
            validate_dataset(changed, self.manifest)

    def test_quick_candidate_cannot_be_published(self) -> None:
        changed = json.loads(json.dumps(self.dataset))
        changed.update(status="candidate", run_mode="quick", generated_at="2026-08-24T00:00:00Z")
        with self.assertRaisesRegex(ValidationError, "only a full"):
            validate_publishable_dataset(changed, self.manifest)

    def test_partial_full_candidate_cannot_be_published(self) -> None:
        changed = self._full_candidate()
        changed["results"].pop()
        with self.assertRaisesRegex(ValidationError, "missing"):
            validate_publishable_dataset(changed, self.manifest)

    def test_failed_scenario_is_retained_in_a_complete_matrix(self) -> None:
        changed = self._full_candidate()
        measured = next(result for result in changed["results"] if result["status"] == "complete")
        measured.update(status="failed", error="worker failed")
        for field in (
            "sample_count", "mean_ns", "p50_ns", "p95_ns", "p99_ns",
            "operations_per_second", "payload_bytes_per_second",
            "cpu_utilization_percent", "round_cv", "rounds",
        ):
            measured.pop(field, None)
        validate_publishable_dataset(changed, self.manifest)

    def test_failed_scenario_requires_captured_error(self) -> None:
        changed = self._full_candidate()
        measured = next(result for result in changed["results"] if result["status"] == "complete")
        measured["status"] = "failed"
        with self.assertRaisesRegex(ValidationError, "needs an error"):
            validate_dataset(changed, self.manifest)

    def test_complete_full_candidate_is_publishable(self) -> None:
        validate_publishable_dataset(self._full_candidate(), self.manifest)

    def _full_candidate(self) -> dict:
        candidate = json.loads(json.dumps(self.dataset))
        candidate.update(status="candidate", run_mode="full", generated_at="2026-08-24T00:00:00Z")
        candidate["methodology"] = {
            "benchmark_suite": "combined",
            "canonical_client": "rust",
            "latency_warmup_seconds": 1,
            "latency_round_seconds": 1,
            "latency_rounds": "5-7",
            "scaling_warmup_seconds": 1,
            "scaling_round_seconds": 2,
            "scaling_rounds": "3-5",
            "noise_threshold_cv": 0.05,
            "seed": 1447316557,
            "server_order": list(self.manifest["implementations"]),
            "load_concurrency": list(LOAD_CONCURRENCY),
        }
        candidate["preflight"] = {
            "ac_power": True,
            "low_power_mode": False,
            "runner": "dedicated_linux_arm64",
            "instance_type": "c9gd.8xlarge",
            "cpu_count": 32,
            "load_average_1m": 0.5,
        }
        candidate["host"]["instance_type"] = "c9gd.8xlarge"
        results = []
        for server, implementation in self.manifest["implementations"].items():
            for transport in TRANSPORTS:
                supported = implementation["commands"][transport] is not None
                for workload in BASE_WORKLOADS:
                    results.append(self._result(server, transport, workload, 1, supported))
                if supported and transport in LOAD_TRANSPORTS:
                    for workload in LOAD_WORKLOADS:
                        for concurrency in LOAD_CONCURRENCY:
                            results.append(self._result(server, transport, workload, concurrency, True))
        candidate["results"] = results
        return candidate

    @staticmethod
    def _result(server: str, transport: str, workload: str, concurrency: int, supported: bool) -> dict:
        result = {
            "layer": "controlled",
            "client": "rust",
            "server": server,
            "transport": transport,
            "workload": workload,
            "concurrency": concurrency,
            "status": "complete" if supported else "unsupported",
        }
        if supported:
            is_load = workload in LOAD_WORKLOADS
            sample_count = concurrency if is_load else 100
            duration_ns = 2_000_000_000 if is_load else 1_000_000_000
            round_count = 3 if is_load else 5
            rounds = [
                {"sample_count": sample_count, "duration_ns": duration_ns, "p50_ns": 1, "p95_ns": 1, "p99_ns": 1, "min_ns": 1, "max_ns": 1}
                for _ in range(round_count)
            ]
            result.update(
                sample_count=sample_count * round_count,
                mean_ns=1,
                p50_ns=1,
                p95_ns=1,
                p99_ns=1,
                operations_per_second=1,
                round_cv=0,
                rounds=rounds,
            )
        return result


if __name__ == "__main__":
    unittest.main()
