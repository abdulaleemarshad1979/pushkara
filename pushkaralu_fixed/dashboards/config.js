// ═══════════════════════════════════════════════════════════════════════════
// GODAVARI PUSHKARALU 2027 — SHARED FRONTEND CONFIG
//
// SECURITY NOTE
// ─────────────
// This file is publicly served and visible to every browser. Therefore it
// MUST NOT contain secrets. The previous revision shipped a hard-coded
// ADMIN_API_KEY which gave any visitor full admin power — that key has been
// rotated and the constant removed.
//
// The admin dashboard now obtains its key at runtime via POST /admin/login
// (username + password against env-var credentials) and stores it ONLY in
// sessionStorage. The key clears when the tab closes.
// ═══════════════════════════════════════════════════════════════════════════

// API host. Override at runtime by defining `window.__API_BASE` BEFORE this
// file loads (e.g. in a small inline <script> for staging deployments).
const API_BASE     = (typeof window !== 'undefined' && window.__API_BASE)
                     || 'https://pushkara.onrender.com';
const WS_URL       = (typeof window !== 'undefined' && window.__WS_URL)
                     || 'wss://pushkara.onrender.com/ws/volunteer';
// Admin uses the same WS endpoint as volunteers — there is no /ws/admin.
const ADMIN_WS_URL = WS_URL;

// Admin API key — SESSION-SCOPED ONLY, never hard-coded.
// Populated by admin.html after a successful POST /admin/login.
// Reads from sessionStorage so a browser refresh keeps the operator logged in,
// but closing the tab forgets the key.
let ADMIN_API_KEY = '';
try {
  ADMIN_API_KEY = sessionStorage.getItem('pushkara_admin_key') || '';
} catch (_e) { /* sessionStorage may be unavailable in some sandboxes */ }

console.log('[Config] API_BASE =', API_BASE);
console.log('[Config] WS_URL   =', WS_URL);
console.log('[Config] ADMIN_API_KEY present =', !!ADMIN_API_KEY);
