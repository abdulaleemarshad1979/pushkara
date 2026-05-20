// ── k6 Test 2: Read endpoints under load ─────────────────────────────────────
// Usage: k6 run tests/k6/test_2_read.js
import http from "k6/http";
import { check, sleep } from "k6";

export const options = {
  stages: [
    { duration: "30s", target: 100  },   // ramp up
    { duration: "60s", target: 500  },   // sustained
    { duration: "30s", target: 1000 },   // peak
    { duration: "30s", target: 0   },    // ramp down
  ],
  thresholds: {
    http_req_duration: ["p(95)<500", "p(99)<1000"],
    http_req_failed:   ["rate<0.02"],
  },
};

const BASE = __ENV.BASE_URL || "http://localhost";  // hits NGINX on 80

const ENDPOINTS = [
  "/get_ghats",
  "/get_issues",
  "/get_sos_alerts",
  "/stats",
  "/get_facilities",
  "/get_transport",
  "/medical",
  "/contacts",
  "/crowd/risk/all",
];

export default function () {
  const url = BASE + ENDPOINTS[Math.floor(Math.random() * ENDPOINTS.length)];
  const res = http.get(url);
  check(res, {
    "status 200": (r) => r.status === 200,
    "response < 500ms": (r) => r.timings.duration < 500,
  });
  sleep(0.5);
}
