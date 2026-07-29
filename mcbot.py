#!/usr/bin/env python3
"""mcbot — Minecraft server -> Discord, reading the server's own data.

Runs on the machine that hosts the Minecraft server. Instead of polling a
third-party status API (5-minute cache, no player names, no real statistics)
it reads three local sources:

  logs/latest.log             instant join / leave / death / advancement / chat
  TCP status ping on :25565   authoritative online count, version, MOTD, latency
  world/players/stats/*.json  real playtime, deaths, kills, blocks mined, ...

Standard library only. Configure with config.json next to this file (see
config.example.json); every key may also be overridden by an env var.

Usage:
  python mcbot.py                 run forever (the normal mode)
  python mcbot.py --once          single pass, no log tailing — for testing
  python mcbot.py --dry-run       print what would be posted, post nothing
"""

import argparse
import datetime as dt
import gzip
import json
import os
import random
import re
import socket
import struct
import sys
import time
import urllib.error
import urllib.request

import funstats
import gamestats
import ledger
import perf
import rcon

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(HERE, "config.json")
STATE_FILE = os.path.join(HERE, "state.json")
LOG_FILE = os.path.join(HERE, "mcbot.log")

USER_AGENT = "mcbot/2.0 (local server watcher)"
# Bumped when the set of cards in a channel changes, so the old ones are
# cleared out and the new set is posted in the intended order.
LAYOUT_VERSION = 5
TICKS_PER_SECOND = 20

# All-time playtime marks that earn a shout-out, in hours.
MILESTONE_HOURS = [10, 25, 50, 100, 250, 500, 1000, 2000]

# Advancements hard enough that earning one is an event in itself. These are
# the display names as the server logs them.
RARE_ADVANCEMENTS = {
    "How Did We Get Here?", "Cover Me in Debris", "Beaconator",
    "A Complete Catalogue", "Two by Two", "Uneasy Alliance", "Arbalistic",
    "The End... Again...", "Monsters Hunted", "A Balanced Diet",
    "Smithing with Style", "Sniper Duel", "Bullseye",
}

# World ages worth a birthday message, in days, besides every full year.
ANNIVERSARY_DAYS = {100, 250, 500, 750}

# ------------------------------------------------------------------ config --

DEFAULTS = {
    # Where the Minecraft server lives. logs/ and world/ are read from here.
    "server_dir": "",
    # Address to ping for live status. Localhost — this runs on the server.
    "host": "127.0.0.1",
    "port": 25565,
    # Public address, used for display: this is what players type.
    "public_address": "",
    # What the reachability check actually pings. Defaults to public_address.
    # Set it when public_address is a domain that players reach through a
    # Minecraft SRV record: the client follows those, this bot does not, and
    # the apex record often points somewhere else entirely (a web host, or
    # Cloudflare, which does not carry Minecraft traffic).
    "check_address": "",
    "webhook_main": "",     # status card + join/leave/death/advancement events
    "webhook_weekly": "",   # weekly + all-time + statistics leaderboards
    "webhook_logs": "",     # mirror of this bot's own log lines
    "webhook_perf": "",     # performance card, outage records, daily report
    "webhook_stats": "",    # every tracked statistic, all-time, by category
    "webhook_daily": "",    # today's statistics, reset at 03:00 Eastern
    "webhook_chat": "",     # every chat line, join, leave, death, advancement
    "webhook_events": "",   # the hype feed: records, streaks, overtakes,
                            # diamond finds, rare advancements, daily awards
    # Post join/leave/death/advancement/record/up-down messages to the main
    # channel. Off by default: that channel holds the status card and nothing
    # else, so the card stays put and never has to be re-posted.
    "main_events": False,
    # Who to ping when the server goes down: "@everyone", "@here", "<@id>", "".
    # Only used when main_events is on; outages are always recorded in the
    # performance channel regardless.
    "down_mention": "@everyone",
    # Seconds of failed pings before announcing the server is down.
    "down_after_seconds": 90,
    # Relay in-game chat to the main channel.
    "relay_chat": False,
    # Mirror event announcements (records, streaks, awards…) into the game
    # itself, via RCON. Does nothing until RCON is enabled on the server.
    "ingame_events": True,
    # Fun facts about a random online player, told in game chat. Roughly
    # this many an hour, at jittered moments; 0 turns them off.
    "fun_facts_per_hour": 3,
    # Cycle a labelled statistic through the tab list, on the same timer.
    "tab_stats": True,
    # Keep the MOTD fresh through MiniMOTD's config. Also needs RCON.
    "dynamic_motd": True,
    "motd_refresh_seconds": 600,
    # The day the world began, for "Day N" and anniversaries. Set this when
    # the log archive does not reach back to the real first day; empty falls
    # back to the oldest date in the archive.
    "world_started": "",
    # Announce deaths and advancements.
    "announce_deaths": True,
    "announce_advancements": True,
    # Check from outside that the server is reachable over the internet
    # (catches port-forwarding breaking while the server itself is fine).
    "external_check": True,
    "external_check_minutes": 15,
    # Timing.
    "log_poll_seconds": 1,
    "ping_seconds": 20,
    "stats_seconds": 60,
    "status_refresh_seconds": 90,
    "leaderboard_refresh_seconds": 600,
    # The all-time statistic cards are large and move slowly.
    "stats_card_seconds": 900,
    # Today's statistics move faster and there are far fewer of them.
    "daily_card_seconds": 300,
    # Cap rows per statistic category; 0 shows every recorded statistic, which
    # is a couple of thousand rows and a whole channel of cards. Ten keeps each
    # category to one card of headline figures.
    "stats_top_per_category": 10,
    "daily_top_per_category": 10,
    # How high the bar is for a statistic to earn a row at all. 1.0 uses the
    # per-category floors in gamestats.py — enough to keep "1 iron ingot picked
    # up" out. Raise it for only the big numbers, lower it to show more.
    "stats_noise_scale": 1.0,
    # Performance monitoring.
    "perf_sample_seconds": 30,
    "perf_card_seconds": 120,
    "perf_alerts": True,
    "alert_host_cpu": 90,       # percent, sustained over five samples
    "alert_host_ram": 92,       # percent, sustained over five samples
    "alert_disk_free_gb": 15,
    "alert_heap_pct": 92,       # percent of the java heap in use
    "daily_report": True,
}


def load_config():
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            cfg.update(json.load(f))
    except FileNotFoundError:
        pass
    except json.JSONDecodeError as e:
        sys.exit(f"config.json is not valid JSON: {e}")
    for key in DEFAULTS:
        env = os.environ.get("MCBOT_" + key.upper())
        if env is not None:
            cfg[key] = type(DEFAULTS[key])(env) if not isinstance(DEFAULTS[key], bool) \
                else env.strip().lower() in ("1", "true", "yes", "on")
    return cfg


CFG = load_config()

LOG_PATH = os.path.join(CFG["server_dir"], "logs", "latest.log")
STATS_DIR = os.path.join(CFG["server_dir"], "world", "players", "stats")
ADVANCEMENTS_DIR = os.path.join(CFG["server_dir"], "world", "players", "advancements")
USERCACHE = os.path.join(CFG["server_dir"], "usercache.json")

DRY_RUN = False

# ------------------------------------------------------------------ output --

_mirroring = False  # guards log() -> webhook -> log() recursion

LOG_MAX_BYTES = 5 * 1024 * 1024


def _rotate_log():
    try:
        if os.path.getsize(LOG_FILE) > LOG_MAX_BYTES:
            os.replace(LOG_FILE, LOG_FILE + ".1")
    except OSError:
        pass


def log(message, mirror=False):
    """Timestamped line to stdout and mcbot.log; optionally to the log channel."""
    global _mirroring
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}"
    # Under pythonw.exe there is no console and sys.stdout is None.
    if sys.stdout is not None:
        try:
            print(line, flush=True)
        except UnicodeEncodeError:
            # A Windows console is usually cp1252, which cannot hold the emoji
            # in card titles or the characters players type in chat. Losing a
            # character from the console is fine; crashing the watcher is not.
            encoding = sys.stdout.encoding or "ascii"
            print(line.encode(encoding, "replace").decode(encoding), flush=True)
    try:
        _rotate_log()
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError as e:
        if sys.stderr is not None:
            print(f"[warn] could not write log file: {e}", file=sys.stderr)
    if mirror and CFG["webhook_logs"] and not _mirroring and not DRY_RUN:
        _mirroring = True
        try:
            discord(CFG["webhook_logs"], "POST", {
                "content": f"`{line}`",
                "username": "MC Logs",
                "allowed_mentions": {"parse": []},  # log text must never ping
            })
        except Exception as e:
            if sys.stderr is not None:
                print(f"[warn] log webhook failed: {e}", file=sys.stderr)
        finally:
            _mirroring = False


def discord(url, method="POST", payload=None, retries=3):
    """One Discord webhook call, honouring 429 rate limits."""
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Content-Type": "application/json", "User-Agent": USER_AGENT})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < retries - 1:
                try:
                    wait = float(json.loads(e.read()).get("retry_after", 1))
                except Exception:
                    wait = 1.0
                time.sleep(min(wait, 10) + 0.1)
                continue
            raise
    raise RuntimeError("unreachable")


def say(content):
    """Post an event message to the main channel, if those are enabled."""
    if not CFG["main_events"]:
        return
    if DRY_RUN:
        log(f"[dry-run] {content}")
        return
    try:
        discord(CFG["webhook_main"], "POST", {
            "content": content,
            "username": "MC Server",
            "allowed_mentions": {"parse": ["everyone", "users", "roles"]},
        })
        log(f"[posted] {content}", mirror=True)
    except (urllib.error.URLError, OSError) as e:
        log(f"[error] could not post to main channel: {e}")


_chat_queue = []
CHAT_BATCH_CHARS = 1900  # Discord allows 2000; leave room for the join


def chat_say(line):
    """Queue one line for the chat-history channel.

    This channel is a transcript, not an announcement feed: it carries every
    chat message, join, leave, death and advancement regardless of what the
    main channel is configured to announce.
    """
    if CFG["webhook_chat"]:
        _chat_queue.append(line)


def flush_chat():
    """Post everything queued for the chat-history channel, oldest first.

    Lines are batched because a busy server produces them faster than a webhook
    will accept messages — one message per cycle stays inside the rate limit
    and keeps the transcript in order.
    """
    global _chat_queue
    if not _chat_queue:
        return
    pending, _chat_queue = _chat_queue, []
    if DRY_RUN:
        for line in pending:
            log(f"[dry-run][chat] {line}")
        return
    while pending:
        batch, size = [], 0
        while pending and size + len(pending[0]) + 1 <= CHAT_BATCH_CHARS:
            size += len(pending[0]) + 1
            batch.append(pending.pop(0))
        if not batch:  # a single line longer than a whole message
            batch = [pending.pop(0)[:CHAT_BATCH_CHARS]]
        try:
            discord(CFG["webhook_chat"], "POST", {
                "content": "\n".join(batch),
                "username": "MC Chat",
                # A transcript repeats whatever players typed, so it must never
                # be able to ping anybody.
                "allowed_mentions": {"parse": []},
            })
        except (urllib.error.URLError, OSError) as e:
            log(f"[error] could not post to the chat channel: {e}")
            return


