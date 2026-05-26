#!/usr/bin/env node
/**
 * sync-web.mjs — copy ../dashboards/* into ./www/ and inject the
 * Capacitor native bridge into user.html.
 *
 * Run before `cap sync` (npm run android:sync does this automatically).
 *
 * Why we copy instead of symlink: Capacitor's `cap copy` reads webDir
 * from disk, and the GitHub Actions Windows/macOS runners don't always
 * preserve symlinks across CI cache. A plain copy is portable.
 */

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT      = path.resolve(__dirname, '..');                   // mobile-app/
const SRC       = path.resolve(ROOT, '..', 'dashboards');           // pushkaralu_fixed/dashboards/
const DST       = path.resolve(ROOT, 'www');                        // mobile-app/www/
const BRIDGE    = path.resolve(ROOT, 'src-bridge', 'capacitor-bridge.js');

const FILES = [
  'user.html',
  'manifest.json',
  'sw.js',
  'icon-192.svg',
  'icon-512.svg',
  'icon-maskable.svg',
  'sample_data.json',
  'config.js',
  'robots.txt',
];

function log(...args) { console.log('[sync-web]', ...args); }

if (!fs.existsSync(SRC)) {
  console.error(`[sync-web] ERROR: source dashboards/ not found at ${SRC}`);
  process.exit(1);
}
if (!fs.existsSync(BRIDGE)) {
  console.error(`[sync-web] ERROR: native bridge not found at ${BRIDGE}`);
  process.exit(1);
}

// Wipe + recreate www/ so stale files never linger between builds.
fs.rmSync(DST, { recursive: true, force: true });
fs.mkdirSync(DST, { recursive: true });

for (const f of FILES) {
  const src = path.join(SRC, f);
  if (!fs.existsSync(src)) {
    log(`skip missing ${f}`);
    continue;
  }
  fs.copyFileSync(src, path.join(DST, f));
  log(`copied ${f}`);
}

// ── Inject the Capacitor bridge into user.html ──────────────────────
const userHtmlPath = path.join(DST, 'user.html');
let html = fs.readFileSync(userHtmlPath, 'utf8');

const injectBlock = [
  '<!-- Capacitor native bridge — injected by mobile-app/scripts/sync-web.mjs.',
  '     No-op in regular browsers; activates native plugins in the APK/IPA. -->',
  '<script src="capacitor-bridge.js" defer></script>',
].join('\n');

if (!html.includes('capacitor-bridge.js')) {
  html = html.replace(/<\/head>/i, `${injectBlock}\n</head>`);
  fs.writeFileSync(userHtmlPath, html);
  log('injected capacitor-bridge.js into user.html');
}

// Copy bridge into www/ for serving.
fs.copyFileSync(BRIDGE, path.join(DST, 'capacitor-bridge.js'));
log('copied capacitor-bridge.js');

// ── Patch sw.js so the bundled paths are pre-cached ─────────────────
// Original sw.js precaches /user (a server route). The native bundle
// serves user.html directly, so we add /index.html and /user.html to
// the precache list. /user is kept for compatibility when the same
// sw.js is served at the production domain.
const swPath = path.join(DST, 'sw.js');
if (fs.existsSync(swPath)) {
  let sw = fs.readFileSync(swPath, 'utf8');
  if (!sw.includes("'/user.html'")) {
    sw = sw.replace(
      /const SHELL_URLS\s*=\s*\[\s*\n\s*'\/user',/,
      `const SHELL_URLS = [\n  '/index.html',\n  '/user.html',\n  '/user',`,
    );
    sw = sw.replace(
      /caches\.match\('\/user'\)/g,
      `caches.match('/user.html') || caches.match('/user')`,
    );
    fs.writeFileSync(swPath, sw);
    log('patched sw.js with bundled paths');
  }
}

// ── index.html — entry point Capacitor loads first ─────────────────
// Forwards to user.html preserving any deep-link hash.
const indexHtml = `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#0B2E6E">
<title>Pushkaralu</title>
<style>
  html,body{margin:0;padding:0;height:100%;background:#0B2E6E;color:#fff;font-family:system-ui,-apple-system,sans-serif}
  .boot{display:flex;align-items:center;justify-content:center;height:100%;flex-direction:column;gap:14px}
  .boot .ring{width:42px;height:42px;border:3px solid rgba(255,255,255,.25);border-top-color:#F0A830;border-radius:50%;animation:spin 1s linear infinite}
  @keyframes spin{to{transform:rotate(360deg)}}
  .boot p{margin:0;font-size:14px;opacity:.8;letter-spacing:.5px}
</style>
<script>
  // Forward any deep-link hash (e.g. #sos) to the real shell.
  (function(){
    var hash = location.hash || '';
    location.replace('user.html' + hash);
  })();
</script>
<noscript><meta http-equiv="refresh" content="0; url=user.html"></noscript>
</head>
<body>
  <div class="boot"><div class="ring"></div><p>Loading Pushkaralu…</p></div>
</body>
</html>
`;
fs.writeFileSync(path.join(DST, 'index.html'), indexHtml);
log('wrote index.html boot redirect');

log('done — www/ is ready for `cap sync`');
