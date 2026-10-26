"""Command line: `tricoach login | sync | backfill | serve | push | status`."""

from __future__ import annotations

import argparse
import json
import logging
import sys

from .config import get_settings
from .garmin import interactive_login


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="tricoach")
    sub = p.add_subparsers(dest="cmd", required=True)
    lg = sub.add_parser("login", help="one-time Garmin login; saves refresh tokens (password is never stored)")
    lg.add_argument("--email")
    sub.add_parser("sync", help="incremental sync now")
    sub.add_parser("backfill", help="re-walk the whole Garmin history")
    sub.add_parser("serve", help="run dashboard + MCP + daily scheduler")
    sub.add_parser("status", help="show what's in the database")
    pw = sub.add_parser("push", help="push a draft workout to the Garmin calendar")
    pw.add_argument("workout_id")
    pw.add_argument("date", nargs="?")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.cmd == "login":
        name = interactive_login(get_settings().garmin_tokenstore, args.email)
        print(f"Logged in as {name}. Tokens saved to {get_settings().garmin_tokenstore}")
        return 0
    if args.cmd == "serve":
        from .web import main as serve

        serve()
        return 0

    from . import analytics
    from .services import build_services

    svc = build_services()
    if args.cmd in ("sync", "backfill"):
        stats = svc.syncer().run(kind=args.cmd, full=args.cmd == "backfill")
        print(json.dumps(stats, indent=2))
    elif args.cmd == "status":
        print(json.dumps({**analytics.overview(svc.db), "load": analytics.load_status(svc.db)}, indent=2, default=str))
    elif args.cmd == "push":
        print(json.dumps(svc.planner.push(args.workout_id, args.date), indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
