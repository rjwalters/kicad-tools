"""Match-group tuner clearance floors follow the net classes (Issue #6969).

``apply_match_group_tuning`` relaxes the tuner's post-insertion clearance
floor from the router-global rule to the group's own net-class clearance,
never below the manufacturer / authored floors.  That floor is only the
group's half of KiCad's rule: two items are held to the LARGER of their two
class clearances, so a neighbour whose class is stricter than the group's
must still reject a meander that lands inside the neighbour's clearance.
"""

from __future__ import annotations

from typing import Any

import pytest

from kicad_tools.router import match_group_tuning
from kicad_tools.router.core import Autorouter
from kicad_tools.router.layers import Layer
from kicad_tools.router.match_group_length import MatchGroup, MatchGroupSource
from kicad_tools.router.match_group_tuning import _post_insertion_clearance_ok_group
from kicad_tools.router.primitives import Pad, Route, Segment, Via
from kicad_tools.router.rules import NetClassRouting

GROUP_NET = 1
NEIGHBOUR_NET = 9


def _seg(x1: float, y1: float, x2: float, y2: float, net: int) -> Segment:
    return Segment(
        x1=x1, y1=y1, x2=x2, y2=y2, width=0.2, layer=Layer.F_CU, net=net, net_name=f"N{net}"
    )


def _route(net: int, pts: list[tuple[float, float]]) -> Route:
    return Route(
        net=net,
        net_name=f"N{net}",
        segments=[_seg(*a, *b, net) for a, b in zip(pts[:-1], pts[1:], strict=True)],
    )


# A new meander segment on the group net at y=0 and a neighbour track whose
# centreline sits at y=0.375: both 0.2 mm wide, so the copper gap is 0.175 mm.
_MEANDER = [_seg(0.0, 0.0, 10.0, 0.0, GROUP_NET)]
_GAP_0175 = 0.375


def _ok_vs_neighbour_track(
    centre_y: float, floor_mm: float, net_clearance_mm: dict[int, float] | None = None
) -> bool:
    return _post_insertion_clearance_ok_group(
        new_segments=_MEANDER,
        candidate_net_id=GROUP_NET,
        group_net_ids={GROUP_NET},
        routes_by_net={NEIGHBOUR_NET: _route(NEIGHBOUR_NET, [(0.0, centre_y), (10.0, centre_y)])},
        intra_group_clearance_mm=floor_mm,
        net_clearance_mm=net_clearance_mm,
    )


