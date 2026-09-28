"""Tests for the heuristic ``width_consistency`` rule.

Synthetic boards (vias as route terminals, one small-pad footprint for the
pad-escape case) exercise each classification of
:class:`~kicad_tools.validate.rules.width_consistency.WidthConsistencyRule`:
width islands, unjustified transitions (with obstacle distance), transitions
justified by other-net clearance or by a pad escape, and the cases the rule
deliberately leaves alone (branches, long wide sections as islands, other
layers).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kicad_tools.manufacturers import DesignRules, get_profile
from kicad_tools.schema.pcb import PCB
from kicad_tools.validate.rules.width_consistency import WidthConsistencyRule

_PCB_TEMPLATE = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "A")
  (net 2 "B")
  (net 3 "C")
  (gr_rect (start 90 90) (end 140 140)
    (stroke (width 0.1) (type default))
    (fill none)
    (layer "Edge.Cuts")
  )
{items})
"""

CLEARANCE = 0.15
_uuid_counter = [0]


def _uuid() -> str:
    _uuid_counter[0] += 1
    return f"00000000-0000-0000-0000-{_uuid_counter[0]:012d}"


def seg(x1, y1, x2, y2, w, net, layer="F.Cu") -> str:
    return (
        f'  (segment (start {x1} {y1}) (end {x2} {y2}) (width {w}) (layer "{layer}") '
        f'(net {net}) (uuid "{_uuid()}"))\n'
    )


def arc(start, mid, end, w, net, layer="F.Cu") -> str:
    return (
        f"  (arc (start {start[0]} {start[1]}) (mid {mid[0]} {mid[1]}) "
        f'(end {end[0]} {end[1]}) (width {w}) (layer "{layer}") (net {net}) '
        f'(uuid "{_uuid()}"))\n'
    )


def via(x, y, net, size=0.6) -> str:
    return (
        f'  (via (at {x} {y}) (size {size}) (drill 0.3) (layers "F.Cu" "B.Cu") '
        f'(net {net}) (uuid "{_uuid()}"))\n'
    )


def pad_fp(x, y, net, net_name, pad_w=0.3) -> str:
    return f"""  (footprint "Test:SmallPad"
    (layer "F.Cu")
    (uuid "{_uuid()}")
    (at {x} {y})
    (property "Reference" "U1" (at 0 -1.5 0) (layer "F.SilkS"))
    (property "Value" "X" (at 0 1.5 0) (layer "F.Fab"))
    (pad "1" smd rect (at 0 0) (size {pad_w} 1.0) (layers "F.Cu") (net {net} "{net_name}"))
  )
"""


def _load(tmp_path: Path, items: str) -> PCB:
    path = tmp_path / "board.kicad_pcb"
    path.write_text(_PCB_TEMPLATE.format(items=items))
    return PCB.load(str(path))


def _check(pcb: PCB, **kw):
    rule = WidthConsistencyRule(clearance_mm=CLEARANCE, **kw)
    return rule.check(pcb, _rules())


def _rules() -> DesignRules:
    return get_profile("jlcpcb").get_design_rules(layers=2)


def _by_id(results, rule_id):
    return [v for v in results.violations if v.rule_id == rule_id]


# ---------------------------------------------------------------------------
# Islands
# ---------------------------------------------------------------------------


def test_short_wide_island_is_reported_once(tmp_path):
    # via -0.2- | 0.5 x 1 mm | -0.2- via
    items = (
        via(100, 100, 1)
        + seg(100, 100, 102, 100, 0.2, 1)
        + seg(102, 100, 103, 100, 0.5, 1)
        + seg(103, 100, 106, 100, 0.2, 1)
        + via(106, 100, 1)
    )
    results = _check(_load(tmp_path, items))
    islands = _by_id(results, "width_island")
    assert len(islands) == 1
    island = islands[0]
    assert island.severity == "warning"
    assert island.actual_value == pytest.approx(1.0)
    assert island.nets == ("A",)
    assert island.layer == "F.Cu"
    assert len(island.items) == 1
    # Island boundaries are folded into the island finding.
    assert _by_id(results, "width_transition") == []


def test_long_wide_section_is_not_an_island(tmp_path):
    items = (
        via(100, 100, 1)
        + seg(100, 100, 102, 100, 0.2, 1)
        + seg(102, 100, 108, 100, 0.5, 1)
        + seg(108, 100, 110, 100, 0.2, 1)
        + via(110, 100, 1)
    )
    results = _check(_load(tmp_path, items))
    assert _by_id(results, "width_island") == []
    # ...but both unobstructed neck-downs are reported as transitions.
    assert len(_by_id(results, "width_transition")) == 2
    # And the island length threshold is configurable.
    assert len(_by_id(_check(_load(tmp_path, items), max_island_length_mm=10), "width_island"))


