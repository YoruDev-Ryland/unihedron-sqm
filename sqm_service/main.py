"""FastAPI application for the Unihedron SQM collector and dashboard."""

from __future__ import annotations

import asyncio
import hmac
import ipaddress
import logging
import os
import re
import secrets
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import (
    Depends,
    FastAPI,
    File,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__, astro, discovery, exporter, meter, metrics, mqtt, sky, sqm_client
from . import annual as annual_module
from .annual import available_years, build_annual_map, timezone_for
from .collector import Collector
from .config import Config
from .db import Database
from .importer import import_text
from .settings import SECTIONS, SettingLocked, Settings, SettingsError
from .security import (
    generate_password,
    generate_token,
    hash_password,
    hash_token,
    verify_password,
)

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("sqm")

SESSION_COOKIE = "sqm_session"
STATIC_DIR = Path(__file__).with_name("static")
HOST_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,252}$")

config = Config()
db = Database(config.db_path)

stored_transport = db.get_setting("sqm_transport")
if (
    stored_transport in {"ethernet", "serial"}
    and not os.environ.get("SQM_TRANSPORT")
):
    config.sqm_transport = stored_transport
if not config.sqm_host:
    config.sqm_host = db.get_setting("sqm_host")
stored_port = db.get_setting("sqm_port")
if stored_port and not os.environ.get("SQM_PORT"):
    try:
        config.sqm_port = int(stored_port)
    except ValueError:
        log.warning("ignoring invalid stored SQM port")
if not config.sqm_serial_device:
    config.sqm_serial_device = db.get_setting("sqm_serial_device")
stored_baud_rate = db.get_setting("sqm_baud_rate")
if stored_baud_rate and not os.environ.get("SQM_BAUD_RATE"):
    try:
        config.sqm_baud_rate = int(stored_baud_rate)
    except ValueError:
        log.warning("ignoring invalid stored SQM baud rate")

connection_lock = asyncio.Lock()
collector = Collector(config, db, connection_lock)
settings = Settings(db, environ=dict(os.environ))


def _site() -> tuple[float, float] | None:
    lat = settings.get("site", "latitude")
    lon = settings.get("site", "longitude")
    return (lat, lon) if lat is not None and lon is not None else None


settings.on_change("site", lambda _section: annual_module.clear_cache())

publisher = mqtt.MqttPublisher()


def _meter_identity() -> tuple[str, str]:
    info = db.device_info()
    serial = str(info["serial"]) if info and info.get("serial") else "meter"
    model = str(info["model"]) if info and info.get("model") is not None else "unknown"
    return serial, model


_mqtt_identity: tuple[str, str] | None = None


def _configure_mqtt(_section: dict | None = None) -> None:
    global _mqtt_identity
    _mqtt_identity = _meter_identity()
    publisher.configure(settings.section("mqtt"), *_mqtt_identity, __version__)


def _mqtt_follow_serial(_collector, reading: dict | None) -> None:
    # The meter may be identified after MQTT was configured (a fresh install,
    # or a different meter); move the topics to the new serial once.
    if reading is not None and _meter_identity() != _mqtt_identity:
        _configure_mqtt()


settings.on_change("mqtt", _configure_mqtt)
# Follow a new serial before publishing, so no reading goes to the old topic.
collector.add_listener(_mqtt_follow_serial)
collector.add_listener(publisher.on_poll)
login_attempts: dict[str, deque[float]] = defaultdict(deque)
DUMMY_PASSWORD_HASH = hash_password(generate_password())


