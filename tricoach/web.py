"""Web app: dashboard, JSON API, sync webhook and the MCP endpoint, in one process."""

from __future__ import annotations

import base64
import contextlib
import hmac
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.concurrency import run_in_threadpool
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response, StreamingResponse
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from . import analytics, coach, scheduler
from .garmin import WebLogin, tokens_present
from .mcp_server import build_mcp
from .services import Services, build_services

log = logging.getLogger(__name__)
TEMPLATES = Environment(loader=FileSystemLoader(Path(__file__).parent / "templates"),
                        autoescape=select_autoescape(["html"]))
TEMPLATES.filters["tojson_safe"] = lambda v: Markup(
    json.dumps(v, default=str).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026"))


def _eq(a: str, b: str) -> bool:
    return bool(a) and bool(b) and hmac.compare_digest(a.encode(), b.encode())


class Gate:
    """Auth in front of everything.

    - /healthz: open
    - /mcp/<MCP_TOKEN> or /mcp with "Authorization: Bearer <MCP_TOKEN>": MCP
    - /api/sync: SYNC_WEBHOOK_TOKEN via "X-Sync-Token" header or ?token=
    - everything else: HTTP basic auth (DASHBOARD_USER / DASHBOARD_PASSWORD)
    """

    def __init__(self, svc: Services, site: ASGIApp, mcp_app: ASGIApp):
        self.svc, self.site, self.mcp_app = svc, site, mcp_app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.site(scope, receive, send)
            return
        path: str = scope["path"]
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        s = self.svc.settings

        if path == "/healthz":
            await self.site(scope, receive, send)
            return

        if path == "/mcp" or path.startswith("/mcp/"):
            rest = path[len("/mcp"):].strip("/")
            bearer = headers.get("authorization", "").removeprefix("Bearer ").strip()
            if not (_eq(rest, s.mcp_token) or (rest == "" and _eq(bearer, s.mcp_token))):
                await PlainTextResponse("unauthorized", status_code=401)(scope, receive, send)
                return
            await self.mcp_app({**scope, "path": "/", "raw_path": b"/"}, receive, send)
            return

        if path == "/api/sync":
            qs = dict(p.split("=", 1) for p in scope.get("query_string", b"").decode().split("&") if "=" in p)
            if not _eq(headers.get("x-sync-token", "") or qs.get("token", ""), s.sync_webhook_token):
                await PlainTextResponse("unauthorized", status_code=401)(scope, receive, send)
                return
            await self.site(scope, receive, send)
            return

        if not s.dashboard_password:
            await PlainTextResponse("Set DASHBOARD_PASSWORD to use the dashboard", status_code=503)(scope, receive, send)
            return
        auth = headers.get("authorization", "")
        ok = False
        if auth.startswith("Basic "):
            with contextlib.suppress(Exception):
                user, _, pw = base64.b64decode(auth[6:]).decode().partition(":")
                ok = _eq(user, s.dashboard_user) and _eq(pw, s.dashboard_password)
        if not ok:
            await Response("auth required", 401, {"WWW-Authenticate": 'Basic realm="tri-coach"'})(scope, receive, send)
            return
        await self.site(scope, receive, send)


def create_app(svc: Services | None = None, run_scheduler: bool | None = None) -> ASGIApp:
    svc = svc or build_services()
    if run_scheduler is None:
        run_scheduler = os.getenv("SCHEDULER_ENABLED", "1") == "1"
    db = svc.db

    garmin_login = WebLogin(svc.settings.garmin_tokenstore)

    def render(name: str, **ctx: Any) -> HTMLResponse:
        last = db.query_one("SELECT started_at, status FROM sync_runs ORDER BY started_at DESC LIMIT 1")
        return HTMLResponse(TEMPLATES.get_template(name).render(
            last_sync=last, garmin_connected=tokens_present(svc.settings.garmin_tokenstore),
            athlete_name=svc.settings.athlete_name, **ctx))

    def same_origin(request: Request) -> bool:
        # Basic-auth credentials ride along on cross-site POSTs; a custom header forces a CORS preflight that fails.
        return request.headers.get("x-requested-with") == "tricoach"

    async def home(request: Request) -> Response:
        return render("index.html", page="home", overview=analytics.overview(db), load=analytics.load_status(db),
                      weekly=analytics.volume_by_period(db, "week", 26), monthly=analytics.volume_by_period(db, "month", 15),
                      races=analytics.race_results(db), recent=analytics.recent_activities(db, 10),
                      notices=coach.notices(svc)[:3], briefing=coach.latest_briefing(svc))

    async def swim(request: Request) -> Response:
        return render("swim.html", page="swim", trend=analytics.swim_trend(db, 15), best=analytics.swim_best_efforts(db),
                      recent=analytics.recent_activities(db, 60, "swim"))

    async def activities(request: Request) -> Response:
        days = int(request.query_params.get("days", "90"))
        sport = request.query_params.get("sport") or None
        return render("activities.html", page="activities", rows=analytics.recent_activities(db, days, sport),
                      days=days, sport=sport)

    async def activity(request: Request) -> Response:
        detail = analytics.activity_detail(db, int(request.path_params["aid"]))
        if not detail:
            return PlainTextResponse("not found", 404)
        return render("activity.html", page="activities", a=detail)

    async def workouts(request: Request) -> Response:
        return render("workouts.html", page="workouts", rows=svc.planner.list())

    async def wellness_page(request: Request) -> Response:
        return render("wellness.html", page="wellness", rows=analytics.wellness_trend(db, 120))

    async def coach_page(request: Request) -> Response:
        return render("coach.html", page="coach", notices=coach.notices(svc), briefing=coach.latest_briefing(svc),
                      history=coach.transcript(svc, coach.current_thread(svc)), enabled=coach.enabled(svc))

    async def api_coach_chat(request: Request) -> Response:
        if not same_origin(request):
            return PlainTextResponse("forbidden", 403)
        text = str((await request.json()).get("message", "")).strip()[:8000]
        if not text:
            return JSONResponse({"error": "empty message"}, 400)
        return StreamingResponse(coach.chat(svc, text), media_type="application/x-ndjson",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    async def api_coach_new(request: Request) -> Response:
        if not same_origin(request):
            return PlainTextResponse("forbidden", 403)
        return JSONResponse({"thread": coach.new_thread(svc)})

    async def api_coach_briefing(request: Request) -> Response:
        if not same_origin(request):
            return PlainTextResponse("forbidden", 403)
        if not coach.enabled(svc):
            return JSONResponse({"error": "Add ANTHROPIC_API_KEY to .env to generate briefings."}, 503)
        try:
            return JSONResponse(await run_in_threadpool(coach.generate_briefing, svc))
        except Exception:
            log.exception("briefing failed")
            return JSONResponse({"error": "Couldn't write the briefing. Check the server log."}, 502)

    async def connect_page(request: Request) -> Response:
        return render("connect.html", page="connect")

    def garmin_error(exc: Exception) -> JSONResponse:
        kind = type(exc).__name__
        if "Authentication" in kind:
            msg = "Garmin didn't accept that. Check the email, password or code and try again."
        elif "TooManyRequests" in kind or "429" in str(exc):
            msg = "Garmin is blocking sign-ins for a while after too many tries. Wait 15 minutes, then try again."
        elif "AuthMissing" in kind:
            msg = str(exc)
        else:
            log.exception("Garmin web login failed")
            msg = "Couldn't reach Garmin. Try again in a minute."
        return JSONResponse({"error": msg}, 400)

    def connected(result: dict[str, Any]) -> JSONResponse:
        threading.Thread(target=scheduler.run_sync, args=(svc, "connect"), daemon=True).start()
        return JSONResponse(result)

    async def api_garmin_login(request: Request) -> Response:
        if not same_origin(request):
            return PlainTextResponse("forbidden", 403)
        body = await request.json()
        email, password = str(body.get("email", "")).strip(), str(body.get("password", ""))
        if not email or not password:
            return JSONResponse({"error": "Enter your Garmin email and password."}, 400)
        try:
            result = await run_in_threadpool(garmin_login.start, email, password)
        except Exception as exc:  # noqa: BLE001 - mapped to a plain message
            return garmin_error(exc)
        return JSONResponse(result) if result["needs_code"] else connected(result)

    async def api_garmin_code(request: Request) -> Response:
        if not same_origin(request):
            return PlainTextResponse("forbidden", 403)
        code = str((await request.json()).get("code", "")).strip()
        if not code:
            return JSONResponse({"error": "Enter the code Garmin sent you."}, 400)
        try:
            return connected(await run_in_threadpool(garmin_login.code, code))
        except Exception as exc:  # noqa: BLE001 - mapped to a plain message
            return garmin_error(exc)

    async def api_workout_push(request: Request) -> Response:
        if not same_origin(request):
            return PlainTextResponse("forbidden", 403)
        when = str((await request.json()).get("date", "")).strip() or None
        try:
            w = await run_in_threadpool(svc.planner.push, request.path_params["wid"], when)
        except Exception as exc:  # noqa: BLE001 - shown on the button
            log.exception("push failed")
            return JSONResponse({"error": f"Couldn't send it to Garmin: {exc}"}, 400)
        return JSONResponse({"status": w["status"], "scheduled_date": w.get("scheduled_date")})

    async def api_volume(request: Request) -> Response:
        period = request.query_params.get("period", "week")
        count = int(request.query_params.get("count", "26"))
        return JSONResponse(analytics.volume_by_period(db, period, count))

    async def api_swim(request: Request) -> Response:
        return JSONResponse(analytics.swim_trend(db, int(request.query_params.get("months", "12"))))

    async def api_sync(request: Request) -> Response:
        full = request.query_params.get("full") == "1"
        threading.Thread(target=scheduler.run_sync, args=(svc, "webhook-full" if full else "webhook"), daemon=True).start()
        return JSONResponse({"started": True})

    async def healthz(request: Request) -> Response:
        last = db.query_one("SELECT started_at, status FROM sync_runs ORDER BY started_at DESC LIMIT 1")
        return JSONResponse({"ok": True, "last_sync": last})

    mcp = build_mcp(svc)
    hosts = [svc.settings.public_host, f"{svc.settings.public_host}:*", "localhost:*", "127.0.0.1:*", "app:*", "app"]
    mcp_app = mcp.streamable_http_app(
        streamable_http_path="/",
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=hosts,
            allowed_origins=[f"https://{svc.settings.public_host}", "https://claude.ai", "http://localhost:*"],
        ),
    )

    stop = threading.Event()

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette):
        async with mcp_app.router.lifespan_context(mcp_app):
            if run_scheduler:
                scheduler.start(svc, stop)
            yield
            stop.set()

    site = Starlette(
        routes=[
            Route("/", home), Route("/swim", swim), Route("/activities", activities),
            Route("/activities/{aid:int}", activity), Route("/workouts", workouts), Route("/wellness", wellness_page),
            Route("/coach", coach_page), Route("/api/coach/chat", api_coach_chat, methods=["POST"]),
            Route("/api/coach/new", api_coach_new, methods=["POST"]),
            Route("/api/coach/briefing", api_coach_briefing, methods=["POST"]),
            Route("/connect", connect_page), Route("/api/garmin/login", api_garmin_login, methods=["POST"]),
            Route("/api/garmin/code", api_garmin_code, methods=["POST"]),
            Route("/api/workouts/{wid}/push", api_workout_push, methods=["POST"]),
            Route("/api/volume", api_volume), Route("/api/swim", api_swim),
            Route("/api/sync", api_sync, methods=["GET", "POST"]), Route("/healthz", healthz),
        ],
        lifespan=lifespan,
    )
    return Gate(svc, site, mcp_app)


def main() -> None:
    import uvicorn

    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    uvicorn.run(create_app(), host="0.0.0.0", port=int(os.getenv("PORT", "8000")), proxy_headers=True,
                forwarded_allow_ips="*")
