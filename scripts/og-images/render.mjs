// Shared satori → PNG pipeline for generated Open Graph images, ported from
// query-farm-astro/scripts/og-images/render.mjs so vgi-rpc's cards match the
// Query.Farm ones. scripts/generate-og-images.mjs builds a satori element tree
// per page and hands it to renderToPng() below.
//
// Why satori + resvg instead of a headless browser: this only needs to run
// occasionally (explicit `npm run og`, output committed), so there is no
// reason to carry a Chromium download for it. satori lays out a
// JSX-like tree with Yoga and emits SVG; @resvg/resvg-js rasterizes that SVG
// to a PNG buffer. Neither needs a browser or a network request at run time.
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { extname } from 'node:path';
import satori from 'satori';
import { Resvg } from '@resvg/resvg-js';

const MIME_BY_EXT = { '.svg': 'image/svg+xml', '.png': 'image/png', '.webp': 'image/webp' };

/** Read a local image (from public/) and inline it as a base64 data URI, for satori's <img>. */
export function fileDataUri(absolutePath) {
  const mime = MIME_BY_EXT[extname(absolutePath).toLowerCase()];
  return `data:${mime};base64,${readFileSync(absolutePath).toString('base64')}`;
}

const FONT_DIR = fileURLToPath(new URL('./fonts/', import.meta.url));

const FONT_FILES = {
  'Petrona:600': 'Petrona-600.ttf',
  'Petrona:700': 'Petrona-700.ttf',
  'Noto Sans:400': 'NotoSans-400.ttf',
  'Noto Sans:500': 'NotoSans-500.ttf',
  'Noto Sans:600': 'NotoSans-600.ttf',
  'JetBrains Mono:700': 'JetBrainsMono-700.ttf',
};

// Loaded once and reused across every card in a generator run.
const fonts = Object.entries(FONT_FILES).map(([key, file]) => {
  const [name, weight] = key.split(':');
  return { name, weight: Number(weight), style: 'normal', data: readFileSync(FONT_DIR + file) };
});

export const WIDTH = 1200;
export const HEIGHT = 630;

/** Strata Sun tokens an OG card is allowed to use (src/styles/global.css). */
export const COLORS = {
  soilPaper: '#f7f3ea', // --color-soil-50
  soil200: '#e6dbc2',
  soil300: '#cfc4ad',
  soil600: '#7a5230',
  soil700: '#5d4632',
  soil900: '#211a12', // ink
  sun300: '#f0c877',
  sun600: '#a9762e',
  sun700: '#7d5714',
};

/** Render one satori element tree to a PNG buffer. */
export async function renderToPng(node) {
  const svg = await satori(node, { width: WIDTH, height: HEIGHT, fonts });
  const resvg = new Resvg(svg, { fitTo: { mode: 'width', value: WIDTH } });
  return resvg.render().asPng();
}

/** Truncate on a word boundary and add an ellipsis, for text baked into a fixed-size card. */
export function clip(text, max) {
  if (text.length <= max) return text;
  const cut = text.slice(0, max);
  const lastSpace = cut.lastIndexOf(' ');
  return `${cut.slice(0, lastSpace > max * 0.6 ? lastSpace : max)}…`;
}

// The Strata Sun mark, copied from query-farm-astro/public/media-kit/logo/mark.svg:
// a disc clipped into four bands. Same file the media kit hands out.
const MARK_SVG = fileDataUri(fileURLToPath(new URL('./mark.svg', import.meta.url)));

/**
 * The Query.Farm wordmark, top-left of every generated card. Rebuilt from
 * satori's own text layer — same weight/colors as
 * query-farm-astro's media-kit wordmark-light.svg (Petrona 700, the "." in sun-700)
 * — rather than embedding that SVG's <text> directly, which would ask resvg
 * to lay out a nested SVG's text with its own font resolution instead of the
 * Petrona/Noto Sans files loaded for this render.
 */
export function wordmark({ size = 'lg' } = {}) {
  const dims = size === 'lg' ? { mark: 46, fontSize: 38, gap: '16px' } : { mark: 30, fontSize: 22, gap: '12px' };
  const textStyle = { fontFamily: 'Petrona', fontWeight: 700, fontSize: `${dims.fontSize}px` };
  return {
    type: 'div',
    props: {
      style: { display: 'flex', alignItems: 'center', gap: dims.gap },
      children: [
        { type: 'img', props: { src: MARK_SVG, width: dims.mark, height: dims.mark } },
        {
          type: 'div',
          props: {
            style: { display: 'flex', alignItems: 'baseline' },
            children: [
              { type: 'div', props: { style: { display: 'flex', ...textStyle, color: COLORS.soil900 }, children: 'Query' } },
              { type: 'div', props: { style: { display: 'flex', ...textStyle, color: COLORS.sun700 }, children: '.' } },
              { type: 'div', props: { style: { display: 'flex', ...textStyle, color: COLORS.soil900 }, children: 'Farm' } },
            ],
          },
        },
      ],
    },
  };
}