_events_warned = False


def events_say(content):
    """Post to the events channel — records, streaks, overtakes, finds.

    These are moments, not cards: they only make sense as a feed. With no
    events webhook configured they are dropped, not rerouted — every other
    channel is either a transcript or a set of silently-edited cards, and
    the whole point of those is that they never notify anybody.
    """
    global _events_warned
    if DRY_RUN:
        log(f"[dry-run][events] {content}")
        return
    if CFG["ingame_events"]:
        # The same moment, told to the people it happened to. Silently a
        # no-op until RCON is enabled on the server.
        rcon.broadcast(CFG["server_dir"], content)
    if not CFG["webhook_events"]:
        if not _events_warned:
            log("[warn] webhook_events is not set — record, streak and other "
                "announcements are being dropped")
            _events_warned = True
        return
    try:
        discord(CFG["webhook_events"], "POST", {
            "content": content,
            "username": "MC Events",
            "allowed_mentions": {"parse": []},
        })
        log(f"[events] {content}", mirror=True)
    except (urllib.error.URLError, OSError) as e:
        log(f"[error] could not post to the events channel: {e}")


def perf_say(content):
    """Post to the performance channel. Never pings anybody."""
    if not CFG["webhook_perf"]:
        return
    if DRY_RUN:
        log(f"[dry-run][perf] {content}")
        return
    try:
        discord(CFG["webhook_perf"], "POST", {
            "content": content,
            "username": "MC Performance",
            "allowed_mentions": {"parse": []},
        })
        log(f"[perf] {content}", mirror=True)
    except (urllib.error.URLError, OSError) as e:
        log(f"[error] could not post to the performance channel: {e}")


def webhook_for(card):
    """Which channel a card belongs to, by its state key."""
    if card.startswith("stat_"):
        return CFG["webhook_stats"]
    if card.startswith("daily_"):
        return CFG["webhook_daily"]
    return {
        "status": CFG["webhook_main"],
        "perf": CFG["webhook_perf"],
        "weekly": CFG["webhook_weekly"],
        "alltime": CFG["webhook_weekly"],
        "weekstats": CFG["webhook_weekly"],
        "records": CFG["webhook_weekly"],
        "streaks": CFG["webhook_weekly"],
        "primetime": CFG["webhook_weekly"],
        "chatstats": CFG["webhook_weekly"],
        "stats": CFG["webhook_weekly"],  # retired card, kept so it can be swept
    }.get(card, "")


def _delete_message(url, mid):
    """True if the message is gone (deleted now, or already absent)."""
    if DRY_RUN:
        # A dry run must not touch a channel. Guarded here rather than at each
        # call site, so no path can delete a real card by accident.
        log(f"[dry-run] would delete message {mid}")
        return True
    try:
        discord(f"{url}/messages/{mid}", "DELETE")
        return True
    except urllib.error.HTTPError as e:
        return e.code == 404  # already gone is success
    except (urllib.error.URLError, OSError):
        return False


def sweep_stale(url, state_key, state):
    """Delete cards this bot posted but lost track of.

    A card whose id was never persisted — the process was killed between
    posting and saving — would otherwise sit in the channel forever as a
    duplicate. Every id is recorded before it is replaced, and retried here.
    """
    stale = state.setdefault("msg_stale", {}).get(state_key, [])
    if not stale:
        return
    current = state["msg"].get(state_key)
    remaining = []
    for mid in stale:
        if mid == current:
            continue
        time.sleep(0.3)  # stay inside the webhook rate limit
        if not _delete_message(url, mid):
            remaining.append(mid)
        else:
            log(f"[card] removed a stale {state_key} card")
    state["msg_stale"][state_key] = remaining


def upsert_embed(url, state_key, state, embed, reposition=False):
    """Keep exactly one embed message current.

    Normally the message is edited in place, which is what keeps it unique.
    reposition deletes and re-posts it, used only where the card must become
    the newest message in its channel again.
    """
    if DRY_RUN:
        log(f"[dry-run] embed {state_key}: {embed.get('title')}")
        return
    mid = state["msg"].get(state_key)
    if mid and not reposition:
        try:
            discord(f"{url}/messages/{mid}", "PATCH", {"embeds": [embed]})
            return
        except urllib.error.HTTPError as e:
            if e.code != 404:
                log(f"[warn] could not edit {state_key}: {e}")
                return
            # 404: someone deleted it — fall through and post a new one.
        except (urllib.error.URLError, OSError) as e:
            log(f"[warn] could not edit {state_key}: {e}")
            return
    if mid and reposition and not _delete_message(url, mid):
        log(f"[warn] could not delete old {state_key}; will retry as stale")
        state.setdefault("msg_stale", {}).setdefault(state_key, []).append(mid)
    try:
        # Record the id before the request, so a crash between posting and
        # saving leaves a lead to clean up rather than an orphan.
        state.setdefault("msg_stale", {}).setdefault(state_key, [])
        resp = discord(url + "?wait=true", "POST",
                       {"username": "MC Server", "embeds": [embed]})
        state["msg"][state_key] = resp["id"]
        state["msg_stale"][state_key].append(resp["id"])
        save_state(state)  # persist immediately; a kill must not orphan this
        log(f"[card] posted {state_key}")
    except (urllib.error.URLError, OSError) as e:
        log(f"[error] could not post {state_key}: {e}")


# ------------------------------------------------------------- status ping --

def _varint(n):
    out = b""
    while True:
        b = n & 0x7F
        n >>= 7
        out += bytes([b | (0x80 if n else 0)])
        if not n:
            return out


def _read_varint(sock):
    n = 0
    for i in range(5):
        b = sock.recv(1)
        if not b:
            raise ConnectionError("connection closed")
        n |= (b[0] & 0x7F) << (7 * i)
        if not b[0] & 0x80:
            return n
    raise ValueError("varint too long")


def ping_server(host=None, port=None, timeout=5):
    """Minecraft Server List Ping. Returns the status dict, or None if down."""
    host = host or CFG["host"]
    port = port or CFG["port"]
    sock = None
    try:
        started = time.time()
        sock = socket.create_connection((host, port), timeout=timeout)
        sock.settimeout(timeout)
        addr = host.encode()
        # handshake: protocol version, address, port, next state = 1 (status)
        hs = (b"\x00" + _varint(776) + _varint(len(addr)) + addr
              + struct.pack(">H", port) + _varint(1))
        sock.sendall(_varint(len(hs)) + hs)
        sock.sendall(_varint(1) + b"\x00")  # status request
        _read_varint(sock)                  # packet length
        _read_varint(sock)                  # packet id
        length = _read_varint(sock)
        buf = b""
        while len(buf) < length:
            chunk = sock.recv(min(8192, length - len(buf)))
            if not chunk:
                break
            buf += chunk
        data = json.loads(buf.decode("utf-8", "replace"))
        data["_latency_ms"] = int((time.time() - started) * 1000)
        return data
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    finally:
        if sock:
            try:
                sock.close()
            except OSError:
                pass


# Legacy formatting: § followed by a colour/style code. A hex colour is six
# of these in a row (§x§F§F§0§0§0§0), so the same pattern consumes it too.
MOTD_CODE_RE = re.compile("§.")


def describe_motd(desc):
    """Flatten a chat-component or plain-string MOTD into plain text.

    Discord renders none of Minecraft's formatting, so the § codes are
    stripped rather than translated.
    """
    if isinstance(desc, str):
        return MOTD_CODE_RE.sub("", desc)
    if isinstance(desc, dict):
        text = describe_motd(desc.get("text", ""))
        for extra in desc.get("extra", []):
            text += describe_motd(extra)
        return text
    if isinstance(desc, list):
        return "".join(describe_motd(d) for d in desc)
    return ""


# ------------------------------------------------------ server stats files --

def load_usercache():
    """uuid -> name, from the server's own cache."""
    try:
        with open(USERCACHE, encoding="utf-8") as f:
            return {e["uuid"]: e["name"] for e in json.load(f)}
    except (OSError, json.JSONDecodeError, KeyError, TypeError):
        return {}


def _sum_prefix(custom, suffix):
    return sum(v for k, v in custom.items() if k.endswith(suffix))


def load_stats():
    """name -> real statistics, read straight out of world/players/stats."""
    names = load_usercache()
    out = {}
    try:
        files = os.listdir(STATS_DIR)
    except OSError:
        return out
    for fn in files:
        if not fn.endswith(".json"):
            continue
        uuid = fn[:-5]
        name = names.get(uuid)
        if not name:
            continue
        path = os.path.join(STATS_DIR, fn)
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        stats = data.get("stats", {})
        custom = stats.get("minecraft:custom", {})
        out[name] = {
            "uuid": uuid,
            "play_time": custom.get("minecraft:play_time", 0) / TICKS_PER_SECOND,
            "deaths": custom.get("minecraft:deaths", 0),
            "mob_kills": custom.get("minecraft:mob_kills",
                                    sum(stats.get("minecraft:killed", {}).values())),
            "player_kills": custom.get("minecraft:player_kills", 0),
            "mined": sum(stats.get("minecraft:mined", {}).values()),
            "crafted": sum(stats.get("minecraft:crafted", {}).values()),
            "distance_cm": _sum_prefix(custom, "_one_cm"),
            "jumps": custom.get("minecraft:jump", 0),
            "damage_dealt": custom.get("minecraft:damage_dealt", 0) / 10,
            "damage_taken": custom.get("minecraft:damage_taken", 0) / 10,
            "fish_caught": custom.get("minecraft:fish_caught", 0),
            "animals_bred": custom.get("minecraft:animals_bred", 0),
            "trades": custom.get("minecraft:traded_with_villager", 0),
            "raid_wins": custom.get("minecraft:raid_win", 0),
            "advancements": count_advancements(uuid),
            "mtime": os.path.getmtime(path),
            "raw": stats,  # every category, for the tracked-statistic cards
        }
    return out


def criterion_values(stats, criteria):
    """{player: {criterion: value}} for every statistic the datapacks track."""
    return {
        name: {c: gamestats.value_of(entry.get("raw", {}), c) for c in criteria}
        for name, entry in stats.items()
    }


def subtract(current, baseline):
    """Per-player change since a baseline snapshot."""
    out = {}
    for name, values in current.items():
        before = baseline.get(name, {})
        out[name] = {c: v - before.get(c, 0) for c, v in values.items()
                     if v - before.get(c, 0) > 0}
    return out


def count_advancements(uuid):
    """Completed advancements, ignoring the recipe-unlock pseudo-advancements."""
    path = os.path.join(ADVANCEMENTS_DIR, f"{uuid}.json")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return 0
    n = 0
    for key, val in data.items():
        if key == "DataVersion" or "recipes/" in key:
            continue
        if isinstance(val, dict) and val.get("done"):
            n += 1
    return n


def load_advancement_sets(stats):
    """{name: set of completed advancement keys}, recipe unlocks excluded."""
    out = {}
    for name, entry in stats.items():
        path = os.path.join(ADVANCEMENTS_DIR, f"{entry['uuid']}.json")
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        out[name] = {key for key, val in data.items()
                     if key != "DataVersion" and "recipes/" not in key
                     and isinstance(val, dict) and val.get("done")}
    return out