def _write_private_file(path: Path, content: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(content)


def _bootstrap() -> None:
    config.data_dir.mkdir(parents=True, exist_ok=True)
    if db.user_count() == 0:
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", config.admin_username):
            raise RuntimeError("ADMIN_USERNAME contains unsupported characters")
        generated = not bool(config.admin_password)
        password = config.admin_password or generate_password()
        db.create_user(
            config.admin_username,
            hash_password(password),
            must_change_password=generated,
        )
        if generated:
            credential_path = config.data_dir / "initial_admin_credentials.txt"
            _write_private_file(
                credential_path,
                "Unihedron SQM initial web login\n"
                f"URL: {config.public_url}\n"
                f"Username: {config.admin_username}\n"
                f"Password: {password}\n"
                "Change this password after signing in.\n",
            )
            log.warning(
                "\n"
                "============================================================\n"
                "  Unihedron SQM: initial admin login\n"
                "  Web interface: %s\n"
                "  Username:      %s\n"
                "  Password:      %s\n"
                "  Saved in:      %s\n"
                "  Change the password after signing in.\n"
                "============================================================",
                config.public_url,
                config.admin_username,
                password,
                credential_path,
            )
        else:
            log.info(
                "created initial admin %r using the configured password; UI: %s",
                config.admin_username,
                config.public_url,
            )

    if not config.api_key:
        api_key_path = config.data_dir / "api_key"
        if api_key_path.exists():
            config.api_key = api_key_path.read_text(encoding="utf-8").strip()
        else:
            config.api_key = generate_token()
            _write_private_file(api_key_path, config.api_key + "\n")
            log.info("generated JSON API key in %s", api_key_path)


@asynccontextmanager
async def lifespan(_: FastAPI):
    _bootstrap()
    log.info("web interface ready at %s", config.public_url)
    collector.start()
    _configure_mqtt()
    try:
        yield
    finally:
        publisher.stop()
        await collector.stop()
        db.close()


app = FastAPI(
    title="Unihedron SQM",
    description="Sky-quality collection, dashboard, discovery, and log migration.",
    version=__version__,
    lifespan=lifespan,
)
app.mount("/assets", StaticFiles(directory=STATIC_DIR), name="assets")

if config.cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "OPTIONS"],
        allow_headers=["X-API-Key"],
    )


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    if request.url.path == "/" or request.url.path.startswith("/api/auth"):
        response.headers["Cache-Control"] = "no-store"
    return response


def _iso(timestamp: float | None) -> str | None:
    if timestamp is None:
        return None
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()


def _decorate(reading: dict | None) -> dict | None:
    if reading is None:
        return None
    result = dict(reading)
    result["timestamp"] = _iso(float(result["ts"]))
    result.update(sky.describe(float(result["mpsas"])))
    return result


def _session_from_request(request: Request) -> dict | None:
    token = request.cookies.get(SESSION_COOKIE)
    return db.session(token) if token else None


def require_user(request: Request) -> dict:
    session = _session_from_request(request)
    if not session:
        raise HTTPException(status_code=401, detail="sign in required")
    return session


def require_csrf(
    request: Request,
    session: dict = Depends(require_user),
    x_csrf_token: str | None = Header(None),
) -> dict:
    if not x_csrf_token or not hmac.compare_digest(
        x_csrf_token, session["csrf_token"]
    ):
        raise HTTPException(status_code=403, detail="invalid CSRF token")
    return session


def require_api_key(x_api_key: str | None = Header(None)) -> None:
    if not x_api_key or not hmac.compare_digest(x_api_key, config.api_key):
        raise HTTPException(status_code=401, detail="invalid or missing API key")


