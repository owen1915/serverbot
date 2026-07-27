#!/usr/bin/env python3
"""Minecraft server watcher -> Discord webhook.

Polls api.mcsrvstat.us and posts to a Discord webhook when:
  - a player joins
  - a player leaves
  - the online player count changes (even when names aren't available)
  - the server goes down or comes back up

Uses only the Python standard library. Configure via the constants below
or environment variables of the same name.

Usage:
  python mcbot.py                # run forever, polling every POLL_SECONDS
  python mcbot.py --once         # single check (for cron / Task Scheduler)
  python mcbot.py --once --dry-run   # print messages instead of posting
"""

import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.request

# ---------------------------------------------------------------- config ---
SERVER_ADDRESS = os.environ.get("MC_SERVER_ADDRESS", "71.176.227.214")
# Webhook URLs come ONLY from environment variables so this file is safe to
# publish. Locally: use run_local.sh. On GitHub Actions: repo secrets.
WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
POLL_SECONDS = int(os.environ.get("MC_POLL_SECONDS", "300"))  # API caches 5 min
# How many consecutive failed/offline checks before we announce "server down".
# Avoids a false alarm from one flaky poll.
OFFLINE_THRESHOLD = int(os.environ.get("MC_OFFLINE_THRESHOLD", "2"))
# Who to ping when the server goes down. "@everyone", "@here", or a personal
# mention like "<@236868384512345678>" (Discord: Settings > Advanced > enable
# Developer Mode, then right-click your name > Copy User ID).
DOWN_MENTION = os.environ.get("MC_DOWN_MENTION", "@everyone")
# All-time playtime marks that earn a shout-out in the main channel, in hours.
MILESTONE_HOURS = [10, 25, 50, 100, 250, 500, 1000]
# Mirror every log line to this webhook/channel (~1 message per check).
# Leave empty to disable.
LOG_WEBHOOK_URL = os.environ.get("MC_LOG_WEBHOOK_URL", "")
# Weekly hours leaderboard, posted to its own webhook/channel. Totals reset
# every Monday. Leave empty to disable.
WEEKLY_WEBHOOK_URL = os.environ.get("MC_WEEKLY_WEBHOOK_URL", "")

API_URL = f"https://api.mcsrvstat.us/3/{SERVER_ADDRESS}"
USER_AGENT = "mcbot-status-watcher/1.0 (owengoodman3@gmail.com)"
_HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(_HERE, "mcbot_state.json")
LOG_FILE = os.path.join(_HERE, "mcbot.log")
# ---------------------------------------------------------------------------


_log_posting = False  # guards against log() -> webhook -> log() recursion


def log(message):
    """Print a timestamped line, append it to mcbot.log, mirror it to Discord."""
    global _log_posting
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except OSError as e:
        print(f"[warn] could not write log file: {e}", file=sys.stderr)
    if LOG_WEBHOOK_URL and not _log_posting:
        _log_posting = True
        try:
            _webhook_request(
                LOG_WEBHOOK_URL,
                "POST",
                {
                    "content": f"`{line}`",
                    "username": "MC Watch Logs",
                    "allowed_mentions": {"parse": []},  # log text can never ping anyone
                },
            )
        except Exception as e:
            print(f"[warn] log webhook post failed: {e}", file=sys.stderr)
        finally:
            _log_posting = False


def fetch_status():
    """Return the parsed API response, or None if the API itself is unreachable."""
    req = urllib.request.Request(API_URL, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        log(f"[warn] API request failed: {e}")
        return None


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"online": None, "players": [], "count": 0, "fail_streak": 0}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def _webhook_request(url, method="POST", payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read()
        return json.loads(raw) if raw else None


def post_discord(content, dry_run=False):
    if dry_run:
        log(f"[dry-run] {content}")
        return
    try:
        _webhook_request(
            WEBHOOK_URL,
            "POST",
            {
                "content": content,
                "username": "MC Server Watch",
                # Without this, webhook @everyone/user mentions may not actually ping.
                "allowed_mentions": {"parse": ["everyone", "users", "roles"]},
            },
        )
        log(f"[posted] {content}")
    except urllib.error.URLError as e:
        log(f"[error] Discord webhook post failed: {e}")


def fmt_duration(seconds):
    m = int(seconds) // 60
    if m < 1:
        return "just joined"
    h, m = divmod(m, 60)
    d, h = divmod(h, 24)
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m}m"
    return f"{m}m"


