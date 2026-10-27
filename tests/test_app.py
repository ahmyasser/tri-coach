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
    settings = Settings(database_url="sqlite:///:memory:", public_host="coach.test", request_pause_ms=0)
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

        server = build_mcp(lambda: svc)

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


class WebBase(unittest.TestCase):
    """A multi-user app on SQLite with fake Garmin accounts and a scripted Claude."""

    def setUp(self):
        from cryptography.fernet import Fernet
        from starlette.testclient import TestClient

        from tricoach.accounts import Accounts, Tenants
        from tricoach.db import Database
        from tricoach.web import create_app

        self.settings = Settings(database_url="sqlite:///:memory:", public_host="coach.test", request_pause_ms=0,
                                 secret_key="test", token_key=Fernet.generate_key().decode(), admin_emails="admin@example.com",
                                 anthropic_api_key="", free_daily_usd=0.10, pro_daily_usd=0.50)
        self.accounts = Accounts(self.settings, Database(self.settings.database_url))
        self.garmins: dict[str, FakeGarmin] = {}
        self.tenants = Tenants(self.settings, self.accounts,
                               garmin_factory=lambda user, tokens: self.garmins.setdefault(user["email"], FakeGarmin()))
        self.app = create_app(self.tenants, run_scheduler=False)
        self.clients = []

    def tearDown(self):
        for c in self.clients:
            c.__exit__(None, None, None)

    def client(self):
        from starlette.testclient import TestClient

        # Only the first client runs the app's startup (the MCP session manager starts once); the rest share it.
        c = TestClient(self.app, base_url="http://coach.test")
        if not self.clients:
            c.__enter__()
            self.clients.append(c)
        return c

    H = {"X-Requested-With": "tricoach"}

    def sign_in(self, email, name="Sara", onboard=True, connect=False):
        """Create the account (if needed) and return a client signed in through a real sign-in link."""
        user = self.accounts.by("email", email) or self.accounts.create(email)
        c = self.client()
        r = c.get("/auth/link?t=" + self.accounts.new_login_link(email), follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        if onboard:
            c.post("/api/profile", json={"name": name, "focus": "triathlon", "timezone": "Africa/Cairo"}, headers=self.H)
        if connect:
            self.accounts.save_garmin_tokens(user["id"], '{"fake": "tokens"}')
            from tricoach import scheduler
            scheduler.run_sync(self.tenants.services(self.accounts.get(user["id"])), "backfill")
        return c, self.accounts.by("email", email)


class PublicAndSignupTests(WebBase):
    def test_landing_legal_and_app_files(self):
        c = self.client()
        r = c.get("/")
        self.assertIn("Join the waitlist", r.text)
        self.assertNotIn("Overview", r.text)
        for path in ("/privacy", "/terms", "/login", "/join", "/healthz", "/icon-192.png", "/apple-touch-icon.png", "/icon.svg"):
            self.assertEqual(c.get(path).status_code, 200, path)
        self.assertEqual(c.get("/manifest.webmanifest").json()["display"], "standalone")
        self.assertEqual(c.get("/coach", follow_redirects=False).headers["location"], "/login?next=/coach")
        self.assertEqual(c.get("/api/volume").status_code, 401)

    def test_waitlist(self):
        c = self.client()
        self.assertEqual(c.post("/api/waitlist", json={"email": "a@b.co"}).status_code, 403)
        self.assertEqual(c.post("/api/waitlist", json={"email": "nope"}, headers=self.H).status_code, 400)
        self.assertTrue(c.post("/api/waitlist", json={"email": "A@B.co", "sport": "running"}, headers=self.H).json()["ok"])
        self.assertEqual([w["email"] for w in self.accounts.waitlist()], ["a@b.co"])

    def test_invite_only_signup_then_onboarding(self):
        from unittest import mock

        sent = []
        c = self.client()
        with mock.patch("tricoach.auth.Mailer.send_link", lambda self_, to, link, invited=False: sent.append((to, link)) or True):
            self.assertTrue(c.post("/auth/email", json={"email": "new@x.co"}, headers=self.H).json()["needs_invite"])
            code = self.accounts.create_invite("for new")
            self.assertIn("You're invited", c.get(f"/join?code={code.lower()}").text)
            self.assertTrue(c.post("/auth/email", json={"email": "new@x.co"}, headers=self.H).json()["sent"])
            # the invite is used up once the account exists; a second person can't reuse it
            link = sent[-1][1].replace("http://coach.test", "")
            self.assertEqual(c.get(link, follow_redirects=False).headers["location"], "/")
            self.assertEqual(c.get("/", follow_redirects=False).headers["location"], "/welcome")
            self.assertIn("Tell your coach", c.get("/welcome").text)
            self.assertEqual(c.post("/api/profile", json={"name": "", "focus": "x"}, headers=self.H).status_code, 400)
            self.assertTrue(c.post("/api/profile", json={"name": "Nour", "focus": "running", "timezone": "Europe/Berlin"}, headers=self.H).json()["ok"])
            page = c.get("/").text
            self.assertIn("Connect your Garmin", page)
            other = self.client()
            other.get(f"/join?code={code}")
            self.assertTrue(other.post("/auth/email", json={"email": "two@x.co"}, headers=self.H).json()["needs_invite"])
        user = self.accounts.by("email", "new@x.co")
        self.assertEqual((user["name"], user["focus"], user["timezone"], user["tier"]), ("Nour", "running", "Europe/Berlin", "free"))
        # links work once
        self.assertIn("error=link", c.get(link, follow_redirects=False).headers["location"])

    def test_sign_in_email_rate_limit(self):
        self.accounts.create("busy@x.co")
        c = self.client()
        codes = [c.post("/auth/email", json={"email": "busy@x.co"}, headers=self.H).status_code for _ in range(7)]
        self.assertEqual(codes[:5], [200] * 5)
        self.assertEqual(codes[-1], 429)


class MultiUserTests(WebBase):
    def test_users_only_see_their_own_data(self):
        a, ua = self.sign_in("a@x.co", "Ahmed", connect=True)
        b, ub = self.sign_in("b@x.co", "Hana")
        self.assertIn("5:44:57", a.get("/").text)
        page_b = b.get("/").text
        self.assertNotIn("5:44:57", page_b)
        self.assertIn("Connect your Garmin", page_b)
        self.assertEqual(a.get("/activities/1009").status_code, 200)
        self.assertEqual(b.get("/activities/1009").status_code, 404)
        self.assertEqual(b.get("/api/volume").json()[-1]["swim_km"], 0)
        # Garmin tokens are stored encrypted
        self.assertNotIn("fake", self.accounts.get(ua["id"])["garmin_tokens"])

    def test_mcp_is_scoped_by_token(self):
        _, ua = self.sign_in("a@x.co", connect=True)
        _, ub = self.sign_in("b@x.co")
        c = self.client()
        hdr = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json", "MCP-Protocol-Version": "2025-06-18"}
        call = {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "training_overview", "arguments": {}}}
        self.assertEqual(c.post("/mcp/nope", json=call, headers=hdr).status_code, 401)
        ra = c.post(f"/mcp/{ua['mcp_token']}", json=call, headers=hdr)
        rb = c.post(f"/mcp/{ub['mcp_token']}", json=call, headers=hdr)
        self.assertEqual(ra.status_code, 200, ra.text)
        self.assertIn('{"sport":"swim","n":7', ra.text)
        self.assertIn('"by_sport":[]', rb.text)
        r = c.post("/mcp", json=call, headers={**hdr, "Authorization": f"Bearer {ub['mcp_token']}"})
        self.assertEqual(r.status_code, 200, r.text)

    def test_sync_webhook_per_user(self):
        from unittest import mock

        _, ua = self.sign_in("a@x.co")
        c = self.client()
        self.assertEqual(c.post("/api/sync?token=wrong").status_code, 401)
        with mock.patch("tricoach.scheduler.run_sync") as run:
            self.assertEqual(c.get(f"/api/sync?token={ua['sync_token']}").status_code, 200)
        self.assertEqual(run.call_args[0][0].user["id"], ua["id"])

    def test_logout_everywhere_and_delete_account(self):
        a, ua = self.sign_in("a@x.co", connect=True)
        a2 = self.client()
        a2.get("/auth/link?t=" + self.accounts.new_login_link("a@x.co"))
        self.assertEqual(a2.get("/settings").status_code, 200)
        a.post("/api/account/logout-everywhere", headers=self.H)
        self.assertEqual(a2.get("/settings", follow_redirects=False).status_code, 303)

        c, uc = self.sign_in("c@x.co", connect=True)
        self.assertEqual(c.post("/api/account/delete", json={"confirm": "wrong"}, headers=self.H).status_code, 400)
        self.assertTrue(c.post("/api/account/delete", json={"confirm": "c@x.co"}, headers=self.H).json()["ok"])
        self.assertIsNone(self.accounts.by("email", "c@x.co"))
        self.assertIn("Join the waitlist", c.get("/").text)

    def test_admin(self):
        u, _ = self.sign_in("user@x.co")
        self.assertEqual(u.get("/admin").status_code, 404)
        self.assertEqual(u.post("/api/admin/invite", json={}, headers=self.H).status_code, 404)
        admin, ua = self.sign_in("admin@example.com", "Ahmed")
        self.assertIn("Beta", admin.get("/admin").text)
        self.accounts.join_waitlist("wait@x.co")
        j = admin.post("/api/admin/invite", json={"email": "wait@x.co"}, headers=self.H).json()
        self.assertTrue(self.accounts.invite_ok(j["code"]))
        self.assertFalse(j["emailed"])  # no Resend key in tests
        self.assertEqual(self.accounts.waitlist()[0]["invited_code"], j["code"])
        self.assertIn("error", admin.post("/api/admin/login-link", json={"email": "ghost@x.co"}, headers=self.H).json())
        self.assertIn("/auth/link?t=", admin.post("/api/admin/login-link", json={"email": "user@x.co"}, headers=self.H).json()["link"])
        target = self.accounts.by("email", "user@x.co")
        self.assertTrue(admin.post("/api/admin/tier", json={"user_id": target["id"], "tier": "pro"}, headers=self.H).json()["ok"])
        self.assertEqual(self.accounts.get(target["id"])["tier"], "pro")


