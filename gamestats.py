"""The full statistic set the server tracks, rendered for Discord.

The Vanilla Tweaks statistic datapacks declare a scoreboard objective per
statistic, each bound to a vanilla criterion such as
``minecraft.killed:minecraft.zombie``. Those criteria are read straight out of
the datapack zips, so this follows whatever is actually installed: add or
remove a datapack and the cards follow, with no list to maintain here.

Every criterion resolves against the same ``world/players/stats/*.json`` files
the rest of the bot reads, so no scoreboard or NBT parsing is needed.

Cards are drawn as aligned monospace tables inside Discord ANSI code blocks,
which is the only way to get columns that line up in a Discord message.
"""

import os
import re
import zipfile

# ---------------------------------------------------------------- discovery --

OBJECTIVE_RE = re.compile(r"scoreboard objectives add (\S+) (\S+)")

# Criteria that are not of the form namespace.section:namespace.key.
SPECIAL_CRITERIA = {
    "deathCount": ("minecraft:custom", "minecraft:deaths"),
    "playerKillCount": ("minecraft:custom", "minecraft:player_kills"),
    "totalKillCount": ("minecraft:custom", "minecraft:mob_kills"),
}


def load_criteria(server_dir):
    """Every statistic criterion the installed datapacks track, in order.

    Several objectives can share one criterion (the packs each define their own
    playtime counter, for instance); the criterion is what matters here, so
    duplicates collapse.
    """
    found = []
    seen = set()
    datapacks = os.path.join(server_dir, "world", "datapacks")
    try:
        names = sorted(os.listdir(datapacks))
    except OSError:
        return found
    for name in names:
        if not name.endswith(".zip"):
            continue
        try:
            with zipfile.ZipFile(os.path.join(datapacks, name)) as pack:
                for entry in pack.namelist():
                    if not entry.endswith(".mcfunction"):
                        continue
                    text = pack.read(entry).decode("utf-8", "replace")
                    for _, criterion in OBJECTIVE_RE.findall(text):
                        if criterion == "dummy":
                            continue
                        # Dedupe on the statistic a criterion resolves to, not
                        # on its spelling: the packs each declare their own
                        # playtime and kill counters, and totalKillCount lands
                        # on the same stat as the plain mob-kill criterion.
                        resolved = split_criterion(criterion)
                        if resolved is None or resolved in seen:
                            continue
                        seen.add(resolved)
                        found.append(criterion)
        except (OSError, zipfile.BadZipFile, KeyError):
            continue
    return found


def split_criterion(criterion):
    """('minecraft:killed', 'minecraft:zombie') for a criterion, or None."""
    if criterion in SPECIAL_CRITERIA:
        return SPECIAL_CRITERIA[criterion]
    if ":" not in criterion:
        return None
    section, _, key = criterion.partition(":")
    # minecraft.killed:minecraft.zombie -> minecraft:killed / minecraft:zombie
    return section.replace(".", ":", 1), key.replace(".", ":", 1)


def criterion_for(section, key):
    """('minecraft:killed', 'minecraft:zombie') -> criterion string."""
    return f"{section.replace(':', '.', 1)}:{key.replace(':', '.', 1)}"


def observed_criteria(stats):
    """Every statistic that actually has a value in the world save.

    Minecraft writes a player's statistics whether or not a datapack declares a
    scoreboard objective for them, and it only stores non-zero entries — so the
    union across players is exactly the set worth showing. This is far larger
    than any datapack's curated list.
    """
    found = set()
    for entry in stats.values():
        for section, values in entry.get("raw", {}).items():
            for key, value in values.items():
                if value:
                    found.add((section, key))
    return [criterion_for(section, key) for section, key in sorted(found)]


def value_of(raw_stats, criterion):
    """The value of one criterion for one player's parsed stats file."""
    parts = split_criterion(criterion)
    if not parts:
        return 0
    section, key = parts
    return raw_stats.get(section, {}).get(key, 0)


# ------------------------------------------------------------- presentation --

