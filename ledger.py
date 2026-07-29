"""Reads Ledger's event log — the ground truth the stats files cannot see.

The Ledger mod records every block place and break, container move and
player kill to world/ledger.sqlite, with who, where and when. The database
is WAL-journaled and indexed on time, so reading it read-only while the
server writes is safe and cheap.

Timestamps are stored as naive local time (verified against the wall
clock), so queries are bounded with local-time strings.
"""

import os
import sqlite3
import time

DB_RELATIVE = os.path.join("world", "ledger.sqlite")

WORLD_NAMES = {
    "minecraft:overworld": "the overworld",
    "minecraft:the_nether": "the Nether",
    "minecraft:the_end": "the End",
}


def _connect(server_dir):
    path = os.path.join(server_dir, DB_RELATIVE)
    if not os.path.isfile(path):
        return None
    uri = "file:" + path.replace("\\", "/") + "?mode=ro"
    try:
        db = sqlite3.connect(uri, uri=True, timeout=2)
        db.execute("SELECT 1 FROM actions LIMIT 1")
        return db
    except sqlite3.Error:
        return None


def _stamp(epoch):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(epoch))


def activity(server_dir, since_epoch):
    """What players actually did since a moment, straight from the event log.

    {"players": {name: {placed, broken, kills, containers}},
     "busiest": (world id, chunk x, chunk z, actions, mid x, mid z) or None,
     "worlds": {world id: actions}}
    — or None when the database is missing or busy, in which case the caller
    keeps whatever it showed last rather than flashing an empty card.
    """
    db = _connect(server_dir)
    if db is None:
        return None
    since = _stamp(since_epoch)
    try:
        players = {}
        for name, action, count in db.execute(
                "SELECT p.player_name, ai.action_identifier, COUNT(*) "
                "FROM actions a "
                "JOIN players p ON p.id = a.player_id "
                "JOIN ActionIdentifiers ai ON ai.id = a.action_id "
                "WHERE a.time >= ? AND ai.action_identifier IN ("
                "'block-place','block-break','entity-kill',"
                "'item-insert','item-remove') "
                "GROUP BY p.player_name, ai.action_identifier", (since,)):
            entry = players.setdefault(
                name, {"placed": 0, "broken": 0, "kills": 0, "containers": 0})
            if action == "block-place":
                entry["placed"] += count
            elif action == "block-break":
                entry["broken"] += count
            elif action == "entity-kill":
                entry["kills"] += count
            else:
                entry["containers"] += count
        busiest = db.execute(
            "SELECT w.identifier, (a.x>>4), (a.z>>4), COUNT(*) AS n, "
            "CAST(AVG(a.x) AS INT), CAST(AVG(a.z) AS INT) "
            "FROM actions a JOIN worlds w ON w.id = a.world_id "
            "WHERE a.time >= ? AND a.player_id IS NOT NULL "
            "GROUP BY 1, 2, 3 ORDER BY n DESC LIMIT 1", (since,)).fetchone()
        worlds = dict(db.execute(
            "SELECT w.identifier, COUNT(*) "
            "FROM actions a JOIN worlds w ON w.id = a.world_id "
            "WHERE a.time >= ? AND a.player_id IS NOT NULL "
            "GROUP BY w.identifier", (since,)))
        return {"players": players, "busiest": busiest, "worlds": worlds}
    except sqlite3.Error:
        return None
    finally:
        db.close()


def building_embed(report, label):
    """The daily ground-truth card: who built what, and where the action was."""
    ranked = sorted(report["players"].items(),
                    key=lambda kv: -(kv[1]["placed"] + kv[1]["broken"]))
    lines = []
    for name, entry in ranked[:10]:
        bits = []
        if entry["placed"]:
            bits.append(f"placed **{entry['placed']:,}**")
        if entry["broken"]:
            bits.append(f"broke **{entry['broken']:,}**")
        if entry["containers"]:
            bits.append(f"{entry['containers']:,} container moves")
        if entry["kills"]:
            bits.append(f"{entry['kills']:,} kills")
        if bits:
            lines.append(f"> 🧱  **{name}** — " + "  ·  ".join(bits))
    body = "\n".join(lines) if lines else "> 💤  *nothing logged yet today*"

    total = sum(report["worlds"].values())
    if total:
        split = "  ·  ".join(
            f"{WORLD_NAMES.get(world, world)} {100 * n / total:.0f}%"
            for world, n in sorted(report["worlds"].items(), key=lambda kv: -kv[1]))
        body += f"\n\n> 🌍  {split}"
    if report["busiest"]:
        world, _, _, actions, x, z = report["busiest"]
        body += (f"\n> 📍  busiest spot: **{x}, {z}** in "
                 f"{WORLD_NAMES.get(world, world)}  ·  {actions:,} actions")
    return {
        "title": "📐  Building Report  ·  Today",
        "description": f"### {label}\n{body}"[:4096],
        "color": 0x009688,
        "footer": {"text": "from Ledger's event log  •  resets at 3:00 am Eastern"},
    }
