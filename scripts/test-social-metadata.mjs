import { readdir, readFile, stat } from 'node:fs/promises';
import { join, relative } from 'node:path';

const distDir = new URL('../dist/', import.meta.url);

async function htmlFiles(directory) {
  const entries = await readdir(directory, { withFileTypes: true });
  const nested = await Promise.all(entries.map(async entry => {
    const path = join(directory, entry.name);
    if (entry.isDirectory()) return htmlFiles(path);
    return entry.isFile() && entry.name.endsWith('.html') ? [path] : [];
  }));
  return nested.flat();
}

function attributes(tag) {
  return Object.fromEntries(
    [...tag.matchAll(/([:\w-]+)="([^"]*)"/g)].map(match => [match[1], match[2]]),
  );
}

function metadata(html) {
  return [...html.matchAll(/<meta\b[^>]*>/g)].map(match => attributes(match[0]));
}

function metaValue(tags, attribute, key) {
  return tags.find(tag => tag[attribute] === key)?.content;
}

function requireValue(value, label, page, failures) {
  if (!value) failures.push(`${page}: missing ${label}`);
  return value;
}

const requiredOpenGraph = [
  'og:title', 'og:description', 'og:type', 'og:url', 'og:locale', 'og:site_name',
  'og:image', 'og:image:secure_url', 'og:image:type', 'og:image:width',
  'og:image:height', 'og:image:alt',
];
const requiredTwitter = [
  'twitter:card', 'twitter:url', 'twitter:title', 'twitter:description',
  'twitter:image', 'twitter:image:alt',
];

const failures = [];
const pages = await htmlFiles(distDir.pathname);

for (const file of pages) {
  const page = relative(distDir.pathname, file);
  const html = await readFile(file, 'utf8');
  const tags = metadata(html);
  const canonical = attributes(html.match(/<link\b[^>]*rel="canonical"[^>]*>/)?.[0] ?? '').href;

  requireValue(metaValue(tags, 'name', 'title'), 'title meta tag', page, failures);
  requireValue(metaValue(tags, 'name', 'description'), 'description meta tag', page, failures);
  requireValue(metaValue(tags, 'name', 'robots'), 'robots meta tag', page, failures);

  for (const key of requiredOpenGraph) {
    requireValue(metaValue(tags, 'property', key), key, page, failures);
  }
  for (const key of requiredTwitter) {
    requireValue(metaValue(tags, 'name', key), key, page, failures);
  }

  const ogType = metaValue(tags, 'property', 'og:type');
  if (ogType === 'article') {
    requireValue(metaValue(tags, 'property', 'article:modified_time'), 'article:modified_time', page, failures);
    requireValue(metaValue(tags, 'property', 'article:section'), 'article:section', page, failures);
    requireValue(metaValue(tags, 'property', 'article:tag'), 'article:tag', page, failures);
  }

  const ogUrl = metaValue(tags, 'property', 'og:url');
  const twitterUrl = metaValue(tags, 'name', 'twitter:url');
  if (canonical !== ogUrl || canonical !== twitterUrl) {
    failures.push(`${page}: canonical, og:url, and twitter:url must match`);
  }

  const image = metaValue(tags, 'property', 'og:image');
  const secureImage = metaValue(tags, 'property', 'og:image:secure_url');
  const twitterImage = metaValue(tags, 'name', 'twitter:image');
  if (image !== secureImage || image !== twitterImage) {
    failures.push(`${page}: Open Graph and Twitter image URLs must match`);
  }
  if (image) {
    try {
      const imageUrl = new URL(image);
      if (imageUrl.protocol !== 'https:') failures.push(`${page}: social image must use HTTPS`);
      await stat(join(distDir.pathname, imageUrl.pathname));
    } catch {
      failures.push(`${page}: social image does not resolve to a built asset`);
    }
  }

  if (metaValue(tags, 'property', 'og:image:width') !== '1200' ||
      metaValue(tags, 'property', 'og:image:height') !== '630') {
    failures.push(`${page}: social image dimensions must be 1200x630`);
  }
  if (metaValue(tags, 'name', 'twitter:card') !== 'summary_large_image') {
    failures.push(`${page}: Twitter card must be summary_large_image`);
  }

  const jsonLd = html.match(/<script type="application\/ld\+json">([\s\S]*?)<\/script>/)?.[1];
  if (!jsonLd) {
    failures.push(`${page}: missing JSON-LD structured data`);
  } else {
    try {
      JSON.parse(jsonLd);
    } catch {
      failures.push(`${page}: invalid JSON-LD structured data`);
    }
  }
}

if (failures.length > 0) {
  console.error(failures.join('\n'));
  process.exit(1);
}

console.log(`social metadata valid for ${pages.length} generated pages`);
