import json

from sqm_service import alerts
from sqm_service.alerts import AlertState

H = 3600


def test_alerts_once_when_offline_too_long_then_recovers_once():
    state = AlertState()
    event, state = alerts.evaluate(now=10 * H, last_success=9 * H, started=0, offline_hours=2, state=state)
    assert event is None
    event, state = alerts.evaluate(now=11.5 * H, last_success=9 * H, started=0, offline_hours=2, state=state)
    assert event == "offline" and state == AlertState(True, 9 * H)
    event, state = alerts.evaluate(now=12 * H, last_success=9 * H, started=0, offline_hours=2, state=state)
    assert event is None  # no repeat
    event, state = alerts.evaluate(now=13 * H, last_success=12.9 * H, started=0, offline_hours=2, state=state)
    assert event == "recovered" and state == AlertState(False, None)


def test_never_succeeded_counts_from_startup():
    event, _ = alerts.evaluate(now=1 * H, last_success=None, started=0, offline_hours=2, state=AlertState())
    assert event is None
    event, _ = alerts.evaluate(now=3 * H, last_success=None, started=0, offline_hours=2, state=AlertState())
    assert event == "offline"


def test_json_request():
    request = alerts.build_request("offline", "json", "https://hook.example/x", {"serial": "4171", "message": "m"})
    assert request.get_method() == "POST"
    assert request.get_header("Content-type") == "application/json"
    assert json.loads(request.data)["event"] == "offline"


def test_ntfy_request():
    request = alerts.build_request("recovered", "ntfy", "https://ntfy.sh/sqm", {"serial": "4171", "message": "Back online"})
    assert request.data == b"Back online"
    assert request.get_header("Title") == "SQM 4171 is collecting again"
    assert request.get_header("Tags") == "white_check_mark"


def test_delivery_failure_is_reported_not_raised():
    request = alerts.build_request("offline", "json", "http://127.0.0.1:9/nope", {"serial": "x", "message": "m"})
    ok, detail = alerts.deliver(request, timeout=1)
    assert ok is False and detail
