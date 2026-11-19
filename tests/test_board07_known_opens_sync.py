"""Board 07 carries no known opens in any CI copy (#7552, #7557).

Until #7557 the ``board-07-end-to-end`` job's three gates
(``--expect-opens``, ``--known-open-nets``, ``--lvs-known-opens``) spelled out
one known-open set, ``{TMDS_D0_N}``.  #7557 moved U4's TMDS balls to its
J2-facing column and the seed-42 re-route reaches 31/31, so the job graduated
to a plain LVS-clean gate.  This test keeps it there: a known-open flag
reappearing in the job script must be a deliberate edit here.

Since issue #9340 the job's check commands live in exactly one place --
``scripts/ci/jobs/board-07-end-to-end.sh`` -- which the ci.yml job calls and
the local gate runs (2am#7561 ruling), so the flags are scanned there.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CI_YML = REPO_ROOT / ".github" / "workflows" / "ci.yml"
BOARD07_JOB_SCRIPT = REPO_ROOT / "scripts" / "ci" / "jobs" / "board-07-end-to-end.sh"

KNOWN_OPEN_FLAGS = ("--expect-opens", "--known-open-nets", "--lvs-known-opens")


def _board07_job_text() -> str:
    text = CI_YML.read_text()
    start = text.index("name: Board 07 End-to-End (LVS clean)")
    nxt = re.search(r"^  [A-Za-z0-9_-]+:\s*$", text[start:], re.MULTILINE)
    return text[start : start + nxt.start()] if nxt else text[start:]


def _commands(text: str) -> str:
    """Drop comment lines so prose that mentions a flag does not count."""
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def test_board07_ci_job_calls_the_shared_script() -> None:
    job = _commands(_board07_job_text())
    assert "bash scripts/ci/jobs/board-07-end-to-end.sh" in job, (
        "the board 07 end-to-end job must call the shared job script (issue #9340, 2am#7561 ruling)"
    )


def test_board07_job_script_has_no_known_opens() -> None:
    script = _commands(BOARD07_JOB_SCRIPT.read_text())
    present = [flag for flag in KNOWN_OPEN_FLAGS if flag in script]
    assert not present, f"board 07 end-to-end script re-grew known-open flags: {present}"
    assert "known_opens" not in script
    assert "--lvs-only" in script
