"""Measure the full C and Rust VGI example workers, separately from RPC workers."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import platform
import random
import shutil
import statistics
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from footprint import (
    ROOT,
    inventory,
    native_libraries,
    output,
    save,
    tree_rss,
    validate,
    validate_inventories,
)

MANIFEST = ROOT / "benchmarks/sdk-footprint-manifest.json"
PUBLIC = ROOT / "public/benchmarks/sdk"
DATA = ROOT / "src/data/sdk-footprint.json"


def verify_source(path, sha):
    if output(["git", "-C", path, "rev-parse", "HEAD"]) != sha:
        raise ValueError(f"Wrong source revision: {path}")
    if output(["git", "-C", path, "status", "--porcelain", "--untracked-files=no"]):
        raise ValueError(f"Modified source checkout: {path}")


def check(data, manifest, directory, *, public=False):
    validate(data, manifest)
    validate_inventories(data, directory, public=public)
    if data["sources"] != manifest["sources"]:
        raise ValueError("SDK dependency or driver source mismatch")
    if data["rust_lock_sha256"] != manifest["rust_lock_sha256"]:
        raise ValueError("Rust dependency lock mismatch")
    recorded_lock = ROOT / "benchmarks/sdk-rust.Cargo.lock"
    if (
        hashlib.sha256(recorded_lock.read_bytes()).hexdigest()
        != manifest["rust_lock_sha256"]
    ):
        raise ValueError("Recorded Rust dependency lock changed")


def measure_round(binary, workload):
    import psutil
    import pyarrow as pa
    from vgi import schema
    from vgi.arguments import Arguments
    from vgi.client import Client

    payload = "0123456789abcdef" * (workload["payload_bytes"] // 16)
    scalar = pa.RecordBatch.from_pydict({"x": [2]}, schema=schema(x=pa.int64()))
    echo = pa.RecordBatch.from_pydict({"x": [payload]}, schema=schema(x=pa.string()))

    def call(client, name, batch, expected):
        batches = list(
            client.scalar_function(
                function_name=name,
                schema_path=["main"],
                input=iter([batch]),
                arguments=Arguments(),
            )
        )
        if (
            len(batches) != 1
            or batches[0].num_columns != 1
            or batches[0].column(0).to_pylist() != [expected]
        ):
            raise ValueError(f"Incorrect {name} result")

    # Direct transport creates one fresh worker and bypasses the client's pool.
    with Client(str(binary), pool=None, worker_limit=1) as client:
        process = client._primary.proc
        deadline = threading.Timer(120, process.kill)
        deadline.daemon = True
        deadline.start()
        stop = threading.Event()
        thread = None
        samples, errors = [], []
        try:
            for _ in range(workload["warmup_calls"]):
                call(client, "double", scalar, 4)
            idle = []
            end = time.monotonic() + workload["idle_seconds"]
            while time.monotonic() < end:
                rss, count = tree_rss(process.pid)
                if count != 1:
                    raise ValueError("Expected exactly one worker process")
                idle.append(rss)
                time.sleep(workload["sample_interval_seconds"])
            start = time.monotonic()

            def sample():
                rss, count = tree_rss(process.pid)
                samples.append(
                    {
                        "seconds": time.monotonic() - start,
                        "rss_bytes": rss,
                        "processes": count,
                    }
                )

            def sampler():
                try:
                    while not stop.wait(workload["sample_interval_seconds"]):
                        sample()
                except (OSError, ValueError, psutil.Error) as error:
                    errors.append(str(error))

            sample()
            thread = threading.Thread(target=sampler, daemon=True)
            thread.start()
            for _ in range(workload["payload_calls"]):
                call(client, "passthru", echo, payload)
            sample()
            stop.set()
            thread.join(timeout=5)
            if errors or thread.is_alive():
                raise ValueError(f"Memory sampler failed: {errors}")
            return {
                "idle_rss_bytes": statistics.median(idle),
                "peak_rss_bytes": max(s["rss_bytes"] for s in samples),
                "idle_samples": idle,
                "payload_samples": sorted(samples, key=lambda s: s["seconds"]),
                "validated_scalar_calls": workload["warmup_calls"],
                "validated_payload_calls": workload["payload_calls"],
            }
        finally:
            deadline.cancel()
            stop.set()
            if thread:
                thread.join(timeout=5)


def measure(workspace, manifest):
    import psutil
    import pyarrow

    if sys.platform != "darwin" or platform.machine() != "arm64":
        raise ValueError("This recipe requires macOS ARM64")
    for key, source in manifest["sources"].items():
        verify_source(workspace / key, source["sha"])
    for key, spec in manifest["implementations"].items():
        verify_source(workspace / spec["directory"], spec["sha"])
    lock_hash = hashlib.sha256((workspace / "rust/Cargo.lock").read_bytes()).hexdigest()
    if lock_hash != manifest["rust_lock_sha256"]:
        raise ValueError("Use the recorded Rust Cargo.lock")
    # Import committed clients ahead of editable packages in a developer's venv.
    sys.path[:0] = [str(workspace / "python"), str(workspace / "rpc-python")]
    for name in list(os.environ):
        if name.startswith(("VGI_", "OTEL_", "RUST_LOG")):
            del os.environ[name]
    data = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "host": {
            "os": platform.platform(),
            "arch": platform.machine(),
            "chip": output(["sysctl", "-n", "machdep.cpu.brand_string"]),
            "cores": os.cpu_count(),
            "memory_bytes": psutil.virtual_memory().total,
            "toolchains": {
                name: output(command).splitlines()[0]
                for name, command in {
                    "python": [sys.executable, "--version"],
                    "rust": ["rustc", "--version"],
                    "clang": ["clang", "--version"],
                    "cmake": ["cmake", "--version"],
                }.items()
            },
            "driver": {"pyarrow": pyarrow.__version__, "psutil": psutil.__version__},
        },
        "sources": manifest["sources"],
        "rust_lock_sha256": lock_hash,
        "workload": manifest["workload"],
        "results": [],
    }
    results = workspace / "results"
    binaries = {}
    for key, spec in manifest["implementations"].items():
        binary = workspace / spec["directory"] / spec["binary"]
        binaries[key] = binary
        parts = {
            "artifact": {binary},
            "app": {binary} | native_libraries([binary]),
            "runtime": set(),
        }
        inv = {part: inventory(files) for part, files in parts.items()}
        save(results / f"{key}-inventory.json", inv)
        row = {
            "id": key,
            **spec,
            "status": "complete",
            "artifact_kind": "stripped native executable",
            "runtime_label": "included in executable / OS",
            **{part + "_bytes": item["bytes"] for part, item in inv.items()},
            "inventory_hashes": {part: item["sha256"] for part, item in inv.items()},
            "rounds": [],
        }
        row["total_bytes"] = row["app_bytes"] + row["runtime_bytes"]
        data["results"].append(row)
    rng = random.Random(20261019)
    for index in range(manifest["workload"]["rounds"]):
        order = data["results"].copy()
        rng.shuffle(order)
        for row in order:
            print(f"Round {index + 1}: {row['name']}", flush=True)
            row["rounds"].append(
                measure_round(binaries[row["id"]], manifest["workload"])
            )
            save(results / "candidate.json", data)
    for row in data["results"]:
        for metric in ("idle_rss_bytes", "peak_rss_bytes"):
            values = [r[metric] for r in row["rounds"]]
            row[metric] = statistics.median(values)
            row[metric + "_min"], row[metric + "_max"] = min(values), max(values)
    check(data, manifest, results)
    save(results / "candidate.json", data)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["measure", "publish", "validate"])
    parser.add_argument("--workspace", type=Path, default=ROOT / ".footprint-cache/sdk")
    args = parser.parse_args()
    manifest = json.loads(MANIFEST.read_text())
    if args.action == "measure":
        measure(args.workspace.resolve(), manifest)
    elif args.action == "publish":
        directory = args.workspace / "results"
        data = json.loads((directory / "candidate.json").read_text())
        check(data, manifest, directory)
        save(DATA, data)
        save(PUBLIC / "footprint.json", data)
        for key in manifest["implementations"]:
            shutil.copyfile(
                directory / f"{key}-inventory.json",
                PUBLIC / f"footprint-{key}-files.json",
            )
        with (PUBLIC / "footprint.csv").open("w") as handle:
            fields = [
                "id",
                "version",
                "sha",
                "artifact_bytes",
                "app_bytes",
                "idle_rss_bytes",
                "peak_rss_bytes",
            ]
            writer = csv.DictWriter(
                handle, fields, extrasaction="ignore", lineterminator="\n"
            )
            writer.writeheader()
            writer.writerows(data["results"])
    else:
        data = json.loads(DATA.read_text())
        check(data, manifest, PUBLIC, public=True)
        if data != json.loads((PUBLIC / "footprint.json").read_text()):
            raise ValueError("Public SDK data differs from website data")
        print("VGI SDK footprint validated")


if __name__ == "__main__":
    main()