# Order here is the order the cards appear in the channel.
CATEGORIES = [
    ("general", "📊", "General", "Statistic", "Total",
     ("minecraft:custom",)),
    ("mined", "⛏️", "Blocks Mined", "Block", "Mined",
     ("minecraft:mined",)),
    ("killed", "🗡️", "Mobs Killed", "Mob", "Kills",
     ("minecraft:killed",)),
    ("killed_by", "💀", "Killed By", "Mob", "Deaths",
     ("minecraft:killed_by",)),
    ("used", "🧪", "Items Used", "Item", "Used",
     ("minecraft:used",)),
    ("crafted", "⚒️", "Items Crafted", "Item", "Crafted",
     ("minecraft:crafted",)),
    ("broken", "🔨", "Tools Broken", "Tool", "Broken",
     ("minecraft:broken",)),
    ("picked_up", "🎒", "Items Picked Up", "Item", "Picked Up",
     ("minecraft:picked_up",)),
    ("dropped", "📤", "Items Dropped", "Item", "Dropped",
     ("minecraft:dropped",)),
]

CATEGORY_COLOURS = {
    "general": 0x3498DB, "mined": 0x95A5A6, "killed": 0xE74C3C,
    "killed_by": 0x9B59B6, "used": 0x1ABC9C, "broken": 0xE67E22,
    "crafted": 0xF1C40F, "picked_up": 0x16A085, "dropped": 0x7F8C8D,
}

# Custom statistics whose stored unit is not a plain count.
TICK_STATS = {
    "minecraft:play_time", "minecraft:time_since_death", "minecraft:sneak_time",
    "minecraft:time_since_rest", "minecraft:total_world_time",
}
DAMAGE_STATS = {
    "minecraft:damage_dealt", "minecraft:damage_taken", "minecraft:damage_absorbed",
    "minecraft:damage_blocked_by_shield", "minecraft:damage_resisted",
    "minecraft:damage_dealt_absorbed", "minecraft:damage_dealt_resisted",
}

WORD_FIXES = {"Tnt": "TNT", "Xp": "XP", "Ui": "UI"}

# Vanilla stat ids that read badly at column width, or are simply misleading
# once the category already says what is being counted.
RENAMES = {
    "minecraft:traded_with_villager": "Villager Trades",
    "minecraft:talked_to_villager": "Villagers Talked To",
    "minecraft:time_since_death": "Longest Life",
    "minecraft:time_since_rest": "Since Last Slept",
    "minecraft:total_world_time": "World Time",
    "minecraft:play_time": "Play Time",
    "minecraft:mob_kills": "Mob Kills",
    "minecraft:player_kills": "Player Kills",
    "minecraft:damage_blocked_by_shield": "Blocked By Shield",
    "minecraft:clean_shulker_box": "Shulker Boxes Washed",
    "minecraft:eat_cake_slice": "Cake Slices Eaten",
    "minecraft:open_barrel": "Barrels Opened",
    "minecraft:open_chest": "Chests Opened",
    "minecraft:open_enderchest": "Ender Chests Opened",
    "minecraft:open_shulker_box": "Shulker Boxes Opened",
    "minecraft:trigger_trapped_chest": "Trapped Chests Set Off",
    "minecraft:sleep_in_bed": "Nights Slept",
}


def display_name(criterion):
    """'minecraft.killed:minecraft.cave_spider' -> 'Cave Spider'."""
    parts = split_criterion(criterion)
    if not parts:
        return criterion
    key = parts[1]
    if key in RENAMES:
        return RENAMES[key]
    key = key.split(":", 1)[-1]
    if key.endswith("_one_cm"):
        key = key[: -len("_one_cm")]
    # "interact_with_furnace" -> "Furnace"; the category already says these are
    # interactions, and the prefix costs a third of the column.
    for prefix in ("interact_with_", "inspect_"):
        if key.startswith(prefix):
            key = key[len(prefix):]
    words = [WORD_FIXES.get(w.capitalize(), w.capitalize()) for w in key.split("_")]
    return " ".join(words)


def unit_of(criterion):
    """'distance', 'time', 'damage' or 'count'."""
    parts = split_criterion(criterion)
    if not parts:
        return "count"
    section, key = parts
    if section != "minecraft:custom":
        return "count"
    if key.endswith("_one_cm"):
        return "distance"
    if key in TICK_STATS:
        return "time"
    if key in DAMAGE_STATS:
        return "damage"
    return "count"


