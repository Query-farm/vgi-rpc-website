# vgi-rpc-website

Source for [vgi-rpc.query.farm](https://vgi-rpc.query.farm) — the marketing/docs site for
[`vgi-rpc`](https://github.com/Query-farm/vgi-rpc-python), Query.Farm's transport-agnostic
Apache-Arrow-IPC RPC framework. Built with [Astro](https://astro.build) + Tailwind CSS.

The site covers what `vgi-rpc` is, per-language quickstarts (Python/Go/Rust/TypeScript/C++), a
wire-protocol deep dive, a transport comparison, and a benchmark suite comparing all five ports.

## Structure

```
src/
  components/   Hero, WhatIs, WhyVgiRpc, LanguageCards, ComparisonTable,
                CapabilityMatrix, TransportOverview, BenchmarkCharts/Visuals, Nav, Footer, ...
  pages/        index.astro, wire-protocol.astro, benchmarks.astro
  diagrams/     d2 source for the transport-overview diagram (rendered to public/diagrams/ at build time)
  data/         structured content the components render (capability matrix, language metadata, ...)
  layouts/      shared page shell
  styles/       Tailwind entry
assets/         logo-master.png (source art) + scripts/regenerate_logo_assets.py-derived favicons/hero image
benchmarks/     Python harness that produces the single snapshot published at /benchmarks/latest.json
  adapters/     one per language port, drives each implementation's own benchmark workload
  driver/       orchestration — runs adapters, validates, assembles results
```

## Development

```sh
npm install
npm run dev       # generates the d2 diagram, then starts the Astro dev server
npm run build      # generates diagrams, builds to ./dist/, then verifies social-metadata tags
npm run preview     # preview a production build locally
```

Regenerating just the transport-overview diagram (requires [`d2`](https://d2lang.com)):

```sh
npm run diagrams
```

Regenerating the logo/favicon assets from `assets/logo-master.png` (requires `uv`, or Python with
Pillow + numpy):

```sh
uv run --with pillow --with numpy python scripts/regenerate_logo_assets.py
```

## Benchmarks

The `/benchmarks` page renders a single published snapshot (`public/benchmarks/latest.json`) that
compares all five `vgi-rpc` ports (Python, TypeScript, Go, Rust, C++) against pinned, commit-exact
releases (`benchmarks/manifest.json`) — never against a moving `main` branch. See
[`benchmarks/README.md`](benchmarks/README.md) for the release policy and the full
`verify` → `full` → `combine` → `publish` pipeline; the short version:

```sh
npm run benchmark:quick     # fast local sanity pass
npm run benchmark:full      # full suite, isolated per-language checkouts under .benchmark-cache/
npm run benchmark:publish   # writes public/benchmarks/latest.json
```

## Testing

```sh
npm test              # benchmark harness unit tests + `benchmarks/run.py validate`
npm run test:metadata # social-card/OpenGraph metadata check (also runs as part of `npm run build`)
```

## Deployment

`site` in `astro.config.mjs` is pinned to `https://vgi-rpc.query.farm`. `.github/workflows/update-capabilities.yml`
(despite the filename, its workflow name is `Deploy`) builds the site and pushes `dist/` to
Cloudflare Pages on every push to `main`, or on demand via `workflow_dispatch`.
