# Worker file size and memory

The website's **Binary size & memory** comparison is a separate dataset from the
Linux latency/throughput suite. It measures the full conformance example worker
in C, C++, Rust, Go, Python, TypeScript, Java, and C#. These services expose the
same scalar-add and large-binary-echo methods. They are not minimal calculator
programs: their other methods, optional integrations, and dependency sets vary.

`footprint-manifest.json` pins the reviewed source commits used by the comparison.
Versions identify those sources; they are not a claim that each is today's latest
release. C is an unreleased 0.1.0 source snapshot. Keep these pins independent of
`manifest.json`, which governs the existing latency publication contract.

## Reproduce

The current recipe measures **macOS ARM64**. Run all implementations on the same
host. It requires CMake, a C/C++ compiler, Go, Rust, Bun, uv, .NET 10, and Java.
The Java build uses its declared JDK 25 toolchain; its Java 21-compatible worker
runs with the `java` selected by PATH. Gradle can provision its build toolchain.
The C dependencies are described in the C SDK's build guide. C++ uses vcpkg at
`05442024c3fda64320bd25d2251cc9807b84fb6f` with the repository's overlay ports.

From the website repository:

```sh
uv venv .footprint-cache/driver --python 3.13
uv pip install --python .footprint-cache/driver/bin/python -r benchmarks/requirements-footprint.txt
python3 benchmarks/footprint.py build
.footprint-cache/driver/bin/python benchmarks/footprint.py measure
python3 benchmarks/footprint.py publish
python3 benchmarks/footprint.py validate
npm run build
```

Builds use isolated checkouts in `.footprint-cache/sources`. A sibling repository
is used as the clone source when available; otherwise the public GitHub URL is
used. No sibling development checkout is switched or cleaned. A cached checkout
with a different revision is refused: move it aside before changing a pin.
`--repos-dir` selects the sibling repository directory. `--only c cpp` can be
used for build or measurement development; a partial measurement cannot publish.

The runner records commands in `.footprint-results/*-build.json`, build logs,
worker stderr, file inventories, and a checkpointed `candidate.json`. Run builds
before measurements and avoid other memory-intensive workloads during the run.
Every process has a timeout and its process group is terminated on completion
or error.

## File accounting

Sizes are uncompressed regular-file byte counts, not download sizes, allocated
filesystem blocks, or resident memory. Symlinks resolve to their targets and
hard-linked files count once within each inventory. Inventory paths replace the
repository and home directories with `{root}` and `{home}`. Each counted file
has a SHA-256 digest; the inventory itself is hashed as well.

| Implementation | Worker artifact | Application plus dependencies | Separate runtime files |
| --- | --- | --- | --- |
| C | Release executable, stripped with `strip -S -x` | Executable plus recursively linked non-system dylibs | None; system libraries excluded |
| C++ | Release executable, stripped with `strip -S -x` | Executable plus recursively linked non-system dylibs; vcpkg libraries are statically linked in this build | None; system libraries excluded |
| Rust | `cargo --release`, `profile.release.strip="symbols"` | Executable and non-system dylibs | None; runtime code is in the executable |
| Go | `go build -trimpath -ldflags='-s -w'` | Executable and non-system dylibs | None; runtime code is in the executable |
| Python | `vgi_rpc/` package source | Source plus the environment's site-packages, installed with the locked HTTP and conformance extras and no dev group | Interpreter, CPython framework, standard library, and interpreter-linked non-system dylibs |
| TypeScript | `src/`, `examples/`, and `package.json` | Source plus `bun install --production --omit peer --frozen-lockfile` packages; all example sources are retained to cover relative imports | Bun executable and non-system dylibs |
| Java | Worker JAR | Gradle `installDist` directory, including framework and dependency JARs | Selected JDK's `bin/java`, `lib/`, and `conf/` |
| C# | Worker assembly | Release, framework-dependent `dotnet publish` directory without PDBs | Selected .NET and ASP.NET shared frameworks, hostfxr, and `dotnet` |

Runtime directories are counted as installed. In particular, Java's full module
image includes development-related modules even though JDK headers, `jmods/`,
and launcher tools such as `javac` are not counted. No `jlink`, .NET trimming,
Native AOT, PyInstaller, or Bun standalone compilation is applied. These are
explicit build/distribution recipes, not minimum possible deployment sizes.
Python bytecode caches are excluded. OS libraries under `/usr/lib/` and
`/System/Library/` are excluded; macOS supplies them through its shared cache.
Source and production packages may include their distributed documentation,
tests, and metadata. The inventories define the exact boundary.

The **application + runtime** sum is useful for an installation without a
preinstalled language runtime. When several workers share a runtime, its disk
cost is paid once. The worker artifact alone is not comparable to a complete
deployment: a small JAR/DLL or script relies on separately counted code.

## Memory workload

Each implementation runs five times in a deterministic shuffled order, with a
new process for every round. All workers use stdio, one connection, one caller,
and one outstanding request. A fixed PyArrow client sends the same IPC schema,
arguments, protocol routing metadata, and payload to every worker.

1. Complete 1,000 `add_floats(a=2.0, b=3.0)` calls, checking every result is 5.0.
2. Sample idle process-tree RSS for 0.5 seconds; take the median for that round.
3. Complete 200 `echo_large_binary` calls with a deterministic 1 MiB payload,
   checking every returned byte. Sample process-tree RSS in a separate thread
   at a target interval of 10 ms and include the final observation.
4. Record the maximum observed RSS during the echo phase, then stop the worker.

The table reports the median of the five round values and their minimum–maximum
range. Raw samples include timestamps and process counts. Process enumeration
and scheduling can make the actual interval longer than 10 ms; short-lived
peaks can be missed. This is a **sampled peak for this workload**, not a kernel
high-water mark, a memory limit, or the peak of the entire worker lifetime.
The echo phase is a fixed number of calls, not a sustained-duration load test.

RSS uses [psutil's `memory_info().rss`](https://psutil.readthedocs.io/stable/index.html#psutil.Process.memory_info).
It is resident memory for the worker and its descendants, including runtime,
native allocations, and shared resident pages. It is not a managed-heap-only or
private-memory figure. Shared pages may be counted in multiple processes.
The client, sampler, build tools, and unrelated processes are excluded. No
forced GC, shared-memory transport, or external payload storage is enabled.

## Publication and validation

`publish` requires the complete eight-language matrix, exactly five rounds per
worker, source-pin agreement, successful correctness checks, positive observed
memory, and summaries matching the raw samples. It writes:

- `src/data/footprint.json`: the site's data source.
- `public/benchmarks/footprint.json` and `.csv`: downloadable results.
- `public/benchmarks/footprint-<language>-files.json`: exact disk inventories.

The website presents this macOS snapshot separately from the canonical Linux
latency results. Do not combine numbers from different hosts or revise the
latency suite's publication gate to accommodate this dataset.

Run the measurement contract tests with the existing suite:

```sh
python3 -m unittest discover -s benchmarks/tests -p 'test_*.py'
```
