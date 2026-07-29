"""Awards, records, streaks, challenges — the celebration layer.

Everything here is a pure function over the same shape of data: a player's
day (or lifetime) reduced to a dozen headline numbers by aggregate(). mcbot
owns all state and all posting; this module only decides who won what and
how to say it.

Anything random-looking (award flavor text, the day's challenge) is
deterministic on the date via crc32, so a restart mid-day never re-rolls it.
"""

import datetime as dt
import zlib

import gamestats

# ------------------------------------------------------------- aggregation --

SECTION_KEYS = {
    "mined": "minecraft:mined",
    "killed": "minecraft:killed",
    "crafted": "minecraft:crafted",
    "used": "minecraft:used",
    "picked_up": "minecraft:picked_up",
    "dropped": "minecraft:dropped",
}
CUSTOM_KEYS = {
    "deaths": "minecraft:deaths",
    "jumps": "minecraft:jump",
    "damage_taken": "minecraft:damage_taken",
    "damage_dealt": "minecraft:damage_dealt",
    "fish": "minecraft:fish_caught",
    "animals_bred": "minecraft:animals_bred",
    "trades": "minecraft:traded_with_villager",
}


def aggregate(criteria, per_player):
    """{player: {criterion: value}} -> {player: {mined, deaths, distance, ...}}.

    The same reduction serves the daily awards, the record ledger, the
    challenge standings and the race watch, so they can never disagree about
    what somebody did.
    """
    blocks = gamestats.block_item_ids(criteria)
    plan = []
    for criterion in criteria:
        parts = gamestats.split_criterion(criterion)
        if not parts:
            continue
        section, key = parts
        if section == "minecraft:custom":
            if gamestats.unit_of(criterion) == "distance":
                plan.append((criterion, "distance"))
            else:
                for name, wanted in CUSTOM_KEYS.items():
                    if key == wanted:
                        plan.append((criterion, name))
        else:
            for name, wanted in SECTION_KEYS.items():
                if section == wanted:
                    plan.append((criterion, name))
            # Placing a block is "using" its item; split those out of used.
            if section == "minecraft:used" and key in blocks:
                plan.append((criterion, "placed"))
    empty = {key: 0 for key in [*SECTION_KEYS, *CUSTOM_KEYS,
                                "distance", "placed"]}
    out = {}
    for player, values in per_player.items():
        agg = dict(empty)
        for criterion, name in plan:
            agg[name] += values.get(criterion, 0)
        agg["blocks"] = agg["mined"] + agg["placed"]
        out[player] = agg
    return out


def fmt_value(unit, value):
    if unit == "time":
        minutes = int(value) // 60
        h, m = divmod(minutes, 60)
        return f"{h}h {m:02d}m" if h else f"{m}m"
    if unit == "distance":
        km = value / 100_000
        return f"{km:,.1f} km" if km >= 1 else f"{value / 100:,.0f} m"
    if unit == "damage":
        return f"{value / 10:,.0f} HP"
    return f"{int(value):,}"


def _pick(day_key, salt, options):
    """A stable choice for the day — restarting the bot must not re-roll it."""
    return options[zlib.crc32(f"{day_key}:{salt}".encode()) % len(options)]


# ------------------------------------------------------------ daily awards --