def week_key(epoch):
    """ISO year-week string, weeks starting Monday — e.g. '2026-W31'."""
    y, w, _ = dt.date.fromtimestamp(epoch).isocalendar()
    return f"{y}-W{w:02d}"


def week_start_label(epoch):
    d = dt.date.fromtimestamp(epoch)
    monday = d - dt.timedelta(days=d.isoweekday() - 1)
    return monday.strftime("Week of %b %d")


def build_weekly_embed(state, epoch, final=False):
    totals = sorted(state.get("weekly_seconds", {}).items(), key=lambda kv: -kv[1])
    if totals:
        medals = ["🥇", "🥈", "🥉"]
        lines = [
            f"> {medals[i] if i < 3 else f'`{i + 1}.`'}  **{name}** — {fmt_duration(secs)}"
            for i, (name, secs) in enumerate(totals)
        ]
        body = "\n".join(lines)
    else:
        body = "> 💤  *No playtime recorded yet*"
    label = week_start_label(epoch - 7 * 86400 if final else epoch)
    return {
        "title": "🏁  Final Standings" if final else "🏆  Weekly Playtime",
        "description": f"### {label}\n{body}",
        "color": 0xF1C40F,  # gold
        "footer": {"text": "resets every Monday  •  updated"},
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z",
    }


def refresh_weekly_message(state, epoch, dry_run=False):
    """Edit the leaderboard embed in place; post it if it doesn't exist yet."""
    if dry_run:
        log(f"[dry-run] weekly leaderboard refresh: {state.get('weekly_seconds')}")
        return
    embed = build_weekly_embed(state, epoch)
    mid = state.get("weekly_msg_id")
    if mid:
        try:
            _webhook_request(f"{WEEKLY_WEBHOOK_URL}/messages/{mid}", "PATCH", {"embeds": [embed]})
            log("[weekly] updated leaderboard")
            return
        except urllib.error.HTTPError as e:
            if e.code != 404:
                log(f"[warn] weekly leaderboard edit failed: {e}")
                return
            # 404: someone deleted it — fall through and post a new one.
        except urllib.error.URLError as e:
            log(f"[warn] weekly leaderboard edit failed: {e}")
            return
    try:
        resp = _webhook_request(
            WEEKLY_WEBHOOK_URL + "?wait=true",
            "POST",
            {"username": "MC Weekly Hours", "embeds": [embed]},
        )
        state["weekly_msg_id"] = resp["id"]
        log("[weekly] posted leaderboard")
    except urllib.error.URLError as e:
        log(f"[error] weekly leaderboard post failed: {e}")


def build_alltime_embed(state):
    totals = sorted(state.get("alltime_seconds", {}).items(), key=lambda kv: -kv[1])
    shown = totals[:20]
    if shown:
        medals = ["🥇", "🥈", "🥉"]
        lines = [
            f"> {medals[i] if i < 3 else f'`{i + 1}.`'}  **{name}** — {fmt_duration(secs)}"
            for i, (name, secs) in enumerate(shown)
        ]
        if len(totals) > 20:
            lines.append(f"> *…and {len(totals) - 20} more*")
        body = "\n".join(lines)
    else:
        body = "> 💤  *No playtime recorded yet*"
    return {
        "title": "👑  All-Time Hours",
        "description": body,
        "color": 0x9B59B6,  # purple
        "footer": {"text": "lifetime totals  •  updated"},
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z",
    }