def test_island_through_arc(tmp_path):
    items = (
        via(100, 100, 1)
        + seg(100, 100, 102, 100, 0.2, 1)
        + arc((102, 100), (102.5, 99.5), (103, 100), 0.5, 1)
        + seg(103, 100, 106, 100, 0.2, 1)
        + via(106, 100, 1)
    )
    islands = _by_id(_check(_load(tmp_path, items)), "width_island")
    assert len(islands) == 1
    assert islands[0].actual_value == pytest.approx(0.5 * 3.141592653589793, abs=1e-4)


def test_island_across_rounding_boundary_is_joined(tmp_path):
    """Endpoints within tolerance but in different rounded buckets still join.

    With ``node_tolerance_mm = 0.0005`` the rounding boundary lies at
    ``x = 102.00025`` (``x / q = 204000.5``); the shared endpoint of the first
    two tracks is written as 102.0002 on one side and 102.0003 on the other
    (0.0001 mm apart, well inside the tolerance) so the plain rounded keys
    differ (204000 vs 204001).  The chain must still be walked through that
    point and the 0.5 mm island reported exactly once (issue #5750).
    """
    rule = WidthConsistencyRule()
    q = rule.node_tolerance_mm
    assert rule._node((102.0002, 100.0)) != rule._node((102.0003, 100.0))
    assert abs(102.0003 - 102.0002) <= q

    items = (
        via(100, 100, 1)
        + seg(100, 100, 102.0002, 100, 0.2, 1)
        + seg(102.0003, 100, 103, 100, 0.5, 1)
        + seg(103, 100, 106, 100, 0.2, 1)
        + via(106, 100, 1)
    )
    results = _check(_load(tmp_path, items))
    islands = _by_id(results, "width_island")
    assert len(islands) == 1
    assert islands[0].actual_value == pytest.approx(1.0, abs=1e-3)
    assert _by_id(results, "width_transition") == []


def test_transition_across_rounding_boundary_is_joined(tmp_path):
    """A two-terminal neck split by a straddled bucket is still one chain."""
    items = (
        via(100, 110, 3)
        + seg(100, 110, 103.0002, 110, 0.5, 3)
        + seg(103.0003, 110, 106, 110, 0.2, 3)
        + via(106, 110, 3)
    )
    (finding,) = _by_id(_check(_load(tmp_path, items)), "width_transition")
    assert "no other-net copper within" in finding.message


def test_narrow_neck_is_not_an_island(tmp_path):
    items = (
        via(100, 100, 1)
        + seg(100, 100, 102, 100, 0.5, 1)
        + seg(102, 100, 103, 100, 0.2, 1)
        + seg(103, 100, 106, 100, 0.5, 1)
        + via(106, 100, 1)
    )
    results = _check(_load(tmp_path, items))
    assert _by_id(results, "width_island") == []
    # One finding for the neck, not one per side.
    (neck,) = _by_id(results, "width_transition")
    assert "0.200 mm run of 1.000 mm meets 0.500 mm" in neck.message


# ---------------------------------------------------------------------------
# Transitions
# ---------------------------------------------------------------------------


def _neck_route(obstacle_y: float | None) -> str:
    # via -0.5 wide (3 mm)- -0.2 wide (3 mm)- via; optional net-B track above
    # the narrow run at y = 110 - obstacle_y offset.
    items = (
        via(100, 110, 3)
        + seg(100, 110, 103, 110, 0.5, 3)
        + seg(103, 110, 106, 110, 0.2, 3)
        + via(106, 110, 3)
    )
    if obstacle_y is not None:
        items += seg(103.5, 110 - obstacle_y, 105.5, 110 - obstacle_y, 0.2, 2)
    return items


def test_unjustified_transition_reports_obstacle_distance(tmp_path):
    # Net-B track centre 0.8 mm away: 0.6 mm gap now, 0.45 mm if widened.
    results = _check(_load(tmp_path, _neck_route(0.8)))
    (finding,) = _by_id(results, "width_transition")
    assert finding.severity == "warning"
    assert finding.nets == ("C",)
    assert finding.actual_value == pytest.approx(0.6, abs=1e-3)
    assert finding.required_value == CLEARANCE
    assert "0.450 mm if widened" in finding.message
    assert "track " in finding.message
    # Rule-level locations are board-relative (origin at the 90,90 outline).
    assert finding.location == pytest.approx((13.0, 20.0))


def test_unjustified_transition_without_neighbours(tmp_path):
    (finding,) = _by_id(_check(_load(tmp_path, _neck_route(None))), "width_transition")
    assert "no other-net copper within" in finding.message
    assert finding.actual_value is None


def test_transition_justified_by_clearance(tmp_path):
    # Net-B centre 0.45 mm away: 0.25 mm gap now, but only 0.10 mm (< 0.15)
    # if the neck were widened to 0.5 mm.
    pcb = _load(tmp_path, _neck_route(0.45))
    assert _by_id(_check(pcb), "width_transition") == []
    (info,) = _by_id(_check(pcb, report_justified=True), "width_transition")
    assert info.severity == "info"
    assert "justified" in info.message


