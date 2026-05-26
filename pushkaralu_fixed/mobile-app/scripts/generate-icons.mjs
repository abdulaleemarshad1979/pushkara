#!/usr/bin/env node
/**
 * generate-icons.mjs — Rasterise the Pushkaralu SVG logo into the
 * PNG inputs that @capacitor/assets needs to populate every Android
 * mipmap and iOS asset-catalog size.
 *
 * Strategy: shell out to `rsvg-convert` (librsvg2-bin), which is a
 * tiny native binary installed via apt-get on the CI runner. We
 * deliberately do NOT use `sharp` because its prebuilt-binary
 * download is the single biggest cause of npm-install failures on
 * GitHub Actions.
 *
 * Inputs:  resources/icon-source.svg
 *          resources/splash-source.svg
 *
 * Outputs (filenames are exact — @capacitor/assets v3 looks for these):
 *   resources/icon-only.png        1024x1024  legacy + adaptive foreground
 *   resources/icon-foreground.png  1024x1024  adaptive icon foreground
 *   resources/icon-background.png  1024x1024  adaptive icon background
 *   resources/splash.png           2732x2732  splash (light)
 *   resources/splash-dark.png      2732x2732  splash dark variant
 */

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { execFileSync, execSync } from 'node:child_process';

const __dirname  = path.dirname(fileURLToPath(import.meta.url));
const ROOT       = path.resolve(__dirname, '..');
const RES_DIR    = path.resolve(ROOT, 'resources');

const ICON_SVG   = path.join(RES_DIR, 'icon-source.svg');
const SPLASH_SVG = path.join(RES_DIR, 'splash-source.svg');

if (!fs.existsSync(ICON_SVG))   { console.error('Missing', ICON_SVG); process.exit(1); }
if (!fs.existsSync(SPLASH_SVG)) { console.error('Missing', SPLASH_SVG); process.exit(1); }

// Verify rsvg-convert is on PATH; fail fast with a clear message if not.
try {
  execSync('rsvg-convert --version', { stdio: 'pipe' });
} catch {
  console.error('[icons] ERROR: rsvg-convert not found on PATH.');
  console.error('[icons] Install it before running this script:');
  console.error('[icons]   Ubuntu:  sudo apt-get install -y librsvg2-bin');
  console.error('[icons]   macOS:   brew install librsvg');
  process.exit(1);
}

function rasterize(svgPath, outPath, size) {
  execFileSync('rsvg-convert', [
    '-w', String(size),
    '-h', String(size),
    '-f', 'png',
    '-o', outPath,
    svgPath,
  ], { stdio: 'inherit' });

  // Sanity check: file must be non-empty, otherwise capacitor-assets
  // will silently skip it later.
  const stat = fs.statSync(outPath);
  if (stat.size < 1024) {
    throw new Error(`[icons] ${outPath} is suspiciously small (${stat.size} bytes)`);
  }
  console.log(`[icons] ${path.basename(outPath)}  ${size}x${size}  (${stat.size} bytes)`);
}

rasterize(ICON_SVG,   path.join(RES_DIR, 'icon-only.png'),       1024);
rasterize(ICON_SVG,   path.join(RES_DIR, 'icon-foreground.png'), 1024);
rasterize(ICON_SVG,   path.join(RES_DIR, 'icon-background.png'), 1024);
rasterize(SPLASH_SVG, path.join(RES_DIR, 'splash.png'),          2732);
rasterize(SPLASH_SVG, path.join(RES_DIR, 'splash-dark.png'),     2732);
console.log('[icons] done');
