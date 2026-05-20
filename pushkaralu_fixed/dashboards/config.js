// ═══════════════════════════════════════════════════════════════════════════
// GODAVARI PUSHKARALU 2027 — SHARED BACKEND CONFIGURATION
// Always runs on http://localhost:8088 via Docker + Nginx
// ═══════════════════════════════════════════════════════════════════════════

const API_BASE = 'http://localhost:8088';
const WS_URL   = 'ws://localhost:8088/ws/volunteer';
// Admin API key — must match ADMIN_API_KEY in .env
// Used by admin.html to authenticate write operations (POST/PUT /lost, volunteer CRUD, etc.)
const ADMIN_API_KEY = '64de5b5ae7863eee64aa9a2c7e54455d2b74e46e7b7536fb15807b95210f203c';

console.log('[Config] API_BASE =', API_BASE);
console.log('[Config] WS_URL   =', WS_URL);
