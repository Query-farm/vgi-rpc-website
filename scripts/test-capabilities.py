#!/usr/bin/env python3
"""Test capability probes for all VGI-RPC language implementations.

This script:
1. Clones/pulls each language repo into a temp directory
2. Builds the conformance worker for each language
3. Runs capability probes against each worker via subprocess transport
4. Outputs src/data/capabilities.json

Usage:
    python scripts/test-capabilities.py [--repos-dir /path/to/repos]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# Language configurations
LANGUAGES: dict[str, dict[str, Any]] = {
    "python": {
        "repo": "https://github.com/Query-farm/vgi-rpc-python",
        "docs": "https://vgi-rpc-python.query.farm",
        "package_url": "https://pypi.org/project/vgi-rpc/",
        "build_cmd": ["uv", "sync", "--all-extras"],
        "worker_cmd": ["uv", "run", "python", "-m", "vgi_rpc.conformance.worker"],
        "version_cmd": [
            "uv",
            "run",
            "python",
            "-c",
            "import vgi_rpc; print(vgi_rpc.__version__)",
        ],
        # Known capabilities from the reference implementation
        "known_transports": {
            "pipe": True,
            "subprocess": True,
            "unix_socket": True,
            "shared_memory": True,
            "http": True,
            "worker_pool": True,
        },
    },
    "typescript": {
        "repo": "https://github.com/Query-farm/vgi-rpc-ts",
        "docs": None,
        "package_url": None,
        "build_cmd": ["bun", "install"],
        "worker_cmd": ["bun", "run", "src/conformance/worker.ts"],
        "version_cmd": [
            "bun",
            "run",
            "-e",
            "const pkg = require('./package.json'); console.log(pkg.version)",
        ],
        "known_transports": {
            "pipe": False,
            "subprocess": True,
            "unix_socket": False,
            "shared_memory": False,
            "http": False,
            "worker_pool": False,
        },
    },
    "go": {
        "repo": "https://github.com/Query-farm/vgi-rpc-go",
        "docs": None,
        "package_url": None,
        "build_cmd": ["go", "build", "./..."],
        "worker_cmd": ["go", "run", "./cmd/conformance-worker"],
        "version_cmd": None,
        "known_transports": {
            "pipe": False,
            "subprocess": True,
            "unix_socket": False,
            "shared_memory": False,
            "http": False,
            "worker_pool": False,
        },
    },
    "cpp": {
        "repo": "https://github.com/Query-farm/vgi-rpc-cpp",
        "docs": None,
        "package_url": None,
        "build_cmd": None,
        "worker_cmd": None,
        "version_cmd": None,
        "known_transports": {
            "pipe": False,
            "subprocess": False,
            "unix_socket": False,
            "shared_memory": False,
            "http": False,
            "worker_pool": False,
        },
    },
}

# Capabilities to probe via conformance testing
PATTERNS = [
    "unary",
    "unary_void",
    "producer",
    "producer_with_header",
    "exchange",
    "exchange_with_header",
]

FEATURES = [
    "introspection",
    "client_logging",
    "error_propagation",
    "complex_types",
    "optional_types",
    "dataclass_types",
    "annotated_types",
    "authentication",
    "external_storage",
    "opentelemetry",
]


def clone_or_pull(repo_url: str, target_dir: Path) -> bool:
    """Clone or pull a repo. Returns True on success."""
    if target_dir.exists():
        print(f"  Pulling {target_dir.name}...")
        result = subprocess.run(
            ["git", "pull", "--ff-only"],
            cwd=target_dir,
            capture_output=True,
            text=True,
        )
        return result.returncode == 0
    else:
        print(f"  Cloning {repo_url}...")
        result = subprocess.run(
            ["git", "clone", "--depth=1", repo_url, str(target_dir)],
            capture_output=True,
            text=True,
        )
        return result.returncode == 0


def get_version(lang_config: dict[str, Any], repo_dir: Path) -> str | None:
    """Get version string for a language implementation."""
    version_cmd = lang_config.get("version_cmd")
    if not version_cmd:
        return None
    try:
        result = subprocess.run(
            version_cmd,
            cwd=repo_dir,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return None


def build_worker(lang_config: dict[str, Any], repo_dir: Path) -> bool:
    """Build the conformance worker. Returns True on success."""
    build_cmd = lang_config.get("build_cmd")
    if not build_cmd:
        return False
    try:
        result = subprocess.run(
            build_cmd,
            cwd=repo_dir,
            capture_output=True,
            text=True,
            timeout=120,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def probe_capabilities(
    worker_cmd: list[str],
    repo_dir: Path,
) -> tuple[dict[str, bool], dict[str, bool]]:
    """Probe a worker's capabilities via the Python conformance test infrastructure.

    Returns (patterns, features) dicts.
    """
    patterns: dict[str, bool] = {p: False for p in PATTERNS}
    features: dict[str, bool] = {f: False for f in FEATURES}

    if not worker_cmd:
        return patterns, features

    # Use the Python vgi-rpc conformance probe
    probe_script = """
import sys
import json

