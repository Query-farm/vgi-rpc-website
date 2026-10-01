// Shiki theme for code blocks, copied from query-farm-astro/astro.config.mjs so
// highlighted code matches query.farm. Ground is rock-900, which every code
// block uses in both site themes (Strata Sun brief §3).
/** @type {import('shiki').ThemeRegistration} */
export const farmTheme = {
  name: 'farm-theme',
  type: 'dark',
  colors: {
    'editor.background': '#1a1512',
    'editor.foreground': '#e9e1d3',
  },
  tokenColors: [
    { scope: ['comment', 'punctuation.definition.comment'], settings: { foreground: '#8a7f70', fontStyle: 'italic' } },
    { scope: ['string', 'string.quoted'], settings: { foreground: '#9fc48c' } },
    { scope: ['keyword', 'storage.type', 'storage.modifier'], settings: { foreground: '#d9a441', fontStyle: 'bold' } },
    { scope: ['entity.name.function', 'support.function'], settings: { foreground: '#d3a6e0' } },
    { scope: ['constant.numeric', 'constant.language'], settings: { foreground: '#e0a44f' } },
    { scope: ['variable', 'entity.name'], settings: { foreground: '#8fc7d8' } },
    { scope: ['punctuation'], settings: { foreground: '#a2988a' } },
    { scope: ['constant.other', 'support.type'], settings: { foreground: '#f0c877' } },
    { scope: ['keyword.operator'], settings: { foreground: '#e9e1d3' } },
  ],
};
