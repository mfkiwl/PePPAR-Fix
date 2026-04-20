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
    args = parser.parse_args()
    PepparMonApp(log_path=args.log_file).run()


if __name__ == "__main__":
    main()
