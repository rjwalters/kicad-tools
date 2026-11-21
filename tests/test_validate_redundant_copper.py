"""Tests for the redundant-copper advisory rule (Issue #8218)."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from kicad_tools.schema.pcb import PCB
from kicad_tools.sexp import parse_string
from kicad_tools.validate.checker import DRCChecker
from kicad_tools.validate.rules.physical_gap import (
    FabGapPolicy,
    GapBand,
    check_physical_copper_gap,
    check_physical_copper_gap_fab_view,
)
from kicad_tools.validate.rules.redundant_copper import RedundantCopperRule


@dataclass
class _Seg:
    start: tuple[float, float]
    end: tuple[float, float]
    width: float = 0.2
    layer: str = "F.Cu"
    net_number: int = 1
    net_name: str = "SIG"


@dataclass
class _Via:
    position: tuple[float, float]
    layers: tuple[str, ...] = ("F.Cu", "B.Cu")
    net_number: int = 1
    net_name: str = "SIG"
    size: float = 0.6


@dataclass
class _PCB:
    segments: list = field(default_factory=list)
    vias: list = field(default_factory=list)
    board_origin: tuple[float, float] = (0.0, 0.0)


def _run(segments=(), vias=()):
    return RedundantCopperRule().check(_PCB(list(segments), list(vias)), None)


def _ids(results):
    return [v.rule_id for v in results.violations]


def test_clean_board_has_no_findings():
    res = _run([_Seg((0, 0), (5, 0)), _Seg((5, 0), (5, 5))])
    assert res.violations == []


def test_fifteen_duplicates_report_fifteen():
    segs = [_Seg((0, 0), (5, 0))] + [_Seg((5, 0), (0, 0)) for _ in range(15)]
    res = _run(segs)
    assert _ids(res).count("redundant_copper_duplicate") == 15
    assert all(v.severity == "warning" and v.location for v in res.violations)


def test_different_width_is_not_duplicate():
    res = _run([_Seg((0, 0), (5, 0)), _Seg((0, 0), (5, 0), width=0.3)])
    assert "redundant_copper_duplicate" not in _ids(res)


def test_fragment_run():
    segs = [_Seg((i * 0.08, 0), ((i + 1) * 0.08, 0)) for i in range(4)]
    res = _run(segs)
    assert _ids(res) == ["redundant_copper_fragments"]


def test_two_fragments_below_threshold():
    segs = [_Seg((i * 0.08, 0), ((i + 1) * 0.08, 0)) for i in range(2)]
    assert _run(segs).violations == []


def test_detour():
    segs = [_Seg((0, 0), (0, 5)), _Seg((0, 5), (1, 5)), _Seg((1, 5), (1, 0))]
    res = _run(segs)
    assert _ids(res) == ["redundant_copper_detour"]


def test_straight_chain_not_detour():
    segs = [_Seg((0, 0), (3, 0)), _Seg((3, 0), (6, 0))]
    assert _run(segs).violations == []


def test_via_offcentre_flagged():
    res = _run([_Seg((0, 0), (4.8, 0))], [_Via((5.0, 0))])
    assert _ids(res) == ["redundant_copper_via_offcentre"]


def test_via_centred_and_other_net_not_flagged():
    assert _run([_Seg((0, 0), (5, 0))], [_Via((5.0, 0))]).violations == []
    other = _Via((5.0, 0), net_number=2)
    assert _run([_Seg((0, 0), (4.8, 0))], [other]).violations == []


def test_via_centred_entry_with_bend_not_flagged():
    # Track enters the via centre with a 45-degree bend ~0.35 mm away; the
    # bend vertex is shared by two segments and must not be flagged.
    segs = [
        _Seg((0, 0), (4.75, 0)),
        _Seg((4.75, 0), (5.0, 0.25)),  # ends exactly on the via centre
    ]
    assert _run(segs, [_Via((5.0, 0.25))]).violations == []


def test_via_offcentre_unshared_end_flagged_when_via_not_centred():
    # Free end 0.2 mm short of the via, nothing else on that layer.
    res = _run([_Seg((0, 0), (4.8, 0))], [_Via((5.0, 0))])
    assert _ids(res) == ["redundant_copper_via_offcentre"]


def test_covered_segment_reported():
    res = _run([_Seg((0, 0), (10, 0)), _Seg((2, 0), (5, 0))])
    ids = _ids(res)
    assert ids.count("redundant_copper_covered") == 1
    v = next(v for v in res.violations if v.rule_id == "redundant_copper_covered")
    assert v.severity == "warning" and "kct pcb prune" in v.message
    assert "within the copper" in v.message
    assert v.location == (2, 0)


def test_exact_duplicate_not_reported_as_covered():
    res = _run([_Seg((0, 0), (5, 0)), _Seg((5, 0), (0, 0))])
    assert _ids(res) == ["redundant_copper_duplicate"]


def test_covered_negatives():
    base = _Seg((0, 0), (10, 0))
    for other in (
        _Seg((8, 0), (12, 0)),  # partial overlap
        _Seg((2, 0), (5, 0), layer="B.Cu"),  # different layer
        _Seg((2, 0), (5, 0), net_number=2, net_name="OTHER"),  # different net
        _Seg((2, 0), (5, 1)),  # non-collinear
    ):
        res = _run([base, other])
        assert "redundant_copper_covered" not in _ids(res)


def test_narrower_container_not_covering():
    res = _run([_Seg((0, 0), (10, 0), width=0.1), _Seg((2, 0), (5, 0), width=0.2)])
    assert "redundant_copper_covered" not in _ids(res)


# --- redundant_copper_staircase (Issue #8831) -------------------------------

_STAIR_PTS = [
    (0, 0),
    (4, 0),
    (6, 2),
    (10, 2),
    (12, 4),
    (16, 4),
    (18, 6),
    (22, 6),
    (22, 8),
    (24, 10),
]


def _staircase(width=2.6):
    return [
        _Seg(a, b, width=width, net_name="PWR")
        for a, b in zip(_STAIR_PTS, _STAIR_PTS[1:], strict=False)
    ]


def _stair_findings(res):
    return [v for v in res.violations if v.rule_id == "redundant_copper_staircase"]


def test_staircase_with_free_straight_path_is_flagged_once():
    found = _stair_findings(_run(_staircase()))
    assert len(found) == 1
    assert found[0].severity == "warning"
    assert found[0].location == (0, 0)
    assert "(24.00, 10.00)" in found[0].message and "saves" in found[0].message


def test_staircase_not_flagged_when_alternative_hits_foreign_copper():
    # Foreign track sits on the diagonal/horizontal replacement, clear of the
    # original staircase's own legs.
    blocker = _Seg((12, 9), (20, 9), width=0.2, net_number=2, net_name="OTHER")
    # replacement diag (0,0)->(10,10) then H to (24,10): blocker at y=9 is hit
    # by the H leg's 2.6 mm width; the orthogonal-first path hits it too.
    blocker2 = _Seg((5, 0.5), (5, 3), width=0.2, net_number=2, net_name="OTHER")
    res = _run([*_staircase(), blocker, blocker2])
    assert _stair_findings(res) == []


def test_serpentine_is_not_a_staircase():
    pts = [(0, 0), (0, 5), (2, 5), (2, 1), (4, 1), (4, 6)]
    segs = [_Seg(a, b) for a, b in zip(pts, pts[1:], strict=False)]
    assert _stair_findings(_run(segs)) == []


def test_already_optimal_path_is_not_a_staircase():
    res = _run([_Seg((0, 0), (3, 3)), _Seg((3, 3), (9, 3)), _Seg((9, 3), (12, 3))])
    assert _stair_findings(res) == []


# --- staircase shortcut vs the physical-gap slit floor (Issue #9862) ---------


def _gap_board(*items):
    return PCB(
        parse_string(
            """(kicad_pcb (version 20240108) (generator pcbnew)
      (general (thickness 1.6))
      (layers (0 "F.Cu" signal) (31 "B.Cu" signal))
      (net 0 "") (net 1 "SIG") (net 2 "OTHER") (net 3 "THIRD")
    """
            + "\n".join(items)
            + ")"
        )
    )


def _track(a, b, *, net=1, width=0.2, layer="F.Cu"):
    return (
        f"(segment (start {a[0]} {a[1]}) (end {b[0]} {b[1]}) "
        f'(width {width}) (layer "{layer}") (net {net}))'
    )


#: A 3-segment, 2-bend staircase whose diagonal-first shortcut runs
#: (0,0)->(4,4)->(8,4) and whose orthogonal-first alternative runs
#: (0,0)->(8,0)->(8,4).
_L_CHAIN = (((0, 0), (4, 0)), ((4, 0), (4, 4)), ((4, 4), (8, 4)))


def _chain_tracks(chain=_L_CHAIN):
    return [_track(a, b) for a, b in chain]


def _stair_pcb(*extra):
    return _gap_board(*_chain_tracks(), *extra)


def _staircase_findings(checker):
    results = checker.check_redundant_copper()
    return [v for v in results.violations if v.rule_id == "redundant_copper_staircase"]


def _checker(pcb, *, gap_mm=None, fab_policy=None):
    return DRCChecker(
        pcb,
        manufacturer="jlcpcb",
        layers=2,
        physical_copper_gap_mm=gap_mm,
        fab_view=fab_policy is not None,
        fab_view_gap_policy=fab_policy,
        warn_on_inactive_skew_rules=False,
    )


def _pad(at, name):
    return f"""(footprint "t{name}" (layer "F.Cu") (at {at[0]} {at[1]})
      (property "Reference" "{name}")
      (pad "1" smd circle (at 0 0) (size 1 1) (layers "F.Cu")
      (net 1 "SIG") (uuid "pad{name}")))"""


def _zone_strip(layer="F.Cu"):
    """Zone copper whose edge parallels the (0,0)->(4,4) diagonal at 0.0828 mm."""
    return f"""(zone (net 1) (net_name "SIG") (layer "{layer}") (uuid "z1")
      (polygon (pts (xy 0.13 -0.13) (xy 4.13 3.87) (xy 4.63 3.37) (xy 0.63 -0.63)))
      (filled_polygon (layer "{layer}")
        (pts (xy 0.13 -0.13) (xy 4.13 3.87) (xy 4.63 3.37) (xy 0.63 -0.63))))"""


def _branch(offset):
    """Same-net diagonal parallel to the shortcut at the given centreline gap.

    Ends at 3.6 along the run so its tip stays clear of the replacement's
    second leg (only the parallel side edge faces the shortcut).
    """
    return _track((offset, -offset), (3.6 + offset, 3.6 - offset))


DIAGONAL_FIRST = " -> (4.00, 4.00) -> "
ORTHOGONAL_FIRST = " -> (4.00, 0.00) -> "


def test_staircase_reports_diagonal_first_without_gap_policy():
    found = _staircase_findings(_checker(_stair_pcb()))
    assert len(found) == 1
    assert DIAGONAL_FIRST in found[0].message


def test_shortcut_rejected_when_it_opens_same_net_slit():
    # Branch parallel to the diagonal-first leg at a 0.0828 mm edge gap
    # (0.2828 mm centreline for two 0.2 mm tracks): under an explicit
    # 0.1016 mm threshold the shortcut is rejected and the orthogonal-first
    # alternative (the only other bend) is recommended instead.
    found = _staircase_findings(_checker(_stair_pcb(_branch(0.2)), gap_mm=0.1016))
    assert len(found) == 1
    assert DIAGONAL_FIRST not in found[0].message
    assert ORTHOGONAL_FIRST in found[0].message


def test_rejected_shortcut_really_opens_physical_gap():
    # Paired oracle: applying exactly the rejected first bend to a copy
    # makes the physical-gap rule report the branch slit; the untouched
    # board reports no physical_copper_gap finding at all.
    applied = _gap_board(
        _track((0, 0), (4, 4)),
        _track((4, 4), (8, 4)),
        _branch(0.2),
    )
    gaps = [
        v
        for v in check_physical_copper_gap(applied, 0.1016).violations
        if v.rule_id == "physical_copper_gap"
    ]
    assert gaps and gaps[0].severity == "error"
    assert gaps[0].actual_value == pytest.approx(0.0828, abs=1e-3)
    untouched = [
        v
        for v in check_physical_copper_gap(_stair_pcb(_branch(0.2)), 0.1016).violations
        if v.rule_id == "physical_copper_gap"
    ]
    assert untouched == []


def test_branch_outside_band_lets_diagonal_return():
    # Centreline 0.5657 mm -> 0.3657 mm edge gap, above the 0.1016 mm floor.
    found = _staircase_findings(_checker(_stair_pcb(_branch(0.4)), gap_mm=0.1016))
    assert len(found) == 1
    assert DIAGONAL_FIRST in found[0].message


def test_all_bends_rejected_no_report():
    # The diagonal-first bend opens the same-net slit and a foreign track
    # blocks the orthogonal-first leg electrically: no candidate remains.
    blocker = _track((2, 0.15), (6, 0.15), net=2)
    found = _staircase_findings(_checker(_stair_pcb(_branch(0.2), blocker), gap_mm=0.1016))
    assert found == []


def test_threshold_boundary_matches_physical_gap_oracle():
    # Just inside / outside the rule's own reporting window
    # (TOLERANCE < gap < minimum - TOLERANCE): the staircase gate must
    # agree with check_physical_copper_gap on the applied shortcut.
    for gap, keeps_diagonal in ((0.1005, False), (0.1007, True)):
        offset = (0.2 + gap) / math.sqrt(2)
        applied = _gap_board(_track((0, 0), (4, 4)), _track((4, 4), (8, 4)), _branch(offset))
        oracle = [
            v
            for v in check_physical_copper_gap(applied, 0.1016).violations
            if v.rule_id == "physical_copper_gap"
        ]
        found = _staircase_findings(_checker(_stair_pcb(_branch(offset)), gap_mm=0.1016))
        assert len(found) == 1
        assert (DIAGONAL_FIRST in found[0].message) == keeps_diagonal
        assert bool(oracle) != keeps_diagonal


def test_terminal_pad_contact_keeps_staircase_reportable():
    pcb = _stair_pcb(_pad((0, 0), "U1"), _pad((8, 4), "U2"))
    found = _staircase_findings(_checker(pcb, gap_mm=0.1016))
    assert len(found) == 1
    assert DIAGONAL_FIRST in found[0].message


def test_unrelated_preexisting_gap_does_not_block():
    noise = (
        _track((20, 0), (24, 0.25), net=2),
        _track((20, 0.5), (24, 0.5), net=3),
    )
    pcb = _stair_pcb(*noise)
    assert check_physical_copper_gap(pcb, 0.1016).violations  # a gap exists today
    found = _staircase_findings(_checker(pcb, gap_mm=0.1016))
    assert len(found) == 1
    assert DIAGONAL_FIRST in found[0].message


def test_zone_slit_rejects_under_explicit_threshold():
    found = _staircase_findings(_checker(_stair_pcb(_zone_strip()), gap_mm=0.1016))
    assert len(found) == 1
    assert DIAGONAL_FIRST not in found[0].message
    assert ORTHOGONAL_FIRST in found[0].message


def test_zone_on_another_layer_does_not_reject():
    found = _staircase_findings(_checker(_stair_pcb(_zone_strip("B.Cu")), gap_mm=0.1016))
    assert len(found) == 1
    assert DIAGONAL_FIRST in found[0].message


def test_fab_view_zone_gap_is_info_and_keeps_candidate_eligible():
    policy = FabGapPolicy(trace=GapBand(0.1016, None), pad=GapBand(0.15, None))
    # Oracle side: once the diagonal shortcut is applied, the zone-fill gap
    # it opens is the fab pass's info tier (zone pours are not judged).
    applied = _gap_board(*_chain_tracks((((0, 0), (4, 4)), ((4, 4), (8, 4)))), _zone_strip())
    info = [
        v
        for v in check_physical_copper_gap_fab_view(applied, policy).violations
        if v.rule_id == "physical_copper_gap"
    ]
    assert info and info[0].severity == "info"
    # Gate side: an info-only zone gap therefore leaves the candidate eligible.
    found = _staircase_findings(_checker(_stair_pcb(_zone_strip()), fab_policy=policy))
    assert len(found) == 1
    assert DIAGONAL_FIRST in found[0].message


def test_fab_view_trace_band_rejects_sub_band_slit():
    policy = FabGapPolicy(trace=GapBand(0.1016, None), pad=GapBand(0.15, None))
    found = _staircase_findings(_checker(_stair_pcb(_branch(0.2)), fab_policy=policy))
    assert len(found) == 1
    assert DIAGONAL_FIRST not in found[0].message
    assert ORTHOGONAL_FIRST in found[0].message


def test_staircase_message_uses_sheet_absolute_coordinates_with_origin():
    """Issue #8868: message coordinates share the frame of ``location``."""
    from kicad_tools.validate.checker import DRCChecker

    origin = (68.5, 55.0)
    pcb = _PCB(_staircase(), [], origin)
    # Same path DRCChecker.check_redundant_copper takes.
    checker = DRCChecker.__new__(DRCChecker)
    checker.pcb = pcb
    res = checker._absolutize(RedundantCopperRule().check(pcb, None))
    (v,) = _stair_findings(res)
    assert v.location == (68.5, 55.0)
    assert f"from ({v.location[0]:.2f}, {v.location[1]:.2f})" in v.message
    assert "(92.50, 65.00)" in v.message
    assert "(24.00, 10.00)" not in v.message


def test_staircase_message_unchanged_at_zero_origin():
    (v,) = _stair_findings(_run(_staircase()))
    assert "from (0.00, 0.00) to (24.00, 10.00)" in v.message


# --- staircase legality (Issue #9817) ---------------------------------------
#
# Each case reproduces one row of the issue's board-02 evidence table as a
# small synthetic board, with a non-vacuous control showing the same chain is
# reported when the defect's cause is absent.

_BOARD_HEAD = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (paper "A4")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (37 "F.SilkS" user)
    (38 "B.Mask" user) (39 "F.Mask" user) (44 "Edge.Cuts" user))
  (setup (pad_to_mask_clearance 0.05))
  (net 0 "")
  (net 1 "SIG")
  (net 2 "OTHER")
