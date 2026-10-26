"""AI coach for the dashboard: chat with tools over the training data, a daily briefing, and rule-based notices.

Chat history is stored verbatim (append-only, including thinking blocks) in coach_messages, so every request
replays exactly what the model saw before. Notices need no API key; chat and briefings need ANTHROPIC_API_KEY.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import uuid
from datetime import date, datetime, timedelta
from statistics import mean
from typing import Any, Callable, Iterator

from . import analytics
from .mcp_server import SCHEMA_DOC
from .services import Services
from .sync import now_str
from .workouts import WorkoutSpec

log = logging.getLogger(__name__)

BETAS = ["server-side-fallback-2026-07-01"]
MAX_TURNS = 12

SYSTEM = """\
You are {who_s} endurance coach and sports nutritionist, working inside their training dashboard.
You have tools over their Garmin history (synced daily): activities with laps, swim reps, multisport race legs,
and daily sleep, HRV and resting heart rate.{focus}

How you work:
- Look at the data before answering anything about training, recovery or progress. Quote real numbers
  (dates, paces, hours, bpm) and say which period they cover.
- Read get_athlete_profile at the start of a conversation: it holds goals, CSS, races and past decisions.
  If it has no goal or main sport yet, ask about them early and save the answers.
- Pool swims with quality_flags (wrong pool length) are excluded from pace stats; mention it when it matters.
- Nutrition: periodize carbs to the day's load (rest/easy ~3-5 g/kg, moderate ~5-7, hard/long ~6-10), protein
  ~1.6-2.2 g/kg/day in 3-5 feedings, 30-90 g carbs/h in sessions over ~75 min, and recovery carbs plus 20-40 g
  protein within ~2 h. Ask for body weight if you need g/kg and don't have it.
- When you and {who} agree on something (a goal, a test result, a plan change, an injury), store it with save_note
  or set_athlete_setting so later conversations know it.
- Workouts: create_workout only makes a DRAFT. It appears on the dashboard's Workouts page, where {who} reviews it
  and presses "Send to Garmin" to put it on their watch. You can't send it yourself.
- You are not a doctor. Pain that persists or gets sharper, illness, or signs of low energy availability (ongoing
  fatigue, frequent illness, falling performance, rapid weight loss): say so plainly and suggest a professional.