# (key, emoji, title, aggregate key, minimum to award, unit, flavor lines).
# The minimum keeps an award from going to somebody who technically led a
# category nobody participated in.
AWARDS = [
    ("dedicated", "🏆", "Most Dedicated", "playtime", 1800, "time", (
        "clocked in and never clocked out.",
        "the server should start charging rent.",
        "the sun rose, the sun set; they remained.")),
    ("miner", "⛏️", "Deepest Commitment", "mined", 128, "count", (
        "the caves echo with their pickaxe.",
        "singlehandedly lowering the world's average altitude.",
        "somewhere, a mountain is missing.")),
    ("builder", "🧱", "Master Builder", "placed", 128, "count", (
        "the skyline is different now.",
        "one block at a time, a monument.",
        "the world is their canvas, and the canvas is cobblestone.")),
    ("hunter", "🗡️", "Menace to Wildlife", "killed", 15, "count", (
        "the mobs have formed a support group.",
        "the food chain has a new apex.",
        "hostile mobs? not for long.")),
    ("victim", "💀", "Most Deaths", "deaths", 2, "count", (
        "the respawn screen knows them by name.",
        "gravity remains undefeated.",
        "died as they lived: frequently.")),
    ("butterfingers", "🧈", "Butterfingers", "dropped", 128, "count", (
        "their hotbar has a trapdoor.",
        "leaving a trail of loot wherever they go.",
        "Q is their most-used key.")),
    ("wanderer", "🧭", "Longest Haul", "distance", 500_000, "distance", (
        "the horizon kept moving, so they kept walking.",
        "has personally greeted every biome.",
        "their boots have filed a grievance.")),
    ("bunny", "🐇", "Bunny", "jumps", 800, "count", (
        "the ground is more of a suggestion.",
        "jump height: unremarkable. jump count: alarming.",
        "part player, part pogo stick.")),
    ("punching_bag", "🥊", "Punching Bag", "damage_taken", 600, "damage", (
        "took every hit so nobody else had to.",
        "their armor bar has filed a formal complaint.",
        "a load-bearing player.")),
    ("angler", "🎣", "Gone Fishin'", "fish", 5, "count", (
        "the ocean is emptier now.",
        "patience of a saint, freezer full of cod.",
        "the fish never stood a chance.")),
    ("chatterbox", "📢", "Chatterbox", "chat", 8, "count", (
        "never met a thought they didn't type.",
        "the chat log's main character.",
        "keyboard warmer than their furnace.")),
]


def compute_awards(agg, day_key):
    """[(emoji, title, winner, value string, flavor)] for one finished day.

    agg must include "playtime" (seconds) and "chat" (messages) per player,
    merged in by the caller — they live outside the statistics files.
    """
    out = []
    for key, emoji, title, stat, minimum, unit, flavor in AWARDS:
        scores = {n: a.get(stat, 0) for n, a in agg.items()}
        scores = {n: v for n, v in scores.items() if v >= minimum}
        if not scores:
            continue
        winner = max(scores, key=lambda n: scores[n])
        out.append((emoji, title, winner, fmt_value(unit, scores[winner]),
                    _pick(day_key, key, flavor)))
    # Homebody: most present, least travelled. A different shape from the
    # rest — it rewards the smallest number among the sufficiently dedicated.
    stayed = {n: a for n, a in agg.items()
              if a.get("playtime", 0) >= 1800 and a.get("distance", 0) < 100_000}
    if stayed:
        winner = min(stayed, key=lambda n: stayed[n]["distance"])
        out.append(("🛋️", "Homebody", winner,
                    fmt_value("distance", stayed[winner]["distance"]),
                    _pick(day_key, "homebody", (
                        "why explore when the furnace is right here?",
                        "home is where the chunk loads.",
                        "travelled less than a village librarian."))))
    return out


def awards_embed(awards, label):
    lines = [f"> {emoji}  **{title}** — **{winner}** ({value})\n> ​　*{flavor}*"
             for emoji, title, winner, value, flavor in awards]
    return {
        "title": "🎖️  Daily Awards",
        "description": f"### {label}\n" + ("\n".join(lines) if lines
                                           else "> 💤  *nobody qualified for anything*"),
        "color": 0xE91E63,
        "footer": {"text": "decided at 3:00 am Eastern  •  minimums apply"},
    }


# ----------------------------------------------------------- daily records --

# All-time bests for a single day. (key, emoji, label, aggregate key,
# minimum to establish, unit) — the minimum stops "daily record: 1 block".
RECORDS = [
    ("playtime", "🕰️", "Longest day", "playtime", 900, "time"),
    ("mined", "⛏️", "Most blocks mined", "mined", 256, "count"),
    ("placed", "🧱", "Most blocks placed", "placed", 256, "count"),
    ("blocks", "🏗️", "Most blocks mined + placed", "blocks", 512, "count"),
    ("killed", "🗡️", "Most mobs killed", "killed", 25, "count"),
    ("crafted", "⚒️", "Most items crafted", "crafted", 128, "count"),
    ("distance", "🏃", "Furthest travelled", "distance", 1_000_000, "distance"),
    ("deaths", "💀", "Most deaths", "deaths", 3, "count"),
]


