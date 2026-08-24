#!/usr/bin/env python3
"""Build and run the latest-release vgi-rpc transport benchmark matrix.

The publish command intentionally overwrites one `latest` dataset. Benchmark
history belongs in source control, not in the website UI or static assets.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import platform
import plistlib
import random
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
BENCHMARKS = ROOT / "benchmarks"
MANIFEST_PATH = BENCHMARKS / "manifest.json"
DATA_PATH = ROOT / "src" / "data" / "benchmarks.json"
PUBLIC_JSON_PATH = ROOT / "public" / "benchmarks" / "latest.json"
PUBLIC_CSV_PATH = ROOT / "public" / "benchmarks" / "latest.csv"
CACHE = ROOT / ".benchmark-cache"
RESULTS = ROOT / ".benchmark-results"
CANONICAL_EC2_INSTANCE_TYPE = "c9gd.8xlarge"

sys.path.insert(0, str(BENCHMARKS))
from lib import (  # noqa: E402
    IMPLEMENTATIONS,
    LOAD_CONCURRENCY,
    TRANSPORTS,
    ValidationError,
    implementations_from_manifest,
    validate_dataset,
    validate_manifest,
    validate_publishable_dataset,
)


def load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValidationError(f"{path} must contain a JSON object")
    return value


def save_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, sort_keys=False)
        handle.write("\n")
    temporary.replace(path)


def run(command: list[str], cwd: Path | None = None, capture: bool = False) -> str:
    print("+", " ".join(command), file=sys.stderr)
    completed = subprocess.run(
        command,
        cwd=cwd,
        check=True,
        text=True,
        stdout=subprocess.PIPE if capture else None,
    )
    return completed.stdout.strip() if capture else ""


def expand(command: list[str], repo: Path, artifacts: Path) -> list[str]:
    replacements = {"{repo}": str(repo), "{artifacts}": str(artifacts), "{root}": str(ROOT)}
    return [
        token.replace("{repo}", replacements["{repo}"])
        .replace("{artifacts}", replacements["{artifacts}"])
        .replace("{root}", replacements["{root}"])
        for token in command
    ]


def verify_local_release(implementation_id: str, implementation: dict[str, Any], repos_dir: Path) -> str:
    directory_names = {
        "python": "vgi-rpc-python",
        "typescript": "vgi-rpc-typescript",
        "go": "vgi-rpc-go",
        "rust": "vgi-rpc-rust",
        "java": "vgi-rpc-java",
        "cpp": "vgi-rpc-c++",
    }
    repo = repos_dir / directory_names[implementation_id]
    if not (repo / ".git").exists():
        raise ValidationError(f"{implementation_id}: local repository not found at {repo}")
    release = implementation["release"]
    ref = release if implementation["release_status"] == "published" else "HEAD"
    actual = run(["git", "-C", str(repo), "rev-list", "-n", "1", ref], capture=True)
    if actual != implementation["sha"]:
        raise ValidationError(
            f"{implementation_id}: {ref} resolves to {actual}, manifest pins {implementation['sha']}"
        )
    return actual


def verify_upstream_release(implementation_id: str, implementation: dict[str, Any]) -> None:
    remote = run(["git", "ls-remote", "--tags", "--heads", implementation["repository"]], capture=True)
    expected_ref = (
        f"refs/tags/{implementation['release']}"
        if implementation["release_status"] == "published"
        else "refs/heads/main"
    )
    matches = [line.split()[0] for line in remote.splitlines() if line.endswith(f"\t{expected_ref}")]
    dereferenced = [
        line.split()[0]
        for line in remote.splitlines()
        if line.endswith(f"\t{expected_ref}^{{}}")
    ]
    resolved = dereferenced[0] if dereferenced else (matches[0] if matches else "")
    if resolved != implementation["sha"]:
        raise ValidationError(
            f"{implementation_id}: upstream {expected_ref} resolves to {resolved or 'nothing'}, "
            f"manifest pins {implementation['sha']}"
        )
    if implementation["release_status"] == "published":
        releases = []
        for line in remote.splitlines():
            ref = line.split()[-1]
            match = re.fullmatch(r"refs/tags/(v(\d+)\.(\d+)\.(\d+))", ref)
            if match:
                releases.append(((int(match[2]), int(match[3]), int(match[4])), match[1]))
        if releases:
            latest = max(releases)[1]
            if implementation["release"] != latest:
                raise ValidationError(
                    f"{implementation_id}: manifest pins {implementation['release']}, "
                    f"but upstream latest release is {latest}"
                )


def prepare_checkout(implementation_id: str, implementation: dict[str, Any]) -> tuple[Path, Path]:
    sources = CACHE / "sources"
    repo = sources / implementation_id
    artifacts = CACHE / "artifacts" / implementation_id
    sources.mkdir(parents=True, exist_ok=True)
    artifacts.mkdir(parents=True, exist_ok=True)
    if not (repo / ".git").exists():
        run(["git", "clone", "--filter=blob:none", "--no-checkout", implementation["repository"], str(repo)])
    run(["git", "-C", str(repo), "fetch", "--tags", "origin", implementation["sha"]])
    run(["git", "-C", str(repo), "checkout", "--detach", "--force", implementation["sha"]])
    actual = run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture=True)
    if actual != implementation["sha"]:
        raise ValidationError(f"{implementation_id}: isolated checkout resolved to {actual}")
    return repo, artifacts


def build_implementation(implementation_id: str, implementation: dict[str, Any]) -> dict[str, Any]:
    repo, artifacts = prepare_checkout(implementation_id, implementation)
    if implementation_id == "cpp":
        vcpkg = repo / "vcpkg"
        if not (vcpkg / ".git").exists():
            run(["git", "clone", "https://github.com/microsoft/vcpkg.git", str(vcpkg)])
        run(["git", "-C", str(vcpkg), "checkout", "--force", implementation["toolchain"]["vcpkg"]])
        if not (vcpkg / "vcpkg").is_file():
            run([str(vcpkg / "bootstrap-vcpkg.sh"), "-disableMetrics"], cwd=repo)
    for command in implementation.get("build", []):
        run(expand(command, repo, artifacts), cwd=repo)
    commands = {
        transport: expand(command, repo, artifacts) if command else None
        for transport, command in implementation["commands"].items()
    }
    return {
        "id": implementation_id,
        "name": implementation["name"],
        "version": implementation["version"],
        "sha": implementation["sha"],
        "commands": commands,
    }


def host_metadata() -> dict[str, Any]:
    model = platform.machine()
    chip = platform.processor() or platform.machine()
    page_size = os.sysconf("SC_PAGE_SIZE")
    physical_pages = os.sysconf("SC_PHYS_PAGES")
    memory_gb = round(page_size * physical_pages / (1024 ** 3), 1)
    instance_type = ec2_instance_type() if sys.platform == "linux" else None
    if instance_type:
        model = f"Amazon EC2 {instance_type}"
        chip = "AWS Graviton5" if instance_type.startswith(("c9g", "m9g", "r9g")) else "AWS Graviton (ARM64)"
    if sys.platform == "darwin":
        try:
            profile = plistlib.loads(
                subprocess.check_output(["system_profiler", "SPHardwareDataType", "-xml"])
            )[0]["_items"][0]
            model = profile.get("machine_name", model)
            chip = profile.get("chip_type", chip)
            memory_text = str(profile.get("physical_memory", "0 GB")).split()[0]
            memory_gb = float(memory_text)
        except (KeyError, ValueError, subprocess.SubprocessError, plistlib.InvalidFileException):
            pass
    return {
        "model": model,
        "chip": chip,
        "cores": os.cpu_count() or 1,
        "memory_gb": memory_gb,
        "os": platform.platform(),
        "arch": platform.machine(),
        "instance_type": instance_type,
        "toolchains": toolchain_versions(),
    }


def ec2_instance_type() -> str | None:
    try:
        token_request = urllib.request.Request(
            "http://169.254.169.254/latest/api/token",
            method="PUT",
            headers={"X-aws-ec2-metadata-token-ttl-seconds": "60"},
        )
        with urllib.request.urlopen(token_request, timeout=0.2) as response:
            token = response.read().decode()
        metadata_request = urllib.request.Request(
            "http://169.254.169.254/latest/meta-data/instance-type",
            headers={"X-aws-ec2-metadata-token": token},
        )
        with urllib.request.urlopen(metadata_request, timeout=0.2) as response:
            return response.read().decode().strip()
    except (OSError, urllib.error.URLError):
        return None


def toolchain_versions() -> dict[str, str]:
    commands = {
        "python": [sys.executable, "--version"],
        "rust": ["rustc", "--version"],
        "go": ["go", "version"],
        "bun": ["bun", "--version"],
        "uv": ["uv", "--version"],
        "cmake": ["cmake", "--version"],
        "cxx": [os.environ.get("CXX", "c++"), "--version"],
        "java": ["java", "--version"],
    }
    versions: dict[str, str] = {}
    for name, command in commands.items():
        try:
            output = subprocess.run(command, check=True, capture_output=True, text=True).stdout.strip()
            versions[name] = output.splitlines()[0]
        except (OSError, subprocess.SubprocessError):
            versions[name] = "unavailable"
    return versions


def publish_preflight(*, require_quiet: bool = True) -> dict[str, Any]:
    machine = platform.machine()
    instance_type = ec2_instance_type()
    if sys.platform != "linux" or machine != "aarch64" or instance_type != CANONICAL_EC2_INSTANCE_TYPE:
        raise ValidationError(
            f"publishable benchmarks require the dedicated ARM64 EC2 {CANONICAL_EC2_INSTANCE_TYPE} runner"
        )
    load_1m = os.getloadavg()[0]
    if require_quiet and load_1m >= 2.0:
        raise ValidationError(f"one-minute load average is {load_1m:.2f}; wait until it is below 2.0")
    return {
        "ac_power": True,
        "low_power_mode": False,
        "runner": "dedicated_linux_arm64",
        "instance_type": instance_type,
        "cpu_count": os.cpu_count() or 1,
        "load_average_1m": load_1m,
    }


def wait_for_publish_preflight(timeout_seconds: int = 900) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            return publish_preflight()
        except ValidationError as error:
            if "load average" not in str(error) or time.monotonic() >= deadline:
                raise
            print(f"benchmark preflight: {error}; checking again in 15 seconds", file=sys.stderr)
            time.sleep(15)


def run_matrix(mode: str, selected: list[str] | None, suite: str = "all") -> Path:
    manifest = load_json(MANIFEST_PATH)
    validate_manifest(manifest)
    RESULTS.mkdir(parents=True, exist_ok=True)
    server_ids = selected or list(manifest["implementations"])
    fragments: list[dict[str, Any]] = []
    configs: dict[str, dict[str, Any]] = {}
    driver_manifest = BENCHMARKS / "driver" / "Cargo.toml"
    for implementation_id in server_ids:
        if implementation_id not in manifest["implementations"]:
            raise ValidationError(f"unknown implementation: {implementation_id}")
        config = build_implementation(implementation_id, manifest["implementations"][implementation_id])
        config_path = RESULTS / f"{implementation_id}-config.json"
        save_json(config_path, config)
        configs[implementation_id] = config
    run(["cargo", "build", "--locked", "--release", "--manifest-path", str(driver_manifest)], cwd=ROOT)
    driver_binary = driver_manifest.parent / "target" / "release" / "vgi-rpc-release-bench"
    preflight = wait_for_publish_preflight() if mode == "full" else None
    benchmark_order = list(configs)
    random.Random(1447316557).shuffle(benchmark_order)
    for implementation_id in benchmark_order:
        config_path = RESULTS / f"{implementation_id}-config.json"
        suffix = "" if suite == "all" else f"-{suite}"
        output_path = RESULTS / f"{implementation_id}-{mode}{suffix}.json"
        run([
            str(driver_binary), "--config", str(config_path), "--mode", mode,
            "--suite", suite, "--output", str(output_path),
        ], cwd=ROOT)
        fragments.append(load_json(output_path))
    dataset = {
        "schema_version": 1,
        "status": "candidate",
        "run_mode": mode,
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "host": host_metadata(),
        "preflight": preflight,
        "implementations": implementations_from_manifest(manifest),
        "results": [result for fragment in fragments for result in fragment.get("results", [])],
        "methodology": {
            "benchmark_suite": "combined" if suite == "all" else suite,
            "canonical_client": "rust",
            "latency_warmup_seconds": 0 if mode == "quick" else 1,
            "latency_round_seconds": 0 if mode == "quick" else 1,
            "latency_rounds": 1 if mode == "quick" else "5-7",
            "scaling_warmup_seconds": 0 if mode == "quick" else 1,
            "scaling_round_seconds": 0 if mode == "quick" else 2,
            "scaling_rounds": 1 if mode == "quick" else "3-5",
            "load_concurrency": list(LOAD_CONCURRENCY),
            "noise_threshold_cv": 0.05,
            "seed": 1447316557,
            "server_order": benchmark_order,
        },
    }
    validate_dataset(dataset, manifest)
    suffix = "" if suite == "all" else f"-{suite}"
    candidate = RESULTS / f"candidate-{mode}{suffix}.json"
    save_json(candidate, dataset)
    return candidate


def combine_full_candidates(latency_path: Path, scaling_path: Path) -> Path:
    manifest = load_json(MANIFEST_PATH)
    latency = load_json(latency_path)
    scaling = load_json(scaling_path)
    for name, dataset, expected_suite in (
        ("latency", latency, "latency"),
        ("scaling", scaling, "scaling"),
    ):
        validate_dataset(dataset, manifest)
        if dataset.get("status") != "candidate" or dataset.get("run_mode") != "full":
            raise ValidationError(f"{name} input must be a full candidate")
        if dataset.get("methodology", {}).get("benchmark_suite") != expected_suite:
            raise ValidationError(f"{name} input is not a {expected_suite} phase")
    host_fields = ("instance_type", "chip", "cores", "memory_gb", "os", "arch")
    if any(latency["host"].get(field) != scaling["host"].get(field) for field in host_fields):
        raise ValidationError("latency and scaling phases were not measured on the same host")
    if latency["implementations"] != scaling["implementations"]:
        raise ValidationError("latency and scaling phases do not use the same releases")

    combined = dict(latency)
    combined["generated_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    combined["results"] = latency["results"] + scaling["results"]
    combined["methodology"] = dict(latency["methodology"])
    combined["methodology"]["benchmark_suite"] = "combined"
    combined["phase_preflights"] = {
        "latency": latency.get("preflight"),
        "scaling": scaling.get("preflight"),
    }
    validate_publishable_dataset(combined, manifest)
    candidate = RESULTS / "candidate-full.json"
    save_json(candidate, combined)
    return candidate


def assemble_full_phase(suite: str) -> Path:
    """Assemble per-server full fragments after an interrupted/resumed phase."""
    manifest = load_json(MANIFEST_PATH)
    candidate = RESULTS / f"candidate-full-{suite}.json"
    dataset = load_json(candidate)
    if dataset.get("status") != "candidate" or dataset.get("run_mode") != "full":
        raise ValidationError("phase base must be a full candidate")
    if dataset.get("methodology", {}).get("benchmark_suite") != suite:
        raise ValidationError(f"phase base is not a {suite} candidate")

    results: list[dict[str, Any]] = []
    for implementation_id in manifest["implementations"]:
        fragment_path = RESULTS / f"{implementation_id}-full-{suite}.json"
        fragment = load_json(fragment_path)
        if (
            fragment.get("schema_version") != 1
            or fragment.get("server") != implementation_id
            or fragment.get("suite") != suite
        ):
            raise ValidationError(f"invalid {suite} fragment for {implementation_id}")
        fragment_results = fragment.get("results", [])
        if not fragment_results or any(row.get("server") != implementation_id for row in fragment_results):
            raise ValidationError(f"incomplete {suite} fragment for {implementation_id}")
        results.extend(fragment_results)

    server_order = list(manifest["implementations"])
    random.Random(1447316557).shuffle(server_order)
    dataset["implementations"] = implementations_from_manifest(manifest)
    dataset["results"] = results
    dataset["methodology"]["server_order"] = server_order
    dataset["host"]["toolchains"] = toolchain_versions()
    dataset["generated_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    validate_dataset(dataset, manifest)
    save_json(candidate, dataset)
    return candidate


def write_csv(path: Path, dataset: dict[str, Any]) -> None:
    fields = [
        "layer", "client", "server", "transport", "workload", "concurrency", "status",
        "sample_count", "mean_ns", "p50_ns", "p95_ns", "p99_ns", "operations_per_second",
        "payload_bytes_per_second", "cpu_utilization_percent", "round_cv",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".csv.tmp")
    with temporary.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=fields,
            extrasaction="ignore",
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(dataset.get("results", []))
    temporary.replace(path)


def command_validate() -> None:
    manifest = load_json(MANIFEST_PATH)
    validate_manifest(manifest)
    website = load_json(DATA_PATH)
    downloadable = load_json(PUBLIC_JSON_PATH)
    validate_dataset(website, manifest)
    validate_dataset(downloadable, manifest)
    if website != downloadable:
        raise ValidationError("website and downloadable latest datasets differ")
    print("benchmark manifest and latest dataset are valid")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("validate", help="validate the manifest and checked-in latest dataset")
    verify = subparsers.add_parser("verify", help="verify pinned releases against local and optional upstream refs")
    verify.add_argument("--repos-dir", type=Path, default=ROOT.parent)
    verify.add_argument("--network", action="store_true", help="also resolve official upstream refs")
    for name in ("quick", "full"):
        benchmark = subparsers.add_parser(name, help=f"build pinned sources and run the {name} matrix")
        benchmark.add_argument("--implementation", action="append", choices=list(IMPLEMENTATIONS))
        benchmark.add_argument("--suite", choices=("all", "latency", "scaling"), default="all")
    combine = subparsers.add_parser("combine", help="combine full latency and scaling phase candidates")
    combine.add_argument("--latency", type=Path, default=RESULTS / "candidate-full-latency.json")
    combine.add_argument("--scaling", type=Path, default=RESULTS / "candidate-full-scaling.json")
    assemble = subparsers.add_parser("assemble", help="assemble a resumed full phase from per-server fragments")
    assemble.add_argument("--suite", choices=("latency", "scaling"), required=True)
    publish = subparsers.add_parser("publish", help="replace the website's latest dataset with a full candidate")
    publish.add_argument("--candidate", type=Path, default=RESULTS / "candidate-full.json")
    args = parser.parse_args()

    try:
        if args.command == "validate":
            command_validate()
        elif args.command == "verify":
            manifest = load_json(MANIFEST_PATH)
            validate_manifest(manifest)
            for implementation_id, implementation in manifest["implementations"].items():
                verify_local_release(implementation_id, implementation, args.repos_dir)
                if args.network:
                    verify_upstream_release(implementation_id, implementation)
                print(f"{implementation_id}: {implementation['release']} -> {implementation['sha']}")
        elif args.command in ("quick", "full"):
            candidate = run_matrix(args.command, args.implementation, args.suite)
            print(candidate)
        elif args.command == "combine":
            print(combine_full_candidates(args.latency, args.scaling))
        elif args.command == "assemble":
            print(assemble_full_phase(args.suite))
        elif args.command == "publish":
            manifest = load_json(MANIFEST_PATH)
            candidate = load_json(args.candidate)
            validate_manifest(manifest)
            validate_publishable_dataset(candidate, manifest)
            candidate["status"] = "published"
            save_json(DATA_PATH, candidate)
            save_json(PUBLIC_JSON_PATH, candidate)
            write_csv(PUBLIC_CSV_PATH, candidate)
            print(f"published latest snapshot to {DATA_PATH} and {PUBLIC_JSON_PATH}")
    except (ValidationError, subprocess.CalledProcessError, OSError) as error:
        print(f"benchmark error: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("benchmark interrupted before publication", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
