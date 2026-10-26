"""Turn raw Garmin Connect JSON into flat rows, and flag data-quality problems."""

from __future__ import annotations

import json
from typing import Any

SPORT_BY_TYPE = {
    "lap_swimming": "swim",
    "open_water_swimming": "swim",
    "swimming": "swim",
    "running": "run",
    "track_running": "run",
    "treadmill_running": "run",
    "trail_running": "run",
    "virtual_run": "run",
    "cycling": "bike",
    "indoor_cycling": "bike",
    "road_biking": "bike",
    "gravel_cycling": "bike",
    "mountain_biking": "bike",
    "virtual_ride": "bike",
    "multi_sport": "multisport",
    "triathlon": "multisport",
    "transition_v2": "transition",
    "transition": "transition",
    "strength_training": "strength",
    "hiit": "strength",
    "indoor_rowing": "other",
}

ACTIVITY_COLUMNS = [
    "activity_id", "start_local", "start_gmt", "local_date", "sport", "type_key", "name",
    "distance_m", "duration_s", "moving_s", "avg_speed", "avg_hr", "max_hr", "calories",
    "elevation_gain", "avg_power", "norm_power", "avg_cadence", "training_load",
    "aerobic_te", "anaerobic_te", "pool_length_m", "avg_swolf", "avg_strokes", "active_lengths",
    "hr_z1_s", "hr_z2_s", "hr_z3_s", "hr_z4_s", "hr_z5_s", "is_parent", "parent_id",
    "quality_flags", "raw", "updated_at",
]

LAP_COLUMNS = [
    "activity_id", "lap_index", "distance_m", "duration_s", "moving_s", "avg_speed", "avg_hr",
    "max_hr", "avg_power", "stroke", "avg_strokes", "swolf", "lengths", "is_rest", "raw",
]


def sport_for(type_key: str | None) -> str:
    if not type_key:
        return "other"
    if type_key in SPORT_BY_TYPE:
        return SPORT_BY_TYPE[type_key]
    if "swim" in type_key:
        return "swim"
    if "run" in type_key:
        return "run"
    if "cycl" in type_key or "bik" in type_key or "ride" in type_key:
        return "bike"
    return "other"


def pool_length_m(a: dict[str, Any]) -> float | None:
    """Garmin reports poolLength in centimetres when the unit is metres (5000 = 50 m)."""
    length = a.get("poolLength")
    if not length:
        return None
    unit = (a.get("unitOfPoolLength") or {}).get("unitKey", "meter")
    value = length / 100.0 if length > 100 else float(length)  # stored in hundredths of the unit
    return round(value * 0.9144, 2) if unit == "yard" else round(value, 2)


def quality_flags(a: dict[str, Any], pool_m: float | None) -> list[str]:
    """Spot recordings that would poison the analysis.

    Wrist-based pool swims count lengths from turns, so a wrong pool-length setting
    doubles or halves distance. Stroke counts per length expose it: ~10-14 per 25 m
    (one arm, as Garmin counts) vs ~19-28 per 50 m.
    """
    flags: list[str] = []
    strokes = a.get("avgStrokes")
    if a.get("activityType", {}).get("typeKey") == "lap_swimming" and pool_m and strokes:
        per_25 = strokes * 25.0 / pool_m
        if per_25 < 7.5:
            flags.append(f"pool_length_suspect:likely_{int(pool_m / 2)}m")
        elif per_25 > 18:
            flags.append(f"pool_length_suspect:likely_{int(pool_m * 2)}m")
    speed = a.get("averageSpeed") or 0
    if a.get("activityType", {}).get("typeKey") in ("lap_swimming", "open_water_swimming") and speed > 1.6:
        flags.append("swim_speed_implausible")
    return flags


