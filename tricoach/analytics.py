"""Training analysis shared by the dashboard and the MCP tools.

Data volumes are small (hundreds to a few thousand activities), so aggregation is done
in Python on top of simple SELECTs. That keeps SQL portable across SQLite and Postgres.
"""

from __future__ import annotations

import json
import statistics
from collections import defaultdict
from datetime import date, timedelta
from typing import Any

from .db import Database

SPORTS = ("swim", "bike", "run", "strength", "other")


# ------------------------------------------------------------------ formatting
def fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    s = int(round(seconds))
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _int(v: float | None) -> int | None:
    return None if v is None else int(round(v))


def pace_per_100m(seconds_per_100m: float | None) -> str:
    return "-" if not seconds_per_100m else fmt_duration(seconds_per_100m)


def pace_per_km(speed_mps: float | None) -> str:
    return "-" if not speed_mps else fmt_duration(1000.0 / speed_mps)


def parse_pace(text: str) -> float:
    """'1:44' -> 104.0 seconds."""
    parts = [float(p) for p in text.strip().split(":")]
    total = 0.0
    for p in parts:
        total = total * 60 + p
    return total


def week_start(day: str) -> str:
    d = date.fromisoformat(day[:10])
    return (d - timedelta(days=d.weekday())).isoformat()


# -------------------------------------------------------------------- queries
def _training_rows(db: Database, since: str, until: str | None = None) -> list[dict[str, Any]]:
    """Activities that count toward volume: real sessions, multisport *legs* (not the parent)."""
    sql = """SELECT activity_id, local_date, start_local, sport, type_key, name, distance_m, duration_s,
                    moving_s, avg_speed, avg_hr, training_load, pool_length_m, avg_swolf, avg_strokes,
                    quality_flags, parent_id
             FROM activities
             WHERE is_parent = 0 AND sport <> 'transition' AND local_date >= :since"""
    params: dict[str, Any] = {"since": since}
    if until:
        sql += " AND local_date <= :until"
        params["until"] = until
    return db.query(sql + " ORDER BY start_local", params)


def volume_by_period(db: Database, period: str = "week", count: int = 12, today: date | None = None) -> list[dict[str, Any]]:
    """Hours / distance / load per sport for the last ``count`` weeks or months."""
    today = today or date.today()
    if period == "week":
        first = date.fromisoformat(week_start(today.isoformat())) - timedelta(weeks=count - 1)
        keys = [(first + timedelta(weeks=i)).isoformat() for i in range(count)]
        key_of = week_start
    else:
        y, m = today.year, today.month
        keys = []
        for _ in range(count):
            keys.append(f"{y:04d}-{m:02d}")
            m -= 1
            if m == 0:
                y, m = y - 1, 12
        keys.reverse()
        first = date.fromisoformat(keys[0] + "-01")
        key_of = lambda d: d[:7]  # noqa: E731

    buckets: dict[str, dict[str, Any]] = {
        k: {"period": k, "sessions": 0, "hours": 0.0, "load": 0.0,
            **{f"{s}_h": 0.0 for s in SPORTS}, **{f"{s}_km": 0.0 for s in ("swim", "bike", "run")}}
        for k in keys
    }
    for r in _training_rows(db, first.isoformat(), today.isoformat()):
        b = buckets.get(key_of(r["local_date"]))
        if b is None:
            continue
        sport = r["sport"] if r["sport"] in SPORTS else "other"
        hours = (r["duration_s"] or 0) / 3600
        b["sessions"] += 1
        b["hours"] += hours
        b["load"] += r["training_load"] or 0
        b[f"{sport}_h"] += hours
        if sport in ("swim", "bike", "run"):
            b[f"{sport}_km"] += _clean_distance(r) / 1000
    for b in buckets.values():
        for k, v in list(b.items()):
            if isinstance(v, float):
                b[k] = round(v, 2)
    return [buckets[k] for k in keys]


def _clean_distance(r: dict[str, Any]) -> float:
    """Distance with known pool-length mistakes corrected (e.g. 25 m pool logged as 50 m)."""
    dist = r["distance_m"] or 0
    flags = r.get("quality_flags") or ""
    if "pool_length_suspect:likely_" in flags and r.get("pool_length_m"):
        true_len = float(flags.split("likely_")[1].split("m")[0])
        dist = dist * true_len / r["pool_length_m"]
    return dist


