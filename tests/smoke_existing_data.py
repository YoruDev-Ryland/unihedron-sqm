#!/usr/bin/env python3
"""Check that the copied legacy database opens and renders through the new app."""

from __future__ import annotations

import http.cookiejar
import json
import os
import urllib.request

BASE_URL = os.environ.get("SQM_SMOKE_URL", "http://127.0.0.1:17943")
PASSWORD = os.environ.get("SQM_SMOKE_PASSWORD", "SmokeTest-Only-Password-7942!")

cookies = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookies))


def json_request(path: str, body: dict | None = None) -> dict:
    request = urllib.request.Request(
        BASE_URL + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"} if body is not None else {},
        method="POST" if body is not None else "GET",
    )
    with opener.open(request, timeout=20) as response:
        assert response.status == 200
        return json.loads(response.read())


health = json_request("/health")
assert health["stored_readings"] > 700_000

login = json_request(
    "/api/auth/login", {"username": "admin", "password": PASSWORD}
)
assert login["authenticated"] is True

dashboard = json_request("/api/dashboard?hours=43800")
assert dashboard["latest"] is not None
assert dashboard["stats"]["count"] > 700_000
assert 1 < len(dashboard["chart"]) <= 1601
assert dashboard["device"] is not None

annual = json_request("/api/annual?year=2025")
assert annual["year"] == 2025
assert annual["days"] == 365
assert annual["observed_nights"] > 100
assert len(annual["cells"]) > 3_000

print(
    "Existing-data smoke test passed: "
    f"{health['stored_readings']:,} rows, "
    f"{len(dashboard['chart']):,} chart points, "
    f"{len(annual['cells']):,} annual cells, "
    f"latest={dashboard['latest']['timestamp']}"
)