def activity_row(a: dict[str, Any], now: str, parent_id: int | None = None) -> dict[str, Any]:
    type_key = (a.get("activityType") or {}).get("typeKey")
    start_local = a.get("startTimeLocal") or ""
    pool_m = pool_length_m(a)
    return {
        "activity_id": int(a["activityId"]),
        "start_local": start_local,
        "start_gmt": a.get("startTimeGMT"),
        "local_date": start_local[:10],
        "sport": sport_for(type_key),
        "type_key": type_key,
        "name": a.get("activityName"),
        "distance_m": a.get("distance"),
        "duration_s": a.get("duration"),
        "moving_s": a.get("movingDuration"),
        "avg_speed": a.get("averageSpeed"),
        "avg_hr": a.get("averageHR"),
        "max_hr": a.get("maxHR"),
        "calories": a.get("calories"),
        "elevation_gain": a.get("elevationGain"),
        "avg_power": a.get("avgPower"),
        "norm_power": a.get("normPower"),
        "avg_cadence": a.get("averageRunningCadenceInStepsPerMinute") or a.get("averageBikingCadenceInRevPerMinute"),
        "training_load": a.get("activityTrainingLoad"),
        "aerobic_te": a.get("aerobicTrainingEffect"),
        "anaerobic_te": a.get("anaerobicTrainingEffect"),
        "pool_length_m": pool_m,
        "avg_swolf": a.get("averageSwolf"),
        "avg_strokes": a.get("avgStrokes"),
        "active_lengths": a.get("activeLengths"),
        "hr_z1_s": a.get("hrTimeInZone_1"),
        "hr_z2_s": a.get("hrTimeInZone_2"),
        "hr_z3_s": a.get("hrTimeInZone_3"),
        "hr_z4_s": a.get("hrTimeInZone_4"),
        "hr_z5_s": a.get("hrTimeInZone_5"),
        "is_parent": 1 if a.get("isParent") or a.get("parent") else 0,
        "parent_id": parent_id if parent_id is not None else a.get("parentId"),
        "quality_flags": ",".join(quality_flags(a, pool_m)) or None,
        "raw": json.dumps(a, separators=(",", ":")),
        "updated_at": now,
    }


def child_summary_to_activity(detail: dict[str, Any]) -> dict[str, Any]:
    """Multisport legs come from /activity/{id} (a summaryDTO), not the list endpoint.

    Reshape them to look like a list entry so ``activity_row`` can handle them.
    """
    s = detail.get("summaryDTO") or {}
    return {
        "activityId": detail.get("activityId"),
        "activityName": detail.get("activityName"),
        "activityType": {"typeKey": (detail.get("activityTypeDTO") or {}).get("typeKey")},
        "startTimeLocal": (s.get("startTimeLocal") or "").replace("T", " ")[:19],
        "startTimeGMT": (s.get("startTimeGMT") or "").replace("T", " ")[:19],
        "distance": s.get("distance"),
        "duration": s.get("duration"),
        "movingDuration": s.get("movingDuration"),
        "averageSpeed": s.get("averageSpeed"),
        "averageHR": s.get("averageHR"),
        "maxHR": s.get("maxHR"),
        "calories": s.get("calories"),
        "elevationGain": s.get("elevationGain"),
        "avgPower": s.get("averagePower"),
        "normPower": s.get("normalizedPower"),
        "activityTrainingLoad": s.get("activityTrainingLoad"),
        "aerobicTrainingEffect": s.get("trainingEffect"),
        "anaerobicTrainingEffect": s.get("anaerobicTrainingEffect"),
    }


def lap_rows(activity_id: int, splits: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for i, lap in enumerate(splits.get("lapDTOs") or []):
        dist = lap.get("distance") or 0
        rows.append({
            "activity_id": activity_id,
            "lap_index": lap.get("lapIndex", i + 1),
            "distance_m": dist,
            "duration_s": lap.get("duration"),
            "moving_s": lap.get("movingDuration"),
            "avg_speed": lap.get("averageSpeed"),
            "avg_hr": lap.get("averageHR"),
            "max_hr": lap.get("maxHR"),
            "avg_power": lap.get("averagePower"),
            "stroke": lap.get("swimStroke"),
            "avg_strokes": lap.get("averageStrokes"),
            "swolf": lap.get("averageSWOLF"),
            "lengths": lap.get("numberOfActiveLengths"),
            "is_rest": 1 if dist == 0 else 0,
            "raw": json.dumps({k: v for k, v in lap.items() if k != "lengthDTOs"}, separators=(",", ":")),
        })
    return rows