def load_status(db: Database, today: date | None = None) -> dict[str, Any]:
    """Acute (last 7 days) vs chronic (last 28 days, per week) Garmin training load."""
    today = today or date.today()
    rows = _training_rows(db, (today - timedelta(days=27)).isoformat(), today.isoformat())
    acute_since = (today - timedelta(days=6)).isoformat()
    acute = sum(r["training_load"] or 0 for r in rows if r["local_date"] >= acute_since)
    chronic_week = sum(r["training_load"] or 0 for r in rows) / 4
    hours7 = sum((r["duration_s"] or 0) for r in rows if r["local_date"] >= acute_since) / 3600
    ratio = round(acute / chronic_week, 2) if chronic_week else None
    return {
        "acute_load_7d": round(acute),
        "chronic_load_per_week_28d": round(chronic_week),
        "acute_chronic_ratio": ratio,
        "hours_7d": round(hours7, 1),
        "interpretation": (
            None if ratio is None else
            "well below usual (recovery / detraining)" if ratio < 0.8 else
            "in the usual range" if ratio <= 1.3 else
            "spike: noticeably above usual, watch fatigue"
        ),
    }


# ---------------------------------------------------------------------- swim
def _plausible_free_lap(lap: dict[str, Any], pool_m: float | None) -> bool:
    """Filter out wrist-detection glitches (merged/split lengths)."""
    if lap["is_rest"] or (lap["stroke"] or "").upper() != "FREESTYLE" or not lap["duration_s"]:
        return False
    if not lap["distance_m"] or lap["distance_m"] < 50:
        return False
    pace = lap["duration_s"] / lap["distance_m"] * 100
    if pace < 70 or pace > 240:
        return False
    if pool_m and lap["avg_strokes"]:
        per_25 = lap["avg_strokes"] * 25.0 / pool_m
        if per_25 < 8 or per_25 > 17:
            return False
    return True


def _swim_laps(db: Database, since: str) -> list[dict[str, Any]]:
    return db.query(
        """SELECT l.*, a.local_date, a.pool_length_m, a.quality_flags
           FROM laps l JOIN activities a ON a.activity_id = l.activity_id
           WHERE a.type_key = 'lap_swimming' AND a.local_date >= :since
           ORDER BY a.start_local, l.lap_index""",
        {"since": since},
    )


def swim_trend(db: Database, months: int = 12, today: date | None = None) -> list[dict[str, Any]]:
    """Per month and pool length: volume, average pace, SWOLF, strokes/length, typical rep pace."""
    today = today or date.today()
    since = (today.replace(day=1) - timedelta(days=31 * (months - 1))).replace(day=1).isoformat()
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for r in _training_rows(db, since):
        if r["sport"] != "swim":
            continue
        venue = "open water" if r["type_key"] == "open_water_swimming" else f"{(r['pool_length_m'] or 0):g} m pool"
        g = groups.setdefault((r["local_date"][:7], venue), {
            "month": r["local_date"][:7], "venue": venue, "sessions": 0, "km": 0.0,
            "_t": 0.0, "_d": 0.0, "_swolf": [], "_strokes": [], "_reps": [], "flagged": 0,
        })
        g["sessions"] += 1
        dist = _clean_distance(r)
        g["km"] += dist / 1000
        if r["quality_flags"]:
            g["flagged"] += 1
            continue
        if r["avg_speed"]:
            g["_t"] += (r["distance_m"] or 0) / r["avg_speed"]
            g["_d"] += r["distance_m"] or 0
        if r["avg_swolf"]:
            g["_swolf"].append(r["avg_swolf"])
        if r["avg_strokes"]:
            g["_strokes"].append(r["avg_strokes"])
    for lap in _swim_laps(db, since):
        if lap["quality_flags"] or not _plausible_free_lap(lap, lap["pool_length_m"]):
            continue
        if 100 <= lap["distance_m"] <= 400:
            key = (lap["local_date"][:7], f"{(lap['pool_length_m'] or 0):g} m pool")
            if key in groups:
                groups[key]["_reps"].append(lap["duration_s"] / lap["distance_m"] * 100)
    out = []
    for key in sorted(groups):
        g = groups[key]
        avg = g["_t"] / g["_d"] * 100 if g["_d"] else None
        rep = statistics.median(g["_reps"]) if g["_reps"] else None
        out.append({
            "month": g["month"], "venue": g["venue"], "sessions": g["sessions"], "km": round(g["km"], 1),
            "avg_pace_100m": pace_per_100m(avg), "avg_pace_s": round(avg, 1) if avg else None,
            "median_rep_pace_100m": pace_per_100m(rep), "median_rep_pace_s": round(rep, 1) if rep else None,
            "reps_counted": len(g["_reps"]),
            "avg_swolf": round(statistics.mean(g["_swolf"]), 1) if g["_swolf"] else None,
            "avg_strokes_per_length": round(statistics.mean(g["_strokes"]), 1) if g["_strokes"] else None,
            "flagged_sessions_excluded": g["flagged"],
        })
    return out


