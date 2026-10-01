#!/usr/bin/env node
// Generate committed social cards with `npm run og`; no browser or network needed.
import { mkdirSync, writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { renderToPng, wordmark, COLORS, WIDTH, HEIGHT } from './og-images/render.mjs';

const OUT_DIR = fileURLToPath(new URL('../public/og/', import.meta.url));
const PAGES = [
  {
    slug: 'home', path: '',
    lines: ['Arrow-native RPC.', 'From subprocess', 'to service.'],
    tagline: 'Typed services. Batched calls. Direct browser access.',
    detail: 'PYTHON · TYPESCRIPT · GO · RUST · JAVA · C# · C++',
  },
  {
    slug: 'wire-protocol', path: '/wire-protocol',
    lines: ['The wire protocol.', 'Every batch,', 'specified.'],
    tagline: 'Arrow IPC, exchange streams, and portable HTTP state.',
    detail: 'WIRE VERSION 1 / TECHNICAL SPECIFICATION',
  },
  {
    slug: 'benchmarks', path: '/benchmarks',
    lines: ['Measured across', 'languages.', 'Across transports.'],
    tagline: 'Release-pinned servers. Reproducible RPC benchmarks.',
    detail: 'LATENCY · THROUGHPUT · REAL TRANSPORTS',
  },
];
const tones = ['#f0c877', '#d9a441', '#a9762e', '#7a5230'];
const el = (type, style, children) => ({ type, props: { style: { display: 'flex', ...style }, children } });

// Original columnar artwork, following RpcIllustration.astro's batches and routed streams.
function illustration(slug) {
  const sun = `<defs><clipPath id="sun"><circle cx="330" cy="92" r="65"/></clipPath></defs><g clip-path="url(#sun)">${tones.map((fill,i)=>`<rect x="265" y="${27+i*33}" width="130" height="33" fill="${fill}"/>`).join('')}</g>`;
  const lines = Array.from({length:7},(_,i)=>`<path d="M${20+i*12} 400C${20+i*12} 295 ${160+i*12} 390 ${160+i*12} 260"/>`).join('');
  const cells = tones.map((fill,col)=>Array.from({length:5},(_,row)=>`<rect x="${14+col*43}" y="${38+row*24}" width="31" height="17" fill="${fill}" opacity="${1-row*.09}"/>`).join('')).join('');
  const batch = `<g transform="translate(92 135) rotate(-12 95 85)">${[2,1,0].map(i=>`<g transform="translate(${i*16} ${i*17})"><rect width="190" height="170" fill="#f7f3ea" stroke="#7a5230"/><path d="M0 25H190" stroke="#7a5230"/>${i===0?cells:''}</g>`).join('')}</g>`;
  const bars = `<path d="M60 340H400" stroke="#7a5230"/>${[110,175,225,285].map((height,i)=>`<rect x="${85+i*75}" y="${340-height}" width="45" height="${height}" fill="${tones[i]}"/><path d="M${85+i*75} ${320-height}h45" stroke="#7a5230"/>`).join('')}`;
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="450" height="440" viewBox="0 0 450 440"><g stroke="#cfc4ad" stroke-width=".7" fill="none"><path d="M15 90H430M15 195H430M15 300H430M15 405H430M60 20V420M165 20V420M270 20V420M375 20V420"/></g>${slug==='benchmarks'?bars:sun+`<g stroke="#7a5230" stroke-width="1" fill="none">${lines}</g>`+batch}</svg>`;
  return `data:image/svg+xml;base64,${Buffer.from(svg).toString('base64')}`;
}

function card(page) {
  return el('div', { width: WIDTH, height: HEIGHT, backgroundColor: COLORS.soilPaper, fontFamily: 'Noto Sans', position:'relative' }, [
    el('div', {position:'absolute',top:0,left:0,width:WIDTH,height:6}, ['#f0c877','#d9a441','#a9762e','#7a5230','#5d4632','#211a12'].map(backgroundColor=>el('div',{flex:1,backgroundColor},[]))),
    el('div', {position:'absolute',top:45,left:64,alignItems:'center',gap:24}, [
      el('div',{fontFamily:'JetBrains Mono',fontWeight:700,fontSize:28,color:COLORS.soil900},'vgi-rpc'),
      el('div',{height:27,width:1,backgroundColor:COLORS.soil300},[]),
      wordmark({size:'sm'}),
    ]),
    el('div',{position:'absolute',left:64,top:137,flexDirection:'column'},page.lines.map((line,i)=>el('div',{
      fontFamily:'Petrona',fontWeight:600,fontSize:page.slug==='benchmarks'?68:72,lineHeight:1.09,letterSpacing:'-0.035em',color:i===2?COLORS.sun700:COLORS.soil900,
    },line))),
    {type:'img',props:{src:illustration(page.slug),width:420,height:411,style:{position:'absolute',right:28,top:105}}},
    el('div',{position:'absolute',left:64,top:404,width:605,fontSize:24,lineHeight:1.5,color:COLORS.soil700},page.tagline),
    el('div',{position:'absolute',left:64,top:515,fontFamily:'JetBrains Mono',fontWeight:700,fontSize:13,letterSpacing:'.02em',color:COLORS.soil600},page.detail),
    el('div',{position:'absolute',left:64,right:64,bottom:36,borderTop:`1px solid ${COLORS.soil300}`,paddingTop:17,fontSize:16,color:COLORS.soil700},`vgi-rpc.query.farm${page.path}`),
  ]);
}
mkdirSync(OUT_DIR, {recursive:true});
for (const page of PAGES) writeFileSync(`${OUT_DIR}/${page.slug}.png`, await renderToPng(card(page)));
console.log(`Wrote ${PAGES.length} Open Graph images to ${OUT_DIR}`);
