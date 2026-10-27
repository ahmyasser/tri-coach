"""Pull data from Garmin into the database: full backfill and daily incremental sync."""

from __future__ import annotations

import json
import logging
import threading
import uuid
from datetime import date, datetime, timedelta
from typing import Any

from .db import Database, upsert_sql
from .garmin import GarminAPI
from .normalize import ACTIVITY_COLUMNS, LAP_COLUMNS, activity_row, child_summary_to_activity, lap_rows

log = logging.getLogger(__name__)

PAGE = 100
DETAIL_SPORTS = ("swim", "run", "bike")  # activities whose laps/intervals we store
_sync_locks: dict[str | None, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock_for(schema: str | None) -> threading.Lock:
    with _locks_guard:
        return _sync_locks.setdefault(schema, threading.Lock())


def now_str() -> str:
    return datetime.now().replace(microsecond=0).isoformat(sep=" ")


class SyncBusy(RuntimeError):
    pass


class Syncer:
    def __init__(self, db: Database, api: GarminAPI, resync_days: int = 3, wellness_backfill_days: int = 400):
        self.db = db
        self.api = api
        self.resync_days = resync_days
        self.wellness_backfill_days = wellness_backfill_days
        self._act_sql = upsert_sql("activities", ACTIVITY_COLUMNS, ["activity_id"])
        self._lap_sql = upsert_sql("laps", LAP_COLUMNS, ["activity_id", "lap_index"])

    # ------------------------------------------------------------------ public
    def run(self, kind: str = "daily", full: bool = False) -> dict[str, Any]:
        """Run a sync. ``full=True`` re-walks the whole activity history."""
        lock = _lock_for(self.db.schema)
        if not lock.acquire(blocking=False):
            raise SyncBusy("A sync is already running")
        run_id = uuid.uuid4().hex
        self.db.execute(
            "INSERT INTO sync_runs (id, kind, started_at, status) VALUES (:id, :kind, :t, 'running')",
            {"id": run_id, "kind": kind, "t": now_str()},
        )
        stats: dict[str, Any] = {}
        status = "ok"
        try:
            stats["activities"] = self.sync_activities(full=full)
            stats["details"] = self.sync_details()
            stats["wellness_days"] = self.sync_wellness()
        except Exception as exc:  # recorded, then re-raised for the caller/logs
            status = "error"
            stats["error"] = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            self.db.execute(
                "UPDATE sync_runs SET finished_at = :t, status = :s, details = :d WHERE id = :id",
                {"t": now_str(), "s": status, "d": json.dumps(stats), "id": run_id},
            )
            lock.release()
        return stats

    # -------------------------------------------------------------- activities
    def sync_activities(self, full: bool = False) -> int:
        latest = self.db.query_one("SELECT MAX(local_date) AS d FROM activities WHERE parent_id IS NULL")
        cutoff = None
        if not full and latest and latest["d"]:
            cutoff = (date.fromisoformat(latest["d"]) - timedelta(days=self.resync_days)).isoformat()

        start, count, now = 0, 0, now_str()
        while True:
            page = self.api.get_activities(start, PAGE) or []
            if not page:
                break
            rows = [activity_row(a, now) for a in page]
            # A re-sync must not reset details_synced, so keep it out of the upsert columns.
            self.db.executemany(self._act_sql, rows)
            count += len(rows)
            oldest = min(r["local_date"] for r in rows)
            if len(page) < PAGE or (cutoff and oldest < cutoff):
                break
            start += PAGE
        log.info("activities upserted: %s", count)
        return count

    def sync_details(self, limit: int | None = None) -> int:
        """Fetch laps (and multisport legs) for activities that don't have them yet."""
        todo = self.db.query(
            """SELECT activity_id, sport, is_parent FROM activities
               WHERE details_synced = 0 AND (is_parent = 1 OR sport IN ('swim', 'run', 'bike'))
               ORDER BY start_local DESC"""
        )
        if limit:
            todo = todo[:limit]
        done = 0
        for item in todo:
            aid = int(item["activity_id"])
            try:
                if item["is_parent"]:
                    self._sync_multisport(aid)
                else:
                    self._store_laps(aid)
                self.db.execute("UPDATE activities SET details_synced = 1 WHERE activity_id = :id", {"id": aid})
                done += 1
            except Exception as exc:
                if "TooManyRequests" in type(exc).__name__:
                    log.warning("Rate limited after %s detail fetches; will continue next sync", done)
                    break
                log.warning("details for %s failed: %s", aid, exc)
        return done

    def _store_laps(self, activity_id: int) -> None:
        splits = self.api.get_activity_splits(activity_id) or {}
        self.db.executemany(self._lap_sql, lap_rows(activity_id, splits))

    def _sync_multisport(self, parent_id: int) -> None:
        detail = self.api.get_activity(parent_id) or {}
        child_ids = (detail.get("metadataDTO") or {}).get("childIds") or detail.get("childIds") or []
        now = now_str()
        for cid in child_ids:
            child = self.api.get_activity(int(cid)) or {}
            row = activity_row(child_summary_to_activity(child), now, parent_id=parent_id)
            self.db.executemany(self._act_sql, [row])
            if row["sport"] in DETAIL_SPORTS:
                self._store_laps(int(cid))
            self.db.execute("UPDATE activities SET details_synced = 1 WHERE activity_id = :id", {"id": int(cid)})

    # ---------------------------------------------------------------- wellness
    def sync_wellness(self, today: date | None = None) -> int:
        today = today or date.today()
        latest = self.db.query_one("SELECT MAX(day) AS d FROM wellness")
        if latest and latest["d"]:
            start = date.fromisoformat(latest["d"]) - timedelta(days=self.resync_days)
        else:
            start = today - timedelta(days=self.wellness_backfill_days)

        days: dict[str, dict[str, Any]] = {}

        def day(d: str) -> dict[str, Any]:
            return days.setdefault(d, {"day": d, "raw": {}})

        chunk = 28
        cur = start
        while cur <= today:
            end = min(cur + timedelta(days=chunk - 1), today)
            s, e = cur.isoformat(), end.isoformat()
            for row in self._safe(self.api.get_sleep_daily, s, e) or []:
                d = row.get("calendarDate")
                if not d:
                    continue
                v = row.get("values") or {}
                rec = day(d)
                rec["sleep_s"] = v.get("totalSleepTimeInSeconds")
                rec["sleep_score"] = v.get("sleepScore")
                rec["body_battery_change"] = v.get("bodyBatteryChange")
                rec.setdefault("resting_hr", v.get("restingHeartRate"))
                rec.setdefault("hrv_last_night", v.get("avgOvernightHrv"))
                rec["raw"]["sleep"] = v
            hrv = self._safe(self.api.get_hrv_data_range, s, e) or {}
            for row in hrv.get("hrvSummaries") or []:
                d = row.get("calendarDate")
                if not d:
                    continue
                rec = day(d)
                rec["hrv_last_night"] = row.get("lastNightAvg") or rec.get("hrv_last_night")
                rec["hrv_weekly_avg"] = row.get("weeklyAvg")
                rec["hrv_status"] = row.get("status")
                rec["raw"]["hrv"] = row
            for row in self._safe(self.api.get_rhr_daily, s, e) or []:
                d = row.get("calendarDate")
                if d and row.get("value") is not None:
                    day(d)["resting_hr"] = row["value"]
            cur = end + timedelta(days=1)

        cols = ["day", "resting_hr", "hrv_last_night", "hrv_weekly_avg", "hrv_status", "sleep_s",
                "sleep_score", "body_battery_change", "raw", "updated_at"]
        now = now_str()
        rows = []
        for rec in days.values():
            row = {c: rec.get(c) for c in cols}
            row["raw"] = json.dumps(rec["raw"], separators=(",", ":"))
            row["updated_at"] = now
            rows.append(row)
        self.db.executemany(upsert_sql("wellness", cols, ["day"]), rows)
        return len(rows)

    @staticmethod
    def _safe(fn: Any, *args: Any) -> Any:
        """Wellness endpoints vary by device; a failing one shouldn't stop the sync."""
        try:
            return fn(*args)
        except Exception as exc:
            if "TooManyRequests" in type(exc).__name__:
                raise
            log.warning("%s%s failed: %s", getattr(fn, "__name__", fn), args, exc)
            return None
