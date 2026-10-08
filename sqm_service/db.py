"""Thread-safe SQLite storage for readings, settings, users, and imports."""

from __future__ import annotations

import hashlib
import math
import sqlite3
import threading
import time
from pathlib import Path
from typing import Iterable


class Database:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            self.path, check_same_thread=False, timeout=60
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=60000")
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS readings (
                    id             INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts             REAL    NOT NULL,
                    mpsas          REAL    NOT NULL,
                    frequency_hz   INTEGER NOT NULL,
                    period_counts  INTEGER NOT NULL,
                    period_seconds REAL    NOT NULL,
                    temperature_c  REAL    NOT NULL,
                    raw            TEXT    NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_readings_ts ON readings(ts);

                CREATE TABLE IF NOT EXISTS device_info (
                    id        INTEGER PRIMARY KEY CHECK (id = 1),
                    protocol  INTEGER,
                    model     INTEGER,
                    feature   INTEGER,
                    serial    INTEGER,
                    raw       TEXT,
                    updated   REAL
                );

                CREATE TABLE IF NOT EXISTS app_settings (
                    key       TEXT PRIMARY KEY,
                    value     TEXT NOT NULL,
                    updated   REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS users (
                    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                    username             TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    password_hash        TEXT NOT NULL,
                    must_change_password INTEGER NOT NULL DEFAULT 1,
                    created              REAL NOT NULL,
                    last_login           REAL
                );

                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY,
                    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    csrf_token TEXT NOT NULL,
                    created    REAL NOT NULL,
                    expires    REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires);

                CREATE TABLE IF NOT EXISTS import_history (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    filename    TEXT NOT NULL,
                    imported    INTEGER NOT NULL,
                    duplicates  INTEGER NOT NULL,
                    bad_lines   INTEGER NOT NULL,
                    created     REAL NOT NULL
                );
                """
            )
            self._conn.commit()

    # --------------------------------------------------------------- settings
    def get_setting(self, key: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM app_settings WHERE key = ?", (key,)
            ).fetchone()
        return str(row["value"]) if row else None

    def set_setting(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO app_settings (key, value, updated)
                   VALUES (?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET
                     value=excluded.value, updated=excluded.updated""",
                (key, value, time.time()),
            )
            self._conn.commit()

    # ---------------------------------------------------------------- readings
    def insert_reading(self, reading: dict, timestamp: float | None = None) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO readings
                   (ts, mpsas, frequency_hz, period_counts, period_seconds,
                    temperature_c, raw)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    timestamp if timestamp is not None else time.time(),
                    reading["mpsas"],
                    reading["frequency_hz"],
                    reading["period_counts"],
                    reading["period_seconds"],
                    reading["temperature_c"],
                    reading["raw"],
                ),
            )
            self._conn.commit()

    def import_readings(self, rows: Iterable[tuple]) -> tuple[int, int]:
        candidates = list(rows)
        if not candidates:
            return 0, 0

        # Dedupe within this file before querying SQLite.
        unique: dict[float, tuple] = {}
        duplicates = 0
        for row in candidates:
            timestamp = float(row[0])
            if timestamp in unique:
                duplicates += 1
            else:
                unique[timestamp] = row

        with self._lock:
            existing: set[float] = set()
            timestamps = list(unique)
            # Stay well below SQLite's traditional 999-variable limit.
            for start in range(0, len(timestamps), 800):
                chunk = timestamps[start : start + 800]
                marks = ",".join("?" for _ in chunk)
                existing.update(
                    float(row[0])
                    for row in self._conn.execute(
                        f"SELECT ts FROM readings WHERE ts IN ({marks})", chunk
                    )
                )
            new_rows = [
                row for timestamp, row in unique.items() if timestamp not in existing
            ]
            duplicates += len(existing)
            self._conn.executemany(
                """INSERT INTO readings
                   (ts, mpsas, frequency_hz, period_counts, period_seconds,
                    temperature_c, raw)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                new_rows,
            )
            self._conn.commit()
        return len(new_rows), duplicates

    def latest_reading(self) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM readings ORDER BY ts DESC LIMIT 1"
            ).fetchone()
        return dict(row) if row else None

    def readings(
        self,
        since: float | None,
        until: float | None,
        limit: int,
        order_desc: bool,
    ) -> list[dict]:
        clauses: list[str] = []
        params: list[float | int] = []
        if since is not None:
            clauses.append("ts >= ?")
            params.append(since)
        if until is not None:
            clauses.append("ts <= ?")
            params.append(until)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        order = "DESC" if order_desc else "ASC"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM readings {where} ORDER BY ts {order} LIMIT ?",
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def readings_after(self, after: float, until: float | None, limit: int) -> list[dict]:
        sql = "SELECT * FROM readings WHERE ts > ?"
        params: list[float | int] = [after]
        if until is not None:
            sql += " AND ts <= ?"
            params.append(until)
        sql += " ORDER BY ts ASC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def chart_readings(self, since: float, limit: int = 1600) -> list[dict]:
        with self._lock:
            total = self._conn.execute(
                "SELECT COUNT(*) FROM readings WHERE ts >= ?", (since,)
            ).fetchone()[0]
            stride = max(1, math.ceil(total / limit))
            rows = self._conn.execute(
                """SELECT ts, mpsas, temperature_c
                   FROM readings
                   WHERE ts >= ? AND (id % ?) = 0
                   ORDER BY ts ASC
                   LIMIT ?""",
                (since, stride, limit),
            ).fetchall()
            latest = self._conn.execute(
                """SELECT ts, mpsas, temperature_c FROM readings
                   WHERE ts >= ? ORDER BY ts DESC LIMIT 1""",
                (since,),
            ).fetchone()
        result = [dict(row) for row in rows]
        if latest and (not result or result[-1]["ts"] != latest["ts"]):
            result.append(dict(latest))
        return result

    def annual_bins(
        self,
        start: float,
        end: float,
        slot_seconds: int = 1800,
        minimum_mpsas: float = 10.0,
    ) -> list[dict]:
        """Return compact UTC time bins for an annual local-time heatmap.

        SQLite reduces roughly half a million minute readings to at most
        17,544 half-hour bins. Local-day/DST mapping is then handled in Python.
        Readings below `minimum_mpsas` are left out of the bins.
        """
        with self._lock:
            rows = self._conn.execute(
                """SELECT CAST((ts - ?) / ? AS INTEGER) AS bin,
                          AVG(mpsas) AS avg_mpsas,
                          COUNT(*) AS reading_count
                   FROM readings
                   WHERE ts >= ? AND ts < ? AND mpsas >= ?
                   GROUP BY bin
                   ORDER BY bin""",
                (start, slot_seconds, start, end, minimum_mpsas),
            ).fetchall()
        return [dict(row) for row in rows]

    def reading_bounds(
        self, since: float | None = None, until: float | None = None
    ) -> tuple[float | None, float | None]:
        with self._lock:
            row = self._conn.execute(
                """SELECT MIN(ts) AS first_ts, MAX(ts) AS last_ts FROM readings
                   WHERE (? IS NULL OR ts >= ?) AND (? IS NULL OR ts <= ?)""",
                (since, since, until, until),
            ).fetchone()
        if not row:
            return None, None
        return row["first_ts"], row["last_ts"]

    def stats(self, since: float) -> dict:
        with self._lock:
            row = self._conn.execute(
                """SELECT COUNT(*) AS count,
                          MIN(mpsas) AS min_mpsas,
                          MAX(mpsas) AS max_mpsas,
                          AVG(mpsas) AS avg_mpsas,
                          MIN(temperature_c) AS min_temp_c,
                          MAX(temperature_c) AS max_temp_c,
                          AVG(temperature_c) AS avg_temp_c
                   FROM readings WHERE ts >= ?""",
                (since,),
            ).fetchone()
        return dict(row) if row else {}

    def count(self) -> int:
        with self._lock:
            return int(
                self._conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0]
            )

    def prune(self, older_than_days: int) -> int:
        if older_than_days <= 0:
            return 0
        cutoff = time.time() - older_than_days * 86400
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM readings WHERE ts < ?", (cutoff,)
            )
            self._conn.commit()
            return cursor.rowcount

    # -------------------------------------------------------------- device info
    def device_info(self) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM device_info WHERE id = 1"
            ).fetchone()
        return dict(row) if row else None

    def upsert_device_info(self, info: dict, endpoint: str) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO device_info
                   (id, protocol, model, feature, serial, raw, updated)
                   VALUES (1, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                     protocol=excluded.protocol, model=excluded.model,
                     feature=excluded.feature, serial=excluded.serial,
                     raw=excluded.raw, updated=excluded.updated""",
                (
                    info["protocol"],
                    info["model"],
                    info["feature"],
                    info["serial"],
                    info["raw"],
                    time.time(),
                ),
            )
            self._conn.execute(
                """INSERT INTO app_settings (key, value, updated)
                   VALUES ('device_info_endpoint', ?, ?)
                   ON CONFLICT(key) DO UPDATE SET
                     value=excluded.value, updated=excluded.updated""",
                (endpoint, time.time()),
            )
            self._conn.commit()

    def clear_device_info(self) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM device_info")
            self._conn.execute(
                "DELETE FROM app_settings WHERE key IN "
                "('device_info_host', 'device_info_endpoint')"
            )
            self._conn.commit()

    # ------------------------------------------------------------------- users
    def user_count(self) -> int:
        with self._lock:
            return int(self._conn.execute("SELECT COUNT(*) FROM users").fetchone()[0])

    def create_user(
        self, username: str, password_hash: str, must_change_password: bool
    ) -> int:
        with self._lock:
            cursor = self._conn.execute(
                """INSERT INTO users
                   (username, password_hash, must_change_password, created)
                   VALUES (?, ?, ?, ?)""",
                (username, password_hash, int(must_change_password), time.time()),
            )
            self._conn.commit()
            return int(cursor.lastrowid)

    def user_by_username(self, username: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM users WHERE username = ?", (username,)
            ).fetchone()
        return dict(row) if row else None

    def update_password(self, user_id: int, password_hash: str) -> None:
        with self._lock:
            self._conn.execute(
                """UPDATE users
                   SET password_hash = ?, must_change_password = 0
                   WHERE id = ?""",
                (password_hash, user_id),
            )
            self._conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
            self._conn.commit()

    def create_session(
        self, token_hash: str, user_id: int, csrf_token: str, expires: float
    ) -> None:
        now = time.time()
        with self._lock:
            self._conn.execute("DELETE FROM sessions WHERE expires < ?", (now,))
            self._conn.execute(
                """INSERT INTO sessions
                   (token_hash, user_id, csrf_token, created, expires)
                   VALUES (?, ?, ?, ?, ?)""",
                (token_hash, user_id, csrf_token, now, expires),
            )
            self._conn.execute(
                "UPDATE users SET last_login = ? WHERE id = ?", (now, user_id)
            )
            self._conn.commit()

    def session(self, token: str) -> dict | None:
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        with self._lock:
            row = self._conn.execute(
                """SELECT sessions.*, users.username, users.must_change_password
                   FROM sessions
                   JOIN users ON users.id = sessions.user_id
                   WHERE sessions.token_hash = ? AND sessions.expires > ?""",
                (token_hash, time.time()),
            ).fetchone()
        return dict(row) if row else None

    def delete_session(self, token: str) -> None:
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        with self._lock:
            self._conn.execute(
                "DELETE FROM sessions WHERE token_hash = ?", (token_hash,)
            )
            self._conn.commit()

    # ----------------------------------------------------------------- imports
    def record_import(
        self, filename: str, imported: int, duplicates: int, bad_lines: int
    ) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO import_history
                   (filename, imported, duplicates, bad_lines, created)
                   VALUES (?, ?, ?, ?, ?)""",
                (filename[:255], imported, duplicates, bad_lines, time.time()),
            )
            self._conn.commit()

    def recent_imports(self, limit: int = 20) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM import_history ORDER BY created DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()