def live_playtime(stats, state, name, now):
    """All-time seconds for a player, including the session in progress.

    Stats files are only written when the server saves the player, so for
    anyone online we extend their last saved total by the session time the
    log has observed since they joined.
    """
    saved = stats.get(name, {}).get("play_time", 0)
    start = state["players"].get(name)
    if start is None:
        return saved
    at_join = state["play_time_at_join"].get(name, saved)
    return max(saved, at_join + (now - start))


def all_playtimes(stats, state, now):
    names = set(stats) | set(state["players"])
    return {n: live_playtime(stats, state, n, now) for n in names}


# ------------------------------------------------------------- log reading --

LINE_RE = re.compile(r"^\[(\d{2}):(\d{2}):(\d{2})\] \[[^\]]*\]: (.*)$")
# Java names are word characters, but Floodgate prefixes Bedrock players with
# a configurable character (".by default"), so allow a leading punctuation mark.
NAME = r"[.\w]{1,20}"
JOIN_RE = re.compile(rf"^({NAME}) joined the game$")
LEAVE_RE = re.compile(rf"^({NAME}) left the game$")
CHAT_RE = re.compile(rf"^<({NAME})> (.+)$")
ADV_RE = re.compile(
    rf"^({NAME}) has (?:made the advancement|completed the challenge|reached the goal) \[(.+)\]$")
START_RE = re.compile(r'^Done \([^)]*\)! For help, type')
STOP_RE = re.compile(r"^Stopping the server$")
# The server's own complaint when a tick takes too long. Two wordings exist
# across versions ("Running 2145ms or 42 ticks behind" and "Running 2145ms
# behind, skipping 42 tick(s)"), so match the milliseconds and find the ticks.
LAG_RE = re.compile(r"Can't keep up!.*?Running (\d+)\s*ms", re.I)
LAG_TICKS_RE = re.compile(r"(\d+)\s*tick")
# Lines that begin with a player name but are not deaths.
NOT_DEATH_RE = re.compile(
    r"^[.\w]{1,20} (?:joined the game|left the game|lost connection|"
    r"has made the advancement|has completed the challenge|has reached the goal|"
    r"issued server command|moved too quickly|moved wrongly|"
    r"was kicked|tried to swim in lava to escape)")


def parse_line(raw):
    """('HH:MM:SS', message) for a well-formed log line, else None."""
    m = LINE_RE.match(raw.rstrip("\n"))
    if not m:
        return None
    return f"{m.group(1)}:{m.group(2)}:{m.group(3)}", m.group(4)


FINGERPRINT_BYTES = 300


class LogReader:
    """Reads new lines from latest.log without holding the file open.

    The server renames latest.log when it rotates, and on Windows an open
    handle would make that rename fail — so every read opens, seeks and closes.
    Positions are byte offsets and the file is read in binary, because text-mode
    offsets are opaque cookies that cannot be added to or compared.
    """

    def __init__(self, path):
        self.path = path
        self.pos = 0
        self.fingerprint = None

    def _rotated(self, head, size):
        """True if latest.log is a different file than the one we were reading."""
        if self.pos > size:
            return True  # truncated or replaced by a shorter file
        # Only trust the header comparison once both samples are full length;
        # a freshly created log is still shorter than the sample window.
        return (self.fingerprint is not None
                and len(head) == FINGERPRINT_BYTES == len(self.fingerprint)
                and head != self.fingerprint)

    def read_new(self):
        """Return the complete lines appended since the last call."""
        try:
            with open(self.path, "rb") as f:
                head = f.read(FINGERPRINT_BYTES)
                size = f.seek(0, os.SEEK_END)
                if self._rotated(head, size):
                    log("[log] latest.log rotated — following the new file")
                    self.pos = 0
                self.fingerprint = head if len(head) == FINGERPRINT_BYTES else None
                f.seek(self.pos)
                data = f.read()
                # Stop at the last newline: a trailing partial line means the
                # server is still writing it, so leave it for the next read.
                cut = data.rfind(b"\n")
                if cut < 0:
                    return []
                self.pos += cut + 1
                return data[:cut + 1].decode("utf-8", "replace").splitlines()
        except FileNotFoundError:
            return []
        except OSError as e:
            log(f"[warn] could not read latest.log: {e}")
            return []

    def seek_end(self):
        try:
            with open(self.path, "rb") as f:
                head = f.read(FINGERPRINT_BYTES)
                self.fingerprint = head if len(head) == FINGERPRINT_BYTES else None
                self.pos = f.seek(0, os.SEEK_END)
        except OSError:
            self.pos = 0


def _absolute_times(raw_lines, start_date=None, mtime=None):
    """Attach absolute timestamps to log lines, which carry only a clock time.

    Every time the clock runs backwards the log has crossed midnight. With a
    known start date we count forwards from it; otherwise we anchor the last
    line at the file's modification date and count backwards.
    """
    parsed = [p for p in (parse_line(line) for line in raw_lines) if p]
    if not parsed:
        return []
    offsets, day, prev = [], 0, None
    for hms, _ in parsed:
        if prev is not None and hms < prev:
            day += 1
        offsets.append(day)
        prev = hms
    if start_date is None:
        start_date = dt.date.fromtimestamp(mtime) - dt.timedelta(days=day)
    out = []
    for (hms, msg), off in zip(parsed, offsets):
        h, m, s = (int(x) for x in hms.split(":"))
        when = dt.datetime.combine(start_date + dt.timedelta(days=off),
                                   dt.time(h, m, s))
        out.append((when.timestamp(), msg))
    return out


def read_whole_log(path):
    """Every line of latest.log, with absolute timestamps."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            raw = f.readlines()
        return _absolute_times(raw, mtime=os.path.getmtime(path))
    except OSError:
        return []


ARCHIVE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})-\d+\.log\.gz$")


def read_all_logs():
    """Every log line the server still has on disk, oldest first.

    Rotated logs are named after the date they *start*, which is a firmer
    anchor than a modification time, so use it where it is available.
    """
    log_dir = os.path.join(CFG["server_dir"], "logs")
    events = []
    try:
        names = os.listdir(log_dir)
    except OSError:
        return []
    for name in sorted(names):
        m = ARCHIVE_RE.match(name)
        if not m:
            continue
        start = dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        try:
            with gzip.open(os.path.join(log_dir, name), "rt",
                           encoding="utf-8", errors="replace") as f:
                events.extend(_absolute_times(f.readlines(), start_date=start))
        except (OSError, EOFError) as e:
            log(f"[warn] could not read {name}: {e}")
    events.extend(read_whole_log(os.path.join(log_dir, "latest.log")))
    events.sort(key=lambda e: e[0])
    return events


def playtime_since(epoch):
    """name -> seconds played since epoch, reconstructed from the log archive.

    Used once, to seed the weekly board when the watcher first starts partway
    through a week; the stats files know lifetime totals but not when the time
    was earned.
    """
    totals, sessions = {}, {}

    def close(name, start, end):
        totals[name] = totals.get(name, 0) + max(0, end - max(start, epoch))

    for when, msg in read_all_logs():
        if START_RE.match(msg) or STOP_RE.match(msg):
            for name, start in sessions.items():
                close(name, start, when)
            sessions.clear()
            continue
        m = JOIN_RE.match(msg)
        if m:
            sessions[m.group(1)] = when
            continue
        m = LEAVE_RE.match(msg)
        if m:
            start = sessions.pop(m.group(1), None)
            if start is not None:
                close(m.group(1), start, when)
    now = time.time()
    for name, start in sessions.items():
        close(name, start, now)
    return totals


def week_start_epoch(now):
    d = dt.date.fromtimestamp(now)
    monday = d - dt.timedelta(days=d.isoweekday() - 1)
    return dt.datetime.combine(monday, dt.time.min).timestamp()


# ------------------------------------------------------- the statistics day --

# The daily board turns over at 03:00 Eastern rather than at midnight, so a
# late-night session stays on the day it felt like it belonged to.
DAY_RESET_HOUR = 3


def _nth_weekday(year, month, weekday, n):
    """The nth given weekday of a month, e.g. the second Sunday in March."""
    first = dt.date(year, month, 1)
    return first + dt.timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))


def eastern_offset(epoch):
    """US Eastern's offset from UTC in seconds: -4h in summer, -5h in winter.

    zoneinfo needs the separate tzdata package, which a stock Windows Python
    does not have, so the rule is spelled out here instead: daylight time runs
    from 07:00 UTC on the second Sunday in March to 06:00 UTC on the first
    Sunday in November. That has been the US rule since 2007.
    """
    year = time.gmtime(epoch).tm_year
    starts = dt.datetime.combine(_nth_weekday(year, 3, 6, 2), dt.time(7),
                                 dt.timezone.utc).timestamp()
    ends = dt.datetime.combine(_nth_weekday(year, 11, 6, 1), dt.time(6),
                               dt.timezone.utc).timestamp()
    return -4 * 3600 if starts <= epoch < ends else -5 * 3600


def stat_day(epoch):
    """The date the daily board is counting at this moment."""
    shifted = epoch + eastern_offset(epoch) - DAY_RESET_HOUR * 3600
    return dt.datetime.fromtimestamp(shifted, dt.timezone.utc).date()


def day_key(epoch):
    return stat_day(epoch).isoformat()


def day_label(epoch):
    return stat_day(epoch).strftime("%A, %b %d")


def day_start_epoch(epoch):
    """When the current statistics day began: 03:00 Eastern on that date."""
    boundary = dt.datetime.combine(stat_day(epoch), dt.time(DAY_RESET_HOUR),
                                   dt.timezone.utc).timestamp()
    return boundary - eastern_offset(epoch)


def eastern_clock(epoch):
    """'02:41' — the Eastern wall clock, for footers that quote the reset."""
    return time.strftime("%H:%M", time.gmtime(epoch + eastern_offset(epoch)))


# ------------------------------------------------------------ dynamic motd --

# The vanilla MOTD is read from server.properties once, at boot. MiniMOTD
# re-reads its own config on "minimotd reload", so the bot keeps the config's
# motd list stocked with live facts and asks for a reload over RCON — the
# server-list entry becomes a tiny dashboard.


def find_minimotd_config(server_dir):
    """MiniMOTD's main.conf, wherever the mod put it, or None until it exists."""
    config_root = os.path.join(server_dir, "config")
    try:
        for name in os.listdir(config_root):
            if "minimotd" in name.lower():
                path = os.path.join(config_root, name, "main.conf")
                if os.path.isfile(path):
                    return path
    except OSError:
        pass
    return None


def replace_motd_block(text, entries_text):
    """Swap the motd=[...] list in a HOCON config, leaving the rest alone.

    Only the list is touched, so every other setting keeps whatever the
    admin set it to. Brackets are counted rather than matched by regex —
    the list nests one level of braces per entry.
    """
    for key in ("motds=[", "motds = [", "motd=[", "motd = ["):
        start = text.find(key)
        if start >= 0:
            keyword = key.split("=")[0]
            break
    else:
        return None
    open_at = text.index("[", start)
    depth = 0
    for i in range(open_at, len(text)):
        if text[i] == "[":
            depth += 1
        elif text[i] == "]":
            depth -= 1
            if depth == 0:
                return text[:start] + keyword + "=" + entries_text + text[i + 1:]
    return None


