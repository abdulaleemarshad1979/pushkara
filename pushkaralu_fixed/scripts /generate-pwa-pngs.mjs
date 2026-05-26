#!/usr/bin/env node
/**
 * generate-pwa-pngs.mjs — produce all PNG assets the PWA / PWABuilder
 * needs for the Pushkaralu pilgrim portal.
 *
 * Pure Node, zero dependencies (uses only `zlib` from the std-lib),
 * because the build sandbox has no image-processing tools and no
 * network access to install one.
 *
 * Outputs (written to dashboards/):
 *   icon-192.png            192x192   launcher icon (purpose: any)
 *   icon-512.png            512x512   launcher icon (purpose: any)
 *   icon-maskable-512.png   512x512   launcher icon (purpose: maskable, safe-zone)
 *   icon-96.png              96x96    shortcut menu icon
 *   screenshot-narrow.png   540x960   manifest screenshot (form_factor: narrow)
 *   screenshot-wide.png    1280x720   manifest screenshot (form_factor: wide)
 *
 * Brand: navy #0B2E6E + white river-ripple motif. Screenshots are
 * stylised UI mockups of the portal so PWABuilder accepts them at
 * the correct aspect ratios required for store packaging.
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

// ── Palette ──────────────────────────────────────────────────────────
const NAVY     = [0x0B, 0x2E, 0x6E, 0xFF]; // brand background
const NAVY_DK  = [0x07, 0x1F, 0x4D, 0xFF];
const WHITE    = [0xFF, 0xFF, 0xFF, 0xFF];
const BG       = [0xF5, 0xF7, 0xFA, 0xFF]; // page background
const CARD     = [0xFF, 0xFF, 0xFF, 0xFF];
const CARD_BD  = [0xE2, 0xE6, 0xEC, 0xFF]; // card border
const TEXT     = [0xC8, 0xCE, 0xD6, 0xFF]; // pseudo text
const SOS_RED  = [0xD3, 0x2F, 0x2F, 0xFF];
const OK_GRN   = [0x2E, 0x7D, 0x32, 0xFF];
const WARN     = [0xF5, 0x9E, 0x0B, 0xFF];

// ── Logo glyph (3 concentric rings on navy) ──────────────────────────
function logoPixel(x, y, size, logoScale) {
  const cx = size / 2;
  const cy = size / 2;
  const dx = x - cx;
  const dy = y - cy;
  const r  = Math.sqrt(dx * dx + dy * dy);

  const outerR = (size / 2) * logoScale;
  if (r > outerR) return NAVY;

  const ringWidth = outerR * 0.08;
  for (let i = 0; i < 3; i++) {
    const ringR = outerR * (0.22 + i * 0.22);
    if (r >= ringR - ringWidth && r <= ringR + ringWidth) return NAVY;
  }
  return WHITE;
}

function generateIcon(filename, size, logoScale) {
  const buf = encodePng(size, size, (x, y) => logoPixel(x, y, size, logoScale));
  const outPath = path.join(OUT_DIR, filename);
  fs.writeFileSync(outPath, buf);
  console.log(`[pwa-pngs] ${filename}  ${size}x${size}  (${buf.length} bytes)`);
}

// ── Mini drawing primitives over a per-pixel "scene" function ────────
function inRect(x, y, x0, y0, w, h) {
  return x >= x0 && x < x0 + w && y >= y0 && y < y0 + h;
}

function inRoundRect(x, y, x0, y0, w, h, r) {
  if (!inRect(x, y, x0, y0, w, h)) return false;
  const xi = x - x0, yi = y - y0;
  // four corners — outside the corner square but inside the circle
  // means the pixel still belongs to the rounded rect.
  const corners = [
    [r, r],           // top-left
    [w - r, r],       // top-right
    [r, h - r],       // bottom-left
    [w - r, h - r],   // bottom-right
  ];
  for (const [cx, cy] of corners) {
    const inXBand = (xi < r && cx === r) || (xi > w - r && cx === w - r);
    const inYBand = (yi < r && cy === r) || (yi > h - r && cy === h - r);
    if (inXBand && inYBand) {
      const dx = xi - cx;
      const dy = yi - cy;
      if (dx * dx + dy * dy > r * r) return false;
    }
  }
  return true;
}

function inDisc(x, y, cx, cy, r) {
  const dx = x - cx, dy = y - cy;
  return dx * dx + dy * dy <= r * r;
}

// Build a small list of "shapes" and resolve per pixel by walking
// them top-to-bottom (last shape wins). Fast enough at our sizes.
function compose(width, height, bg, shapes) {
  return (x, y) => {
    let colour = bg;
    for (const s of shapes) {
      if (s.test(x, y)) colour = s.colour;
    }
    return colour;
  };
}

// ── Narrow (portrait) screenshot — phone-style UI mockup ─────────────
function generateNarrowScreenshot() {
  const W = 540, H = 960;

  const shapes = [];

  // Top header bar (navy)
  shapes.push({
    test: (x, y) => inRect(x, y, 0, 0, W, 110),
    colour: NAVY,
  });
  // White logo glyph in header (left)
  shapes.push({
    test: (x, y) => {
      if (!inDisc(x, y, 60, 55, 30)) return false;
      const dx = x - 60, dy = y - 55, r = Math.sqrt(dx*dx + dy*dy);
      // three white rings + white outer disc
      const outerR = 30;
      const ringWidth = outerR * 0.10;
      for (let i = 0; i < 3; i++) {
        const ringR = outerR * (0.22 + i * 0.22);
        if (r >= ringR - ringWidth && r <= ringR + ringWidth) return false;
      }
      return true;
    },
    colour: WHITE,
  });
  // Faux title bar (white pill where text would be)
  shapes.push({
    test: (x, y) => inRoundRect(x, y, 110, 35, 220, 18, 9),
    colour: WHITE,
  });
  shapes.push({
    test: (x, y) => inRoundRect(x, y, 110, 65, 140, 12, 6),
    colour: [0xCF, 0xD9, 0xEC, 0xFF],
  });

  // SOS pill in header (right)
  shapes.push({
    test: (x, y) => inRoundRect(x, y, W - 110, 35, 90, 40, 20),
    colour: SOS_RED,
  });
  // White "SOS" bars on the pill
  for (let i = 0; i < 3; i++) {
    const x0 = W - 95 + i * 18;
    shapes.push({
      test: (x, y) => inRoundRect(x, y, x0, 47, 12, 16, 2),
      colour: WHITE,
    });
  }

  // Status banner under header (green)
  shapes.push({
    test: (x, y) => inRoundRect(x, y, 30, 135, W - 60, 60, 12),
    colour: [0xE6, 0xF4, 0xEA, 0xFF],
  });
  shapes.push({
    test: (x, y) => inDisc(x, y, 60, 165, 10),
    colour: OK_GRN,
  });
  shapes.push({
    test: (x, y) => inRoundRect(x, y, 85, 152, 220, 12, 6),
    colour: TEXT,
  });
  shapes.push({
    test: (x, y) => inRoundRect(x, y, 85, 172, 140, 10, 5),
    colour: [0xDB, 0xE3, 0xEC, 0xFF],
  });

  // 5 ghat-status cards
  const cardX = 30, cardW = W - 60, cardH = 110, gap = 16;
  let y0 = 220;
  const ghatStatuses = [OK_GRN, OK_GRN, WARN, OK_GRN, SOS_RED];
  for (let i = 0; i < 5; i++) {
    const cy = y0 + i * (cardH + gap);
    // card body
    shapes.push({
      test: (x, y) => inRoundRect(x, y, cardX, cy, cardW, cardH, 14),
      colour: CARD,
    });
    // 1px-ish border
    shapes.push({
      test: (x, y) => {
        if (!inRoundRect(x, y, cardX, cy, cardW, cardH, 14)) return false;
        return !inRoundRect(x, y, cardX + 1, cy + 1, cardW - 2, cardH - 2, 13);
      },
      colour: CARD_BD,
    });
    // status disc
    shapes.push({
      test: (x, y) => inDisc(x, y, cardX + 35, cy + cardH / 2, 18),
      colour: ghatStatuses[i],
    });
    // title bar
    shapes.push({
      test: (x, y) => inRoundRect(x, y, cardX + 70, cy + 25, 280, 16, 8),
      colour: TEXT,
    });
    // sub line
    shapes.push({
      test: (x, y) => inRoundRect(x, y, cardX + 70, cy + 55, 200, 12, 6),
      colour: [0xE0, 0xE5, 0xEC, 0xFF],
    });
    // count pill (right)
    shapes.push({
      test: (x, y) => inRoundRect(x, y, cardX + cardW - 90, cy + 35, 70, 40, 12),
      colour: [0xEE, 0xF2, 0xF7, 0xFF],
    });
  }

  // Bottom tab-bar
  shapes.push({
    test: (x, y) => inRect(x, y, 0, H - 80, W, 80),
    colour: WHITE,
  });
  shapes.push({
    test: (x, y) => inRect(x, y, 0, H - 81, W, 1),
    colour: CARD_BD,
  });
  // 4 bottom-bar dots
  for (let i = 0; i < 4; i++) {
    const cx = 70 + i * 135;
    shapes.push({
      test: (x, y) => inDisc(x, y, cx, H - 40, 16),
      colour: i === 0 ? NAVY : [0xC0, 0xC8, 0xD2, 0xFF],
    });
  }

  const buf = encodePng(W, H, compose(W, H, BG, shapes));
  fs.writeFileSync(path.join(OUT_DIR, 'screenshot-narrow.png'), buf);
  console.log(`[pwa-pngs] screenshot-narrow.png  ${W}x${H}  (${buf.length} bytes)`);
}

// ── Wide (landscape) screenshot — desktop-style UI mockup ────────────
function generateWideScreenshot() {
  const W = 1280, H = 720;

  const shapes = [];

  // Top header bar (navy)
  shapes.push({
    test: (x, y) => inRect(x, y, 0, 0, W, 80),
    colour: NAVY,
  });
  // Logo glyph (left)
  shapes.push({
    test: (x, y) => {
      if (!inDisc(x, y, 50, 40, 22)) return false;
      const dx = x - 50, dy = y - 40, r = Math.sqrt(dx*dx + dy*dy);
      const outerR = 22;
      const ringWidth = outerR * 0.10;
      for (let i = 0; i < 3; i++) {
        const ringR = outerR * (0.22 + i * 0.22);
        if (r >= ringR - ringWidth && r <= ringR + ringWidth) return false;
      }
      return true;
    },
    colour: WHITE,
  });
  // Brand title pill
  shapes.push({
    test: (x, y) => inRoundRect(x, y, 90, 28, 280, 16, 8),
    colour: WHITE,
  });
  shapes.push({
    test: (x, y) => inRoundRect(x, y, 90, 50, 180, 10, 5),
    colour: [0xCF, 0xD9, 0xEC, 0xFF],
  });
  // Nav pills (right of header)
  for (let i = 0; i < 4; i++) {
    shapes.push({
      test: (x, y) => inRoundRect(x, y, W - 540 + i * 110, 28, 90, 24, 12),
      colour: [0x18, 0x40, 0x8A, 0xFF],
    });
  }
  // SOS pill (far right)
  shapes.push({
    test: (x, y) => inRoundRect(x, y, W - 100, 25, 80, 30, 15),
    colour: SOS_RED,
  });

  // Sidebar (left)
  shapes.push({
    test: (x, y) => inRect(x, y, 0, 80, 240, H - 80),
    colour: WHITE,
  });
  shapes.push({
    test: (x, y) => inRect(x, y, 240, 80, 1, H - 80),
    colour: CARD_BD,
  });
  // Sidebar items
  for (let i = 0; i < 6; i++) {
    const yi = 110 + i * 50;
    if (i === 0) {
      shapes.push({
        test: (x, y) => inRoundRect(x, y, 16, yi - 6, 208, 38, 10),
        colour: [0xE8, 0xEE, 0xFB, 0xFF],
      });
    }
    shapes.push({
      test: (x, y) => inDisc(x, y, 36, yi + 13, 9),
      colour: i === 0 ? NAVY : [0xC0, 0xC8, 0xD2, 0xFF],
    });
    shapes.push({
      test: (x, y) => inRoundRect(x, y, 56, yi + 7, 140, 12, 6),
      colour: i === 0 ? NAVY_DK : TEXT,
    });
  }

  // Main content area background
  shapes.push({
    test: (x, y) => inRect(x, y, 240, 80, W - 240, H - 80),
    colour: BG,
  });

  // KPI strip (3 wide cards at the top of main)
  for (let i = 0; i < 3; i++) {
    const cx = 270 + i * 320;
    const cy = 110;
    shapes.push({
      test: (x, y) => inRoundRect(x, y, cx, cy, 290, 110, 14),
      colour: CARD,
    });
    shapes.push({
      test: (x, y) => {
        if (!inRoundRect(x, y, cx, cy, 290, 110, 14)) return false;
        return !inRoundRect(x, y, cx + 1, cy + 1, 288, 108, 13);
      },
      colour: CARD_BD,
    });
    // big number bar
    shapes.push({
      test: (x, y) => inRoundRect(x, y, cx + 20, cy + 22, 130, 28, 6),
      colour: i === 0 ? OK_GRN : (i === 1 ? WARN : NAVY),
    });
    // label bar
    shapes.push({
      test: (x, y) => inRoundRect(x, y, cx + 20, cy + 65, 200, 12, 6),
      colour: TEXT,
    });
    shapes.push({
      test: (x, y) => inRoundRect(x, y, cx + 20, cy + 85, 140, 10, 5),
      colour: [0xE0, 0xE5, 0xEC, 0xFF],
    });
  }

  // Ghat-grid (6 cards in a 3x2 grid, bottom)
  const gx0 = 270, gy0 = 250, gw = 290, gh = 200, ggap = 30;
  const statuses = [OK_GRN, OK_GRN, WARN, OK_GRN, SOS_RED, OK_GRN];
  for (let i = 0; i < 6; i++) {
    const col = i % 3, row = Math.floor(i / 3);
    const cx = gx0 + col * (gw + ggap);
    const cy = gy0 + row * (gh + ggap);
    shapes.push({
      test: (x, y) => inRoundRect(x, y, cx, cy, gw, gh, 14),
      colour: CARD,
    });
    shapes.push({
      test: (x, y) => {
        if (!inRoundRect(x, y, cx, cy, gw, gh, 14)) return false;
        return !inRoundRect(x, y, cx + 1, cy + 1, gw - 2, gh - 2, 13);
      },
      colour: CARD_BD,
    });
    // status disc
    shapes.push({
      test: (x, y) => inDisc(x, y, cx + 40, cy + 40, 18),
      colour: statuses[i],
    });
    // title
    shapes.push({
      test: (x, y) => inRoundRect(x, y, cx + 75, cy + 32, 170, 16, 8),
      colour: TEXT,
    });
    // chart bars (5 short bars)
    for (let b = 0; b < 5; b++) {
      const bh = 30 + (b * 13) % 60;
      shapes.push({
        test: (x, y) => inRoundRect(x, y, cx + 25 + b * 50, cy + gh - bh - 25, 30, bh, 5),
        colour: statuses[i],
      });
    }
  }

  const buf = encodePng(W, H, compose(W, H, BG, shapes));
  fs.writeFileSync(path.join(OUT_DIR, 'screenshot-wide.png'), buf);
  console.log(`[pwa-pngs] screenshot-wide.png  ${W}x${H}  (${buf.length} bytes)`);
}

// ── Run ──────────────────────────────────────────────────────────────
if (!fs.existsSync(OUT_DIR)) {
  console.error('[pwa-pngs] missing dashboards/ at', OUT_DIR);
  process.exit(1);
}

generateIcon('icon-96.png',           96,  0.78); // shortcut icon
generateIcon('icon-192.png',          192, 0.78); // 'any' purpose
generateIcon('icon-512.png',          512, 0.78); // 'any' purpose
generateIcon('icon-maskable-512.png', 512, 0.55); // 'maskable' (safe zone)

generateNarrowScreenshot();
generateWideScreenshot();

console.log('[pwa-pngs] done');
