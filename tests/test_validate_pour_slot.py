"""Tests for the pour-slot advisory rule (Issue #8435)."""

from __future__ import annotations

from dataclasses import dataclass, field

from kicad_tools.validate.rules.pour_slot import PourSlotRule


@dataclass
class _Seg:
    start: tuple[float, float]
    end: tuple[float, float]
    width: float = 0.2
    layer: str = "F.Cu"
    net_number: int = 1
    net_name: str = "A"


@dataclass
class _Zone:
    layer: str = "F.Cu"
    layers: list = field(default_factory=list)
    keepout: object = None
    clearance: float = 0.3
    min_thickness: float = 0.25


@dataclass
class _PCB:
    segments: list = field(default_factory=list)
    zones: list = field(default_factory=list)


def _pair(gap, **kw):
    """Two 0.2 mm tracks 10 mm long with the given edge-to-edge gap."""
    pitch = gap + 0.2
    return [
        _Seg((0, 0), (10, 0)),
        _Seg((0, pitch), (10, pitch), net_number=2, net_name="B", **kw),
    ]


def _run(segs, zones=(None,), rule=None):
    zs = [_Zone()] if zones == (None,) else list(zones)
    return (rule or PourSlotRule()).check(_PCB(segs, zs), None)


# Threshold with defaults: 2 * 0.3 + 0.25 = 0.85 mm.


def test_flagged_pair():
    res = _run(_pair(0.5))
    assert [v.rule_id for v in res.violations] == ["pour_slot"]
    v = res.violations[0]
    assert v.severity == "warning"
    assert "10.00 mm" in v.message
    assert abs(v.actual_value - 0.5) < 1e-6
    assert abs(v.required_value - 0.85) < 1e-6


def test_pair_at_or_above_fill_width_is_clean():
    assert _run(_pair(0.85)).violations == []
    assert _run(_pair(1.5)).violations == []


def test_same_net_pair_not_reported():
    segs = _pair(0.5)
    segs[1].net_number, segs[1].net_name = 1, "A"
    assert _run(segs).violations == []


def test_layer_without_zone_not_reported():
    assert _run(_pair(0.5), zones=[_Zone(layer="B.Cu")]).violations == []
    assert _run(_pair(0.5), zones=[]).violations == []


def test_touching_or_overlapping_left_to_drc():
    assert _run(_pair(0.0)).violations == []
    assert _run(_pair(-0.1)).violations == []


def test_netclass_clearance_raises_threshold():
    # gap 1.0 is clean with zone clearance 0.3 (threshold 0.85) ...
    assert _run(_pair(1.0)).violations == []
    # ... but a 0.5 mm netclass clearance gives 2*0.5+0.25 = 1.25.
    res = _run(_pair(1.0), rule=PourSlotRule({"B": 0.5}))
    assert len(res.violations) == 1
    assert abs(res.violations[0].required_value - 1.25) < 1e-6


def test_zone_clearance_wins_over_smaller_netclass():
    res = _run(_pair(0.5), rule=PourSlotRule({"B": 0.1}))
    assert abs(res.violations[0].required_value - 0.85) < 1e-6


def test_collinear_non_overlapping_not_reported():
    segs = [_Seg((0, 0), (5, 0)), _Seg((5.1, 0), (10, 0), net_number=2, net_name="B")]
    assert _run(segs).violations == []


def test_side_by_side_but_no_overlap_not_reported():
    segs = [_Seg((0, 0), (5, 0)), _Seg((6, 0.7), (10, 0.7), net_number=2, net_name="B")]
    assert _run(segs).violations == []


def test_diagonal_pair_flagged_and_perpendicular_ignored():
    d = 0.5 / 2**0.5 + 0.2 / 2**0.5  # perpendicular pitch 0.7 along the diagonal normal
    segs = [
        _Seg((0, 0), (10, 10)),
        _Seg((-d, d), (10 - d, 10 + d), net_number=2, net_name="B"),
    ]
    assert len(_run(segs).violations) == 1
    cross = [_Seg((0, 0), (10, 0)), _Seg((5, -3), (5, 3), net_number=2, net_name="B")]
    assert _run(cross).violations == []


def test_keepout_zone_is_not_a_pour():
    assert _run(_pair(0.5), zones=[_Zone(keepout=object())]).violations == []


def test_multi_zone_layer_uses_widest_requirement():
    zones = [_Zone(), _Zone(clearance=0.5, min_thickness=0.25)]
    res = _run(_pair(1.0), zones=zones)
    assert len(res.violations) == 1


def test_each_pair_reported_once():
    segs = _pair(0.5) + [_Seg((0, 1.4), (10, 1.4), net_number=3, net_name="C")]
    # A-B gap 0.5 and B-C gap 0.7 (both flagged); A-C gap 1.2 clean.
    assert len(_run(segs).violations) == 2


def test_duplicated_segment_reports_slot_once():
    # Issue #8503: two identical copies of a track (already reported by
    # redundant_copper_duplicate) next to a neighbour are one slot, not two.
    segs = _pair(0.5)
    segs.append(_Seg((0, 0), (10, 0)))
    assert len(_run(segs).violations) == 1
    # Reversed endpoint order is the same copper too.
    segs.append(_Seg((10, 0), (0, 0)))
    assert len(_run(segs).violations) == 1


def test_duplicate_of_both_tracks_reports_slot_once():
    segs = _pair(0.5) + _pair(0.5)
    assert len(_run(segs).violations) == 1


def test_parallel_copy_with_different_width_is_distinct():
    # Not an exact duplicate (different width): it is distinct geometry.
    segs = _pair(0.5)
    segs.append(_Seg((0, 0), (10, 0), width=0.25))
    assert len(_run(segs).violations) == 2


def test_two_zones_on_one_layer_report_each_pair_once():
    # Issue #8503: several pours sharing a layer must not multiply findings.
    res = _run(_pair(0.5), zones=[_Zone(), _Zone(clearance=0.2)])
    assert len(res.violations) == 1
    # The widest requirement of the layer's pours applies.
    assert abs(res.violations[0].required_value - 0.85) < 1e-6
