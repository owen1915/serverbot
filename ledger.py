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


DENSITY = " ░▒▓█"


def heatmap(server_dir, since_epoch, world="minecraft:overworld",
            cols=26, rows=12):
    """A density grid of where the action has been, or None without data.

    The 2nd–98th percentile of event positions sets the bounds, so one long
    nether-portal errand cannot zoom the whole map out to nothing.
    """
    db = _connect(server_dir)
    if db is None:
        return None
    try:
        points = db.execute(
            "SELECT a.x, a.z FROM actions a "
            "JOIN worlds w ON w.id = a.world_id "
            "WHERE a.time >= ? AND a.player_id IS NOT NULL "
            "AND w.identifier = ?", (_stamp(since_epoch), world)).fetchall()
    except sqlite3.Error:
        return None
    finally:
        db.close()
    if len(points) < 20:
        return None
    xs = sorted(p[0] for p in points)
    zs = sorted(p[1] for p in points)
    lo, hi = len(xs) * 2 // 100, len(xs) * 98 // 100
    x0, x1 = xs[lo], xs[hi]
    z0, z1 = zs[lo], zs[hi]
    # At least one chunk per cell, and identical scale on both axes so the
    # map is not silently stretched.
    cell = max(16, (x1 - x0) // cols + 1, (z1 - z0) // rows + 1)
    x0 = (x0 // cell) * cell
    z0 = (z0 // cell) * cell
    grid = [[0] * cols for _ in range(rows)]
    for x, z in points:
        col = min(max((x - x0) // cell, 0), cols - 1)
        row = min(max((z - z0) // cell, 0), rows - 1)
        grid[row][col] += 1
    peak = max(max(row) for row in grid)
    lines = []
    for r, row in enumerate(grid):
        cells = []
        for c, count in enumerate(row):
            if count and count == peak:
                cells.append("★")
            elif count:
                # Log-ish scale: a base camp must not white out the map.
                level = min(len(DENSITY) - 1,
                            1 + int(3 * (count / peak) ** 0.4))
                cells.append(DENSITY[level])
            else:
                cells.append(DENSITY[0])
        lines.append("".join(cells))
    return {
        "grid": lines,
        "cell": cell,
        "x_range": (x0, x0 + cell * cols),
        "z_range": (z0, z0 + cell * rows),
        "events": len(points),
    }


def heatmap_embed(data, label):
    x0, x1 = data["x_range"]
    z0, z1 = data["z_range"]
    return {
        "title": "🗺️  Activity Map",
        "description": (
            f"### {label}\n"
            "```\n" + "\n".join(data["grid"]) + "\n```\n"
            f"**{data['events']:,}** logged actions in the overworld  ·  "
            f"★ marks the busiest spot\n"
            f"> x {x0:,} … {x1:,}   ·   z {z0:,} … {z1:,}   ·   "
            f"{data['cell']}m per cell"),
        "color": 0x2C3E50,
        "footer": {"text": "from Ledger's event log  •  north is up"},
    }


def first_joins(server_dir):
    """{name: 'YYYY-MM-DD'} — when Ledger first saw each player."""
    db = _connect(server_dir)
    if db is None:
        return {}
    try:
        return {name: (stamp or "")[:10]
                for name, stamp in db.execute(
                    "SELECT player_name, first_join FROM players")
                if stamp}
    except sqlite3.Error:
        return {}
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
