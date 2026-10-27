# Tri Coach

Your Garmin training history in your own database, synced every day, with:

- a **dashboard** (volume by sport, swim pace trends, races, wellness), and
- an **MCP server**, so Claude can analyse the data with you and put the workouts you agree on straight onto your **Garmin calendar** (they sync to the watch, like TrainingPeaks does).

```
Garmin Connect ──(daily sync / webhook)──▶ tricoach app ──▶ Postgres
                                              │
                    https://HOST/  ◀──────────┤  dashboard (basic auth)
                    https://HOST/mcp/<token> ◀┘  MCP: Claude reads data, drafts + pushes workouts
```

It runs as three containers: Postgres, the app (dashboard, MCP, daily scheduler) and Caddy for HTTPS.

## What gets stored

| Table | What |
|---|---|
| `activities` | Every activity: sport, distance, time, pace/speed, HR, power, cadence, training load and effect, HR zones, pool length, SWOLF, strokes, plus the full raw Garmin JSON. Multisport races are stored with each leg as its own row. |
| `laps` | Laps and intervals, including every swim rep with its stroke, time and strokes per length, and the rests between reps. |
| `wellness` | Daily resting HR, overnight HRV and its status, sleep duration and score. |
| `workouts` | Planned workouts: drafts, and the ones already pushed to Garmin. |
| `coach_notes`, `athlete_settings` | What you and Claude decide (goals, test results, CSS, availability), so the next chat starts with that context. |
| `sync_runs` | History of each sync. |

The app checks the data while it saves it. For example, a pool swim recorded with the wrong pool length (your 20 Aug swim was logged as 50 m when the pool was 25 m) gets flagged. Its distance is corrected in volume totals, and it's left out of the pace statistics.

## Deploy on a VPS

You need a Linux server with Docker, and a domain (or subdomain) whose DNS A record points at it. The smallest Hetzner, DigitalOcean or Lightsail server is enough.

```bash
git clone git@github.com:<you>/tri-coach.git && cd tri-coach
cp .env.example .env
# edit .env: PUBLIC_HOST, and generate each secret with:  openssl rand -hex 24
docker compose up -d --build
```

### Connect your Garmin account (one time)

Easiest: open the dashboard and press **Connect Garmin**. It asks for the Garmin email and password (and the
code Garmin emails, if it asks for one), saves only Garmin's refresh token, and starts the history import.
From a terminal instead:

```bash
docker compose run --rm app tricoach login
```

This asks for your Garmin email and password, and your MFA code if you use one. The password isn't stored. Only Garmin's refresh token is saved, in the `appdata` volume, and it renews itself. Then pull your whole history:

```bash
docker compose exec app tricoach backfill     # roughly 5-15 minutes; it pauses when Garmin rate-limits
docker compose exec app tricoach status
```

After that the app syncs every day at `SYNC_TIME` (05:30 Cairo time by default). Each daily sync re-checks the last few days, to catch late uploads and edits.

### Sync on demand (the "webhook")

The library this app uses has no Garmin push notifications. Instead, you can trigger a sync whenever you like:

```
https://PUBLIC_HOST/api/sync?token=<SYNC_WEBHOOK_TOKEN>
```

Save that link as a bookmark, or as an iOS Shortcut or Android tile you tap after a workout. You can also ask Claude to sync (`sync_now`).

## Accounts (multi-user)

One deployment serves many athletes. Each account's training data lives in its own PostgreSQL schema (`u_<id>`),
with a read-only role per schema so the coach's ad-hoc SQL can't reach anyone else's data. Shared tables hold
accounts, invites, the waitlist, sign-in links and AI usage.

- **Sign-in:** Google, or a one-time email link (Resend). No passwords.
- **Beta:** `INVITE_ONLY=1` means new accounts need an invite; everyone else can join the waitlist on the landing page.
  Invite from `/admin` (or `tricoach invite`); without email configured, `/admin` gives you links to send by hand.
- **AI limits:** each account gets a daily allowance in USD, counted from token usage (`FREE_*` / `PRO_*`).
  Set someone to Pro on `/admin` or with `tricoach set-tier EMAIL pro`.
