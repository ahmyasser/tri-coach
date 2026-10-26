"""MCP server: lets Claude read your training data and manage workouts on your Garmin calendar."""

from __future__ import annotations

import re
import threading
import uuid
from datetime import date, timedelta
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer

from . import analytics
from .services import Services
from .sync import SyncBusy, now_str
from .workouts import WorkoutSpec

INSTRUCTIONS = """\
Tri-coach: Ahmed's triathlon training database (Garmin data, synced daily) plus his Garmin workout calendar.

How to coach with these tools:
- Start from the data: training_overview, training_volume, swim_trend, list_activities, wellness.
- Swimming is the main focus. Pool swims flagged with quality_flags (wrong pool length) are excluded
  from pace stats; say so when it matters.
- Decisions you make together go in save_note (goals, test results, plan changes) so future chats know them.
  Read get_athlete_profile first in a new conversation.
- Workouts: create_workout makes a DRAFT and returns a readable summary. Show it and get explicit approval
  before push_workout, which puts it on his real Garmin calendar/watch.
"""

SCHEMA_DOC = """Tables (read-only):
activities(activity_id, start_local, local_date, sport[swim|bike|run|multisport|transition|strength|other], type_key,
  name, distance_m, duration_s, moving_s, avg_speed[m/s], avg_hr, max_hr, calories, elevation_gain, avg_power,
  norm_power, avg_cadence, training_load, aerobic_te, anaerobic_te, pool_length_m, avg_swolf, avg_strokes[per length],
  active_lengths, hr_z1_s..hr_z5_s, is_parent[1=multisport parent], parent_id[leg of a multisport], quality_flags, raw[json])
laps(activity_id, lap_index, distance_m, duration_s, moving_s, avg_speed, avg_hr, max_hr, avg_power, stroke, avg_strokes,
  swolf, lengths, is_rest, raw)
wellness(day, resting_hr, hrv_last_night, hrv_weekly_avg, hrv_status, sleep_s, sleep_score, body_battery_change)
workouts(id, name, sport, spec[json], notes, status[draft|uploaded|scheduled|deleted], garmin_workout_id, scheduled_date)
coach_notes(id, created_at, topic, body); athlete_settings(key, value[json]); sync_runs(...)
Count volume with is_parent = 0 (legs are separate rows). Dates are 'YYYY-MM-DD' text."""

_WRITE = re.compile(r"\b(insert|update|delete|drop|alter|create|truncate|grant|revoke|copy|attach|pragma|vacuum)\b", re.I)


