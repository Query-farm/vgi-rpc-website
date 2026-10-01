# OG image fonts

Static-weight instances of the Query.Farm typefaces (Petrona and Noto Sans), for `satori` to rasterize
into generated Open Graph images (`scripts/og-images/render.mjs`). Committed
here rather than fetched at build time, same reasoning as the usage snapshots
in `generated/usage.json`: a production build must be deterministic and
offline.

Both families ship from Google Fonts as **variable** fonts only — see the
`google/fonts` repo, `ofl/petrona/Petrona[wght].ttf` and
`ofl/notosans/NotoSans[wdth,wght].ttf`. Satori does not
interpolate variable axes, so each weight actually used on an OG card was
pinned to a static instance with `fonttools`:

```sh
python3 -m venv .fontenv && .fontenv/bin/pip install fonttools brotli
.fontenv/bin/python3 -m fontTools.varLib.instancer -o Petrona-600.ttf Petrona[wght].ttf wght=600
.fontenv/bin/python3 -m fontTools.varLib.instancer -o Petrona-700.ttf Petrona[wght].ttf wght=700
# Noto Sans: instanced at wdth=100, then subset to Latin, punctuation, currency,
# arrows and number forms (the full font is 2 MB; each cut here is ~100 KB).
for w in 400 500 600; do
  .fontenv/bin/python3 -m fontTools.varLib.instancer -o NotoSans-$w.ttf 'NotoSans[wdth,wght].ttf' wght=$w wdth=100
  .fontenv/bin/pyftsubset NotoSans-$w.ttf --output-file=NotoSans-$w.ttf --layout-features='*' --name-IDs='*' \
    --unicodes='U+0020-024F,U+2000-206F,U+20A0-20CF,U+2100-215F,U+2190-21FF,U+2212'
done
```

Both fonts are SIL Open Font License 1.1 (see the `OFL.txt` alongside each
family in google/fonts) — redistribution as part of this repo's tooling is
permitted.

`JetBrainsMono-700.ttf` is the static Bold cut of JetBrains Mono (OFL), used for
the lowercase `vgi-rpc` name, which the site always sets in monospace.
