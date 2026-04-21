"""Web-serve entry point: ``python -m peppar_mon._serve PORT LOG_FILE``.

Uses the ``textual-serve`` package to wrap the TUI in a web terminal.
The older in-tree ``textual serve`` subcommand was removed in Textual
8.x and split into the standalone ``textual-serve`` package, which
takes a shell *command* rather than an ``APP:CLASS`` import target.

We construct the command so the same ``python -m peppar_mon LOG_FILE``
that drives the direct TUI also drives the web-served version —
keeps the two codepaths behaving identically.
"""

from __future__ import annotations

import argparse
import shlex
import sys


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="peppar-mon --web",
        description=(
            "Serve the peppar-mon TUI over HTTP via textual-serve.  "
            "Open http://<host>:<port>/ in a browser once the server "
            "starts.  Ctrl+C to stop."
        ),
    )
    parser.add_argument("port", type=int)
    parser.add_argument("log_file")
    args = parser.parse_args()

    # Lazy import — keeps direct-TUI callers from needing textual-serve
    # installed if they never use --web.
    try:
        from textual_serve.server import Server
    except ImportError as exc:  # pragma: no cover — install hint
        print(
            "peppar-mon --web requires the `textual-serve` package.\n"
            "Install it with:  pip install textual-serve\n"
            "(It ships separately from textual as of 8.x.)",
            file=sys.stderr,
        )
        raise SystemExit(2) from exc

    # Build the subprocess command textual-serve will spawn.  shlex.quote
    # keeps log paths with spaces / special characters safe.  We target
    # the same interpreter running us so venvs follow through.
    command = " ".join([
        shlex.quote(sys.executable),
        "-m", "peppar_mon",
        shlex.quote(args.log_file),
    ])
    Server(command=command, port=args.port).serve()


if __name__ == "__main__":
    main()