class LoginBody(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class PasswordBody(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=12, max_length=256)


class DeviceBody(BaseModel):
    transport: Literal["ethernet", "serial"] = "ethernet"
    host: str | None = Field(default=None, max_length=253)
    port: int = Field(default=10001, ge=1, le=65535)
    serial_device: str | None = Field(default=None, max_length=255)
    baud_rate: int = Field(default=115200, ge=1200, le=3_000_000)


class IntervalBody(BaseModel):
    period_s: int = Field(ge=0, le=meter.MAX_PERIOD_S)
    threshold_mpsas: float | None = Field(
        default=None, ge=0, le=meter.MAX_THRESHOLD_MPSAS
    )
    permanent: bool = False


class DiscoveryBody(BaseModel):
    broadcast_address: str = Field(default="255.255.255.255", max_length=45)
    subnet: str | None = Field(default=None, max_length=43)


def _validated_host(host: str) -> str:
    host = host.strip()
    if not HOST_PATTERN.fullmatch(host) or any(part in host for part in ("/", "@")):
        raise HTTPException(status_code=422, detail="invalid meter host")
    return host


def _validated_serial_device(device: str) -> str:
    device = device.strip()
    path = Path(device)
    if (
        not device.startswith("/dev/")
        or not path.is_absolute()
        or ".." in path.parts
        or any(character in device for character in ("\x00", "\r", "\n"))
    ):
        raise HTTPException(
            status_code=422,
            detail="serial device must be an absolute path below /dev",
        )
    return device


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


# Browsers request this path on their own, before reading any <link> tags.
@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> FileResponse:
    return FileResponse(STATIC_DIR / "favicon-32.png", media_type="image/png")


@app.get("/health", tags=["system"])
def health() -> dict:
    return {
        "status": "ok",
        "version": __version__,
        "collector": collector.state,
        "configured": collector.configured,
        "device_reachable": collector.state == "collecting",
        "last_success": _iso(collector.last_success_ts),
        "stored_readings": db.count(),
    }


# --------------------------------------------------------------- web sessions
@app.get("/api/auth/status", tags=["web"])
def auth_status(request: Request) -> dict:
    session = _session_from_request(request)
    if not session:
        return {"authenticated": False}
    return {
        "authenticated": True,
        "username": session["username"],
        "must_change_password": bool(session["must_change_password"]),
        "csrf_token": session["csrf_token"],
    }


@app.post("/api/auth/login", tags=["web"])
def login(body: LoginBody, request: Request, response: Response) -> dict:
    address = request.client.host if request.client else "unknown"
    attempts = login_attempts[address]
    cutoff = time.time() - 300
    while attempts and attempts[0] < cutoff:
        attempts.popleft()
    if len(attempts) >= 8:
        raise HTTPException(status_code=429, detail="too many attempts; try again later")

    user = db.user_by_username(body.username)
    if not user or not verify_password(body.password, user["password_hash"]):
        attempts.append(time.time())
        # Do enough work for unknown users to reduce username timing differences.
        if not user:
            verify_password(body.password, DUMMY_PASSWORD_HASH)
        raise HTTPException(status_code=401, detail="invalid username or password")

    attempts.clear()
    token = generate_token()
    csrf_token = generate_token(24)
    expires = time.time() + config.session_hours * 3600
    db.create_session(hash_token(token), user["id"], csrf_token, expires)
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=config.session_hours * 3600,
        httponly=True,
        secure=config.cookie_secure,
        samesite="lax",
        path="/",
    )
    return {
        "authenticated": True,
        "username": user["username"],
        "must_change_password": bool(user["must_change_password"]),
        "csrf_token": csrf_token,
    }


@app.post("/api/auth/logout", tags=["web"])
def logout(
    request: Request, response: Response, _: dict = Depends(require_csrf)
) -> dict:
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        db.delete_session(token)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@app.post("/api/account/password", tags=["web"])
def change_password(
    body: PasswordBody,
    response: Response,
    session: dict = Depends(require_csrf),
) -> dict:
    user = db.user_by_username(session["username"])
    if not user or not verify_password(body.current_password, user["password_hash"]):
        raise HTTPException(status_code=400, detail="current password is incorrect")
    if hmac.compare_digest(body.current_password, body.new_password):
        raise HTTPException(status_code=400, detail="new password must be different")
    db.update_password(user["id"], hash_password(body.new_password))
    response.delete_cookie(SESSION_COOKIE, path="/")
    credential_path = config.data_dir / "initial_admin_credentials.txt"
    try:
        credential_path.unlink(missing_ok=True)
    except OSError:
        log.warning("could not remove initial credential file")
    return {"ok": True, "sign_in_again": True}


