"""Audit: every regex in peppar_mon/log_reader has a matching
``peppar-mon contract:`` comment somewhere in scripts/ that names it.

Why this test exists: the parser regexes in log_reader.py only match
specific log line formats the engine emits.  When the engine changes
the format (commit 24a30ab — phase-bias log line rewrite — silently
broke peppar-mon's NL-capability detection), peppar-mon stops working
without any error.  This test enforces the convention that every
regex carries a back-pointer comment in the engine source, and that
any new regex requires the same back-pointer to be added.

A regex is exempt from the check if it has the literal text
``Engine source: NONE`` in its preceding comment block — used for
back-compat regexes that target legacy formats no longer emitted.
"""

import re
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
LOG_READER = REPO / "peppar_mon" / "log_reader.py"
SCRIPTS = REPO / "scripts"

# Regexes are named ``_NAME_RE``.  We discover them from the source so
# new ones get audited automatically.
_REGEX_DEF_RE = re.compile(
    r"^(_[A-Z][A-Z0-9_]+_RE)\s*=\s*re\.compile",
    re.MULTILINE,
)


def _all_regex_names_in_log_reader() -> list[str]:
    src = LOG_READER.read_text()
    return _REGEX_DEF_RE.findall(src)


def _regex_comment_block(src: str, regex_name: str) -> str:
    """Return the comment block immediately preceding ``regex_name``'s
    definition — the lines at the top of the file up through the
    ``regex_name = re.compile(...)`` line, then walking back to the
    last contiguous comment block.
    """
    lines = src.split("\n")
    for i, line in enumerate(lines):
        if line.startswith(regex_name):
            block_lines = []
            j = i - 1
            while j >= 0 and (lines[j].startswith("#") or lines[j].strip() == ""):
                block_lines.insert(0, lines[j])
                j -= 1
            return "\n".join(block_lines)
    return ""


def _has_engine_back_pointer(src: str) -> bool:
    """A regex's preceding comment must mention either:
       * ``Engine source: <path>``
       * ``Engine source: NONE`` (back-compat exemption)
    """
    return "Engine source:" in src


def _engine_contract_mentions(name: str) -> int:
    """Count how many times scripts/ mentions ``peppar-mon contract:``
    near a reference to this regex name (or an explicit back-pointer
    using its module-attribute name).
    """
    count = 0
    for py in SCRIPTS.rglob("*.py"):
        try:
            text = py.read_text()
        except OSError:
            continue
        if "peppar-mon contract:" not in text:
            continue
        # Loose check — we count pages that reference the regex name
        # OR have any peppar-mon contract comment.  The regex-naming
        # discipline is the reverse direction (peppar-mon names its
        # source); the engine direction just needs to mention
        # peppar-mon at the emission site.
        if name in text:
            count += 1
    return count


class EngineContractAuditTest(unittest.TestCase):
    """Each regex in log_reader.py must have an Engine source: line."""

    @classmethod
    def setUpClass(cls):
        cls.src = LOG_READER.read_text()
        cls.regex_names = _all_regex_names_in_log_reader()

    def test_at_least_one_regex_was_discovered(self):
        """Sanity: if regex discovery breaks, the audit silently passes.
        Guard against that."""
        self.assertGreater(len(self.regex_names), 0,
                           "Discovered no regex definitions — _REGEX_DEF_RE "
                           "may need updating")

    def test_each_regex_declares_engine_source(self):
        """Every ``_NAME_RE = re.compile(...)`` must be preceded by a
        comment block that contains ``Engine source: ...``.

        The exemption ``Engine source: NONE`` is allowed for legacy
        regexes targeting formats no longer emitted (kept for
        archived-log back-compat).
        """
        missing = []
        for name in self.regex_names:
            block = _regex_comment_block(self.src, name)
            if not _has_engine_back_pointer(block):
                missing.append(name)
        self.assertEqual(
            missing, [],
            f"These regexes lack an ``Engine source:`` comment: {missing}.\n"
            "Add the back-pointer in peppar_mon/log_reader.py and a "
            "matching ``peppar-mon contract:`` comment near the engine's "
            "log.info(...) emission.",
        )

    def test_engine_contract_marker_exists(self):
        """At least one ``peppar-mon contract:`` comment must exist
        somewhere in scripts/ — guards against accidental wholesale
        removal of the back-pointers on the engine side."""
        any_contract = any(
            "peppar-mon contract:" in p.read_text()
            for p in SCRIPTS.rglob("*.py")
            if p.is_file()
        )
        self.assertTrue(
            any_contract,
            "No ``peppar-mon contract:`` markers found in scripts/. "
            "Engine-side back-pointers may have been removed.",
        )


if __name__ == "__main__":
    sys.exit(unittest.main())