def check_records(ledger, agg, day_label):
    """Update the ledger with one finished day; return the broken records.

    Returns [(emoji, label, name, value string, previous holder or None,
    previous value string or None)]. First-time entries go into the ledger
    silently — a record with no previous holder is a baseline, not news.
    """
    broken = []
    for key, emoji, label, stat, minimum, unit in RECORDS:
        scores = {n: a.get(stat, 0) for n, a in agg.items()}
        if not scores:
            continue
        best = max(scores, key=lambda n: scores[n])
        value = scores[best]
        if value < minimum:
            continue
        previous = ledger.get(key)
        if previous and value <= previous["value"]:
            continue
        ledger[key] = {"holder": best, "value": value, "day": day_label}
        if previous:
            broken.append((emoji, label, best, fmt_value(unit, value),
                           previous["holder"], fmt_value(unit, previous["value"])))
    return broken


def records_embed(ledger):
    lines = []
    for key, emoji, label, _, _, unit in RECORDS:
        entry = ledger.get(key)
        if entry:
            lines.append(f"> {emoji}  **{label}** — **{entry['holder']}**  ·  "
                         f"{fmt_value(unit, entry['value'])}  ·  *{entry['day']}*")
    return {
        "title": "📜  Single-Day Records",
        "description": ("### The best any player has done in one day\n"
                        + ("\n".join(lines) if lines
                           else "> 💤  *no records established yet*")),
        "color": 0x8E44AD,
        "footer": {"text": "all-time  •  one statistics day each  •  broken records are announced"},
    }


# ---------------------------------------------------------------- streaks --

STREAK_MILESTONES = (3, 5, 7, 14, 21, 30, 50, 100)
STREAK_MIN_SECONDS = 600


def update_streaks(streaks, played, finished_day):
    """Advance every streak by one finished day.

    streaks is {name: {"current", "best", "last"}} with "last" an ISO date;
    played is {name: seconds} for the day that just ended. Returns
    (milestones, broken) as [(name, length)]. Ten minutes counts as playing —
    a login-and-out should not extend a streak.
    """
    previous = (finished_day - dt.timedelta(days=1)).isoformat()
    today = finished_day.isoformat()
    milestones = []
    for name, seconds in played.items():
        if seconds < STREAK_MIN_SECONDS:
            continue
        entry = streaks.setdefault(name, {"current": 0, "best": 0, "last": ""})
        entry["current"] = entry["current"] + 1 if entry["last"] == previous else 1
        entry["last"] = today
        entry["best"] = max(entry["best"], entry["current"])
        if entry["current"] in STREAK_MILESTONES:
            milestones.append((name, entry["current"]))
    broken = []
    for name, entry in streaks.items():
        if entry["last"] != today and entry["current"]:
            if entry["current"] >= 3:
                broken.append((name, entry["current"]))
            entry["current"] = 0
    return milestones, broken


def streaks_embed(streaks):
    current = sorted(((n, e) for n, e in streaks.items() if e["current"]),
                     key=lambda kv: -kv[1]["current"])
    lines = [f"> 🔥  **{name}** — **{e['current']}** day"
             f"{'s' if e['current'] != 1 else ''} running"
             + (f"  ·  best {e['best']}" if e["best"] > e["current"] else "")
             for name, e in current[:10]]
    bests = sorted(streaks.items(), key=lambda kv: -kv[1]["best"])
    if bests and bests[0][1]["best"] >= 3:
        name, e = bests[0]
        lines.append(f"\n> 👑  Longest ever: **{name}** — {e['best']} days")
    return {
        "title": "🔥  Playtime Streaks",
        "description": ("### Consecutive days played\n"
                        + ("\n".join(lines) if lines
                           else "> 💤  *no active streaks*")),
        "color": 0xE74C3C,
        "footer": {"text": "10+ minutes counts  •  a missed day resets to zero"},
    }


# ------------------------------------------------------ challenge of the day --

