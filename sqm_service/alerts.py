"""Tell someone when the meter has stopped answering, and when it is back."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass

TITLES = {
    "offline": "SQM {serial} has stopped collecting",
    "recovered": "SQM {serial} is collecting again",
    "test": "SQM {serial} alert test",
}
TAGS = {"offline": "warning", "recovered": "white_check_mark", "test": "bell"}


@dataclass
class AlertState:
    alerting: bool = False
    since: float | None = None


def evaluate(now, last_success, started, offline_hours, state):
    reference = last_success if last_success is not None else started
    if not state.alerting:
        if now - reference > offline_hours * 3600:
            return "offline", AlertState(True, reference)
        return None, state
    if last_success is not None and last_success > (state.since or 0):
        return "recovered", AlertState()
    return None, state


def build_request(event: str, fmt: str, url: str, context: dict) -> urllib.request.Request:
    if fmt == "ntfy":
        request = urllib.request.Request(url, data=context["message"].encode(), method="POST")
        request.add_header("Title", TITLES[event].format(serial=context.get("serial", "")))
        request.add_header("Tags", TAGS[event])
        request.add_header("Priority", "high" if event == "offline" else "default")
        return request
    body = json.dumps({"event": event, **context}).encode()
    request = urllib.request.Request(url, data=body, method="POST")
    request.add_header("Content-Type", "application/json")
    return request


def deliver(request: urllib.request.Request, timeout: float = 10) -> tuple[bool, str]:
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return 200 <= response.status < 300, f"HTTP {response.status}"
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}"
    except Exception as exc:  # network errors of every kind
        return False, str(exc) or exc.__class__.__name__
