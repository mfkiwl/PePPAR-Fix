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


def format_uncertainty(sigma_m: Optional[float]) -> str:
    """Render a position σ as a humane ``± X unit`` string.

    Scale the unit to the σ magnitude so the number stays at one
    or two significant digits:

        σ < 0.1 m       → ``± 2.3 cm``    (one decimal on cm)
        σ < 1 m         → ``± 23 cm``     (round to cm)
        σ < 10 m        → ``± 1.2 m``     (one decimal on m)
        σ ≥ 10 m        → ``± 12 m``      (round to m)
        None / negative → ``± ?``

    ``±`` is a plain Unicode character, not markup.
    """
    if sigma_m is None or sigma_m < 0:
        return "± ?"
    if sigma_m < 0.1:
        return f"± {sigma_m * 100:.1f} cm"
    if sigma_m < 1.0:
        return f"± {sigma_m * 100:.0f} cm"
    if sigma_m < 10.0:
        return f"± {sigma_m:.1f} m"
    return f"± {sigma_m:.0f} m"


def uncertain_decimals_deg(sigma_m: Optional[float]) -> int:
    """How many *trailing* decimal places of a degree-valued
    coordinate are below the σ quantum?

    Used by the AntennaPositionLine widget to decide which
    trailing digits of lat/lon to render dim.  Returns 0 when σ
    is unknown or so large that every digit is uncertain.

    Math: 1° of latitude ≈ 111_320 m at Earth's radius.  A σ of
    N m on the ground corresponds to N / 111320 ° of latitude
    uncertainty.  The number of *confident* decimal places is
    ``floor(-log10(σ_deg))``; trailing decimals beyond that are
    uncertain.  We take the total decimal count in the formatted
    string minus the confident count — that's the dim span.

    (Longitude is slightly coarser than lat at non-equator
    latitudes — cos(lat) tightens the meters-per-degree — but
    at mid-latitudes the effect is < 1.5× and a conservative
    common bound is fine for shading.)
    """
    if sigma_m is None or sigma_m <= 0:
        return 0
    import math
    sigma_deg = sigma_m / 111_320.0
    # Confident decimal places = smallest N where 10^-N >= σ_deg.
    confident = max(0, int(math.floor(-math.log10(sigma_deg))))
    return confident


def uncertain_decimals_m(sigma_m: Optional[float]) -> int:
    """Same idea for altitude (already in metres).

    Returns the number of confident decimal places; digits beyond
    that are uncertain.  A σ of 0.023 m gives 1 confident
    decimal (10^-1 = 0.1 ≥ 0.023 > 10^-2 = 0.01), so ``198.247``
    would show the ``2`` confident and ``47`` dim.
    """
    if sigma_m is None or sigma_m <= 0:
        return 0
    import math
    return max(0, int(math.floor(-math.log10(sigma_m))))


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