def render_motd_entries(pairs):
    """The HOCON list text for [(line1, line2), ...]."""
    blocks = []
    for line1, line2 in pairs:
        line1 = line1.replace('"', "'")
        line2 = line2.replace('"', "'")
        blocks.append("    {\n"
                      "        icon=random\n"
                      f"        line1=\"{line1}\"\n"
                      f"        line2=\"{line2}\"\n"
                      "    }")
    return "[\n" + ",\n".join(blocks) + "\n]"


# ------------------------------------------------------------- formatting --

def fmt_duration(seconds):
    seconds = int(max(0, seconds))
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    d, h = divmod(h, 24)
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m"
    return f"{s}s"


def fmt_distance(cm):
    km = cm / 100_000
    if km >= 1:
        return f"{km:,.1f} km"
    return f"{cm / 100:,.0f} m"


def fmt_number(n):
    return f"{int(n):,}"


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z"


def week_key(epoch):
    y, w, _ = dt.date.fromtimestamp(epoch).isocalendar()
    return f"{y}-W{w:02d}"


def week_label(epoch):
    d = dt.date.fromtimestamp(epoch)
    monday = d - dt.timedelta(days=d.isoweekday() - 1)
    return monday.strftime("Week of %b %d")


MEDALS = ["🥇", "🥈", "🥉"]


def rank_lines(pairs, fmt, limit=15, empty="*nothing recorded yet*"):
    pairs = [p for p in pairs if p[1]]
    if not pairs:
        return f"> 💤  {empty}"
    pairs.sort(key=lambda kv: -kv[1])
    lines = []
    for i, (name, value) in enumerate(pairs[:limit]):
        badge = MEDALS[i] if i < 3 else f"`{i + 1}.`"
        lines.append(f"> {badge}  **{name}** — {fmt(value)}")
    if len(pairs) > limit:
        lines.append(f"> *…and {len(pairs) - limit} more*")
    return "\n".join(lines)


# ----------------------------------------------------------------- embeds --

def status_embed(state, status, stats, now):
    """The live card: who is on right now, and for how long."""
    address = CFG["public_address"] or f"{CFG['host']}:{CFG['port']}"
    if not status:
        return {
            "title": "🔴  Server Offline",
            "description": f"`{address}` is not responding.",
            "color": 0xED4245,
            "footer": {"text": "last checked"},
            "timestamp": now_iso(),
        }
    players = status.get("players", {})
    count = len(state["players"]) or players.get("online", 0)
    maximum = players.get("max", "?")
    if state["players"]:
        rows = sorted(state["players"].items(), key=lambda kv: kv[1])
        body = "\n".join(
            f"> 🎮  **{n}**  ·  on for {fmt_duration(now - start)}"
            for n, start in rows)
    elif players.get("online", 0):
        body = f"> 🎮  {players['online']} online"
    else:
        body = "> 💤  *No players online*"

    details = []
    version = status.get("version", {}).get("name")
    if version:
        details.append(f"Minecraft {version}")
    if state.get("server_start"):
        details.append(f"up {fmt_duration(now - state['server_start'])}")
    if status.get("_latency_ms") is not None:
        details.append(f"{status['_latency_ms']} ms")
    last_deaths = state.get("fun", {}).get("last_death", {})
    if last_deaths:
        # The factory-floor safety sign, but for creepers.
        quiet = now - max(last_deaths.values())
        if quiet >= 86400:
            details.append(f"☠️ {int(quiet // 86400)}d death-free")
    if CFG["external_check"] and state.get("external_ok") is False:
        details.append("⚠️ unreachable from the internet")

    motd = describe_motd(status.get("description", "")).strip()
    header = f"### Players Online — {count}/{maximum}"
    return {
        "title": "🟢  Server Online",
        "description": f"{header}\n{body}",
        "color": 0x57F287,
        "footer": {"text": f"{address}  •  " + "  •  ".join(details) + "  •  last checked"
                   if details else f"{address}  •  last checked"},
        "timestamp": now_iso(),
        **({"author": {"name": motd}} if motd else {}),
    }


def weekly_embed(state, playtimes, now, final=False):
    base = state["week_baseline"]
    pairs = [(n, max(0, t - base.get(n, t))) for n, t in playtimes.items()]
    label = week_label(now - 7 * 86400 if final else now)
    return {
        "title": "🏁  Final Standings" if final else "🏆  Weekly Playtime",
        "description": f"### {label}\n" + rank_lines(pairs, fmt_duration,
                                                     empty="*no playtime this week yet*"),
        "color": 0xF1C40F,
        "footer": {"text": "resets Monday  •  from the server's own play_time"},
        "timestamp": now_iso(),
    }


DAY_FOOTER = "resets at 3:00 am Eastern  •  from the server's own play_time"


def daily_playtime_embed(state, playtimes, now, final=False):
    """Who played today, counted from the 03:00 Eastern baseline."""
    base = state["day_playtime_baseline"]
    pairs = [(n, max(0, t - base.get(n, t))) for n, t in playtimes.items()]
    return {
        "title": "🌅  Yesterday's Playtime" if final else "📅  Today's Playtime",
        "description": f"### {day_label(now - 86400 if final else now)}\n"
                       + rank_lines(pairs, fmt_duration,
                                    empty="*nobody has played today yet*"),
        "color": 0xE67E22,
        "footer": {"text": DAY_FOOTER},
        "timestamp": now_iso(),
    }


def daily_overview_embed(state, criteria, deltas, playtimes, now):
    """Today at a glance: what the whole server did, and who is ahead.

    The category tables below this card show ten rows each; this one is where
    the breadth lives — every headline total, and how much moved at all.
    """
    base = state["day_playtime_baseline"]
    played = {n: max(0, t - base.get(n, t)) for n, t in playtimes.items()}
    active = [n for n, seconds in played.items() if seconds > 0]

    tiles = gamestats.summary_totals(criteria, deltas)
    totals = "\n".join(f"> {emoji}  **{value}**  ·  {label}"
                       for emoji, label, value in tiles)

    rows = gamestats.leaders(criteria, deltas, CFG["stats_noise_scale"])
    wins = {}
    for row in rows:
        wins[row[1]] = wins.get(row[1], 0) + 1
    ranked = sorted(wins.items(), key=lambda kv: -kv[1])[:5]
    ahead = "\n".join(
        f"> {MEDALS[i] if i < 3 else f'`{i + 1}.`'}  **{name}** — "
        f"leads **{count}** of today's statistics"
        for i, (name, count) in enumerate(ranked))

    if not tiles and not ranked:
        description = f"### {day_label(now)}\n> 💤  *nothing recorded today yet*"
    else:
        description = (f"### {day_label(now)}\n"
                       + (f"**Today's totals**\n{totals}\n" if totals else "")
                       + (f"\n**Leading today**\n{ahead}\n" if ahead else "")
                       + f"\n**{len(rows):,}** statistics moved today, across "
                         f"**{len(active)}** player{'s' if len(active) != 1 else ''}.")
    return {
        "title": "🌞  Today at a Glance",
        "description": description[:4096],
        "color": 0xF39C12,
        "footer": {"text": DAY_FOOTER},
        "timestamp": now_iso(),
    }


def alltime_embed(playtimes, prev_ranks=None, pace=()):
    """Lifetime hours, with movement arrows against yesterday's ranking."""
    ranked = sorted((kv for kv in playtimes.items() if kv[1]),
                    key=lambda kv: -kv[1])
    lines = []
    for i, (name, seconds) in enumerate(ranked[:20]):
        badge = MEDALS[i] if i < 3 else f"`{i + 1}.`"
        arrow = ""
        if prev_ranks and prev_ranks.get(name) not in (None, i + 1):
            moved = prev_ranks[name] - (i + 1)
            arrow = f"  ·  {'▲' if moved > 0 else '▼'}{abs(moved)}"
        lines.append(f"> {badge}  **{name}** — {fmt_duration(seconds)}{arrow}")
    body = "\n".join(lines) if lines else "> 💤  *nothing recorded yet*"
    if pace:
        body += "\n\n**On pace**\n" + "\n".join(pace)
    return {
        "title": "👑  All-Time Hours",
        "description": body,
        "color": 0x9B59B6,
        "footer": {"text": "lifetime totals, straight from the world save"
                           "  •  arrows vs yesterday"},
        "timestamp": now_iso(),
    }


# ------------------------------------------------------------------ state --

def new_state():
    return {
        "schema": 2,
        "online": None,
        "players": {},             # name -> session start epoch
        "play_time_at_join": {},   # name -> all-time seconds when they joined
        "week": None,
        "week_baseline": {},       # name -> all-time seconds at week start
        "stat_baseline": {},       # name -> {criterion: value} at week start
        "day": None,               # the statistics day, rolling at 03:00 Eastern
        "day_playtime_baseline": {},  # name -> all-time seconds at 03:00
        "day_stat_baseline": {},   # name -> {criterion: value} at 03:00
        "daily_layout": [],        # the daily channel's cards, in channel order
        "msg": {},                 # card name -> Discord message id
        "msg_stale": {},           # card name -> ids still to be cleaned up
        "record_players": 0,
        "milestones": {},
        "milestones_initialised": False,
        "server_start": None,
        "external_ok": None,
        "down_since": None,
        "perf": {},                # performance history and the uptime ledger
        "fun": {},                 # awards, records, streaks, races, chat counts
    }


FUN_DEFAULTS = {
    "seeded": False,          # the one-time log-archive replay has run
    "world_first_day": "",    # oldest date in the log archive
    "hours": [0.0] * 24,      # player-seconds by local clock hour
    "chat_total": {},         # name -> lifetime chat messages
    "chat_today": {},         # name -> messages since the 03:00 reset
    "records": {},            # single-day record ledger
    "streaks": {},            # name -> {current, best, last}
    "history": [],            # last 30 days of {day, played, chat}
    "ranks": {},              # yesterday's all-time playtime ranks, for arrows
    "last_death": {},         # name -> epoch of their latest death
    "diamond_base": {},       # name -> diamond ore mined, for find announcements
    "race": {},               # stat -> current all-time leader
    "race_hot": {},           # "stat:pair" -> when that close race was last called
    "annv": [],               # world anniversaries already announced
    "mvp": "",                # yesterday's Most Dedicated, for the MOTD
    "motd_sig": "",           # what the MOTD currently says, to skip rewrites
    "fact_recent": [],        # who the fun facts featured lately, newest last
    "fact_kinds": [],         # which kinds of fact were told lately
    "fact_history": [],       # [epoch, name, kind] — nothing repeats in an hour
    "next_fact": 0.0,         # when the next fact is due — survives restarts
}


def fun_state(state):
    fun = state.setdefault("fun", {})
    for key, default in FUN_DEFAULTS.items():
        fun.setdefault(key, default if not isinstance(default, (dict, list))
                       else type(default)(default))
    return fun


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            state = json.load(f)
    except (OSError, json.JSONDecodeError):
        return new_state()
    base = new_state()
    base.update(state)
    return base


def save_state(state):
    # A dry run must not persist anything: it may well be running alongside the
    # real watcher, which owns this file.
    if DRY_RUN:
        return
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_FILE)


# ------------------------------------------------------------ event engine --

