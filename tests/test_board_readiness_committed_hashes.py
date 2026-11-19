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
    # Issue #9058: board 01's recipe now slides the two VOUT vias out of
    # R1.2/R2.1's mask apertures and re-fills the pours after the
    # fiducials land (plus UUID canonicalization), so a fresh regen no
    # longer carries the 2 via pass-through errors and is byte-stable
    # across runs.  The re-pin itself still waits on the operator's
    # review of the five same-net own-pour zone findings (J1.2/J2.2
    # front+back, R2.2) -- declaration review per the operator ruling,
    # not a rule change.  Remove the entry when the re-pin lands.
    "01-voltage-divider": (
        "Issue #9058: board 01 evidence is stale against the "
        "recipe-repaired copper (vias slid out of the R1.2/R2.1 mask "
        "apertures, pours refilled post-fiducials); the re-pin waits on "
        "the operator's review of the 5 same-net own-pour "
        "mask_to_copper findings."
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
    "07-matchgroup-test": (
        "Issue #8399: the #8602 copper prune changed board 07's authored/reviewed "
        "sources; `refresh_readiness.py` refuses to re-pin while redundant_copper_* "
        "warnings remain (kct check allowance is 0). The last child of #8399 owns the re-pin."
    ),
    # PR #9502 (issue #9495) moved board 09's two producers under
    # run_on_evidence_toolchain without regenerating the committed output:
    # both files are recorded input hashes of the receipt (check_design.py
    # AND generate_design.py), so read_readiness() fails closed on two
    # stale inputs. The staleness is the known consequence of that scope
    # decision; the re-pin regenerates output the way #9469 did (pinned
    # Docker toolchain, scratch KICAD_CONFIG_HOME). Remove the entry when
    # that re-pin lands (strict xfail turns an unexpected pass into a
    # failure).
    "09-usbc-pd-power": (
        "Issue #9495: PR #9502 put board 09's producers (check_design.py, "
        "generate_design.py -- both recorded receipt inputs) under "
        "run_on_evidence_toolchain without regenerating output; the re-pin "
        "regenerates via the #9469 pinned-toolchain workflow. Remove when "
        "the re-pin lands."
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
