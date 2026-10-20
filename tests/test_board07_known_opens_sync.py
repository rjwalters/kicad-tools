"""Board 07 known-open set must be identical in every CI copy (#7552).

The same net list is spelled out in the ``board-07-end-to-end`` job's three
gates in ``.github/workflows/ci.yml`` (``--expect-opens``,
``--known-open-nets``, ``--lvs-known-opens``) and in ``known_opens`` in
``scripts/ci/local-gate.sh``.  Nothing else ties them together, so a partial
edit would leave the local gate and CI disagreeing.  This test pins them to
one set.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CI_YML = REPO_ROOT / ".github" / "workflows" / "ci.yml"
LOCAL_GATE = REPO_ROOT / "scripts" / "ci" / "local-gate.sh"

CI_FLAGS = ("--expect-opens", "--known-open-nets", "--lvs-known-opens")


def _board07_job_text() -> str:
    text = CI_YML.read_text()
    start = text.index("name: Board 07 End-to-End (LVS known opens)")
    nxt = re.search(r"^  [A-Za-z0-9_-]+:\s*$", text[start:], re.MULTILINE)
    return text[start : start + nxt.start()] if nxt else text[start:]


def _as_set(csv: str) -> set[str]:
    return {n.strip() for n in csv.split(",") if n.strip()}


def _ci_sets() -> dict[str, set[str]]:
    job = _board07_job_text()
    found: dict[str, set[str]] = {}
    for flag in CI_FLAGS:
        m = re.search(rf'{re.escape(flag)}\s+"([^"]*)"', job)
        assert m, f"{flag} not found in the board 07 end-to-end job"
        found[flag] = _as_set(m.group(1))
    return found


def _local_gate_set() -> set[str]:
    text = LOCAL_GATE.read_text()
    start = text.index("job_board_07_end_to_end()")
    m = re.search(r'local known_opens="([^"]*)"', text[start:])
    assert m, "known_opens not found in job_board_07_end_to_end"
    return _as_set(m.group(1))


def test_board07_known_opens_identical_across_ci_and_local_gate() -> None:
    sets = {**_ci_sets(), "local-gate.sh known_opens": _local_gate_set()}
    distinct = {frozenset(v) for v in sets.values()}
    assert len(distinct) == 1, f"board 07 known-open sets drifted: {sets}"


def test_board07_known_opens_is_sole_tmdsd0n() -> None:
    # Tighten-only (#7552): widening this set must be a deliberate edit here.
    assert _local_gate_set() == {"TMDS_D0_N"}