def refresh_alltime_message(state, dry_run=False, reposition=False):
    """Edit the all-time card in place; repost it (delete + post) on rollover."""
    if dry_run:
        log(f"[dry-run] all-time leaderboard refresh: {state.get('alltime_seconds')}")
        return
    embed = build_alltime_embed(state)
    mid = state.get("alltime_msg_id")
    if mid and not reposition:
        try:
            _webhook_request(f"{WEEKLY_WEBHOOK_URL}/messages/{mid}", "PATCH", {"embeds": [embed]})
            log("[weekly] updated all-time leaderboard")
            return
        except urllib.error.HTTPError as e:
            if e.code != 404:
                log(f"[warn] all-time leaderboard edit failed: {e}")
                return
        except urllib.error.URLError as e:
            log(f"[warn] all-time leaderboard edit failed: {e}")
            return
    if mid and reposition:
        try:
            _webhook_request(f"{WEEKLY_WEBHOOK_URL}/messages/{mid}", "DELETE")
        except urllib.error.HTTPError as e:
            if e.code != 404:
                log(f"[warn] could not delete old all-time card: {e}")
        except urllib.error.URLError as e:
            log(f"[warn] could not delete old all-time card: {e}")
    try:
        resp = _webhook_request(
            WEEKLY_WEBHOOK_URL + "?wait=true",
            "POST",
            {"username": "MC Weekly Hours", "embeds": [embed]},
        )
        state["alltime_msg_id"] = resp["id"]
        log("[weekly] posted all-time leaderboard")
    except urllib.error.URLError as e:
        log(f"[error] all-time leaderboard post failed: {e}")


def update_weekly(state, names, epoch, dry_run=False, refresh_now=False):
    """Accrue online time into this week's totals; roll over each Monday.

    Totals accumulate silently every check; the leaderboard card is only
    refreshed when refresh_now is set (someone joined/left), on the Monday
    rollover, or if the card doesn't exist yet.
    """
    if not WEEKLY_WEBHOOK_URL:
        return
    wk = week_key(epoch)
    rollover = False

    if state.get("week") != wk:
        rollover = True
        # New week: post last week's final standings as a permanent message,
        # then start a fresh leaderboard.
        if state.get("week") and state.get("weekly_seconds"):
            final = build_weekly_embed(state, epoch, final=True)
            if dry_run:
                log("[dry-run] weekly final standings post")
            else:
                try:
                    _webhook_request(
                        WEEKLY_WEBHOOK_URL, "POST",
                        {"username": "MC Weekly Hours", "embeds": [final]},
                    )
                    log(f"[weekly] posted final standings for {state['week']}")
                except urllib.error.URLError as e:
                    log(f"[error] weekly final standings post failed: {e}")
        state["week"] = wk
        state["weekly_seconds"] = {}
        state["weekly_msg_id"] = None
        refresh_now = True

    last = state.get("last_poll")
    if last and names:
        # Credit elapsed time since the previous check, capped so a long gap
        # (watcher was off) doesn't over-credit anyone.
        credit = min(epoch - last, POLL_SECONDS * 2)
        if credit > 0:
            totals = state.setdefault("weekly_seconds", {})
            alltime = state.setdefault("alltime_seconds", {})
            for n in names:
                totals[n] = totals.get(n, 0) + credit
                alltime[n] = alltime.get(n, 0) + credit
    state["last_poll"] = epoch

    if refresh_now or not state.get("weekly_msg_id"):
        refresh_weekly_message(state, epoch, dry_run)
    # Repost (not edit) the all-time card on rollover so the channel order stays:
    # final standings -> new weekly card -> all-time card.
    if rollover or refresh_now or not state.get("alltime_msg_id"):
        refresh_alltime_message(state, dry_run, reposition=rollover)


