// ── k6 Test 4: WebSocket connections ─────────────────────────────────────────
// Usage: k6 run tests/k6/test_4_ws.js
import ws from "k6/ws";
import { check, sleep } from "k6";

export const options = {
  stages: [
    { duration: "30s", target: 200  },
    { duration: "60s", target: 1000 },
    { duration: "30s", target: 0   },
  ],
  thresholds: {
    ws_session_duration: ["p(95)<65000"],
  },
};

const BASE_WS = (__ENV.BASE_URL || "ws://localhost").replace("http", "ws");
const GHAT_IDS = ["g01", "g02", "g03", "g04", "g05", "g06"];

export default function () {
  const ghatId = GHAT_IDS[__VU % GHAT_IDS.length];
  const url = `${BASE_WS}/ws/pilgrim/${ghatId}`;
  const res = ws.connect(url, {}, (socket) => {
    socket.on("open",    ()    => socket.setTimeout(() => socket.close(), 30000));
    socket.on("message", (msg) => {
      try {
        const data = JSON.parse(msg);
        check(data, { "has type": (d) => d.type !== undefined });
      } catch {}
    });
    socket.on("error", (e) => console.log("WS error:", e));
  });
  check(res, { "connected": (r) => r && r.status === 101 });
  sleep(1);
}
