# Release benchmarks

This suite produces the single benchmark snapshot published at
`/benchmarks/latest.json`. A new publish replaces that file; the website does
not retain or compare older benchmark releases.

## Release policy

`manifest.json` pins the latest published Python, TypeScript, Go, Rust, and C++
releases to full Git commit IDs. Never change a version without also resolving
and updating its dereferenced commit ID.

Verify local tags and the official upstream refs:

```sh
python3 benchmarks/run.py verify --network
```

The runner builds isolated checkouts in `.benchmark-cache`; it never checks out
or cleans the sibling development repositories.

## Running

```sh
npm run benchmark:quick
npm run benchmark:full
npm run benchmark:publish
```

For the canonical host, run scaling first and latency as a separate phase,
then combine them through the normal publication gate:

```sh
python3 benchmarks/run.py full --suite scaling
python3 benchmarks/run.py full --suite latency
python3 benchmarks/run.py combine
```

If a phase is resumed one implementation at a time, `assemble --suite
scaling` (or `latency`) rebuilds that phase candidate from its validated
per-server fragments before `combine`.

`quick` is a lifecycle and compatibility smoke test. `full` writes
`.benchmark-results/candidate-full.json` without changing the website.
`publish` validates the candidate's recorded canonical-runner preflight and
replaces the tracked latest JSON/CSV files. It may be run on the website host
after copying the candidate back from the benchmark runner.

For a full run, all release workers are built first. The runner then waits up
to 15 minutes for the one-minute load average to fall below 2.0 before timing
anything and records that preflight. Canonical published runs use the dedicated
ARM64 Linux `c9gd.8xlarge` EC2 runner.

The controlled layer uses the released Rust client for every server. Native
same-language results use the same dataset contract but are intentionally not
published until all five native drivers cover the same workload definitions;
partial native rankings would recreate the bias this suite removes.

## Measurement rules

- Release builds only, followed by a correctness call before timing.
- Python HTTP is hosted by pinned Granian 2.8.1 with 16 worker processes and
  one blocking thread per worker; this matches the 16-client scaling ceiling
  without using Waitress's fixed thread pool.
- Unloaded latency uses a one-second warm-up and five one-second rounds, with a
  deterministic shuffled server and transport order.
- Unary rounds collect at least 100 calls (at least 500 samples per result);
  payload rounds collect at least 100 calls through 1 MiB and 5 calls at 16 MiB.
- Results over 5% round-to-round coefficient of variation get two additional
  rounds. Persistently unstable results are labelled `noisy`.
- Mean/P50/P95/P99 and measured operations per second are reported; minimum latency
  is retained only as round evidence and is never a headline.
- Echo bandwidth is bidirectional application payload, not encoded wire bytes.
- Unix, TCP, and HTTP scaling scenarios use 1, 2, 4, 8, and 16 simultaneous
  clients. They run three two-second rounds after a one-second warm-up, use a
  fresh server for each point, and record whole-host CPU utilization.
- Every completed, unsupported, or failed scenario is appended immediately to
  a JSONL checkpoint beside its implementation result, with progress and ETA.
- Producer and exchange streams are excluded from this call-latency suite;
  their element rate requires a separate streaming-specific measurement model.
- Publication accepts only the complete full matrix. Quick, partial, and failed
  candidates cannot replace the website dataset.
