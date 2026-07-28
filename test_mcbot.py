#!/usr/bin/env python3
"""Tests for the two pieces most likely to break silently: following
latest.log across appends and rotations, and classifying log lines.

    python test_mcbot.py

Needs no Minecraft server and no network — it posts nothing.
"""

import os
import sys
import tempfile

import mcbot

# Keep test output out of the real watcher's log.
mcbot.LOG_FILE = os.path.join(tempfile.mkdtemp(), "test.log")

HEADER =("[05:36:30] [main/INFO]: Loading Minecraft 26.2 with Fabric Loader 0.19.3\n"
          "[05:36:31] [main/INFO]: " + "x" * 400 + "\n")

failures = []


def check(label, got, want):
    if got == want:
        print(f"PASS  {label}")
        return
    failures.append(label)
    print(f"FAIL  {label}\n        got:  {got!r}\n        want: {want!r}")


def append(path, text):
    with open(path, "a", encoding="utf-8") as f:
        f.write(text)


def test_reader():
    path = os.path.join(tempfile.mkdtemp(), "latest.log")
    append(path, HEADER)
    reader = mcbot.LogReader(path)
    reader.seek_end()
    check("quiet file yields nothing", reader.read_new(), [])

    append(path, "[06:00:01] [Server thread/INFO]: owen1915 joined the game\n")
    check("appended line is picked up", reader.read_new(),
          ["[06:00:01] [Server thread/INFO]: owen1915 joined the game"])

    # A line the server is still writing must wait for its newline.
    append(path, "[06:00:02] [Server thread/INFO]: partial")
    check("partial line is held back", reader.read_new(), [])
    append(path, " line here\n")
    check("partial line emitted once complete", reader.read_new(),
          ["[06:00:02] [Server thread/INFO]: partial line here"])

    # Chat is UTF-8; byte offsets and character counts must not be confused.
    append(path, "[06:00:03] [Server thread/INFO]: <Joksuu_> héllo 🌍 wörld\n"
                 "[06:00:04] [Server thread/INFO]: PowerRubik joined the game\n")
    check("multi-byte characters do not desynchronise the offset", reader.read_new(),
          ["[06:00:03] [Server thread/INFO]: <Joksuu_> héllo 🌍 wörld",
           "[06:00:04] [Server thread/INFO]: PowerRubik joined the game"])

    # Rotation, where the replacement file is shorter than our position.
    os.remove(path)
    append(path, "[07:00:00] [main/INFO]: Loading Minecraft 26.2 with Fabric Loader 0.19.3\n")
    check("rotation to a shorter file is detected", reader.read_new(),
          ["[07:00:00] [main/INFO]: Loading Minecraft 26.2 with Fabric Loader 0.19.3"])

    # Rotation, where the replacement is already past our position.
    other = mcbot.LogReader(path)
    os.remove(path)
    append(path, HEADER)
    other.read_new()
    os.remove(path)
    append(path, "[08:00:00] [main/INFO]: Loading Minecraft 26.2 with Fabric Loader 0.19.3\n"
                 "[08:00:01] [main/INFO]: " + "y" * 400 + "\n"
                 "[08:00:02] [Server thread/INFO]: Sam1915 joined the game\n")
    lines = other.read_new()
    check("rotation to a longer file is detected", lines[0] if lines else None,
          "[08:00:00] [main/INFO]: Loading Minecraft 26.2 with Fabric Loader 0.19.3")


def test_events():
    bot = mcbot.Bot.__new__(mcbot.Bot)
    bot.state = mcbot.new_state()
    bot.stats = {}
    bot.dirty = False
    bot.perf = None

    posted = []
    original_say = mcbot.say
    mcbot.say = posted.append
    try:
        for message in [
            "owen1915 joined the game",
            "PowerRubik joined the game",
            "[voicechat] Sent secret to PowerRubik",
            "<owen1915> yo",
            "PowerRubik has made the advancement [Diamonds!]",
            "PowerRubik was blown up by Creeper",
            "Named entity SulfurCube['cube'/100251, l='ServerLevel[X]', x=1, y=2, z=3]"
            " died: cube was squished too much",
            "owen1915 lost connection: Disconnected",
            "Joksuu_ was shot by Skeleton",   # not online, so not a real death here
            "PowerRubik left the game",
        ]:
            bot.handle(message, 1000.0)
    finally:
        mcbot.say = original_say

    def count(fragment):
        return sum(fragment in p for p in posted)

    check("joins announced", count("joined the server"), 2)
    check("chat not relayed when relay_chat is off", count("💬"), 0)
    check("advancement announced", count("advancement"), 1)
    check("player death announced", count("blown up by Creeper"), 1)
    check("mob death ignored", count("SulfurCube"), 0)
    check("lost connection ignored", count("lost connection"), 0)
    check("death of an offline player ignored", count("Joksuu_"), 0)
    check("leave announced", count("left the server"), 1)
    check("concurrent-player record announced", count("New record"), 1)
    check("online set correct at the end", sorted(bot.state["players"]), ["owen1915"])


def test_bedrock_names():
    """Floodgate prefixes Bedrock players with '.', which must not be skipped."""
    bot = mcbot.Bot.__new__(mcbot.Bot)
    bot.state = mcbot.new_state()
    bot.stats = {}
    bot.dirty = False
    bot.perf = None

    posted = []
    original_say = mcbot.say
    mcbot.say = posted.append
    try:
        for message in [
            ".BedrockKid joined the game",
            ".BedrockKid has made the advancement [Stone Age]",
            ".BedrockKid was slain by Zombie",
            ".BedrockKid left the game",
        ]:
            bot.handle(message, 1000.0)
    finally:
        mcbot.say = original_say

    check("bedrock join announced", sum("joined the server" in p for p in posted), 1)
    check("bedrock advancement announced", sum("Stone Age" in p for p in posted), 1)
    check("bedrock death announced", sum("slain by Zombie" in p for p in posted), 1)
    check("bedrock leave announced", sum("left the server" in p for p in posted), 1)
    check("bedrock player removed from the online set", bot.state["players"], {})


