"""Enforce the peppar-mon ↔ peppar-fix boundary.

peppar_mon is a log-file consumer.  It must not import anything from
the engine (``peppar_fix`` package, any top-level ``scripts/`` module
like ``peppar_fix_engine``, ``ppp_ar``, ``solve_ppp``).  If it ever
does, refactors in the engine start breaking the monitor and the
separation we designed falls apart.

The test works by static scan: walk every .py file under peppar_mon/,
scan for forbidden ``import`` / ``from ... import`` statements, fail
if any match.  No runtime imports — catches violations even in code
paths that tests don't exercise.

Run: ``PYTHONPATH=. python -m unittest peppar_mon.tests.test_boundary``
"""

from __future__ import annotations

import ast
import pathlib
import unittest


FORBIDDEN_ROOTS: set[str] = {
    # Engine package and every top-level module under scripts/.  If
    # a new engine-side module appears, add its top-level name here.
    "peppar_fix",
    "peppar_fix_engine",
    "ppp_ar",
    "solve_ppp",
    "solve_pseudorange",
    "solve_dualfreq",
    "broadcast_eph",
    "ssr_corrections",
    "lambda_ar",
    "ntrip_client",
    "realtime_ppp",
    "ppp_corrections",
    "ticc",
    "phc_servo",
}


def _top_level(module: str) -> str:
    return module.split(".", 1)[0] if module else ""


def _forbidden_imports_in(path: pathlib.Path) -> list[tuple[int, str]]:
    """Return list of (line, module) for forbidden imports in one file."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _top_level(alias.name) in FORBIDDEN_ROOTS:
                    hits.append((node.lineno, alias.name))
        elif isinstance(node, ast.ImportFrom):
            # Relative imports (level > 0) have module=None or a local
            # name; ignore them — they stay inside peppar_mon.
            if node.level == 0 and node.module:
                if _top_level(node.module) in FORBIDDEN_ROOTS:
                    hits.append((node.lineno, node.module))
    return hits


class BoundaryTest(unittest.TestCase):
    def test_no_engine_imports(self):
        pkg_root = pathlib.Path(__file__).resolve().parent.parent
        violations: list[str] = []
        for py in pkg_root.rglob("*.py"):
            if "tests" in py.relative_to(pkg_root).parts:
                # Skip the test tree itself — we're allowed to read
                # engine filenames as strings here.
                continue
            for line, module in _forbidden_imports_in(py):
                violations.append(f"{py.relative_to(pkg_root)}:{line}: {module}")
        self.assertEqual(
            violations,
            [],
            msg=(
                "peppar-mon must not import from the engine.  "
                "Violations:\n  " + "\n  ".join(violations)
            ),
        )


if __name__ == "__main__":
    unittest.main()