Style: direct and brief, like a coach texting an athlete. Lead with the answer. Short paragraphs or bullets,
numbers over adjectives, Markdown for structure (no tables wider than 5 columns). No filler, no pep talk.
Plain words: explain any jargon (CSS, HRV, zones) in a few words the first time you use it.
"""


def system_prompt(svc: Services) -> str:
    name, focus = svc.settings.athlete_name, svc.settings.athlete_focus
    return SYSTEM.format(who=name or "the athlete", who_s=f"{name}'s" if name else "the athlete's",
                         focus=f" {focus.capitalize()} is the main focus." if focus else "")


BRIEFING_PROMPT = """\
Write today's coaching briefing from the data below. Format, in Markdown:
- First line: one sentence headline (no heading marker), the single most useful thing to know today.
- "**Last 7 days**": 2-4 bullets on what was done versus usual (hours, sessions per sport, notable sessions).
- "**Recovery**": 1-3 bullets from HRV, resting HR and sleep, with the numbers.
- "**Next 7 days**": 2-4 bullets of concrete focus (sessions, intensity balance, a test if one is due).
- "**Fuel**": 1-2 bullets of nutrition guidance tied to the coming load.
Keep it under 200 words. Use only the data given; if something is missing, say what.
"""

TOOL_LABELS = {
    "training_overview": "Checked training overview", "training_volume": "Looked at training volume",
    "list_activities": "Read recent activities", "get_activity": "Opened an activity", "swim_trend": "Checked swim trend",
    "swim_best_efforts": "Checked best swim efforts", "race_results": "Read race results", "wellness": "Read sleep and HRV",
    "intensity_distribution": "Checked intensity zones", "run_sql": "Queried the database",
    "get_athlete_profile": "Read athlete profile", "set_athlete_setting": "Saved a setting", "save_note": "Saved a note",
    "list_notes": "Read coaching notes", "list_workouts": "Read planned workouts", "create_workout": "Drafted a workout",
}

_WRITE = re.compile(r"\b(insert|update|delete|drop|alter|create|truncate|grant|revoke|copy|attach|pragma|vacuum)\b", re.I)
_chat_lock = threading.Lock()


# ------------------------------------------------------------------ tools
def _obj(props: dict[str, Any] | None = None, required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": props or {}, "required": required or [], "additionalProperties": False}


def _int(args: dict[str, Any], key: str, default: int, lo: int, hi: int) -> int:
    return max(lo, min(int(args.get(key, default)), hi))


def _workout_schema() -> dict[str, Any]:
    spec = WorkoutSpec.model_json_schema()
    defs = spec.pop("$defs", {})
    schema = _obj({"spec": spec, "scheduled_date": {"type": "string", "description": "YYYY-MM-DD"},
                   "notes": {"type": "string"}}, ["spec"])
    schema["$defs"] = defs
    return schema


def tool_specs() -> list[dict[str, Any]]:
    sport = {"type": "string", "enum": ["swim", "bike", "run", "multisport", "strength", "other"]}
    specs = [
        ("training_overview", "Data coverage per sport, last sync, data-quality issues, and acute vs chronic training load.", _obj()),
        ("training_volume", "Hours, km and Garmin training load per sport for the last `count` weeks (Mon-start) or months.",
         _obj({"period": {"type": "string", "enum": ["week", "month"]}, "count": {"type": "integer"}})),
        ("list_activities", "Activities in the last `days` days, newest first, with pace/speed and HR.",
         _obj({"days": {"type": "integer"}, "sport": sport})),
        ("get_activity", "One activity with all laps/intervals (swim stroke, pace/100m, strokes per length), HR zones, training effect.",
         _obj({"activity_id": {"type": "integer"}}, ["activity_id"])),
        ("swim_trend", "Swim progress per month and venue: sessions, km, average pace, median freestyle rep pace, SWOLF, strokes per length.",
         _obj({"months": {"type": "integer"}})),
        ("swim_best_efforts", "Fastest freestyle intervals (100/200/400/800+) since a date (default 180 days) and CSS.",
         _obj({"since": {"type": "string", "description": "YYYY-MM-DD"}})),
        ("race_results", "Multisport races with swim/T1/bike/T2/run splits.", _obj()),
        ("wellness", "Daily resting HR, overnight HRV (and status), sleep duration and score.", _obj({"days": {"type": "integer"}})),
        ("intensity_distribution", "Share of time in HR zones 1-5 per sport over the last `weeks` weeks.", _obj({"weeks": {"type": "integer"}})),
        ("run_sql", "Run a read-only SQL SELECT for anything the other tools don't cover. Max 200 rows.\n" + SCHEMA_DOC,
         _obj({"sql": {"type": "string"}}, ["sql"])),
        ("get_athlete_profile", "Saved athlete settings (CSS, goals, races, availability) and the 20 most recent coaching notes.", _obj()),
        ("set_athlete_setting", "Save an athlete setting, e.g. goal_race='Ironman 70.3 Egypt 2027-03-07', body_weight_kg='72'.",
         _obj({"key": {"type": "string"}, "value": {"type": "string"}}, ["key", "value"])),
        ("save_note", "Record a coaching decision, test result or observation (topic e.g. 'plan', 'injury', 'test', 'nutrition').",
         _obj({"body": {"type": "string"}, "topic": {"type": "string"}}, ["body"])),
        ("list_notes", "Coaching notes, newest first, optionally filtered by topic.", _obj({"topic": {"type": "string"}, "limit": {"type": "integer"}})),
        ("list_workouts", "Planned workouts (drafts and ones on the Garmin calendar) from 7 days ago onwards.", _obj()),
        ("create_workout", "Create a DRAFT structured workout on the dashboard's Workouts page (the athlete sends it to Garmin). Returns a summary. "
                           "Steps: warmup|interval|recovery|rest|cooldown|other with distance_m, seconds or lap_button; swim steps take "
                           "stroke, equipment and css_offset_s; one target per step (pace, hr_zone, hr_bpm, power_zone, power_w).",
         _workout_schema()),
    ]
    return [{"name": n, "description": d, "input_schema": s, "eager_input_streaming": True} for n, d, s in specs]


def run_tool(svc: Services, name: str, args: Any) -> Any:
    """Execute one tool call. Inputs come from the model, so they're coerced and bounded here."""
    if not isinstance(args, dict):
        raise ValueError("tool input must be an object")
    db = svc.db
    if name == "training_overview":
        return {**analytics.overview(db), "load": analytics.load_status(db)}
    if name == "training_volume":
        period = args.get("period", "week")
        return analytics.volume_by_period(db, period if period in ("week", "month") else "week", _int(args, "count", 12, 1, 104))
    if name == "list_activities":
        return analytics.recent_activities(db, _int(args, "days", 14, 1, 3650), args.get("sport") or None)
    if name == "get_activity":
        return analytics.activity_detail(db, int(args["activity_id"])) or {"error": "activity not found"}
    if name == "swim_trend":
        return analytics.swim_trend(db, _int(args, "months", 12, 1, 60))
    if name == "swim_best_efforts":
        return analytics.swim_best_efforts(db, args.get("since") or None)
    if name == "race_results":
        return analytics.race_results(db)
    if name == "wellness":
        return analytics.wellness_trend(db, _int(args, "days", 28, 1, 400))
    if name == "intensity_distribution":
        return analytics.zone_distribution(db, _int(args, "weeks", 8, 1, 52))
    if name == "run_sql":
        stmt = str(args["sql"]).strip().rstrip(";")
        if ";" in stmt or not re.match(r"^\s*(select|with)\b", stmt, re.I) or _WRITE.search(stmt):
            return {"error": "Only a single SELECT/WITH query is allowed"}
        with db.readonly():
            rows = db.query_raw(stmt, 200)
        return {"rows": rows, "row_count": len(rows)}
    if name == "get_athlete_profile":
        return profile(svc)
    if name == "set_athlete_setting":
        db.set_setting(str(args["key"]), str(args["value"]), now_str())
        return {"saved": args["key"]}
    if name == "save_note":
        nid = uuid.uuid4().hex[:12]
        db.execute("INSERT INTO coach_notes (id, created_at, topic, body) VALUES (:id, :t, :topic, :body)",
                   {"id": nid, "t": now_str(), "topic": args.get("topic"), "body": str(args["body"])})
        return {"saved": nid}
    if name == "list_notes":
        sql, params = "SELECT id, created_at, topic, body FROM coach_notes", {"lim": _int(args, "limit", 50, 1, 500)}
        if args.get("topic"):
            sql, params["topic"] = sql + " WHERE topic = :topic", args["topic"]
        return db.query(sql + " ORDER BY created_at DESC LIMIT :lim", params)
    if name == "list_workouts":
        return svc.planner.list((date.today() - timedelta(days=7)).isoformat())
    if name == "create_workout":
        return svc.planner.create(args["spec"], notes=args.get("notes"), scheduled_date=args.get("scheduled_date"))
    raise ValueError(f"unknown tool {name}")


