"""Validation and statistics for benchmark-dataset/v1.

The module is deliberately standard-library-only so CI can validate benchmark
data without installing the language implementations or benchmark toolchain.
"""

from __future__ import annotations

import math
import statistics
from typing import Any, Iterable

TRANSPORTS = ("stdio", "unix", "tcp", "http_identity", "http_zstd", "stdio_shm")
LAYERS = ("controlled", "native")
RESULT_STATUSES = ("complete", "unsupported", "failed", "noisy")
DATASET_STATUSES = ("awaiting_run", "candidate", "published")
IMPLEMENTATIONS = ("python", "typescript", "go", "rust", "java", "csharp", "cpp")
BASE_WORKLOADS = (
    "void_noop",
    "add_floats",
    "echo_string_11b",
    "echo_binary_1024",
    "echo_binary_65536",
    "echo_binary_1048576",
    "echo_binary_16777216",
)
LOAD_WORKLOADS = ("add_floats_load", "echo_binary_1048576_load")
LOAD_TRANSPORTS = ("unix", "tcp", "http_identity", "http_zstd")
LOAD_CONCURRENCY = (1, 2, 4, 8, 16)


class ValidationError(ValueError):
    """A benchmark manifest or dataset is unsafe to publish."""


def percentile(samples: Iterable[float], percentile_value: float) -> float:
    """Return a linearly interpolated percentile (R-7 / NumPy default)."""
    values = sorted(float(value) for value in samples)
    if not values:
        raise ValueError("percentile requires at least one sample")
    if not 0 <= percentile_value <= 100:
        raise ValueError("percentile must be between 0 and 100")
    position = (len(values) - 1) * percentile_value / 100
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return values[lower]
    weight = position - lower
    return values[lower] * (1 - weight) + values[upper] * weight


def coefficient_of_variation(values: Iterable[float]) -> float:
    """Return population standard deviation divided by the mean."""
    samples = [float(value) for value in values]
    if not samples:
        raise ValueError("coefficient_of_variation requires samples")
    mean = statistics.fmean(samples)
    if mean == 0:
        return 0.0 if all(value == 0 for value in samples) else math.inf
    return statistics.pstdev(samples) / mean


def summarize_rounds(rounds: list[dict[str, Any]], payload_bytes: int = 0) -> dict[str, Any]:
    """Aggregate raw nanosecond samples while retaining round-level evidence."""
    if not rounds:
        raise ValidationError("a complete result needs at least one round")
    all_samples: list[float] = []
    total_duration_ns = 0
    round_p50: list[float] = []
    normalized: list[dict[str, Any]] = []
    for index, round_data in enumerate(rounds, start=1):
        samples = [float(value) for value in round_data.get("samples_ns", [])]
        duration_ns = int(round_data.get("duration_ns", 0))
        if not samples or duration_ns <= 0 or any(value <= 0 for value in samples):
            raise ValidationError(f"round {index} has invalid samples or duration")
        p50 = percentile(samples, 50)
        all_samples.extend(samples)
        total_duration_ns += duration_ns
        round_p50.append(p50)
        normalized.append({
            "sample_count": len(samples),
            "duration_ns": duration_ns,
            "p50_ns": p50,
            "p95_ns": percentile(samples, 95),
            "p99_ns": percentile(samples, 99),
            "min_ns": min(samples),
            "max_ns": max(samples),
        })
    rate = len(all_samples) / (total_duration_ns / 1_000_000_000)
    cv = coefficient_of_variation(round_p50)
    return {
        "sample_count": len(all_samples),
        "mean_ns": statistics.fmean(all_samples),
        "p50_ns": percentile(all_samples, 50),
        "p95_ns": percentile(all_samples, 95),
        "p99_ns": percentile(all_samples, 99),
        "operations_per_second": rate,
        "payload_bytes_per_second": rate * payload_bytes * 2 if payload_bytes else 0,
        "round_cv": cv,
        "rounds": normalized,
        "status": "noisy" if cv > 0.05 else "complete",
    }


def validate_manifest(manifest: dict[str, Any]) -> None:
    if manifest.get("schema_version") != 1:
        raise ValidationError("manifest schema_version must be 1")
    canonical = manifest.get("canonical_client", {})
    if canonical.get("implementation") != "rust":
        raise ValidationError("the controlled comparison client must be Rust")
    implementations = manifest.get("implementations", {})
    if tuple(implementations) != IMPLEMENTATIONS:
        raise ValidationError(f"implementations must be ordered as {IMPLEMENTATIONS}")
    for implementation_id, implementation in implementations.items():
        sha = implementation.get("sha", "")
        if len(sha) != 40 or any(char not in "0123456789abcdef" for char in sha):
            raise ValidationError(f"{implementation_id}: sha must be a full lowercase commit id")
        if implementation.get("release_status") not in ("published", "unreleased_snapshot"):
            raise ValidationError(f"{implementation_id}: invalid release_status")
        commands = implementation.get("commands", {})
        if tuple(commands) != TRANSPORTS:
            raise ValidationError(f"{implementation_id}: transport keys must be {TRANSPORTS}")
        for transport, command in commands.items():
            if command is not None and (not isinstance(command, list) or not command):
                raise ValidationError(f"{implementation_id}/{transport}: command must be argv or null")
    if canonical.get("sha") != implementations["rust"].get("sha"):
        raise ValidationError("canonical client and Rust server must use the same release commit")


