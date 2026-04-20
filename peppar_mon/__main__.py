"""Entry point: ``python -m peppar_mon`` runs the app.

Usage::

    python -m peppar_mon                          # clock + monitor uptime
    python -m peppar_mon --log PATH/TO/engine.log # clock + engine uptime

The launcher at ``scripts/peppar-mon`` forwards ``"$@"`` here, so the
same flags work through ``peppar-mon --log PATH`` and
``peppar-mon --web 8000 --log PATH`` (Textual's ``serve`` subcommand
passes its own ``--`` separator; until we need one, bare flags suffice).
"""

from __future__ import annotations

import argparse
from pathlib import Path

from peppar_mon.app import PepparMonApp


def main() -> None:
    parser = argparse.ArgumentParser(prog="peppar-mon")
    parser.add_argument(
        "--log",
        type=Path,
        default=None,
        help=(
            "Path to the PePPAR-Fix engine log file.  The reader replays "
            "it from the start to recover the engine's start time and "
            "any accumulated state, then follows the file for live "
            "updates.  Without --log the display shows peppar-mon's own "
            "uptime (labelled \"(monitor)\") as a placeholder."
        ),
    )
    args = parser.parse_args()
    PepparMonApp(log_path=args.log).run()


if __name__ == "__main__":
    main()
