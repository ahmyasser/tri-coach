"""Runtime configuration, read from environment variables (see .env.example)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    return int(raw) if raw not in (None, "") else default


@dataclass(frozen=True)
class Settings:
    database_url: str = field(default_factory=lambda: os.getenv("DATABASE_URL", "sqlite:///data/tricoach.db"))
    garmin_tokenstore: str = field(default_factory=lambda: os.getenv("GARMIN_TOKENSTORE", "/data/garmin-tokens"))
    timezone: str = field(default_factory=lambda: os.getenv("TZ", "Africa/Cairo"))
    # Daily sync time in local time, HH:MM
    sync_time: str = field(default_factory=lambda: os.getenv("SYNC_TIME", "05:30"))
    # How far back to pull wellness (sleep/HRV/resting HR) on the first backfill
    wellness_backfill_days: int = field(default_factory=lambda: _int("WELLNESS_BACKFILL_DAYS", 400))
    # On each daily sync, re-check this many recent days (catches late uploads/edits)
    resync_days: int = field(default_factory=lambda: _int("RESYNC_DAYS", 3))
    # Pause between Garmin detail calls, in milliseconds (Garmin rate-limits bursts)
    request_pause_ms: int = field(default_factory=lambda: _int("GARMIN_REQUEST_PAUSE_MS", 400))

    dashboard_user: str = field(default_factory=lambda: os.getenv("DASHBOARD_USER", "ahmed"))
    dashboard_password: str = field(default_factory=lambda: os.getenv("DASHBOARD_PASSWORD", ""))
    # Secret used by the MCP endpoint: /mcp/<MCP_TOKEN> or "Authorization: Bearer <MCP_TOKEN>"
    mcp_token: str = field(default_factory=lambda: os.getenv("MCP_TOKEN", ""))
    # Secret for POST /api/sync (the "webhook" you can hit after a workout)
    sync_webhook_token: str = field(default_factory=lambda: os.getenv("SYNC_WEBHOOK_TOKEN", ""))
    # Public hostname (e.g. coach.example.com) — allowed Host header for MCP
    public_host: str = field(default_factory=lambda: os.getenv("PUBLIC_HOST", "localhost"))

    # AI coach page (chat + daily briefing). Needs ANTHROPIC_API_KEY; without it the page shows notices only.
    anthropic_api_key: str = field(default_factory=lambda: os.getenv("ANTHROPIC_API_KEY", ""))
    coach_model: str = field(default_factory=lambda: os.getenv("COACH_MODEL", "claude-opus-5-5"))
    # Write a fresh briefing after each daily sync (one API call a day)
    coach_daily_briefing: bool = field(default_factory=lambda: os.getenv("COACH_DAILY_BRIEFING", "1") == "1")


def get_settings() -> Settings:
    return Settings()
