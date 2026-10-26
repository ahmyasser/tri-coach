"""Wires settings, database, Garmin client, sync and planner together."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Callable

from .config import Settings, get_settings
from .db import Database
from .garmin import GarminAPI, Throttled, connect
from .planner import Planner
from .sync import Syncer


@dataclass
class Services:
    settings: Settings
    db: Database
    api_factory: Callable[[], GarminAPI]
    # Builds the Anthropic client for the coach page; None means "use ANTHROPIC_API_KEY" (tests inject a fake)
    llm_factory: Callable[[], Any] | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def planner(self) -> Planner:
        return Planner(self.db, self.api_factory)

    def syncer(self) -> Syncer:
        return Syncer(self.db, self.api_factory(), resync_days=self.settings.resync_days,
                      wellness_backfill_days=self.settings.wellness_backfill_days)


def build_services(settings: Settings | None = None, api_factory: Callable[[], GarminAPI] | None = None) -> Services:
    settings = settings or get_settings()
    db = Database(settings.database_url)
    db.init_schema()
    if api_factory is None:
        cache: dict[str, Any] = {}
        lock = threading.Lock()

        def api_factory() -> GarminAPI:
            with lock:
                if "api" not in cache:
                    cache["api"] = Throttled(connect(settings.garmin_tokenstore), settings.request_pause_ms)
                return cache["api"]

    return Services(settings=settings, db=db, api_factory=api_factory)
