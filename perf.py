"""Performance and availability monitoring for the Minecraft host.

Three things are tracked:

  the Minecraft server   responsiveness (status-ping latency), lag warnings the
                         server logs when it falls behind, and how long it has
                         been up
  the java process       CPU, resident memory against the configured heap,
                         thread count
  the host machine       CPU per core, RAM, disk on the world's drive, disk and
                         network throughput, and how long the PC has been up

Availability is an honest ledger rather than a ratio of samples: intervals are
recorded as up, down, or *unknown* (the watcher itself was not running), and
uptime percentages are reported over observed time only.

psutil supplies the host and process metrics. If it is missing this module
degrades to server-side numbers instead of failing.
"""

import collections
import platform
import socket
import time

try:
    import psutil
except ImportError:  # host metrics unavailable; server metrics still work
    psutil = None

AVAILABLE = psutil is not None

# A gap longer than this between samples means the watcher was not running and
# the server's state over that period is genuinely unknown.
GAP_SECONDS = 300
LEDGER_RETENTION_DAYS = 40
HISTORY_SAMPLES = 240  # rolling window kept in memory for sparklines

SPARK = "▁▂▃▄▅▆▇█"


# ------------------------------------------------------------- formatting --

def spark(values, width=24):
    """A tiny inline chart, e.g. ▁▁▂▅█▃▂▁."""
    values = [v for v in values if v is not None][-width:]
    if not values:
        return "—"
    low, high = min(values), max(values)
    if high - low < 1e-9:
        return SPARK[0] * len(values)
    step = (high - low) / (len(SPARK) - 1)
    return "".join(SPARK[min(len(SPARK) - 1, int((v - low) / step))] for v in values)


def bar(pct, width=20):
    """A proportional bar, e.g. ██████░░░░░░░░░░░░░░."""
    pct = max(0.0, min(100.0, pct or 0.0))
    filled = int(round(width * pct / 100))
    return "█" * filled + "░" * (width - filled)