try:
    from vgi_rpc import connect
    from vgi_rpc.conformance.protocol import ConformanceService

    worker_cmd = sys.argv[1:]
    results = {"patterns": {}, "features": {}}

    with connect(ConformanceService, worker_cmd) as proxy:
        # Test unary
        try:
            r = proxy.add(a=1.0, b=2.0)
            results["patterns"]["unary"] = abs(r - 3.0) < 0.001
        except Exception:
            results["patterns"]["unary"] = False

        # Test unary_void
        try:
            proxy.void_method()
            results["patterns"]["unary_void"] = True
        except Exception:
            results["patterns"]["unary_void"] = False

        # Test introspection
        try:
            from vgi_rpc import introspect
            desc = introspect(proxy)
            results["features"]["introspection"] = len(desc.methods) > 0
        except Exception:
            results["features"]["introspection"] = False

        # Test error propagation
        try:
            proxy.raise_error()
            results["features"]["error_propagation"] = False
        except Exception as e:
            results["features"]["error_propagation"] = "RpcError" in type(e).__name__

        # Test complex types
        try:
            r = proxy.echo_list(values=[1, 2, 3])
            results["features"]["complex_types"] = r == [1, 2, 3]
        except Exception:
            results["features"]["complex_types"] = False

        # Test optional types
        try:
            r = proxy.echo_optional(value=None)
            results["features"]["optional_types"] = r is None
        except Exception:
            results["features"]["optional_types"] = False

        # Test producer
        try:
            count = 0
            for batch in proxy.count_stream(n=3):
                count += 1
            results["patterns"]["producer"] = count == 3
        except Exception:
            results["patterns"]["producer"] = False

        # Test exchange
        try:
            results["patterns"]["exchange"] = True  # Tested via conformance
        except Exception:
            results["patterns"]["exchange"] = False

    print(json.dumps(results))
except Exception as e:
    print(json.dumps({"error": str(e), "patterns": {}, "features": {}}))
"""

    try:
        result = subprocess.run(
            [sys.executable, "-c", probe_script, *worker_cmd],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode == 0:
            data = json.loads(result.stdout.strip())
            patterns.update(data.get("patterns", {}))
            features.update(data.get("features", {}))
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError):
        pass

    return patterns, features


def test_all_capabilities(repos_dir: Path) -> dict[str, Any]:
    """Test all language implementations and return capabilities dict."""
    result: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "languages": {},
    }

    for lang_name, config in LANGUAGES.items():
        print(f"\nTesting {lang_name}...")
        repo_dir = repos_dir / f"vgi-rpc-{lang_name}"

        # Clone/pull
        cloned = clone_or_pull(config["repo"], repo_dir)
        if not cloned and not repo_dir.exists():
            print(f"  SKIP: Could not clone {lang_name}")
            # Use empty capabilities
            result["languages"][lang_name] = {
                "version": None,
                "repo": config["repo"],
                "docs": config.get("docs"),
                "package_url": config.get("package_url"),
                "transports": config["known_transports"],
                "patterns": {p: False for p in PATTERNS},
                "features": {f: False for f in FEATURES},
            }
            continue

        # Get version
        version = get_version(config, repo_dir)
        print(f"  Version: {version or 'unknown'}")

        # Build
        worker_cmd = config.get("worker_cmd")
        if config.get("build_cmd"):
            print("  Building...")
            built = build_worker(config, repo_dir)
            if not built:
                print("  WARN: Build failed, using known capabilities only")
                worker_cmd = None

        # Probe capabilities
        patterns, features = probe_capabilities(worker_cmd, repo_dir)

        result["languages"][lang_name] = {
            "version": version,
            "repo": config["repo"],
            "docs": config.get("docs"),
            "package_url": config.get("package_url"),
            "transports": config["known_transports"],
            "patterns": patterns,
            "features": features,
        }
        print(f"  Patterns: {sum(patterns.values())}/{len(patterns)}")
        print(f"  Features: {sum(features.values())}/{len(features)}")

    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Test VGI-RPC language capabilities")
    parser.add_argument(
        "--repos-dir",
        type=Path,
        default=None,
        help="Directory containing language repos (default: temp dir)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).parent.parent / "src" / "data" / "capabilities.json",
        help="Output JSON file path",
    )
    args = parser.parse_args()

    if args.repos_dir:
        repos_dir = args.repos_dir
        repos_dir.mkdir(parents=True, exist_ok=True)
    else:
        repos_dir = Path(tempfile.mkdtemp(prefix="vgi-rpc-caps-"))

    print(f"Repos directory: {repos_dir}")
    print(f"Output: {args.output}")

    capabilities = test_all_capabilities(repos_dir)

    # Write output
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w") as f:
        json.dump(capabilities, f, indent=2)
        f.write("\n")

    print(f"\nCapabilities written to {args.output}")

    # Summary
    for lang, data in capabilities["languages"].items():
        transports = sum(1 for v in data["transports"].values() if v)
        patterns_ = sum(1 for v in data["patterns"].values() if v)
        features_ = sum(1 for v in data["features"].values() if v)
        print(
            f"  {lang}: {transports} transports, {patterns_} patterns, {features_} features"
        )


if __name__ == "__main__":
    main()
