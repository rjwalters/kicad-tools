"""Kernel tests for the copper-vs-board-edge pairwise column (#9685).

The #4588 post-route audit had no board-edge model at all: ``route --complete
--voltage-map`` could commit copper closer to ``Edge.Cuts`` than the voltage
map requires and still print SUCCESS, while ``kct creepage`` failed the same
board's ``net vs <board edge>`` rows.  These tests pin the three kernel pieces
the audit gained: the ``_edge_voltage`` sidecar parse, the per-net edge
requirement (same standard lookup, worst-case interval differencing, no
``_pair_bounds``), and the copper-vs-outline walk itself.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from shapely.geometry import LineString
from shapely.ops import unary_union

from kicad_tools.router.pairwise_clearance import (
    board_edge_pairwise_violations,
    edge_required_clearances,
    load_edge_voltage,
)
from kicad_tools.router.primitives import Route, Segment, Via

# IEC 60664-1, PD2, material group IIIa step-up rows the fixture relies on.
REQ_150V = 1.6
REQ_300V = 3.2


def _vmap(tmp_path: Path, payload: dict) -> Path:
    p = tmp_path / "vmap.json"
    p.write_text(json.dumps(payload))
    return p


class TestLoadEdgeVoltage:
    def test_absent_key_defaults_to_zero(self, tmp_path: Path) -> None:
        assert load_edge_voltage(_vmap(tmp_path, {"/A": 150.0})) == 0.0

    def test_explicit_reference_potential_is_parsed(self, tmp_path: Path) -> None:
        vmap = _vmap(tmp_path, {"_edge_voltage": 12.0, "/A": 300.0})
        assert load_edge_voltage(vmap) == 12.0

    def test_range_reference_is_rejected(self, tmp_path: Path) -> None:
        vmap = _vmap(tmp_path, {"_edge_voltage": {"min": 0, "max": 10}})
        with pytest.raises(ValueError):
            load_edge_voltage(vmap)


class TestEdgeRequiredClearances:
    def test_scalar_map_150v_net_owes_the_census_row_requirement(self) -> None:
        req = edge_required_clearances(
            {"LINE": 150.0, "NEUT": 0.0}, edge_voltage=0.0, hv_threshold=0.0
        )
        assert req["LINE"] == pytest.approx(REQ_150V)
        # 0 V vs the 0 V earth reference: equal potential, no widening.
        assert "NEUT" not in req

    def test_edge_reference_is_differenced_not_assumed_earth(self) -> None:
        req = edge_required_clearances({"LINE": 12.0}, edge_voltage=0.0, hv_threshold=0.0)
        assert req["LINE"] == pytest.approx(0.42)  # 12.5 V row, PD2 IIIa

    def test_interval_net_uses_worst_case_endpoint(self) -> None:
        req = edge_required_clearances(
            {"LINE": 150.0},
            edge_voltage=0.0,
            hv_threshold=0.0,
            net_intervals={"LINE": (-150.0, 160.0)},
        )
        assert req["LINE"] == pytest.approx(REQ_150V)  # 160 V steps up to the 160 V row

    def test_hv_threshold_gates_the_edge_column_too(self) -> None:
        req = edge_required_clearances({"SENSE": 5.0}, edge_voltage=0.0, hv_threshold=30.0)
        assert req == {}

    def test_signed_map_differences_against_the_reference(self) -> None:
        # -300 V vs 0 V earth is a 300 V pair, not "no stress".
        req = edge_required_clearances({"LINE": -300.0}, edge_voltage=0.0, hv_threshold=0.0)
        assert req["LINE"] == pytest.approx(REQ_300V)


def _edge_rect(x1: float, y1: float, x2: float, y2: float):
    pts = [(x1, y1), (x2, y1), (x2, y2), (x1, y2), (x1, y1)]
    return unary_union([LineString([a, b]) for a, b in zip(pts, pts[1:], strict=False)])


def _route(net_name: str, *copper) -> Route:
    segments = [c for c in copper if isinstance(c, Segment)]
    vias = [c for c in copper if isinstance(c, Via)]
    return Route(net=1, net_name=net_name, segments=list(segments), vias=list(vias))


class TestBoardEdgePairwiseViolations:
    EDGE = _edge_rect(100.0, 100.0, 170.0, 152.0)

    def test_track_closer_than_the_edge_requirement_is_reported(self) -> None:
        # 0.2 mm-wide track centred 1.5 mm below the top edge: copper edge at
        # 1.4 mm -- under the 1.6 mm the 150 V map row requires.
        track = Segment(120.0, 101.5, 130.0, 101.5, 0.2, "F.Cu", 1, "LINE")
        out = board_edge_pairwise_violations(
            [_route("LINE", track)],
            {"LINE": REQ_150V},
            edge_geometry=self.EDGE,
            floor=0.2,
        )
        assert len(out) == 1
        v = out[0]
        assert v.net_a == "LINE"
        assert v.net_b == "<board edge>"
        assert v.actual_mm == pytest.approx(1.4, abs=1e-6)
        assert v.required_mm == pytest.approx(REQ_150V)

    def test_compliant_track_is_clean(self) -> None:
        track = Segment(120.0, 101.8, 130.0, 101.8, 0.2, "F.Cu", 1, "LINE")
        assert (
            board_edge_pairwise_violations(
                [_route("LINE", track)],
                {"LINE": REQ_150V},
                edge_geometry=self.EDGE,
                floor=0.2,
            )
            == []
        )

    def test_via_barrel_near_the_edge_is_reported(self) -> None:
        via = Via(125.0, 101.7, 0.4, 0.6, ("F.Cu", "B.Cu"), 1, "LINE")
        out = board_edge_pairwise_violations(
            [_route("LINE", via)],
            {"LINE": REQ_150V},
            edge_geometry=self.EDGE,
            floor=0.2,
        )
        assert len(out) == 1
        assert out[0].net_b == "<board edge>"
        assert out[0].actual_mm == pytest.approx(1.4, abs=1e-6)

    def test_net_without_an_edge_requirement_is_unchecked(self) -> None:
        track = Segment(120.0, 101.5, 130.0, 101.5, 0.2, "F.Cu", 2, "NEUT")
        assert (
            board_edge_pairwise_violations(
                [_route("NEUT", track)],
                {},
                edge_geometry=self.EDGE,
                floor=0.2,
            )
            == []
        )

    def test_requirement_at_or_below_the_floor_needs_no_widening(self) -> None:
        track = Segment(120.0, 101.5, 130.0, 101.5, 0.2, "F.Cu", 1, "SENSE")
        assert (
            board_edge_pairwise_violations(
                [_route("SENSE", track)],
                {"SENSE": 0.4},  # sub-threshold lookup below a 0.5 mm DRU floor
                edge_geometry=self.EDGE,
                floor=0.5,
            )
            == []
        )