def format_value(criterion, value):
    unit = unit_of(criterion)
    if unit == "distance":
        km = value / 100_000
        return f"{km:,.1f}km" if km >= 1 else f"{value / 100:,.0f}m"
    if unit == "time":
        hours = value / 20 / 3600
        return f"{hours:,.1f}h" if hours >= 1 else f"{value / 20 / 60:,.0f}m"
    if unit == "damage":
        return f"{value / 10:,.0f}"
    return f"{int(value):,}"


# Discord renders ANSI escapes inside ```ansi blocks; this is the only way to
# get both aligned columns and colour in a message.
RESET = "[0m"
DIM = "[0;30m"
HEAD = "[1;37m"
NAME = "[0;37m"
LEADER = "[1;36m"
VALUE = "[0;33m"
GOLD = "[1;33m"

NAME_WIDTH = 23
LEADER_WIDTH = 13
VALUE_WIDTH = 9
# Escape sequences count against the 4096-character description, so each row
# carries colour only on the leader — colouring every cell tripled the escape
# overhead and roughly doubled the number of cards needed.
BLOCK_LIMIT = 3850


def _clip(text, width):
    return text if len(text) <= width else text[: width - 1] + "…"


def table(rows, subject_header, value_header):
    """An ANSI code block: the statistic, who leads it, and their value."""
    out = [f"{HEAD}{_clip(subject_header, NAME_WIDTH - 1).ljust(NAME_WIDTH)}"
           f"{'Leader'.ljust(LEADER_WIDTH)}"
           f"{value_header.rjust(VALUE_WIDTH)}{RESET}",
           f"{DIM}{'─' * (NAME_WIDTH + LEADER_WIDTH + VALUE_WIDTH)}{RESET}"]
    for index, (label, leader, shown) in enumerate(rows):
        colour = GOLD if index == 0 else LEADER
        out.append(
            f"{_clip(label, NAME_WIDTH - 1).ljust(NAME_WIDTH)}"
            f"{colour}{_clip(leader, LEADER_WIDTH - 1).ljust(LEADER_WIDTH)}{RESET}"
            f"{shown.rjust(VALUE_WIDTH)}")
    return "```ansi\n" + "\n".join(out) + "\n```"


def leaders(criteria, per_player, minimum=1):
    """[(label, leader, formatted value, total)] for criteria anyone has scored."""
    rows = []
    for criterion in criteria:
        scores = {name: values.get(criterion, 0) for name, values in per_player.items()}
        scores = {n: v for n, v in scores.items() if v >= minimum}
        if not scores:
            continue
        leader = max(scores, key=lambda n: scores[n])
        rows.append((display_name(criterion), leader,
                     format_value(criterion, scores[leader]), scores[leader],
                     criterion))
    # Sort by the number actually on screen, so the ordering is one a reader
    # can verify rather than a hidden total.
    rows.sort(key=lambda r: -r[3])
    return rows


def untouched(criteria, per_player):
    """Statistics nobody has scored yet."""
    out = []
    for criterion in criteria:
        if not any(values.get(criterion, 0) for values in per_player.values()):
            out.append(display_name(criterion))
    return out