def build_status_embed(is_online, count, max_players, names, sessions=None):
    """A colored embed showing who's online right now and for how long."""
    now = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z"
    if not is_online:
        return {
            "title": "🔴  Server Offline",
            "description": f"`{SERVER_ADDRESS}` is not responding.",
            "color": 0xED4245,  # Discord red
            "footer": {"text": "Last checked"},
            "timestamp": now,
        }
    if names:
        epoch = time.time()
        sessions = sessions or {}
        players = "\n".join(
            f"> 🎮  **{n}**  ·  {fmt_duration(epoch - sessions.get(n, epoch))}"
            for n in names
        )
    elif count > 0:
        players = f"> 🎮  {count} online *(names hidden by server)*"
    else:
        players = "> 💤  *No players online*"
    return {
        "title": "🟢  Server Online",
        "description": f"### Players Online — {count}/{max_players}\n{players}",
        "color": 0x57F287,  # Discord green
        "footer": {"text": f"{SERVER_ADDRESS}  •  last checked"},
        "timestamp": now,
    }


def refresh_status_message(state, is_online, count, max_players, names, dry_run=False, reposition=True, sessions=None):
    """Keep the status embed current.

    reposition=True: delete the old embed and post a fresh one (used after event
    messages, so the embed stays the newest message in the channel).
    reposition=False: edit the existing embed in place — updates the player list
    and "last checked" timestamp on quiet checks without re-sending anything.
    """
    if dry_run:
        log(f"[dry-run] status embed: online={is_online} players={count} {names}")
        return
    embed = build_status_embed(is_online, count, max_players, names, sessions)
    old_id = state.get("status_msg_id")

    if old_id and not reposition:
        try:
            _webhook_request(f"{WEBHOOK_URL}/messages/{old_id}", "PATCH", {"embeds": [embed]})
            log("[status] updated status embed in place")
            return
        except urllib.error.HTTPError as e:
            if e.code != 404:
                log(f"[warn] could not edit status message: {e}")
                return
            # 404: someone deleted it — fall through and post a new one.
        except urllib.error.URLError as e:
            log(f"[warn] could not edit status message: {e}")
            return

    if old_id and reposition:
        try:
            _webhook_request(f"{WEBHOOK_URL}/messages/{old_id}", "DELETE")
        except urllib.error.HTTPError as e:
            if e.code != 404:  # already gone is fine
                log(f"[warn] could not delete old status message: {e}")
        except urllib.error.URLError as e:
            log(f"[warn] could not delete old status message: {e}")

    try:
        resp = _webhook_request(
            WEBHOOK_URL + "?wait=true",
            "POST",
            {"username": "MC Server Watch", "embeds": [embed]},
        )
        state["status_msg_id"] = resp["id"]
        log(f"[status] reposted status embed ({count} online)" if is_online else "[status] reposted status embed (offline)")
    except urllib.error.URLError as e:
        log(f"[error] could not post status embed: {e}")