CHALLENGES = [
    ("⛏️", "Mine the most blocks", "mined", "count"),
    ("🗡️", "Slay the most mobs", "killed", "count"),
    ("⚒️", "Craft the most items", "crafted", "count"),
    ("🏃", "Travel the furthest", "distance", "distance"),
    ("🎣", "Catch the most fish", "fish", "count"),
    ("🐄", "Breed the most animals", "animals_bred", "count"),
    ("🦘", "Jump the most times", "jumps", "count"),
    ("💰", "Trade the most with villagers", "trades", "count"),
]


def pick_challenge(day_key):
    """The day's challenge — the same one all day, restart or not."""
    return CHALLENGES[zlib.crc32(f"challenge:{day_key}".encode()) % len(CHALLENGES)]


def challenge_standings(challenge, agg):
    """[(name, value string)] top five, zeros excluded."""
    _, _, stat, unit = challenge
    scores = {n: a.get(stat, 0) for n, a in agg.items()}
    ranked = sorted((kv for kv in scores.items() if kv[1] > 0),
                    key=lambda kv: -kv[1])
    return [(name, fmt_value(unit, value)) for name, value in ranked[:5]]


def challenge_embed(challenge, agg, label):
    emoji, title, _, _ = challenge
    rows = challenge_standings(challenge, agg)
    medals = ["🥇", "🥈", "🥉"]
    body = "\n".join(
        f"> {medals[i] if i < 3 else f'`{i + 1}.`'}  **{name}** — {value}"
        for i, (name, value) in enumerate(rows)) or "> 💤  *nobody has scored yet*"
    return {
        "title": f"🎯  Today's Challenge — {emoji} {title}",
        "description": f"### {label}\n{body}",
        "color": 0x3498DB,
        "footer": {"text": "winner crowned at 3:00 am Eastern"},
    }


def challenge_result_embed(challenge, agg, label):
    emoji, title, _, _ = challenge
    rows = challenge_standings(challenge, agg)
    if rows:
        winner, value = rows[0]
        body = f"> 🏅  **{winner}** wins with **{value}**"
        runners = rows[1:3]
        if runners:
            body += "\n" + "\n".join(f"> 　  {name} — {value}"
                                     for name, value in runners)
    else:
        body = "> 💤  *nobody entered — the challenge goes unclaimed*"
    return {
        "title": f"🏁  Challenge Complete — {emoji} {title}",
        "description": f"### {label}\n{body}",
        "color": 0x2980B9,
        "footer": {"text": "a new challenge starts now"},
    }


# --------------------------------------------------------------- prime time --

def primetime_embed(buckets):
    """A 24-row histogram of player-hours by local clock hour."""
    total = sum(buckets)
    if total <= 0:
        description = "> 💤  *gathering data*"
    else:
        peak = max(buckets)
        rows = []
        for hour, seconds in enumerate(buckets):
            bar = "█" * round(14 * seconds / peak) if peak else ""
            share = 100 * seconds / total
            rows.append(f"{hour:02d}  {bar:<14} {share:4.1f}%")
        description = ("```\n" + "\n".join(rows) + "\n```"
                       + f"\n**{total / 3600:,.0f}** player-hours observed. "
                         f"Peak hour: **{buckets.index(peak):02d}:00**.")
    return {
        "title": "🕐  Prime Time",
        "description": ("### When this server is alive\n" + description)[:4096],
        "color": 0x34495E,
        "footer": {"text": "player-hours by local clock hour  •  lifetime"},
    }


# --------------------------------------------------------------- chat stats --

def chat_embed(totals, today):
    ranked = sorted((kv for kv in totals.items() if kv[1]), key=lambda kv: -kv[1])
    medals = ["🥇", "🥈", "🥉"]
    lines = [f"> {medals[i] if i < 3 else f'`{i + 1}.`'}  **{name}** — "
             f"{count:,} messages"
             + (f"  ·  {today[name]:,} today" if today.get(name) else "")
             for i, (name, count) in enumerate(ranked[:10])]
    return {
        "title": "🗣️  Chat Leaders",
        "description": ("### Who does the talking\n"
                        + ("\n".join(lines) if lines
                           else "> 💤  *nobody has said anything yet*")),
        "color": 0x1ABC9C,
        "footer": {"text": "counted from the whole log archive  •  updated live"},
    }