def test_lag_parsing():
    """Both wordings of the server's 'Can't keep up!' warning must parse."""
    recorded = []

    class Recorder:
        def record_lag(self, now, ms, ticks):
            recorded.append((ms, ticks))

    bot = mcbot.Bot.__new__(mcbot.Bot)
    bot.state = mcbot.new_state()
    bot.stats = {}
    bot.dirty = False
    bot.perf = Recorder()

    original_say = mcbot.say
    mcbot.say = lambda c: None
    try:
        bot.handle("Can't keep up! Is the server overloaded? "
                   "Running 2145ms or 42 ticks behind", 1000.0)
        bot.handle("Can't keep up! Did the system time change, or is the server "
                   "overloaded? Running 5000ms behind, skipping 100 tick(s)", 1000.0)
    finally:
        mcbot.say = original_say

    check("both lag wordings parsed", recorded, [(2145, 42), (5000, 100)])


def test_availability():
    """Uptime must be measured over observed time, not assumed for gaps."""
    import perf
    now = 1_000_000.0
    hour = 3600.0

    # Four hours of ordinary 30-second sampling, the third hour spent down.
    ledger = perf.Availability({})
    moment = now - 4 * hour
    while moment <= now:
        down = now - 2 * hour <= moment < now - 1 * hour
        ledger.observe(not down, moment)
        moment += 30

    pct, observed, downtime = ledger.summary(4 * hour, now)
    check("an hour of downtime is measured", round(downtime / 60), 60)
    check("uptime is 75% of a four-hour window", round(pct), 75)
    check("the whole window counts as observed", round(observed / hour), 4)

    # A blind spot must count as unknown rather than silently as uptime.
    gapped = perf.Availability({})
    gapped.observe(True, now - 4 * hour)
    gapped.observe(True, now)
    pct, observed, _ = gapped.summary(4 * hour, now)
    check("a sampling gap is not counted as observed", round(observed), 0)
    check("uptime is unknown rather than a claimed 100%", pct, None)

    # An outage shorter than the sampling interval still lands in the ledger.
    brief = perf.Availability({})
    brief.observe(True, now - 120)
    brief.observe(False, now - 90)
    brief.observe(True, now - 60)
    brief.observe(True, now)
    _, _, downtime = brief.summary(300, now)
    check("a brief outage is recorded", round(downtime), 30)


def test_weekly_statistic_deltas():
    """This week's statistics are a change since a baseline, not a total.

    With no baseline every lifetime total reads as this week's activity, so the
    baseline must be seeded for every player before the card is drawn.
    """
    current = {"ann": {"mined": 100, "kills": 5}, "bob": {"mined": 7}}
    baseline = {"ann": {"mined": 90, "kills": 5}, "bob": {"mined": 7}}
    got = mcbot.subtract(current, baseline)
    check("only the increase counts", got["ann"], {"mined": 10})
    check("statistics that did not move are dropped", "kills" in got["ann"], False)
    check("a player who did nothing has an empty row", got["bob"], {})

    # An unseeded player is credited everything — which is exactly why
    # refresh_cards seeds the baseline for every player it sees.
    unseeded = mcbot.subtract({"cat": {"mined": 500}}, {})
    check("an unseeded player would be credited their whole history",
          unseeded["cat"], {"mined": 500})


def test_statistic_criteria():
    """Criteria come from the datapacks and resolve into the stats files."""
    import gamestats
    check("a kill criterion resolves",
          gamestats.split_criterion("minecraft.killed:minecraft.cave_spider"),
          ("minecraft:killed", "minecraft:cave_spider"))
    check("a bare criterion resolves",
          gamestats.split_criterion("deathCount"),
          ("minecraft:custom", "minecraft:deaths"))
    check("names are humanised",
          gamestats.display_name("minecraft.killed:minecraft.cave_spider"),
          "Cave Spider")
    check("interaction prefixes are dropped",
          gamestats.display_name("minecraft.custom:minecraft.interact_with_furnace"),
          "Furnace")
    check("distances are read off a real stats file",
          gamestats.value_of({"minecraft:custom": {"minecraft:walk_one_cm": 250_000}},
                             "minecraft.custom:minecraft.walk_one_cm"), 250_000)
    check("distances format as kilometres",
          gamestats.format_value("minecraft.custom:minecraft.walk_one_cm", 250_000),
          "2.5km")
    check("playtime formats as hours",
          gamestats.format_value("minecraft.custom:minecraft.play_time", 72_000 * 20),
          "20.0h")
    check("counts stay counts",
          gamestats.format_value("minecraft.killed:minecraft.zombie", 1234), "1,234")

    per_player = {"ann": {"minecraft.killed:minecraft.zombie": 10},
                  "bob": {"minecraft.killed:minecraft.zombie": 3}}
    rows = gamestats.leaders(["minecraft.killed:minecraft.zombie"], per_player)
    check("the leader is the highest scorer", rows[0][1], "ann")
    check("statistics nobody scored are listed as untouched",
          gamestats.untouched(["minecraft.killed:minecraft.warden"], per_player),
          ["Warden"])


def main():
    test_reader()
    print()
    test_events()
    print()
    test_bedrock_names()
    print()
    test_lag_parsing()
    print()
    test_availability()
    print()
    test_weekly_statistic_deltas()
    print()
    test_statistic_criteria()
    print()
    if failures:
        print(f"{len(failures)} FAILED: {', '.join(failures)}")
        return 1
    print("all passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
