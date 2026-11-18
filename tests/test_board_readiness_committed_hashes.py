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
    # Issue #9058: PR #8848 kept its regenerated board 01 copper (VOUT
    # sharp_trace_corner wedge removed) over main's, so the evidence main
    # re-pinned for #8937's ``.kicad_dru`` no longer applies.  Stale input:
    # the routed ``.kicad_dru`` only; the refill under it agrees with the
    # saved copper.  Since #9064 (closing #9009) ``mask_to_copper`` runs to
    # completion under ``KCT_KICAD_DOCKER=1``, so the host is no longer the
    # obstacle: the producer's ``kct check`` gate refuses on 12 completed,
    # all same-net ``mask_to_copper`` errors (10 own-pad entry / same-net zone
    # exposures that main's own board 01 also has, plus 2 same-net VOUT via
    # pass-throughs at R1.2/R2.1).  Native DRC (saved and refilled), LVS,
    # zone_fill, artifacts and bom pass.  The re-pin waits on mask_to_copper
    # qualifying same-net exposures; #9058 owns it.
    "01-voltage-divider": (
        "Issue #9058: board 01 copper from PR #8848 cannot be re-pinned under "
        "#8937's .kicad_dru while kct check refuses on 12 same-net mask_to_copper "
        "errors (check completes since #9064/#9009); waits on same-net "
        "mask_to_copper qualification."
    ),
    # PR #9177 (issue #9143) landed the fiducials + board-ID silk on board 03
    # and refreshed its evidence, but deliberately did NOT re-issue the
    # readiness receipt: every fresh board-03 receipt is blocked (385
    # same-net mask_to_copper errors, #8900/#9144), and whether that demotes
    # board 03 from the ready set is the operator decision parked in #9144
    # (recommended: keep ready).  Until #9144 lands, main's 2026-10-21 ready
    # receipt stays committed and goes stale against the #9143 tree change.
    "03-usb-joystick": (
        "Issue #9144: board 03's tree moved in #9143 (fiducials, board-ID "
        "silk, refreshed evidence) while every fresh receipt is blocked; the "
        "pending ready/demote decision owns the re-pin. Remove when #9144 "
        "lands."
    ),
    # Issue #8937 (PR #8955) re-emitted the routed ``.kicad_dru`` of the
    # boards whose recipes byte-reproduce it (01/05/06; the recipe-replay
    # tests pin the committed sidecar to the current emitter, so reverting
    # the DRU is not an option).  Board 06's evidence was re-pinned on the
    # pinned toolchain; board 01's is covered by its #9058 entry above.
    # Board 05 could not be re-pinned: its ``kct check`` refuses
    # on the unaccepted physical_copper_gap/redundant-copper/silk warning
    # flood board 07's entry cites (the #8399/#8938-era stalemate,
    # reproduced with main's code on pristine main boards).  That work owns
    # the re-pin; remove the entry when it lands.
    #
    # Since #9057 the recipe itself is also ahead of the committed copper:
    # the three sharp_trace_corner wedges (RESET, HALL_B, HALL_C) are fixed
    # in ``redesign/routing.json`` and ``redesign/build.py`` now adds the
    # #8532 fiducials + board-ID silk, but the committed routed PCB and
    # ``readiness.json`` predate both (the strict gate still refuses on the
    # physical_copper_gap/silk_hole_overlap/mask_to_copper flood, so
    # ``build.py`` cannot publish).  Issue #9068 (the gate-pass split of
    # #9057) owns the rebuild + re-pin; remove the entry when it lands.
    "05-bldc-motor-controller": (
        "Issue #8937/#8399: PR #8955 re-emitted the routed .kicad_dru "
        "(zone-clearance rules); `refresh_readiness.py` refuses to re-pin while "
        "physical_copper_gap/redundant-copper/silk warnings remain unaccepted. "
        "The #8399 warning-flood work owns the re-pin. Since #9057 the recipe "
        "(wedge fix + #8532 fiducials/board-ID) is ahead of the committed "
        "copper too; the gate-pass rebuild owns the re-pin."
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