def profile(svc: Services) -> dict[str, Any]:
    db = svc.db
    settings = {r["key"]: json.loads(r["value"]) if r["value"] else None
                for r in db.query("SELECT key, value FROM athlete_settings WHERE key <> 'coach_thread' ORDER BY key")}
    notes = db.query("SELECT created_at, topic, body FROM coach_notes ORDER BY created_at DESC LIMIT 20")
    return {"settings": settings, "recent_notes": notes}


# ---------------------------------------------------------------- client
def enabled(svc: Services) -> bool:
    return bool(svc.settings.anthropic_api_key or svc.llm_factory)


def _client(svc: Services) -> Any:
    if svc.llm_factory:
        return svc.llm_factory()
    import anthropic

    return anthropic.Anthropic(api_key=svc.settings.anthropic_api_key)


def _params(svc: Services) -> dict[str, Any]:
    return {
        "model": svc.settings.coach_model, "max_tokens": 64000,
        "system": [{"type": "text", "text": system_prompt(svc)}], "thinking": {"type": "adaptive"},
        "output_config": {"effort": "medium"}, "cache_control": {"type": "ephemeral"},
        "betas": BETAS, "fallbacks": "default",
    }


def _dump(blocks: list[Any]) -> list[dict[str, Any]]:
    return [b if isinstance(b, dict) else b.model_dump(mode="json", exclude_none=True) for b in blocks]