# ------------------------------------------------------------- projections --

def pace_lines(playtimes, history, milestones, today):
    """'X → 100h in ~12d (Aug 10)' — the three soonest milestone arrivals.

    history is the last ~week of {name: seconds-played}; with too little of
    it, lifetime-average pace would credit years-old habits, so say nothing.
    """
    if len(history) < 2:
        return []
    out = []
    for name, total in playtimes.items():
        daily = sum(day.get(name, 0) for day in history) / len(history)
        if daily < 600:
            continue
        hours = total / 3600
        ahead = [m for m in milestones if m > hours]
        if not ahead:
            continue
        days = (ahead[0] * 3600 - total) / daily
        if days > 90:
            continue
        arrival = today + dt.timedelta(days=round(days))
        out.append((days, f"> 📈  **{name}** → **{ahead[0]}h** in ~{round(days)}d "
                          f"(*{arrival.strftime('%b %d')}*)"))
    out.sort(key=lambda pair: pair[0])
    return [line for _, line in out[:3]]


# ------------------------------------------------------------ advancements --

def _humanize_advancement(key):
    return key.split("/")[-1].replace("_", " ").title()


def advancements_embed(done_sets):
    """The race to complete everything this server has discovered.

    The full vanilla list lives inside the server jar, which this bot does
    not read — so the honest denominator is the union of everything anybody
    here has completed.
    """
    union = set()
    for done in done_sets.values():
        union |= done
    ranked = sorted(done_sets.items(), key=lambda kv: -len(kv[1]))
    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for i, (name, done) in enumerate(ranked[:10]):
        pct = 100 * len(done) / len(union) if union else 0
        lines.append(f"> {medals[i] if i < 3 else f'`{i + 1}.`'}  **{name}** — "
                     f"{len(done)}/{len(union)}  ·  {pct:.0f}%")
    exclusive = sorted(
        key for key in union
        if sum(1 for done in done_sets.values() if key in done) == 1)
    body = "\n".join(lines) if lines else "> 💤  *no advancements yet*"
    if exclusive:
        shown = ", ".join(_humanize_advancement(k) for k in exclusive[:8])
        more = f" *…and {len(exclusive) - 8} more*" if len(exclusive) > 8 else ""
        body += (f"\n\n**Completed by only one player ({len(exclusive)}):** "
                 f"{shown}{more}")
    return {
        "title": "🗺️  Advancement Race",
        "description": (f"### Toward everything this server has discovered\n"
                        f"{body}")[:4096],
        "color": 0x27AE60,
        "footer": {"text": "recipe unlocks excluded  •  denominator is the server's discoveries"},
    }


# ---------------------------------------------------------------- fun facts --

MARATHON_KM = 42.195