# ---------------------------------------------------------------- dashboard UI
@app.get("/api/dashboard", tags=["web"])
def dashboard(
    hours: float = Query(24, gt=0, le=87600),
    _: dict = Depends(require_user),
) -> dict:
    since = time.time() - hours * 3600
    info = db.device_info()
    if info and info.get("updated"):
        info = dict(info)
        info["updated_iso"] = _iso(info["updated"])
    now = time.time()
    fraction, waxing = astro.moon_phase(now)
    site = _site()
    moon = {
        "fraction": round(fraction, 3),
        "waxing": waxing,
        "up": astro.moon_altitude(now, *site) > 0 if site else None,
    }
    return {
        "moon": moon,
        "latest": _decorate(db.latest_reading()),
        "stats": db.stats(since),
        "chart": db.chart_readings(since),
        "device": info,
        "collector": {
            "state": collector.state,
            "configured": collector.configured,
            "transport": config.sqm_transport,
            "host": config.sqm_host,
            "port": config.sqm_port,
            "serial_device": config.sqm_serial_device,
            "baud_rate": config.sqm_baud_rate,
            "endpoint": collector.endpoint_label,
            "last_success": _iso(collector.last_success_ts),
            "last_error": collector.last_error,
        },
    }


@app.get("/api/annual", tags=["web"])
def annual_map(
    year: int | None = Query(None, ge=2000, le=2100),
    _: dict = Depends(require_user),
) -> dict:
    try:
        timezone = timezone_for(config.timezone)
        current_year = datetime.now(timezone).year
        selected_year = year if year is not None else current_year
        result = build_annual_map(db, selected_year, config.timezone, site=_site())
        result["available_years"] = available_years(
            db, config.timezone, current_year
        )
        return result
    except ValueError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/config", tags=["web"])
def get_config(_: dict = Depends(require_user)) -> dict:
    return {
        "transport": config.sqm_transport,
        "host": config.sqm_host,
        "port": config.sqm_port,
        "serial_device": config.sqm_serial_device,
        "baud_rate": config.sqm_baud_rate,
        "poll_interval": config.poll_interval,
        "retention_days": config.retention_days,
        "timezone": config.timezone,
        "import_directory_available": config.import_dir.is_dir(),
        "import_directory": str(config.import_dir),
        "public_url": config.public_url,
    }


@app.post("/api/device", tags=["web"])
def save_device(
    body: DeviceBody, _: dict = Depends(require_csrf)
) -> dict:
    if body.transport == "serial":
        if not body.serial_device:
            raise HTTPException(status_code=422, detail="serial device is required")
        device = _validated_serial_device(body.serial_device)
        db.set_setting("sqm_transport", body.transport)
        db.set_setting("sqm_serial_device", device)
        db.set_setting("sqm_baud_rate", str(body.baud_rate))
        db.clear_device_info()
        collector.configure_serial(device, body.baud_rate)
        return {
            "ok": True,
            "transport": "serial",
            "serial_device": device,
            "baud_rate": body.baud_rate,
        }

    if not body.host:
        raise HTTPException(status_code=422, detail="meter host is required")
    host = _validated_host(body.host)
    db.set_setting("sqm_transport", body.transport)
    db.set_setting("sqm_host", host)
    db.set_setting("sqm_port", str(body.port))
    db.clear_device_info()
    collector.configure_ethernet(host, body.port)
    return {
        "ok": True,
        "transport": "ethernet",
        "host": host,
        "port": body.port,
    }


