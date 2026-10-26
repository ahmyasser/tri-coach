"""Run with:  python -m unittest discover -s tests   (or pytest)"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import unittest
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from fake_garmin import FakeGarmin  # noqa: E402

from tricoach import analytics  # noqa: E402
from tricoach.config import Settings  # noqa: E402
from tricoach.services import build_services  # noqa: E402
from tricoach.workouts import WorkoutSpec, describe, to_garmin  # noqa: E402

TODAY = date(2026, 10, 26)


def make_services(fake: FakeGarmin | None = None):
    fake = fake or FakeGarmin()
    settings = Settings(database_url="sqlite:///:memory:", dashboard_password="pw", mcp_token="mcptok",
                        sync_webhook_token="synctok", public_host="coach.test", request_pause_ms=0)
    return build_services(settings, api_factory=lambda: fake), fake


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.svc, self.fake = make_services()
        self.stats = self.svc.syncer().run(kind="backfill", full=True)
        self.db = self.svc.db

    def test_backfill_counts(self):
        self.assertEqual(self.stats["activities"], 8)
        n = self.db.query_one("SELECT COUNT(*) AS n FROM activities")["n"]
        self.assertEqual(n, 8 + 5)  # + 5 multisport legs

    def test_multisport_legs_linked(self):
        legs = self.db.query("SELECT sport FROM activities WHERE parent_id = 1006 ORDER BY start_local")
        self.assertEqual([l["sport"] for l in legs], ["swim", "transition", "bike", "transition", "run"])
        races = analytics.race_results(self.db)
        self.assertEqual(races[0]["time"], "5:44:57")
        self.assertEqual(races[0]["legs"][0]["pace"], "2:09/100m")
        self.assertEqual(races[0]["legs"][2]["speed"], "30.5 km/h")
        self.assertEqual(races[0]["legs"][4]["pace"], "5:51/km")

    def test_pool_length_flag(self):
        row = self.db.query_one("SELECT quality_flags, pool_length_m FROM activities WHERE activity_id = 1004")
        self.assertEqual(row["pool_length_m"], 50.0)
        self.assertIn("pool_length_suspect:likely_25m", row["quality_flags"])
        good = self.db.query_one("SELECT quality_flags FROM activities WHERE activity_id = 1009")
        self.assertIsNone(good["quality_flags"])

    def test_laps_stored(self):
        laps = self.db.query("SELECT * FROM laps WHERE activity_id = 1009 ORDER BY lap_index")
        self.assertEqual(len(laps), 6)
        self.assertEqual(sum(l["is_rest"] for l in laps), 2)
        raw = json.loads(laps[0]["raw"])
        self.assertNotIn("lengthDTOs", raw)

    def test_wellness(self):
        rows = {r["day"]: r for r in self.db.query("SELECT * FROM wellness")}
        self.assertEqual(rows["2026-10-01"]["sleep_score"], 77)
        self.assertEqual(rows["2026-10-18"]["hrv_status"], "BALANCED")
        self.assertEqual(rows["2026-10-18"]["resting_hr"], 47)

    def test_incremental_sync_is_idempotent_and_keeps_details(self):
        calls_before = len([c for c in self.fake.calls if c[0] == "get_activity_splits"])
        self.svc.syncer().run(kind="daily")
        n = self.db.query_one("SELECT COUNT(*) AS n FROM activities")["n"]
        self.assertEqual(n, 13)
        calls_after = len([c for c in self.fake.calls if c[0] == "get_activity_splits"])
        self.assertEqual(calls_before, calls_after, "details must not be refetched")
        self.assertEqual(self.db.query_one("SELECT COUNT(*) AS n FROM sync_runs WHERE status='ok'")["n"], 2)

    def test_new_activity_picked_up(self):
        from fake_garmin import _run
        self.fake.activities.insert(0, _run(1010, "2026-10-26 06:00:00", 5000, 1700))
        self.svc.syncer().run(kind="daily")
        self.assertIsNotNone(self.db.query_one("SELECT * FROM activities WHERE activity_id = 1010"))


class AnalyticsTests(unittest.TestCase):
    def setUp(self):
        self.svc, _ = make_services()
        self.svc.syncer().run(full=True)
        self.db = self.svc.db

    def test_swim_trend_excludes_glitches(self):
        trend = {(r["month"], r["venue"]): r for r in analytics.swim_trend(self.db, 12, today=TODAY)}
        oct50 = trend[("2026-10", "50 m pool")]
        self.assertEqual(oct50["sessions"], 2)
        # reps 200/400/200/100/100 at 1:53,1:40.5,1:37.5,1:44,1:43 -> median 1:43 ; the 0:37 glitch is excluded
        self.assertEqual(oct50["median_rep_pace_100m"], "1:43")
        aug = trend[("2026-08", "50 m pool")]
        self.assertEqual(aug["flagged_sessions_excluded"], 1)
        self.assertEqual(aug["km"], 2.5)  # corrected from 5.0 km
        self.assertIsNone(aug["median_rep_pace_s"])
        self.assertIn(("2026-10", "open water"), trend)

    def test_best_efforts_and_css(self):
        best = analytics.swim_best_efforts(self.db, "2026-01-01")
        self.assertEqual(best["400"][0]["time"], "6:42")
        self.assertEqual(best["200"][0]["time"], "3:15")
        self.assertEqual(best["css"]["pace_100m"], "1:44")
        self.db.set_setting("css_pace_s", 100.0, "now")
        self.assertEqual(analytics.swim_best_efforts(self.db, "2026-01-01")["css"]["pace_100m"], "1:40")

    def test_volume(self):
        weeks = analytics.volume_by_period(self.db, "week", 4, today=TODAY)
        self.assertEqual(len(weeks), 4)
        race_week = next(w for w in weeks if w["period"] == "2026-10-12")
        self.assertAlmostEqual(race_week["bike_h"], 10740 / 3600, places=2)
        self.assertEqual(race_week["sessions"], 4)  # 3 legs + OW swim (transitions and parent excluded)
        months = analytics.volume_by_period(self.db, "month", 4, today=TODAY)
        self.assertEqual(months[-1]["period"], "2026-10")
        aug = next(m for m in months if m["period"] == "2026-08")
        self.assertEqual(aug["swim_km"], 2.5)

    def test_activity_detail(self):
        d = analytics.activity_detail(self.db, 1009)
        self.assertEqual(d["pace"], "1:51/100m")
        self.assertEqual(d["laps"][1], {"rest": "0:30"})
        self.assertEqual(d["laps"][2]["pace"], "1:40")


class WorkoutTests(unittest.TestCase):
    SWIM = {
        "name": "CSS 6x200", "sport": "swim", "pool_length_m": 25,
        "steps": [
            {"kind": "warmup", "distance_m": 400, "note": "easy"},
            {"repeat": 4, "steps": [{"kind": "interval", "distance_m": 50, "stroke": "drill", "note": "catch-up"},
                                    {"kind": "rest", "seconds": 15}]},
            {"repeat": 6, "skip_last_rest": True, "steps": [
                {"kind": "interval", "distance_m": 200, "css_offset_s": 2},
                {"kind": "rest", "seconds": 20}]},
            {"kind": "interval", "distance_m": 300, "equipment": "pull_buoy", "pace": "1:50-1:55"},
            {"kind": "cooldown", "distance_m": 200, "stroke": "any"},
        ],
    }

    def test_swim_json(self):
        spec = WorkoutSpec.model_validate(self.SWIM)
        w = to_garmin(spec, css_pace_s=104)
        self.assertEqual(w["sportType"]["sportTypeKey"], "swimming")
        self.assertEqual(w["poolLength"], 25.0)
        steps = w["workoutSegments"][0]["workoutSteps"]
        self.assertEqual(steps[0]["endCondition"]["conditionTypeKey"], "distance")
        self.assertEqual(steps[0]["strokeType"]["strokeTypeKey"], "any_stroke")
        rep = steps[2]
        self.assertEqual(rep["type"], "RepeatGroupDTO")
        self.assertEqual(rep["numberOfIterations"], 6)
        self.assertTrue(rep["skipLastRestStep"])
        main, rest = rep["workoutSteps"]
        self.assertEqual(main["strokeType"]["strokeTypeKey"], "free")
        self.assertEqual(main["secondaryTargetType"]["workoutTargetTypeKey"], "swim.css.offset")
        self.assertEqual(main["secondaryTargetValueOne"], 2.0)
        self.assertIn("1:46/100m", main["description"])
        self.assertEqual(rest["endCondition"]["conditionTypeKey"], "fixed.rest")
        self.assertNotIn("strokeType", rest)
        pull = steps[3]
        self.assertEqual(pull["equipmentType"]["equipmentTypeKey"], "pull_buoy")
        self.assertEqual(pull["targetType"]["workoutTargetTypeKey"], "pace.zone")
        self.assertAlmostEqual(pull["targetValueOne"], 100 / 115, places=3)  # slower bound first
        orders = []

        def walk(ss):
            for s in ss:
                orders.append(s["stepOrder"])
                walk(s.get("workoutSteps", []))
        walk(steps)
        self.assertEqual(orders, sorted(orders))
        self.assertEqual(len(orders), len(set(orders)))
        self.assertGreater(w["estimatedDurationInSecs"], 1800)
        self.assertIn("2300 m total", describe(spec))

    def test_run_json(self):
        spec = WorkoutSpec.model_validate({"name": "Threshold", "sport": "run", "steps": [
            {"kind": "warmup", "seconds": 600, "hr_zone": 2},
            {"repeat": 5, "steps": [{"kind": "interval", "distance_m": 1000, "pace": "4:35-4:45"},
                                    {"kind": "recovery", "seconds": 90}]},
            {"kind": "cooldown"}]})
        w = to_garmin(spec)
        steps = w["workoutSegments"][0]["workoutSteps"]
        self.assertEqual(steps[0]["zoneNumber"], 2)
        iv = steps[1]["workoutSteps"][0]
        self.assertAlmostEqual(iv["targetValueOne"], 1000 / 285, places=3)
        self.assertEqual(steps[2]["endCondition"]["conditionTypeKey"], "lap.button")
        self.assertNotIn("poolLength", w)

    def test_validation(self):
        with self.assertRaises(Exception):
            WorkoutSpec.model_validate({"name": "x", "sport": "run", "steps": [{"distance_m": 100, "seconds": 30}]})
        with self.assertRaises(Exception):
            WorkoutSpec.model_validate({"name": "x", "sport": "run", "steps": [{"distance_m": 100, "stroke": "free"}]})

    def test_planner_push_and_unpush(self):
        svc, fake = make_services()
        p = svc.planner
        w = p.create(self.SWIM, notes="week 1", scheduled_date="2026-11-03")
        self.assertEqual(w["status"], "draft")
        self.assertEqual(fake.uploaded, [])
        w = p.push(w["id"])
        self.assertEqual(w["status"], "scheduled")
        self.assertEqual(fake.scheduled, [(901, "2026-11-03")])
        self.assertEqual(w["garmin_schedule_id"], 7001)
        with self.assertRaises(ValueError):
            p.update(w["id"], notes="x")
        w = p.unpush(w["id"])
        self.assertEqual((w["status"], w["garmin_workout_id"]), ("draft", None))
        self.assertEqual(fake.unscheduled, [7001])
        self.assertEqual(fake.deleted, [901])
        p.delete(w["id"])
        self.assertEqual(p.list(), [])


class McpTests(unittest.TestCase):
    def test_tools_end_to_end(self):
        from mcp import Client

        svc, fake = make_services()
        svc.syncer().run(full=True)
        from tricoach.mcp_server import build_mcp

        server = build_mcp(svc)

        async def go():
            async with Client(server) as c:
                names = {t.name for t in (await c.list_tools()).tools}
                self.assertTrue({"swim_trend", "create_workout", "push_workout", "run_sql", "save_note"} <= names)
                r = await c.call_tool("race_results", {})
                self.assertFalse(r.is_error)
                self.assertIn("5:44:57", json.dumps(r.structured_content or [x.text for x in r.content]))
                r = await c.call_tool("run_sql", {"sql": "SELECT COUNT(*) AS n FROM activities WHERE sport='swim'"})
                self.assertIn('"n"', json.dumps(r.structured_content))
                r = await c.call_tool("run_sql", {"sql": "DELETE FROM activities"})
                self.assertIn("Only a single SELECT", json.dumps(r.structured_content))
                r = await c.call_tool("run_sql", {"sql": "WITH x AS (SELECT 1) SELECT * FROM x; DROP TABLE laps"})
                self.assertIn("Only a single SELECT", json.dumps(r.structured_content))
                r = await c.call_tool("set_css", {"pace_per_100m": "1:44"})
                self.assertIn("1:44", json.dumps(r.structured_content))
                r = await c.call_tool("save_note", {"body": "Off-season swim block starts Nov 2", "topic": "plan"})
                self.assertFalse(r.is_error)
                r = await c.call_tool("create_workout", {"spec": WorkoutTests.SWIM, "scheduled_date": "2026-11-03"})
                self.assertFalse(r.is_error, r)
                wid = r.structured_content["id"]
                self.assertEqual(fake.uploaded, [])
                r = await c.call_tool("push_workout", {"workout_id": wid})
                self.assertEqual(r.structured_content["status"], "scheduled")
                self.assertEqual(len(fake.uploaded), 1)
                r = await c.call_tool("get_athlete_profile", {})
                self.assertIn("Off-season", json.dumps(r.structured_content))
                r = await c.call_tool("create_workout", {"spec": {"name": "bad", "sport": "run", "steps": []}})
                self.assertTrue(r.is_error)

        asyncio.run(go())
        # a write attempt through run_sql must not have changed anything
        self.assertGreater(svc.db.query_one("SELECT COUNT(*) AS n FROM laps")["n"], 0)


class WebTests(unittest.TestCase):
    def setUp(self):
        from starlette.testclient import TestClient

        from tricoach.web import create_app

        self.svc, _ = make_services()
        self.svc.syncer().run(full=True)
        self.client = TestClient(create_app(self.svc, run_scheduler=False), base_url="http://coach.test")
        self.client.__enter__()
        self.auth = {"Authorization": "Basic " + base64.b64encode(b"ahmed:pw").decode()}

    def tearDown(self):
        self.client.__exit__(None, None, None)

    def test_dashboard_auth_and_pages(self):
        self.assertEqual(self.client.get("/").status_code, 401)
        for path in ("/", "/swim", "/activities", "/activities/1009", "/workouts", "/wellness", "/api/volume"):
            r = self.client.get(path, headers=self.auth)
            self.assertEqual(r.status_code, 200, path)
        self.assertIn("5:44:57", self.client.get("/", headers=self.auth).text)
        self.assertEqual(self.client.get("/activities/999", headers=self.auth).status_code, 404)
        self.assertEqual(self.client.get("/healthz").status_code, 200)

    def test_sync_webhook_auth(self):
        self.assertEqual(self.client.post("/api/sync").status_code, 401)
        self.assertEqual(self.client.post("/api/sync?token=nope").status_code, 401)
        self.assertEqual(self.client.post("/api/sync", headers={"X-Sync-Token": "synctok"}).status_code, 200)

    def test_mcp_http(self):
        init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}
        hdr = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
        self.assertEqual(self.client.post("/mcp", json=init, headers=hdr).status_code, 401)
        self.assertEqual(self.client.post("/mcp/wrong", json=init, headers=hdr).status_code, 401)
        r = self.client.post("/mcp/mcptok", json=init, headers=hdr)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("tri-coach", r.text)
        r = self.client.post("/mcp", json=init, headers={**hdr, "Authorization": "Bearer mcptok"})
        self.assertEqual(r.status_code, 200, r.text)
        listing = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
        r = self.client.post("/mcp/mcptok", json=listing, headers={**hdr, "MCP-Protocol-Version": "2025-06-18"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("swim_trend", r.text)


if __name__ == "__main__":
    unittest.main()
