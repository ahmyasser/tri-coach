"""Command line: serve, admin tasks (invites, sign-in links, tiers), per-user sync, and moving old single-user data in."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

from .config import get_settings

USER_TABLES = ["activities", "laps", "wellness", "workouts", "coach_notes", "athlete_settings", "sync_runs",
               "coach_messages", "coach_briefings"]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="tricoach")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve", help="run the web app, MCP and the daily scheduler")
    sub.add_parser("users", help="list accounts")
    inv = sub.add_parser("invite", help="create an invite code")
    inv.add_argument("--note")
    inv.add_argument("--uses", type=int, default=1)
    ll = sub.add_parser("login-link", help="one-time sign-in link for an existing account (valid 24 h)")
    ll.add_argument("email")
    tier = sub.add_parser("set-tier", help="free or pro")
    tier.add_argument("email")
    tier.add_argument("tier", choices=["free", "pro"])
    for name in ("sync", "backfill", "status"):
        sp = sub.add_parser(name, help=f"{name} for one user")
        sp.add_argument("email")
    for name, help_ in (("import-legacy", "move this database's old single-user tables into a new account (PostgreSQL)"),
                        ("import-db", "copy another single-user database into a new account")):
        sp = sub.add_parser(name, help=help_)
        sp.add_argument("--email", required=True)
        sp.add_argument("--name")
        sp.add_argument("--focus")
        sp.add_argument("--admin", action="store_true")
        sp.add_argument("--tier", choices=["free", "pro"], default="free")
        sp.add_argument("--tokens", help="old garmin_tokens.json to carry over")
        if name == "import-db":
            sp.add_argument("--from-url", required=True)
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.cmd == "serve":
        from .web import main as serve

        serve()
        return 0

    from .accounts import Accounts, build_tenants
    from .auth import base_url

    tenants = build_tenants()
    accounts, settings = tenants.accounts, tenants.settings

    def need(email: str) -> dict[str, Any]:
        user = accounts.by("email", email)
        if not user:
            sys.exit(f"No account for {email}")
        return user

    if args.cmd == "users":
        for u in accounts.list():
            print(f"{u['email']:<36} {u['name'] or '-':<14} {u['tier']:<5} garmin={'yes' if u['garmin_tokens'] else 'no'} "
                  f"admin={'yes' if accounts.is_admin(u) else 'no'} schema={Accounts.schema(u)}")
    elif args.cmd == "invite":
        code = accounts.create_invite(args.note, args.uses)
        print(f"{code}  {base_url(settings)}/join?code={code}")
    elif args.cmd == "login-link":
        need(args.email)
        print(f"{base_url(settings)}/auth/link?t={accounts.new_login_link(args.email, minutes=60 * 24)}")
    elif args.cmd == "set-tier":
        accounts.update(need(args.email)["id"], tier=args.tier)
        print(f"{args.email}: {args.tier}")
    elif args.cmd in ("sync", "backfill"):
        svc = tenants.services(need(args.email))
        print(json.dumps(svc.syncer().run(kind=args.cmd, full=args.cmd == "backfill"), indent=2))
        if svc.save_garmin_tokens:
            svc.save_garmin_tokens()
    elif args.cmd == "status":
        from . import analytics

        db = tenants.services(need(args.email)).db
        print(json.dumps({**analytics.overview(db), "load": analytics.load_status(db)}, indent=2, default=str))
    elif args.cmd in ("import-legacy", "import-db"):
        _import(tenants, args)
    return 0


def _import(tenants: Any, args: argparse.Namespace) -> None:
    from .accounts import Accounts
    from .db import Database

    accounts, settings = tenants.accounts, tenants.settings
    if accounts.by("email", args.email):
        sys.exit(f"{args.email} already has an account")
    core = accounts.db
    if args.cmd == "import-legacy":
        if core.kind != "postgres":
            sys.exit("import-legacy needs PostgreSQL")
        present = {r["table_name"] for r in core.query(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")}
        if "activities" not in present:
            sys.exit("No old single-user tables in this database")
    user = accounts.create(args.email, args.name)
    schema = Accounts.schema(user)
    accounts.update(user["id"], focus=args.focus, tier=args.tier, onboarded=1 if args.name else 0,
                    is_admin=1 if args.admin else 0)
    if args.cmd == "import-legacy":
        core.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
        for t in USER_TABLES:
            if t in present:
                core.execute(f'ALTER TABLE public."{t}" SET SCHEMA "{schema}"')
        dest = Database(settings.database_url, schema)
        dest.init_schema()  # creates anything missing, plus the read-only role and grants
    else:
        src, dest = Database(args.from_url), Database(settings.database_url, schema)
        dest.init_schema()
        for t in USER_TABLES:
            rows = src.query(f"SELECT * FROM {t}")
            if rows:
                cols = list(rows[0])
                dest.executemany(f"INSERT INTO {t} ({', '.join(cols)}) VALUES ({', '.join(':' + c for c in cols)})", rows)
            print(f"{t}: {len(rows)} rows")
    if args.tokens:
        accounts.save_garmin_tokens(user["id"], Path(args.tokens).read_text())
    n = dest.query_one("SELECT COUNT(*) AS n FROM activities")["n"]
    print(f"Created {args.email} ({schema}) with {n} activities{' and Garmin tokens' if args.tokens else ''}")


if __name__ == "__main__":
    sys.exit(main())
