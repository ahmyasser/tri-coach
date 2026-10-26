"""Structured workouts: a compact spec (what Claude and you write) -> Garmin Connect workout JSON.

Example spec::

    {
      "name": "CSS 6x200",
      "sport": "swim",
      "pool_length_m": 25,
      "description": "Main set at threshold",
      "steps": [
        {"kind": "warmup", "distance_m": 400, "stroke": "free", "note": "easy, build"},
        {"repeat": 4, "steps": [
            {"kind": "interval", "distance_m": 50, "stroke": "drill", "note": "catch-up"},
            {"kind": "rest", "seconds": 15}]},
        {"repeat": 6, "skip_last_rest": true, "steps": [
            {"kind": "interval", "distance_m": 200, "stroke": "free", "css_offset_s": 2},
            {"kind": "rest", "seconds": 20}]},
        {"kind": "cooldown", "distance_m": 200, "stroke": "any"}
      ]
    }

IDs below come from Garmin's /workout-service/workout/types (checked against a live account, Oct 2026).
"""

from __future__ import annotations

from typing import Any, Literal, Union

from pydantic import BaseModel, Field, model_validator

from .analytics import parse_pace

SPORTS = {
    "run": {"sportTypeId": 1, "sportTypeKey": "running", "displayOrder": 1},
    "bike": {"sportTypeId": 2, "sportTypeKey": "cycling", "displayOrder": 2},
    "swim": {"sportTypeId": 4, "sportTypeKey": "swimming", "displayOrder": 3},
    "strength": {"sportTypeId": 5, "sportTypeKey": "strength_training", "displayOrder": 5},
    "other": {"sportTypeId": 3, "sportTypeKey": "other", "displayOrder": 9},
}
STEP_TYPES = {"warmup": 1, "cooldown": 2, "interval": 3, "recovery": 4, "rest": 5, "repeat": 6, "other": 7, "main": 8}
CONDITIONS = {"lap.button": 1, "time": 2, "distance": 3, "iterations": 7, "fixed.rest": 8}
TARGETS = {"no.target": 1, "power.zone": 2, "cadence": 3, "heart.rate.zone": 4, "speed.zone": 5, "pace.zone": 6,
           "swim.css.offset": 17}
STROKES = {"any": (1, "any_stroke"), "back": (2, "backstroke"), "breast": (3, "breaststroke"), "drill": (4, "drill"),
           "fly": (5, "fly"), "free": (6, "free"), "im": (7, "individual_medley"), "mixed": (8, "mixed")}
EQUIPMENT = {"fins": 1, "kickboard": 2, "paddles": 3, "pull_buoy": 4, "snorkel": 5}

StepKind = Literal["warmup", "interval", "recovery", "rest", "cooldown", "other"]


def _range(text: str, parse=float) -> tuple[float, float]:
    parts = [p.strip() for p in str(text).replace("–", "-").split("-") if p.strip()]
    lo = parse(parts[0])
    hi = parse(parts[1]) if len(parts) > 1 else lo
    return (min(lo, hi), max(lo, hi))


class Step(BaseModel):
    kind: StepKind = "interval"
    distance_m: float | None = Field(None, gt=0)
    seconds: float | None = Field(None, gt=0)
    lap_button: bool = False
    stroke: Literal["any", "back", "breast", "drill", "fly", "free", "im", "mixed"] | None = None
    equipment: Literal["fins", "kickboard", "paddles", "pull_buoy", "snorkel"] | None = None
    css_offset_s: float | None = Field(None, ge=-30, le=60, description="swim: seconds/100m slower (+) or faster (-) than CSS")
    pace: str | None = Field(None, description="'1:40-1:44' per 100 m (swim) or per km (run)")
    hr_zone: int | None = Field(None, ge=1, le=5)
    hr_bpm: str | None = Field(None, description="'135-145'")
    power_zone: int | None = Field(None, ge=1, le=7)
    power_w: str | None = Field(None, description="'200-220'")
    cadence: str | None = Field(None, description="'85-95' rpm or spm")
    note: str | None = Field(None, max_length=500)

    @model_validator(mode="after")
    def _one_end(self) -> "Step":
        ends = sum(x is not None and x is not False for x in (self.distance_m, self.seconds, self.lap_button or None))
        if ends == 0:
            self.lap_button = True  # open-ended: press lap to move on
        elif ends > 1:
            raise ValueError("a step ends on exactly one of distance_m, seconds or lap_button")
        targets = [t for t in (self.pace, self.hr_zone, self.hr_bpm, self.power_zone, self.power_w) if t is not None]
        if len(targets) > 1:
            raise ValueError("use at most one primary target (pace, hr_zone, hr_bpm, power_zone, power_w)")
        return self


