#!/usr/bin/env python3
"""Black-box smoke test for a running disposable container."""

from __future__ import annotations

import http.cookiejar
import json
import os
import urllib.error
import urllib.request
import uuid

BASE_URL = os.environ.get("SQM_SMOKE_URL", "http://127.0.0.1:17942")
PASSWORD = os.environ.get("SQM_SMOKE_PASSWORD", "SmokeTest-Only-Password-7942!")

cookies = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookies))


def request(
    path: str,
    method: str = "GET",
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, bytes]:
    target = urllib.request.Request(
        BASE_URL + path, data=body, headers=headers or {}, method=method
    )
    try:
        response = opener.open(target, timeout=10)
        return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


status, payload = request("/health")
assert status == 200
health = json.loads(payload)
assert health["status"] == "ok"
assert health["configured"] is False

status, payload = request("/")
assert status == 200 and b"Sky quality" in payload
status, payload = request("/assets/app.js")
assert status == 200 and b"loadDashboard" in payload

status, payload = request("/api/auth/status")
assert status == 200 and json.loads(payload)["authenticated"] is False

status, payload = request(
    "/api/auth/login",
    method="POST",
    body=json.dumps({"username": "admin", "password": PASSWORD}).encode(),
    headers={"Content-Type": "application/json"},
)
assert status == 200, payload
login = json.loads(payload)
csrf = login["csrf_token"]

status, payload = request("/api/dashboard?hours=24")
assert status == 200
assert json.loads(payload)["collector"]["state"] == "unconfigured"

boundary = "----sqm-smoke-" + uuid.uuid4().hex
sample = (
    b"# Light Pollution Monitoring Data Format 1.0\n"
    b"2024-01-02T00:00:00.083;2024-01-01T18:00:00.083;"
    b"24.4;0;556911;18.20\n"
)
multipart = (
    f"--{boundary}\r\n"
    'Content-Disposition: form-data; name="files"; filename="smoke.dat"\r\n'
    "Content-Type: text/plain\r\n\r\n"
).encode() + sample + f"\r\n--{boundary}--\r\n".encode()
status, payload = request(
    "/api/import/upload",
    method="POST",
    body=multipart,
    headers={
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "X-CSRF-Token": csrf,
    },
)
assert status == 200, payload
import_result = json.loads(payload)
assert import_result["imported"] == 1

status, payload = request("/api/annual?year=2024")
assert status == 200
annual = json.loads(payload)
assert annual["days"] == 366
assert annual["observed_nights"] == 1
assert len(annual["cells"]) == 1

status, _ = request("/api/latest")
assert status == 401

status, payload = request(
    "/api/account/password",
    method="POST",
    body=json.dumps(
        {
            "current_password": PASSWORD,
            "new_password": "Changed-Smoke-Password-7942!",
        }
    ).encode(),
    headers={"Content-Type": "application/json", "X-CSRF-Token": csrf},
)
assert status == 200, payload

status, payload = request("/api/auth/status")
assert status == 200 and json.loads(payload)["authenticated"] is False

print("HTTP smoke test passed: health, assets, login, dashboard, annual map, import, API auth, password change")
