"""A stand-in for Garmin Connect, shaped like real API responses (field names checked against a live account)."""

from __future__ import annotations

from typing import Any


def _swim(aid: int, start: str, pool_cm: int, distance: float, duration: float, speed: float, strokes: float,
          lengths: int, swolf: int, hr: int = 135, type_key: str = "lap_swimming") -> dict[str, Any]:
    a = {
        "activityId": aid, "activityName": "Pool Swim" if type_key == "lap_swimming" else "Open Water Swimming",
        "startTimeLocal": start, "startTimeGMT": start, "activityType": {"typeId": 27, "typeKey": type_key},
        "distance": distance, "duration": duration, "movingDuration": duration * 0.8, "averageSpeed": speed,
        "averageHR": hr, "maxHR": hr + 25, "calories": 400, "activityTrainingLoad": 60.0,
        "aerobicTrainingEffect": 2.6, "anaerobicTrainingEffect": 1.0, "hrTimeInZone_1": 300, "hrTimeInZone_2": 900,
        "hrTimeInZone_3": 600, "hrTimeInZone_4": 100, "hrTimeInZone_5": 0, "isParent": False,
    }
    if type_key == "lap_swimming":
        a.update({"poolLength": pool_cm, "unitOfPoolLength": {"unitId": 1, "unitKey": "meter", "factor": 100.0},
                  "averageSwolf": swolf, "avgStrokes": strokes, "activeLengths": lengths})
    return a


def _run(aid: int, start: str, distance: float, duration: float, hr: int = 145) -> dict[str, Any]:
    return {"activityId": aid, "activityName": "El Maadi Running", "startTimeLocal": start, "startTimeGMT": start,
            "activityType": {"typeKey": "running"}, "distance": distance, "duration": duration,
            "movingDuration": duration, "averageSpeed": distance / duration, "averageHR": hr, "maxHR": hr + 20,
            "activityTrainingLoad": 80.0, "elevationGain": 5, "averageRunningCadenceInStepsPerMinute": 168,
            "isParent": False}


ACTIVITIES = [  # newest first, like Garmin
    _swim(1009, "2026-10-25 07:23:57", 5000, 2350, 3459, 0.90, 22.5, 47, 78),
    _swim(1008, "2026-10-22 07:24:41", 5000, 1150, 1717, 0.90, 22.7, 23, 78),
    _run(1007, "2026-10-21 07:08:35", 4645, 1773),
    {"activityId": 1006, "activityName": "Sharm El Sheikh Multisport", "startTimeLocal": "2026-10-16 06:46:52",
     "startTimeGMT": "2026-10-16 03:46:52", "activityType": {"typeKey": "multi_sport"}, "distance": 114009.8,
     "duration": 20696.9, "averageSpeed": 5.51, "isParent": True, "parent": True},
    _swim(1005, "2026-10-14 16:54:18", 0, 498.7, 777, 0.64, 0, 0, 0, hr=118, type_key="open_water_swimming"),
    _swim(1004, "2026-08-20 07:00:00", 5000, 5000, 4173, 1.6, 12.2, 100, 43),  # wrong pool length (really 25 m)
    _swim(1003, "2026-07-10 07:00:00", 2500, 3000, 4266, 0.96, 10.5, 120, 37),
    _swim(1002, "2026-01-12 07:00:00", 2500, 1500, 2400, 0.85, 11.1, 60, 40),
]

CHILDREN = {
    1006: [
        (2001, "open_water_swimming", 1828, 2353, 0.777, 121, None),
        (2002, "transition_v2", 520, 304, 1.709, 133, None),
        (2003, "cycling", 91075, 10740, 8.48, 150, None),
        (2004, "transition_v2", 289, 170, 1.699, 143, None),
        (2005, "running", 20297, 7130, 2.847, 159, None),
    ]
}


def _lap(dist: float, dur: float, stroke: str | None, strokes: float | None, hr: int = 140) -> dict[str, Any]:
    return {"distance": dist, "duration": dur, "movingDuration": dur, "averageSpeed": dist / dur if dist else 0,
            "averageHR": hr, "maxHR": hr + 10, "swimStroke": stroke, "averageStrokes": strokes,
            "averageSWOLF": 40, "numberOfActiveLengths": int(dist / 25) if dist else 0, "lengthDTOs": [{"x": 1}]}