class TestTunerPairwiseFloor:
    def test_class_floor_accepts_meander_the_global_rule_rejected(self):
        assert not _ok_vs_neighbour_track(_GAP_0175, 0.200)
        assert _ok_vs_neighbour_track(_GAP_0175, 0.175)
        # A neighbour in the same (or a looser) class keeps the group floor.
        assert _ok_vs_neighbour_track(_GAP_0175, 0.175, {NEIGHBOUR_NET: 0.175})
        assert _ok_vs_neighbour_track(_GAP_0175, 0.175, {NEIGHBOUR_NET: 0.15})

    def test_looser_neighbour_class_never_lowers_the_group_floor(self):
        # 0.16 mm gap: below the group's 0.175 mm floor, above the neighbour's 0.15.
        assert not _ok_vs_neighbour_track(0.36, 0.175, {NEIGHBOUR_NET: 0.15})

    def test_stricter_neighbour_class_rejects_track_meander(self):
        assert not _ok_vs_neighbour_track(_GAP_0175, 0.175, {NEIGHBOUR_NET: 0.2})

    def test_stricter_neighbour_class_rejects_via_meander(self):
        # Via of diameter 0.4 centred 0.475 mm off the meander axis: the
        # edge gap is 0.475 - 0.1 (half track) - 0.2 (via radius) = 0.175 mm.
        via = Via(
            x=5.0,
            y=0.475,
            drill=0.2,
            diameter=0.4,
            layers=(Layer.F_CU, Layer.B_CU),
            net=NEIGHBOUR_NET,
            net_name=f"N{NEIGHBOUR_NET}",
        )
        routes = {NEIGHBOUR_NET: Route(net=NEIGHBOUR_NET, net_name="N9", vias=[via])}
        kwargs: dict[str, Any] = {
            "new_segments": _MEANDER,
            "candidate_net_id": GROUP_NET,
            "group_net_ids": {GROUP_NET},
            "routes_by_net": routes,
            "intra_group_clearance_mm": 0.175,
            "via_clearance_mm": 0.175,
        }
        assert _post_insertion_clearance_ok_group(**kwargs)
        assert not _post_insertion_clearance_ok_group(
            **kwargs, net_clearance_mm={NEIGHBOUR_NET: 0.2}
        )

    def test_stricter_neighbour_class_rejects_pad_meander(self):
        # 0.4 mm square pad centred 0.475 mm off the axis: edge gap 0.175 mm.
        pad = Pad(x=5.0, y=0.475, width=0.4, height=0.4, net=NEIGHBOUR_NET, net_name="N9", ref="U1")
        kwargs: dict[str, Any] = {
            "new_segments": _MEANDER,
            "candidate_net_id": GROUP_NET,
            "group_net_ids": {GROUP_NET},
            "routes_by_net": {},
            "intra_group_clearance_mm": 0.175,
            "foreign_pads": [pad],
            "pad_clearance_mm": 0.175,
        }
        assert _post_insertion_clearance_ok_group(**kwargs)
        assert not _post_insertion_clearance_ok_group(
            **kwargs, net_clearance_mm={NEIGHBOUR_NET: 0.2}
        )

    def test_diff_pair_partner_is_exempt_from_class_raise(self):
        # The partner runs at the intra-pair gap; its class must not lift
        # the pass-2 floor above the group floor.
        assert _post_insertion_clearance_ok_group(
            new_segments=_MEANDER,
            candidate_net_id=GROUP_NET,
            group_net_ids={GROUP_NET},
            routes_by_net={
                NEIGHBOUR_NET: _route(NEIGHBOUR_NET, [(0.0, _GAP_0175), (10.0, _GAP_0175)])
            },
            intra_group_clearance_mm=0.175,
            diff_pair_partners={GROUP_NET: NEIGHBOUR_NET},
            intra_pair_clearance_mm=0.1,
            net_clearance_mm={NEIGHBOUR_NET: 0.2},
        )


def _autorouter(group_clearance: float, neighbour_clearance: float | None = 0.2) -> Autorouter:
    ar = Autorouter(width=60.0, height=60.0)
    ar.net_names = {1: "A0", 2: "A1", NEIGHBOUR_NET: "VCC"}
    ar.routes = [
        _route(1, [(0.0, 0.0), (22.0, 0.0)]),
        _route(2, [(0.0, 10.0), (18.0, 10.0)]),
        _route(NEIGHBOUR_NET, [(0.0, 20.0), (18.0, 20.0)]),
    ]
    bus = NetClassRouting(
        name="BUS",
        clearance=group_clearance,
        length_critical=True,
        length_match_group="BUS",
        length_match_tolerance_mm=0.1,
    )
    ar.net_class_map = {"A0": bus, "A1": bus}
    if neighbour_clearance is not None:
        ar.net_class_map["VCC"] = NetClassRouting(name="Default", clearance=neighbour_clearance)
    return ar


