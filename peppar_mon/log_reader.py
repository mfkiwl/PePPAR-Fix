"""Two-phase engine-log consumer: replay then follow.

Why "reader" not "tailer": the monitor needs the engine's start time
(for uptime) and any state the engine has accumulated since startup.
That requires reading the log from the beginning before we can do
anything useful.  Once caught up, we continue following the file for
live updates — the same way ``tail -n +1 -f`` would.

The reader runs in its own daemon thread and exposes a thread-safe
``LogState`` snapshot.  Consumers (the Textual app) poll the state on
a timer — no callbacks, no event queue plumbing for the first pass.
When we add real state (SV-state histogram, NL fix counts, etc.) the
same pattern scales: the reader writes into the state dataclass,
readers read it.

Scope today: extract the engine's start time from the first
timestamped line.  Everything else is a hook for the next commit.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from peppar_mon._util import parse_log_timestamp

log = logging.getLogger(__name__)


# Poll interval for the follow phase.  Fast enough to feel live on a
# 1 Hz engine, slow enough that a quiet log doesn't spin a core.
_FOLLOW_POLL_S = 0.2

# Retry delay when the log file doesn't exist yet.  The engine might
# not have started or might be writing somewhere else; don't busy-loop.
_WAIT_FOR_FILE_S = 1.0


@dataclass
class LogState:
    """Thread-safe-ish snapshot of what the reader has inferred.

    The writing thread (LogReader) sets fields via simple attribute
    writes.  The reading thread (Textual app) reads them.  Python's
    GIL makes single-attribute reads and writes atomic, so no lock is
    needed for the scalars currently exposed here.  When fields like
    dicts or lists land, a lock goes in.
    """

    #: Parsed timestamp from the first timestamped line we saw.  None
    #: until we've observed one.  Naive-local, same convention as
    #: ``datetime.now()`` — see ``parse_log_timestamp``.
    engine_start_time: Optional[datetime] = None

    #: Line count processed (for debugging — will become a heartbeat
    #: once we have real parsing).
    lines_read: int = 0

    #: Last-line timestamp (useful to detect a stalled engine — if the
    #: log hasn't advanced in a while, the engine likely crashed).
    last_line_time: Optional[datetime] = None


class LogReader:
    """Threaded engine-log consumer.

    Usage::

        reader = LogReader(Path("/var/log/peppar-fix.log"))
        reader.start()
        ...
        reader.state.engine_start_time   # readable from any thread
        reader.stop()

    The thread is a daemon so process exit doesn't wait on it.  Errors
    inside the thread are logged at WARNING and don't propagate — a
    dead reader just stops updating state; the UI keeps working.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.state = LogState()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ── Lifecycle ──────────────────────────────────────────────── #

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="peppar-mon-log-reader", daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=timeout)

    # ── Reader thread body ─────────────────────────────────────── #

    def _run(self) -> None:
        try:
            self._wait_for_file()
            if self._stop.is_set():
                return
            with self.path.open("r", encoding="utf-8", errors="replace") as f:
                # Replay: consume everything currently in the file.
                self._consume(f, follow=False)
                if self._stop.is_set():
                    return
                # Follow: block-poll for new content.
                self._consume(f, follow=True)
        except Exception:
            # Any exception kills the reader thread but not the app.
            # Log it and return — the state snapshot freezes at whatever
            # we'd inferred before the failure.
            log.warning("LogReader crashed", exc_info=True)

    def _wait_for_file(self) -> None:
        """Block until the log file exists or stop is signalled.

        The engine might not be running yet.  We don't want to error
        out — just wait politely.
        """
        while not self._stop.is_set():
            if self.path.exists():
                return
            if self._stop.wait(timeout=_WAIT_FOR_FILE_S):
                return  # stop signalled

    def _consume(self, f, *, follow: bool) -> None:
        """Read lines from ``f``.

        ``follow=False`` reads to EOF and returns.  ``follow=True``
        keeps polling for new content, returning only when stop is set.
        """
        while not self._stop.is_set():
            line = f.readline()
            if not line:
                if not follow:
                    return
                # EOF during follow — sleep briefly and try again.  The
                # Event.wait call makes stop() responsive without
                # blocking the full poll interval.
                if self._stop.wait(timeout=_FOLLOW_POLL_S):
                    return
                continue
            self._ingest(line)

    # ── Per-line processing ────────────────────────────────────── #

    def _ingest(self, line: str) -> None:
        """Update state from one raw log line.

        Only the timestamp extraction is wired up today.  Structured
        ``[TAG] key=value`` parsing lands in the next commit.
        """
        self.state.lines_read += 1
        ts = parse_log_timestamp(line)
        if ts is None:
            return
        if self.state.engine_start_time is None:
            self.state.engine_start_time = ts
        self.state.last_line_time = ts
