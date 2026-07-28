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


def main():
    test_reader()
    print()
    test_events()
    print()
    if failures:
        print(f"{len(failures)} FAILED: {', '.join(failures)}")
        return 1
    print("all passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
