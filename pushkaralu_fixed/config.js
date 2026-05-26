// ═══════════════════════════════════════════════════════════════════════════
// GODAVARI PUSHKARALU 2027 — LOCAL DEV / NGINX CONFIG
//
// Used when the dashboards are served via the dockerised Nginx on
// http://localhost:8088. SECRETS MUST NOT live in this file — see the
// security note in dashboards/config.js for why.
// ═══════════════════════════════════════════════════════════════════════════

const API_BASE = (typeof window !== 'undefined' && window.__API_BASE)
                 || 'http://localhost:8088';
const WS_URL   = (typeof window !== 'undefined' && window.__WS_URL)
                 || 'ws://localhost:8088/ws/volunteer';

// SESSION-scoped only. Populated by /admin/login flow in admin.html.
let ADMIN_API_KEY = '';
try {
  ADMIN_API_KEY = sessionStorage.getItem('pushkara_admin_key') || '';
} catch (_e) { /* noop */ }

console.log('[Config] API_BASE =', API_BASE);
console.log('[Config] WS_URL   =', WS_URL);