def build_mcp(svc: Services) -> MCPServer:
    mcp = MCPServer("tri-coach", instructions=INSTRUCTIONS)
    db = svc.db

    # ------------------------------------------------------------- reading
    @mcp.tool()
    def training_overview() -> dict[str, Any]:
        """Data coverage per sport, last sync, data-quality issues, and current acute vs chronic training load."""
        return {**analytics.overview(db), "load": analytics.load_status(db)}

    @mcp.tool()
    def training_volume(period: Literal["week", "month"] = "week", count: int = 12) -> list[dict[str, Any]]:
        """Hours, km and Garmin training load per sport for the last `count` weeks (Mon-start) or months."""
        return analytics.volume_by_period(db, period, max(1, min(count, 104)))

    @mcp.tool()
    def list_activities(days: int = 14, sport: Literal["swim", "bike", "run", "multisport", "strength", "other"] | None = None) -> list[dict[str, Any]]:
        """Activities in the last `days` days, newest first, with pace/speed and HR."""
        return analytics.recent_activities(db, max(1, min(days, 3650)), sport)

    @mcp.tool()
    def get_activity(activity_id: int) -> dict[str, Any]:
        """One activity with all laps/intervals (swim: stroke, pace/100m, strokes per length), HR zones, training effect."""
        return analytics.activity_detail(db, activity_id) or {"error": f"activity {activity_id} not found"}

    @mcp.tool()
    def swim_trend(months: int = 12) -> list[dict[str, Any]]:
        """Swim progress per month and venue (25 m pool / 50 m pool / open water): sessions, km, average pace,
        median freestyle rep pace (100-400 m reps, glitches filtered), SWOLF and strokes per length."""
        return analytics.swim_trend(db, max(1, min(months, 60)))

    @mcp.tool()
    def swim_best_efforts(since: str | None = None) -> dict[str, Any]:
        """Fastest freestyle intervals (100/200/400/800+) since a date (default 180 days) and CSS (saved or estimated)."""
        return analytics.swim_best_efforts(db, since)

    @mcp.tool()
    def race_results() -> list[dict[str, Any]]:
        """Multisport races with swim/T1/bike/T2/run splits."""
        return analytics.race_results(db)

    @mcp.tool()
    def wellness(days: int = 28) -> list[dict[str, Any]]:
        """Daily resting HR, overnight HRV (and status), sleep duration and score."""
        return analytics.wellness_trend(db, max(1, min(days, 400)))

    @mcp.tool()
    def intensity_distribution(weeks: int = 8) -> dict[str, Any]:
        """Share of time in HR zones 1-5 per sport over the last `weeks` weeks."""
        return analytics.zone_distribution(db, max(1, min(weeks, 52)))

    @mcp.tool(description="Run a read-only SQL SELECT for anything the other tools don't cover. Max 500 rows.\n" + SCHEMA_DOC)
    def run_sql(sql: str) -> dict[str, Any]:
        stmt = sql.strip().rstrip(";")
        if ";" in stmt or not re.match(r"^\s*(select|with)\b", stmt, re.I) or _WRITE.search(stmt):
            return {"error": "Only a single SELECT/WITH query is allowed"}
        with db.readonly():
            rows = db.query_raw(stmt, 500)
        return {"rows": rows, "row_count": len(rows)}

    # -------------------------------------------------- athlete & decisions
    @mcp.tool()
    def get_athlete_profile() -> dict[str, Any]:
        """Saved athlete settings (CSS, goals, races, availability...) and the most recent coaching notes."""
        settings = {r["key"]: r["value"] for r in db.query("SELECT key, value FROM athlete_settings ORDER BY key")}
        notes = db.query("SELECT created_at, topic, body FROM coach_notes ORDER BY created_at DESC LIMIT 20")
        return {"settings": settings, "recent_notes": notes}

    @mcp.tool()
    def set_athlete_setting(key: str, value: str) -> dict[str, Any]:
        """Save an athlete setting, e.g. goal_race='Ironman 70.3 Egypt 2027-03-07', weekly_hours='8-10',
        pool_access='25 m Mon/Wed/Fri'. For CSS use set_css."""
        db.set_setting(key, value, now_str())
        return {"saved": key}

    @mcp.tool()
    def set_css(pace_per_100m: str, source: str = "test") -> dict[str, Any]:
        """Save Critical Swim Speed, e.g. '1:44' (from a 400/200 test). Swim workouts use it for CSS-offset targets."""
        secs = analytics.parse_pace(pace_per_100m)
        if not 60 <= secs <= 240:
            return {"error": "CSS should be between 1:00 and 4:00 per 100 m"}
        db.set_setting("css_pace_s", secs, now_str())
        db.set_setting("css_source", f"{source} ({date.today().isoformat()})", now_str())
        return {"css_pace_100m": analytics.pace_per_100m(secs)}

    @mcp.tool()
    def save_note(body: str, topic: str | None = None) -> dict[str, Any]:
        """Record a coaching decision, test result or observation (e.g. topic='plan', 'injury', 'test')."""
        nid = uuid.uuid4().hex[:12]
        db.execute("INSERT INTO coach_notes (id, created_at, topic, body) VALUES (:id, :t, :topic, :body)",
                   {"id": nid, "t": now_str(), "topic": topic, "body": body})
        return {"saved": nid}

    @mcp.tool()
    def list_notes(topic: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        """Coaching notes, newest first, optionally filtered by topic."""
        sql = "SELECT id, created_at, topic, body FROM coach_notes"
        params: dict[str, Any] = {"lim": max(1, min(limit, 500))}
        if topic:
            sql += " WHERE topic = :topic"
            params["topic"] = topic
        return db.query(sql + " ORDER BY created_at DESC LIMIT :lim", params)

    # ---------------------------------------------------------------- sync
    @mcp.tool()
    def sync_now(full: bool = False) -> dict[str, Any]:
        """Pull new data from Garmin now (normally runs daily). full=True re-walks all history (slow)."""
        def job() -> None:
            try:
                svc.syncer().run(kind="manual-full" if full else "manual", full=full)
            except SyncBusy:
                pass

        threading.Thread(target=job, daemon=True).start()
        return {"started": True, "check": "training_overview.last_sync in a minute or two"}

    # ------------------------------------------------------------ workouts
    @mcp.tool()
    def create_workout(spec: WorkoutSpec, scheduled_date: str | None = None, notes: str | None = None) -> dict[str, Any]:
        """Create a DRAFT structured workout (not yet on Garmin). Returns a readable summary to confirm with the athlete.

        spec: {name, sport: swim|bike|run, pool_length_m (swim, default 25), description, steps:[...]}
        step: {kind: warmup|interval|recovery|rest|cooldown|other, one of distance_m | seconds | lap_button,
               swim: stroke free|back|breast|fly|im|drill|any|mixed, equipment fins|kickboard|paddles|pull_buoy|snorkel,
               css_offset_s (e.g. +3 = 3 s/100m slower than CSS),
               targets (one): pace '1:40-1:44' (per 100 m swim / per km run) | hr_zone 1-5 | hr_bpm '135-145' |
               power_zone | power_w '200-220'; optional cadence '85-95'; note (shown on the watch)}
        repeat: {repeat: n, steps:[...], skip_last_rest: bool}
        """
        return svc.planner.create(spec, notes=notes, scheduled_date=scheduled_date)

    @mcp.tool()
    def update_workout(workout_id: str, spec: WorkoutSpec | None = None, scheduled_date: str | None = None,
                       notes: str | None = None) -> dict[str, Any]:
        """Edit a draft workout (spec, date or notes)."""
        return svc.planner.update(workout_id, spec.model_dump(exclude_none=True) if spec else None, notes, scheduled_date)

    @mcp.tool()
    def list_workouts(since: str | None = None, status: Literal["draft", "uploaded", "scheduled"] | None = None) -> list[dict[str, Any]]:
        """Planned workouts (drafts and ones on the Garmin calendar). `since` defaults to 7 days ago."""
        return svc.planner.list(since or (date.today() - timedelta(days=7)).isoformat(), status)

    @mcp.tool()
    def push_workout(workout_id: str, scheduled_date: str | None = None) -> dict[str, Any]:
        """Upload a draft to Garmin Connect and schedule it (YYYY-MM-DD). Only after the athlete approved it."""
        return svc.planner.push(workout_id, scheduled_date)

    @mcp.tool()
    def unpush_workout(workout_id: str) -> dict[str, Any]:
        """Remove a workout from the Garmin calendar and library; it stays here as a draft."""
        return svc.planner.unpush(workout_id)

    @mcp.tool()
    def delete_workout(workout_id: str) -> dict[str, Any]:
        """Delete a planned workout (also removes it from Garmin if it was pushed)."""
        svc.planner.delete(workout_id)
        return {"deleted": workout_id}

    return mcp