class CoachTests(WebBase):
    def setUp(self):
        super().setUp()
        self.c, self.user = self.sign_in("a@x.co", "Sara", connect=True)

    def llm(self, replies, usage=None):
        fake = FakeLLM(replies, usage)
        self.tenants.llm_factory = lambda: fake
        return fake

    def test_coach_off_without_key(self):
        self.assertIn("warming up", self.c.get("/coach").text)
        r = self.c.post("/api/coach/chat", json={"message": "hi"}, headers=self.H)
        self.assertIn("isn't switched on", r.text)
        self.assertEqual(self.c.post("/api/coach/chat", json={"message": "hi"}).status_code, 403)

    def test_chat_with_tools_uses_tier_model(self):
        from tricoach import coach

        fake = self.llm([[{"type": "tool_use", "id": "tu1", "name": "training_overview", "input": {}}],
                         [{"type": "text", "text": "Solid week. **Swim more.**"}]])
        r = self.c.post("/api/coach/chat", json={"message": "How was my week?"}, headers=self.H)
        events = [json.loads(line) for line in r.text.splitlines()]
        self.assertEqual([e["t"] for e in events], ["tool", "text", "allowance", "done"])
        self.assertEqual(fake.calls[0]["model"], "claude-sonnet-5-5")
        self.assertIn("You are Sara's endurance coach", fake.calls[0]["system"][0]["text"])
        self.assertIn("by_sport", fake.calls[1]["messages"][-1]["content"][0]["content"])
        svc = self.tenants.services(self.accounts.get(self.user["id"]))
        history = coach.transcript(svc, coach.current_thread(svc))
        self.assertEqual(history[1]["tools"], ["Checked training overview"])
        self.assertIn("Swim more", self.c.get("/coach").text)

    def test_daily_allowance(self):
        from tricoach import coach

        big = {"input_tokens": 1000, "output_tokens": 20000}  # 0.002 + 0.20 USD on Sonnet 5.5
        self.llm([[{"type": "text", "text": "One."}]] * 3, usage=big)
        r = self.c.post("/api/coach/chat", json={"message": "q1"}, headers=self.H)
        self.assertIn('"allowance", "v": 0.0', r.text)
        self.assertAlmostEqual(self.accounts.spent_today(self.user["id"]), 0.202)
        r = self.c.post("/api/coach/chat", json={"message": "q2"}, headers=self.H)
        self.assertIn(coach.OUT_OF_ALLOWANCE, r.text)
        self.assertEqual(self.c.post("/api/coach/briefing", headers=self.H).status_code, 429)
        self.accounts.update(self.user["id"], tier="pro")  # cap 0.50 and Opus
        fake = self.llm([[{"type": "text", "text": "Two."}]], usage={"output_tokens": 10})
        r = self.c.post("/api/coach/chat", json={"message": "q3"}, headers=self.H)
        self.assertIn("Two.", r.text)
        self.assertEqual(fake.calls[0]["model"], "claude-opus-5-5")

    def test_briefing_and_notices(self):
        from tricoach import coach

        fake = self.llm([[{"type": "text", "text": "Recovery week: keep it easy.\n\n**Last 7 days**\n- 2 swims"}]],
                        usage={"output_tokens": 1000})
        r = self.c.post("/api/coach/briefing", headers=self.H)
        self.assertEqual(r.json()["headline"], "Recovery week: keep it easy.", r.text)
        self.assertIn("Recovery week", self.c.get("/").text)
        self.assertNotIn("tools", fake.calls[0])
        self.assertEqual(self.accounts.spent_today(self.user["id"]), 0)  # briefings don't use the allowance
        self.assertGreater(self.accounts.spent_today(self.user["id"] + "#briefing"), 0)
        svc = self.tenants.services(self.accounts.get(self.user["id"]))
        for n in coach.notices(svc):
            self.assertIn(n["level"], ("warn", "info", "good"))