class Bot:
    def __init__(self):
        self.state = load_state()
        self.fun = fun_state(self.state)
        self.reader = LogReader(LOG_PATH)
        self.stats = {}
        self.last_loop = time.time()   # for the prime-time hour buckets
        self.dirty = False          # an event happened: refresh cards, reposition
        self.last_ping = 0.0
        self.last_stats = 0.0
        self.last_status_refresh = 0.0
        self.last_leaderboards = 0.0
        self.last_external = 0.0
        self.last_perf_sample = 0.0
        self.last_perf_card = 0.0
        self.last_stat_cards = 0.0
        self.last_daily_cards = 0.0
        self.last_motd = 0.0
        self.status = None
        # The datapacks' objectives are a curated goal list, used to say which
        # goals are still unmet. What gets *shown* comes from the save itself,
        # which records every statistic whether a datapack names it or not.
        self.curated = gamestats.load_criteria(CFG["server_dir"])
        self.criteria = []
        self.criterion_values = {}
        self.perf = None
        if CFG["webhook_perf"]:
            if not perf.AVAILABLE:
                log("[warn] psutil is not installed — the performance card will "
                    "show server metrics only. Fix with: pip install psutil")
            self.perf = perf.Monitor(
                self.state.setdefault("perf", {}),
                CFG["server_dir"],
                heap_max_bytes=perf.detect_heap_max(CFG["server_dir"]),
                log=log,
            )

    # -- log events -------------------------------------------------------

    def refresh_criteria(self):
        """Rebuild the statistic set from the save, plus the curated goals.

        The save only stores non-zero entries, so this grows as players do new
        things. The curated list is merged in so unmet goals still get named.
        """
        observed = gamestats.observed_criteria(self.stats)
        seen, merged = set(), []
        for criterion in list(observed) + list(self.curated):
            resolved = gamestats.split_criterion(criterion)
            if resolved and resolved not in seen:
                seen.add(resolved)
                merged.append(criterion)
        before = len(self.criteria)
        self.criteria = merged
        self.criterion_values = criterion_values(self.stats, self.criteria)
        if CFG["webhook_stats"] and len(merged) != before:
            log(f"[stats] {len(merged)} statistics recorded "
                f"({len(observed)} in the world save, "
                f"{len(self.curated)} datapack goals)")

    def migrate_layout(self):
        """Re-lay-out a channel whose set of cards has changed.

        Cards are edited in place, so their order in the channel is fixed when
        they are first posted. Changing which cards belong there means deleting
        them once and letting them come back in the new order.
        """
        if self.state.get("layout") == LAYOUT_VERSION or DRY_RUN:
            return
        for key in ("weekly", "alltime", "stats", "weekstats",
                    "records", "streaks", "primetime", "chatstats"):
            mid = self.state["msg"].pop(key, None)
            if mid and CFG["webhook_weekly"]:
                _delete_message(CFG["webhook_weekly"], mid)
        # Statistic cards gain and lose pages as categories grow, and new pages
        # would otherwise land at the bottom of the channel instead of after
        # the category they belong to.
        for key in [k for k in list(self.state["msg"])
                    if k.startswith("stat_") or k.startswith("daily_")]:
            mid = self.state["msg"].pop(key)
            url = webhook_for(key)
            if mid and url:
                time.sleep(0.3)
                _delete_message(url, mid)
        self.state["layout"] = LAYOUT_VERSION
        log("[card] re-laid out the leaderboard and statistics channels",
            mirror=True)

    def sweep(self):
        """Clear out any duplicate cards left behind by an earlier run."""
        self.migrate_layout()
        if DRY_RUN:
            return
        # Sweep every card the bot has ever recorded, not a fixed list: the
        # statistic channel's cards are named dynamically, and leaving them out
        # meant their orphans were never collected.
        for key in list(self.state.get("msg_stale", {})):
            url = webhook_for(key)
            if url:
                sweep_stale(url, key, self.state)

    def replay(self):
        """Rebuild the online set and session starts from latest.log, silently."""
        state = self.state
        state["players"] = {}
        state["server_start"] = None
        for when, msg in read_whole_log(LOG_PATH):
            if START_RE.match(msg):
                state["server_start"] = when
                state["players"] = {}
                continue
            if STOP_RE.match(msg):
                state["players"] = {}
                continue
            m = JOIN_RE.match(msg)
            if m:
                state["players"][m.group(1)] = when
                continue
            m = LEAVE_RE.match(msg)
            if m:
                state["players"].pop(m.group(1), None)
        self.stats = load_stats()
        for name in state["players"]:
            state["play_time_at_join"][name] = max(
                0, self.stats.get(name, {}).get("play_time", 0)
                - (time.time() - state["players"][name]))
        log(f"[replay] {len(state['players'])} online: "
            f"{', '.join(sorted(state['players'])) or '(nobody)'}")

    def seed_engagement(self):
        """One archive replay to backfill what the live loop tracks from now on.

        The prime-time buckets, the chat counts and the world's first day all
        have history sitting in the log archive; without this pass they would
        all start from zero and take weeks to become interesting.
        """
        fun = self.fun
        if fun["seeded"]:
            return
        events = read_all_logs()
        sessions = {}

        def credit(start, end):
            # Split a session across the clock hours it touched.
            moment = start
            while moment < end:
                boundary = (moment // 3600 + 1) * 3600
                chunk = min(boundary, end) - moment
                fun["hours"][time.localtime(moment).tm_hour] += chunk
                moment += chunk

        for when, msg in events:
            if START_RE.match(msg) or STOP_RE.match(msg):
                for start in sessions.values():
                    credit(start, when)
                sessions.clear()
                continue
            m = JOIN_RE.match(msg)
            if m:
                sessions[m.group(1)] = when
                continue
            m = LEAVE_RE.match(msg)
            if m:
                start = sessions.pop(m.group(1), None)
                if start is not None:
                    credit(start, when)
                continue
            m = CHAT_RE.match(msg)
            if m:
                name = m.group(1)
                fun["chat_total"][name] = fun["chat_total"].get(name, 0) + 1
        if events:
            fun["world_first_day"] = dt.date.fromtimestamp(events[0][0]).isoformat()
        else:
            fun["world_first_day"] = stat_day(time.time()).isoformat()
        fun["seeded"] = True
        log(f"[fun] seeded from the archive: "
            f"{sum(fun['hours']) / 3600:,.0f} player-hours, "
            f"{sum(fun['chat_total'].values()):,} chat messages, "
            f"world began {fun['world_first_day']}")

    def handle(self, msg, now):
        state = self.state

        m = JOIN_RE.match(msg)
        if m:
            name = m.group(1)
            state["players"][name] = now
            state["play_time_at_join"][name] = self.stats.get(name, {}).get("play_time", 0)
            count = len(state["players"])
            total = live_playtime(self.stats, state, name, now)
            extra = f" — {fmt_duration(total)} all-time" if total >= 60 else " — first time here! 👋"
            chat_say(f"➡️  **{name}** joined the game  ·  *{count} online*")
            say(f":arrow_right: **{name}** joined the server ({count} online){extra}")
            self.dirty = True
            self.check_record(count)
            return

        m = LEAVE_RE.match(msg)
        if m:
            name = m.group(1)
            start = state["players"].pop(name, None)
            state["play_time_at_join"].pop(name, None)
            count = len(state["players"])
            played = fmt_duration(now - start) if start else "an unknown time"
            chat_say(f"⬅️  **{name}** left the game  ·  *on for {played}, "
                     f"{count} online*")
            say(f":arrow_left: **{name}** left the server ({count} online) "
                f"— was on for **{played}**")
            self.dirty = True
            return

        if START_RE.match(msg):
            state["server_start"] = now
            state["players"] = {}
            chat_say("🟢  *the server finished starting up*")
            if state["online"] is False:
                say(f":green_circle: **Server UP** — `{CFG['public_address'] or CFG['host']}` "
                    f"is back online.")
            state["online"] = True
            state["down_since"] = None
            self.dirty = True
            return

        if STOP_RE.match(msg):
            log("[event] server is stopping", mirror=True)
            chat_say("🔴  *the server is shutting down*")
            state["players"] = {}
            self.dirty = True
            return

        m = LAG_RE.search(msg)
        if m:
            ticks = LAG_TICKS_RE.search(msg)
            behind = int(m.group(1))
            if self.perf:
                self.perf.record_lag(now, behind, int(ticks.group(1)) if ticks else 0)
            log(f"[perf] server fell {behind} ms behind", mirror=True)
            return

        m = ADV_RE.match(msg)
        if m:
            name, adv = m.group(1), m.group(2)
            chat_say(f"🏅  **{name}** earned the advancement **[{adv}]**")
            if adv in RARE_ADVANCEMENTS:
                events_say(f"🌟  **{name}** just earned **[{adv}]** — "
                           f"one of the hardest advancements in the game!")
            if CFG["announce_advancements"] and name in state["players"]:
                say(f":medal: **{name}** earned the advancement **[{adv}]**")
                self.dirty = True
            return

        m = CHAT_RE.match(msg)
        if m:
            name = m.group(1)
            self.fun["chat_total"][name] = self.fun["chat_total"].get(name, 0) + 1
            self.fun["chat_today"][name] = self.fun["chat_today"].get(name, 0) + 1
            chat_say(f"💬  **{name}**: {m.group(2)[:1500]}")
            if CFG["relay_chat"]:
                say(f"💬  **{name}**: {m.group(2)[:1500]}")
            return

        # Anything else that starts with the name of somebody currently online
        # and is not a known non-death line is a death message.
        first = msg.split(" ", 1)[0]
        if (first in state["players"] and not NOT_DEATH_RE.match(msg)
                and len(msg) > len(first) + 1):
            # The obituary: their death count, and how long they lasted.
            last = self.fun["last_death"].get(first)
            self.fun["last_death"][first] = now
            context = []
            deaths = self.stats.get(first, {}).get("deaths", 0)
            if deaths:
                context.append(f"death #{deaths + 1}")
            if last:
                context.append(f"first in {fmt_duration(now - last)}")
            suffix = f"  ·  *{', '.join(context)}*" if context else ""
            chat_say(f"💀  {msg}{suffix}")
            if CFG["announce_deaths"]:
                say(f":skull: {msg}")
                self.dirty = True

    def check_record(self, count):
        state = self.state
        if count > state["record_players"]:
            if count >= 2:
                previous = (f" (previous best: {state['record_players']})"
                            if state["record_players"] >= 2 else "")
                say(f":tada: **New record!** {count} players online at once{previous}")
            state["record_players"] = count

    # -- periodic work ----------------------------------------------------

    def do_ping(self, now):
        self.status = ping_server()
        state = self.state
        if self.status:
            if state["online"] is False:
                outage = now - state["down_since"] if state["down_since"] else None
                say(f":green_circle: **Server UP** — "
                    f"`{CFG['public_address'] or CFG['host']}` is responding again.")
                perf_say(f":green_circle: **Recovered** after "
                         f"**{fmt_duration(outage)}** of downtime."
                         if outage else ":green_circle: **Recovered.**")
                self.dirty = True
            state["online"] = True
            state["down_since"] = None
            return
        # Ping failed.
        if state["down_since"] is None:
            state["down_since"] = now
        elif (state["online"] is not False
              and now - state["down_since"] >= CFG["down_after_seconds"]):
            mention = (CFG["down_mention"] + " ") if CFG["down_mention"] else ""
            say(f"{mention}:red_circle: **Server DOWN** — "
                f"`{CFG['public_address'] or CFG['host']}` is not responding.")
            perf_say(f":red_circle: **Outage began** at "
                     f"{time.strftime('%H:%M:%S', time.localtime(state['down_since']))} "
                     f"— the server stopped responding.")
            state["online"] = False
            state["players"] = {}
            self.dirty = True

    def do_external_check(self):
        """Confirm the server is reachable from outside, not just on localhost."""
        address = CFG["public_address"]
        probe = CFG["check_address"] or address
        if not probe:
            return
        host, _, port = probe.partition(":")
        reachable = ping_server(host, int(port) if port else 25565, timeout=8) is not None
        was = self.state["external_ok"]
        self.state["external_ok"] = reachable
        if was is True and not reachable:
            say(":warning: The server is running but **cannot be reached from the "
                f"internet** at `{address}` — check port forwarding.")
            self.dirty = True
        elif was is False and reachable:
            say(f":white_check_mark: `{address}` is reachable from the internet again.")
            self.dirty = True

    def do_perf(self, now):
        """Sample the machine, raise alerts, and keep the performance card fresh."""
        if not self.perf:
            return
        if now - self.last_perf_sample >= CFG["perf_sample_seconds"]:
            self.last_perf_sample = now
            latency = self.status.get("_latency_ms") if self.status else None
            # The server's own tick cost, if RCON will answer. Skipped while
            # the ping says the server is down — no point knocking.
            tick = rcon.query_tick(CFG["server_dir"]) if self.status else None
            self.perf.sample(now, latency, len(self.state["players"]), tick=tick)
            if CFG["perf_alerts"]:
                for message in self.perf.check_alerts(now, {
                    "host_cpu": CFG["alert_host_cpu"],
                    "host_ram": CFG["alert_host_ram"],
                    "disk_free_gb": CFG["alert_disk_free_gb"],
                    "heap_pct": CFG["alert_heap_pct"],
                }):
                    perf_say(message)
            if CFG["daily_report"]:
                finished = self.perf.rolled_over_day()
                if finished and not DRY_RUN and CFG["webhook_perf"]:
                    try:
                        discord(CFG["webhook_perf"], "POST", {
                            "username": "MC Performance",
                            "embeds": [self.perf.daily_embed(finished, now)]})
                        log(f"[perf] posted the daily report for {finished['day']}",
                            mirror=True)
                    except (urllib.error.URLError, OSError) as e:
                        log(f"[error] daily report post failed: {e}")

        # Wait for a second sample: rates and sparklines are differences
        # between readings, so a card drawn from one reading looks empty.
        if (now - self.last_perf_card >= CFG["perf_card_seconds"]
                and len(self.perf.history) >= 2):
            self.last_perf_card = now
            address = CFG["public_address"] or f"{CFG['host']}:{CFG['port']}"
            upsert_embed(CFG["webhook_perf"], "perf", self.state,
                         self.perf.embed(now, address))

    def roll_week(self, playtimes, now):
        """Post final standings and reset the weekly baseline every Monday."""
        state = self.state
        key = week_key(now)
        if state["week"] == key:
            return False
        first_run = state["week"] is None
        if state["week"] and state["week_baseline"] and CFG["webhook_weekly"]:
            final = weekly_embed(state, playtimes, now, final=True)
            if DRY_RUN:
                log("[dry-run] final standings")
            else:
                try:
                    discord(CFG["webhook_weekly"], "POST",
                            {"username": "MC Server", "embeds": [final]})
                    log(f"[weekly] posted final standings for {state['week']}", mirror=True)
                except (urllib.error.URLError, OSError) as e:
                    log(f"[error] final standings post failed: {e}")
        if first_run:
            # Starting partway through a week: recover what has already been
            # played since Monday from the log archive, so the board is not
            # blank until the next reset.
            played = playtime_since(week_start_epoch(now))
            state["week_baseline"] = {n: max(0, t - played.get(n, 0))
                                      for n, t in playtimes.items()}
            log(f"[weekly] seeded this week from the logs: "
                f"{ {n: fmt_duration(s) for n, s in played.items()} }")
        else:
            state["week_baseline"] = dict(playtimes)
        # Statistics have no equivalent of the log archive to recover from, so
        # the week's counts necessarily start from wherever they stand now.
        state["stat_baseline"] = {n: dict(v) for n, v in self.criterion_values.items()}
        state["week"] = key
        state["msg"].pop("weekly", None)
        return True

    DIAMOND_CRITERIA = ("minecraft.mined:minecraft.diamond_ore",
                        "minecraft.mined:minecraft.deepslate_diamond_ore")
    # The races worth narrating: name -> how to read each player's number.
    RACE_STATS = [
        ("all-time playtime", "playtime", fmt_duration),
        ("all-time blocks mined", "mined", fmt_number),
        ("all-time mob kills", "killed", fmt_number),
        ("advancements", "advancements", fmt_number),
    ]

    def check_fun(self, now):
        """Diamond finds and lead changes — checked after every stats refresh."""
        fun = self.fun

        # Diamond watch. Stats files only save every few minutes, so this is
        # near-time rather than real-time; still worth a cheer.
        for name, values in self.criterion_values.items():
            total = sum(values.get(c, 0) for c in self.DIAMOND_CRITERIA)
            base = fun["diamond_base"].get(name)
            if base is not None and total > base:
                found = total - base
                events_say(f"💎  **{name}** mined **{found}** diamond ore "
                           f"({total:,} all-time)")
            fun["diamond_base"][name] = total

        # Race watch: announce when the #1 spot in a headline stat changes
        # hands, and call a race "hot" when #2 closes within 3%.
        agg = funstats.aggregate(self.criteria, self.criterion_values)
        playtimes = all_playtimes(self.stats, self.state, now)
        for label, stat, fmt in self.RACE_STATS:
            if stat == "playtime":
                scores = playtimes
            elif stat == "advancements":
                scores = {n: e.get("advancements", 0) for n, e in self.stats.items()}
            else:
                scores = {n: a.get(stat, 0) for n, a in agg.items()}
            ranked = sorted((kv for kv in scores.items() if kv[1] > 0),
                            key=lambda kv: -kv[1])
            if not ranked:
                continue
            leader, top = ranked[0]
            was = fun["race"].get(stat)
            fun["race"][stat] = leader
            if was and was != leader and was in scores:
                events_say(f"🏁  **{leader}** has overtaken **{was}** for #1 in "
                           f"**{label}** — {fmt(top)} to {fmt(scores[was])}!")
            elif len(ranked) > 1:
                chaser, second = ranked[1]
                if top and (top - second) / top < 0.03:
                    key = f"{stat}:{leader}:{chaser}"
                    if now - fun["race_hot"].get(key, 0) > 7 * 86400:
                        fun["race_hot"][key] = now
                        events_say(f"🔥  **The race is on!** {chaser} is within "
                                   f"{fmt(top - second)} of {leader} in {label} "
                                   f"({fmt(second)} vs {fmt(top)}).")

    # Readable on the dark chat background; picked at random per fact.
    FACT_COLORS = ("aqua", "green", "yellow", "gold", "light_purple",
                   "red", "blue", "dark_aqua", "white")

    # What the tab list can show: label, and where each player's number
    # comes from. "agg" keys read the lifetime aggregate; callables read
    # anything else. Values are lifetime, like the facts.
    TAB_STATS = [
        ("blocks mined", "mined", "{:,.0f}"),
        ("deaths", "deaths", "{:,.0f}"),
        ("hours played",
         lambda self, agg, name: self.stats.get(name, {}).get("play_time", 0) / 3600,
         "{:,.0f}"),
        ("mob kills", "killed", "{:,.0f}"),
        ("km travelled",
         lambda self, agg, name: agg.get(name, {}).get("distance", 0) / 100_000,
         "{:,.1f}"),
        ("blocks placed", "placed", "{:,.0f}"),
        ("jumps", "jumps", "{:,.0f}"),
        ("diamond ore",
         lambda self, agg, name: sum(
             self.criterion_values.get(name, {}).get(c, 0)
             for c in Bot.DIAMOND_CRITERIA),
         "{:,.0f}"),
        ("fish caught", "fish", "{:,.0f}"),
        ("villager trades", "trades", "{:,.0f}"),
    ]

    def rotate_tab(self, online):
        """Show the next statistic in the tab list, labelled on every row.

        A dummy objective sits in the tab-list slot; each rotation rewrites
        every online player's score with a fixed number format like
        "1,234 blocks mined", so the tab says what it is showing.
        """
        if not CFG["tab_stats"] or not online:
            return
        agg = funstats.aggregate(self.criteria, self.criterion_values)
        index = self.fun.get("tab_index", 0) % len(self.TAB_STATS)
        self.fun["tab_index"] = index + 1
        label, source, fmt = self.TAB_STATS[index]
        cmds = ["scoreboard objectives add mcbot_tab dummy"]
        for name in online:
            if callable(source):
                value = source(self, agg, name)
            else:
                value = agg.get(name, {}).get(source, 0)
            shown = fmt.format(value)
            cmds.append(f"scoreboard players set {name} mcbot_tab {int(value)}")
            cmds.append(f"scoreboard players display numberformat {name} "
                        f'mcbot_tab fixed "{shown} {label}"')
        cmds.append("scoreboard objectives setdisplay list mcbot_tab")
        if rcon.commands(CFG["server_dir"], cmds) is not None:
            log(f"[tab] now showing {label}")

    def maybe_fun_fact(self, now):
        """Every so often, tell the room something true about somebody in it.

        Fires at jittered moments averaging fun_facts_per_hour, and only
        when somebody is actually online to be told about — an empty server
        is told nothing. Both the featured player and the *kind* of fact
        rotate: the last two players and the last six kinds are skipped, so
        it neither dwells on one person nor keeps reciting jump counts.
        """
        if not CFG["fun_facts_per_hour"] or DRY_RUN:
            return
        fun = self.fun
        average = 3600 / CFG["fun_facts_per_hour"]
        # The schedule is persisted, so a restart neither fires a fact early
        # nor re-rolls the countdown — the cadence holds across restarts.
        if not fun["next_fact"]:
            fun["next_fact"] = now + random.uniform(average * 0.6, average * 1.4)
            return
        if now < fun["next_fact"]:
            return
        fun["next_fact"] = now + random.uniform(average * 0.6, average * 1.4)
        online = [n for n in self.state["players"] if n in self.stats]
        if not online:
            return
        # The tab list rotates on the same beat, even when every fact has
        # already been told this hour.
        self.rotate_tab(online)
        # No fact repeats within an hour: everything said in the last hour
        # (player + kind) is off the table.
        fun["fact_history"] = [h for h in fun["fact_history"] if h[0] >= now - 3600]
        said = {(h[1], h[2]) for h in fun["fact_history"]}
        # Prefer whoever has not featured in the last two facts. The very
        # last subject is excluded outright unless they are alone on the
        # server — but a recent subject may still be chosen when only they
        # have something unsaid left.
        recent = fun["fact_recent"]
        last = recent[-1] if recent else ""
        if len(online) == 1:
            pool = list(online)
        else:
            preferred = [n for n in online if n not in recent[-2:]]
            backup = [n for n in online if n not in preferred and n != last]
            pool = (random.sample(preferred, len(preferred))
                    + random.sample(backup, len(backup)))
        for name in pool:
            facts = funstats.fun_facts(
                name, self.stats[name],
                chat_total=fun["chat_total"].get(name, 0),
                streak=fun["streaks"].get(name, {}).get("current", 0))
            available = [f for f in facts if (name, f[0]) not in said]
            if available:
                break
        else:
            log("[fact] everything about everybody online was already said "
                "this hour — staying quiet")
            return
        unworn = [f for f in available if f[0] not in fun["fact_kinds"][-6:]]
        kind, fact = random.choice(unworn or available)
        if rcon.broadcast(CFG["server_dir"], f"Did you know? {fact}",
                          color=random.choice(self.FACT_COLORS)) is None:
            return  # nobody heard it — keep it out of the transcript too
        fun["fact_recent"] = (fun["fact_recent"] + [name])[-4:]
        fun["fact_kinds"] = (fun["fact_kinds"] + [kind])[-8:]
        fun["fact_history"].append([now, name, kind])
        chat_say(f"✨  *Did you know? {fact}*")
        log(f"[fact] ({kind}) {fact}")

    def roll_day(self, playtimes, now):
        """The 3:00 am Eastern ceremony: recap, awards, records, streaks.

        Everything about the finished day is computed before the baselines
        reset — after that its deltas are gone.
        """
        state = self.state
        fun = self.fun
        key = day_key(now)
        if state["day"] == key:
            return False
        first_run = state["day"] is None

        if not first_run and state["day_playtime_baseline"]:
            finished = dt.date.fromisoformat(state["day"])
            label = finished.strftime("%A, %b %d")
            base = state["day_playtime_baseline"]
            played = {n: max(0, t - base.get(n, t)) for n, t in playtimes.items()}
            deltas = subtract(self.criterion_values, state["day_stat_baseline"])
            agg = funstats.aggregate(self.criteria, deltas)
            for name in set(played) | set(fun["chat_today"]):
                agg.setdefault(name, {})
                agg[name]["playtime"] = played.get(name, 0)
                agg[name]["chat"] = fun["chat_today"].get(name, 0)

            awards = funstats.compute_awards(agg, state["day"])
            fun["mvp"] = next((winner for _, title, winner, _, _ in awards
                               if title == "Most Dedicated"), "")
            # The recap bundle: one post, one notification at most.
            recap = [
                daily_playtime_embed(state, playtimes, now, final=True),
                gamestats.period_embed(
                    self.criteria, deltas,
                    "🌙  Yesterday's Statistics", label,
                    0xE67E22, "the day that just ended  •  resets at 3:00 am Eastern",
                    CFG["stats_noise_scale"],
                    empty="nothing was recorded yesterday"),
                funstats.awards_embed(awards, label),
            ]
            url = CFG["webhook_events"] or CFG["webhook_daily"]
            if DRY_RUN:
                log("[dry-run] daily recap, awards and challenge result")
            elif url:
                try:
                    discord(url, "POST", {"username": "MC Server", "embeds": recap})
                    log(f"[daily] posted the recap for {state['day']}", mirror=True)
                except (urllib.error.URLError, OSError) as e:
                    log(f"[error] daily recap post failed: {e}")

            # The record ledger and the streak ledger both advance one day.
            for emoji, rec_label, name, value, was, was_value in \
                    funstats.check_records(fun["records"], agg,
                                           finished.strftime("%b %d")):
                own = " their own record of" if was == name else f" {was}'s"
                events_say(f"{emoji}  **Daily record!** {name} set a new best "
                           f"for **{rec_label.lower()}** — **{value}**, "
                           f"beating{own} {was_value}.")
            milestones, broken = funstats.update_streaks(fun["streaks"],
                                                         played, finished)
            for name, length in milestones:
                events_say(f"🔥  **{name}** is on a **{length}-day** playtime streak!")
            for name, length in broken:
                events_say(f"💔  **{name}**'s {length}-day streak has ended.")

            # History for pace projections and the chat card, capped at 30 days.
            fun["history"].append({"day": state["day"], "played": played,
                                   "chat": dict(fun["chat_today"])})
            del fun["history"][:-30]

            # World birthdays.
            if self.world_start():
                age = (stat_day(now) - self.world_start()).days
                if ((age in ANNIVERSARY_DAYS or (age > 0 and age % 365 == 0))
                        and age not in fun["annv"]):
                    fun["annv"].append(age)
                    years = f" — {age // 365} year{'s' if age >= 730 else ''}!" \
                        if age % 365 == 0 else "!"
                    events_say(f"🎂  **The world is {age} days old today{years}** "
                               f"It began on {fun['world_first_day']}.")

        if first_run:
            # Starting partway through a day: recover the playtime already
            # earned since 03:00 from the log archive, the same way the weekly
            # board is seeded, so the card is not blank until tomorrow.
            played = playtime_since(day_start_epoch(now))
            state["day_playtime_baseline"] = {n: max(0, t - played.get(n, 0))
                                              for n, t in playtimes.items()}
            log(f"[daily] seeded today from the logs: "
                f"{ {n: fmt_duration(s) for n, s in played.items()} }")
        else:
            state["day_playtime_baseline"] = dict(playtimes)
        # Statistics have no log archive to recover from, so today's counts
        # necessarily start from wherever they stand right now.
        state["day_stat_baseline"] = {n: dict(v)
                                      for n, v in self.criterion_values.items()}
        # Yesterday's ranks, for the movement arrows on the all-time board.
        ranked = sorted((kv for kv in playtimes.items() if kv[1]),
                        key=lambda kv: -kv[1])
        fun["ranks"] = {name: i + 1 for i, (name, _) in enumerate(ranked)}
        fun["chat_today"] = {}
        state["day"] = key
        return True

    def check_milestones(self, playtimes):
        state = self.state
        if not state["milestones_initialised"]:
            # First run against real stats: record where everybody already is
            # instead of announcing years of history all at once.
            for name, secs in playtimes.items():
                crossed = [h for h in MILESTONE_HOURS if secs / 3600 >= h]
                if crossed:
                    state["milestones"][name] = max(crossed)
            state["milestones_initialised"] = True
            return
        for name, secs in playtimes.items():
            hours = secs / 3600
            crossed = [h for h in MILESTONE_HOURS
                       if hours >= h > state["milestones"].get(name, 0)]
            if crossed:
                top = max(crossed)
                say(f":military_medal: **{name}** has now played over "
                    f"**{top} hours** on the server!")
                state["milestones"][name] = top
                self.dirty = True

    def refresh_cards(self, now, reposition):
        state = self.state
        playtimes = all_playtimes(self.stats, state, now)

        if CFG["webhook_main"]:
            # The status card is only ever moved when the main channel also
            # carries event messages; otherwise it is edited in place forever,
            # which is what guarantees there is exactly one of it.
            upsert_embed(CFG["webhook_main"], "status", state,
                         status_embed(state, self.status, self.stats, now),
                         reposition=reposition and CFG["main_events"])
        if not CFG["webhook_weekly"]:
            return
        rolled = self.roll_week(playtimes, now)
        for name in playtimes:
            state["week_baseline"].setdefault(name, playtimes[name])
        # Seed any player the baseline has not seen yet — on the very first run
        # that is everybody, so the week counts from now rather than crediting
        # a lifetime of statistics to it.
        for name, values in self.criterion_values.items():
            state["stat_baseline"].setdefault(name, dict(values))
        url = CFG["webhook_weekly"]
        fun = self.fun
        pace = funstats.pace_lines(
            playtimes, [entry["played"] for entry in fun["history"][-7:]],
            MILESTONE_HOURS, dt.date.fromtimestamp(now))
        # All-time hours first, then this week, then the celebration cards.
        upsert_embed(url, "alltime", state,
                     alltime_embed(playtimes, fun["ranks"], pace),
                     reposition=rolled)
        upsert_embed(url, "weekly", state, weekly_embed(state, playtimes, now),
                     reposition=rolled)
        upsert_embed(url, "weekstats", state,
                     gamestats.weekly_embed(
                         self.criteria,
                         subtract(self.criterion_values, state["stat_baseline"]),
                         week_label(now), CFG["stats_noise_scale"]),
                     reposition=rolled)
        time.sleep(0.4)
        upsert_embed(url, "records", state,
                     funstats.records_embed(fun["records"]))
        upsert_embed(url, "streaks", state,
                     funstats.streaks_embed(fun["streaks"]))
        time.sleep(0.4)
        upsert_embed(url, "primetime", state,
                     funstats.primetime_embed(fun["hours"]))
        upsert_embed(url, "chatstats", state,
                     funstats.chat_embed(fun["chat_total"], fun["chat_today"]))

    def do_daily(self, now):
        """Roll the statistics day at 03:00 Eastern, and keep today's cards fresh."""
        if not CFG["webhook_daily"]:
            return
        state = self.state
        playtimes = all_playtimes(self.stats, state, now)
        rolled = self.roll_day(playtimes, now)
        # Seed anybody the baselines have not seen, so a player who first
        # appears mid-day is credited today's activity and not their lifetime.
        for name, seconds in playtimes.items():
            state["day_playtime_baseline"].setdefault(name, seconds)
        for name, values in self.criterion_values.items():
            state["day_stat_baseline"].setdefault(name, dict(values))
        if rolled or now - self.last_daily_cards >= CFG["daily_card_seconds"]:
            self.last_daily_cards = now
            self.refresh_daily_cards(now)
            save_state(state)
        if rolled or now - self.last_motd >= CFG["motd_refresh_seconds"]:
            self.last_motd = now
            self.refresh_motd(now)

    def refresh_stat_cards(self):
        """The all-time statistic channel: an overview plus one card a category."""
        url = CFG["webhook_stats"]
        if not url or not self.criteria:
            return
        state = self.state
        scale = CFG["stats_noise_scale"]
        wanted = ["stat_overview", "stat_spotlight", "stat_advrace"]
        upsert_embed(url, "stat_overview", state,
                     gamestats.overview_embed(self.criteria, self.criterion_values,
                                              len(self.criterion_values), scale))
        today = day_key(time.time())
        spotlight = funstats.pick_spotlight(
            [n for n, e in self.stats.items() if e.get("play_time", 0) >= 3600],
            today)
        if spotlight:
            time.sleep(0.4)
            upsert_embed(url, "stat_spotlight", state,
                         funstats.spotlight_embed(spotlight,
                                                  self.stats[spotlight], today))
        done_sets = load_advancement_sets(self.stats)
        if done_sets:
            time.sleep(0.4)
            upsert_embed(url, "stat_advrace", state,
                         funstats.advancements_embed(done_sets))
        for key, embed in gamestats.category_embeds(
                self.criteria, self.criterion_values, curated=self.curated,
                top=CFG["stats_top_per_category"], scale=scale):
            wanted.append(f"stat_{key}")
            # Space the updates out: a webhook allows roughly five requests
            # every two seconds, and this is a whole channel of cards at once.
            time.sleep(0.4)
            upsert_embed(url, f"stat_{key}", state, embed)
        self.drop_unused_cards(url, "stat_", wanted)

    def world_start(self):
        """The date the world began: configured, or the oldest archived log."""
        for value in (CFG["world_started"], self.fun["world_first_day"]):
            if value:
                try:
                    return dt.date.fromisoformat(str(value))
                except ValueError:
                    continue
        return None

    def motd_pairs(self, now):
        """[(line1, line2)] for the server list: the name and day, then a fact.

        MiniMOTD picks one entry at random per ping, so each fact is its own
        entry and every refresh of the server list shows a different one.
        """
        fun = self.fun
        name = "Minecraft Server"
        try:
            with open(os.path.join(CFG["server_dir"], "server.properties"),
                      encoding="utf-8") as f:
                for line in f:
                    if line.startswith("motd="):
                        stripped = MOTD_CODE_RE.sub("", line[5:].strip())
                        name = stripped or name
                        break
        except OSError:
            pass
        title = f"<gradient:#55ff88:#ffd75f><bold>{name}</bold></gradient>"
        start = self.world_start()
        if start:
            day_number = (dt.date.fromtimestamp(now) - start).days
            title += f"<gray> — <white>Day {day_number}</white></gray>"

        facts = []
        if fun["last_death"] and now - max(fun["last_death"].values()) >= 86400:
            quiet = int((now - max(fun["last_death"].values())) // 86400)
            facts.append(f"<green>{quiet} day{'s' if quiet != 1 else ''} death-free")
        else:
            deaths = sum(e.get("deaths", 0) for e in self.stats.values())
            if deaths:
                facts.append(f"<red>{deaths:,} deaths <gray>and counting")
        running = [(n, e["current"]) for n, e in fun["streaks"].items()
                   if e["current"] >= 2]
        if running:
            top, days = max(running, key=lambda kv: kv[1])
            facts.append(f"<light_purple>{top} is on a {days}-day streak")
        if fun["mvp"]:
            facts.append(f"<gold>yesterday's MVP: <white>{fun['mvp']}")
        hours = sum(e.get("play_time", 0) for e in self.stats.values()) / 3600
        if hours >= 1:
            facts.append(f"<green>{hours:,.0f} hours <gray>played together")
        # Lifetime fun stats, straight off the save.
        agg = funstats.aggregate(self.criteria, self.criterion_values)
        totals = {key: sum(a.get(key, 0) for a in agg.values())
                  for key in ("mined", "placed", "killed", "distance",
                              "fish", "trades")}
        diamonds = sum(sum(v.get(c, 0) for c in self.DIAMOND_CRITERIA)
                       for v in self.criterion_values.values())
        for value, line in (
                (totals["mined"], f"<aqua>{totals['mined']:,.0f} <gray>blocks mined"),
                (totals["placed"], f"<aqua>{totals['placed']:,.0f} <gray>blocks placed"),
                (totals["killed"], f"<red>{totals['killed']:,.0f} <gray>mobs slain"),
                (totals["distance"],
                 f"<yellow>{totals['distance'] / 100_000:,.0f} km <gray>travelled"),
                (diamonds, f"<blue>{diamonds:,.0f} <gray>diamond ore mined"),
                (totals["fish"], f"<dark_aqua>{totals['fish']:,.0f} <gray>fish caught"),
                (totals["trades"],
                 f"<gold>{totals['trades']:,.0f} <gray>villager trades")):
            if value:
                facts.append(line)
        for key, _, label, _, _, unit in funstats.RECORDS:
            entry = fun["records"].get(key)
            if entry:
                facts.append(f"<aqua>{label.lower()} in a day: "
                             f"{funstats.fmt_value(unit, entry['value'])} "
                             f"<gray>({entry['holder']})")
        if not facts:
            facts = ["<gray>a fine day for block games"]
        return [(title, fact) for fact in facts[:12]]

    def refresh_motd(self, now):
        """Restock MiniMOTD's rotation and ask the server to re-read it.

        Quietly does nothing until the mod's config exists; a reload that
        cannot be delivered (RCON off) still leaves the file ready for the
        server's next boot.
        """
        if not CFG["dynamic_motd"] or DRY_RUN:
            return
        path = find_minimotd_config(CFG["server_dir"])
        if not path:
            return
        pairs = self.motd_pairs(now)
        signature = json.dumps(pairs)
        if signature == self.fun["motd_sig"]:
            return
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read()
            replaced = replace_motd_block(text, render_motd_entries(pairs))
            if not replaced:
                log(f"[warn] no motd list found in {path}; leaving it alone")
                self.fun["motd_sig"] = signature  # do not retry every cycle
                return
            with open(path, "w", encoding="utf-8") as f:
                f.write(replaced)
        except OSError as e:
            log(f"[warn] could not update the MOTD config: {e}")
            return
        self.fun["motd_sig"] = signature
        delivered = rcon.command(CFG["server_dir"], "minimotd reload")
        log(f"[motd] refreshed with {len(pairs)} entries"
            + ("" if delivered is not None
               else " (reload pending — RCON unavailable)"))

    def refresh_daily_cards(self, now):
        """Today's statistics: the same categories, counted since 03:00 Eastern.

        Every card here is only ever edited in place — never deleted, never
        re-posted — so this channel produces no messages and no notifications.
        Categories that are empty this early in the day say so on their card
        rather than disappearing, which is what keeps the set stable.
        """
        url = CFG["webhook_daily"]
        if not url or not self.criteria:
            return
        state = self.state
        scale = CFG["stats_noise_scale"]
        deltas = subtract(self.criterion_values, state["day_stat_baseline"])
        playtimes = all_playtimes(self.stats, state, now)
        upsert_embed(url, "daily_overview", state,
                     daily_overview_embed(state, self.criteria, deltas,
                                          playtimes, now))
        time.sleep(0.4)
        upsert_embed(url, "daily_playtime", state,
                     daily_playtime_embed(state, playtimes, now))
        # Ground truth from Ledger's event log, where the mod is installed.
        # Always in the wanted list so a briefly-busy database does not
        # delete the card; it just keeps yesterday's numbers for a cycle.
        report = ledger.activity(CFG["server_dir"], day_start_epoch(now))
        if report:
            time.sleep(0.4)
            upsert_embed(url, "daily_building", state,
                         ledger.building_embed(report, day_label(now)))
        wanted = ["daily_overview", "daily_playtime", "daily_building"]
        for key, embed in gamestats.category_embeds(
                self.criteria, deltas, top=CFG["daily_top_per_category"],
                title_suffix="  ·  Today", scale=scale):
            wanted.append(f"daily_{key}")
            # Space the updates out: a webhook allows roughly five requests
            # every two seconds, and this is a whole channel of cards at once.
            time.sleep(0.4)
            upsert_embed(url, f"daily_{key}", state, embed)
        self.drop_unused_cards(url, "daily_", wanted)

    def drop_unused_cards(self, url, prefix, wanted):
        """Remove cards whose category or page no longer has anything to show.

        Categories gain and lose pages as their tables grow and shrink, and a
        daily category empties out entirely at every reset.
        """
        for key in [k for k in list(self.state["msg"])
                    if k.startswith(prefix) and k not in wanted]:
            if _delete_message(url, self.state["msg"].pop(key)):
                log(f"[card] removed {key}, no longer needed")

    # -- main loop --------------------------------------------------------

    def tick(self, now):
        # First, so the transcript stays close to live even when the slower
        # card work below runs.
        flush_chat()
        # The prime-time ledger: whoever is on right now is on during this
        # clock hour. Capped in case the loop was suspended (laptop lid).
        elapsed = min(max(0.0, now - self.last_loop), 120.0)
        self.last_loop = now
        if self.state["players"] and elapsed:
            hour = time.localtime(now).tm_hour
            self.fun["hours"][hour] += len(self.state["players"]) * elapsed
        if now - self.last_ping >= CFG["ping_seconds"]:
            self.last_ping = now
            self.do_ping(now)
        if now - self.last_stats >= CFG["stats_seconds"]:
            self.last_stats = now
            self.stats = load_stats()
            self.refresh_criteria()
            self.check_milestones(all_playtimes(self.stats, self.state, now))
            self.check_fun(now)
        self.maybe_fun_fact(now)
        if (CFG["external_check"] and CFG["public_address"]
                and now - self.last_external >= CFG["external_check_minutes"] * 60):
            self.last_external = now
            self.do_external_check()
        self.do_perf(now)
        if (CFG["webhook_stats"]
                and now - self.last_stat_cards >= CFG["stats_card_seconds"]):
            self.last_stat_cards = now
            self.refresh_stat_cards()
            save_state(self.state)
        self.do_daily(now)

        due_status = now - self.last_status_refresh >= CFG["status_refresh_seconds"]
        due_boards = now - self.last_leaderboards >= CFG["leaderboard_refresh_seconds"]
        if self.dirty or due_status or due_boards:
            self.refresh_cards(now, reposition=self.dirty)
            self.last_status_refresh = now
            if self.dirty or due_boards:
                self.last_leaderboards = now
            self.dirty = False
            save_state(self.state)

    def run_once(self):
        self.sweep()
        self.replay()
        self.seed_engagement()
        now = time.time()
        self.do_ping(now)
        self.stats = load_stats()
        self.refresh_criteria()
        self.check_milestones(all_playtimes(self.stats, self.state, now))
        if self.perf:
            # CPU percentages and I/O rates are deltas between readings, so a
            # single sample would have nothing to compare against.
            for _ in range(3):
                time.sleep(1)
                self.perf.sample(
                    time.time(),
                    self.status.get("_latency_ms") if self.status else None,
                    len(self.state["players"]),
                    tick=rcon.query_tick(CFG["server_dir"]) if self.status else None)
            self.do_perf(time.time())
        self.refresh_cards(now, reposition=False)
        self.refresh_stat_cards()
        self.do_daily(now)
        save_state(self.state)

    def run(self):
        log(f"[start] watching {CFG['server_dir']}", mirror=True)
        self.sweep()
        self.replay()
        self.seed_engagement()
        self.reader.seek_end()
        self.do_ping(time.time())
        self.stats = load_stats()
        self.refresh_criteria()
        self.check_milestones(all_playtimes(self.stats, self.state, time.time()))
        self.dirty = True
        while True:
            try:
                now = time.time()
                for raw in self.reader.read_new():
                    parsed = parse_line(raw)
                    if parsed:
                        self.handle(parsed[1], time.time())
                self.tick(now)
            except Exception as e:  # never let one bad cycle kill the watcher
                log(f"[error] cycle failed: {e!r}", mirror=True)
            time.sleep(CFG["log_poll_seconds"])


def main():
    global DRY_RUN
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--once", action="store_true", help="single pass, then exit")
    ap.add_argument("--dry-run", action="store_true", help="post nothing")
    args = ap.parse_args()
    DRY_RUN = args.dry_run

    if not CFG["server_dir"] or not os.path.isdir(CFG["server_dir"]):
        sys.exit(f"server_dir is not set or does not exist: {CFG['server_dir']!r}\n"
                 f"Copy config.example.json to config.json and fill it in.")
    if not DRY_RUN and not CFG["webhook_main"]:
        sys.exit("webhook_main is not set in config.json.")

    bot = Bot()
    if args.once:
        bot.run_once()
    else:
        try:
            bot.run()
        except KeyboardInterrupt:
            log("[stop] watcher stopped")


if __name__ == "__main__":
    main()
