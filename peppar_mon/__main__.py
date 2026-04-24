"""Entry point: ``python -m peppar_mon LOG_FILE`` runs the app.

Usage::

    python -m peppar_mon PATH/TO/engine.log

Equivalent launcher invocation::

    scripts/peppar-mon PATH/TO/engine.log
    scripts/peppar-mon --web 8000 PATH/TO/engine.log

The log file argument is mandatory — peppar-mon's whole job is to
render state reconstructed from the engine's log, so without one
there's nothing useful to display.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from peppar_mon.app import PepparMonApp


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="peppar-mon",
        description=(
            "Status display for the PePPAR-Fix engine.  Replays the log "
            "file from the start to recover state (engine start time, "
            "SV states, AR counts, etc.), then follows the file for "
            "live updates."
        ),
    )
    parser.add_argument(
        "log_file",
        type=Path,
        metavar="LOG_FILE",
        help=(
            "Path to the PePPAR-Fix engine log file.  The reader waits "
            "for this file to exist if it doesn't yet, then replays it "
            "from the first line and follows for new content."
        ),
    )
    parser.add_argument(
        "--fleet",
        action="store_true",
        help=(
            "Enable fleet mode.  Publishes this host's state to a "
            "UDP-multicast peer bus + subscribes to peers, and "
            "adds a fleet-summary row (cross-host position Δ, "
            "Anchored counts, ZTD spread).  See "
            "docs/peer-state-sharing.md."
        ),
    )
    parser.add_argument(
        "--fleet-host",
        default=None,
        metavar="NAME",
        help=(
            "Host identifier to publish as on the fleet bus.  "
            "Defaults to the system hostname.  Must be unique "
            "within the fleet."
        ),
    )
    parser.add_argument(
        "--fleet-antenna-ref",
        default="",
        metavar="NAME",
        help=(
            "Antenna identifier (e.g. 'UFO1').  Peers sharing the "
            "same antenna_ref get cross-antenna position "
            "comparison in the fleet summary.  Empty (default) "
            "means 'don't claim a specific antenna'."
        ),
    )
    args = parser.parse_args()
    PepparMonApp(
        log_path=args.log_file,
        fleet_mode=args.fleet,
        fleet_host=args.fleet_host,
        fleet_antenna_ref=args.fleet_antenna_ref,
    ).run()


if __name__ == "__main__":
    main()
