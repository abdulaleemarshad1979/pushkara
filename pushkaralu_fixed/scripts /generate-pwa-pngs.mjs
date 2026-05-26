#!/usr/bin/env node
/**
 * generate-pwa-pngs.mjs — produce the PNG launcher icons that
 * PWABuilder / Android require for the Pushkaralu PWA.
 *
 * Pure Node, zero dependencies (uses only `zlib` from the std-lib),
 * because the build sandbox has no image-processing tools and no
 * network access to install one.
 *
 * Outputs (written to dashboards/):
 *   icon-192.png            192x192  legacy + adaptive 'any'
 *   icon-512.png            512x512  legacy + adaptive 'any'
 *   icon-maskable-512.png   512x512  Android adaptive icon 'maskable'
 *
 * Design: navy-blue background (#0B2E6E, the Pushkaralu brand colour)
 * with three concentric white rings in the centre — a stylised river
 * motif appropriate for a Godavari pilgrimage app.
 */

import fs from 'node:fs';
import path from 'node:path';
import zlib from 'node:zlib';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const OUT_DIR   = path.resolve(__dirname, '..', 'dashboards');

// ── PNG byte-level encoder ───────────────────────────────────────────
const CRC_TABLE = (() => {
  const t = new Uint32Array(256);
  for (let n = 0; n < 256; n++) {
    let c = n;
    for (let k = 0; k < 8; k++) c = (c & 1) ? (0xEDB88320 ^ (c >>> 1)) : (c >>> 1);
    t[n] = c;
  }
  return t;
})();

function crc32(buf) {
  let crc = 0xFFFFFFFF;
  for (let i = 0; i < buf.length; i++) crc = CRC_TABLE[(crc ^ buf[i]) & 0xFF] ^ (crc >>> 8);
  return (crc ^ 0xFFFFFFFF) >>> 0;
}

function chunk(type, data) {
  const len = Buffer.alloc(4);
  len.writeUInt32BE(data.length, 0);
  const typeBuf = Buffer.from(type, 'ascii');
  const crcBuf  = Buffer.alloc(4);
  crcBuf.writeUInt32BE(crc32(Buffer.concat([typeBuf, data])), 0);
  return Buffer.concat([len, typeBuf, data, crcBuf]);
}

function encodePng(w, h, drawPixel) {
  // RGBA raw bytes with one filter-type byte at the start of each row.
  const rowSize = 1 + w * 4;
  const raw = Buffer.alloc(h * rowSize);
  for (let y = 0; y < h; y++) {
    raw[y * rowSize] = 0; // filter: None
    for (let x = 0; x < w; x++) {
      const [r, g, b, a] = drawPixel(x, y);
      const off = y * rowSize + 1 + x * 4;
      raw[off]     = r;
      raw[off + 1] = g;
      raw[off + 2] = b;
      raw[off + 3] = a;
    }
  }

  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(w, 0);
  ihdr.writeUInt32BE(h, 4);
  ihdr[8]  = 8;   // bit depth
  ihdr[9]  = 6;   // colour type: RGBA
  ihdr[10] = 0;   // compression: deflate
  ihdr[11] = 0;   // filter: none
  ihdr[12] = 0;   // interlace: no

  const idat = zlib.deflateSync(raw);

  return Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A]),
    chunk('IHDR', ihdr),
    chunk('IDAT', idat),
    chunk('IEND', Buffer.alloc(0)),
  ]);
}

// ── Icon design ──────────────────────────────────────────────────────
const NAVY  = [0x0B, 0x2E, 0x6E, 0xFF]; // brand background
const WHITE = [0xFF, 0xFF, 0xFF, 0xFF];

/**
 * Render one pixel of the logo.
 *
 * @param x,y          pixel coordinates inside the canvas
 * @param size         canvas side length (canvas is square)
 * @param logoScale    fraction of the canvas the logo occupies (0..1).
 *                     For 'any' purpose use ~0.78 (logo close to edge).
 *                     For 'maskable' purpose use ~0.55 so the logo
 *                     stays inside Android's 80% safe-zone after the
 *                     OS applies any mask shape.
 */
function pixelAt(x, y, size, logoScale) {
  const cx = size / 2;
  const cy = size / 2;
  const dx = x - cx;
  const dy = y - cy;
  const r  = Math.sqrt(dx * dx + dy * dy);

  const outerR = (size / 2) * logoScale;
  if (r > outerR) return NAVY;

  // White disc forming the logo body.
  // Three navy concentric rings inside it (river ripples).
  const ringWidth   = outerR * 0.08;
  const ringSpacing = outerR * 0.22;
  // Innermost (smallest) ring – ring0 – at radius outerR*0.22
  // Then ring1 at outerR*0.44, ring2 at outerR*0.66.
  for (let i = 0; i < 3; i++) {
    const ringR = outerR * (0.22 + i * 0.22);
    if (r >= ringR - ringWidth && r <= ringR + ringWidth) return NAVY;
  }
  return WHITE;
}

function generate(filename, size, logoScale) {
  const buf = encodePng(size, size, (x, y) => pixelAt(x, y, size, logoScale));
  const outPath = path.join(OUT_DIR, filename);
  fs.writeFileSync(outPath, buf);
  console.log(`[pwa-pngs] ${filename}  ${size}x${size}  (${buf.length} bytes)`);
}

if (!fs.existsSync(OUT_DIR)) {
  console.error('[pwa-pngs] missing dashboards/ at', OUT_DIR);
  process.exit(1);
}

generate('icon-192.png',          192, 0.78); // 'any' purpose
generate('icon-512.png',          512, 0.78); // 'any' purpose
generate('icon-maskable-512.png', 512, 0.55); // 'maskable' (safe zone)

console.log('[pwa-pngs] done');
