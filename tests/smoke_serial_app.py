#!/usr/bin/env python3
"""Run the complete web collector against a pseudo-terminal SQM-LU."""

from __future__ import annotations

import http.cookiejar
import json
import os
import subprocess
import tempfile
import time
import urllib.error
import urllib.request

from fake_serial import FakeSerialSQM


PORT = 17944
BASE_URL = f"http://127.0.0.1:{PORT}"
PASSWORD = "Serial-Smoke-Password-7942!"


def request(
    opener: urllib.request.OpenerDirector,
    path: str,
    method: str = "GET",
    body: dict | None = None,
    csrf: str | None = None,
) -> tuple[int, dict]:
    headers = {}
    payload = None
    if body is not None:
        payload = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    if csrf:
        headers["X-CSRF-Token"] = csrf
    target = urllib.request.Request(
        BASE_URL + path,
        data=payload,
        headers=headers,
        method=method,
    )
    try:
        with opener.open(target, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def start_server(environment: dict[str, str]) -> subprocess.Popen:
    return subprocess.Popen(
        [
            "python",
            "-m",
            "uvicorn",
            "sqm_service.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(PORT),
        ],
        env=environment,
    )


def stop_server(server: subprocess.Popen) -> None:
    server.terminate()
    try:
        server.wait(timeout=5)
    except subprocess.TimeoutExpired:
        server.kill()
        server.wait(timeout=5)


def wait_for_health(
    opener: urllib.request.OpenerDirector,
    collector_state: str,
    minimum_readings: int = 0,
) -> dict:
    deadline = time.monotonic() + 15
    health: dict = {}
    while time.monotonic() < deadline:
        try:
            status, health = request(opener, "/health")
            if (
                status == 200
                and health["collector"] == collector_state
                and health["stored_readings"] >= minimum_readings
            ):
                return health
        except (OSError, urllib.error.URLError):
            pass
        time.sleep(0.2)
    raise AssertionError(f"collector did not reach {collector_state}: {health}")


with FakeSerialSQM() as meter, tempfile.TemporaryDirectory() as data_dir:
    environment = os.environ.copy()
    environment.update(
        {
            "ADMIN_PASSWORD": PASSWORD,
            "DATA_DIR": data_dir,
            "DB_PATH": os.path.join(data_dir, "sqm.db"),
            "PUBLIC_URL": BASE_URL,
            "SQM_BAUD_RATE": "115200",
            "SQM_POLL_INTERVAL": "5",
            "SQM_READ_TIMEOUT": "2",
            "SQM_SERIAL_DEVICE": meter.path,
            # Start unconfigured so the API test follows the same flow as the UI.
            "SQM_TRANSPORT": "",
            "WEB_PORT": str(PORT),
        }
    )
    cookies = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(cookies)
    )
    server = start_server(environment)
    try:
        wait_for_health(opener, "unconfigured")

        status, login = request(
            opener,
            "/api/auth/login",
            method="POST",
            body={"username": "admin", "password": PASSWORD},
        )
        assert status == 200, login

        status, discovered = request(
            opener,
            "/api/discover/serial",
            method="POST",
            body={},
            csrf=login["csrf_token"],
        )
        assert status == 200, discovered
        assert discovered["count"] == 1
        assert discovered["devices"][0]["device"]["serial"] == 1234

        serial_body = {
            "transport": "serial",
            "serial_device": meter.path,
            "baud_rate": 115200,
        }
        status, tested = request(
            opener,
            "/api/device/test",
            method="POST",
            body=serial_body,
            csrf=login["csrf_token"],
        )
        assert status == 200, tested
        assert tested["device"]["serial"] == 1234

        status, saved = request(
            opener,
            "/api/device",
            method="POST",
            body=serial_body,
            csrf=login["csrf_token"],
        )
        assert status == 200, saved
        wait_for_health(opener, "collecting", minimum_readings=1)

        status, dashboard = request(opener, "/api/dashboard")
        assert status == 200, dashboard
        assert dashboard["collector"]["transport"] == "serial"
        assert dashboard["collector"]["endpoint"] == meter.path
        assert dashboard["device"]["serial"] == 1234
        assert dashboard["latest"]["mpsas"] == 21.37
    finally:
        stop_server(server)

    # A blank transport environment allows the saved UI selection to survive.
    server = start_server(environment)
    try:
        health = wait_for_health(opener, "collecting", minimum_readings=2)
        assert health["configured"] is True
        status, persisted = request(opener, "/api/config")
        assert status == 200, persisted
        assert persisted["transport"] == "serial"
        assert persisted["serial_device"] == meter.path
    finally:
        stop_server(server)

    print(
        "Serial app smoke test passed: discovery, connection test, saved "
        "configuration, restart persistence, collection, database write, "
        "and dashboard API"
    )