class GarminConnectTests(WebBase):
    def test_code_login_saves_encrypted_tokens_and_syncs(self):
        from unittest import mock

        from tricoach import garmin

        c, user = self.sign_in("hana@x.co", "Hana")
        self.assertEqual(c.post("/api/garmin/login", json={"email": "h@g.co", "password": "x"}).status_code, 403)
        fake = FakeGarminLogin(needs_code=True)
        with mock.patch.object(garmin, "_new_client", lambda email, pw: fake), mock.patch("tricoach.scheduler.run_sync") as sync:
            self.assertEqual(c.post("/api/garmin/login", json={"email": "h@g.co", "password": "pw1"}, headers=self.H).json(), {"needs_code": True})
            self.assertIsNone(self.accounts.get(user["id"])["garmin_tokens"])
            r = c.post("/api/garmin/code", json={"code": "123456"}, headers=self.H)
            self.assertEqual(r.json()["name"], "Sara Example", r.text)
            sync.assert_called_once()
        self.assertEqual(self.accounts.garmin_tokens(self.accounts.get(user["id"])), fake.dumps())
        self.assertNotIn("Connect your Garmin", c.get("/").text)
        self.assertIn("expired", c.post("/api/garmin/code", json={"code": "1"}, headers=self.H).json()["error"])

    def test_wrong_password_message(self):
        from unittest import mock

        from tricoach import garmin

        c, _ = self.sign_in("x@x.co")
        with mock.patch.object(garmin, "_new_client", lambda email, pw: FakeGarminLogin(fail=True)):
            r = c.post("/api/garmin/login", json={"email": "a@b.c", "password": "nope"}, headers=self.H)
        self.assertIn("didn't accept", r.json()["error"])

    def test_send_draft_to_garmin(self):
        c, user = self.sign_in("a@x.co", connect=True)
        svc = self.tenants.services(self.accounts.get(user["id"]))
        w = svc.planner.create(WorkoutTests.SWIM)
        r = c.post(f"/api/workouts/{w['id']}/push", json={"date": "2026-11-03"}, headers=self.H)
        self.assertEqual(r.json(), {"status": "scheduled", "scheduled_date": "2026-11-03"}, r.text)
        self.assertEqual(len(self.garmins["a@x.co"].uploaded), 1)
        self.assertIn("on garmin", c.get("/workouts").text)


