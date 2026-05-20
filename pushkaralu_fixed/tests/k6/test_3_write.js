// ── k6 Test 3: Write endpoints ────────────────────────────────────────────────
// Usage: k6 run tests/k6/test_3_write.js
import http from "k6/http";
import { check, sleep } from "k6";

export const options = {
  stages: [
    { duration: "30s", target: 50  },
    { duration: "60s", target: 200 },
    { duration: "30s", target: 0  },
  ],
  thresholds: {
    http_req_duration: ["p(95)<1000"],
    http_req_failed:   ["rate<0.05"],
  },
};

const BASE = __ENV.BASE_URL || "http://localhost";

export default function () {
  // SOS alert
  const sosRes = http.post(`${BASE}/sos_alert`, {
    user_name: `k6user_${__VU}`,
    latitude: "16.9891",
    longitude: "81.7873",
    phone: `900000${__VU}`,
  });
  check(sosRes, {
    "SOS 200": (r) => r.status === 200,
    "SOS has alert_id": (r) => {
      try { return JSON.parse(r.body).alert_id !== undefined; } catch { return false; }
    },
  });
  sleep(2);
}