class Repeat(BaseModel):
    repeat: int = Field(ge=1, le=99)
    steps: list[Union["Repeat", Step]]
    skip_last_rest: bool = False


class WorkoutSpec(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    sport: Literal["swim", "bike", "run", "strength", "other"]
    description: str | None = Field(None, max_length=1000)
    pool_length_m: float | None = Field(None, gt=10, le=100)
    steps: list[Union[Repeat, Step]] = Field(min_length=1)

    @model_validator(mode="after")
    def _swim_defaults(self) -> "WorkoutSpec":
        if self.sport == "swim" and not self.pool_length_m:
            self.pool_length_m = 25.0
        if self.sport != "swim":
            for st in _iter_steps(self.steps):
                if st.stroke or st.equipment or st.css_offset_s is not None:
                    raise ValueError("stroke/equipment/css_offset_s only apply to swim workouts")
        return self


Repeat.model_rebuild()


def _iter_steps(steps: list[Any]):
    for s in steps:
        if isinstance(s, Repeat):
            yield from _iter_steps(s.steps)
        else:
            yield s


# ------------------------------------------------------------------- builder
class _Builder:
    def __init__(self, spec: WorkoutSpec, css_pace_s: float | None):
        self.spec = spec
        self.css = css_pace_s
        self.order = 0
        self.child = 0
        self.est_seconds = 0.0

    def _cond(self, key: str) -> dict[str, Any]:
        return {"conditionTypeId": CONDITIONS[key], "conditionTypeKey": key, "displayOrder": CONDITIONS[key],
                "displayable": key != "iterations"}

    def _target(self, key: str, order: int = 1) -> dict[str, Any]:
        return {"workoutTargetTypeId": TARGETS[key], "workoutTargetTypeKey": key, "displayOrder": order}

    def _guess_speed(self, st: Step) -> float:
        """m/s used only for the duration estimate."""
        if st.pace:
            lo, hi = _range(st.pace, parse_pace)
            per = 100 if self.spec.sport == "swim" else 1000
            return per / ((lo + hi) / 2)
        if self.spec.sport == "swim":
            base = self.css or 110.0
            return 100 / (base + (st.css_offset_s or 8))
        return {"run": 2.9, "bike": 8.0}.get(self.spec.sport, 2.0)

    def step(self, st: Step, child_id: int | None) -> dict[str, Any]:
        self.order += 1
        kind = st.kind
        swim = self.spec.sport == "swim"
        out: dict[str, Any] = {
            "type": "ExecutableStepDTO",
            "stepOrder": self.order,
            "stepType": {"stepTypeId": STEP_TYPES[kind], "stepTypeKey": kind, "displayOrder": STEP_TYPES[kind]},
            "childStepId": child_id,
            "description": st.note,
            "targetType": self._target("no.target"),
        }
        if st.distance_m:
            out["endCondition"], out["endConditionValue"] = self._cond("distance"), float(st.distance_m)
            self.est_seconds += st.distance_m / self._guess_speed(st)
        elif st.seconds:
            # In swim workouts a timed rest is Garmin's "fixed.rest" condition.
            key = "fixed.rest" if swim and kind == "rest" else "time"
            out["endCondition"], out["endConditionValue"] = self._cond(key), float(st.seconds)
            self.est_seconds += st.seconds
        else:
            out["endCondition"], out["endConditionValue"] = self._cond("lap.button"), None
            self.est_seconds += 30

        # primary target
        if st.pace:
            lo, hi = _range(st.pace, parse_pace)  # seconds per 100 m / per km, lo = faster
            per = 100.0 if swim else 1000.0
            out["targetType"] = self._target("pace.zone")
            out["targetValueOne"], out["targetValueTwo"] = round(per / hi, 4), round(per / lo, 4)  # slow, fast (m/s)
        elif st.hr_zone:
            out["targetType"], out["zoneNumber"] = self._target("heart.rate.zone"), st.hr_zone
        elif st.hr_bpm:
            lo, hi = _range(st.hr_bpm)
            out["targetType"] = self._target("heart.rate.zone")
            out["targetValueOne"], out["targetValueTwo"] = lo, hi
        elif st.power_zone:
            out["targetType"], out["zoneNumber"] = self._target("power.zone"), st.power_zone
        elif st.power_w:
            lo, hi = _range(st.power_w)
            out["targetType"] = self._target("power.zone")
            out["targetValueOne"], out["targetValueTwo"] = lo, hi
        if st.cadence:
            lo, hi = _range(st.cadence)
            out["secondaryTargetType"] = self._target("cadence", 2)
            out["secondaryTargetValueOne"], out["secondaryTargetValueTwo"] = lo, hi

        if swim:
            stroke = st.stroke or ("free" if kind == "interval" else "any")
            sid, skey = STROKES[stroke]
            if kind != "rest":
                out["strokeType"] = {"strokeTypeId": sid, "strokeTypeKey": skey, "displayOrder": sid}
            if st.equipment:
                eid = EQUIPMENT[st.equipment]
                out["equipmentType"] = {"equipmentTypeId": eid, "equipmentTypeKey": st.equipment, "displayOrder": eid}
            if st.css_offset_s is not None:
                out["secondaryTargetType"] = self._target("swim.css.offset", 2)
                out["secondaryTargetValueOne"], out["secondaryTargetValueTwo"] = float(st.css_offset_s), 0.0
                if self.css:
                    hint = f"target {_fmt(self.css + st.css_offset_s)}/100m (CSS {st.css_offset_s:+g}s)"
                    out["description"] = f"{st.note} · {hint}" if st.note else hint
        return {k: v for k, v in out.items() if v is not None or k == "endConditionValue"}

    def repeat(self, rp: Repeat) -> dict[str, Any]:
        self.order += 1
        self.child += 1
        cid = self.child
        group = {
            "type": "RepeatGroupDTO",
            "stepOrder": self.order,
            "stepType": {"stepTypeId": 6, "stepTypeKey": "repeat", "displayOrder": 6},
            "childStepId": cid,
            "numberOfIterations": rp.repeat,
            "endCondition": self._cond("iterations"),
            "endConditionValue": float(rp.repeat),
            "smartRepeat": False,
            "skipLastRestStep": rp.skip_last_rest,
        }
        before = self.est_seconds
        group["workoutSteps"] = [self.repeat(s) if isinstance(s, Repeat) else self.step(s, cid) for s in rp.steps]
        one = self.est_seconds - before
        self.est_seconds = before + one * rp.repeat
        return group

    def build(self) -> dict[str, Any]:
        sport = SPORTS[self.spec.sport]
        steps = [self.repeat(s) if isinstance(s, Repeat) else self.step(s, None) for s in self.spec.steps]
        workout: dict[str, Any] = {
            "workoutName": self.spec.name,
            "description": self.spec.description,
            "sportType": sport,
            "estimatedDurationInSecs": int(self.est_seconds),
            "workoutSegments": [{"segmentOrder": 1, "sportType": sport, "workoutSteps": steps}],
        }
        if self.spec.sport == "swim":
            workout["poolLength"] = float(self.spec.pool_length_m)
            workout["poolLengthUnit"] = {"unitId": 1, "unitKey": "meter", "factor": 100.0}
        return {k: v for k, v in workout.items() if v is not None}


def _fmt(seconds: float) -> str:
    s = int(round(seconds))
    return f"{s // 60}:{s % 60:02d}"


def to_garmin(spec: WorkoutSpec, css_pace_s: float | None = None) -> dict[str, Any]:
    return _Builder(spec, css_pace_s).build()


def total_distance(spec: WorkoutSpec) -> float:
    def walk(steps: list[Any]) -> float:
        tot = 0.0
        for s in steps:
            tot += walk(s.steps) * s.repeat if isinstance(s, Repeat) else (s.distance_m or 0)
        return tot

    return walk(spec.steps)


def describe(spec: WorkoutSpec) -> str:
    """Human-readable version of a spec (shown before pushing)."""

    def line(st: Step) -> str:
        what = f"{st.distance_m:g} m" if st.distance_m else (f"{_fmt(st.seconds)}" if st.seconds else "until lap")
        bits = [st.kind, what]
        if st.stroke:
            bits.append(st.stroke)
        if st.equipment:
            bits.append(f"w/ {st.equipment}")
        if st.css_offset_s is not None:
            bits.append(f"@ CSS {st.css_offset_s:+g}s")
        for label, val in (("pace", st.pace), ("HR z", st.hr_zone), ("HR", st.hr_bpm), ("power z", st.power_zone),
                           ("power", st.power_w), ("cadence", st.cadence)):
            if val is not None:
                bits.append(f"{label} {val}")
        if st.note:
            bits.append(f"({st.note})")
        return " ".join(str(b) for b in bits)

    def walk(steps: list[Any], indent: str) -> list[str]:
        out = []
        for s in steps:
            if isinstance(s, Repeat):
                out.append(f"{indent}{s.repeat}x:")
                out.extend(walk(s.steps, indent + "  "))
            else:
                out.append(indent + line(s))
        return out

    head = f"{spec.name} [{spec.sport}" + (f", {spec.pool_length_m:g} m pool" if spec.sport == "swim" else "") + "]"
    dist = total_distance(spec)
    if dist:
        head += f" — {dist:g} m total"
    return "\n".join([head, *walk(spec.steps, "  ")])