def validate_dataset(dataset: dict[str, Any], manifest: dict[str, Any]) -> None:
    if dataset.get("schema_version") != 1:
        raise ValidationError("dataset schema_version must be 1")
    if dataset.get("status") not in DATASET_STATUSES:
        raise ValidationError("invalid dataset status")
    if dataset.get("status") != "awaiting_run" and not dataset.get("generated_at"):
        raise ValidationError("candidate and published datasets require generated_at")
    if dataset.get("status") != "awaiting_run" and dataset.get("run_mode") not in ("quick", "full"):
        raise ValidationError("candidate and published datasets require a valid run_mode")
    host = dataset.get("host", {})
    for field in ("model", "chip", "cores", "memory_gb", "os", "arch"):
        if field not in host:
            raise ValidationError(f"host.{field} is required")
    expected = manifest["implementations"]
    implementations = dataset.get("implementations", [])
    if [item.get("id") for item in implementations] != list(expected):
        raise ValidationError("dataset implementation order does not match the manifest")
    for item in implementations:
        source = expected[item["id"]]
        for field in ("version", "release", "sha", "release_status"):
            if item.get(field) != source.get(field):
                raise ValidationError(f"{item['id']}: dataset {field} is not pinned to the manifest")
    seen: set[tuple[Any, ...]] = set()
    for index, result in enumerate(dataset.get("results", [])):
        layer = result.get("layer")
        server = result.get("server")
        client = result.get("client")
        transport = result.get("transport")
        status = result.get("status")
        concurrency = result.get("concurrency", 1)
        if layer not in LAYERS or transport not in TRANSPORTS or status not in RESULT_STATUSES:
            raise ValidationError(f"result {index}: invalid layer, transport, or status")
        if not isinstance(concurrency, int) or isinstance(concurrency, bool) or concurrency < 1:
            raise ValidationError(f"result {index}: concurrency must be a positive integer")
        if server not in expected or client not in expected:
            raise ValidationError(f"result {index}: unknown client/server")
        if layer == "controlled" and client != "rust":
            raise ValidationError(f"result {index}: controlled results must use the Rust client")
        if layer == "native" and client != server:
            raise ValidationError(f"result {index}: native results must use matching client/server")
        key = (layer, client, server, transport, result.get("workload"), concurrency)
        if key in seen:
            raise ValidationError(f"result {index}: duplicate scenario {key}")
        seen.add(key)
        command = expected[server]["commands"][transport]
        if status == "unsupported" and command is not None:
            raise ValidationError(f"result {index}: supported transport marked unsupported")
        if status != "unsupported" and command is None:
            raise ValidationError(f"result {index}: unsupported transport has measurements")
        if status == "failed" and (
            not isinstance(result.get("error"), str) or not result["error"].strip()
        ):
            raise ValidationError(f"result {index}: failed result needs an error")
        if status in ("complete", "noisy"):
            for field in ("sample_count", "mean_ns", "p50_ns", "p95_ns", "p99_ns", "operations_per_second", "round_cv"):
                if not isinstance(result.get(field), (int, float)) or result[field] < 0:
                    raise ValidationError(f"result {index}: invalid {field}")
            if result["sample_count"] < 1 or result["p50_ns"] <= 0 or result["operations_per_second"] <= 0:
                raise ValidationError(f"result {index}: measured counts, latency, and rate must be positive")
            if not result["p50_ns"] <= result["p95_ns"] <= result["p99_ns"]:
                raise ValidationError(f"result {index}: latency percentiles are not ordered")
            rounds = result.get("rounds")
            if not rounds:
                raise ValidationError(f"result {index}: measured result needs round evidence")
            round_samples = 0
            for round_index, round_data in enumerate(rounds, start=1):
                required = ("sample_count", "duration_ns", "p50_ns", "p95_ns", "p99_ns", "min_ns", "max_ns")
                if any(not isinstance(round_data.get(field), (int, float)) for field in required):
                    raise ValidationError(f"result {index} round {round_index}: invalid round evidence")
                if round_data["sample_count"] < 1 or round_data["duration_ns"] < 1 or round_data["min_ns"] < 1:
                    raise ValidationError(f"result {index} round {round_index}: round evidence must be positive")
                if not round_data["min_ns"] <= round_data["p50_ns"] <= round_data["p95_ns"] <= round_data["p99_ns"] <= round_data["max_ns"]:
                    raise ValidationError(f"result {index} round {round_index}: round statistics are not ordered")
                round_samples += round_data["sample_count"]
            if round_samples != result["sample_count"]:
                raise ValidationError(f"result {index}: sample_count does not match round evidence")


