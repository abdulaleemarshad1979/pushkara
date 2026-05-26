#!/usr/bin/env node
/**
 * generate-icons.mjs — Rasterise the Pushkaralu SVG logo into the
 * PNG inputs that @capacitor/assets needs to populate every Android
 * mipmap and iOS asset-catalog size.
 *
 * Inputs:  resources/icon-source.svg
 *          resources/splash-source.svg
 * Outputs: resources/icon.png        (1024x1024 — used as adaptive icon foreground)
 *          resources/splash.png      (2732x2732 — full splash)
 *          resources/splash-dark.png (same image; theme is dark-first)
 *
 * After this runs, `npx capacitor-assets generate --android --ios` will
 * fan these out to all required resolutions.
 */

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import sharp from 'sharp';

const __dirname  = path.dirname(fileURLToPath(import.meta.url));
const ROOT       = path.resolve(__dirname, '..');
const RES_DIR    = path.resolve(ROOT, 'resources');

const ICON_SVG   = path.join(RES_DIR, 'icon-source.svg');
const SPLASH_SVG = path.join(RES_DIR, 'splash-source.svg');

if (!fs.existsSync(ICON_SVG))  { console.error('Missing', ICON_SVG); process.exit(1); }
if (!fs.existsSync(SPLASH_SVG)){ console.error('Missing', SPLASH_SVG); process.exit(1); }

async function rasterize(svgPath, outPath, size) {
  const svg = fs.readFileSync(svgPath);
  await sharp(svg, { density: 384 })
    .resize(size, size, { fit: 'contain', background: { r: 11, g: 46, b: 110, alpha: 1 } })
    .png()
    .toFile(outPath);
  console.log(`[icons] ${path.basename(outPath)}  ${size}x${size}`);
}

(async () => {
  await rasterize(ICON_SVG,   path.join(RES_DIR, 'icon.png'),         1024);
  await rasterize(SPLASH_SVG, path.join(RES_DIR, 'splash.png'),       2732);
  await rasterize(SPLASH_SVG, path.join(RES_DIR, 'splash-dark.png'),  2732);
  console.log('[icons] done');
})().catch((e) => { console.error(e); process.exit(1); });