def swim_best_efforts(db: Database, since: str | None = None) -> dict[str, Any]:
    """Fastest continuous freestyle intervals by distance + a rough CSS estimate."""
    since = since or (date.today() - timedelta(days=180)).isoformat()
    buckets = {"100": (100, 150), "200": (200, 300), "400": (400, 600), "800+": (800, 10_000)}
    best: dict[str, list[dict[str, Any]]] = {k: [] for k in buckets}
    for lap in _swim_laps(db, since):
        if lap["quality_flags"] or not _plausible_free_lap(lap, lap["pool_length_m"]):
            continue
        for name, (lo, hi) in buckets.items():
            if lo <= lap["distance_m"] < hi:
                best[name].append({
                    "date": lap["local_date"], "pool_m": lap["pool_length_m"], "distance_m": lap["distance_m"],
                    "time": fmt_duration(lap["duration_s"]), "pace_s": lap["duration_s"] / lap["distance_m"] * 100,
                    "avg_hr": lap["avg_hr"],
                })
    result: dict[str, Any] = {"since": since}
    for name, items in best.items():
        items.sort(key=lambda x: x["pace_s"])
        result[name] = [{**i, "pace_100m": pace_per_100m(i["pace_s"]), "pace_s": round(i["pace_s"], 1)} for i in items[:3]]
    css = db.get_setting("css_pace_s")
    if css:
        result["css"] = {"pace_100m": pace_per_100m(css), "source": db.get_setting("css_source", "saved")}
    elif result["200"] and result["400"]:
        t200 = result["200"][0]["pace_s"] * 2
        t400 = result["400"][0]["pace_s"] * 4
        est = (t400 - t200) / 2
        if 60 < est < 240:
            result["css"] = {"pace_100m": pace_per_100m(est), "source": "estimate from best 200/400 training efforts (not a test)"}
    return result


# --------------------------------------------------------------------- other
def race_results(db: Database) -> list[dict[str, Any]]:
    parents = db.query(
        "SELECT activity_id, local_date, name, distance_m, duration_s FROM activities WHERE is_parent = 1 ORDER BY start_local DESC"
    )
    out = []
    for p in parents:
        legs = db.query(
            """SELECT sport, type_key, distance_m, duration_s, avg_speed, avg_hr, avg_power
               FROM activities WHERE parent_id = :pid ORDER BY start_local, activity_id""",
            {"pid": p["activity_id"]},
        )
        leg_out = []
        for leg in legs:
            item = {"leg": leg["sport"], "distance_m": round(leg["distance_m"] or 0), "time": fmt_duration(leg["duration_s"]),
                    "avg_hr": _int(leg["avg_hr"])}
            if leg["sport"] == "swim" and leg["avg_speed"]:
                item["pace"] = pace_per_100m(100 / leg["avg_speed"]) + "/100m"
            elif leg["sport"] == "bike" and leg["avg_speed"]:
                item["speed"] = f"{leg['avg_speed'] * 3.6:.1f} km/h"
                if leg["avg_power"]:
                    item["avg_power"] = leg["avg_power"]
            elif leg["sport"] == "run" and leg["avg_speed"]:
                item["pace"] = pace_per_km(leg["avg_speed"]) + "/km"
            leg_out.append(item)
        out.append({"activity_id": p["activity_id"], "date": p["local_date"], "name": p["name"],
                    "distance_km": round((p["distance_m"] or 0) / 1000, 1), "time": fmt_duration(p["duration_s"]),
                    "legs": leg_out})
    return out


def activity_summary(r: dict[str, Any]) -> dict[str, Any]:
    out = {
        "activity_id": r["activity_id"], "start": r["start_local"], "sport": r["sport"], "type": r["type_key"],
        "name": r["name"], "distance_m": round(r["distance_m"] or 0), "duration": fmt_duration(r["duration_s"]),
        "avg_hr": _int(r["avg_hr"]), "training_load": round(r["training_load"]) if r["training_load"] else None,
    }
    if r["sport"] == "swim" and r["avg_speed"]:
        out["pace"] = pace_per_100m(100 / r["avg_speed"]) + "/100m"
        if r.get("pool_length_m"):
            out["pool_m"] = r["pool_length_m"]
    elif r["sport"] == "run" and r["avg_speed"]:
        out["pace"] = pace_per_km(r["avg_speed"]) + "/km"
    elif r["sport"] == "bike" and r["avg_speed"]:
        out["speed"] = f"{r['avg_speed'] * 3.6:.1f} km/h"
    if r.get("quality_flags"):
        out["quality_flags"] = r["quality_flags"]
    return out


