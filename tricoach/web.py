"""Web app: landing page, sign-in, each user's dashboard and coach, admin, sync webhook and MCP, in one process."""

from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import os
import secrets
import threading
import time
from collections import defaultdict, deque
from functools import wraps
from pathlib import Path
from typing import Any, Awaitable, Callable

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.middleware.sessions import SessionMiddleware
from starlette.requests import Request
from starlette.responses import (FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse,
                                 Response, StreamingResponse)
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from . import analytics, coach, scheduler
from .accounts import Tenants, build_tenants
from .auth import Google, Mailer, base_url
from .mcp_server import build_mcp
from .services import Services

log = logging.getLogger(__name__)
HERE = Path(__file__).parent
TEMPLATES = Environment(loader=FileSystemLoader(HERE / "templates"), autoescape=select_autoescape(["html"]))
TEMPLATES.filters["tojson_safe"] = lambda v: Markup(
    json.dumps(v, default=str).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026"))

SPORTS = ["triathlon", "running", "cycling", "swimming", "other"]
_mcp_user: contextvars.ContextVar[Services] = contextvars.ContextVar("tricoach_mcp_user")


class RateLimit:
    """In-memory sliding window, e.g. sign-in emails per address and per IP."""

    def __init__(self, limit: int, seconds: int):
        self.limit, self.seconds = limit, seconds
        self.hits: dict[str, deque[float]] = defaultdict(deque)
        self.lock = threading.Lock()

    def ok(self, key: str) -> bool:
        now = time.monotonic()
        with self.lock:
            q = self.hits[key]
            while q and now - q[0] > self.seconds:
                q.popleft()
            if len(q) >= self.limit:
                return False
            q.append(now)
            return True


class Gate:
    """MCP requests are matched to a user by the secret in their URL; everything else goes to the site."""

    def __init__(self, tenants: Tenants, site: ASGIApp, mcp_app: ASGIApp):
        self.tenants, self.site, self.mcp_app = tenants, site, mcp_app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if scope["type"] == "http" and (path == "/mcp" or path.startswith("/mcp/")):
            headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
            token = path[len("/mcp"):].strip("/") or headers.get("authorization", "").removeprefix("Bearer ").strip()
            user = await run_in_threadpool(self.tenants.accounts.by, "mcp_token", token)
            if not user:
                await PlainTextResponse("unauthorized", status_code=401)(scope, receive, send)
                return
            reset = _mcp_user.set(self.tenants.services(user))
            try:
                await self.mcp_app({**scope, "path": "/", "raw_path": b"/"}, receive, send)
            finally:
                _mcp_user.reset(reset)
            return
        await self.site(scope, receive, send)


def create_app(tenants: Tenants | None = None, run_scheduler: bool | None = None) -> ASGIApp:
    tenants = tenants or build_tenants()
    settings, accounts = tenants.settings, tenants.accounts
    google, mailer = Google(settings), Mailer(settings)
    if run_scheduler is None:
        run_scheduler = os.getenv("SCHEDULER_ENABLED", "1") == "1"
    email_limit, ip_limit, waitlist_limit = RateLimit(5, 3600), RateLimit(30, 3600), RateLimit(10, 3600)

    # ------------------------------------------------------------ helpers
    def user_of(request: Request) -> dict[str, Any] | None:
        uid = request.session.get("uid")
        if not uid:
            return None
        user = accounts.get(uid)
        if not user or user["session_version"] != request.session.get("v"):
            return None
        return user

    def sign_in(request: Request, user: dict[str, Any]) -> None:
        request.session.clear()
        request.session.update({"uid": user["id"], "v": user["session_version"]})
        accounts.update(user["id"], last_seen_at=time.strftime("%Y-%m-%d %H:%M:%S"))

    def render(request: Request, name: str, **ctx: Any) -> HTMLResponse:
        user = getattr(request.state, "user", None)
        svc: Services | None = getattr(request.state, "svc", None)
        last = svc.db.query_one("SELECT started_at, status FROM sync_runs ORDER BY started_at DESC LIMIT 1") if svc else None
        return HTMLResponse(TEMPLATES.get_template(name).render(
            brand=settings.brand_name, user=user, is_admin=bool(user and accounts.is_admin(user)), last_sync=last,
            garmin_connected=bool(user and user.get("garmin_tokens")), google_enabled=google.enabled, **ctx))

    def ip(request: Request) -> str:
        return request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (request.client.host if request.client else "-")

    Handler = Callable[[Request], Awaitable[Response]]

    def page(admin: bool = False, onboarding: bool = False) -> Callable[[Handler], Handler]:
        """Signed-in HTML page. Sends people to /login, then through /welcome until their profile is filled in."""
        def deco(fn: Handler) -> Handler:
            @wraps(fn)
            async def inner(request: Request) -> Response:
                user = await run_in_threadpool(user_of, request)
                if not user:
                    return RedirectResponse(f"/login?next={request.url.path}", 303)
                if admin and not accounts.is_admin(user):
                    return PlainTextResponse("not found", 404)
                if not user["onboarded"] and not onboarding:
                    return RedirectResponse("/welcome", 303)
                request.state.user = user
                request.state.svc = await run_in_threadpool(tenants.services, user)
                return await fn(request)
            return inner
        return deco

    def api(admin: bool = False) -> Callable[[Handler], Handler]:
        """Signed-in JSON endpoint. POSTs need our custom header, which a cross-site form or fetch can't send."""
        def deco(fn: Handler) -> Handler:
            @wraps(fn)
            async def inner(request: Request) -> Response:
                if request.method != "GET" and request.headers.get("x-requested-with") != "tricoach":
                    return PlainTextResponse("forbidden", 403)
                user = await run_in_threadpool(user_of, request)
                if not user:
                    return JSONResponse({"error": "Please sign in again."}, 401)
                if admin and not accounts.is_admin(user):
                    return JSONResponse({"error": "not found"}, 404)
                request.state.user = user
                request.state.svc = await run_in_threadpool(tenants.services, user)
                return await fn(request)
            return inner
        return deco

    def public_post(fn: Handler) -> Handler:
        @wraps(fn)
        async def inner(request: Request) -> Response:
            if request.headers.get("x-requested-with") != "tricoach":
                return PlainTextResponse("forbidden", 403)
            return await fn(request)
        return inner

    async def body(request: Request) -> dict[str, Any]:
        with contextlib.suppress(Exception):
            data = await request.json()
            if isinstance(data, dict):
                return data
        return {}

    def svc_of(request: Request) -> Services:
        return request.state.svc

    # ------------------------------------------------------------ public
    async def home(request: Request) -> Response:
        user = await run_in_threadpool(user_of, request)
        if not user:
            return render(request, "landing.html", page="landing")
        return await dashboard(request)

    @page()
    async def dashboard(request: Request) -> Response:
        db, svc = svc_of(request).db, svc_of(request)
        return render(request, "index.html", page="home", overview=analytics.overview(db), load=analytics.load_status(db),
                      weekly=analytics.volume_by_period(db, "week", 26), monthly=analytics.volume_by_period(db, "month", 15),
                      races=analytics.race_results(db), recent=analytics.recent_activities(db, 10),
                      notices=coach.notices(svc)[:3], briefing=coach.latest_briefing(svc))

    async def login_page(request: Request) -> Response:
        if await run_in_threadpool(user_of, request):
            return RedirectResponse("/", 303)
        return render(request, "login.html", page="login", error=request.query_params.get("error"),
                      invite=request.session.get("invite"), mode="login")

    async def join_page(request: Request) -> Response:
        code = (request.query_params.get("code") or "").strip().upper()
        valid = await run_in_threadpool(accounts.invite_ok, code)
        if valid:
            request.session["invite"] = code
        return render(request, "login.html", page="login", mode="join", invite=code if valid else None,
                      bad_code=bool(code and not valid), error=request.query_params.get("error"))

    async def legal(request: Request) -> Response:
        name = request.url.path.strip("/")
        # Only a deliberately public address goes on these pages (not the admin's personal email)
        return render(request, f"{name}.html", page=name, contact=os.getenv("CONTACT_EMAIL", ""))

    @public_post
    async def api_waitlist(request: Request) -> Response:
        data = await body(request)
        email = str(data.get("email", "")).strip().lower()
        if "@" not in email or len(email) > 200:
            return JSONResponse({"error": "That email doesn't look right."}, 400)
        if not waitlist_limit.ok(ip(request)):
            return JSONResponse({"error": "Too many tries. Try again later."}, 429)
        await run_in_threadpool(accounts.join_waitlist, email, str(data.get("sport", "")))
        return JSONResponse({"ok": True})

    @public_post
    async def api_email_link(request: Request) -> Response:
        """Send a one-time sign-in link: to existing users, or to new ones holding a valid invite."""
        data = await body(request)
        email = str(data.get("email", "")).strip().lower()
        invite = (str(data.get("invite") or request.session.get("invite") or "")).strip().upper() or None
        if "@" not in email or len(email) > 200:
            return JSONResponse({"error": "That email doesn't look right."}, 400)
        if not (email_limit.ok(email) and ip_limit.ok(ip(request))):
            return JSONResponse({"error": "Too many sign-in emails. Wait a bit and try again."}, 429)
        existing = await run_in_threadpool(accounts.by, "email", email)
        if not existing and settings.invite_only and not await run_in_threadpool(accounts.invite_ok, invite):
            return JSONResponse({"needs_invite": True})
        token = await run_in_threadpool(accounts.new_login_link, email, None if existing else invite)
        link = f"{base_url(settings)}/auth/link?t={token}"
        sent = await run_in_threadpool(mailer.send_link, email, link, not existing)
        return JSONResponse({"sent": True, "delivered": sent})

    async def auth_link(request: Request) -> Response:
        row = await run_in_threadpool(accounts.use_login_link, request.query_params.get("t", ""))
        if not row:
            return RedirectResponse("/login?error=link", 303)
        user = await run_in_threadpool(accounts.by, "email", row["email"])
        if not user:
            if settings.invite_only and not accounts.invite_ok(row["invite_code"]):
                return RedirectResponse("/join?error=invite", 303)
            user = await run_in_threadpool(accounts.create, row["email"])
            if row["invite_code"]:
                accounts.use_invite(row["invite_code"])
        sign_in(request, user)
        return RedirectResponse("/", 303)

    async def auth_google(request: Request) -> Response:
        if not google.enabled:
            return RedirectResponse("/login?error=google", 303)
        state = secrets.token_urlsafe(24)
        request.session["oauth_state"] = state
        return RedirectResponse(google.auth_url(state), 303)

    async def auth_google_callback(request: Request) -> Response:
        state = request.session.pop("oauth_state", None)
        if not state or request.query_params.get("state") != state or "code" not in request.query_params:
            return RedirectResponse("/login?error=google", 303)
        try:
            info = await run_in_threadpool(google.user_for_code, request.query_params["code"])
        except Exception:
            log.exception("Google sign-in failed")
            return RedirectResponse("/login?error=google", 303)
        if not info.get("email") or not info.get("email_verified"):
            return RedirectResponse("/login?error=google", 303)
        user = accounts.by("google_sub", info["sub"]) or accounts.by("email", info["email"])
        if not user:
            invite = request.session.get("invite")
            if settings.invite_only and not accounts.invite_ok(invite):
                return RedirectResponse("/join?error=invite", 303)
            user = await run_in_threadpool(accounts.create, info["email"], info.get("name"), info["sub"])
            if invite:
                accounts.use_invite(invite)
        elif not user.get("google_sub"):
            accounts.update(user["id"], google_sub=info["sub"])
        sign_in(request, user)
        return RedirectResponse("/", 303)

    async def logout(request: Request) -> Response:
        request.session.clear()
        return RedirectResponse("/", 303)

    async def healthz(request: Request) -> Response:
        return JSONResponse({"ok": True})

    async def static(request: Request) -> Response:
        name = request.url.path.strip("/")
        if name == "manifest.webmanifest":
            return JSONResponse({
                "name": settings.brand_name, "short_name": settings.brand_name, "start_url": "/", "display": "standalone",
                "background_color": "#08090b", "theme_color": "#08090b",
                "icons": [{"src": "/icon-192.png", "sizes": "192x192", "type": "image/png"},
                          {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png"},
                          {"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any"}],
            }, media_type="application/manifest+json")
        path = HERE / "static" / name
        if not path.is_file():
            return PlainTextResponse("not found", 404)
        return FileResponse(path, headers={"Cache-Control": "public, max-age=86400"})

    # -------------------------------------------------------- onboarding
    @page(onboarding=True)
    async def welcome(request: Request) -> Response:
        return render(request, "welcome.html", page="welcome", sports=SPORTS)

    @api()
    async def api_profile(request: Request) -> Response:
        data, user = await body(request), request.state.user
        name = str(data.get("name", "")).strip()[:60]
        focus = str(data.get("focus", "")).strip().lower()
        tz = str(data.get("timezone", "")).strip()[:60]
        if not name or focus not in SPORTS:
            return JSONResponse({"error": "Add your first name and pick a sport."}, 400)
        with contextlib.suppress(Exception):
            from zoneinfo import ZoneInfo

            ZoneInfo(tz)
            accounts.update(user["id"], timezone=tz)
        accounts.update(user["id"], name=name, focus=None if focus == "other" else focus, onboarded=1)
        return JSONResponse({"ok": True})

    @page()
    async def settings_page(request: Request) -> Response:
        user, svc = request.state.user, svc_of(request)
        budget = svc.budget
        return render(request, "settings.html", page="settings", sports=SPORTS, mcp_url=f"{base_url(settings)}/mcp/{user['mcp_token']}",
                      sync_url=f"{base_url(settings)}/api/sync?token={user['sync_token']}",
                      allowance=budget.fraction_left() if budget else None, model=svc.settings.coach_model)

    @api()
    async def api_garmin_disconnect(request: Request) -> Response:
        accounts.forget_garmin(request.state.user["id"])
        tenants.reset_garmin(request.state.user["id"])
        return JSONResponse({"ok": True})

    @api()
    async def api_delete_account(request: Request) -> Response:
        data, user = await body(request), request.state.user
        if str(data.get("confirm", "")).strip().lower() != user["email"]:
            return JSONResponse({"error": "Type your email address to confirm."}, 400)
        await run_in_threadpool(tenants.delete_user, user["id"])
        request.session.clear()
        return JSONResponse({"ok": True})

    @api()
    async def api_logout_everywhere(request: Request) -> Response:
        user = request.state.user
        accounts.update(user["id"], session_version=user["session_version"] + 1)
        request.session.clear()
        return JSONResponse({"ok": True})

    # ---------------------------------------------------------- dashboard
    @page()
    async def swim(request: Request) -> Response:
        db = svc_of(request).db
        return render(request, "swim.html", page="swim", trend=analytics.swim_trend(db, 15), best=analytics.swim_best_efforts(db),
                      recent=analytics.recent_activities(db, 60, "swim"))

    @page()
    async def activities(request: Request) -> Response:
        try:
            days = max(1, min(int(request.query_params.get("days", "90")), 3650))
        except ValueError:
            days = 90
        sport = request.query_params.get("sport") or None
        return render(request, "activities.html", page="activities", rows=analytics.recent_activities(svc_of(request).db, days, sport),
                      days=days, sport=sport)

    @page()
    async def activity(request: Request) -> Response:
        detail = analytics.activity_detail(svc_of(request).db, int(request.path_params["aid"]))
        if not detail:
            return PlainTextResponse("not found", 404)
        return render(request, "activity.html", page="activities", a=detail)

    @page()
    async def workouts(request: Request) -> Response:
        return render(request, "workouts.html", page="workouts", rows=svc_of(request).planner.list())

    @page()
    async def wellness_page(request: Request) -> Response:
        return render(request, "wellness.html", page="wellness", rows=analytics.wellness_trend(svc_of(request).db, 120))

    @page()
    async def coach_page(request: Request) -> Response:
        svc = svc_of(request)
        return render(request, "coach.html", page="coach", notices=coach.notices(svc), briefing=coach.latest_briefing(svc),
                      history=coach.transcript(svc, coach.current_thread(svc)), enabled=coach.enabled(svc),
                      allowance=svc.budget.fraction_left() if svc.budget else None)

    @api()
    async def api_coach_chat(request: Request) -> Response:
        text = str((await body(request)).get("message", "")).strip()[:8000]
        if not text:
            return JSONResponse({"error": "empty message"}, 400)
        return StreamingResponse(coach.chat(svc_of(request), text), media_type="application/x-ndjson",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    @api()
    async def api_coach_new(request: Request) -> Response:
        return JSONResponse({"thread": coach.new_thread(svc_of(request))})

    @api()
    async def api_coach_briefing(request: Request) -> Response:
        svc = svc_of(request)
        if not coach.enabled(svc):
            return JSONResponse({"error": "The coach isn't switched on yet."}, 503)
        if svc.budget and svc.budget.remaining() <= 0:
            return JSONResponse({"error": coach.OUT_OF_ALLOWANCE}, 429)
        try:
            return JSONResponse(await run_in_threadpool(coach.generate_briefing, svc))
        except Exception:
            log.exception("briefing failed")
            return JSONResponse({"error": "Couldn't write the briefing. Try again in a minute."}, 502)

    @page()
    async def connect_page(request: Request) -> Response:
        return render(request, "connect.html", page="connect")

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

    def connected(request: Request, result: dict[str, Any]) -> JSONResponse:
        user = accounts.get(request.state.user["id"])
        threading.Thread(target=scheduler.run_sync, args=(tenants.services(user), "connect"), daemon=True).start()
        return JSONResponse(result)

    @api()
    async def api_garmin_login(request: Request) -> Response:
        data = await body(request)
        email, password = str(data.get("email", "")).strip(), str(data.get("password", ""))
        if not email or not password:
            return JSONResponse({"error": "Enter your Garmin email and password."}, 400)
        if not ip_limit.ok("garmin:" + request.state.user["id"]):
            return JSONResponse({"error": "Too many tries. Wait a bit and try again."}, 429)
        try:
            result = await run_in_threadpool(tenants.web_login(request.state.user).start, email, password)
        except Exception as exc:  # noqa: BLE001 - mapped to a plain message
            return garmin_error(exc)
        return JSONResponse(result) if result["needs_code"] else connected(request, result)

    @api()
    async def api_garmin_code(request: Request) -> Response:
        code = str((await body(request)).get("code", "")).strip()
        if not code:
            return JSONResponse({"error": "Enter the code Garmin sent you."}, 400)
        try:
            return connected(request, await run_in_threadpool(tenants.web_login(request.state.user).code, code))
        except Exception as exc:  # noqa: BLE001 - mapped to a plain message
            return garmin_error(exc)

    @api()
    async def api_workout_push(request: Request) -> Response:
        when = str((await body(request)).get("date", "")).strip() or None
        try:
            w = await run_in_threadpool(svc_of(request).planner.push, request.path_params["wid"], when)
        except Exception as exc:  # noqa: BLE001 - shown on the button
            log.exception("push failed")
            return JSONResponse({"error": f"Couldn't send it to Garmin: {exc}"}, 400)
        return JSONResponse({"status": w["status"], "scheduled_date": w.get("scheduled_date")})

    @api()
    async def api_volume(request: Request) -> Response:
        period = request.query_params.get("period", "week")
        count = int(request.query_params.get("count", "26"))
        return JSONResponse(analytics.volume_by_period(svc_of(request).db, period, count))

    @api()
    async def api_swim(request: Request) -> Response:
        return JSONResponse(analytics.swim_trend(svc_of(request).db, int(request.query_params.get("months", "12"))))

    async def api_sync(request: Request) -> Response:
        """Sync now: from the dashboard (signed in), or the per-user webhook link (?token=...)."""
        token = request.query_params.get("token") or request.headers.get("x-sync-token", "")
        user = accounts.by("sync_token", token) if token else None
        if not user:
            if request.headers.get("x-requested-with") != "tricoach":
                return PlainTextResponse("unauthorized", 401)
            user = await run_in_threadpool(user_of, request)
        if not user:
            return PlainTextResponse("unauthorized", 401)
        kind = "webhook-full" if request.query_params.get("full") == "1" else "webhook"
        threading.Thread(target=scheduler.run_sync, args=(tenants.services(user), kind), daemon=True).start()
        return JSONResponse({"started": True})

    # -------------------------------------------------------------- admin
    @page(admin=True)
    async def admin_page(request: Request) -> Response:
        usage = {r["user_id"]: r for r in accounts.usage_report(30)}
        users = []
        for u in accounts.list():
            chat, brief = usage.get(u["id"], {}), usage.get(f"{u['id']}#briefing", {})
            users.append({**u, "chat_usd": (chat.get("cost_micro") or 0) / 1e6, "brief_usd": (brief.get("cost_micro") or 0) / 1e6,
                          "requests": chat.get("requests") or 0, "today_usd": accounts.spent_today(u["id"]),
                          "cap_usd": accounts.daily_cap(u)})
        return render(request, "admin.html", page="admin", users=users, waitlist=accounts.waitlist(), invites=accounts.invites(),
                      email_enabled=mailer.enabled, base=base_url(settings))

    @api(admin=True)
    async def api_admin_invite(request: Request) -> Response:
        data = await body(request)
        email = str(data.get("email", "")).strip().lower() or None
        code = accounts.create_invite(note=email or str(data.get("note", ""))[:80] or None, max_uses=int(data.get("max_uses", 1) or 1))
        link = f"{base_url(settings)}/join?code={code}"
        delivered = False
        if email:
            accounts.mark_invited(email, code)
            token = accounts.new_login_link(email, code, minutes=60 * 24 * 3)
            delivered = await run_in_threadpool(mailer.send_link, email, f"{base_url(settings)}/auth/link?t={token}", True)
        return JSONResponse({"code": code, "link": link, "emailed": delivered})

    @api(admin=True)
    async def api_admin_tier(request: Request) -> Response:
        data = await body(request)
        tier = data.get("tier")
        if tier not in ("free", "pro") or not accounts.get(str(data.get("user_id"))):
            return JSONResponse({"error": "bad request"}, 400)
        accounts.update(str(data["user_id"]), tier=tier)
        return JSONResponse({"ok": True})

    @api(admin=True)
    async def api_admin_login_link(request: Request) -> Response:
        """A one-time sign-in link to hand someone directly (when email isn't set up yet)."""
        email = str((await body(request)).get("email", "")).strip().lower()
        if not accounts.by("email", email):
            return JSONResponse({"error": "No account with that email. Send an invite instead."}, 400)
        token = accounts.new_login_link(email, minutes=60 * 24)
        return JSONResponse({"link": f"{base_url(settings)}/auth/link?t={token}"})

    # ---------------------------------------------------------------- mcp
    mcp = build_mcp(lambda: _mcp_user.get())
    host = settings.public_host
    mcp_app = mcp.streamable_http_app(
        streamable_http_path="/", stateless_http=True, json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[host, f"{host}:*", "localhost:*", "127.0.0.1:*", "app:*", "app"],
            allowed_origins=[f"https://{host}", "https://claude.ai", "http://localhost:*"],
        ),
    )

    stop = threading.Event()

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette):
        async with mcp_app.router.lifespan_context(mcp_app):
            if run_scheduler:
                scheduler.start(tenants, stop)
            yield
            stop.set()

    site = Starlette(routes=[
        Route("/", home), Route("/login", login_page), Route("/join", join_page),
        Route("/privacy", legal), Route("/terms", legal),
        Route("/api/waitlist", api_waitlist, methods=["POST"]), Route("/auth/email", api_email_link, methods=["POST"]),
        Route("/auth/link", auth_link), Route("/auth/google", auth_google), Route("/auth/google/callback", auth_google_callback),
        Route("/logout", logout, methods=["GET", "POST"]), Route("/healthz", healthz),
        *[Route(f"/{n}", static) for n in ("manifest.webmanifest", "icon.svg", "icon-192.png", "icon-512.png", "apple-touch-icon.png")],
        Route("/welcome", welcome), Route("/api/profile", api_profile, methods=["POST"]),
        Route("/settings", settings_page), Route("/api/garmin/disconnect", api_garmin_disconnect, methods=["POST"]),
        Route("/api/account/delete", api_delete_account, methods=["POST"]),
        Route("/api/account/logout-everywhere", api_logout_everywhere, methods=["POST"]),
        Route("/swim", swim), Route("/activities", activities), Route("/activities/{aid:int}", activity),
        Route("/workouts", workouts), Route("/wellness", wellness_page), Route("/coach", coach_page),
        Route("/api/coach/chat", api_coach_chat, methods=["POST"]), Route("/api/coach/new", api_coach_new, methods=["POST"]),
        Route("/api/coach/briefing", api_coach_briefing, methods=["POST"]),
        Route("/connect", connect_page), Route("/api/garmin/login", api_garmin_login, methods=["POST"]),
        Route("/api/garmin/code", api_garmin_code, methods=["POST"]),
        Route("/api/workouts/{wid}/push", api_workout_push, methods=["POST"]),
        Route("/api/volume", api_volume), Route("/api/swim", api_swim),
        Route("/api/sync", api_sync, methods=["GET", "POST"]),
        Route("/admin", admin_page), Route("/api/admin/invite", api_admin_invite, methods=["POST"]),
        Route("/api/admin/tier", api_admin_tier, methods=["POST"]),
        Route("/api/admin/login-link", api_admin_login_link, methods=["POST"]),
    ], lifespan=lifespan)

    secret = settings.secret_key
    if not secret:
        log.warning("SECRET_KEY not set: using a random one, so everyone is signed out on restart")
        secret = secrets.token_hex(32)
    local = host.startswith(("localhost", "127.0.0.1")) or host.endswith(".test")
    site_with_sessions = SessionMiddleware(site, secret_key=secret, session_cookie="tc_session", max_age=60 * 86400,
                                           same_site="lax", https_only=not local)
    return Gate(tenants, site_with_sessions, mcp_app)


def main() -> None:
    import uvicorn

    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    uvicorn.run(create_app(), host="0.0.0.0", port=int(os.getenv("PORT", "8000")), proxy_headers=True,
                forwarded_allow_ips="*")
