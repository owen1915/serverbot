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
    bot.fun = mcbot.fun_state(bot.state)
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
    bot.fun = mcbot.fun_state(bot.state)
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
    bot.fun = mcbot.fun_state(bot.state)
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


def test_day_boundary():
    """The statistics day turns over at 03:00 Eastern, summer and winter."""
    import datetime as dt

    def eastern(y, mo, d, h, mi=0):
        """Epoch for a wall-clock Eastern moment, via the offset itself."""
        naive = dt.datetime(y, mo, d, h, mi, tzinfo=dt.timezone.utc).timestamp()
        return naive - mcbot.eastern_offset(naive)

    check("winter is EST", mcbot.eastern_offset(eastern(2026, 1, 15, 12)), -5 * 3600)
    check("summer is EDT", mcbot.eastern_offset(eastern(2026, 7, 15, 12)), -4 * 3600)
    # 2026: DST starts Sunday 8 March, ends Sunday 1 November.
    check("the day before the spring change is still EST",
          mcbot.eastern_offset(eastern(2026, 3, 7, 12)), -5 * 3600)
    check("the day after the spring change is EDT",
          mcbot.eastern_offset(eastern(2026, 3, 9, 12)), -4 * 3600)
    check("the day after the autumn change is EST",
          mcbot.eastern_offset(eastern(2026, 11, 2, 12)), -5 * 3600)

    check("02:59 still belongs to the previous day",
          mcbot.day_key(eastern(2026, 7, 15, 2, 59)), "2026-07-14")
    check("03:01 begins the new day",
          mcbot.day_key(eastern(2026, 7, 15, 3, 1)), "2026-07-15")
    check("midday belongs to that day",
          mcbot.day_key(eastern(2026, 7, 15, 12)), "2026-07-15")
    check("a winter night rolls over at 03:00 too",
          mcbot.day_key(eastern(2026, 1, 15, 2, 59)), "2026-01-14")

    noon = eastern(2026, 7, 15, 12)
    check("the day began at 03:00 Eastern",
          mcbot.eastern_clock(mcbot.day_start_epoch(noon)), "03:00")
    check("the day is 24 hours long",
          round((mcbot.day_start_epoch(noon + 86400)
                 - mcbot.day_start_epoch(noon)) / 3600), 24)


def test_noise_floor():
    """Statistics somebody has barely touched must not earn a row."""
    import gamestats
    per_player = {
        "ann": {"minecraft.picked_up:minecraft.iron_ingot": 1,
                "minecraft.picked_up:minecraft.cobblestone": 4000,
                "minecraft.killed_by:minecraft.warden": 1,
                "minecraft.custom:minecraft.walk_one_cm": 500},
    }
    shown = [row[0] for row in gamestats.leaders(list(per_player["ann"]), per_player)]
    check("one iron ingot picked up is hidden", "Iron Ingot" in shown, False)
    check("four thousand cobblestone is shown", "Cobblestone" in shown, True)
    check("a single death to a warden is still shown", "Warden" in shown, True)
    check("five metres walked is hidden", "Walk" in shown, False)
    check("the hidden statistics are counted, not dropped silently",
          gamestats.below_floor(list(per_player["ann"]), per_player), 2)

    # The scale knob moves the bar in both directions.
    lowered = [row[0] for row in
               gamestats.leaders(list(per_player["ann"]), per_player, scale=0.01)]
    check("lowering the scale shows the small statistics again",
          "Iron Ingot" in lowered, True)
    # At scale 100 the picked-up floor is 3,200 — the cobblestone clears it and
    # nothing else comes close.
    raised = [row[0] for row in
              gamestats.leaders(list(per_player["ann"]), per_player, scale=100)]
    check("raising the scale hides all but the biggest", raised, ["Cobblestone"])


def test_summary_totals():
    """The glance card adds whole sections up, and drops what did not happen."""
    import gamestats
    criteria = ["minecraft.mined:minecraft.stone",
                "minecraft.mined:minecraft.dirt",
                "minecraft.custom:minecraft.deaths",
                "minecraft.custom:minecraft.walk_one_cm",
                "minecraft.custom:minecraft.sprint_one_cm",
                "minecraft.custom:minecraft.fish_caught"]
    per_player = {
        "ann": {"minecraft.mined:minecraft.stone": 400,
                "minecraft.custom:minecraft.deaths": 2,
                "minecraft.custom:minecraft.walk_one_cm": 150_000},
        "bob": {"minecraft.mined:minecraft.stone": 100,
                "minecraft.mined:minecraft.dirt": 40,
                "minecraft.custom:minecraft.sprint_one_cm": 250_000},
    }
    got = {label: value for _, label, value in
           gamestats.summary_totals(criteria, per_player)}
    check("a section is summed across items and players",
          got.get("Blocks Mined"), "540")
    check("deaths are counted", got.get("Deaths"), "2")
    check("distance is summed across every way of moving",
          got.get("Distance Travelled"), "4.0km")
    check("a statistic nobody scored is left off the card",
          "Fish Caught" in got, False)
    check("totals ignore the noise floor, unlike table rows",
          got.get("Blocks Mined") is not None, True)