def _api_error(exc: Exception) -> str | None:
    import anthropic

    if isinstance(exc, anthropic.AuthenticationError):
        return "The Anthropic API key was rejected. Check ANTHROPIC_API_KEY in .env."
    if isinstance(exc, anthropic.RateLimitError):
        return "Rate limited by the Anthropic API. Try again in a minute."
    if isinstance(exc, anthropic.APIStatusError):
        return f"Anthropic API error {exc.status_code}: {exc.message}"
    if isinstance(exc, anthropic.APIConnectionError):
        return "Couldn't reach the Anthropic API."
    return None


# ---------------------------------------------------------------- threads
def current_thread(svc: Services) -> str:
    tid = svc.db.get_setting("coach_thread")
    if not tid:
        tid = new_thread(svc)
    return tid


def new_thread(svc: Services) -> str:
    tid = uuid.uuid4().hex[:12]
    svc.db.set_setting("coach_thread", tid, now_str())
    return tid


def _messages(svc: Services, tid: str) -> list[dict[str, Any]]:
    rows = svc.db.query("SELECT role, content FROM coach_messages WHERE thread_id = :t ORDER BY seq", {"t": tid})
    return [{"role": r["role"], "content": json.loads(r["content"])} for r in rows]


def _append(svc: Services, tid: str, role: str, content: list[dict[str, Any]]) -> None:
    row = svc.db.query_one("SELECT COALESCE(MAX(seq), -1) + 1 AS n FROM coach_messages WHERE thread_id = :t", {"t": tid})
    svc.db.execute("INSERT INTO coach_messages (thread_id, seq, created_at, role, content) VALUES (:t, :s, :c, :r, :b)",
                   {"t": tid, "s": row["n"], "c": now_str(), "r": role, "b": json.dumps(content)})


def transcript(svc: Services, tid: str) -> list[dict[str, Any]]:
    """Chat history for display: user texts, and one assistant bubble per reply (its text plus tool chips)."""
    out: list[dict[str, Any]] = []
    for m in _messages(svc, tid):
        blocks = m["content"]
        if m["role"] == "user":
            if any(b.get("type") == "tool_result" for b in blocks):
                continue
            out.append({"role": "user", "text": blocks[-1]["text"]})
            continue
        if not out or out[-1]["role"] != "assistant":
            out.append({"role": "assistant", "text": "", "tools": []})
        for b in blocks:
            if b.get("type") == "text":
                out[-1]["text"] += ("\n\n" if out[-1]["text"] else "") + b["text"]
            elif b.get("type") == "tool_use":
                out[-1]["tools"].append(TOOL_LABELS.get(b["name"], b["name"]))
    return out


# ------------------------------------------------------------------ chat
def _event(kind: str, value: Any = None) -> str:
    return json.dumps({"t": kind, "v": value}) + "\n"


def chat(svc: Services, text: str) -> Iterator[str]:
    """Stream one coach reply as NDJSON events: text deltas, tool chips, done or error."""
    if not enabled(svc):
        yield _event("error", "Add ANTHROPIC_API_KEY to .env on the server to turn on the coach.")
        return
    if not _chat_lock.acquire(blocking=False):
        yield _event("error", "The coach is still answering your previous message.")
        return
    try:
        yield from _chat(svc, text)
    finally:
        _chat_lock.release()


