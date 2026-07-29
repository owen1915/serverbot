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
owengoodman.com • Minecraft 26.2 • up 9h 4m • 4 ms • last checked
```

`public_address` is what players type, so it is what every footer shows. Players reach
`owengoodman.com` through a Minecraft SRV record — the client follows those, this bot
does not, and the apex A record points at Cloudflare, which does not carry Minecraft
traffic. So the reachability check pings `check_address` (`mc.owengoodman.com:25565`)
instead; without that split it would report the server unreachable every 15 minutes while
players were happily connected.

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

**Statistics channel** (`webhook_stats`) — the statistics the server records, all-time.
The save holds **2,358** of them and **1,166** clear the noise floor, of which each card
shows its top ten — one card per category, ten cards in all.

Minecraft writes every statistic to `world/players/stats/*.json` on its own, whether or
not a datapack declares a scoreboard objective for it, and it stores only non-zero
entries. So the set worth showing is simply the union across players — no scoreboard, no
NBT, and nothing to enable. A statistic datapack is *not* what makes a statistic get
recorded; it only puts one on an in-game scoreboard.

The Vanilla Tweaks packs in `world/datapacks` are still read, for a different purpose:
their objectives are a curated goal list, so each card can name the goals nobody has met
yet ("Warden, Elder Guardian…"). Read from the save alone, the unscored set would be
every item in the game, which is not worth printing.

One card per category — General, Blocks Mined, Mobs Killed, Killed By, Items Used, Items
Crafted, Tools Broken, Items Picked Up, Items Dropped — each an aligned table inside a
Discord ANSI code block, which is the only way to get columns that line up in a message:

```
Mob                    Leader           Kills
─────────────────────────────────────────────
Zombified Piglin       insaneff        11,299
Cow                    owen1915         1,481
Zombie                 owen1915           912
```

A 🏛️ **Hall of Fame** card ranks players by how many statistics they lead. Each card's
footer says how many statistics it is showing out of how many were recorded, so the cap
never reads as the whole picture. Set `stats_top_per_category` to `0` to show every
statistic instead — categories too large for one message then split across numbered
cards rather than being silently truncated.

### The noise floor

The save records a statistic the moment a player touches it once, so left alone the cards
fill up with "1 iron ingot picked up" and bury everything worth reading. Each category
gets the floor that suits how often it ticks:

| Category | Needs at least |
| --- | --- |
| Items Picked Up | 32 — it ticks constantly, including on items other people dropped |
| Blocks Mined / Items Dropped | 16 — a stack; below that it is incidental |
| Items Used | 10 |
| Items Crafted | 8 |
| Mobs Killed | 5 |
| Killed By / Tools Broken | 1 — deaths are rare and wearing a tool out takes real time |
| General — distance | 1 km |
| General — time | 5 minutes |
| General — everything else | 5 |

The bar is per player, not a total: a row appears once *somebody* is genuinely past it.
Nothing is dropped silently — each card's footer says how many statistics were too small
to list. `stats_noise_scale` moves every floor at once: `2.0` for only the big numbers,
`0.1` to see almost everything, `0` to turn the filter off.

**Daily channel** (`webhook_daily`) — the same categories, but counting **today only**,
and reset at **3:00 am Eastern** rather than midnight so a late-night session stays on the
day it felt like it belonged to.

- 🌞 **Today at a Glance** — every headline total added up across the whole server:
  blocks mined, placed, and mined + placed combined, items crafted and used, mobs
  killed, distance travelled, deaths, damage, trades, nights slept, raids won — plus
  who leads the most of today's statistics

Minecraft has no "blocks placed" statistic — placing a block counts as *using* its
item, filed alongside eating bread. The bot pulls placements out of `minecraft:used` by
learning which item ids are blocks from the save itself: anything the server has ever
seen mined is a block. No hardcoded registry, so new blocks in new versions just work
(a block placed but never once mined by anybody is missed until someone breaks one).
Placed and mined + placed also feed a 🧱 **Master Builder** daily award and two entries
on the single-day records ledger.
- 📅 **Today's Playtime** — who has played today, seeded from the log archive when the
  watcher starts partway through a day
- 🎯 **Today's Challenge** — live standings for the day's competition
- one card per category, counted since 03:00

Every statistic is tracked daily, not a subset: on a normal day here around **840** of
them clear the noise floor. The tables show the top ten of each category, so the glance
card is where the breadth lives — its totals are sums, so they ignore the floor entirely.
Raise `daily_top_per_category` for longer tables.

This channel never notifies: every card is edited in place, never deleted or re-posted,
and a category with nothing yet says so on its card rather than disappearing — which is
what keeps the set of messages stable. The day's recap (🌅 **Yesterday's Playtime**,
🌙 **Yesterday's Statistics**, 🎖️ **Daily Awards**, 🏁 the challenge result) goes to the
events channel as one post; without an events webhook it falls back to this channel.

**Events channel** (`webhook_events`) — the hype feed. Everything that is a *moment*
rather than a standing card lands here:

- 🎖️ the nightly recap bundle: yesterday's playtime and statistics, the **Daily
  Awards** (Most Dedicated, Menace to Wildlife, Butterfingers, Homebody, Punching Bag,
  Chatterbox…— each with a minimum so nobody wins an empty category, and flavor text
  that is deterministic on the date so a restart never re-rolls it), and the challenge
  result
- 🎯 the new **challenge of the day** (mine the most, travel the furthest, catch the
  most fish… — picked deterministically from the date)
- 📜 **daily records broken** — "new best for blocks mined in a day", with the previous
  holder named
- 🔥 **streak milestones** (3, 5, 7, 14… consecutive days) and 💔 broken streaks
- 🏁 **overtakes** — the #1 spot changing hands in all-time playtime, blocks mined, mob
  kills or advancements — and "the race is on" when #2 closes within 3%
- 💎 **diamond finds** (near-time — stats files save every few minutes)
- 🌟 **rare advancements** (How Did We Get Here?, Cover Me in Debris, …)
- 🎂 **world birthdays** (day 100, 250, 500, 750, and every full year)

Announcement state is seeded silently on first run, so switching the feature on does not
replay history as news. Without `webhook_events` these are dropped (logged once); the
other channels stay silent by design, so nothing is rerouted into them.

**Leaderboard channel additions** — four more edited-in-place cards:

- 📜 **Single-Day Records** — the all-time ledger of daily bests
- 🔥 **Playtime Streaks** — current runs (10+ minutes counts) and the longest ever
- 🕐 **Prime Time** — a 24-hour histogram of player-hours by clock hour, seeded from the
  whole log archive and accumulated live from then on
- 🗣️ **Chat Leaders** — lifetime message counts, also archive-seeded

The 👑 All-Time Hours card also gains ▲▼ movement arrows against yesterday's ranking and
an "On pace" section projecting who reaches their next playtime milestone when (needs a
few days of history before it will say anything).

**Statistics channel additions** —

- 🌟 **Player Spotlight** — a different player each day: favorite block, nemesis, most
  hunted mob, distance, trades
- 🗺️ **Advancement Race** — progress toward everything the server has discovered (the
  full vanilla list lives inside the server jar, so the honest denominator is the union
  of what anybody here has completed), plus the advancements only one player has

**Transcript flavor** — deaths in the chat channel now carry an obituary ("death #47,
first in 4d 3h"), and the status card footer shows "☠️ 3d death-free" when the server
has gone a day or more without one.

The 3:00 am boundary follows US Eastern including daylight saving. `zoneinfo` needs the
separate `tzdata` package, which a stock Windows Python does not have, so the rule is
spelled out in `eastern_offset()` instead — no extra dependency.

**Chat history channel** (`webhook_chat`) — a plain transcript of everything that happens
in game, whatever the other channels are configured to announce:

```
➡️  Joksuu_ joined the game  ·  2 online
💬  Joksuu_: anyone got spare iron
🏅  PowerRubik earned the advancement [Diamonds!]
💀  PowerRubik was blown up by Creeper
⬅️  Joksuu_ left the game  ·  on for 1h 12m, 1 online
```

Chat, joins, leaves, deaths, advancements and server start/stop — but not the server's
internal log noise, which is what `webhook_logs` is for. Lines are batched into one
message per cycle, because a busy server produces them faster than a webhook will accept
messages, and mentions are disabled so nothing players type can ping the channel.

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
| `external_check` | `true` | also ping from outside, to catch port-forwarding breaking |
| `check_address` | `""` | what that check pings; defaults to `public_address` |
| `perf_sample_seconds` | `30` | how often to read the host and process metrics |
| `perf_card_seconds` | `120` | how often to redraw the performance card |
| `perf_alerts` | `true` | post threshold alerts to the performance channel |
| `alert_host_cpu` / `alert_host_ram` | `90` / `92` | percent, sustained over five samples |
| `alert_disk_free_gb` | `15` | warn below this much free space |
| `alert_heap_pct` | `92` | warn above this share of the java heap |
| `daily_report` | `true` | post a summary of the previous day at midnight |
| `stats_card_seconds` | `900` | how often to redraw the all-time statistic cards |
| `stats_top_per_category` | `10` | cap rows per category; `0` shows every statistic |
| `daily_card_seconds` | `300` | how often to redraw today's statistic cards |
| `daily_top_per_category` | `10` | cap rows per category on the daily cards |
| `stats_noise_scale` | `1.0` | how high the bar is to earn a row; `0` shows everything |

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
rather than counted as uptime.

Also the 3:00 am Eastern day boundary across both daylight-saving changes, the noise
floor (one iron ingot hidden, one death to a warden still shown), and that the chat
transcript repeats everything even with every announcement setting turned off. No server
or network needed.

## Notes

- The bot never holds `latest.log` open. On Windows an open handle would stop the server
  from rotating its own log, so every read opens, seeks and closes.
- Log lines carry a time but no date. Timestamps are reconstructed by counting midnight
  crossings — forwards from the date in a rotated log's filename, or backwards from the
  file's modification time for `latest.log`.
- Stats files are only written when the server saves a player, so for anyone currently
  online the bot extends their last saved total with the session time seen in the log.
- Starting mid-week, the weekly board is seeded by replaying the retained log archive, so
  it is not blank until the next Monday. The daily board is seeded the same way from the
  3:00 am boundary.
- Statistic totals have no log archive to recover from, so a week's or a day's counts
  necessarily start from wherever they stand when the baseline is taken.
- `enable-query` and `rcon` are both off and are not needed — nothing here depends on them.
