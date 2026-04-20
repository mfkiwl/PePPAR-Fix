"""peppar-mon — status display for PePPAR-Fix.

Separate package from ``peppar_fix``.  Hard rule: this package MUST NOT
import anything from ``peppar_fix`` or from the engine scripts.  Its
only inputs are log files the engine writes and any future on-disk
state files.  Enforced by ``peppar_mon/tests/test_boundary.py``.

That rule exists so the two can evolve independently — refactors in
the engine don't break the monitor, and a monitor crash can't perturb
the engine.  The engine has no knowledge that peppar-mon exists.

Entry point: ``python -m peppar_mon`` or ``scripts/peppar-mon``.
"""

__version__ = "0.0.1"
