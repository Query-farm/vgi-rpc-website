#!/usr/bin/env python3
"""Measure pinned example workers; keep footprint results separate from latency."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import platform
import random
import re
import shutil
import signal
import statistics
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "benchmarks/footprint-manifest.json"
CACHE = ROOT / ".footprint-cache"
RESULTS = ROOT / ".footprint-results"
VCPKG_SHA = "05442024c3fda64320bd25d2251cc9807b84fb6f"


def output(command, **kwargs):
    return subprocess.check_output(
        [str(x) for x in command], text=True, **kwargs
    ).strip()


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")


def build_commands(key):
    return {
        "c": [
            [
                "cmake",
                "-S",
                ".",
                "-B",
                "build",
                "-DCMAKE_BUILD_TYPE=Release",
                "-DBUILD_SHARED_LIBS=OFF",
            ],
            [
                "cmake",
                "--build",
                "build",
                "--parallel",
                "3",
                "--target",
                "conformance_worker",
            ],
        ],
        "cpp": [
            ["cmake", "--preset", "release", "-DVGI_RPC_BUILD_TESTS=ON"],
            [
                "cmake",
                "--build",
                "build",
                "--parallel",
                "3",
                "--target",
                "conformance_worker",
            ],
        ],
        "rust": [
            [
                "cargo",
                "build",
                "--locked",
                "--release",
                "-j",
                "3",
                "-p",
                "vgi-rpc-conformance-rust",
                "--config",
                'profile.release.strip="symbols"',
            ]
        ],
        "go": [
            [
                "go",
                "build",
                "-p",
                "3",
                "-trimpath",
                "-ldflags=-s -w",
                "-o",
                "conformance-worker",
                "./conformance/cmd/vgi-rpc-conformance-go",
            ]
        ],
        "python": [
            [
                "uv",
                "sync",
                "--frozen",
                "--no-dev",
                "--extra",
                "http",
                "--extra",
                "conformance",
            ]
        ],
        "typescript": [
            ["bun", "install", "--production", "--omit", "peer", "--frozen-lockfile"]
        ],
        "java": [
            [
                "./gradlew",
                "--no-daemon",
                "--max-workers=2",
                ":conformance-worker:installDist",
            ]
        ],
        "csharp": [
            [
                "dotnet",
                "publish",
                "conformance/QueryFarm.VgiRpc.ConformanceWorker/QueryFarm.VgiRpc.ConformanceWorker.csproj",
                "-c",
                "Release",
                "--no-self-contained",
                "-p:DebugType=None",
                "-p:DebugSymbols=false",
                "-o",
                "publish",
            ]
        ],
    }[key]


def build(key, spec, repos_dir):
    repo = CACHE / "sources" / key
    if not repo.exists():
        local = repos_dir / spec["local_directory"]
        source = str(local) if local.exists() else spec["repository"]
        subprocess.run(["git", "clone", "--no-checkout", source, str(repo)], check=True)
        subprocess.run(
            ["git", "-C", str(repo), "checkout", "--detach", spec["sha"]], check=True
        )
    verify_source(repo, spec)
    if key == "cpp":
        vcpkg = repo / "vcpkg"
        if not vcpkg.exists():
            subprocess.run(
                ["git", "clone", "https://github.com/microsoft/vcpkg.git", str(vcpkg)],
                check=True,
            )
            subprocess.run(["git", "-C", str(vcpkg), "checkout", VCPKG_SHA], check=True)
        if output(["git", "-C", vcpkg, "rev-parse", "HEAD"]) != VCPKG_SHA:
            raise ValueError("Unexpected vcpkg revision")
        if not (vcpkg / "vcpkg").exists():
            subprocess.run(
                [str(vcpkg / "bootstrap-vcpkg.sh"), "-disableMetrics"], check=True
            )
    if key == "typescript" and (repo / "node_modules").exists():
        # Bun does not remove already-installed optional peer packages with --omit.
        shutil.rmtree(repo / "node_modules")
    RESULTS.mkdir(parents=True, exist_ok=True)
    with (RESULTS / f"{key}-build.log").open("w") as log:
        for command in build_commands(key):
            print(key, "build:", " ".join(command), flush=True)
            subprocess.run(
                command, cwd=repo, check=True, stdout=log, stderr=subprocess.STDOUT
            )
    # Strip the measured native artifact, never a developer's working binary.
    if key in ("c", "cpp"):
        executable = repo / "build/conformance/conformance_worker"
        subprocess.run(["strip", "-S", "-x", str(executable)], check=True)
    save(
        RESULTS / f"{key}-build.json",
        {"sha": spec["sha"], "commands": build_commands(key)},
    )


def verify_source(repo, spec):
    if output(["git", "-C", repo, "rev-parse", "HEAD"]) != spec["sha"]:
        raise ValueError(f"{repo}: source revision differs from the manifest")
    subprocess.run(
        ["git", "-C", str(repo), "diff", "--exit-code", "HEAD"],
        check=True,
        stdout=subprocess.DEVNULL,
    )


def collect_files(roots, excluded=()):
    """Count regular file bytes once, resolving symlinks and hard links."""
    files = {}
    for root in roots:
        root = Path(root)
        if not root.exists():
            raise ValueError(f"Missing artifact: {root}")
        paths = root.rglob("*") if root.is_dir() else [root]
        for path in paths:
            if not path.is_file() or any(part in excluded for part in path.parts):
                continue
            resolved = path.resolve()
            st = resolved.stat()
            files.setdefault((st.st_dev, st.st_ino), resolved)
    return set(files.values())


def native_libraries(executables):
    """Mach-O load commands, recursively; the macOS shared cache is excluded."""
    pending = list(executables)
    visited, libraries = set(), set()
    while pending:
        path = Path(pending.pop()).resolve()
        if path in visited:
            continue
        visited.add(path)
        result = subprocess.run(
            ["otool", "-L", str(path)], capture_output=True, text=True, check=False
        )
        if result.returncode:
            raise ValueError(f"Cannot inspect native dependencies of {path}")
        for line in result.stdout.splitlines()[1:]:
            name = line.strip().split(" (", 1)[0]
            if name.startswith(("/usr/lib/", "/System/Library/")):
                continue
            if name.startswith("@loader_path/"):
                dep = path.parent / name.removeprefix("@loader_path/")
            elif name.startswith("/"):
                dep = Path(name)
            else:
                raise ValueError(f"Unresolved dependency {name} in {path}")
            if not dep.exists():
                raise ValueError(f"Missing native dependency {dep}")
            dep = dep.resolve()
            if dep != path:
                libraries.add(dep)
                pending.append(dep)
    return libraries


def worker_layout(key, spec):
    repo = CACHE / "sources" / key
    env = os.environ.copy()
    excluded = ("__pycache__", ".DS_Store")
    runtime, runtime_label = set(), "Included / system libraries"
    if key in ("c", "cpp", "rust", "go"):
        relative = {
            "c": "build/conformance/conformance_worker",
            "cpp": "build/conformance/conformance_worker",
            "rust": "target/release/vgi-rpc-conformance-rust",
            "go": "conformance-worker",
        }[key]
        executable = repo / relative
        command, artifact = [str(executable)], collect_files([executable])
        app = artifact | native_libraries(artifact)
        kind = "Native executable"
    elif key == "python":
        executable = repo / ".venv/bin/python"
        command = [
            str(executable),
            "-c",
            "from vgi_rpc.conformance._cli import main; main()",
        ]
        info = json.loads(
            output(
                [
                    executable,
                    "-c",
                    (
                        "import json,sys,sysconfig; print(json.dumps({"
                        "'base':sys.base_prefix,'packages':sysconfig.get_path('purelib'),"
                        "'version':sys.version.split()[0]}))"
                    ),
                ]
            )
        )
        artifact = collect_files([repo / "vgi_rpc"], excluded)
        app = artifact | collect_files([info["packages"]], excluded)
        base = Path(info["base"])
        runtime_roots = [
            executable.resolve(),
            base / "lib" / ("python" + ".".join(info["version"].split(".")[:2])),
        ]
        if (base / "Python").exists():
            runtime_roots.append(base / "Python")
        runtime = collect_files(
            runtime_roots, excluded + ("site-packages",)
        ) | native_libraries([executable])
        runtime_label, kind = "CPython " + info["version"], "Python package source"
    elif key == "typescript":
        executable = Path(shutil.which("bun"))
        command = [str(executable), "run", str(repo / "examples/conformance.ts")]
        # Retain the complete example source directory to cover its relative imports.
        artifact = collect_files(
            [repo / "src", repo / "examples", repo / "package.json"], excluded
        )
        app = artifact | collect_files([repo / "node_modules"], excluded)
        runtime = collect_files([executable]) | native_libraries([executable])
        runtime_label, kind = (
            "Bun " + output([executable, "--version"]),
            "TypeScript source",
        )
    elif key == "java":
        dist = repo / "conformance-worker/build/install/conformance-worker"
        command = [str(dist / "bin/conformance-worker")]
        artifact = collect_files(
            [dist / "lib" / ("conformance-worker-" + spec["version"] + ".jar")]
        )
        app = collect_files([dist], excluded)
        settings = subprocess.run(
            ["java", "-XshowSettings:properties", "-version"],
            capture_output=True,
            text=True,
            check=True,
        )
        java_home = Path(re.search(r"java.home = (.+)", settings.stderr)[1].strip())
        env["JAVA_HOME"] = str(java_home)
        runtime = collect_files(
            [java_home / "bin/java", java_home / "lib", java_home / "conf"], excluded
        )
        runtime_label = re.search(r"java.version = (.+)", settings.stderr)[1].strip()
        runtime_label, kind = "OpenJDK " + runtime_label, "Worker JAR"
    else:
        dist = repo / "publish"
        executable = Path(shutil.which("dotnet")).resolve()
        command = [
            str(executable),
            str(dist / "QueryFarm.VgiRpc.ConformanceWorker.dll"),
        ]
        artifact = collect_files([dist / "QueryFarm.VgiRpc.ConformanceWorker.dll"])
        app = collect_files([dist], excluded)
        frameworks = json.loads(
            (dist / "QueryFarm.VgiRpc.ConformanceWorker.runtimeconfig.json").read_text()
        )["runtimeOptions"]["frameworks"]
        installed = [
            line.split()
            for line in output([executable, "--list-runtimes"]).splitlines()
        ]
        roots = [executable]
        versions = []
        for framework in frameworks:
            matches = [
                r
                for r in installed
                if r[0] == framework["name"]
                and r[1].split(".")[:2] == framework["version"].split(".")[:2]
            ]
            selected = max(matches, key=lambda r: tuple(map(int, r[1].split("."))))
            roots.append(Path(selected[2].strip("[]")) / selected[1])
            versions.append(selected[1])
        roots.append(executable.parent / "host/fxr" / versions[0])
        runtime = collect_files(roots, excluded)
        runtime_label, kind = ".NET + ASP.NET " + versions[0], "Worker assembly"
    runtime -= app
    return {
        "command": command,
        "cwd": repo,
        "env": env,
        "artifact": artifact,
        "app": app,
        "runtime": runtime,
        "runtime_label": runtime_label,
        "artifact_kind": kind,
    }


def inventory(files):
    rows = []
    for path in sorted(files):
        # Retain provenance without publishing local user names.
        name = (
            str(path).replace(str(ROOT), "{root}").replace(str(Path.home()), "{home}")
        )
        rows.append(
            {
                "path": name,
                "bytes": path.stat().st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    return {
        "bytes": sum(row["bytes"] for row in rows),
        "files": len(rows),
        "sha256": hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest(),
        "inventory": rows,
    }


def encode_request(method, payload=None):
    import pyarrow as pa

    if payload is None:
        schema = pa.schema(
            [pa.field("a", pa.float64(), False), pa.field("b", pa.float64(), False)]
        )
        values = [pa.array([2.0]), pa.array([3.0])]
    else:
        schema = pa.schema([pa.field("value", pa.large_binary(), False)])
        values = [pa.array([payload], type=pa.large_binary())]
    sink = pa.BufferOutputStream()
    metadata = {
        b"vgi_rpc.method": method.encode(),
        b"vgi_rpc.request_version": b"1",
        b"vgi_rpc.protocol": b"ConformanceService",
        b"vgi_rpc.protocol_version": b"2.0.0",
    }
    with pa.ipc.new_stream(sink, schema) as writer:
        writer.write_batch(
            pa.RecordBatch.from_arrays(values, schema=schema), custom_metadata=metadata
        )
    return sink.getvalue().to_pybytes()


def call(process, request, expected):
    import pyarrow as pa

    process.stdin.write(request)
    process.stdin.flush()
    with pa.ipc.open_stream(process.stdout) as reader:
        batches = list(reader)
    if len(batches) != 1 or batches[0].num_rows != 1 or batches[0].num_columns != 1:
        raise ValueError("Worker did not return exactly one result row")
    value = batches[0].column(0)[0].as_py()
    if type(value) is not type(expected) or value != expected:
        raise ValueError("Worker returned an incorrect result")


def tree_rss(pid):
    import psutil

    process = psutil.Process(pid)
    members = [process] + process.children(recursive=True)
    sizes = []
    for member in members:
        try:
            sizes.append(member.memory_info().rss)
        except psutil.NoSuchProcess:
            pass
    if not sizes or sum(sizes) == 0:
        raise ValueError("No live worker memory was observed")
    return sum(sizes), len(sizes)


def terminate(process):
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except PermissionError:
        if process.poll() is None:
            process.kill()
    process.wait(timeout=10)
    process.stdin.close()
    process.stdout.close()


def measure_round(layout, workload, log_path):
    import psutil

    payload = (bytes(range(256)) * ((workload["payload_bytes"] + 255) // 256))[
        : workload["payload_bytes"]
    ]
    scalar = encode_request("add_floats")
    echo = encode_request("echo_large_binary", payload)
    with log_path.open("wb") as stderr:
        process = subprocess.Popen(
            layout["command"],
            cwd=layout["cwd"],
            env=layout["env"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr,
            start_new_session=True,
        )
        # A stalled worker cannot hang the complete benchmark or leak children.
        deadline = threading.Timer(120, lambda: terminate(process))
        deadline.daemon = True
        deadline.start()
        stop = threading.Event()
        samples, sample_errors = [], []
        start = time.monotonic()

        def sample():
            while not stop.is_set():
                try:
                    rss, count = tree_rss(process.pid)
                    samples.append(
                        {
                            "seconds": time.monotonic() - start,
                            "rss_bytes": rss,
                            "processes": count,
                        }
                    )
                except (psutil.Error, ValueError, OSError) as error:
                    sample_errors.append(str(error))
                    break
                stop.wait(workload["sample_interval_seconds"])

        thread = None
        try:
            for _ in range(workload["warmup_calls"]):
                call(process, scalar, 5.0)
            idle = []
            until = time.monotonic() + workload["idle_seconds"]
            while time.monotonic() < until:
                idle.append(tree_rss(process.pid)[0])
                time.sleep(workload["sample_interval_seconds"])
            start = time.monotonic()
            thread = threading.Thread(target=sample, daemon=True)
            thread.start()
            for _ in range(workload["payload_calls"]):
                call(process, echo, payload)
            # Include the final state even when a fast workload fits between samples.
            rss, count = tree_rss(process.pid)
            samples.append(
                {
                    "seconds": time.monotonic() - start,
                    "rss_bytes": rss,
                    "processes": count,
                }
            )
            stop.set()
            thread.join()
            if sample_errors or len(samples) < 2:
                raise ValueError(f"Memory sampling failed: {sample_errors}")
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
            terminate(process)


def validate(data, manifest):
    expected = manifest["implementations"]
    rows = data.get("results", [])
    if data.get("schema_version") != 1 or data.get("workload") != manifest["workload"]:
        raise ValueError("Footprint dataset contract mismatch")
    if data.get("host", {}).get("arch") != "arm64" or not data.get("host", {}).get(
        "os", ""
    ).startswith("macOS"):
        raise ValueError("Footprint snapshot requires its declared macOS ARM64 host")
    if len(rows) != len(expected) or {r["id"] for r in rows} != set(expected):
        raise ValueError("Publication requires every implementation exactly once")
    for row in rows:
        if row["sha"] != expected[row["id"]]["sha"] or row.get("status") != "complete":
            raise ValueError("Unverified implementation revision or failed measurement")
        if len(row["rounds"]) != manifest["workload"]["rounds"]:
            raise ValueError("Incomplete measurement rounds")
        for round_ in row["rounds"]:
            if (
                round_["validated_scalar_calls"] != manifest["workload"]["warmup_calls"]
                or round_["validated_payload_calls"]
                != manifest["workload"]["payload_calls"]
            ):
                raise ValueError("Incomplete correctness checks")
            if not round_["idle_samples"] or len(round_["payload_samples"]) < 2:
                raise ValueError("Missing memory observations")
            if any(v <= 0 for v in round_["idle_samples"]) or any(
                s["rss_bytes"] <= 0 or s["processes"] < 1
                for s in round_["payload_samples"]
            ):
                raise ValueError("Invalid resident memory observation")
            if round_["idle_rss_bytes"] != statistics.median(round_["idle_samples"]):
                raise ValueError("Idle RSS does not match observations")
            if round_["peak_rss_bytes"] != max(
                s["rss_bytes"] for s in round_["payload_samples"]
            ):
                raise ValueError("Peak RSS does not match observations")
        for metric in ("idle_rss_bytes", "peak_rss_bytes"):
            values = [r[metric] for r in row["rounds"]]
            if (
                row[metric] != statistics.median(values)
                or row[metric + "_min"] != min(values)
                or row[metric + "_max"] != max(values)
            ):
                raise ValueError("Summary does not match observations")
        for metric in (
            "artifact_bytes",
            "app_bytes",
            "total_bytes",
            "idle_rss_bytes",
            "peak_rss_bytes",
        ):
            if row[metric] <= 0:
                raise ValueError(f"Invalid {metric}")
        if (
            row["total_bytes"] != row["app_bytes"] + row["runtime_bytes"]
            or row["artifact_bytes"] > row["app_bytes"]
        ):
            raise ValueError("Inconsistent disk inventory")


def validate_inventories(data, directory, *, public=False):
    for row in data["results"]:
        filename = (
            f"footprint-{row['id']}-files.json"
            if public
            else f"{row['id']}-inventory.json"
        )
        inventories = json.loads((directory / filename).read_text())
        for part in ("artifact", "app", "runtime"):
            item = inventories[part]
            files = item["inventory"]
            digest = hashlib.sha256(
                json.dumps(files, sort_keys=True).encode()
            ).hexdigest()
            if (
                item["files"] != len(files)
                or item["bytes"] != sum(f["bytes"] for f in files)
                or item["bytes"] != row[part + "_bytes"]
                or item["sha256"] != digest
                or digest != row["inventory_hashes"][part]
            ):
                raise ValueError(
                    f"{row['id']}: file inventory does not match published sizes or hashes"
                )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["build", "measure", "publish", "validate"])
    parser.add_argument("--only", nargs="+")
    parser.add_argument("--repos-dir", type=Path, default=ROOT.parent)
    args = parser.parse_args()
    manifest = json.loads(MANIFEST.read_text())
    selected = args.only or list(manifest["implementations"])
    if set(selected) - set(manifest["implementations"]):
        parser.error("Unknown implementation")
    if args.action == "build":
        for key in selected:
            build(key, manifest["implementations"][key], args.repos_dir)
        return
    if args.action == "measure":
        if sys.platform != "darwin" or platform.machine() != "arm64":
            parser.error(
                "This footprint recipe currently measures macOS ARM64; do not mix hosts"
            )
        import psutil
        import pyarrow

        RESULTS.mkdir(parents=True, exist_ok=True)
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
                        "clang": ["clang", "--version"],
                        "rust": ["rustc", "--version"],
                        "go": ["go", "version"],
                        "bun": ["bun", "--version"],
                        "dotnet": ["dotnet", "--version"],
                        "cmake": ["cmake", "--version"],
                    }.items()
                },
                "driver": {
                    "pyarrow": pyarrow.__version__,
                    "psutil": psutil.__version__,
                },
            },
            "workload": manifest["workload"],
            "results": [],
        }
        layouts = {}
        for key in selected:
            spec = manifest["implementations"][key]
            verify_source(CACHE / "sources" / key, spec)
            stamp = json.loads((RESULTS / f"{key}-build.json").read_text())
            if stamp != {"sha": spec["sha"], "commands": build_commands(key)}:
                raise ValueError("Rebuild after changing the source or build recipe")
            layouts[key] = worker_layout(key, spec)
            layout = layouts[key]
            inventories = {
                part: inventory(layout[part]) for part in ("artifact", "app", "runtime")
            }
            save(RESULTS / f"{key}-inventory.json", inventories)
            row = {
                "id": key,
                **spec,
                "status": "complete",
                "artifact_kind": layout["artifact_kind"],
                "runtime_label": layout["runtime_label"],
                "build_commands": build_commands(key),
                "post_build_commands": [
                    ["strip", "-S", "-x", "build/conformance/conformance_worker"]
                ]
                if key in ("c", "cpp")
                else [],
                **{part + "_bytes": inv["bytes"] for part, inv in inventories.items()},
                "inventory_hashes": {
                    part: inv["sha256"] for part, inv in inventories.items()
                },
                "rounds": [],
            }
            row["total_bytes"] = row["app_bytes"] + row["runtime_bytes"]
            data["results"].append(row)
        by_id = {r["id"]: r for r in data["results"]}
        rng = random.Random(20261019)
        for round_index in range(manifest["workload"]["rounds"]):
            order = list(selected)
            rng.shuffle(order)
            for key in order:
                print(f"Round {round_index + 1}: {key}", flush=True)
                measured = measure_round(
                    layouts[key],
                    manifest["workload"],
                    RESULTS / f"{key}-{round_index}.stderr",
                )
                by_id[key]["rounds"].append(measured)
                save(RESULTS / "candidate.json", data)
        for row in data["results"]:
            for metric in ("idle_rss_bytes", "peak_rss_bytes"):
                values = [r[metric] for r in row["rounds"]]
                row[metric], row[metric + "_min"], row[metric + "_max"] = (
                    statistics.median(values),
                    min(values),
                    max(values),
                )
        save(RESULTS / "candidate.json", data)
        if not args.only:
            validate(data, manifest)
    elif args.action == "publish":
        data = json.loads((RESULTS / "candidate.json").read_text())
        validate(data, manifest)
        validate_inventories(data, RESULTS)
        save(ROOT / "src/data/footprint.json", data)
        save(ROOT / "public/benchmarks/footprint.json", data)
        for key in manifest["implementations"]:
            shutil.copyfile(
                RESULTS / f"{key}-inventory.json",
                ROOT / "public/benchmarks" / f"footprint-{key}-files.json",
            )
        fields = [
            "id",
            "version",
            "sha",
            "artifact_kind",
            "artifact_bytes",
            "app_bytes",
            "runtime_bytes",
            "total_bytes",
            "idle_rss_bytes",
            "peak_rss_bytes",
        ]
        with (ROOT / "public/benchmarks/footprint.csv").open("w") as handle:
            writer = csv.DictWriter(handle, fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(data["results"])
    else:
        data = json.loads((ROOT / "src/data/footprint.json").read_text())
        validate(data, manifest)
        validate_inventories(data, ROOT / "public/benchmarks", public=True)
        print("Footprint dataset validated")


if __name__ == "__main__":
    main()
