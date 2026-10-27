"""Background daily sync for every connected user (runs inside the web process)."""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from .garmin import GarminAuthMissing
from .services import Services
from .sync import SyncBusy

if TYPE_CHECKING:
    from .accounts import Tenants

log = logging.getLogger(__name__)


def next_run(now: datetime, hhmm: str) -> datetime:
    hour, minute = (int(x) for x in hhmm.split(":"))
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return candidate if candidate > now else candidate + timedelta(days=1)


def run_sync(svc: Services, kind: str) -> None:
    """Sync one user's Garmin data; the first run is a full backfill. Never raises."""
    who = (svc.user or {}).get("id", "-")
    try:
        first = svc.db.query_one("SELECT COUNT(*) AS n FROM activities")["n"] == 0
        stats = svc.syncer().run(kind="backfill" if first else kind, full=first)
        log.info("sync %s for %s finished: %s", kind, who, stats)
        if svc.save_garmin_tokens:
            svc.save_garmin_tokens()
        if kind in ("daily", "startup", "connect"):
            from .coach import safe_briefing

            safe_briefing(svc)
    except SyncBusy:
        log.info("sync for %s already running, skipped", who)
    except GarminAuthMissing as exc:
        log.error("%s: %s", who, exc)
    except Exception:
        log.exception("sync failed for %s", who)


def sync_everyone(tenants: Tenants, kind: str, only_stale: bool = False) -> None:
    for user in tenants.connected_users():
        svc = tenants.services(user)
        if only_stale:
            last = svc.db.query_one("SELECT MAX(started_at) AS t FROM sync_runs WHERE status = 'ok'")
            if last and last["t"] and last["t"] >= (datetime.now() - timedelta(hours=26)).isoformat(sep=" "):
                continue
        run_sync(svc, kind)


def start(tenants: Tenants, stop: threading.Event) -> threading.Thread:
    tz = ZoneInfo(tenants.settings.timezone)

    def loop() -> None:
        # Catch up straight away for anyone who missed a day (e.g. after a deploy).
        sync_everyone(tenants, "startup", only_stale=True)
        while not stop.is_set():
            now = datetime.now(tz)
            target = next_run(now, tenants.settings.sync_time)
            log.info("next Garmin sync at %s", target.isoformat())
            if stop.wait((target - now).total_seconds()):
                break
            sync_everyone(tenants, "daily")

    t = threading.Thread(target=loop, name="daily-sync", daemon=True)
    t.start()
    return t
