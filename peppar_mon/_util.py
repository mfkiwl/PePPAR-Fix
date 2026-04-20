"""Small pure-Python helpers for peppar-mon.

Kept separate from ``app.py`` so unit tests can exercise them without
pulling in Textual — useful in CI or in any venv that has the engine
deps but not the monitor deps.
"""

from __future__ import annotations

from datetime import timedelta


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
