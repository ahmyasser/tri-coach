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

    # Public hostname (e.g. coach.example.com): links in emails, Google sign-in callback, allowed Host for MCP
    public_host: str = field(default_factory=lambda: os.getenv("PUBLIC_HOST", "localhost"))
    brand_name: str = field(default_factory=lambda: os.getenv("BRAND_NAME", "Tri Coach"))
    # Signs session cookies. Required in production: openssl rand -hex 32
    secret_key: str = field(default_factory=lambda: os.getenv("SECRET_KEY", ""))
    # Encrypts each user's Garmin tokens at rest (Fernet key): python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    token_key: str = field(default_factory=lambda: os.getenv("TOKEN_KEY", ""))
    # Comma-separated emails that get the admin page
    admin_emails: str = field(default_factory=lambda: os.getenv("ADMIN_EMAILS", ""))
    # Beta: new accounts need an invite code
    invite_only: bool = field(default_factory=lambda: os.getenv("INVITE_ONLY", "1") == "1")

    # Sign-in. Google: OAuth client from Google Cloud. Email links: Resend API key (without it, links are only logged)
    google_client_id: str = field(default_factory=lambda: os.getenv("GOOGLE_CLIENT_ID", ""))
    google_client_secret: str = field(default_factory=lambda: os.getenv("GOOGLE_CLIENT_SECRET", ""))
    resend_api_key: str = field(default_factory=lambda: os.getenv("RESEND_API_KEY", ""))
    email_from: str = field(default_factory=lambda: os.getenv("EMAIL_FROM", ""))

    # Per-user values, filled in from the account (see accounts.user_settings)
    athlete_name: str = ""
    athlete_focus: str = ""

    # AI coach. Free and Pro tiers differ in model and daily spend cap (USD, counted from token usage)
    anthropic_api_key: str = field(default_factory=lambda: os.getenv("ANTHROPIC_API_KEY", ""))
    coach_model: str = field(default_factory=lambda: os.getenv("FREE_MODEL", "claude-sonnet-5-5"))
    free_model: str = field(default_factory=lambda: os.getenv("FREE_MODEL", "claude-sonnet-5-5"))
    free_daily_usd: float = field(default_factory=lambda: float(os.getenv("FREE_DAILY_USD", "0.10")))
    pro_model: str = field(default_factory=lambda: os.getenv("PRO_MODEL", "claude-opus-5-5"))
    pro_daily_usd: float = field(default_factory=lambda: float(os.getenv("PRO_DAILY_USD", "0.50")))
    # Write a fresh briefing after each daily sync (one small API call per user a day; not counted against the cap)
    coach_daily_briefing: bool = field(default_factory=lambda: os.getenv("COACH_DAILY_BRIEFING", "1") == "1")


def get_settings() -> Settings:
    return Settings()
