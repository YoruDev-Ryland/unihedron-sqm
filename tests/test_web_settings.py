import pytest

from sqm_service import main


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


def test_settings_require_sign_in(app_client):
    app_client.cookies.clear()
    assert app_client.get("/api/settings").status_code == 401


def test_get_and_update_settings(client):
    body = client.get("/api/settings").json()
    assert body["settings"]["mqtt"]["port"] == {"value": 1883, "locked": False}
    response = client.put("/api/settings/site", json={"latitude": "39.74", "longitude": "-104.99"})
    assert response.status_code == 200
    assert response.json()["latitude"]["value"] == 39.74


def test_invalid_setting_is_rejected(client):
    response = client.put("/api/settings/site", json={"latitude": "123"})
    assert response.status_code == 422
    assert response.json()["detail"]["field"] == "latitude"


def test_locked_setting_conflicts(client, monkeypatch):
    monkeypatch.setitem(main.settings._environ, "SQM_MQTT_HOST", "env-broker")
    response = client.put("/api/settings/mqtt", json={"host": "x"})
    assert response.status_code == 409


def test_unknown_section_is_not_found(client):
    assert client.put("/api/settings/nope", json={}).status_code == 404


def test_export_with_api_key(app_client):
    response = app_client.get("/api/export?format=csv", headers={"X-API-Key": "web-test-api-key"})
    assert response.status_code == 200
    assert response.headers["content-disposition"].startswith('attachment; filename="sqm-')
    assert response.text.startswith("utc_iso,")


def test_export_requires_auth(app_client):
    app_client.cookies.clear()
    assert app_client.get("/api/export").status_code == 401


def test_dashboard_reports_moon(client):
    client.put("/api/settings/site", json={"latitude": "", "longitude": ""})
    moon = client.get("/api/dashboard").json()["moon"]
    assert 0 <= moon["fraction"] <= 1 and isinstance(moon["waxing"], bool)
    assert moon["up"] is None  # no site set


def test_site_change_clears_the_darkness_cache(client):
    from sqm_service import annual

    annual._darkness_year(2025, 1.0, 2.0, "UTC", 2)
    assert annual._darkness_year.cache_info().currsize > 0
    client.put("/api/settings/site", json={"latitude": "", "longitude": ""})
    assert annual._darkness_year.cache_info().currsize == 0


def test_mqtt_settings_start_and_stop_publishing(client):
    client.put("/api/settings/mqtt", json={"host": "127.0.0.1", "port": "1"})
    assert client.get("/api/settings").json()["status"]["mqtt"] != "off"
    client.put("/api/settings/mqtt", json={"host": ""})
    assert client.get("/api/settings").json()["status"]["mqtt"] == "off"


def test_mqtt_follows_the_meter_serial(client, monkeypatch):
    calls = []
    monkeypatch.setattr(main.publisher, "configure", lambda config, serial, model, version: calls.append(serial))
    main.db.upsert_device_info(
        {"protocol": 4, "model": 3, "feature": 80, "serial": 5555, "raw": "i"}, "test"
    )
    main._mqtt_follow_serial(main.collector, {"mpsas": 21.0})
    main._mqtt_follow_serial(main.collector, {"mpsas": 21.0})
    assert calls == ["5555"]  # reconfigured once, not on every poll
