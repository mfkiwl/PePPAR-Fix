"""Small pure-Python helpers for peppar-mon.

Kept separate from ``app.py`` so unit tests can exercise them without
pulling in Textual — useful in CI or in any venv that has the engine
deps but not the monitor deps.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Optional


def format_uptime(elapsed_s: float) -> str:
    """Render ``elapsed_s`` seconds as ``Dd Hh Mm`` (no zero-padding).

    Seconds are intentionally dropped — uptime is coarse by nature and
    a once-per-second repaint of a seconds digit would draw the eye
    away from the time-of-day line above it in the display.
    """
    td = timedelta(seconds=int(elapsed_s))
    days = td.days
    hours, remainder = divmod(td.seconds, 3600)
    minutes = remainder // 60
    return f"{days}d {hours}h {minutes}m"


def format_elapsed_short(elapsed_s: float) -> str:
    """Render elapsed as a compact Xh Ym Zs / Xm Ys / Xs string.

    Used by the death-detection indicator, which needs to show
    second-scale precision at short durations ("DOWN — 35s")
    AND drop seconds once the gap becomes minutes-scale
    ("DOWN — 5m 2s" → seconds kept; "DOWN — 1h 5m" → seconds
    dropped for readability).  Threshold: drop seconds once
    elapsed crosses one hour.
    """
    total = max(0, int(elapsed_s))
    hours, rem = divmod(total, 3600)
    minutes, seconds = divmod(rem, 60)
    if hours > 0:
        return f"{hours}h {minutes}m"
    if minutes > 0:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


# Python logging's default format puts a comma between seconds and the
# milliseconds field.  strptime can't consume "," as a decimal separator,
# so we match with a regex and stitch the microseconds back on manually.
_LOG_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),(\d{3})\b")


def parse_log_timestamp(line: str) -> Optional[datetime]:
    """Extract a naive-local ``datetime`` from the start of a log line.

    Expected format: ``"2026-04-19 21:09:12,007 INFO ..."`` — the default
    ``logging.Formatter`` produces this.  Returns ``None`` if the line
    doesn't begin with a timestamp (blank lines, tracebacks, etc.).

    The returned datetime is naive and in the host's local timezone —
    matches ``datetime.now()``, so subtracting the two produces a clean
    ``timedelta`` for uptime without any tz arithmetic.  That convention
    is safe as long as the monitor and engine run on the same host and
    neither crosses a DST boundary mid-run.
    """
    m = _LOG_TS_RE.match(line)
    if m is None:
        return None
    base = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
    return base.replace(microsecond=int(m.group(2)) * 1000)
