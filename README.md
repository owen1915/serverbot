# mcbot — Minecraft server → Discord

Runs **on the machine hosting the Minecraft server** and reads the server's own data
instead of a third-party status API. Python 3 standard library only — no dependencies,
no bot token, no plugin.

## Where the data comes from

| Source | What it gives |
| --- | --- |
| `logs/latest.log` | joins, leaves, deaths, advancements, chat, start/stop — **within a second** |
| status ping on `127.0.0.1:25565` | authoritative online count, version, MOTD, latency |
| `world/players/stats/*.json` | **real** playtime, deaths, kills, blocks mined, distance, trades … |
| `world/players/advancements/*.json` | advancement counts |
| `usercache.json` | UUID → name |

This replaces the old mcsrvstat.us polling, which had a 5-minute cache, capped the
player list at ~12 names, missed anyone who joined and left inside one window, and could
only estimate playtime by watching. Hours now come from Minecraft's own `play_time`
counter, so they include history from before this bot existed.

## What it posts

**Main channel** (`webhook_main`)
- a live status card — who is on, this session's time, their all-time hours, server
  version, uptime and ping. Edited in place on quiet checks, re-posted after events so
  it stays the newest message.
- `➡️ Name joined the server (2 online) — 16h 32m all-time`
- `⬅️ Name left the server (1 online) — was on for 2h 15m`
- `💀 Name was blown up by Creeper`
- `🎖️ Name earned the advancement [Diamonds!]`
- `🎉 New record! N players online at once`
- `🎖️ Name has now played over 50 hours on the server!`
- `@everyone 🔴 Server DOWN` after ~90s of failed pings; `🟢 Server UP` on recovery
- `⚠️ running but unreachable from the internet` if the port stops forwarding

**Leaderboard channel** (`webhook_weekly`) — three cards, all edited in place
- 🏆 **Weekly Playtime** — resets Monday, with 🏁 **Final Standings** posted permanently
- 👑 **All-Time Hours**
- 📊 **Server Statistics** — blocks mined, mob kills, deaths, distance, advancements,
  items crafted, villager trades, animals bred, fish caught, plus server-wide totals

**Log channel** (`webhook_logs`) — the watcher's own significant log lines.

## Setup

1. `cp config.example.json config.json` and fill it in. `config.json` holds the webhook
   URLs and is gitignored — **it must never be committed**.
2. `python mcbot.py --once --dry-run` to check it reads the server correctly.
3. `python mcbot.py` to run it.

Any setting can also be given as an environment variable: `MCBOT_RELAY_CHAT=1`,
`MCBOT_DOWN_MENTION=@here`, and so on.

### Options worth knowing

| Key | Default | Meaning |
| --- | --- | --- |
| `relay_chat` | `false` | mirror in-game chat into the main channel |
| `announce_deaths` | `true` | post death messages |
| `announce_advancements` | `true` | post advancement messages |
| `down_mention` | `@everyone` | who to ping when the server goes down (`""` for nobody) |
| `down_after_seconds` | `90` | failed-ping time before announcing DOWN |
| `external_check` | `true` | also ping `public_address` to catch port-forwarding breaking |

## Running as a service (Windows)

Registered as the scheduled task **mcbot**, started at logon under `pythonw.exe` so it
has no console window, and restarted automatically if it ever exits.

```powershell
Get-ScheduledTask mcbot | Get-ScheduledTaskInfo   # status
Stop-ScheduledTask  -TaskName mcbot               # stop
Start-ScheduledTask -TaskName mcbot               # start
Get-Content mcbot.log -Tail 30 -Wait              # follow the log
```

State (sessions, weekly baselines, message IDs) lives in `state.json`; the watcher's log
is `mcbot.log`, rotated at 5 MB. Both are gitignored. Deleting `state.json` is safe — the
bot rebuilds who is online by replaying `latest.log` and re-posts its cards.

## Tests

```
python test_mcbot.py
```

Covers following `latest.log` across appends, partial writes, UTF-8 chat and rotation,
and the classification of log lines (a creeper kill is a death; a `SulfurCube` dying is
not). No server or network needed.

## Notes

- The bot never holds `latest.log` open. On Windows an open handle would stop the server
  from rotating its own log, so every read opens, seeks and closes.
- Log lines carry a time but no date. Timestamps are reconstructed by counting midnight
  crossings — forwards from the date in a rotated log's filename, or backwards from the
  file's modification time for `latest.log`.
- Stats files are only written when the server saves a player, so for anyone currently
  online the bot extends their last saved total with the session time seen in the log.
- Starting mid-week, the weekly board is seeded by replaying the retained log archive, so
  it is not blank until the next Monday.
- `enable-query` and `rcon` are both off and are not needed — nothing here depends on them.
