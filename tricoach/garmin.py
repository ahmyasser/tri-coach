"""Garmin Connect access through the unofficial ``garminconnect`` library.

Everything Garmin-specific lives here so the rest of the app talks to a small
interface (``GarminAPI``). If you later get access to Garmin's official Health /
Activity / Training APIs, write another class with the same methods and swap it in.
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable, Protocol

log = logging.getLogger(__name__)


class GarminAPI(Protocol):
    def get_activities(self, start: int, limit: int) -> list[dict[str, Any]]: ...
    def get_activity(self, activity_id: int) -> dict[str, Any]: ...
    def get_activity_splits(self, activity_id: int) -> dict[str, Any]: ...
    def get_sleep_daily(self, start: str, end: str) -> list[dict[str, Any]]: ...
    def get_hrv_data_range(self, start: str, end: str) -> dict[str, Any] | None: ...
    def get_rhr_daily(self, start: str, end: str) -> list[dict[str, Any]]: ...
    def upload_workout(self, workout_json: dict[str, Any]) -> dict[str, Any]: ...
    def schedule_workout(self, workout_id: int, date_str: str) -> dict[str, Any]: ...
    def unschedule_workout(self, scheduled_workout_id: int) -> Any: ...
    def delete_workout(self, workout_id: int) -> Any: ...
    def get_scheduled_workouts_by_period(self, start: str, end: str) -> Any: ...


class GarminAuthMissing(RuntimeError):
    pass


def tokens_present(tokenstore: str) -> bool:
    return (Path(tokenstore).expanduser() / "garmin_tokens.json").exists()


def connect(tokenstore: str) -> Any:
    """Log in with saved tokens (created once by ``tricoach login``)."""
    from garminconnect import Garmin

    if not tokens_present(tokenstore):
        raise GarminAuthMissing(
            "No Garmin tokens yet. Run `docker compose run --rm app tricoach login` once on the server."
        )
    client = Garmin()
    client.login(tokenstore)
    return client


def connect_tokens(token_json: str) -> Any:
    """Log in with a user's saved tokens (the JSON from ``client.dumps()``)."""
    from garminconnect import Garmin

    client = Garmin()
    client.login(token_json)
    return client


def interactive_login(tokenstore: str, email: str | None = None) -> str:
    """One-time login. Asks for password (and MFA code if enabled); saves refresh tokens."""
    from getpass import getpass

    from garminconnect import Garmin

    email = email or input("Garmin email: ").strip()
    password = getpass("Garmin password (not stored): ")
    Path(tokenstore).expanduser().mkdir(parents=True, exist_ok=True, mode=0o700)
    client = Garmin(email, password, prompt_mfa=lambda: input("Garmin MFA code: ").strip())
    client.login(tokenstore)
    return client.full_name or client.display_name or email


def _new_client(email: str, password: str) -> Any:
    from garminconnect import Garmin

    return Garmin(email, password, return_on_mfa=True)


class WebLogin:
    """Two-step Garmin login for the dashboard's Connect page: password first, then the emailed code if asked.

    The password is only held for the request. A login waiting for its code keeps Garmin's session state in
    memory for 10 minutes; nothing is stored until it succeeds, and then only the tokens, handed to ``save``.
    """

    TTL_S = 600

    def __init__(self, save: Callable[[str], None]):
        self.save = save
        self._pending: tuple[Any, Any, str, float] | None = None
        self._lock = threading.Lock()

    def start(self, email: str, password: str) -> dict[str, Any]:
        client = _new_client(email, password)
        status, state = client.login()
        if status == "needs_mfa":
            with self._lock:
                self._pending = (client, state, email, time.monotonic())
            return {"needs_code": True}
        return self._finish(client, email)

    def code(self, code: str) -> dict[str, Any]:
        with self._lock:
            pending, self._pending = self._pending, None
        if not pending or time.monotonic() - pending[3] > self.TTL_S:
            raise GarminAuthMissing("That sign-in expired. Start again with your email and password.")
        client, state, email, _ = pending
        client.resume_login(state, code)
        return self._finish(client, email)

    def _finish(self, client: Any, email: str) -> dict[str, Any]:
        self.save(client.client.dumps())
        with contextlib.suppress(Exception):
            client._load_profile_and_settings()  # fills full_name; tokens are already saved
        return {"needs_code": False, "name": getattr(client, "full_name", None) or getattr(client, "display_name", None) or email}


class Throttled:
    """Wraps a Garmin client: pauses between calls and backs off on HTTP 429."""

    def __init__(self, client: Any, pause_ms: int = 400, max_retries: int = 4):
        self._client = client
        self._pause = pause_ms / 1000.0
        self._retries = max_retries

    def __getattr__(self, name: str) -> Any:
        fn = getattr(self._client, name)
        if not callable(fn):
            return fn

        def call(*args: Any, **kwargs: Any) -> Any:
            delay = 5.0
            for attempt in range(self._retries + 1):
                try:
                    result = fn(*args, **kwargs)
                    time.sleep(self._pause)
                    return result
                except Exception as exc:  # garminconnect raises its own TooManyRequests error
                    too_many = "TooManyRequests" in type(exc).__name__ or "429" in str(exc)
                    if not too_many or attempt == self._retries:
                        raise
                    log.warning("Garmin rate limit on %s, sleeping %.0fs", name, delay)
                    time.sleep(delay)
                    delay *= 2
            raise RuntimeError("unreachable")

        return call
