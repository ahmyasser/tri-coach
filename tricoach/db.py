"""Tiny database layer that runs the same SQL on PostgreSQL (production) and SQLite (local/tests).

SQL is written with named parameters (``:name``). For PostgreSQL they are rewritten to
psycopg's ``%(name)s`` style. Only portable SQL is used (``INSERT ... ON CONFLICT``,
TEXT/BIGINT/DOUBLE PRECISION), so no ORM is needed.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator

SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS activities (
        activity_id BIGINT PRIMARY KEY,
        start_local TEXT NOT NULL,
        start_gmt TEXT,
        local_date TEXT NOT NULL,
        sport TEXT NOT NULL,
        type_key TEXT,
        name TEXT,
        distance_m DOUBLE PRECISION,
        duration_s DOUBLE PRECISION,
        moving_s DOUBLE PRECISION,
        avg_speed DOUBLE PRECISION,
        avg_hr DOUBLE PRECISION,
        max_hr DOUBLE PRECISION,
        calories DOUBLE PRECISION,
        elevation_gain DOUBLE PRECISION,
        avg_power DOUBLE PRECISION,
        norm_power DOUBLE PRECISION,
        avg_cadence DOUBLE PRECISION,
        training_load DOUBLE PRECISION,
        aerobic_te DOUBLE PRECISION,
        anaerobic_te DOUBLE PRECISION,
        pool_length_m DOUBLE PRECISION,
        avg_swolf DOUBLE PRECISION,
        avg_strokes DOUBLE PRECISION,
        active_lengths INTEGER,
        hr_z1_s DOUBLE PRECISION,
        hr_z2_s DOUBLE PRECISION,
        hr_z3_s DOUBLE PRECISION,
        hr_z4_s DOUBLE PRECISION,
        hr_z5_s DOUBLE PRECISION,
        is_parent INTEGER DEFAULT 0,
        parent_id BIGINT,
        quality_flags TEXT,
        details_synced INTEGER DEFAULT 0,
        raw TEXT,
        updated_at TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_activities_date ON activities (local_date)",
    "CREATE INDEX IF NOT EXISTS idx_activities_sport ON activities (sport, local_date)",
    """
    CREATE TABLE IF NOT EXISTS laps (
        activity_id BIGINT NOT NULL,
        lap_index INTEGER NOT NULL,
        distance_m DOUBLE PRECISION,
        duration_s DOUBLE PRECISION,
        moving_s DOUBLE PRECISION,
        avg_speed DOUBLE PRECISION,
        avg_hr DOUBLE PRECISION,
        max_hr DOUBLE PRECISION,
        avg_power DOUBLE PRECISION,
        stroke TEXT,
        avg_strokes DOUBLE PRECISION,
        swolf DOUBLE PRECISION,
        lengths INTEGER,
        is_rest INTEGER DEFAULT 0,
        raw TEXT,
        PRIMARY KEY (activity_id, lap_index)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS wellness (
        day TEXT PRIMARY KEY,
        resting_hr DOUBLE PRECISION,
        hrv_last_night DOUBLE PRECISION,
        hrv_weekly_avg DOUBLE PRECISION,
        hrv_status TEXT,
        sleep_s DOUBLE PRECISION,
        sleep_score DOUBLE PRECISION,
        body_battery_change DOUBLE PRECISION,
        raw TEXT,
        updated_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS workouts (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        sport TEXT NOT NULL,
        spec TEXT NOT NULL,
        notes TEXT,
        status TEXT NOT NULL,
        garmin_workout_id BIGINT,
        scheduled_date TEXT,
        garmin_schedule_id BIGINT,
        created_at TEXT,
        updated_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS coach_notes (
        id TEXT PRIMARY KEY,
        created_at TEXT NOT NULL,
        topic TEXT,
        body TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS athlete_settings (
        key TEXT PRIMARY KEY,
        value TEXT,
        updated_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sync_runs (
        id TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        status TEXT NOT NULL,
        details TEXT
    )
    """,
]

_NAMED = re.compile(r"(?<!:):([A-Za-z_][A-Za-z0-9_]*)")


class Database:
    def __init__(self, url: str):
        self.url = url
        self._lock = threading.RLock()
        if url.startswith("sqlite"):
            self.kind = "sqlite"
            path = url.split("sqlite:///", 1)[1] if "sqlite:///" in url else ":memory:"
            if path != ":memory:":
                Path(path).parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
        elif url.startswith(("postgres://", "postgresql://")):
            self.kind = "postgres"
            import psycopg  # imported lazily so tests don't need it
            from psycopg.rows import dict_row

            self._conn = psycopg.connect(url, autocommit=True, row_factory=dict_row)
        else:
            raise ValueError(f"Unsupported DATABASE_URL: {url}")

    # -- helpers ---------------------------------------------------------
    def _sql(self, sql: str) -> str:
        if self.kind == "postgres":
            return _NAMED.sub(r"%(\1)s", sql.replace("%", "%%"))
        return sql

    def init_schema(self) -> None:
        for stmt in SCHEMA:
            self.execute(stmt)

    def execute(self, sql: str, params: dict[str, Any] | None = None) -> None:
        with self._lock:
            if self.kind == "sqlite":
                self._conn.execute(sql, params or {})
                self._conn.commit()
            else:
                with self._conn.cursor() as cur:
                    cur.execute(self._sql(sql), params or {})

    def executemany(self, sql: str, rows: Iterable[dict[str, Any]]) -> None:
        rows = list(rows)
        if not rows:
            return
        with self._lock:
            if self.kind == "sqlite":
                self._conn.executemany(sql, rows)
                self._conn.commit()
            else:
                with self._conn.cursor() as cur:
                    cur.executemany(self._sql(sql), rows)

    def query(self, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        with self._lock:
            if self.kind == "sqlite":
                cur = self._conn.execute(sql, params or {})
                return [dict(r) for r in cur.fetchall()]
            with self._conn.cursor() as cur:
                cur.execute(self._sql(sql), params or {})
                return list(cur.fetchall()) if cur.description else []

    def query_raw(self, sql: str, limit: int) -> list[dict[str, Any]]:
        """Run caller-supplied SQL with no parameter rewriting (used inside ``readonly()``)."""
        with self._lock:
            if self.kind == "sqlite":
                cur = self._conn.execute(sql)
                return [dict(r) for r in cur.fetchmany(limit)]
            with self._conn.cursor() as cur:
                cur.execute(sql)
                return list(cur.fetchmany(limit)) if cur.description else []

    def query_one(self, sql: str, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    @contextmanager
    def readonly(self) -> Iterator[None]:
        """Run ad-hoc SQL (from the MCP ``run_sql`` tool) inside a read-only transaction."""
        with self._lock:
            if self.kind == "postgres":
                self._conn.autocommit = False
                try:
                    with self._conn.cursor() as cur:
                        cur.execute("SET TRANSACTION READ ONLY")
                        cur.execute("SET LOCAL statement_timeout = '15s'")
                    yield
                finally:
                    self._conn.rollback()
                    self._conn.autocommit = True
            else:
                self._conn.execute("PRAGMA query_only = ON")
                try:
                    yield
                finally:
                    self._conn.execute("PRAGMA query_only = OFF")

    # -- settings --------------------------------------------------------
    def get_setting(self, key: str, default: Any = None) -> Any:
        row = self.query_one("SELECT value FROM athlete_settings WHERE key = :k", {"k": key})
        return json.loads(row["value"]) if row and row["value"] is not None else default

    def set_setting(self, key: str, value: Any, now: str) -> None:
        self.execute(
            """INSERT INTO athlete_settings (key, value, updated_at) VALUES (:k, :v, :now)
               ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at""",
            {"k": key, "v": json.dumps(value), "now": now},
        )


def upsert_sql(table: str, columns: list[str], key: list[str]) -> str:
    cols = ", ".join(columns)
    vals = ", ".join(f":{c}" for c in columns)
    updates = ", ".join(f"{c} = excluded.{c}" for c in columns if c not in key)
    return f"INSERT INTO {table} ({cols}) VALUES ({vals}) ON CONFLICT ({', '.join(key)}) DO UPDATE SET {updates}"
