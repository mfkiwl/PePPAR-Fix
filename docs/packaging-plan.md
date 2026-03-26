# Packaging Plan: From Repo Checkout to Installable Tool

## Goal

PePPAR Fix should be installable on any Linux host meeting the
requirements, while continuing to work as a direct repo checkout for
development.  The tool is too narrowly-focused for PyPI or apt, but
GitHub Releases provide versioned, updatable distribution.

Target install experience:

```bash
pip install git+https://github.com/bobvan/PePPAR-Fix@v0.1.0
peppar-fix-engine --serial /dev/gnss-top --servo /dev/ptp0 ...
```

Upgrade:

```bash
pip install --upgrade git+https://github.com/bobvan/PePPAR-Fix@v0.2.0
```

## Current state (post-reorganization)

```
scripts/          16 .py files — engine runtime
  peppar_fix/     14 .py files — core library package
tools/            standalone diagnostics (not installed)
tests/            automated tests
drivers/          kernel driver patches (not installed)
```

The `peppar_fix/` sub-package is already a proper Python package.  The
16 top-level `.py` files in `scripts/` import each other by bare name
(`from solve_ppp import ...`) and rely on all being in the same
directory.

A `pyproject.toml` stub is in the repo root with metadata, version,
dependencies, and commented-out entry points.

## What needs to happen

### Phase 1: Editable install for development (low effort)

`pip install -e .` already works for the `peppar_fix` sub-package (the
`[tool.setuptools.packages.find]` section finds it under `scripts/`).
But the top-level scripts (`peppar_fix_engine.py`, `solve_ppp.py`, etc.)
are not importable as `peppar_fix.*` — they're siblings, not children.

To make dev installs work today:

1. Keep running scripts directly from repo checkout (current workflow).
2. `pip install -e .` makes `from peppar_fix import ...` work from
   anywhere — useful for tools, tests, notebooks.
3. Top-level scripts still need to be run from `scripts/` or via
   `python3 scripts/peppar_fix_engine.py`.

No code changes needed.  This is the current state.

### Phase 2: Flatten into one package (big refactor, one commit)

Move the 16 top-level scripts into the `peppar_fix` package:

```
scripts/peppar_fix/
  __init__.py
  engine.py             ← peppar_fix_engine.py
  bootstrap.py          ← phc_bootstrap.py
  solve_ppp.py          ← solve_ppp.py (name unchanged)
  solve_pseudorange.py
  solve_dualfreq.py
  broadcast_eph.py
  ssr_corrections.py
  ppp_corrections.py
  realtime_ppp.py
  ntrip_client.py
  ntrip_caster.py
  rtcm_encoder.py
  ticc.py
  configure_f9t.py
  rx_config.py          ← peppar_rx_config.py
  host_config.py        ← peppar_host_config.py
  servo.py              (already here)
  error_sources.py      (already here)
  ptp_device.py         (already here)
  ...                   (already here)
```

Every `from solve_ppp import X` becomes `from peppar_fix.solve_ppp
import X`.  This is mechanical — ~100 import lines across ~30 files —
but touches everything and must be done in one commit to avoid a broken
intermediate state.

After this, `pip install .` installs the full package, and entry points
work:

```toml
[project.scripts]
peppar-fix-engine = "peppar_fix.engine:main"
peppar-fix-bootstrap = "peppar_fix.bootstrap:main"
peppar-fix-configure = "peppar_fix.configure_f9t:main"
```

### Phase 3: GitHub Releases and versioning

1. Tag releases with semver: `v0.1.0`, `v0.2.0`, etc.
2. Create GitHub Releases with changelog.
3. The `version` field in `pyproject.toml` is the single source of truth.
4. Optionally use `setuptools-scm` to derive version from git tags.

Users install a specific version:
```bash
pip install git+https://github.com/bobvan/PePPAR-Fix@v0.1.0
```

Or track the latest:
```bash
pip install git+https://github.com/bobvan/PePPAR-Fix@main
```

### Phase 4: Non-Python assets

The ice driver patch and build script (`drivers/ice-gnss-streaming/`)
can't be installed via pip.  Options:

- **Package data**: include the patch and script in the pip package,
  install to a known location, provide a `peppar-fix-setup-driver` CLI.
- **Separate instructions**: document the driver fix as a manual
  prerequisite (current approach).  This is probably right — driver
  builds need root, kernel headers, and are host-specific.

Receiver config (`config/receivers.toml`) should be package data:
```toml
[tool.setuptools.package-data]
peppar_fix = ["*.toml", "config/*.toml"]
```

NTRIP credentials (`ntrip.conf`) are user-specific and never packaged.

## What stays outside the package

| Directory | Installed? | Reason |
|-----------|-----------|--------|
| `tools/` | No | Developer/lab diagnostics |
| `tests/` | No | Dev-only, run with `pip install -e .[dev]` |
| `drivers/` | No | Kernel module build, needs root |
| `docs/` | No | Design documents |
| `config/` | As package data | Receiver profiles |
| `timelab/` | No | Lab-specific inventory |
| `old/` | No | Deprecated, pending deletion |
| `data/` | No | Captured lab data |

## Naming considerations

The pip package name is `peppar-fix` (hyphenated, as is convention).
The Python import name is `peppar_fix` (underscored).  CLI commands
are `peppar-fix-engine`, `peppar-fix-bootstrap`, etc.

The current `peppar_fix/` package under `scripts/` will become the
top-level installed package.  No rename needed — just moving the
sibling modules inside it.

## When to do each phase

- **Phase 1** (editable install): Done. `pyproject.toml` is in the repo.
- **Phase 2** (flatten): When the engine API stabilizes.  The current
  rapid iteration on error sources, calibration, and servo logic means
  frequent changes to multiple files — waiting avoids merge conflicts
  from a wholesale rename.
- **Phase 3** (releases): Can start immediately.  Tag `v0.1.0` after
  the current PPS+PPP work is validated.
- **Phase 4** (non-Python assets): When Phase 2 is done and we have
  real users installing via pip.