- **Garmin:** connected per account from the browser; tokens are encrypted with `TOKEN_KEY`.
- **Admin CLI:** `tricoach users | invite | login-link EMAIL | set-tier EMAIL pro | sync EMAIL | status EMAIL`.
- **Moving an old single-user install in:** `tricoach import-legacy --email you@x.com --name You --admin --tokens /data/garmin-tokens/garmin_tokens.json`
  (moves this database's tables into the new account) or `tricoach import-db --email ... --from-url postgresql://...`.

## Coach page

`/coach` is a chat with an AI coach that reads your training data (activities, swim reps, sleep, HRV) through the same
tools as the MCP connector, can save notes and settings, and can draft workouts. It never pushes to Garmin; approve
drafts through the connector. The page also shows a daily briefing, written after each morning sync, and rule-based
notices (load spikes, HRV or resting HR drifting, short sleep, gaps in a sport, stale sync) that need no API key.

Set `ANTHROPIC_API_KEY` in `.env` to turn on the chat and briefings. It uses `claude-opus-5-5` (`COACH_MODEL`) with
prompt caching; a typical question costs a few cents, a briefing about one.

## Use it with Claude

In claude.ai, go to **Settings → Connectors → Add custom connector** and enter:

```
https://PUBLIC_HOST/mcp/<MCP_TOKEN>
```

For Claude Code or Claude Desktop you can use `https://PUBLIC_HOST/mcp` with the header `Authorization: Bearer <MCP_TOKEN>` instead.

Tools Claude gets:

- **Analysis:** `training_overview`, `training_volume`, `list_activities`, `get_activity`, `swim_trend`, `swim_best_efforts`, `race_results`, `wellness`, `intensity_distribution`, and `run_sql`, a read-only SQL query for anything else.
- **Decisions:** `get_athlete_profile`, `set_athlete_setting`, `set_css`, `save_note`, `list_notes`.
- **Workouts:** `create_workout` creates a draft and shows a readable summary. `push_workout` uploads it and schedules it on your Garmin calendar. `update_workout`, `list_workouts`, `unpush_workout` and `delete_workout` manage them.
- **Sync:** `sync_now`.

Claude is told to always show you a draft, and to push it only after you approve.

### Workout format

```json
{
  "name": "CSS 6x200", "sport": "swim", "pool_length_m": 25,
  "steps": [
    {"kind": "warmup", "distance_m": 400, "note": "easy, build"},
    {"repeat": 4, "steps": [
      {"kind": "interval", "distance_m": 50, "stroke": "drill", "note": "catch-up"},
      {"kind": "rest", "seconds": 15}]},
    {"repeat": 6, "skip_last_rest": true, "steps": [
      {"kind": "interval", "distance_m": 200, "css_offset_s": 2},
      {"kind": "rest", "seconds": 20}]},
    {"kind": "interval", "distance_m": 300, "equipment": "pull_buoy", "pace": "1:50-1:55"},
    {"kind": "cooldown", "distance_m": 200}
  ]
}
```

- **Step types (`kind`):** warmup, interval, recovery, rest, cooldown or other.
- **How a step ends:** after `distance_m`, after `seconds`, or when you press the lap button (`lap_button`, or set none of the three).
- **Swim options:** `stroke` (free, back, breast, fly, im, drill, any, mixed), `equipment` (fins, kickboard, paddles, pull_buoy, snorkel), and `css_offset_s`, which sets a target relative to your CSS. Garmin's swim workouts support CSS targets natively. Save your CSS with `set_css` after a 400/200 test, and set it on the watch as well.
- **Targets, at most one per step:** `pace` (per 100 m for swims, per km for runs), `hr_zone`, `hr_bpm`, `power_zone` or `power_w`. You can add a `cadence` range on top.
- **Notes:** `note` text shows on the watch during that step.

## Dashboard

Open `https://PUBLIC_HOST/` and sign in with `DASHBOARD_USER` / `DASHBOARD_PASSWORD`. It has five pages: Overview, Swim, Activities, Workouts and Wellness.

## Run it locally / tests

```bash
pip install -e '.[dev]'
python -m unittest discover -s tests      # or: pytest
DATABASE_URL=sqlite:///data/tricoach.db DASHBOARD_PASSWORD=x MCP_TOKEN=x tricoach serve
```

The tests replay realistic Garmin responses: pool swims in 25 m and 50 m pools, a mis-set pool length, a 70.3 race with its legs, sleep and HRV. They cover sync, the analysis, building workouts, pushing and removing workouts, the MCP tools over HTTP, and dashboard and webhook auth.

## Caveats

- **Unofficial API.** This uses [`python-garminconnect`](https://github.com/cyberjunky/python-garminconnect), the same sign-in flow as Garmin's mobile app, for personal use. Garmin can change things without notice. If a sync fails, `docker compose logs app` shows why. Usually the fix is `pip install -U garminconnect`, a rebuild, or another `tricoach login`. All Garmin calls are in `tricoach/garmin.py`, so moving to Garmin's official Health, Activity and Training APIs (real webhooks) later only means replacing that one file.
- **Workout format was tested on 26 Oct 2026.** A sample swim workout was uploaded to your Garmin account, then deleted. Garmin accepted and kept every field: pool length, stroke types, drill, pull buoy, CSS-offset target, pace target, fixed rests and the repeat that skips its last rest. Still check the first workout that reaches your watch.
- **Security.** The MCP URL token and the Garmin token file both give access to your data, so treat them like passwords. To revoke the Garmin session, use Garmin's account security settings.
- **Backups.** Run `docker compose exec db pg_dump -U tricoach tricoach | gzip > backup-$(date +%F).sql.gz` from a nightly cron job.
