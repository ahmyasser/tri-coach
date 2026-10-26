"""Background daily sync (runs inside the web process)."""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .garmin import GarminAuthMissing, tokens_present
from .services import Services
from .sync import SyncBusy

log = logging.getLogger(__name__)


def next_run(now: datetime, hhmm: str) -> datetime:
    hour, minute = (int(x) for x in hhmm.split(":"))
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return candidate if candidate > now else candidate + timedelta(days=1)


def run_sync(svc: Services, kind: str) -> None:
    try:
        first = svc.db.query_one("SELECT COUNT(*) AS n FROM activities")["n"] == 0
        stats = svc.syncer().run(kind="backfill" if first else kind, full=first)
        log.info("sync %s finished: %s", kind, stats)
    except SyncBusy:
        log.info("sync already running, skipped")
    except GarminAuthMissing as exc:
        log.error("%s", exc)
    except Exception:
        log.exception("sync failed")


def start(svc: Services, stop: threading.Event) -> threading.Thread:
    tz = ZoneInfo(svc.settings.timezone)

    def loop() -> None:
        # Catch up straight away if we've never synced (first deploy) or missed a day.
        last = svc.db.query_one("SELECT MAX(started_at) AS t FROM sync_runs WHERE status = 'ok'")
        stale = not last or not last["t"] or last["t"] < (datetime.now() - timedelta(hours=26)).isoformat(sep=" ")
        if stale and tokens_present(svc.settings.garmin_tokenstore):
            run_sync(svc, "startup")
        while not stop.is_set():
            now = datetime.now(tz)
            target = next_run(now, svc.settings.sync_time)
            log.info("next Garmin sync at %s", target.isoformat())
            if stop.wait((target - now).total_seconds()):
                break
            run_sync(svc, "daily")

    t = threading.Thread(target=loop, name="daily-sync", daemon=True)
    t.start()
    return t
