import pytest

from sqm_service.db import Database
from sqm_service.settings import SettingLocked, Settings, SettingsError


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "sqm.db")
    yield database
    database.close()


def test_defaults(db):
    settings = Settings(db, environ={})
    assert settings.get("mqtt", "port") == 1883
    assert settings.get("mqtt", "discovery") is True
    assert settings.get("site", "latitude") is None
    assert settings.get("alerts", "offline_hours") == 2.0


def test_update_persists_and_coerces(db):
    Settings(db, environ={}).update("mqtt", {"host": "broker.lan", "port": "1884", "tls": "true"})
    reloaded = Settings(db, environ={})
    assert reloaded.get("mqtt", "host") == "broker.lan"
    assert reloaded.get("mqtt", "port") == 1884
    assert reloaded.get("mqtt", "tls") is True


def test_environment_overrides_and_locks(db):
    settings = Settings(db, environ={"SQM_MQTT_HOST": "env-broker"})
    assert settings.get("mqtt", "host") == "env-broker"
    assert settings.public()["mqtt"]["host"] == {"value": "env-broker", "locked": True}
    with pytest.raises(SettingLocked):
        settings.update("mqtt", {"host": "other"})


def test_secret_file_variant(db, tmp_path):
    secret = tmp_path / "pw"
    secret.write_text("s3cret\n")
    settings = Settings(db, environ={"SQM_MQTT_PASSWORD_FILE": str(secret)})
    assert settings.get("mqtt", "password") == "s3cret"


def test_secrets_are_write_only(db):
    settings = Settings(db, environ={})
    settings.update("mqtt", {"password": "hunter2"})
    assert settings.public()["mqtt"]["password"] == {"set": True, "locked": False}
    # Omitting the secret keeps it.
    settings.update("mqtt", {"host": "broker.lan"})
    assert settings.get("mqtt", "password") == "hunter2"
    # An explicit empty string clears it.
    settings.update("mqtt", {"password": ""})
    assert settings.get("mqtt", "password") == ""
    assert settings.public()["mqtt"]["password"]["set"] is False


@pytest.mark.parametrize(
    ("section", "values"),
    [
        ("site", {"latitude": "91"}),
        ("site", {"longitude": "-181"}),
        ("mqtt", {"port": "0"}),
        ("alerts", {"offline_hours": "0.1"}),
        ("alerts", {"format": "email"}),
        ("mqtt", {"nonsense": "1"}),
    ],
)
def test_validation(db, section, values):
    with pytest.raises(SettingsError):
        Settings(db, environ={}).update(section, values)


def test_blank_optional_number_means_unset(db):
    settings = Settings(db, environ={})
    settings.update("site", {"latitude": "39.7", "longitude": "-105"})
    settings.update("site", {"latitude": "", "longitude": ""})
    assert settings.get("site", "latitude") is None


def test_change_callback_receives_section(db):
    seen = []
    settings = Settings(db, environ={})
    settings.on_change("alerts", seen.append)
    settings.update("alerts", {"url": "https://ntfy.sh/x"})
    assert seen[-1]["url"] == "https://ntfy.sh/x"
