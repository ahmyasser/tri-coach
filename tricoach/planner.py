"""Planned workouts: saved as drafts in the database, then pushed to Garmin Connect's calendar."""

from __future__ import annotations

import json
import uuid
from datetime import date
from typing import Any, Callable

from .db import Database
from .garmin import GarminAPI
from .sync import now_str
from .workouts import WorkoutSpec, describe, to_garmin

ApiFactory = Callable[[], GarminAPI]


class Planner:
    def __init__(self, db: Database, api_factory: ApiFactory):
        self.db = db
        self._api_factory = api_factory

    def _css(self) -> float | None:
        return self.db.get_setting("css_pace_s")

    def create(self, spec: dict[str, Any] | WorkoutSpec, notes: str | None = None, scheduled_date: str | None = None) -> dict[str, Any]:
        spec = spec if isinstance(spec, WorkoutSpec) else WorkoutSpec.model_validate(spec)
        to_garmin(spec, self._css())  # fail early if it can't be built
        if scheduled_date:
            date.fromisoformat(scheduled_date)
        wid, now = uuid.uuid4().hex[:12], now_str()
        self.db.execute(
            """INSERT INTO workouts (id, name, sport, spec, notes, status, scheduled_date, created_at, updated_at)
               VALUES (:id, :name, :sport, :spec, :notes, 'draft', :d, :now, :now)""",
            {"id": wid, "name": spec.name, "sport": spec.sport, "spec": spec.model_dump_json(exclude_none=True),
             "notes": notes, "d": scheduled_date, "now": now},
        )
        return self.get(wid)

    def get(self, wid: str) -> dict[str, Any]:
        row = self.db.query_one("SELECT * FROM workouts WHERE id = :id", {"id": wid})
        if not row:
            raise KeyError(f"No workout {wid}")
        spec = WorkoutSpec.model_validate_json(row["spec"])
        return {**{k: row[k] for k in ("id", "name", "sport", "status", "scheduled_date", "garmin_workout_id",
                                       "garmin_schedule_id", "notes", "created_at")},
                "summary": describe(spec), "spec": json.loads(row["spec"])}

    def list(self, since: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT id FROM workouts WHERE status <> 'deleted'"
        params: dict[str, Any] = {}
        if since:
            sql += " AND (scheduled_date IS NULL OR scheduled_date >= :since)"
            params["since"] = since
        if status:
            sql += " AND status = :status"
            params["status"] = status
        sql += " ORDER BY COALESCE(scheduled_date, '9999'), created_at"
        return [self.get(r["id"]) for r in self.db.query(sql, params)]

    def update(self, wid: str, spec: dict[str, Any] | None = None, notes: str | None = None,
               scheduled_date: str | None = None) -> dict[str, Any]:
        cur = self.get(wid)
        if cur["status"] != "draft":
            raise ValueError("Only drafts can be edited; unpush it first")
        new_spec = WorkoutSpec.model_validate(spec) if spec else WorkoutSpec.model_validate(cur["spec"])
        to_garmin(new_spec, self._css())
        self.db.execute(
            """UPDATE workouts SET name = :name, sport = :sport, spec = :spec, notes = :notes,
               scheduled_date = :d, updated_at = :now WHERE id = :id""",
            {"id": wid, "name": new_spec.name, "sport": new_spec.sport,
             "spec": new_spec.model_dump_json(exclude_none=True),
             "notes": notes if notes is not None else cur["notes"],
             "d": scheduled_date or cur["scheduled_date"], "now": now_str()},
        )
        return self.get(wid)

    def push(self, wid: str, scheduled_date: str | None = None) -> dict[str, Any]:
        """Upload to Garmin Connect and put it on the calendar (it then syncs to the watch)."""
        cur = self.get(wid)
        when = scheduled_date or cur["scheduled_date"]
        if not when:
            raise ValueError("Give a date (YYYY-MM-DD) to schedule the workout on")
        date.fromisoformat(when)
        if cur["status"] in ("scheduled",):
            raise ValueError("Already on the Garmin calendar; unpush first to change it")
        api = self._api_factory()
        spec = WorkoutSpec.model_validate(cur["spec"])
        garmin_id = cur["garmin_workout_id"]
        if not garmin_id:
            created = api.upload_workout(to_garmin(spec, self._css()))
            garmin_id = int(created["workoutId"])
            self.db.execute("UPDATE workouts SET garmin_workout_id = :g, status = 'uploaded', updated_at = :now WHERE id = :id",
                            {"g": garmin_id, "now": now_str(), "id": wid})
        sched = api.schedule_workout(garmin_id, when) or {}
        sched_id = sched.get("workoutScheduleId") or sched.get("scheduleId") or sched.get("id")
        self.db.execute(
            """UPDATE workouts SET status = 'scheduled', scheduled_date = :d, garmin_schedule_id = :s, updated_at = :now
               WHERE id = :id""",
            {"d": when, "s": sched_id, "now": now_str(), "id": wid},
        )
        return self.get(wid)

    def unpush(self, wid: str, delete_from_garmin: bool = True) -> dict[str, Any]:
        """Remove from the Garmin calendar (and library) and turn it back into a draft."""
        cur = self.get(wid)
        api = self._api_factory()
        if cur["garmin_schedule_id"]:
            api.unschedule_workout(int(cur["garmin_schedule_id"]))
        if delete_from_garmin and cur["garmin_workout_id"]:
            api.delete_workout(int(cur["garmin_workout_id"]))
        self.db.execute(
            """UPDATE workouts SET status = 'draft', garmin_schedule_id = NULL,
               garmin_workout_id = CASE WHEN :del = 1 THEN NULL ELSE garmin_workout_id END, updated_at = :now
               WHERE id = :id""",
            {"del": 1 if delete_from_garmin else 0, "now": now_str(), "id": wid},
        )
        return self.get(wid)

    def delete(self, wid: str) -> None:
        cur = self.get(wid)
        if cur["status"] != "draft":
            self.unpush(wid)
        self.db.execute("UPDATE workouts SET status = 'deleted', updated_at = :now WHERE id = :id",
                        {"now": now_str(), "id": wid})