def check_once(dry_run=False):
    state = load_state()
    data = fetch_status()

    is_online = bool(data and data.get("online"))

    if is_online:
        p = data.get("players", {})
        names = sorted(x["name"] for x in p.get("list", []))
        log(
            f"[check] online, {p.get('online', 0)}/{p.get('max', '?')} players"
            + (f" ({', '.join(names)})" if names else "")
        )
    else:
        log(f"[check] offline/unreachable (streak {state.get('fail_streak', 0) + 1})")

    if not is_online:
        state["fail_streak"] = state.get("fail_streak", 0) + 1
        # Announce down only once, after enough consecutive failures,
        # and only if we previously knew it was up.
        if state["fail_streak"] == OFFLINE_THRESHOLD and state.get("online") is not False:
            post_discord(
                f"{DOWN_MENTION} :red_circle: **Server DOWN** — `{SERVER_ADDRESS}` is not responding.",
                dry_run,
            )
            state["online"] = False
            state["players"] = []
            state["count"] = 0
            state["sessions"] = {}
            refresh_status_message(state, False, 0, "?", [], dry_run, reposition=True)
        elif state.get("online") is False:
            # Already announced down — just keep the offline card's timestamp fresh.
            refresh_status_message(state, False, 0, "?", [], dry_run, reposition=False)
        update_weekly(state, [], time.time(), dry_run)
        save_state(state)
        return

    # --- server is online ---
    came_back = state.get("online") is False
    first_run = state.get("online") is None
    state["fail_streak"] = 0

    players_info = data.get("players", {})
    count = players_info.get("online", 0)
    names = sorted(p["name"] for p in players_info.get("list", []))

    old_names = state.get("players", [])
    old_count = state.get("count", 0)
    sessions = state.get("sessions", {})
    epoch = time.time()

    messages = []

    if came_back:
        messages.append(
            f":green_circle: **Server UP** — `{SERVER_ADDRESS}` is back online "
            f"({count} player{'s' if count != 1 else ''} on)."
        )

    if not first_run and not came_back:
        joined = [n for n in names if n not in old_names]
        left = [n for n in old_names if n not in names]
        for n in joined:
            messages.append(f":arrow_right: **{n}** joined the server ({count} online)")
        for n in left:
            played = epoch - sessions.pop(n, epoch)
            was_on = fmt_duration(played) if played >= 60 else "under a minute"
            messages.append(
                f":arrow_left: **{n}** left the server ({count} online) — was on for **{was_on}**"
            )

        # Count changed but the names don't explain it (list missing or partial).
        explained = old_count + len(joined) - len(left)
        if count != old_count and count != explained:
            messages.append(
                f":busts_in_silhouette: Players online changed: "
                f"**{old_count} → {count}** (of {players_info.get('max', '?')})"
            )

    if first_run:
        messages.append(
            f":white_check_mark: Watcher started — `{SERVER_ADDRESS}` is online "
            f"with {count} player{'s' if count != 1 else ''}."
            + (f" ({', '.join(names)})" if names else "")
        )

    for msg in messages:
        post_discord(msg, dry_run)

    # Session start times: current players keep theirs, new arrivals start now.
    sessions = {n: sessions.get(n, epoch) for n in names}

    update_weekly(state, names, epoch, dry_run, refresh_now=bool(messages))

    # Concurrent-player record (records of 1 aren't worth announcing).
    record = state.get("record_players", 0)
    if count > record:
        if count >= 2:
            prev = f" (previous best: {record})" if record >= 2 else ""
            post_discord(
                f":tada: **New record!** {count} players online at once{prev}", dry_run
            )
        state["record_players"] = count

    # Playtime milestones against all-time hours, announced once each.
    announced = state.setdefault("milestones", {})
    for n, secs in state.get("alltime_seconds", {}).items():
        hours = secs / 3600
        crossed = [m for m in MILESTONE_HOURS if hours >= m > announced.get(n, 0)]
        if crossed:
            top = max(crossed)
            post_discord(
                f":military_medal: **{n}** has now played over **{top} hours** on the server!",
                dry_run,
            )
            announced[n] = top

    # After event messages, repost the embed so it stays the newest message;
    # on quiet checks, edit it in place so "last checked" stays current.
    refresh_status_message(
        state, True, count, players_info.get("max", "?"), names, dry_run,
        reposition=bool(messages), sessions=sessions,
    )

    state["online"] = True
    state["players"] = names
    state["count"] = count
    state["sessions"] = sessions
    save_state(state)


def main():
    dry_run = "--dry-run" in sys.argv
    once = "--once" in sys.argv

    if not dry_run and not WEBHOOK_URL:
        sys.exit(
            "DISCORD_WEBHOOK_URL is not set. Locally, run via run_local.sh; "
            "on GitHub Actions, add it as a repository secret."
        )

    if once:
        check_once(dry_run)
        return

    log(f"[start] watching {SERVER_ADDRESS} every {POLL_SECONDS}s")
    while True:
        try:
            check_once(dry_run)
        except Exception as e:  # keep the loop alive on any unexpected error
            log(f"[error] check failed: {e}")
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