def test_celebrations():
    """Awards, records, streaks and challenges — the funstats layer."""
    import datetime as dtm
    import funstats

    criteria = ["minecraft.mined:minecraft.stone",
                "minecraft.killed:minecraft.zombie",
                "minecraft.custom:minecraft.deaths",
                "minecraft.custom:minecraft.walk_one_cm"]
    per_player = {
        "ann": {"minecraft.mined:minecraft.stone": 900,
                "minecraft.custom:minecraft.walk_one_cm": 700_000},
        "bob": {"minecraft.killed:minecraft.zombie": 30,
                "minecraft.custom:minecraft.deaths": 4},
    }
    agg = funstats.aggregate(criteria, per_player)
    check("aggregation sums a section", agg["ann"]["mined"], 900)
    check("aggregation reads custom stats", agg["bob"]["deaths"], 4)
    check("absent statistics aggregate to zero", agg["ann"]["deaths"], 0)

    for name, extra in (("ann", {"playtime": 7200, "chat": 0}),
                        ("bob", {"playtime": 3600, "chat": 12})):
        agg[name].update(extra)
    awards = {title: winner for _, title, winner, _, _ in
              funstats.compute_awards(agg, "2026-07-29")}
    check("the miner award goes to the miner", awards.get("Deepest Commitment"), "ann")
    check("the death award goes to the dier", awards.get("Most Deaths"), "bob")
    check("chatterbox goes to the talker", awards.get("Chatterbox"), "bob")
    check("nobody fished, so no fishing award", "Gone Fishin'" in awards, False)
    check("homebody rewards the one who stayed put", awards.get("Homebody"), "bob")
    check("awards are deterministic for a day",
          funstats.compute_awards(agg, "2026-07-29"),
          funstats.compute_awards(agg, "2026-07-29"))

    ledger = {}
    check("a first record is set silently",
          funstats.check_records(ledger, agg, "Jul 28"), [])
    check("the ledger remembers the holder", ledger["mined"]["holder"], "ann")
    agg2 = {"bob": dict(agg["bob"], mined=2_000, playtime=8000)}
    broken = funstats.check_records(ledger, agg2, "Jul 29")
    check("beating a record is announced",
          [(b[1], b[2], b[4]) for b in broken
           if b[1] == "Most blocks mined"],
          [("Most blocks mined", "bob", "ann")])
    check("a smaller day does not touch the ledger",
          funstats.check_records(ledger, {"ann": dict(agg["ann"])}, "Jul 30"), [])

    streaks = {}
    day1 = dtm.date(2026, 7, 27)
    funstats.update_streaks(streaks, {"ann": 3600, "bob": 200}, day1)
    check("ten minutes is required for a streak", "bob" in streaks, False)
    m, _ = funstats.update_streaks(streaks, {"ann": 3600}, day1 + dtm.timedelta(1))
    m, _ = funstats.update_streaks(streaks, {"ann": 3600}, day1 + dtm.timedelta(2))
    check("a three-day streak is a milestone", m, [("ann", 3)])
    _, b = funstats.update_streaks(streaks, {}, day1 + dtm.timedelta(3))
    check("a missed day breaks the streak", b, [("ann", 3)])
    check("the best is remembered", streaks["ann"]["best"], 3)

    c1 = funstats.pick_challenge("2026-07-29")
    check("the challenge is stable across restarts",
          c1, funstats.pick_challenge("2026-07-29"))
    standings = funstats.challenge_standings(("⛏️", "Mine", "mined", "count"), agg2)
    check("challenge standings rank by the stat", standings[0][0], "bob")

    pace = funstats.pace_lines(
        {"ann": 90 * 3600},
        [{"ann": 7200}, {"ann": 7200}, {"ann": 7200}],
        [10, 100, 250], dtm.date(2026, 7, 29))
    check("pace projects the next milestone", "100h" in pace[0], True)
    check("pace needs history, not a lifetime average",
          funstats.pace_lines({"ann": 90 * 3600}, [{"ann": 7200}],
                              [100], dtm.date(2026, 7, 29)), [])


def test_chat_transcript():
    """The chat channel repeats everything, whatever the main channel announces."""
    bot = mcbot.Bot.__new__(mcbot.Bot)
    bot.state = mcbot.new_state()
    bot.fun = mcbot.fun_state(bot.state)
    bot.stats = {}
    bot.dirty = False
    bot.perf = None

    original_say, original_cfg = mcbot.say, dict(mcbot.CFG)
    mcbot.say = lambda c: None
    # Every announcement switch off: the transcript must not depend on them.
    mcbot.CFG.update({"webhook_chat": "https://example.invalid/hook",
                      "relay_chat": False, "announce_deaths": False,
                      "announce_advancements": False, "main_events": False})
    del mcbot._chat_queue[:]
    try:
        for message in [
            "owen1915 joined the game",
            "<owen1915> hello everyone",
            "owen1915 has made the advancement [Diamonds!]",
            "owen1915 was blown up by Creeper",
            "[voicechat] Sent secret to owen1915",
            "owen1915 left the game",
        ]:
            bot.handle(message, 1000.0)
        queued = list(mcbot._chat_queue)
    finally:
        mcbot.say = original_say
        mcbot.CFG.clear()
        mcbot.CFG.update(original_cfg)
        del mcbot._chat_queue[:]

    def count(fragment):
        return sum(fragment in line for line in queued)

    check("join mirrored", count("joined the game"), 1)
    check("chat mirrored even with relay_chat off", count("hello everyone"), 1)
    check("advancement mirrored with announcements off", count("Diamonds!"), 1)
    check("death mirrored with announcements off", count("blown up by Creeper"), 1)
    check("server internals stay out of the transcript", count("voicechat"), 0)
    check("leave mirrored", count("left the game"), 1)
    check("nothing else was queued", len(queued), 5)


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
    test_day_boundary()
    print()
    test_noise_floor()
    print()
    test_summary_totals()
    print()
    test_celebrations()
    print()
    test_chat_transcript()
    print()
    if failures:
        print(f"{len(failures)} FAILED: {', '.join(failures)}")
        return 1
    print("all passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
