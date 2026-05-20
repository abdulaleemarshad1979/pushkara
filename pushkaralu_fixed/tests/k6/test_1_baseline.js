// ── k6 Test 1: Baseline health check ─────────────────────────────────────────
// Usage: k6 run tests/k6/test_1_baseline.js
import http from "k6/http";
import { check, sleep } from "k6";

export const options = {
  vus: 10,
  duration: "30s",
  thresholds: {
    http_req_duration: ["p(95)<200"],
    http_req_failed:   ["rate<0.01"],
  },
};

const BASE = __ENV.BASE_URL || "http://localhost";

export default function () {
  const res = http.get(`${BASE}/health`);
  check(res, {
    "status is 200 or 503": (r) => r.status === 200 || r.status === 503,
    "has version field":     (r) => JSON.parse(r.body).version !== undefined,
  });
  sleep(1);
}