def test_transition_justified_by_pad_escape(tmp_path):
    # 0.2 mm trace leaves a 0.3 mm wide pad, then widens to 0.5 mm.
    items = (
        pad_fp(100, 120, 3, "C")
        + seg(100, 120, 102, 120, 0.2, 3)
        + seg(102, 120, 106, 120, 0.5, 3)
        + via(106, 120, 3)
    )
    pcb = _load(tmp_path, items)
    assert _by_id(_check(pcb), "width_transition") == []
    (info,) = _by_id(_check(pcb, report_justified=True), "width_transition")
    assert "pad escape" in info.message


def test_branches_are_not_audited_as_two_terminal_chains(tmp_path):
    # T junction at (103, 110): three chains, none two-terminal via-to-via.
    items = (
        via(100, 110, 3)
        + seg(100, 110, 103, 110, 0.5, 3)
        + seg(103, 110, 106, 110, 0.2, 3)
        + seg(103, 110, 103, 114, 0.2, 3)
        + via(106, 110, 3)
        + via(103, 114, 3)
    )
    assert _by_id(_check(_load(tmp_path, items)), "width_transition") == []


def test_same_net_on_other_layer_is_independent(tmp_path):
    items = (
        via(100, 100, 1)
        + seg(100, 100, 106, 100, 0.2, 1)
        + seg(100, 100, 106, 100, 0.8, 1, layer="B.Cu")
        + via(106, 100, 1)
    )
    results = _check(_load(tmp_path, items))
    assert results.violations == []


def test_uniform_route_is_clean_and_toggles_work(tmp_path):
    pcb = _load(tmp_path, _neck_route(None))
    assert _check(pcb, report_transitions=False).violations == []
    clean = via(100, 100, 1) + seg(100, 100, 106, 100, 0.2, 1) + via(106, 100, 1)
    assert _check(_load(tmp_path, clean)).violations == []


def test_severity_is_configurable_and_validated(tmp_path):
    results = _check(_load(tmp_path, _neck_route(None)), severity="info")
    assert {v.severity for v in results.violations} == {"info"}
    with pytest.raises(ValueError):
        WidthConsistencyRule(severity="fatal")


# ---------------------------------------------------------------------------
# CLI / checker wiring
# ---------------------------------------------------------------------------


def test_kct_check_only_width_consistency_json(tmp_path, capsys):
    from kicad_tools.cli.check_cmd import main

    path = tmp_path / "board.kicad_pcb"
    path.write_text(_PCB_TEMPLATE.format(items=_neck_route(None)))
    rc = main([str(path), "--only", "width_consistency", "--format", "json", "--drc-only"])
    data = json.loads(capsys.readouterr().out)
    assert rc == 0  # warnings do not fail without --strict
    found = [v for v in data["violations"] if v["rule_id"] == "width_transition"]
    assert len(found) == 1
    # Sheet-absolute location of the transition node.
    assert found[0]["location"] == pytest.approx([103.0, 110.0])


def test_rule_ids_are_advisory_quality():
    from kicad_tools.validate import DRCChecker

    for rule_id in ("width_consistency", "width_island", "width_transition"):
        assert DRCChecker.category_for_rule(rule_id) == DRCChecker.CATEGORY_ADVISORY


def test_opt_in_not_run_by_default(tmp_path, capsys):
    """A plain ``kct check`` / ``check_all`` must not change existing verdicts."""
    from kicad_tools.cli.check_cmd import main
    from kicad_tools.validate import DRCChecker

    path = tmp_path / "board.kicad_pcb"
    path.write_text(_PCB_TEMPLATE.format(items=_neck_route(None)))
    main([str(path), "--format", "json", "--drc-only"])
    data = json.loads(capsys.readouterr().out)
    assert not [v for v in data["violations"] if v["rule_id"].startswith("width_")]

    checker = DRCChecker(PCB.load(str(path)), manufacturer="jlcpcb", layers=2)
    assert not [v for v in checker.check_all().violations if v.rule_id.startswith("width_")]
    checker.width_consistency_options = {}
    found = [v for v in checker.check_all().violations if v.rule_id == "width_transition"]
    assert len(found) == 1


def test_checker_method_accepts_rule_options(tmp_path):
    from kicad_tools.validate import DRCChecker

    pcb = _load(tmp_path, _neck_route(0.45))
    checker = DRCChecker(pcb, manufacturer="jlcpcb", layers=2)
    assert checker.check_width_consistency(clearance_mm=CLEARANCE).violations == []
    justified = checker.check_width_consistency(clearance_mm=CLEARANCE, report_justified=True)
    assert [v.severity for v in justified.violations] == ["info"]
