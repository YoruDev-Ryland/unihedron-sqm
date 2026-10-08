"""Route coverage for the parts of the web API not exercised elsewhere."""

from __future__ import annotations

import pytest

from sqm_service import main, sqm_client

KEY = {"X-API-Key": "web-test-api-key"}


@pytest.fixture
def client(app_client):
    auth = app_client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "Web-Test-Password-1"},
    ).json()
    app_client.headers["X-CSRF-Token"] = auth["csrf_token"]
    yield app_client
    app_client.post("/api/auth/logout")
    app_client.headers.pop("X-CSRF-Token", None)
    app_client.cookies.clear()


def test_index_and_health_need_no_sign_in(app_client):
    app_client.cookies.clear()
    assert "<title>Unihedron SQM</title>" in app_client.get("/").text
    health = app_client.get("/health").json()
    assert health["status"] == "ok" and health["version"] == main.__version__


def test_auth_status_and_logout(client):
    status = client.get("/api/auth/status").json()
    assert status["authenticated"] is True and status["username"] == "admin"
    assert client.post("/api/auth/logout").json() == {"ok": True}
    client.cookies.clear()
    assert client.get("/api/auth/status").json() == {"authenticated": False}


def test_wrong_password_is_rejected(app_client):
    response = app_client.post("/api/auth/login", json={"username": "admin", "password": "nope"})
    assert response.status_code == 401


def test_config_and_annual_need_sign_in(app_client):
    app_client.cookies.clear()
    assert app_client.get("/api/config").status_code == 401
    assert app_client.get("/api/annual").status_code == 401


def test_config_and_annual(client):
    assert client.get("/api/config").json()["poll_interval"] == main.config.poll_interval
    annual = client.get("/api/annual?year=2025").json()
    assert annual["year"] == 2025 and annual["slots"] == 34


def test_json_api_needs_the_key(app_client):
    app_client.cookies.clear()
    for path in ("/api/latest", "/api/readings", "/api/stats", "/api/device"):
        assert app_client.get(path).status_code == 401, path


def test_json_api_with_the_key(app_client):
    main.db.insert_reading(
        {"mpsas": 21.09, "frequency_hz": 3, "period_counts": 127534,
         "period_seconds": 0.276, "temperature_c": 8.4, "raw": "r"},
        2_000_000_000.0,
    )
    latest = app_client.get("/api/latest", headers=KEY).json()
    assert latest["mpsas"] == 21.09 and latest["bortle"] == 4
    readings = app_client.get("/api/readings?limit=1", headers=KEY).json()
    assert readings["count"] == 1
    assert app_client.get("/api/stats?hours=1", headers=KEY).status_code == 200
    assert app_client.get("/api/device", headers=KEY).status_code in (200, 404)


def test_import_upload_and_history(client):
    log = b"# header\n2025-01-06T03:00:00.000;2025-01-05T20:00:00.000;8.5;0;3;21.04\n"
    response = client.post("/api/import/upload", files={"files": ("night.dat", log)})
    assert response.status_code == 200 and response.json()["parsed"] == 1
    history = client.get("/api/imports").json()["imports"]
    assert history[0]["filename"] == "night.dat"


def test_import_rejects_other_file_types(client):
    response = client.post("/api/import/upload", files={"files": ("notes.txt", b"x")})
    assert response.status_code == 415


def test_import_directory_needs_a_mount(client):
    assert client.post("/api/import/directory").status_code == 404


def test_password_change_checks_the_current_password(client):
    response = client.post(
        "/api/account/password",
        json={"current_password": "wrong-password", "new_password": "Another-Password-1"},
    )
    assert response.status_code == 400


def test_device_test_reports_an_unreachable_meter(client, monkeypatch):
    async def unreachable(*args, **kwargs):
        raise sqm_client.SQMError("connect to 192.0.2.9:10001 failed: timed out")

    monkeypatch.setattr(sqm_client, "get_info", unreachable)
    response = client.post("/api/device/test", json={"transport": "ethernet", "host": "192.0.2.9"})
    assert response.status_code == 502
    assert "timed out" in response.json()["detail"]


def test_device_save_rejects_a_bad_host(client):
    response = client.post("/api/device", json={"transport": "ethernet", "host": "bad host/../"})
    assert response.status_code == 422


def test_device_save(client, monkeypatch):
    saved = []
    monkeypatch.setattr(main.collector, "configure_ethernet", lambda host, port: saved.append((host, port)))
    monkeypatch.setattr(main.db, "set_setting", lambda key, value: None)
    monkeypatch.setattr(main.db, "clear_device_info", lambda: None)
    response = client.post("/api/device", json={"transport": "ethernet", "host": "192.0.2.9", "port": 10001})
    assert response.status_code == 200 and saved == [("192.0.2.9", 10001)]


def test_discovery_routes(client, monkeypatch):
    monkeypatch.setattr(main.discovery, "broadcast_discover", lambda address, timeout: [])
    assert client.post("/api/discover", json={}).json()["count"] == 0

    async def none_found(**kwargs):
        return []

    monkeypatch.setattr(sqm_client, "discover_serial_devices", none_found)
    assert client.post("/api/discover/serial").json() == {"count": 0, "devices": []}


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("put", "/api/settings/site", {"latitude": "1"}),
        ("post", "/api/settings/alerts/test", None),
        ("post", "/api/meter/logger/download", None),
        ("post", "/api/meter/interval", {"period_s": 0}),
        ("post", "/api/device", {"transport": "ethernet", "host": "192.0.2.9"}),
        ("post", "/api/account/password", {"current_password": "x", "new_password": "y" * 12}),
    ],
)
def test_state_changes_need_the_csrf_token(client, method, path, body):
    del client.headers["X-CSRF-Token"]
    response = getattr(client, method)(path, json=body) if body is not None else getattr(client, method)(path)
    assert response.status_code == 403
