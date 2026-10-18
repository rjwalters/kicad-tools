"""Committed ``boards/*/output/readiness.json`` must match the committed tree (Issue #6945).

``read_readiness()`` fails closed with "Evidence is stale" when a recorded input
hash no longer matches its file.  Nothing ran it against the real boards, so
#6876 changed board 03's ``net_class_map.json`` and routed ``.kicad_dru``
without refreshing the readiness record and CI stayed green.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_tools.cli.board_readiness import read_readiness

BOARDS_DIR = Path(__file__).resolve().parent.parent / "boards"

# Boards whose committed readiness evidence is known to be stale, keyed by board
# directory name.  Each entry cites the issue that owns the refresh; remove the
# entry when that lands (strict xfail turns an unexpected pass into a failure).
EXPECTED_STALE = {
    "01-voltage-divider": "#6404: readiness inputs reference files absent from the tree",
}

BOARDS = sorted(p.parent.parent for p in BOARDS_DIR.glob("*/output/readiness.json"))


@pytest.mark.parametrize("board", BOARDS, ids=lambda p: p.name)
def test_committed_readiness_inputs_match_tree(board: Path, request) -> None:
    if board.name in EXPECTED_STALE:
        request.applymarker(pytest.mark.xfail(reason=EXPECTED_STALE[board.name], strict=True))
    result = read_readiness(board)
    stale = [b for b in result["blockers"] if "stale" in b or "missing" in b]
    assert result["status"] != "unverified", result["blockers"]
    assert not stale, stale
