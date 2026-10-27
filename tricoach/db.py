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
    CREATE TABLE IF NOT EXISTS coach_messages (
        thread_id TEXT NOT NULL,
        seq INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        role TEXT NOT NULL,
        content TEXT NOT NULL,
        PRIMARY KEY (thread_id, seq)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS coach_briefings (
        id TEXT PRIMARY KEY,
        created_at TEXT NOT NULL,
        headline TEXT,
        body TEXT NOT NULL,
        model TEXT
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

# Shared tables (accounts, invites, AI usage). Each user's training data lives in its own schema built from SCHEMA.
CORE_SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS users (
        id TEXT PRIMARY KEY,
        email TEXT NOT NULL UNIQUE,
        name TEXT,
        google_sub TEXT UNIQUE,
        tier TEXT NOT NULL DEFAULT 'free',
        is_admin INTEGER NOT NULL DEFAULT 0,
        focus TEXT,
        timezone TEXT,
        garmin_tokens TEXT,
        garmin_connected_at TEXT,
        mcp_token TEXT UNIQUE,
        sync_token TEXT UNIQUE,
        session_version INTEGER NOT NULL DEFAULT 1,
        onboarded INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        last_seen_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS invites (
        code TEXT PRIMARY KEY,
        created_at TEXT NOT NULL,
        note TEXT,
        max_uses INTEGER NOT NULL DEFAULT 1,
        uses INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS waitlist (
        email TEXT PRIMARY KEY,
        created_at TEXT NOT NULL,
        sport TEXT,
        invited_code TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS login_links (
        token_hash TEXT PRIMARY KEY,
        email TEXT NOT NULL,
        invite_code TEXT,
        expires_at TEXT NOT NULL,
        used_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ai_usage (
        user_id TEXT NOT NULL,
        day TEXT NOT NULL,
        cost_micro BIGINT NOT NULL DEFAULT 0,
        input_tokens BIGINT NOT NULL DEFAULT 0,
        output_tokens BIGINT NOT NULL DEFAULT 0,
        requests INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (user_id, day)
    )
    """,
]

_NAMED = re.compile(r"(?<!:):([A-Za-z_][A-Za-z0-9_]*)")


class Database:
    """One connection, optionally scoped to a schema.

    PostgreSQL: ``schema`` is created if needed and set as the connection's search_path, so the same unqualified SQL
    reads and writes only that user's tables. SQLite (local/tests): each schema is a separate database file
    (``<name>.db`` next to the main file), or a separate in-memory database.
    """

    def __init__(self, url: str, schema: str | None = None):
        if schema is not None and not re.fullmatch(r"[a-z][a-z0-9_]{0,40}", schema):
            raise ValueError(f"bad schema name {schema!r}")
        self.url, self.schema = url, schema
        self._lock = threading.RLock()
        if url.startswith("sqlite"):
            self.kind = "sqlite"
            path = url.split("sqlite:///", 1)[1] if "sqlite:///" in url else ":memory:"
            if path != ":memory:":
                if schema:
                    path = str(Path(path).with_name(f"{schema}.db"))
                Path(path).parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
        elif url.startswith(("postgres://", "postgresql://")):
            self.kind = "postgres"
            import psycopg  # imported lazily so tests don't need it
            from psycopg.rows import dict_row

            self._conn = psycopg.connect(url, autocommit=True, row_factory=dict_row)
            if schema:
                with self._conn.cursor() as cur:
                    cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
                    cur.execute(f'SET search_path TO "{schema}"')
        else:
            raise ValueError(f"Unsupported DATABASE_URL: {url}")

    # -- helpers ---------------------------------------------------------
    def _sql(self, sql: str) -> str:
        if self.kind == "postgres":
            return _NAMED.sub(r"%(\1)s", sql.replace("%", "%%"))
        return sql

    def init_schema(self, statements: list[str] | None = None) -> None:
        for stmt in statements if statements is not None else SCHEMA:
            self.execute(stmt)
        if self.kind == "postgres" and self.schema and statements is None:
            # A read-only role per user schema: ad-hoc SQL (run_sql) runs as this role, so it can't see other users.
            role = f"{self.schema}_ro"
            with self._lock, self._conn.cursor() as cur:
                cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,))
                if not cur.fetchone():
                    cur.execute(f'CREATE ROLE "{role}" NOLOGIN')
                cur.execute(f'GRANT USAGE ON SCHEMA "{self.schema}" TO "{role}"')
                cur.execute(f'GRANT SELECT ON ALL TABLES IN SCHEMA "{self.schema}" TO "{role}"')

    def close(self) -> None:
        with self._lock:
            self._conn.close()

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
                        if self.schema:
                            cur.execute(f'SET LOCAL ROLE "{self.schema}_ro"')
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
