"""Accounts for the multi-user app: users, invites, waitlist, sign-in links, Garmin tokens and AI usage.

These live in the shared (core) tables. Each user's training data lives in a private schema (``u_<id>``), opened
through ``Tenants.services(user)``, which hands back the same ``Services`` object the single-user code always used.
"""

from __future__ import annotations

import dataclasses
import hashlib
import logging
import secrets
import threading
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Callable

from .config import Settings
from .db import CORE_SCHEMA, Database
from .garmin import Throttled, WebLogin
from .services import Services
from .sync import now_str

log = logging.getLogger(__name__)

# USD per million tokens: input, output, cache write, cache read
PRICES = {
    "claude-opus-5-5": (4.0, 20.0, 5.0, 0.20),
    "claude-sonnet-5-5": (2.0, 10.0, 2.5, 0.20),
    "claude-haiku-5-5": (0.10, 0.50, 0.125, 0.01),
}

_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


def cost_micro(model: str, usage: Any) -> int:
    """Cost of one response in millionths of a dollar (so it adds up exactly in an integer column)."""
    p_in, p_out, p_cw, p_cr = PRICES.get(model, PRICES["claude-opus-5-5"])
    get = (lambda k: getattr(usage, k, None) or 0) if not isinstance(usage, dict) else (lambda k: usage.get(k) or 0)
    return round(get("input_tokens") * p_in + get("output_tokens") * p_out
                 + get("cache_creation_input_tokens") * p_cw + get("cache_read_input_tokens") * p_cr)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Accounts:
    def __init__(self, settings: Settings, db: Database):
        self.settings, self.db = settings, db
        db.init_schema(CORE_SCHEMA)
        self._fernet = None
        if settings.token_key:
            from cryptography.fernet import Fernet

            self._fernet = Fernet(settings.token_key.encode())

    # ------------------------------------------------------------ users
    def get(self, user_id: str) -> dict[str, Any] | None:
        return self.db.query_one("SELECT * FROM users WHERE id = :id", {"id": user_id})

    def by(self, column: str, value: str) -> dict[str, Any] | None:
        if column not in ("email", "google_sub", "mcp_token", "sync_token") or not value:
            return None
        if column == "email":
            value = value.strip().lower()
        return self.db.query_one(f"SELECT * FROM users WHERE {column} = :v", {"v": value})

    def list(self) -> list[dict[str, Any]]:
        return self.db.query("SELECT * FROM users ORDER BY created_at")

    def create(self, email: str, name: str | None = None, google_sub: str | None = None) -> dict[str, Any]:
        uid = uuid.uuid4().hex[:10]
        email = email.strip().lower()
        self.db.execute(
            """INSERT INTO users (id, email, name, google_sub, tier, is_admin, mcp_token, sync_token, created_at)
               VALUES (:id, :email, :name, :sub, 'free', :admin, :mcp, :sync, :now)""",
            {"id": uid, "email": email, "name": name, "sub": google_sub, "admin": int(self.admin_email(email)),
             "mcp": secrets.token_hex(24), "sync": secrets.token_hex(24), "now": now_str()},
        )
        return self.get(uid)  # type: ignore[return-value]

    _EDITABLE = {"name", "google_sub", "tier", "is_admin", "focus", "timezone", "onboarded", "last_seen_at",
                 "garmin_connected_at", "session_version", "email"}

    def update(self, user_id: str, **fields: Any) -> None:
        bad = set(fields) - self._EDITABLE
        if bad:
            raise ValueError(f"not editable: {bad}")
        sets = ", ".join(f"{k} = :{k}" for k in fields)
        self.db.execute(f"UPDATE users SET {sets} WHERE id = :id", {**fields, "id": user_id})

    def admin_email(self, email: str) -> bool:
        admins = {e.strip().lower() for e in self.settings.admin_emails.split(",") if e.strip()}
        return email.strip().lower() in admins

    def is_admin(self, user: dict[str, Any]) -> bool:
        return bool(user.get("is_admin")) or self.admin_email(user["email"])

    @staticmethod
    def schema(user: dict[str, Any]) -> str:
        return f"u_{user['id']}"

    # ---------------------------------------------------------- invites
    def create_invite(self, note: str | None = None, max_uses: int = 1) -> str:
        code = "-".join("".join(secrets.choice(_ALPHABET) for _ in range(4)) for _ in range(2))
        self.db.execute("INSERT INTO invites (code, created_at, note, max_uses) VALUES (:c, :t, :n, :m)",
                        {"c": code, "t": now_str(), "n": note, "m": max(1, max_uses)})
        return code

    def invite_ok(self, code: str | None) -> bool:
        if not code:
            return False
        row = self.db.query_one("SELECT uses, max_uses FROM invites WHERE code = :c", {"c": code.strip().upper()})
        return bool(row and row["uses"] < row["max_uses"])

    def use_invite(self, code: str) -> None:
        self.db.execute("UPDATE invites SET uses = uses + 1 WHERE code = :c", {"c": code.strip().upper()})

    def invites(self) -> list[dict[str, Any]]:
        return self.db.query("SELECT * FROM invites ORDER BY created_at DESC")

    # --------------------------------------------------------- waitlist
    def join_waitlist(self, email: str, sport: str | None = None) -> None:
        self.db.execute(
            """INSERT INTO waitlist (email, created_at, sport) VALUES (:e, :t, :s)
               ON CONFLICT (email) DO UPDATE SET sport = excluded.sport""",
            {"e": email.strip().lower(), "t": now_str(), "s": (sport or "")[:40] or None},
        )

    def waitlist(self) -> list[dict[str, Any]]:
        return self.db.query("SELECT * FROM waitlist ORDER BY created_at")

    def mark_invited(self, email: str, code: str) -> None:
        self.db.execute("UPDATE waitlist SET invited_code = :c WHERE email = :e", {"c": code, "e": email.lower()})

    # ------------------------------------------------------ email links
    def new_login_link(self, email: str, invite_code: str | None = None, minutes: int = 20) -> str:
        token = secrets.token_urlsafe(32)
        self.db.execute(
            "INSERT INTO login_links (token_hash, email, invite_code, expires_at) VALUES (:h, :e, :i, :x)",
            {"h": _hash(token), "e": email.strip().lower(), "i": invite_code,
             "x": (datetime.now() + timedelta(minutes=minutes)).isoformat(sep=" ", timespec="seconds")},
        )
        return token

    def use_login_link(self, token: str) -> dict[str, Any] | None:
        row = self.db.query_one("SELECT * FROM login_links WHERE token_hash = :h", {"h": _hash(token)})
        if not row or row["used_at"] or row["expires_at"] < now_str():
            return None
        self.db.execute("UPDATE login_links SET used_at = :t WHERE token_hash = :h", {"t": now_str(), "h": row["token_hash"]})
        return row

    # --------------------------------------------------- garmin tokens
    def save_garmin_tokens(self, user_id: str, token_json: str) -> None:
        if not self._fernet:
            raise RuntimeError("TOKEN_KEY is not set, so Garmin tokens can't be stored safely")
        enc = self._fernet.encrypt(token_json.encode()).decode()
        first = not (self.get(user_id) or {}).get("garmin_connected_at")
        self.db.execute("UPDATE users SET garmin_tokens = :t" + (", garmin_connected_at = :now" if first else "") + " WHERE id = :id",
                        {"t": enc, "now": now_str(), "id": user_id})

    def garmin_tokens(self, user: dict[str, Any]) -> str | None:
        if not user.get("garmin_tokens") or not self._fernet:
            return None
        return self._fernet.decrypt(user["garmin_tokens"].encode()).decode()

    def forget_garmin(self, user_id: str) -> None:
        self.db.execute("UPDATE users SET garmin_tokens = NULL WHERE id = :id", {"id": user_id})

    # ------------------------------------------------------------ usage
    def daily_cap(self, user: dict[str, Any]) -> float:
        return self.settings.pro_daily_usd if user["tier"] == "pro" else self.settings.free_daily_usd

    def model_for(self, user: dict[str, Any]) -> str:
        return self.settings.pro_model if user["tier"] == "pro" else self.settings.free_model

    def record_usage(self, key: str, model: str, usage: Any, day: date | None = None) -> int:
        micro = cost_micro(model, usage)
        get = (lambda k: getattr(usage, k, None) or 0) if not isinstance(usage, dict) else (lambda k: usage.get(k) or 0)
        tokens_in = get("input_tokens") + get("cache_creation_input_tokens") + get("cache_read_input_tokens")
        self.db.execute(
            """INSERT INTO ai_usage (user_id, day, cost_micro, input_tokens, output_tokens, requests)
               VALUES (:u, :d, :c, :i, :o, 1)
               ON CONFLICT (user_id, day) DO UPDATE SET cost_micro = ai_usage.cost_micro + excluded.cost_micro,
                 input_tokens = ai_usage.input_tokens + excluded.input_tokens,
                 output_tokens = ai_usage.output_tokens + excluded.output_tokens, requests = ai_usage.requests + 1""",
            {"u": key, "d": (day or date.today()).isoformat(), "c": micro, "i": tokens_in, "o": get("output_tokens")},
        )
        return micro

    def spent_today(self, key: str, day: date | None = None) -> float:
        row = self.db.query_one("SELECT cost_micro FROM ai_usage WHERE user_id = :u AND day = :d",
                                {"u": key, "d": (day or date.today()).isoformat()})
        return (row["cost_micro"] if row else 0) / 1e6

    def usage_report(self, days: int = 30) -> list[dict[str, Any]]:
        since = (date.today() - timedelta(days=days)).isoformat()
        return self.db.query(
            """SELECT user_id, SUM(cost_micro) AS cost_micro, SUM(requests) AS requests, MAX(day) AS last_day
               FROM ai_usage WHERE day >= :s GROUP BY user_id""", {"s": since})


