"""Board 07 carries no known opens in any CI copy (#7552, #7557).

Until #7557 the ``board-07-end-to-end`` job's three gates in
``.github/workflows/ci.yml`` (``--expect-opens``, ``--known-open-nets``,
``--lvs-known-opens``) and ``known_opens`` in ``scripts/ci/local-gate.sh``
spelled out one known-open set, ``{TMDS_D0_N}``.  #7557 moved U4's TMDS balls
to its J2-facing column and the seed-42 re-route reaches 31/31, so both copies
graduated to plain LVS-clean gates.  This test keeps them there: a known-open
flag reappearing in either copy must be a deliberate edit here.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CI_YML = REPO_ROOT / ".github" / "workflows" / "ci.yml"
LOCAL_GATE = REPO_ROOT / "scripts" / "ci" / "local-gate.sh"

KNOWN_OPEN_FLAGS = ("--expect-opens", "--known-open-nets", "--lvs-known-opens")


def _board07_job_text() -> str:
    text = CI_YML.read_text()
    start = text.index("name: Board 07 End-to-End (LVS clean)")
    nxt = re.search(r"^  [A-Za-z0-9_-]+:\s*$", text[start:], re.MULTILINE)
    return text[start : start + nxt.start()] if nxt else text[start:]


def _local_gate_job_text() -> str:
    text = LOCAL_GATE.read_text()
    start = text.index("job_board_07_end_to_end()")
    end = text.index("\n}\n", start)
    return text[start:end]


def _commands(text: str) -> str:
    """Drop comment lines so prose that mentions a flag does not count."""
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def test_board07_ci_job_has_no_known_opens() -> None:
    job = _commands(_board07_job_text())
    present = [flag for flag in KNOWN_OPEN_FLAGS if flag in job]
    assert not present, f"board 07 end-to-end job re-grew known-open flags: {present}"
    assert "--lvs-only" in job


def test_board07_local_gate_has_no_known_opens() -> None:
    job = _commands(_local_gate_job_text())
    present = [flag for flag in KNOWN_OPEN_FLAGS if flag in job]
    assert not present, f"local-gate board 07 job re-grew known-open flags: {present}"
    assert "known_opens" not in job
    assert "--lvs-only" in job
