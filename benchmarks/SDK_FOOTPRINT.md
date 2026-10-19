# C versus Rust VGI application workers

This dataset compares `vgi-c` and `vgi-rust`, the full VGI application SDKs.
It is separate from the RPC conformance worker comparison in `FOOTPRINT.md`.
The worker programs include the integration suite's catalogs, functions, and
fixtures. Their broader feature sets and catalog representations differ.

The source pins and workload are in `sdk-footprint-manifest.json`. In particular,
the C SDK uses RPC C at `01ab0873`; the Rust SDK uses RPC Rust 0.31.3 and Arrow
60.0.0. The recorded `sdk-rust.Cargo.lock` fixes its dependency resolution (the
SDK repository does not track a lockfile). Measurements use clean, detached
checkouts and exclude uncommitted development changes.

## Reproduce on macOS ARM64

Run from the website repository. Install the C SDK's documented build
dependencies, Rust, CMake, and uv. Prepare the isolated source checkouts:

```sh
python3 - <<'PY'
import json, subprocess
from pathlib import Path
manifest = json.loads(Path('benchmarks/sdk-footprint-manifest.json').read_text())
root = Path('.footprint-cache/sdk')
sources = dict(manifest['sources'])
sources.update({row['directory']: row for row in manifest['implementations'].values()})
for directory, source in sources.items():
    target = root / directory
    subprocess.run(['git', 'clone', '--no-checkout', source['repository'], str(target)], check=True)
    subprocess.run(['git', '-C', str(target), 'checkout', '--detach', source['sha']], check=True)
PY
cp benchmarks/sdk-rust.Cargo.lock .footprint-cache/sdk/rust/Cargo.lock
cmake -S .footprint-cache/sdk/c -B .footprint-cache/sdk/c/build \
  -DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=OFF -DVGI_BUILD_TESTS=OFF \
  -DVGI_RPC_SOURCE_DIR="$PWD/.footprint-cache/sdk/rpc-c"
cmake --build .footprint-cache/sdk/c/build --parallel 3 --target vgi-example-worker
strip -S -x .footprint-cache/sdk/c/build/example-worker/vgi-example-worker
cargo build --manifest-path .footprint-cache/sdk/rust/Cargo.toml \
  --locked --release -j 3 -p vgi-example-worker --config 'profile.release.strip="symbols"'
uv venv .footprint-cache/sdk/driver --python 3.14
uv pip install --python .footprint-cache/sdk/driver/bin/python \
  -e .footprint-cache/sdk/python -e .footprint-cache/sdk/rpc-python \
  -r benchmarks/requirements-footprint.txt
.footprint-cache/sdk/driver/bin/python benchmarks/sdk_footprint.py measure
python3 benchmarks/sdk_footprint.py publish
npm test
npm run build
```

`--workspace PATH` selects another directory containing the same five source
checkouts. The original run used `/tmp/vgi-sdk-footprint`; its `rpc-c` directory
was a symlink to the already pinned RPC footprint checkout. Build commands
otherwise match this recipe. CMake Release uses `-O3 -DNDEBUG`; Rust retains
the repository's release `opt-level = 2`. No extra LTO or size optimization is
introduced. Toolchain versions and executable hashes are in the result and
file inventories. Finish all builds before measuring.

## Workload and accounting

The pinned Python VGI client runs in a separate process. Each round starts a
fresh worker using direct subprocess transport, `pool=None`, and `worker_limit=1`.
The runner removes inherited VGI/OTEL configuration from its environment; the
client uses ordinary pipes with no SHM arena or external payload configuration.

1. Invoke the `main.double` scalar function 1,000 times with one int64 row
   containing 2. Each complete VGI invocation binds and initializes a stream;
   every result must be one row containing 4.
2. Sample worker process-tree RSS for 0.5 seconds. Record its median.
3. Invoke `main.passthru` 200 times, with one UTF-8 string row of exactly 1 MiB
   (`0123456789abcdef` repeated). Validate every returned byte. Sample RSS in a
   background thread every 10 ms, plus observations at the beginning and end.
   Record the maximum observed RSS.
4. Close the client and worker. Repeat five times per implementation, with
   deterministic shuffled order (seed 20261019). Report medians and min–max
   ranges across rounds. Each worker has a 120-second watchdog.

These are fixed-call workloads, not sustained load or peak lifetime memory.
Sampling can miss brief spikes. RSS includes shared resident pages and excludes
the client. Disk accounting follows `FOOTPRINT.md`: stripped executable plus
recursively linked non-system dylibs, excluding `/usr/lib` and `/System/Library`.
Neither worker needs separate runtime files.

The C example embeds `catalogs.arrow` (18,422,096 bytes) and `fixtures.arrow`
(171,384 bytes): **17.73 MiB of fixtures**, about 92% of its stripped executable.
`example-worker/fixtures.c` decodes catalogs at initialization and registers
catalog snapshots. Rust constructs its catalogs in code. This packaging
accounts for much of the C binary size and is a likely contributor to its
higher baseline memory; this measurement does not attribute individual heap
allocations. It does not establish a minimum footprint for either SDK.

The publication gate reuses the RPC dataset's checks for source revisions,
complete rounds, correctness counters, memory observations, and file inventory
hashes. It additionally checks driver/dependency pins and the Rust lock hash.
Generated files live in `src/data/sdk-footprint.json` and `public/benchmarks/sdk/`.
Do not hand-edit the results or merge them into the RPC language table.