def _chat(svc: Services, text: str) -> Iterator[str]:
    client, tid, tools = _client(svc), current_thread(svc), tool_specs()
    today = date.today()
    user = [{"type": "text", "text": f"(Today is {today:%A} {today.isoformat()}.)"}, {"type": "text", "text": text}]
    _append(svc, tid, "user", user)
    messages = _messages(svc, tid)
    parse_retries = 0
    for _ in range(MAX_TURNS):
        try:
            with client.beta.messages.stream(**_params(svc), tools=tools, messages=messages) as stream:
                for ev in stream:
                    if ev.type == "text":
                        yield _event("text", ev.text)
                    elif ev.type == "content_block_start" and ev.content_block.type == "tool_use":
                        yield _event("tool", TOOL_LABELS.get(ev.content_block.name, ev.content_block.name))
                    elif ev.type == "content_block_start" and ev.content_block.type == "thinking":
                        yield _event("thinking")
                final = stream.get_final_message()
            parse_retries = 0
        except ValueError:
            # Tool input the SDK couldn't parse at all; nothing was stored for this turn, so re-issue it.
            parse_retries += 1
            if parse_retries > 2:
                yield _event("error", "The coach sent a malformed tool call. Try again.")
                return
            continue
        except Exception as exc:  # noqa: BLE001 - surfaced to the user, logged with traceback
            msg = _api_error(exc)
            log.exception("coach chat failed")
            yield _event("error", msg or "Something went wrong talking to the coach.")
            return

        if final.stop_reason == "refusal":
            yield _event("error", "The coach declined to answer that one. Try rephrasing.")
            return
        content = _dump(final.content)
        messages.append({"role": "assistant", "content": content})
        _append(svc, tid, "assistant", content)
        if final.stop_reason == "pause_turn":
            continue
        tool_uses = [b for b in content if b.get("type") == "tool_use"]
        if not tool_uses:
            break
        results = []
        for b in tool_uses:
            if final.stop_reason == "max_tokens":
                results.append({"type": "tool_result", "tool_use_id": b["id"], "is_error": True,
                                "content": "Not run: the tool input was cut off by max_tokens."})
                continue
            try:
                out = run_tool(svc, b["name"], b.get("input"))
                results.append({"type": "tool_result", "tool_use_id": b["id"], "content": json.dumps(out, default=str)})
            except Exception as exc:  # noqa: BLE001 - reported back to the model as a tool error
                results.append({"type": "tool_result", "tool_use_id": b["id"], "is_error": True, "content": f"{type(exc).__name__}: {exc}"})
        messages.append({"role": "user", "content": results})
        _append(svc, tid, "user", results)
        if final.stop_reason == "max_tokens":
            yield _event("error", "The reply hit the length limit.")
            return
    yield _event("done")


# --------------------------------------------------------------- briefing
def briefing_context(svc: Services) -> dict[str, Any]:
    db = svc.db
    return {
        "today": date.today().isoformat(), "profile": profile(svc), "load": analytics.load_status(db),
        "weekly_volume": analytics.volume_by_period(db, "week", 6), "last_14_days": analytics.recent_activities(db, 14),
        "wellness_21_days": analytics.wellness_trend(db, 21), "intensity_4_weeks": analytics.zone_distribution(db, 4),
        "planned_workouts": [{k: w[k] for k in ("name", "sport", "status", "scheduled_date", "summary")}
                             for w in svc.planner.list(date.today().isoformat())],
        "notices": notices(svc),
    }


def generate_briefing(svc: Services) -> dict[str, Any]:
    client = _client(svc)
    data = json.dumps(briefing_context(svc), default=str)
    params = {**_params(svc), "max_tokens": 16000, "output_config": {"effort": "low"}}
    with client.beta.messages.stream(**params, messages=[
        {"role": "user", "content": [{"type": "text", "text": BRIEFING_PROMPT}, {"type": "text", "text": data}]},
    ]) as stream:
        final = stream.get_final_message()
    if final.stop_reason == "refusal":
        raise RuntimeError("briefing was declined")
    body = "\n".join(b.text for b in final.content if b.type == "text").strip()
    headline, _, rest = body.partition("\n")
    row = {"id": uuid.uuid4().hex[:12], "created_at": now_str(), "headline": headline.strip().lstrip("#").strip(),
           "body": rest.strip(), "model": final.model}
    svc.db.execute("INSERT INTO coach_briefings (id, created_at, headline, body, model) VALUES (:id, :created_at, :headline, :body, :model)", row)
    return row


def latest_briefing(svc: Services) -> dict[str, Any] | None:
    return svc.db.query_one("SELECT * FROM coach_briefings ORDER BY created_at DESC LIMIT 1")


def safe_briefing(svc: Services) -> None:
    """Called after the daily sync; never raises."""
    if not (enabled(svc) and svc.settings.coach_daily_briefing):
        return
    try:
        generate_briefing(svc)
    except Exception:
        log.exception("daily briefing failed")


# ---------------------------------------------------------------- notices
def _avg(xs: list[float | None]) -> float | None:
    xs = [x for x in xs if x]
    return mean(xs) if xs else None