def fmt_bytes(n):
    if n is None:
        return "—"
    for unit in ("B", "kB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:,.1f} {unit}" if unit != "B" else f"{n:,.0f} B"
        n /= 1024
    return f"{n:,.1f} TB"


def fmt_rate(bytes_per_second):
    if bytes_per_second is None:
        return "—"
    return fmt_bytes(bytes_per_second) + "/s"


def fmt_span(seconds):
    if seconds is None:
        return "—"
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


def _mean(values):
    values = [v for v in values if v is not None]
    return sum(values) / len(values) if values else None


# ----------------------------------------------------------- availability --

class Availability:
    """A ledger of up / down / unknown intervals, persisted across restarts."""

    def __init__(self, store):
        self.store = store
        store.setdefault("ledger", [])
        store.setdefault("last_seen", None)

    def observe(self, up, now):
        ledger = self.store["ledger"]
        last_seen = self.store["last_seen"]
        # A long gap means this watcher was down; do not claim to know what the
        # server was doing while nobody was looking.
        if last_seen is not None and now - last_seen > GAP_SECONDS:
            ledger.append({"from": last_seen, "state": "unknown"})
        state = "up" if up else "down"
        if not ledger or ledger[-1]["state"] != state:
            ledger.append({"from": now, "state": state})
        self.store["last_seen"] = now
        self._prune(now)

    def _prune(self, now):
        cutoff = now - LEDGER_RETENTION_DAYS * 86400
        ledger = self.store["ledger"]
        recent = [e for e in ledger if e["from"] >= cutoff]
        older = [e for e in ledger if e["from"] < cutoff]
        if older:  # keep the state in force at the cutoff
            head = dict(older[-1])
            head["from"] = cutoff
            recent.insert(0, head)
        self.store["ledger"] = recent

    def _intervals(self, now):
        ledger = self.store["ledger"]
        end_of_last = self.store["last_seen"] or now
        for i, entry in enumerate(ledger):
            start = entry["from"]
            end = ledger[i + 1]["from"] if i + 1 < len(ledger) else end_of_last
            yield start, end, entry["state"]

    def summary(self, window, now):
        """(uptime percent, observed seconds, downtime seconds) over a window."""
        window_start = now - window
        totals = {"up": 0.0, "down": 0.0, "unknown": 0.0}
        for start, end, state in self._intervals(now):
            start, end = max(start, window_start), min(end, now)
            if end > start:
                totals[state] += end - start
        observed = totals["up"] + totals["down"]
        pct = 100.0 * totals["up"] / observed if observed else None
        return pct, observed, totals["down"]

    def current_streak(self, now):
        """(state, seconds) for the run of time the server has been in it."""
        intervals = list(self._intervals(now))
        if not intervals:
            return None, 0
        state = intervals[-1][2]
        start = intervals[-1][0]
        for begin, _, this_state in reversed(intervals):
            if this_state != state:
                break
            start = begin
        return state, now - start

    def last_incident(self, now):
        """(ended_at, duration) of the most recent completed outage."""
        latest = None
        for start, end, state in self._intervals(now):
            if state == "down" and end < now - 1:
                latest = (end, end - start)
        return latest


# --------------------------------------------------------------- sampling --

class Monitor:
    """Samples the host, the java process and the server, and renders a card."""

    def __init__(self, store, server_dir, heap_max_bytes=None, log=print):
        self.store = store
        store.setdefault("availability", {})
        store.setdefault("alerts", {})
        store.setdefault("lag", [])
        self.availability = Availability(store["availability"])
        self.server_dir = server_dir
        self.heap_max = heap_max_bytes
        self.log = log
        self.history = collections.deque(maxlen=HISTORY_SAMPLES)
        self._proc = None
        self._last_disk = None
        self._last_net = None
        self._last_sample_time = None
        if psutil:
            psutil.cpu_percent(percpu=True)  # prime the delta-based readings

    # -- the java process -------------------------------------------------

    def _java(self):
        """The running Minecraft server process, found by its working directory."""
        if self._proc is not None and self._proc.is_running():
            return self._proc
        if not psutil:
            return None
        target = self.server_dir.replace("/", "\\").rstrip("\\").lower()
        for proc in psutil.process_iter(["name", "cmdline"]):
            try:
                if not (proc.info["name"] or "").lower().startswith("java"):
                    continue
                cwd = (proc.cwd() or "").replace("/", "\\").rstrip("\\").lower()
                cmdline = " ".join(proc.info["cmdline"] or []).lower()
                if cwd == target or "fabric.jar" in cmdline or "server.jar" in cmdline:
                    proc.cpu_percent()  # prime
                    self._proc = proc
                    return proc
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        self._proc = None
        return None

    # -- one sample -------------------------------------------------------

    def sample(self, now, ping_ms, players, tick=None):
        """Take a reading and fold it into the history and the ledger.

        tick is the /tick query result when RCON can supply one — the
        server's own measure of tick cost, which no amount of pinging from
        outside can see.
        """
        self.availability.observe(ping_ms is not None, now)
        point = {"t": now, "ping": ping_ms, "players": players,
                 "up": ping_ms is not None}
        if tick:
            point["mspt"] = tick.get("mspt")
            point["tps"] = tick.get("tps")
            point["mspt_p95"] = tick.get("p95")

        if psutil:
            elapsed = (now - self._last_sample_time) if self._last_sample_time else None
            self._last_sample_time = now

            per_core = psutil.cpu_percent(percpu=True)
            point["cpu"] = sum(per_core) / len(per_core) if per_core else None
            point["cpu_cores"] = per_core

            memory = psutil.virtual_memory()
            point["ram_used"] = memory.total - memory.available
            point["ram_total"] = memory.total
            point["ram_pct"] = memory.percent

            try:
                disk = psutil.disk_usage(self.server_dir)
                point["disk_free"] = disk.free
                point["disk_total"] = disk.total
                point["disk_pct"] = disk.percent
            except OSError:
                pass

            point["disk_read"], point["disk_write"] = self._rate(
                psutil.disk_io_counters(), "_last_disk",
                ("read_bytes", "write_bytes"), elapsed)
            point["net_recv"], point["net_sent"] = self._rate(
                psutil.net_io_counters(), "_last_net",
                ("bytes_recv", "bytes_sent"), elapsed)

            proc = self._java()
            if proc:
                try:
                    with proc.oneshot():
                        cores = psutil.cpu_count() or 1
                        point["java_cpu"] = proc.cpu_percent() / cores
                        point["java_rss"] = proc.memory_info().rss
                        point["java_threads"] = proc.num_threads()
                        point["java_uptime"] = now - proc.create_time()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    self._proc = None

        self.history.append(point)
        self._accumulate(now, point)
        return point

    ACCUMULATED = ("cpu", "ram_pct", "ping", "players", "java_cpu", "java_rss",
                   "mspt")

    def _accumulate(self, now, point):
        """Fold the sample into today's running totals.

        The in-memory history only spans a couple of hours, so daily figures
        are accumulated as they happen rather than reconstructed later.
        """
        day = time.strftime("%Y-%m-%d", time.localtime(now))
        today = self.store.setdefault("today", {})
        if today.get("day") != day:
            if today.get("day"):
                self.store["yesterday"] = today
            today = {"day": day, "started": now}
            self.store["today"] = today
        for key in self.ACCUMULATED:
            value = point.get(key)
            if value is None:
                continue
            today[f"{key}_sum"] = today.get(f"{key}_sum", 0) + value
            today[f"{key}_max"] = max(today.get(f"{key}_max", 0), value)
            today[f"{key}_n"] = today.get(f"{key}_n", 0) + 1

    def rolled_over_day(self):
        """The finished day's totals, once, the first time it is asked for."""
        finished = self.store.get("yesterday")
        if finished and not finished.get("reported"):
            finished["reported"] = True
            return finished
        return None

    def daily_embed(self, day, now):
        """A short report on a finished day."""
        def average(key):
            count = day.get(f"{key}_n", 0)
            return day[f"{key}_sum"] / count if count else None

        def peak(key):
            return day.get(f"{key}_max")

        started = day.get("started", now - 86400)
        pct, observed, downtime = self.availability.summary(now - started, now)
        lag = [e for e in self.store["lag"] if e["t"] >= started]

        lines = [f"### {day['day']}"]
        if pct is not None:
            lines.append(f"> `uptime ` **{pct:.2f}%**"
                         + (f"  ·  {fmt_span(downtime)} of downtime" if downtime >= 60
                            else "  ·  no downtime"))
        peak_players = peak("players")
        if peak_players:
            lines.append(f"> `players` peak **{int(peak_players)}** online")
        if average("ping") is not None:
            lines.append(f"> `ping   ` avg **{average('ping'):.0f} ms**  ·  "
                         f"peak {peak('ping'):.0f} ms")
        if average("cpu") is not None:
            lines.append(f"> `host   ` cpu avg **{average('cpu'):.0f}%** "
                         f"(peak {peak('cpu'):.0f}%)  ·  "
                         f"ram avg {average('ram_pct'):.0f}% "
                         f"(peak {peak('ram_pct'):.0f}%)")
        if average("java_cpu") is not None:
            lines.append(f"> `server ` cpu avg **{average('java_cpu'):.0f}%** "
                         f"(peak {peak('java_cpu'):.0f}%)  ·  "
                         f"memory peak {fmt_bytes(peak('java_rss'))}")
        if average("mspt") is not None:
            lines.append(f"> `tick   ` avg **{average('mspt'):.1f} ms/t**  ·  "
                         f"peak {peak('mspt'):.1f} ms/t")
        lines.append(f"> `lag    ` " + (f"**{len(lag)}** tick overrun(s)" if lag
                                        else "**none**"))
        return {
            "title": "🗓️  Daily Report",
            "description": "\n".join(lines),
            "color": 0x5865F2,
            "footer": {"text": "previous day  •  posted"},
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z",
        }

    def _rate(self, counters, attr, fields, elapsed):
        """Per-second rates from a pair of cumulative counters."""
        if counters is None:
            return None, None
        current = tuple(getattr(counters, f) for f in fields)
        previous = getattr(self, attr)
        setattr(self, attr, current)
        if previous is None or not elapsed or elapsed <= 0:
            return None, None
        return tuple(max(0, c - p) / elapsed for c, p in zip(current, previous))

    # -- server-side lag --------------------------------------------------

    def record_lag(self, now, milliseconds, ticks):
        """Note a 'Can't keep up!' warning from the server log."""
        self.store["lag"].append({"t": now, "ms": milliseconds, "ticks": ticks})
        cutoff = now - 30 * 86400
        self.store["lag"] = [e for e in self.store["lag"] if e["t"] >= cutoff]

    def lag_since(self, since):
        return [e for e in self.store["lag"] if e["t"] >= since]

    # -- helpers over the in-memory history --------------------------------

    def series(self, key, since=None):
        points = self.history
        if since is not None:
            points = [p for p in points if p["t"] >= since]
        return [p.get(key) for p in points]

    def latest(self, key):
        for point in reversed(self.history):
            if point.get(key) is not None:
                return point[key]
        return None

    # -- alerting ---------------------------------------------------------

    def _sustained(self, key, threshold, samples):
        """True when the last few readings are all above a threshold."""
        recent = [v for v in self.series(key)[-samples:] if v is not None]
        return len(recent) >= samples and all(v >= threshold for v in recent)

    def check_alerts(self, now, thresholds, cooldown=1800):
        """Conditions worth saying out loud, each rate-limited by a cooldown."""
        seen = self.store["alerts"]
        found = []

        def raise_alert(key, message):
            if now - seen.get(key, 0) >= cooldown:
                seen[key] = now
                found.append(message)

        if self._sustained("cpu", thresholds["host_cpu"], 5):
            raise_alert("host_cpu",
                        f":chart_with_upwards_trend: Host CPU has been above "
                        f"**{thresholds['host_cpu']}%** for several minutes "
                        f"(now {self.latest('cpu'):.0f}%).")
        if self._sustained("ram_pct", thresholds["host_ram"], 5):
            raise_alert("host_ram",
                        f":brain: Host memory is at **{self.latest('ram_pct'):.0f}%** "
                        f"({fmt_bytes(self.latest('ram_used'))} of "
                        f"{fmt_bytes(self.latest('ram_total'))}).")
        free = self.latest("disk_free")
        if free is not None and free < thresholds["disk_free_gb"] * 1024 ** 3:
            raise_alert("disk", f":floppy_disk: Only **{fmt_bytes(free)}** of disk space "
                                f"is left on the drive holding the world.")
        heap = self.heap_max
        rss = self.latest("java_rss")
        if heap and rss and rss / heap * 100 >= thresholds["heap_pct"]:
            raise_alert("heap", f":coffee: The Minecraft process is using "
                                f"**{fmt_bytes(rss)}** of its {fmt_bytes(heap)} heap.")
        recent_lag = self.lag_since(now - 900)
        if recent_lag:
            worst = max(e["ms"] for e in recent_lag)
            raise_alert("lag", f":turtle: The server fell behind **{len(recent_lag)}** "
                               f"time(s) in the last 15 minutes (worst: {worst} ms).")
        return found

    # -- the card ---------------------------------------------------------

    def embed(self, now, address):
        state, streak = self.availability.current_streak(now)
        up = state == "up"
        lines = []

        # --- the Minecraft server
        players = self.latest("players") or 0
        headline = (f"### {'🟢' if up else '🔴'}  Server {'up' if up else 'DOWN'} "
                    f"for {fmt_span(streak)}"
                    + (f"  ·  {players} online" if up else ""))
        lines.append(headline)
        pings = [p for p in self.series("ping") if p is not None]
        if pings:
            hour_ago = now - 3600
            recent = [p for p in self.series("ping", hour_ago) if p is not None]
            average = _mean(recent) or 0
            lines.append(f"> `ping   ` **{pings[-1]:.0f} ms**  {spark(pings)}  "
                         f"avg {average:.0f} ms · max {max(recent or pings):.0f} ms")
        mspts = [m for m in self.series("mspt") if m is not None]
        if mspts:
            tps = self.latest("tps")
            p95 = self.latest("mspt_p95")
            lines.append(f"> `tick   ` **{mspts[-1]:.1f} ms/t**  {spark(mspts)}  "
                         f"TPS **{tps:.1f}**" + (f" · p95 {p95:.1f} ms" if p95 else ""))
        day_lag = self.lag_since(now - 86400)
        lines.append(f"> `lag    ` " + (
            f"**{len(day_lag)}** tick overrun(s) in 24h "
            f"(worst {max(e['ms'] for e in day_lag)} ms)" if day_lag
            else "**none** in 24h — the server has not fallen behind"))

        # --- availability
        lines.append("### Availability")
        for label, window in (("24h", 86400), ("7d", 7 * 86400), ("30d", 30 * 86400)):
            pct, observed, downtime = self.availability.summary(window, now)
            if pct is None:
                lines.append(f"> `{label:<4}` no observations yet")
                continue
            note = f"  ·  {fmt_span(downtime)} down" if downtime >= 60 else ""
            # Say how much of the window was actually watched, so a short
            # history cannot masquerade as a full month of perfect uptime.
            coverage = ("" if observed >= window * 0.95
                        else f"  ·  {fmt_span(observed)} observed")
            lines.append(f"> `{label:<4}` {bar(pct, 16)} **{pct:.2f}%**{note}{coverage}")
        incident = self.availability.last_incident(now)
        if incident:
            ended, duration = incident
            lines.append(f"> last outage {fmt_span(now - ended)} ago, "
                         f"lasted {fmt_span(duration)}")

        # --- the java process
        if self.latest("java_rss") is not None:
            lines.append("### Minecraft process")
            cpu = self.latest("java_cpu")
            lines.append(f"> `cpu    ` **{cpu:.0f}%** of the machine  "
                         f"{spark(self.series('java_cpu'))}")
            rss = self.latest("java_rss")
            if self.heap_max:
                pct = rss / self.heap_max * 100
                lines.append(f"> `memory ` {bar(pct, 16)} **{fmt_bytes(rss)}** "
                             f"of {fmt_bytes(self.heap_max)} heap")
            else:
                lines.append(f"> `memory ` **{fmt_bytes(rss)}**")
            lines.append(f"> `threads` {self.latest('java_threads')}  ·  "
                         f"process up {fmt_span(self.latest('java_uptime'))}")

        # --- the host
        if self.latest("cpu") is not None:
            lines.append(f"### Host — {platform.node()}")
            cores = self.latest("cpu_cores") or []
            lines.append(f"> `cpu    ` {bar(self.latest('cpu'), 16)} "
                         f"**{self.latest('cpu'):.0f}%** over {len(cores)} cores  "
                         f"{spark(self.series('cpu'))}")
            lines.append(f"> `ram    ` {bar(self.latest('ram_pct'), 16)} "
                         f"**{fmt_bytes(self.latest('ram_used'))}** of "
                         f"{fmt_bytes(self.latest('ram_total'))}")
            if self.latest("disk_total"):
                lines.append(f"> `disk   ` {bar(self.latest('disk_pct'), 16)} "
                             f"**{fmt_bytes(self.latest('disk_free'))}** free of "
                             f"{fmt_bytes(self.latest('disk_total'))}")
            lines.append(f"> `network` ↓ {fmt_rate(self.latest('net_recv'))}  "
                         f"↑ {fmt_rate(self.latest('net_sent'))}   "
                         f"`disk io` ↓ {fmt_rate(self.latest('disk_read'))}  "
                         f"↑ {fmt_rate(self.latest('disk_write'))}")
            if psutil:
                lines.append(f"> `uptime ` PC up "
                             f"{fmt_span(now - psutil.boot_time())}")
        elif not AVAILABLE:
            lines.append("### Host\n> *psutil is not installed — host metrics unavailable*")

        colour = 0x57F287 if up else 0xED4245
        if up and (self._sustained("cpu", 90, 3) or (self.latest("ram_pct") or 0) >= 92):
            colour = 0xFEE75C  # degraded but serving

        return {
            "title": "📈  Performance",
            "description": "\n".join(lines),
            "color": colour,
            "footer": {"text": f"{address}  •  sampled every "
                               f"{int(self._interval())}s  •  updated"},
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z",
        }

    def _interval(self):
        if len(self.history) < 2:
            return 30
        span = self.history[-1]["t"] - self.history[0]["t"]
        return max(1, round(span / (len(self.history) - 1)))


def parse_heap_max(cmdline):
    """Bytes from a -Xmx flag, e.g. -Xmx8000M -> 8388608000."""
    units = {"K": 1024, "M": 1024 ** 2, "G": 1024 ** 3}
    for argument in cmdline or []:
        if not argument.lower().startswith("-xmx"):
            continue
        value = argument[4:].strip()
        multiplier = units.get(value[-1:].upper(), 1)
        digits = value[:-1] if multiplier > 1 else value
        try:
            return int(digits) * multiplier
        except ValueError:
            return None
    return None


def detect_heap_max(server_dir):
    """Read -Xmx off the running server, so the heap need not be configured."""
    if not psutil:
        return None
    target = server_dir.replace("/", "\\").rstrip("\\").lower()
    for proc in psutil.process_iter(["name", "cmdline"]):
        try:
            if not (proc.info["name"] or "").lower().startswith("java"):
                continue
            cwd = (proc.cwd() or "").replace("/", "\\").rstrip("\\").lower()
            cmdline = proc.info["cmdline"] or []
            if cwd == target or "fabric.jar" in " ".join(cmdline).lower():
                return parse_heap_max(cmdline)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return None