def recent_activities(db: Database, days: int = 14, sport: str | None = None) -> list[dict[str, Any]]:
    since = (date.today() - timedelta(days=days)).isoformat()
    rows = db.query(
        """SELECT * FROM activities WHERE local_date >= :since AND sport <> 'transition'
           AND (parent_id IS NULL) ORDER BY start_local DESC""",
        {"since": since},
    )
    if sport:
        rows = [r for r in rows if r["sport"] == sport]
    return [activity_summary(r) for r in rows]


def activity_detail(db: Database, activity_id: int) -> dict[str, Any] | None:
    r = db.query_one("SELECT * FROM activities WHERE activity_id = :id", {"id": activity_id})
    if not r:
        return None
    laps = db.query("SELECT * FROM laps WHERE activity_id = :id ORDER BY lap_index", {"id": activity_id})
    out = activity_summary(r)
    out["laps"] = []
    for lap in laps:
        if lap["is_rest"]:
            out["laps"].append({"rest": fmt_duration(lap["duration_s"])})
            continue
        item = {"distance_m": round(lap["distance_m"] or 0), "time": fmt_duration(lap["duration_s"]), "avg_hr": _int(lap["avg_hr"])}
        if r["sport"] == "swim":
            item["stroke"] = lap["stroke"]
            if lap["distance_m"]:
                item["pace"] = pace_per_100m(lap["duration_s"] / lap["distance_m"] * 100)
            if lap["avg_strokes"]:
                item["strokes_per_length"] = round(lap["avg_strokes"], 1)
        elif lap["avg_speed"]:
            item["pace" if r["sport"] == "run" else "speed"] = (
                pace_per_km(lap["avg_speed"]) if r["sport"] == "run" else f"{lap['avg_speed'] * 3.6:.1f} km/h")
            if lap["avg_power"]:
                item["avg_power"] = lap["avg_power"]
        out["laps"].append(item)
    raw = json.loads(r["raw"] or "{}")
    out["hr_zones_min"] = [round((r.get(f"hr_z{i}_s") or 0) / 60, 1) for i in range(1, 6)]
    out["aerobic_te"], out["anaerobic_te"] = r["aerobic_te"], r["anaerobic_te"]
    out["description"] = raw.get("description")
    return out


def wellness_trend(db: Database, days: int = 28) -> list[dict[str, Any]]:
    since = (date.today() - timedelta(days=days)).isoformat()
    return db.query(
        """SELECT day, resting_hr, hrv_last_night, hrv_weekly_avg, hrv_status, sleep_s, sleep_score
           FROM wellness WHERE day >= :since ORDER BY day""",
        {"since": since},
    )


def overview(db: Database) -> dict[str, Any]:
    counts = db.query(
        "SELECT sport, COUNT(*) AS n, MIN(local_date) AS first, MAX(local_date) AS last FROM activities WHERE is_parent = 0 GROUP BY sport"
    )
    last_sync = db.query_one("SELECT * FROM sync_runs ORDER BY started_at DESC LIMIT 1")
    flagged = db.query(
        "SELECT activity_id, local_date, name, quality_flags FROM activities WHERE quality_flags IS NOT NULL ORDER BY local_date DESC"
    )
    return {"by_sport": counts, "last_sync": last_sync, "data_quality_issues": flagged}


def zone_distribution(db: Database, weeks: int = 8) -> dict[str, Any]:
    since = (date.today() - timedelta(weeks=weeks)).isoformat()
    tot: dict[str, list[float]] = defaultdict(lambda: [0.0] * 5)
    for r in db.query(
        "SELECT sport, hr_z1_s, hr_z2_s, hr_z3_s, hr_z4_s, hr_z5_s FROM activities WHERE is_parent = 0 AND local_date >= :s",
        {"s": since},
    ):
        for i in range(5):
            tot[r["sport"]][i] += r[f"hr_z{i + 1}_s"] or 0
    out = {}
    for sport, z in tot.items():
        total = sum(z)
        if total:
            out[sport] = {f"z{i + 1}_pct": round(z[i] / total * 100) for i in range(5)}
    return {"weeks": weeks, "by_sport": out}