def _captured_floors(ar: Autorouter, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    def fake_tune(group, routes_by_net, **kwargs):
        captured.update(kwargs)
        return {}

    monkeypatch.setattr(match_group_tuning, "tune_match_group_v2", fake_tune)
    group = MatchGroup(
        name="BUS", net_ids=[1, 2], tolerance=0.1, source=MatchGroupSource.LEGACY_API
    )
    ar.apply_match_group_tuning(detected_groups=[group], verbose=False)
    assert captured, "tuner was not invoked"
    return captured


class TestApplyMatchGroupTuningFloors:
    def test_group_class_floor_is_applied(self, monkeypatch):
        ar = _autorouter(group_clearance=0.175)
        assert ar.rules.trace_clearance == pytest.approx(0.2)
        kw = _captured_floors(ar, monkeypatch)
        assert kw["intra_group_clearance_mm"] == pytest.approx(0.175)
        assert kw["via_clearance_mm"] == pytest.approx(0.175)
        assert kw["pad_clearance_mm"] == pytest.approx(0.175)

    @pytest.mark.parametrize(
        ("attr", "floor"),
        [("authored_min_clearance", 0.15), ("mfr_clearance_floor", 0.16)],
    )
    def test_mfr_or_authored_floor_clamps_from_below(self, monkeypatch, attr, floor):
        ar = _autorouter(group_clearance=0.1)
        setattr(ar.rules, attr, floor)
        kw = _captured_floors(ar, monkeypatch)
        assert kw["intra_group_clearance_mm"] == pytest.approx(floor)
        assert kw["via_clearance_mm"] == pytest.approx(floor)
        assert kw["pad_clearance_mm"] == pytest.approx(floor)

    def test_stricter_neighbour_class_reaches_the_tuner(self, monkeypatch):
        kw = _captured_floors(_autorouter(group_clearance=0.15), monkeypatch)
        assert kw["intra_group_clearance_mm"] == pytest.approx(0.15)
        per_net = kw["net_clearance_mm"]
        assert per_net[NEIGHBOUR_NET] == pytest.approx(0.2)
        assert per_net[1] == pytest.approx(0.15)
        # The pairwise floor the tuner applies to the neighbour:
        assert max(kw["intra_group_clearance_mm"], per_net[NEIGHBOUR_NET]) == pytest.approx(0.2)

    def test_group_class_never_raises_floor_above_global_rule(self, monkeypatch):
        # Issue #6969 R2: the class value only relaxes the global rule; a
        # class stricter than the rule never tightens the tuner's own floor.
        ar = _autorouter(group_clearance=0.3, neighbour_clearance=0.4)
        kw = _captured_floors(ar, monkeypatch)
        assert kw["intra_group_clearance_mm"] == pytest.approx(0.2)
        assert kw["via_clearance_mm"] == pytest.approx(0.2)
        assert kw["pad_clearance_mm"] == pytest.approx(0.2)

    def test_stricter_unrouted_pad_net_class_reaches_the_tuner(self, monkeypatch):
        # Issue #6969 R2: a classified net present only as a pad (unrouted)
        # is held to its own class; the group's relaxed floor is untouched.
        ar = _autorouter(group_clearance=0.175, neighbour_clearance=None)
        ar.net_names[7] = "SENSE"
        ar.pads = {
            ("U9", "1"): Pad(
                x=40.0, y=40.0, width=0.3, height=0.3, net=7, net_name="SENSE", ref="U9", pin="1"
            )
        }
        ar.net_class_map["SENSE"] = NetClassRouting(name="SENSE", clearance=0.19)
        kw = _captured_floors(ar, monkeypatch)
        assert kw["pad_clearance_mm"] == pytest.approx(0.175)
        assert kw["via_clearance_mm"] == pytest.approx(0.175)
        assert kw["net_clearance_mm"][7] == pytest.approx(0.19)

    def test_classless_group_keeps_single_threshold(self, monkeypatch):
        ar = _autorouter(group_clearance=0.175)
        ar.net_class_map = {}
        kw = _captured_floors(ar, monkeypatch)
        assert kw["intra_group_clearance_mm"] == pytest.approx(0.2)
        assert kw["net_clearance_mm"] is None


class TestNetZeroAndUnclassifiedNeighbours:
    """Issue #6979: KiCad puts net-0 and unclassified copper in the Default class."""

    @staticmethod
    def _with_pad(ar: Autorouter, net: int, name: str) -> Autorouter:
        ar.pads = {
            ("J1", "1"): Pad(
                x=10.0, y=30.0, width=0.3, height=0.3, net=net, net_name=name, ref="J1", pin="1"
            )
        }
        return ar

    def test_net_zero_pad_uses_the_default_class(self, monkeypatch):
        ar = self._with_pad(_autorouter(group_clearance=0.15), 0, "")
        ar.net_class_map["Default"] = NetClassRouting(name="Default", clearance=0.25)
        kw = _captured_floors(ar, monkeypatch)
        assert kw["pad_clearance_mm"] == pytest.approx(0.15)
        assert kw["net_clearance_mm"][0] == pytest.approx(0.25)

    def test_net_zero_pad_falls_back_to_trace_clearance(self, monkeypatch):
        ar = self._with_pad(_autorouter(group_clearance=0.15), 0, "")
        assert "Default" not in ar.net_class_map
        kw = _captured_floors(ar, monkeypatch)
        assert kw["net_clearance_mm"][0] == pytest.approx(ar.rules.trace_clearance)

    def test_unclassified_unrouted_pad_net_uses_the_default_class(self, monkeypatch):
        ar = self._with_pad(_autorouter(group_clearance=0.15), 7, "NC_7")
        ar.net_names[7] = "NC_7"
        ar.net_class_map["Default"] = NetClassRouting(name="Default", clearance=0.2)
        kw = _captured_floors(ar, monkeypatch)
        assert kw["net_clearance_mm"][7] == pytest.approx(0.2)

    def test_net_zero_pad_rejects_meander_inside_default_clearance(self):
        # Judge repro from PR #6972: a 0.15 mm group with a no-net pad under
        # a 0.2 mm Default class.  The relaxed floor alone accepted a
        # 0.170 mm meander-to-pad gap; the pairwise rule must keep >= 0.2 mm.
        from kicad_tools.router.clearance_shapes import copper_gap, pad_shape, segment_shape

        def build(pad: Pad | None) -> Autorouter:
            ar = _autorouter(group_clearance=0.15, neighbour_clearance=None)
            ar.routes = [r for r in ar.routes if r.net != NEIGHBOUR_NET]
            ar.rules.trace_clearance = 0.2
            ar.rules.via_clearance = 0.2
            ar.rules.mfr_clearance_floor = None
            ar.rules.authored_min_clearance = None
            ar.net_class_map["Default"] = NetClassRouting(name="Default", clearance=0.2)
            if pad is not None:
                ar.pads = {("J1", "1"): pad}
            group = MatchGroup(
                name="BUS", net_ids=[1, 2], tolerance=0.1, source=MatchGroupSource.LEGACY_API
            )
            ar.apply_match_group_tuning(
                detected_groups=[group], verbose=False, engage="length_critical"
            )
            return ar

        ar = build(None)
        meander = next(r for r in ar.routes if r.net == 2).segments
        assert len(meander) > 1, "the group must meander without the pad"
        outer = max(meander, key=lambda s: max(abs(s.y1 - 10.0), abs(s.y2 - 10.0)))
        sign = 1.0 if max(outer.y1, outer.y2) - 10.0 > 0 else -1.0
        y_edge = max(outer.y1, outer.y2) if sign > 0 else min(outer.y1, outer.y2)
        # 0.3 x 0.3 pad whose edge sits 0.17 mm beyond the meander's edge.
        pad = Pad(
            x=(outer.x1 + outer.x2) / 2,
            y=y_edge + sign * (0.1 + 0.17 + 0.15),
            width=0.3,
            height=0.3,
            net=0,
            net_name="",
            ref="J1",
            pin="1",
        )
        ar = build(pad)
        segs = next(r for r in ar.routes if r.net == 2).segments
        gap = min(copper_gap(segment_shape(s), pad_shape(pad)) for s in segs)
        assert gap + 1e-9 >= 0.2