def validate_publishable_dataset(dataset: dict[str, Any], manifest: dict[str, Any]) -> None:
    """Reject smoke, partial, or structurally incomplete candidates."""
    validate_dataset(dataset, manifest)
    if dataset.get("status") != "candidate":
        raise ValidationError("publish input must be a candidate dataset")
    if dataset.get("run_mode") != "full":
        raise ValidationError("only a full benchmark run can be published")
    methodology = dataset.get("methodology", {})
    required_methodology = {
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
        "load_concurrency": list(LOAD_CONCURRENCY),
    }
    for field, value in required_methodology.items():
        if methodology.get(field) != value:
            raise ValidationError(f"full candidate methodology.{field} must be {value!r}")
    if sorted(methodology.get("server_order", [])) != sorted(IMPLEMENTATIONS):
        raise ValidationError("full candidate methodology.server_order must include every implementation once")
    preflight = dataset.get("preflight", {})
    if preflight.get("ac_power") is not True or preflight.get("low_power_mode") is not False:
        raise ValidationError("full candidate must record AC power with Low Power Mode disabled")
    if preflight.get("runner") != "dedicated_linux_arm64" or preflight.get("instance_type") != "c9gd.8xlarge":
        raise ValidationError("full candidate must identify an approved benchmark runner")
    load_average = preflight.get("load_average_1m")
    if not isinstance(load_average, (int, float)) or isinstance(load_average, bool) or load_average >= 2.0:
        raise ValidationError("full candidate must record a pre-run load average below 2.0")
    if dataset.get("host", {}).get("instance_type") != "c9gd.8xlarge":
        raise ValidationError("full candidate host metadata must identify the canonical EC2 instance type")

    expected: dict[tuple[str, str, str, str, int], str] = {}
    for server, implementation in manifest["implementations"].items():
        for transport in TRANSPORTS:
            command = implementation["commands"][transport]
            base_status = "unsupported" if command is None else "measured"
            for workload in BASE_WORKLOADS:
                expected[("controlled", "rust", server, transport, workload, 1)] = base_status
            if transport in LOAD_TRANSPORTS and command is not None:
                for workload in LOAD_WORKLOADS:
                    for concurrency in LOAD_CONCURRENCY:
                        expected[("controlled", "rust", server, transport, workload, concurrency)] = "measured"

    actual: dict[tuple[str, str, str, str, int], str] = {}
    for result in dataset.get("results", []):
        if result.get("layer") != "controlled":
            continue
        key = (
            result["layer"],
            result["client"],
            result["server"],
            result["transport"],
            result.get("workload"),
            result.get("concurrency", 1),
        )
        actual[key] = result["status"]

    missing = sorted(set(expected) - set(actual))
    extra = sorted(set(actual) - set(expected))
    if missing:
        raise ValidationError(f"full candidate is missing {len(missing)} controlled scenarios; first: {missing[0]}")
    if extra:
        raise ValidationError(f"full candidate has {len(extra)} unexpected controlled scenarios; first: {extra[0]}")
    for key, expected_status in expected.items():
        status = actual[key]
        allowed = {"unsupported"} if expected_status == "unsupported" else {"complete", "noisy", "failed"}
        if status not in allowed:
            raise ValidationError(f"scenario {key} has status {status}; expected one of {sorted(allowed)}")

    minimum_samples = {
        "void_noop": 100,
        "add_floats": 100,
        "echo_string_11b": 100,
        "echo_binary_1024": 100,
        "echo_binary_65536": 100,
        "echo_binary_1048576": 100,
        "echo_binary_16777216": 5,
    }
    for index, result in enumerate(dataset.get("results", [])):
        if result.get("layer") != "controlled" or result.get("status") not in ("complete", "noisy"):
            continue
        workload = result["workload"]
        is_load = workload in LOAD_WORKLOADS
        rounds = result["rounds"]
        expected_rounds = (3, 5) if is_load else (5, 7)
        if len(rounds) not in expected_rounds:
            raise ValidationError(f"result {index}: full measurement must retain {expected_rounds} rounds")
        if result["status"] == "noisy" and len(rounds) != expected_rounds[-1]:
            raise ValidationError(f"result {index}: noisy measurement must include two extension rounds")
        minimum_duration_ns = 2_000_000_000 if is_load else 1_000_000_000
        minimum_count = result.get("concurrency", 1) if is_load else minimum_samples[workload]
        for round_index, round_data in enumerate(rounds, start=1):
            if round_data.get("duration_ns", 0) < minimum_duration_ns:
                raise ValidationError(f"result {index} round {round_index}: duration is shorter than the full-run window")
            if round_data.get("sample_count", 0) < minimum_count:
                raise ValidationError(f"result {index} round {round_index}: too few samples for the workload")


def implementations_from_manifest(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "id": implementation_id,
            "name": implementation["name"],
            "version": implementation["version"],
            "release": implementation["release"],
            "sha": implementation["sha"],
            "release_status": implementation["release_status"],
            **(
                {"benchmark_runtime": implementation["benchmark_runtime"]}
                if "benchmark_runtime" in implementation
                else {}
            ),
        }
        for implementation_id, implementation in manifest["implementations"].items()
    ]