"""


def _pad_fp(ref, x, y, net=1, size=1.0, mask=True):
    layers = '"F.Cu" "F.Mask"' if mask else '"F.Cu"'
    net_name = {1: "SIG", 2: "OTHER"}[net]
    return (
        f'  (footprint "t:pad" (layer "F.Cu") (at {x} {y})\n'
        f'    (property "Reference" "{ref}" (at 0 0) (layer "F.SilkS"))\n'
        f'    (pad "1" smd rect (at 0 0) (size {size} {size}) (layers {layers}) '
        f'(net {net} "{net_name}"))\n'
        f"  )\n"
    )


def _seg_text(points, width=0.25, net=1, start_uuid=0):
    return "".join(
        f"  (segment (start {a[0]} {a[1]}) (end {b[0]} {b[1]}) (width {width}) "
        f'(layer "F.Cu") (net {net}) (uuid "s{start_uuid + i}"))\n'
        for i, (a, b) in enumerate(zip(points, points[1:], strict=False))
    )


def _board(tmp_path: Path, body: str):
    from kicad_tools.schema.pcb import PCB

    path = tmp_path / "board.kicad_pcb"
    path.write_text(_BOARD_HEAD + body + ")\n")
    return PCB.load(path)


def _checker_stairs(pcb):
    """Run through ``DRCChecker`` as ``kct check --mfr jlcpcb`` does."""
    from kicad_tools.validate.checker import DRCChecker

    return _stair_findings(
        DRCChecker(pcb, manufacturer="jlcpcb", layers=2).check_redundant_copper()
    )


def _power_project(pcb_path: Path, pattern: str) -> None:
    from tests.router.test_authored_netclass_board import _netclass

    project = {
        "meta": {"filename": pcb_path.with_suffix(".kicad_pro").name, "version": 3},
        "net_settings": {
            "meta": {"version": 4},
            "classes": [_netclass("Default", 0.127, 2147483647), _netclass("Power", 0.2, 0)],
            "netclass_assignments": None,
            "netclass_patterns": [{"netclass": "Power", "pattern": pattern}],
        },
    }
    pcb_path.with_suffix(".kicad_pro").write_text(json.dumps(project))


# A 4-segment / 3-bend chain; its fewer-bend candidates are
# (0,0)->(4,4)->(8,4) and (0,0)->(4,0)->(8,4).
_NC_CHAIN = [(0, 0), (2, 0), (4, 2), (6, 2), (8, 4)]
# Foreign tracks 0.16 mm (copper to copper) from each candidate's new leg:
# clear of JLCPCB's 0.127 mm floor, short of a 0.2 mm Power class, and far
# (> 0.4 mm) from the chain itself.
_OFF = 0.125 + 0.16 + 0.1
_NC_BLOCKERS = [
    ((2.5, -_OFF), (3.5, -_OFF)),
    (
        (1 - _OFF / 2**0.5, 1 + _OFF / 2**0.5),
        (2 - _OFF / 2**0.5, 2 + _OFF / 2**0.5),
    ),
]


def _netclass_board(tmp_path):
    body = _seg_text(_NC_CHAIN)
    for i, blocker in enumerate(_NC_BLOCKERS):
        body += _seg_text(list(blocker), width=0.2, net=2, start_uuid=100 + 10 * i)
    return _board(tmp_path, body)


def test_netclass_case_control_fab_floor_alone_reports(tmp_path):
    """Control: without a Power class the 0.16 mm gaps are legal and reported."""
    assert len(_checker_stairs(_netclass_board(tmp_path))) == 1


@pytest.mark.parametrize("power_net", ["SIG", "OTHER"])
def test_shortcut_inside_netclass_clearance_not_reported(tmp_path, power_net):
    """Board 02 [VCC]: the shortcut clears the fab floor but not the class.

    The requirement is ``max(class[a], class[b])``, so the obstacle's class
    counts as much as the chain's own (``netclass_clearance_copper``).
    """
    pcb = _netclass_board(tmp_path)
    _power_project(pcb.path, power_net)
    from kicad_tools.schema.pcb import PCB

    assert _checker_stairs(PCB.load(pcb.path)) == []


# Board 02 [NODE_D]: a 4-bend chain between two pads, entering both
# horizontally at the pad centres.  Both 1-bend candidates put their bend
# inside a pad (0.4 mm from its centre), so the shortcut leg straddles the
# pad's edge: copper under mask beside the opening.
_MASK_CHAIN = [(0, 0), (3, 0), (3.2, 0.2), (6, 0.2), (6.2, 0.4), (10, 0.4)]


def _mask_board(tmp_path, mask=True):
    body = _pad_fp("D9", 0, 0, mask=mask) + _pad_fp("D10", 10, 0.4, mask=mask)
    return _board(tmp_path, body + _seg_text(_MASK_CHAIN))


def test_mask_case_control_pads_without_mask_opening_report(tmp_path):
    """Control: the same chain between mask-less pads is reported."""
    assert len(_checker_stairs(_mask_board(tmp_path, mask=False))) == 1


def test_shortcut_with_new_copper_in_mask_zone_not_reported(tmp_path):
    assert _checker_stairs(_mask_board(tmp_path)) == []


def test_mask_zone_uses_policy_clearance(tmp_path):
    """Rule-level: the zone is the opening grown by ``mask_clearance_mm``."""
    pcb = _mask_board(tmp_path)
    assert _stair_findings(RedundantCopperRule(mask_clearance_mm=0.15).check(pcb, None)) == []


# Board 02 [NODE_A]: the chain runs through the centre of a same-net pad.
# Each half has one bend; only the walk through the pad made it a "3-bend
# staircase" whose shortcut would bypass (and disconnect) the pad.
_THROUGH_CHAIN = [(0, 0), (2, 0), (4, 2), (6, 2), (8, 4)]


def test_pass_through_case_control_without_pad_reports(tmp_path):
    assert len(_checker_stairs(_board(tmp_path, _seg_text(_THROUGH_CHAIN)))) == 1


def test_chain_through_same_net_pad_centre_not_reported(tmp_path):
    pcb = _board(tmp_path, _pad_fp("D1", 4, 2) + _seg_text(_THROUGH_CHAIN))
    assert _checker_stairs(pcb) == []


def test_chain_through_same_net_via_centre_not_reported():
    segs = [_Seg(a, b) for a, b in zip(_THROUGH_CHAIN, _THROUGH_CHAIN[1:], strict=False)]
    assert len(_stair_findings(_run(segs))) == 1  # control
    assert _stair_findings(_run(segs, [_Via((4, 2))])) == []


def test_foreign_pad_on_chain_vertex_does_not_split_chain(tmp_path):
    """Only a same-net pad is a terminal (a foreign one is clearance's job)."""
    pcb = _board(tmp_path, _pad_fp("Q1", 4, 2, net=2) + _seg_text(_THROUGH_CHAIN))
    assert len(_stair_findings(RedundantCopperRule().check(pcb, None))) == 1


def test_genuine_staircase_between_pads_still_reported(tmp_path):
    """A free, class-clear, mask-clean shortcut is still flagged.

    The chain leaves pad A horizontally and enters pad B on a diagonal; the
    shortcut (0,0)->(6,0)->(10,4) is collinear with both end legs, so it adds
    no copper near either opening.  The diagonal-first candidate would leave
    A at 45 degrees through its mask zone and is skipped.
    """
    chain = [(0, 0), (2, 0), (4, 2), (8, 2), (10, 4)]
    pcb = _board(tmp_path, _pad_fp("A1", 0, 0) + _pad_fp("B1", 10, 4) + _seg_text(chain))
    _power_project(pcb.path, "SIG")
    from kicad_tools.schema.pcb import PCB

    (finding,) = _checker_stairs(PCB.load(pcb.path))
    assert "(0.00, 0.00) -> (6.00, 0.00) -> (10.00, 4.00)" in finding.message
