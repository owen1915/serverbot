"""A minimal RCON client for the server this bot lives next to.

Credentials come straight out of server.properties, so there is nothing to
configure and no password copied into config.json. Every call opens a fresh
connection: commands are rare (a tick query every half minute, the odd
broadcast), the server restarts whenever it likes, and a held-open socket
would just be another thing to reconnect.

Standard library only, like everything else here.
"""

import os
import re
import socket
import struct

SECTION_RE = re.compile("§.")

LOGIN, COMMAND = 3, 2


class RconError(Exception):
    pass


def read_credentials(server_dir):
    """(port, password) if RCON is enabled in server.properties, else None."""
    props = {}
    try:
        with open(os.path.join(server_dir, "server.properties"),
                  encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, value = line.partition("=")
                    props[key] = value
    except OSError:
        return None
    if props.get("enable-rcon") != "true" or not props.get("rcon.password"):
        return None
    try:
        port = int(props.get("rcon.port") or 25575)
    except ValueError:
        port = 25575
    return port, props["rcon.password"]


def _packet(request_id, kind, body):
    payload = (struct.pack("<ii", request_id, kind)
               + body.encode("utf-8") + b"\x00\x00")
    return struct.pack("<i", len(payload)) + payload


def _read_packet(sock):
    def recvall(n):
        buf = b""
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                raise RconError("connection closed")
            buf += chunk
        return buf

    (length,) = struct.unpack("<i", recvall(4))
    payload = recvall(length)
    request_id, kind = struct.unpack("<ii", payload[:8])
    return request_id, kind, payload[8:-2].decode("utf-8", "replace")


def commands(server_dir, texts, timeout=5):
    """Run several commands over one connection; their responses in order.

    Returns None when RCON is off, unreachable, or refuses the password —
    callers treat all three as "the server cannot be asked right now".
    """
    creds = read_credentials(server_dir)
    if not creds:
        return None
    port, password = creds
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall(_packet(1, LOGIN, password))
            request_id, _, _ = _read_packet(sock)
            if request_id == -1:
                raise RconError("authentication rejected")
            out = []
            for text in texts:
                sock.sendall(_packet(2, COMMAND, text))
                _, _, body = _read_packet(sock)
                out.append(SECTION_RE.sub("", body))
            return out
    except (OSError, RconError):
        return None


def command(server_dir, text, timeout=5):
    """Run one command on the local server; its response as plain text."""
    out = commands(server_dir, [text], timeout)
    return out[0] if out else None


# ------------------------------------------------------------- tick health --

TICK_MSPT_RE = re.compile(r"Average time per tick:\s*([\d.]+)\s*ms", re.I)
TICK_TARGET_RE = re.compile(r"Target tick rate:\s*([\d.]+)", re.I)
TICK_P95_RE = re.compile(r"P95:\s*([\d.]+)\s*ms", re.I)


def parse_tick(out):
    """{'mspt', 'tps', 'target', 'p95'} from /tick query output, or None.

    TPS is derived: the server holds its target rate until a tick costs more
    than its slot, so min(target, 1000/mspt) is the honest figure.
    """
    if not out:
        return None
    m = TICK_MSPT_RE.search(out)
    if not m:
        return None
    mspt = float(m.group(1))
    target_m = TICK_TARGET_RE.search(out)
    target = float(target_m.group(1)) if target_m else 20.0
    p95_m = TICK_P95_RE.search(out)
    return {
        "mspt": mspt,
        "tps": min(target, 1000.0 / mspt) if mspt > 0 else target,
        "target": target,
        "p95": float(p95_m.group(1)) if p95_m else None,
    }


def query_tick(server_dir):
    """The server's own tick health over RCON, or None if it cannot be asked."""
    return parse_tick(command(server_dir, "tick query"))


# --------------------------------------------------------------- broadcast --

# Discord copy leans on markdown and emoji; Minecraft's font renders neither.
MARKDOWN_RE = re.compile(r"[*_`]+")
NON_TEXT_RE = re.compile(
    "[\U0001F000-\U0001FAFF←-⯿☀-➿️]")


def broadcast(server_dir, text, color="gold"):
    """Say something to everybody in the game. Quietly does nothing without RCON."""
    plain = NON_TEXT_RE.sub("", MARKDOWN_RE.sub("", text)).strip()
    if not plain:
        return None
    import json
    return command(server_dir, "tellraw @a " + json.dumps(
        [{"text": "✦ ", "color": "yellow"},
         {"text": plain, "color": color}]))


def whisper(server_dir, player, text, color="gray"):
    """Say something to one player only. Quietly does nothing without RCON."""
    plain = NON_TEXT_RE.sub("", MARKDOWN_RE.sub("", text)).strip()
    if not plain:
        return None
    import json
    return command(server_dir, f"tellraw {player} " + json.dumps(
        [{"text": "✦ ", "color": "yellow"},
         {"text": plain, "color": color, "italic": True}]))