@app.post("/api/device/test", tags=["web"])
async def test_device(
    body: DeviceBody, _: dict = Depends(require_csrf)
) -> dict:
    try:
        async with connection_lock:
            if body.transport == "serial":
                if not body.serial_device:
                    raise HTTPException(
                        status_code=422, detail="serial device is required"
                    )
                device = _validated_serial_device(body.serial_device)
                info = await sqm_client.get_serial_info(
                    device,
                    body.baud_rate,
                    config.read_timeout,
                )
                return {
                    "ok": True,
                    "transport": "serial",
                    "serial_device": device,
                    "baud_rate": body.baud_rate,
                    "device": info,
                }

            if not body.host:
                raise HTTPException(status_code=422, detail="meter host is required")
            host = _validated_host(body.host)
            info = await sqm_client.get_info(
                host,
                body.port,
                config.connect_timeout,
                config.read_timeout,
            )
            return {
                "ok": True,
                "transport": "ethernet",
                "host": host,
                "port": body.port,
                "device": info,
            }
    except sqm_client.SQMError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


# Long enough to catch one pushed report from a meter in interval-reporting
# mode, which commonly reports every five seconds. Hosts without port 10001
# open still fail at the short connect timeout.
PROBE_READ_TIMEOUT = 7


# ----------------------------------------------------------------- settings
@app.get("/api/settings", tags=["web"])
def get_settings(_: dict = Depends(require_user)) -> dict:
    return {"settings": settings.public(), "status": _integration_status()}


@app.put("/api/settings/{section}", tags=["web"])
def put_settings(section: str, body: dict, _: dict = Depends(require_csrf)) -> dict:
    if section not in SECTIONS:
        raise HTTPException(status_code=404, detail="unknown settings section")
    try:
        return settings.update(section, body)
    except SettingLocked as exc:
        raise HTTPException(status_code=409, detail={"field": exc.field, "message": str(exc)}) from exc
    except SettingsError as exc:
        raise HTTPException(status_code=422, detail={"field": exc.field, "message": str(exc)}) from exc


def _integration_status() -> dict:
    return {"mqtt": publisher.status}


# ---------------------------------------------------------- meter control
def _require_configured_meter() -> None:
    if not collector.configured:
        raise HTTPException(status_code=409, detail="No meter is configured yet.")


# Long enough for `ix` to fall back to a pushed interval report when the
# meter is not answering commands.
METER_IDENTIFY_TIMEOUT = 10


@app.get("/api/meter", tags=["web"])
async def meter_details(_: dict = Depends(require_user)) -> dict:
    _require_configured_meter()
    try:
        async with collector.session() as link:
            details = await meter.read_details(
                link.send, lambda: link.identify(METER_IDENTIFY_TIMEOUT)
            )
    except sqm_client.SQMError as exc:
        details = {
            "health": {"status": "unreachable", "message": str(exc)},
            "info": None,
            "calibration": None,
            "interval": None,
            "lock": None,
            "freshness": None,
            "errors": {},
        }
    info = details["info"]
    if details["health"]["status"] == "ok" and info:
        # A full `ix` reply replaces partial identity saved while the meter
        # was not answering commands.
        await asyncio.to_thread(
            db.upsert_device_info, info, collector.endpoint_key or ""
        )
    return details


@app.post("/api/meter/interval", tags=["web"])
async def change_interval(
    body: IntervalBody, _: dict = Depends(require_csrf)
) -> dict:
    _require_configured_meter()
    try:
        async with collector.session() as link:
            return await meter.set_interval(
                link.send,
                period_s=body.period_s,
                threshold_mpsas=body.threshold_mpsas,
                permanent=body.permanent,
            )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except meter.MeterLocked as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except sqm_client.SQMError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


async def _probe_device(host: str, port: int, timeout: float = 0.6) -> dict | None:
    try:
        if config.sqm_transport == "ethernet" and host == config.sqm_host:
            async with connection_lock:
                info = await sqm_client.get_info(host, port, timeout, PROBE_READ_TIMEOUT)
        else:
            info = await sqm_client.get_info(host, port, timeout, PROBE_READ_TIMEOUT)
        return info
    except sqm_client.SQMError:
        return None