class FakeGarminLogin:
    """Stands in for garminconnect.Garmin(return_on_mfa=True) during the web sign-in."""

    def __init__(self, needs_code=False, fail=False):
        self.needs_code, self.fail, self.code_used = needs_code, fail, None
        self.full_name = None
        self.client = self

    def login(self):
        if self.fail:
            raise type("GarminConnectAuthenticationError", (Exception,), {})("401")
        return ("needs_mfa", {"state": 1}) if self.needs_code else (None, None)

    def resume_login(self, state, code):
        self.code_used = code
        return None, None

    def dumps(self):
        return '{"garmin": "tokens"}'

    def _load_profile_and_settings(self):
        self.full_name = "Sara Example"

class FakeBlock:
    def __init__(self, d):
        self.__dict__.update(d)
        self._d = d

    def model_dump(self, **_):
        return dict(self._d)


class FakeLLM:
    """Stands in for anthropic.Anthropic: each beta.messages.stream() call plays the next scripted reply."""

    def __init__(self, replies, usage=None):
        self.replies, self.calls, self.usage = list(replies), [], usage
        self.beta = self
        self.messages = self

    def stream(self, **kw):
        import copy

        self.calls.append(copy.deepcopy(kw))
        return FakeStream([FakeBlock(b) for b in self.replies.pop(0)], self.usage)


class FakeStream:
    def __init__(self, blocks, usage=None):
        self.blocks, self.usage = blocks, usage

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        from types import SimpleNamespace as NS

        for b in self.blocks:
            yield NS(type="content_block_start", content_block=b)
            if b.type == "text":
                yield NS(type="text", text=b.text)

    def get_final_message(self):
        from types import SimpleNamespace as NS

        tool = any(b.type == "tool_use" for b in self.blocks)
        return NS(content=self.blocks, model=None, usage=self.usage, stop_reason="tool_use" if tool else "end_turn")


if __name__ == "__main__":
    unittest.main()
