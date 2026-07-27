# mcbot — Minecraft server → Discord notifier

Polls the [mcsrvstat.us](https://api.mcsrvstat.us) API for `71.176.227.214` and posts to
Discord webhooks. Python 3 standard library only — no dependencies, no bot token.

## What it posts

**Main channel** (`DISCORD_WEBHOOK_URL`):
- joins: `➡️ Name joined the server (2 online)`
- leaves with playtime: `⬅️ Name left the server (1 online) — was on for 2h 15m`
- player-count changes when names aren't available
- `@everyone 🔴 Server DOWN` after 2 consecutive failed checks; `🟢 Server UP` on recovery
- `🎉 New record! N players online at once`
- `🎖️ Name has now played over 50 hours on the server!` (10/25/50/100/250/500/1000h)
- a live status card (green/red embed with the player list and time-on), always kept
  as the newest message: edited in place on quiet checks, re-posted after events

**Weekly channel** (`MC_WEEKLY_WEBHOOK_URL`):
- `🏆 Weekly Playtime` card — hours per player, refreshed on joins/leaves, resets Monday
- `🏁 Final Standings` posted permanently every Monday for the finished week
- `👑 All-Time Hours` card — lifetime totals, never resets

**Log channel** (`MC_LOG_WEBHOOK_URL`): every log line, ~1 message per check.

State (player sessions, weekly/all-time totals, message IDs) lives in `mcbot_state.json`.
A local copy of every log line goes to `mcbot.log`.

## Running locally

Webhook URLs come only from environment variables — `mcbot.py` contains no secrets and
is safe to publish. For local runs, `run_local.sh` exports the real URLs (**never commit
that file**):

```
bash run_local.sh                 # watch forever, every 5 minutes
bash run_local.sh --once          # single check (what GitHub Actions runs)
bash run_local.sh --once --dry-run  # print instead of posting
```

## Free 24/7 hosting: GitHub Actions

`.github/workflows/watch.yml` runs `mcbot.py --once` on a schedule and commits
`mcbot_state.json` back to the repo. Setup:

1. Create a **public** GitHub repo (public = unlimited free Actions minutes; private
   repos would exceed the 2,000 free min/month at this cadence).
2. Repo → Settings → Secrets and variables → Actions → add three secrets:
   `DISCORD_WEBHOOK_URL`, `MC_WEEKLY_WEBHOOK_URL`, `MC_LOG_WEBHOOK_URL`.
3. Push `mcbot.py`, `README.md`, `mcbot_state.json`, and `.github/workflows/watch.yml`.
   **Do not push `run_local.sh`** — it contains the webhook URLs.
4. Actions tab → mc-watch → Run workflow (manual test), check the log channel.
5. Stop any locally running watcher — two watchers double-count hours.

Caveats: GitHub's cron is best-effort (runs land ~5–15 min apart), so join/leave
detection lags accordingly and a DOWN alert (2 missed checks) can take up to ~30 min.
The workflow sets `TZ=America/New_York` so the Monday reset follows US Eastern time.

## Limitations

- The API caches for 5 minutes; someone joining and leaving inside one window is missed.
  Instant notifications would need a server-side plugin (e.g. DiscordSRV) instead.
- The server has query disabled, so player names come from the ping sample (caps at ~12
  players). `enable-query=true` in `server.properties` + opening the query UDP port
  makes name lists reliable. Count-based events work regardless.
- All hour totals are "observed time" at poll granularity (±5 min per session locally,
  a bit coarser on GitHub Actions).