@app.post("/api/discover", tags=["web"])
async def discover_devices(
    body: DiscoveryBody, _: dict = Depends(require_csrf)
) -> dict:
    try:
        broadcast_results = await asyncio.to_thread(
            discovery.broadcast_discover, body.broadcast_address, 2.0
        )
        scan_hosts = discovery.hosts_in_subnet(body.subnet) if body.subnet else []
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    results: dict[str, dict] = {
        item["host"]: {
            **item,
            "transport": "ethernet",
            "port": 10001,
            "verified": False,
        }
        for item in broadcast_results
    }
    candidates = set(results).union(scan_hosts)
    semaphore = asyncio.Semaphore(32)

    async def probe(host: str) -> tuple[str, dict | None]:
        async with semaphore:
            return host, await _probe_device(host, 10001)

    for host, info in await asyncio.gather(*(probe(host) for host in candidates)):
        if info:
            existing = results.get(
                host,
                {
                    "transport": "ethernet",
                    "host": host,
                    "mac": None,
                    "port": 10001,
                    "source": "tcp-scan",
                },
            )
            existing.update({"verified": True, "device": info})
            results[host] = existing

    def address_key(item: dict) -> int:
        try:
            return int(ipaddress.ip_address(item["host"]))
        except ValueError:
            return 0

    return {
        "count": len(results),
        "devices": sorted(results.values(), key=address_key),
        "broadcast_address": body.broadcast_address,
        "subnet_scanned": body.subnet,
    }


@app.post("/api/discover/serial", tags=["web"])
async def discover_serial_devices(_: dict = Depends(require_csrf)) -> dict:
    candidates = sqm_client.serial_candidates(
        [config.sqm_serial_device] if config.sqm_serial_device else ()
    )
    async with connection_lock:
        devices = await sqm_client.discover_serial_devices(
            paths=candidates,
            baud_rate=config.sqm_baud_rate,
            timeout=min(config.read_timeout, 2),
        )
    return {"count": len(devices), "devices": devices}


# -------------------------------------------------------------------- imports
def _aggregate_imports(results: list[dict]) -> dict:
    return {
        "files": len(results),
        "parsed": sum(item["parsed"] for item in results),
        "imported": sum(item["imported"] for item in results),
        "duplicates": sum(item["duplicates"] for item in results),
        "bad_lines": sum(item["bad_lines"] for item in results),
        "results": results,
    }


@app.get("/api/imports", tags=["web"])
def import_history(_: dict = Depends(require_user)) -> dict:
    rows = db.recent_imports()
    for row in rows:
        row["created_iso"] = _iso(row["created"])
    return {"imports": rows}


@app.post("/api/import/upload", tags=["web"])
async def upload_logs(
    files: list[UploadFile] = File(...),
    _: dict = Depends(require_csrf),
) -> dict:
    if len(files) > 2000:
        raise HTTPException(status_code=413, detail="at most 2,000 files per import")
    maximum = config.max_upload_mb * 1024 * 1024
    results: list[dict] = []
    for upload in files:
        filename = Path(upload.filename or "upload.dat").name
        if Path(filename).suffix.lower() not in {".dat", ".csv"}:
            raise HTTPException(
                status_code=415, detail=f"{filename}: only .dat and .csv are supported"
            )
        content = await upload.read(maximum + 1)
        await upload.close()
        if len(content) > maximum:
            raise HTTPException(
                status_code=413,
                detail=f"{filename}: exceeds {config.max_upload_mb} MB limit",
            )
        result = await asyncio.to_thread(import_text, db, filename, content)
        results.append(result.as_dict())
    return _aggregate_imports(results)