def notices(svc: Services, today: date | None = None, now: Callable[[], datetime] = datetime.now) -> list[dict[str, str]]:
    """Plain rules over the data, no AI: things worth a look today. level: warn | info | good."""
    db, today = svc.db, today or date.today()
    out: list[dict[str, str]] = []

    last = db.query_one("SELECT started_at, status FROM sync_runs ORDER BY started_at DESC LIMIT 1")
    if not last:
        out.append({"level": "warn", "title": "No Garmin sync yet", "detail": "Run tricoach login and backfill on the server."})
    elif last["status"] not in ("ok", "running"):
        out.append({"level": "warn", "title": "Last sync failed", "detail": f"Attempt at {last['started_at'][:16]} ended '{last['status']}'."})
    elif last["started_at"] < (now() - timedelta(hours=36)).isoformat(sep=" "):
        out.append({"level": "warn", "title": "Data is out of date", "detail": f"Last sync {last['started_at'][:16]}."})

    load = analytics.load_status(db, today)
    ratio = load.get("acute_chronic_ratio")
    if ratio and ratio > 1.5:
        out.append({"level": "warn", "title": "Load spike", "detail": f"Last 7 days are {ratio}× your 4-week average. Injury risk climbs above ~1.5."})
    elif ratio and ratio < 0.6:
        out.append({"level": "info", "title": "Training well below usual", "detail": f"{load['hours_7d']} h in 7 days, {ratio}× your 4-week average. Fine for a rest week."})

    since = (today - timedelta(days=28)).isoformat()
    well = db.query("SELECT day, resting_hr, hrv_last_night, hrv_status, sleep_s FROM wellness WHERE day >= :s ORDER BY day", {"s": since})
    if well:
        recent, base = well[-3:], well[:-3]
        hrv_now, hrv_base = _avg([w["hrv_last_night"] for w in recent]), _avg([w["hrv_last_night"] for w in base])
        if hrv_now and hrv_base and hrv_now < hrv_base * 0.9:
            out.append({"level": "warn", "title": "HRV is down", "detail": f"3-night average {hrv_now:.0f} ms vs {hrv_base:.0f} ms usual ({(hrv_now / hrv_base - 1) * 100:.0f}%)."})
        status = (well[-1].get("hrv_status") or "").upper()
        if status in ("LOW", "UNBALANCED", "POOR"):
            out.append({"level": "warn", "title": f"Garmin HRV status: {status.lower()}", "detail": f"As of {well[-1]['day']}."})
        rhr_now, rhr_base = _avg([w["resting_hr"] for w in recent]), _avg([w["resting_hr"] for w in base])
        if rhr_now and rhr_base and rhr_now > rhr_base + 5:
            out.append({"level": "warn", "title": "Resting HR is up", "detail": f"{rhr_now:.0f} bpm over 3 days vs {rhr_base:.0f} bpm usual."})
        sleep = _avg([w["sleep_s"] for w in well[-7:]])
        if sleep and sleep < 6.5 * 3600:
            out.append({"level": "warn", "title": "Short sleep", "detail": f"{sleep / 3600:.1f} h a night over the last week."})
        elif sleep and sleep >= 7.5 * 3600:
            out.append({"level": "good", "title": "Sleep on track", "detail": f"{sleep / 3600:.1f} h a night over the last week."})

    for sport in ("swim", "bike", "run"):
        row = db.query_one("SELECT MAX(local_date) AS d FROM activities WHERE sport = :s AND is_parent = 0", {"s": sport})
        if row and row["d"]:
            gap = (today - date.fromisoformat(row["d"])).days
            if gap > 7:
                out.append({"level": "info", "title": f"No {sport} for {gap} days", "detail": f"Last one on {row['d']}."})

    flagged = db.query("SELECT local_date FROM activities WHERE quality_flags IS NOT NULL AND local_date >= :s",
                       {"s": (today - timedelta(days=30)).isoformat()})
    if flagged:
        out.append({"level": "info", "title": f"{len(flagged)} swim{'s' if len(flagged) > 1 else ''} with a suspect pool length",
                    "detail": "Left out of swim pace stats. Fix the pool length in Garmin Connect to include them."})

    drafts = db.query("SELECT id FROM workouts WHERE status = 'draft'")
    if drafts:
        out.append({"level": "info", "title": f"{len(drafts)} draft workout{'s' if len(drafts) > 1 else ''} waiting",
                    "detail": "Review them on the Workouts page and press Send to Garmin."})

    order = {"warn": 0, "info": 1, "good": 2}
    return sorted(out, key=lambda n: order[n["level"]])
