#!/usr/bin/env node
// Generates one Open Graph card per page, in the Query.Farm card style
// (query-farm-astro/scripts/generate-vgi-docs-og-images.mjs is the model).
//
// Output goes to public/og/<slug>.png and is referenced by each page's
// `ogImage` prop. Run explicitly with `npm run og` and commit the result;
// nothing here runs during `npm run build`.
import { mkdirSync, writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { renderToPng, wordmark, fileDataUri, COLORS, WIDTH, HEIGHT } from './og-images/render.mjs';

const ROOT = fileURLToPath(new URL('..', import.meta.url));
const OUT_DIR = `${ROOT}/public/og`;

// The illustrated VGI badge, same file as the homepage hero.
const VGI_LOGO = fileDataUri(`${ROOT}/public/logo-hero.png`);
const VGI_LOGO_HEIGHT = 270;
const VGI_LOGO_WIDTH = Math.round(VGI_LOGO_HEIGHT * (600 / 437));

const PAGES = [
  {
    slug: 'home',
    path: '',
    title: { text: 'vgi-rpc', mono: true },
    tagline: 'Transport-agnostic RPC on Apache Arrow, in seven languages.',
  },
  {
    slug: 'wire-protocol',
    path: '/wire-protocol',
    title: { text: 'Wire protocol' },
    tagline: 'The normative spec for wire version 1.',
  },
  {
    slug: 'benchmarks',
    path: '/benchmarks',
    title: { text: 'Benchmarks' },
    tagline: 'Release-pinned servers measured over real transports.',
  },
];

const el = (type, style, children) => ({ type, props: { style: { display: 'flex', ...style }, children } });

function card({ path, title, tagline }) {
  const titleStyle = title.mono
    ? { fontFamily: 'JetBrains Mono', fontWeight: 700, fontSize: '84px', letterSpacing: '-0.02em' }
    : { fontFamily: 'Petrona', fontWeight: 700, fontSize: '76px', letterSpacing: '-0.022em' };

  return el('div', {
    flexDirection: 'column',
    width: `${WIDTH}px`,
    height: `${HEIGHT}px`,
    backgroundColor: COLORS.soilPaper,
    fontFamily: 'Noto Sans',
  }, [
    el('div', { width: '100%', height: '10px', backgroundColor: COLORS.sun700 }),
    el('div', { flexDirection: 'column', flex: 1, padding: '64px 72px', justifyContent: 'space-between' }, [
      el('div', { flexDirection: 'row', alignItems: 'center', gap: '32px' }, [
        el('div', { flexDirection: 'column', flex: 1 }, [
          wordmark(),
          el('div', {
            marginTop: '44px',
            alignSelf: 'flex-start',
            padding: '6px 16px',
            borderRadius: '9999px',
            backgroundColor: '#efe9db', // --color-soil-100
            color: COLORS.soil700,
            // The home card's title is already the lowercase monospace name, so its
            // pill names the category; subpage pills carry the name instead.
            ...(title.mono
              ? { fontWeight: 600, fontSize: '16px', letterSpacing: '0.04em' }
              : { fontFamily: 'JetBrains Mono', fontWeight: 700, fontSize: '17px' }),
          }, title.mono ? 'RPC FRAMEWORK' : 'vgi-rpc'),
          el('div', { marginTop: '28px', color: COLORS.soil900, lineHeight: 1.05, ...titleStyle }, title.text),
          el('div', {
            marginTop: '24px',
            maxWidth: '600px',
            fontWeight: 400,
            fontSize: '30px',
            lineHeight: 1.4,
            color: COLORS.soil700,
          }, tagline),
        ]),
        { type: 'img', props: { src: VGI_LOGO, width: VGI_LOGO_WIDTH, height: VGI_LOGO_HEIGHT } },
      ]),
      el('div', { fontWeight: 500, fontSize: '19px', color: COLORS.soil600 }, `vgi-rpc.query.farm${path}`),
    ]),
  ]);
}

async function main() {
  mkdirSync(OUT_DIR, { recursive: true });
  for (const page of PAGES) {
    writeFileSync(`${OUT_DIR}/${page.slug}.png`, await renderToPng(card(page)));
  }
  console.log(`Wrote ${PAGES.length} Open Graph image(s) to ${OUT_DIR}`);
}

main();