@dataclass
class Budget:
    """A user's daily AI allowance. The coach checks it before each model call and records each response."""

    accounts: Accounts
    user_id: str
    cap_usd: float

    def remaining(self) -> float:
        return max(0.0, self.cap_usd - self.accounts.spent_today(self.user_id))

    def fraction_left(self) -> float:
        return self.remaining() / self.cap_usd if self.cap_usd else 0.0

    def record(self, model: str, usage: Any) -> None:
        self.accounts.record_usage(self.user_id, model, usage)

    def record_briefing(self, model: str, usage: Any) -> None:
        # Briefings are ours to schedule, so they're tracked (for the admin page) but don't use up the allowance.
        self.accounts.record_usage(f"{self.user_id}#briefing", model, usage)


class _Entry:
    def __init__(self, db: Database):
        self.db = db
        self.client: Any = None
        self.lock = threading.Lock()


class Tenants:
    """Per-user Services, with a small pool of open per-user database connections."""

    def __init__(self, settings: Settings, accounts: Accounts, llm_factory: Callable[[], Any] | None = None,
                 garmin_factory: Callable[[dict[str, Any], str], Any] | None = None, pool_size: int = 40):
        self.settings, self.accounts = settings, accounts
        self.llm_factory, self.garmin_factory = llm_factory, garmin_factory
        self.pool_size = pool_size
        self._pool: OrderedDict[str, _Entry] = OrderedDict()
        self._pool_lock = threading.Lock()
        self.logins: dict[str, WebLogin] = {}

    def _entry(self, user: dict[str, Any]) -> _Entry:
        with self._pool_lock:
            entry = self._pool.get(user["id"])
            if entry:
                self._pool.move_to_end(user["id"])
                return entry
            db = Database(self.settings.database_url, Accounts.schema(user))
            db.init_schema()
            entry = self._pool[user["id"]] = _Entry(db)
            while len(self._pool) > self.pool_size:
                _, old = self._pool.popitem(last=False)
                old.db.close()
            return entry

    def user_settings(self, user: dict[str, Any]) -> Settings:
        return dataclasses.replace(self.settings, athlete_name=user.get("name") or "", athlete_focus=user.get("focus") or "",
                                   coach_model=self.accounts.model_for(user), timezone=user.get("timezone") or self.settings.timezone)

    def services(self, user: dict[str, Any]) -> Services:
        entry, uid = self._entry(user), user["id"]

        def api_factory() -> Any:
            from .garmin import GarminAuthMissing, connect_tokens

            with entry.lock:
                if entry.client is None:
                    fresh = self.accounts.get(uid) or user
                    tokens = self.accounts.garmin_tokens(fresh)
                    if not tokens:
                        raise GarminAuthMissing("Garmin isn't connected yet. Press Connect Garmin on the dashboard.")
                    raw = self.garmin_factory(fresh, tokens) if self.garmin_factory else connect_tokens(tokens)
                    entry.client = Throttled(raw, self.settings.request_pause_ms)
                return entry.client

        svc = Services(settings=self.user_settings(user), db=entry.db, api_factory=api_factory, llm_factory=self.llm_factory)
        svc.user = user
        svc.budget = Budget(self.accounts, uid, self.accounts.daily_cap(user))
        svc.save_garmin_tokens = lambda: self._persist_tokens(uid, entry)
        return svc

    def _persist_tokens(self, user_id: str, entry: _Entry) -> None:
        """Garmin refreshes tokens as they're used; store the newest ones after each sync."""
        raw = getattr(entry.client, "_client", None)
        dumps = getattr(getattr(raw, "client", None), "dumps", None)
        if dumps:
            try:
                self.accounts.save_garmin_tokens(user_id, dumps())
            except Exception:
                log.exception("couldn't save refreshed Garmin tokens for %s", user_id)

    def reset_garmin(self, user_id: str) -> None:
        with self._pool_lock:
            entry = self._pool.get(user_id)
        if entry:
            entry.client = None

    def web_login(self, user: dict[str, Any]) -> WebLogin:
        uid = user["id"]
        if uid not in self.logins:
            def save(token_json: str) -> None:
                self.accounts.save_garmin_tokens(uid, token_json)
                self.reset_garmin(uid)

            self.logins[uid] = WebLogin(save)
        return self.logins[uid]

    def connected_users(self) -> list[dict[str, Any]]:
        return [u for u in self.accounts.list() if u.get("garmin_tokens")]

    def delete_user(self, user_id: str) -> None:
        """Delete an account and all its data (training schema, chat, Garmin tokens, usage)."""
        user = self.accounts.get(user_id)
        if not user:
            return
        with self._pool_lock:
            entry = self._pool.pop(user_id, None)
        if entry:
            entry.db.close()
        schema = Accounts.schema(user)
        core = self.accounts.db
        if core.kind == "postgres":
            core.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            core.execute(f'DROP ROLE IF EXISTS "{schema}_ro"')
        elif "sqlite:///" in self.settings.database_url and ":memory:" not in self.settings.database_url:
            from pathlib import Path

            Path(self.settings.database_url.split("sqlite:///", 1)[1]).with_name(f"{schema}.db").unlink(missing_ok=True)
        core.execute("DELETE FROM ai_usage WHERE user_id IN (:u, :b)", {"u": user_id, "b": f"{user_id}#briefing"})
        core.execute("DELETE FROM users WHERE id = :u", {"u": user_id})
        self.logins.pop(user_id, None)


def build_tenants(settings: Settings | None = None) -> Tenants:
    from .config import get_settings

    settings = settings or get_settings()
    return Tenants(settings, Accounts(settings, Database(settings.database_url)))
