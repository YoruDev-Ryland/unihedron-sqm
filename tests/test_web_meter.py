from __future__ import annotations

import contextlib

import pytest
from fastapi.testclient import TestClient

from sqm_service import main
from test_meter import FakeMeter


@pytest.fixture(scope="session")
def app_client():
    # The app closes its module-level database on shutdown, so it is started
    # once for the whole session.
    with TestClient(main.app) as test_client:
        yield test_client


@pytest.fixture
def client(app_client, monkeypatch):
    fake = FakeMeter()

    class FakeLink:
        send = staticmethod(fake.send)

        @staticmethod
        async def identify(timeout):
            from sqm_service.sqm_client import parse_info

            return parse_info(await fake.send(b"ix", lambda line: True, timeout))

    @contextlib.asynccontextmanager
    async def session():
        yield FakeLink()

    monkeypatch.setattr(main.collector, "session", session)
    monkeypatch.setattr(main.config, "sqm_transport", "ethernet")
    monkeypatch.setattr(main.config, "sqm_host", "192.0.2.9")
    auth = app_client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "Web-Test-Password-1"},
    ).json()
    app_client.headers["X-CSRF-Token"] = auth["csrf_token"]
    app_client.fake = fake
    yield app_client
    app_client.post("/api/auth/logout")
    app_client.headers.pop("X-CSRF-Token", None)
    app_client.cookies.clear()


def test_meter_details_require_sign_in(app_client):
    app_client.cookies.clear()
    assert app_client.get("/api/meter").status_code == 401


def test_meter_details(client):
    body = client.get("/api/meter").json()
    assert body["health"]["status"] == "ok"
    assert body["interval"]["enabled"] is True
    assert body["lock"]["locked"] is False
    assert main.db.device_info()["serial"] == 4171


def test_meter_details_need_a_configured_meter(client, monkeypatch):
    monkeypatch.setattr(main.config, "sqm_host", None)
    assert client.get("/api/meter").status_code == 409


def test_turn_interval_reporting_off(client):
    response = client.post(
        "/api/meter/interval", json={"period_s": 0, "permanent": True}
    )
    assert response.status_code == 200
    assert response.json()["enabled"] is False
    assert client.fake.eeprom[0] == 0


def test_interval_change_requires_csrf(client):
    del client.headers["X-CSRF-Token"]
    response = client.post("/api/meter/interval", json={"period_s": 0})
    assert response.status_code == 403


def test_locked_meter_returns_conflict(client):
    client.fake.replies[b"zcalDx"] = "zxdL"
    response = client.post("/api/meter/interval", json={"period_s": 0})
    assert response.status_code == 409
    assert "lock switch" in response.json()["detail"]
    assert client.fake.ram[0] == 5


def test_invalid_interval_is_rejected(client):
    response = client.post("/api/meter/interval", json={"period_s": -5})
    assert response.status_code == 422


def test_unreachable_meter_reports_health_instead_of_failing(client, monkeypatch):
    from sqm_service.sqm_client import SQMError

    @contextlib.asynccontextmanager
    async def refused():
        raise SQMError("connect to 192.0.2.9:10001 failed: refused")
        yield

    monkeypatch.setattr(main.collector, "session", refused)
    body = client.get("/api/meter").json()
    assert body["health"]["status"] == "unreachable"
    assert "refused" in body["health"]["message"]


def test_favicon_is_served(app_client):
    response = app_client.get("/favicon.ico")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
