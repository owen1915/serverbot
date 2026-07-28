# mcbot — Minecraft server → Discord

Runs **on the machine hosting the Minecraft server** and reads the server's own data
instead of a third-party status API. No bot token and no plugin; the standard library
covers everything except the host performance metrics, which use `psutil`:

```
pip install psutil
```

Without it the bot still runs — the performance card just drops the host and process
sections and reports server-side numbers only.

## Where the data comes from

| Source | What it gives |
| --- | --- |
| `logs/latest.log` | joins, leaves, deaths, advancements, chat, start/stop — **within a second** |
| status ping on `127.0.0.1:25565` | authoritative online count, version, MOTD, latency |
| `world/players/stats/*.json` | **real** playtime, deaths, kills, blocks mined, distance, trades … |
| `world/datapacks/*.zip` | which statistics the server tracks, and what to call them |
| `world/players/advancements/*.json` | advancement counts |
| `usercache.json` | UUID → name |

This replaces the old mcsrvstat.us polling, which had a 5-minute cache, capped the
player list at ~12 names, missed anyone who joined and left inside one window, and could
only estimate playtime by watching. Hours now come from Minecraft's own `play_time`
counter, so they include history from before this bot existed.

## What it posts

**Main channel** (`webhook_main`) — **one message, and only one**: a live status card
showing who is online and how long each of them has been on, plus the server version,
uptime and ping.

```
🟢  Server Online
### Players Online — 2/1000
> 🎮  Joksuu_      ·  on for 1h 12m
> 🎮  PowerRubik   ·  on for 24m
71.176.227.214:25565 • Minecraft 26.2 • up 9h 4m • 4 ms • last checked
```

The card is never re-posted — it is edited in place, across restarts and server
shutdowns alike, so the channel never accumulates duplicates. Every id this bot posts is
persisted before anything else happens, and any card it loses track of is deleted on the
next start.

Setting `main_events` to `true` restores the event stream in this channel (joins, leaves,
deaths, advancements, records, playtime milestones, and `@everyone 🔴 Server DOWN`); the
card then re-posts after each event so it stays the newest message. Outages are recorded
in the performance channel either way.

**Leaderboard channel** (`webhook_weekly`) — three cards, in this order, edited in place
- 👑 **All-Time Hours**
- 🏆 **Weekly Playtime** — resets Monday, with 🏁 **Final Standings** posted permanently
- 📈 **This Week's Statistics** — only what has moved since the week began

**Statistics channel** (`webhook_stats`) — every statistic the server tracks, all-time.

The Vanilla Tweaks statistic datapacks declare a scoreboard objective per statistic,
each bound to a vanilla criterion. Those criteria are read straight out of the datapack
zips in `world/datapacks`, so this follows whatever is installed — add or remove a pack
and the cards follow, with no list to maintain. Currently **216** distinct statistics.

One card per category (General, Blocks Mined, Mobs Killed, Killed By, Items Used, Tools
Broken, Items Crafted), each an aligned table inside a Discord ANSI code block, which is
the only way to get columns that line up in a message:

```
Mob                    Leader           Kills
─────────────────────────────────────────────
Zombified Piglin       insaneff        11,299
Cow                    owen1915         1,481
Zombie                 owen1915           912
```

Each card lists the statistics nobody has scored yet, so the untouched ones are visible
rather than merely absent, and a 🏛️ **Hall of Fame** card ranks players by how many
statistics they lead. Categories too large for one message split across numbered cards
rather than being silently truncated.

**Performance channel** (`webhook_perf`) — a live 📈 **Performance** card, edited in place

- *server* — status-ping latency with a sparkline and hourly average, and any
  "Can't keep up!" tick overruns the server has logged
- *availability* — uptime over 24h / 7d / 30d, plus the last outage and its duration.
  Time when the watcher itself was not running is recorded as **unknown** and excluded,
  so a short history cannot masquerade as a perfect month
- *Minecraft process* — CPU share of the machine, resident memory against the `-Xmx`
  heap (detected from the running process), thread count, process uptime
- *host* — CPU across all cores, RAM, free disk on the world's drive, network and disk
  throughput, and how long the PC has been up

It also posts outage records (`🔴 Outage began` / `🟢 Recovered after 12m`), a
🗓️ **Daily Report** at midnight with the previous day's uptime, peak players, average
and peak CPU/RAM and lag events, and threshold alerts for sustained high CPU, memory
pressure, low disk and heap pressure. Alerts never ping anyone and are rate-limited to
one per condition every 30 minutes.

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
| `main_events` | `false` | post events to the main channel, not just the status card |
| `relay_chat` | `false` | mirror in-game chat into the main channel |
| `announce_deaths` | `true` | post death messages (needs `main_events`) |
| `announce_advancements` | `true` | post advancement messages (needs `main_events`) |
| `down_mention` | `@everyone` | who to ping when the server goes down (`""` for nobody) |
| `down_after_seconds` | `90` | failed-ping time before announcing DOWN |
| `external_check` | `true` | also ping `public_address` to catch port-forwarding breaking |
| `perf_sample_seconds` | `30` | how often to read the host and process metrics |
| `perf_card_seconds` | `120` | how often to redraw the performance card |
| `perf_alerts` | `true` | post threshold alerts to the performance channel |
| `alert_host_cpu` / `alert_host_ram` | `90` / `92` | percent, sustained over five samples |
| `alert_disk_free_gb` | `15` | warn below this much free space |
| `alert_heap_pct` | `92` | warn above this share of the java heap |
| `daily_report` | `true` | post a summary of the previous day at midnight |
| `stats_card_seconds` | `900` | how often to redraw the all-time statistic cards |

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

Covers following `latest.log` across appends, partial writes, UTF-8 chat and rotation;
the classification of log lines (a creeper kill is a death; a `SulfurCube` dying is not);
Bedrock names carrying Floodgate's `.` prefix; both wordings of the server's lag warning;
and the availability ledger, including that a gap in sampling is reported as unknown
rather than counted as uptime. No server or network needed.

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