def category_embeds(criteria, per_player, curated=(), top=0, title_suffix=""):
    """One embed per category, in CATEGORIES order.

    Categories with more rows than fit in a description are split across
    consecutive embeds so nothing is silently dropped.

    curated is the datapack's list. Statistics nobody has scored are only worth
    naming when something nominated them as a goal: read from the save the
    unscored set would be every item in the game.
    """
    by_section = {}
    for criterion in criteria:
        parts = split_criterion(criterion)
        if parts:
            by_section.setdefault(parts[0], []).append(criterion)
    curated_by_section = {}
    for criterion in curated:
        parts = split_criterion(criterion)
        if parts:
            curated_by_section.setdefault(parts[0], []).append(criterion)

    embeds = []
    for key, emoji, title, name_header, value_header, sections in CATEGORIES:
        mine = [c for section in sections for c in by_section.get(section, [])]
        if not mine:
            continue
        rows = scored = leaders(mine, per_player)
        if top:
            rows = rows[:top]
        missing = untouched(
            [c for section in sections for c in curated_by_section.get(section, [])],
            per_player)

        pages, page = [], []
        for row in rows:
            page.append(row[:3])
            if len(table(page, name_header, value_header)) > BLOCK_LIMIT:
                page.pop()
                pages.append(page)
                page = [row[:3]]
        if page or not pages:
            pages.append(page)

        for index, page in enumerate(pages):
            description = table(page, name_header, value_header) if page else \
                "```\nnothing recorded yet\n```"
            if index == len(pages) - 1 and missing:
                shown = ", ".join(missing[:24])
                more = f" *…and {len(missing) - 24} more*" if len(missing) > 24 else ""
                description += f"\n**Not yet recorded ({len(missing)}):** {shown}{more}"
            suffix = f"  ({index + 1}/{len(pages)})" if len(pages) > 1 else ""
            embeds.append((f"{key}{index if index else ''}", {
                "title": f"{emoji}  {title}{title_suffix}{suffix}",
                "description": description[:4096],
                "color": CATEGORY_COLOURS.get(key, 0x5865F2),
                "footer": {"text": (f"{len(rows)} of {len(scored)} recorded"
                                    if len(rows) != len(scored)
                                    else f"{len(scored)} recorded")
                                   + "  •  leader shown  •  updated"},
            }))
    return embeds


def overview_embed(criteria, per_player, players_tracked):
    """A summary card: how much is tracked, and who leads the most of it."""
    wins = {}
    for row in leaders(criteria, per_player):
        wins[row[1]] = wins.get(row[1], 0) + 1
    ranked = sorted(wins.items(), key=lambda kv: -kv[1])
    medals = ["🥇", "🥈", "🥉"]
    lines = [f"> {medals[i] if i < 3 else f'`{i + 1}.`'}  **{name}** — "
             f"leads **{count}** statistic{'s' if count != 1 else ''}"
             for i, (name, count) in enumerate(ranked[:10])]
    recorded = len(leaders(criteria, per_player))
    by_section = {}
    for criterion in criteria:
        parts = split_criterion(criterion)
        if parts:
            by_section[parts[0]] = by_section.get(parts[0], 0) + 1
    breakdown = "  ·  ".join(
        f"**{by_section.get(section, 0)}** {title.lower()}"
        for _, _, title, _, _, sections in CATEGORIES
        for section in sections[:1] if by_section.get(section))
    return {
        "title": "🏛️  Hall of Fame",
        "description": (f"### Who leads the most statistics\n"
                        + ("\n".join(lines) if lines
                           else "> 💤  *nothing recorded yet*")
                        + f"\n\n**{recorded:,}** statistics recorded across "
                          f"**{players_tracked}** players.\n> {breakdown}"),
        "color": 0xFFD700,
        "footer": {"text": "all-time  •  every statistic in the world save  •  updated"},
    }


def weekly_embed(criteria, deltas, label):
    """A single card of what actually moved this week, best categories first."""
    sections = []
    by_section = {}
    for criterion in criteria:
        parts = split_criterion(criterion)
        if parts:
            by_section.setdefault(parts[0], []).append(criterion)

    for key, emoji, title, _, _, wanted in CATEGORIES:
        mine = [c for section in wanted for c in by_section.get(section, [])]
        rows = leaders(mine, deltas)
        if not rows:
            continue
        top = rows[:6]
        body = "\n".join(f"> {emoji if i == 0 else '　'}  {label_}  ·  "
                         f"**{leader}** {shown}"
                         for i, (label_, leader, shown, _, _) in enumerate(top))
        sections.append(f"**{title}**\n{body}")

    description = f"### {label}\n" + (
        "\n".join(sections) if sections else "> 💤  *nothing recorded this week yet*")
    return {
        "title": "📈  This Week's Statistics",
        "description": description[:4096],
        "color": 0x2ECC71,
        "footer": {"text": "resets Monday  •  change since the week began"},
    }