SPLITS = {
    1009: [_lap(200, 226, "FREESTYLE", 22), _lap(0, 30, None, None), _lap(400, 402, "FREESTYLE", 21),
           _lap(0, 60, None, None), _lap(200, 195, "FREESTYLE", 22), _lap(100, 37, "FREESTYLE", 11)],  # last = glitch
    1008: [_lap(100, 104, "FREESTYLE", 22), _lap(0, 20, None, None), _lap(100, 103, "FREESTYLE", 22)],
    1003: [_lap(200, 212, "FREESTYLE", 10.5), _lap(0, 20, None, None), _lap(200, 210, "FREESTYLE", 10.4),
           _lap(100, 120, "DRILL", 9)],
    1002: [_lap(200, 240, "FREESTYLE", 11.1), _lap(0, 30, None, None), _lap(200, 236, "FREESTYLE", 11.0)],
    1004: [_lap(100, 62, "FREESTYLE", 12)],
    1007: [{"distance": 1000, "duration": 380, "averageSpeed": 2.63, "averageHR": 140}],
    2001: [_lap(1828, 2353, "FREESTYLE", None)],
    2003: [{"distance": 91075, "duration": 10740, "averageSpeed": 8.48, "averageHR": 150}],
    2005: [{"distance": 20297, "duration": 7130, "averageSpeed": 2.847, "averageHR": 159}],
}


class FakeGarmin:
    def __init__(self, activities: list[dict[str, Any]] | None = None):
        self.activities = activities if activities is not None else list(ACTIVITIES)
        self.calls: list[tuple] = []
        self.uploaded: list[dict[str, Any]] = []
        self.scheduled: list[tuple] = []
        self.deleted: list[int] = []
        self.unscheduled: list[int] = []

    def get_activities(self, start: int, limit: int) -> list[dict[str, Any]]:
        self.calls.append(("get_activities", start, limit))
        return self.activities[start:start + limit]

    def get_activity(self, activity_id: int) -> dict[str, Any]:
        self.calls.append(("get_activity", activity_id))
        if activity_id in CHILDREN:
            return {"activityId": activity_id, "metadataDTO": {"childIds": [c[0] for c in CHILDREN[activity_id]]}}
        for pid, kids in CHILDREN.items():
            for cid, tk, dist, dur, spd, hr, pw in kids:
                if cid == activity_id:
                    return {"activityId": cid, "activityName": tk, "activityTypeDTO": {"typeKey": tk},
                            "summaryDTO": {"startTimeLocal": f"2026-10-16T07:{cid % 100:02d}:00.0",
                                           "distance": dist, "duration": dur, "averageSpeed": spd, "averageHR": hr,
                                           "averagePower": pw}}
        raise KeyError(activity_id)

    def get_activity_splits(self, activity_id: int) -> dict[str, Any]:
        self.calls.append(("get_activity_splits", activity_id))
        return {"activityId": activity_id, "lapDTOs": [dict(l, lapIndex=i + 1) for i, l in enumerate(SPLITS.get(activity_id, []))]}

    def get_sleep_daily(self, start: str, end: str) -> list[dict[str, Any]]:
        rows = [{"calendarDate": "2026-10-01", "values": {"totalSleepTimeInSeconds": 24600, "sleepScore": 77,
                                                            "restingHeartRate": 47, "avgOvernightHrv": 74,
                                                            "bodyBatteryChange": 62}}]
        return [r for r in rows if start <= r["calendarDate"] <= end]

    def get_hrv_data_range(self, start: str, end: str) -> dict[str, Any]:
        rows = [{"calendarDate": "2026-10-17", "weeklyAvg": None, "lastNightAvg": 68, "status": "NONE"},
                {"calendarDate": "2026-10-18", "weeklyAvg": 70, "lastNightAvg": 70, "status": "BALANCED"}]
        return {"hrvSummaries": [r for r in rows if start <= r["calendarDate"] <= end]}

    def get_rhr_daily(self, start: str, end: str) -> list[dict[str, Any]]:
        rows = [{"calendarDate": "2026-10-17", "value": 48}, {"calendarDate": "2026-10-18", "value": 47}]
        return [r for r in rows if start <= r["calendarDate"] <= end]

    def upload_workout(self, workout_json: dict[str, Any]) -> dict[str, Any]:
        self.uploaded.append(workout_json)
        return {"workoutId": 900 + len(self.uploaded), **workout_json}

    def schedule_workout(self, workout_id: int, date_str: str) -> dict[str, Any]:
        self.scheduled.append((workout_id, date_str))
        return {"workoutScheduleId": 7000 + len(self.scheduled), "calendarDate": date_str}

    def unschedule_workout(self, scheduled_workout_id: int) -> None:
        self.unscheduled.append(scheduled_workout_id)

    def delete_workout(self, workout_id: int) -> None:
        self.deleted.append(workout_id)

    def get_scheduled_workouts_by_period(self, start: str, end: str) -> list[Any]:
        return []