@app.post("/api/import/directory", tags=["web"])
async def import_directory(
    _: dict = Depends(require_csrf),
) -> dict:
    if not config.import_dir.is_dir():
        raise HTTPException(
            status_code=404, detail=f"{config.import_dir} is not mounted"
        )
    dat_files = sorted(config.import_dir.glob("*.dat"))
    paths = dat_files or sorted(config.import_dir.glob("*.csv"))
    if not paths:
        raise HTTPException(status_code=404, detail="no .dat or .csv files found")
    if len(paths) > 5000:
        raise HTTPException(status_code=413, detail="directory contains too many files")

    maximum = config.max_upload_mb * 1024 * 1024
    results: list[dict] = []
    for path in paths:
        if path.stat().st_size > maximum:
            results.append(
                {
                    "filename": path.name,
                    "parsed": 0,
                    "imported": 0,
                    "duplicates": 0,
                    "bad_lines": 1,
                    "bad_samples": ["file exceeds configured size limit"],
                    "first_timestamp": None,
                    "last_timestamp": None,
                }
            )
            continue
        content = await asyncio.to_thread(path.read_bytes)
        result = await asyncio.to_thread(import_text, db, path.name, content)
        results.append(result.as_dict())
    return _aggregate_imports(results)


@app.get("/metrics", include_in_schema=False)
def prometheus_metrics(
    authorization: str | None = Header(None), x_api_key: str | None = Header(None)
) -> Response:
    if not settings.get("prometheus", "public"):
        bearer = authorization[7:] if authorization and authorization.startswith("Bearer ") else None
        require_api_key(bearer or x_api_key)
    info = db.device_info()
    serial = str(info["serial"]) if info and info.get("serial") else "meter"
    body = metrics.render(
        db.latest_reading(), serial, collector.state == "collecting",
        collector.last_success_ts, db.count(),
    )
    return Response(body, media_type="text/plain; version=0.0.4; charset=utf-8")


# ---------------------------------------------------------- API-key JSON API
def require_user_or_key(
    request: Request, x_api_key: str | None = Header(None)
) -> None:
    if _session_from_request(request):
        return
    require_api_key(x_api_key)


@app.get("/api/export", tags=["JSON API"])
def export_readings(
    format: Literal["csv", "dat"] = Query("csv"),
    since: float | None = Query(None),
    until: float | None = Query(None),
    _: None = Depends(require_user_or_key),
) -> StreamingResponse:
    info = db.device_info()
    serial = info["serial"] if info else None
    first, last = db.reading_bounds(since, until)
    name = exporter.filename(format, serial, first, last, config.timezone)
    media = "text/csv" if format == "csv" else "text/plain"
    return StreamingResponse(
        exporter.stream(db, format, since, until, config.timezone, serial),
        media_type=f"{media}; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


@app.get(
    "/api/latest",
    tags=["JSON API"],
    dependencies=[Depends(require_api_key)],
)
def api_latest() -> dict:
    reading = _decorate(db.latest_reading())
    if reading is None:
        raise HTTPException(status_code=404, detail="no readings recorded yet")
    return reading


@app.get(
    "/api/readings",
    tags=["JSON API"],
    dependencies=[Depends(require_api_key)],
)
def api_readings(
    since: float | None = Query(None),
    until: float | None = Query(None),
    hours: float | None = Query(None, gt=0),
    limit: int = Query(1000, gt=0, le=50000),
    order: str = Query("desc", pattern="^(asc|desc)$"),
) -> dict:
    if hours is not None and since is None:
        since = time.time() - hours * 3600
    rows = db.readings(since, until, limit, order_desc=(order == "desc"))
    return {"count": len(rows), "readings": [_decorate(row) for row in rows]}


@app.get(
    "/api/stats",
    tags=["JSON API"],
    dependencies=[Depends(require_api_key)],
)
def api_stats(hours: float = Query(24, gt=0, le=87600)) -> dict:
    since = time.time() - hours * 3600
    result = db.stats(since)
    result.update({"window_hours": hours, "since": _iso(since)})
    return result


@app.get(
    "/api/device",
    tags=["JSON API"],
    dependencies=[Depends(require_api_key)],
)
def api_device() -> dict:
    info = db.device_info()
    if info is None:
        raise HTTPException(status_code=404, detail="device information unavailable")
    result = dict(info)
    result["updated_iso"] = _iso(result.get("updated"))
    result["transport"] = config.sqm_transport
    result["endpoint"] = collector.endpoint_label
    return result