def fun_facts(name, entry, chat_total=0, streak=0):
    """Every true thing worth saying about one player, from their own save.

    The caller picks one at random; everything here is only offered when the
    underlying number is big enough to be worth announcing.
    """
    raw = entry.get("raw", {})
    custom = raw.get("minecraft:custom", {})

    def top(section):
        table = raw.get(section, {})
        if not table:
            return None
        key = max(table, key=table.get)
        return key.split(":", 1)[-1].replace("_", " ").title(), table[key]

    facts = []
    favorite = top("minecraft:mined")
    if favorite and favorite[1] >= 100:
        facts.append(f"{name} has mined {favorite[1]:,} {favorite[0]} — "
                     f"their favorite block")
    nemesis = top("minecraft:killed_by")
    if nemesis and nemesis[1] >= 2:
        facts.append(f"{nemesis[0]} has killed {name} {nemesis[1]:,} times. "
                     f"A true nemesis")
    prey = top("minecraft:killed")
    if prey and prey[1] >= 25:
        facts.append(f"{name} has personally ended {prey[1]:,} {prey[0]}s")
    deaths = entry.get("deaths", 0)
    if deaths >= 3:
        facts.append(f"{name} has died {deaths:,} times so far")
    km = entry.get("distance_cm", 0) / 100_000
    if km >= 10:
        marathons = km / MARATHON_KM
        facts.append(f"{name} has travelled {km:,.0f} km — "
                     f"about {marathons:,.1f} marathons")
    jumps = custom.get("minecraft:jump", 0)
    if jumps >= 1000:
        facts.append(f"{name} has jumped {jumps:,} times")
    fish = entry.get("fish_caught", 0)
    if fish >= 5:
        facts.append(f"{name} has pulled {fish:,} fish out of the water")
    hours = entry.get("play_time", 0) / 3600
    if hours >= 5:
        facts.append(f"{name} has spent {hours:,.0f} hours of their life here")
    chests = custom.get("minecraft:open_chest", 0)
    if chests >= 100:
        facts.append(f"{name} has opened a chest {chests:,} times")
    cake = custom.get("minecraft:eat_cake_slice", 0)
    if cake >= 3:
        facts.append(f"{name} has eaten {cake:,} slices of cake")
    trades = entry.get("trades", 0)
    if trades >= 10:
        facts.append(f"{name} has made {trades:,} villager trades")
    bred = entry.get("animals_bred", 0)
    if bred >= 10:
        facts.append(f"{name} has bred {bred:,} animals")
    hearts = entry.get("damage_taken", 0) / 2
    if hearts >= 100:
        facts.append(f"{name} has absorbed {hearts:,.0f} hearts of damage "
                     f"and lived to tell about it")
    slept = custom.get("minecraft:sleep_in_bed", 0)
    if slept >= 5:
        facts.append(f"{name} has slept through {slept:,} nights")
    if chat_total >= 25:
        facts.append(f"{name} has sent {chat_total:,} chat messages")
    if streak >= 3:
        facts.append(f"{name} has played {streak} days in a row")
    if not facts:
        facts = [f"{name} remains a person of complete mystery"]
    return facts


# ---------------------------------------------------------------- spotlight --

def spotlight_embed(name, entry, day_key):
    """One player's story, rotating daily. entry is a load_stats() row."""
    raw = entry.get("raw", {})

    def top_of(section, prefix="minecraft:"):
        table = raw.get(section, {})
        if not table:
            return None
        key = max(table, key=lambda k: table[k])
        label = key[len(prefix):].replace("_", " ").title() if key.startswith(prefix) else key
        return label, table[key]

    facts = [f"> 🕰️  **{fmt_value('time', entry.get('play_time', 0))}** all-time"]
    favorite = top_of("minecraft:mined")
    if favorite:
        facts.append(f"> ⛏️  Favorite block: **{favorite[0]}** ({favorite[1]:,} mined)")
    nemesis = top_of("minecraft:killed_by")
    if nemesis:
        times = f"{nemesis[1]:,} death{'s' if nemesis[1] != 1 else ''}"
        facts.append(f"> 😱  Nemesis: **{nemesis[0]}** ({times} to it)")
    prey = top_of("minecraft:killed")
    if prey:
        facts.append(f"> 🗡️  Most hunted: **{prey[0]}** ({prey[1]:,} kills)")
    if entry.get("advancements"):
        facts.append(f"> 🗺️  **{entry['advancements']}** advancements")
    if entry.get("deaths"):
        facts.append(f"> 💀  **{entry['deaths']:,}** deaths")
    if entry.get("distance_cm"):
        facts.append(f"> 🏃  **{fmt_value('distance', entry['distance_cm'])}** travelled")
    if entry.get("fish_caught"):
        facts.append(f"> 🎣  **{entry['fish_caught']:,}** fish caught")
    if entry.get("trades"):
        facts.append(f"> 💰  **{entry['trades']:,}** villager trades")
    return {
        "title": f"🌟  Player Spotlight — {name}",
        "description": "\n".join(facts),
        "color": 0xF1C40F,
        "footer": {"text": "a different player every day"},
    }


def pick_spotlight(names, day_key):
    """Which player gets today's spotlight — stable across restarts."""
    ordered = sorted(names)
    if not ordered:
        return None
    return ordered[zlib.crc32(f"spotlight:{day_key}".encode()) % len(ordered)]
