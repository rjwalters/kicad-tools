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
EXPECTED_STALE: dict[str, str] = {
    # Issue #8937 (PR #8955) re-emitted the routed ``.kicad_dru`` of the
    # boards whose recipes byte-reproduce it (01/05/06; the recipe-replay
    # tests pin the committed sidecar to the current emitter, so reverting
    # the DRU is not an option).  Board 06's evidence was re-pinned on the
    # pinned toolchain; board 01 followed at the review-fix stage: its saved
    # copper was re-filled under the corrected block (native refill,
    # divergent none), the copper Gerbers re-plotted, the bundle re-built
    # and ``readiness.json`` re-pinned via ``refresh_readiness.py`` -- its
    # two pre-existing same-net VOUT stitch-via slit findings are waived
    # evidence-bound in ``voltage_divider_routed.kct-waivers.json`` (the
    # #8938 mechanism; rerouting the pair as #8848 does retires the
    # waiver).  Board 05 could not be re-pinned: its ``kct check`` refuses
    # on the unaccepted physical_copper_gap/redundant-copper/silk warning
    # flood board 07's entry cites (the #8399/#8938-era stalemate,
    # reproduced with main's code on pristine main boards).  That work owns
    # the re-pin; remove the entry when it lands.
    "05-bldc-motor-controller": (
        "Issue #8937/#8399: PR #8955 re-emitted the routed .kicad_dru "
        "(zone-clearance rules); `refresh_readiness.py` refuses to re-pin while "
        "physical_copper_gap/redundant-copper/silk warnings remain unaccepted. "
        "The #8399 warning-flood work owns the re-pin."
    ),
    # Issue #8979: board 02 was regenerated through its recipe so the routed
    # PCB clears the strict Routed PCB DRC gate (13 same-net slits + 1
    # tht_smd_spacing -> 0).  Its release producer, package_release.py, run
    # on the pinned toolchain, refuses to re-publish: readiness's kct check
    # completes native mask attribution and grades 88 mask_to_copper errors
    # (127 on the PRE-change committed board under the same run, so not
    # introduced here -- the default-on mask_to_copper qualification is
    # #8858, operator-held), plus unacknowledged silk_hole_overlap warnings
    # (5 on the committed board, 9 after).  The old package stays committed,
    # bound to the old readiness record, until that re-pin can run.
    "02-charlieplex-led": (
        "Issue #8979/#8858: copper regenerated to clear the routed-DRC gate; "
        "package_release.py refuses to re-pin while readiness's native-complete "
        "kct check grades mask_to_copper errors (pre-existing, #8858) and "
        "silk_hole_overlap warnings remain unacknowledged."
    ),
    "07-matchgroup-test": (
        "Issue #8399: the #8602 copper prune changed board 07's authored/reviewed "
        "sources; `refresh_readiness.py` refuses to re-pin while redundant_copper_* "
        "warnings remain (kct check allowance is 0). The last child of #8399 owns the re-pin."
    ),
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
