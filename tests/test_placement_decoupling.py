"""Decoupling-capacitor affinity for the placement optimizer (issue #6020).

Covers the cap-to-pin identification, the spreading assignment, the cost
term inside ``evaluate_placement``, the post-optimizer snap pass, and the
``optimize-placement`` wiring.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from kicad_tools.placement.cost import (
    BoardOutline,
    ComponentPlacement,
    CostMode,
    DesignRuleSet,
    Net,
    PlacementCostConfig,
    compute_overlap,
    evaluate_placement,
)
from kicad_tools.placement.decoupling import (
    DecouplingCap,
    DecouplingGroup,
    SupplyPin,
    assign_caps_to_pins,
    compute_decoupling_distance,
    decoupling_pairs,
    identify_decoupling_groups,
    snap_decoupling_caps,
)
from kicad_tools.placement.vector import (
    ComponentDef,
    PadDef,
    PlacedComponent,
    PlacementVector,
    decode,
    encode,
)
from kicad_tools.placement.wirelength import build_pad_position_map

# ---------------------------------------------------------------------------
# Identification
# ---------------------------------------------------------------------------


def _mcu_nets() -> list[Net]:
    """U1 has two VDD pins, a VSS pin and an EN pin tied to +3V3."""
    return [
        Net("+3V3", [("U1", "1"), ("U1", "2"), ("U1", "4"), ("C1", "1"), ("C2", "2"), ("R1", "1")]),
        Net("GND", [("U1", "3"), ("C1", "2"), ("C2", "1"), ("C3", "2"), ("C4", "2")]),
        Net("SIG", [("C3", "1"), ("R1", "2"), ("U1", "5")]),
        Net("+5V", [("C4", "1"), ("U2", "1"), ("C5", "1")]),
        # C5 sits between two supplies, not supply and ground.
        Net("+3V3_B", [("C5", "2"), ("U2", "2")]),
    ]


class TestIdentifyDecouplingGroups:
    def test_finds_caps_between_supply_and_ground(self):
        groups = {g.net: g for g in identify_decoupling_groups(_mcu_nets())}
        assert set(groups) == {"+3V3", "+5V"}
        rail = groups["+3V3"]
        assert rail.caps == (
            DecouplingCap("C1", supply_pad="1", ground_pad="2"),
            DecouplingCap("C2", supply_pad="2", ground_pad="1"),
        )
        # Untyped board: every IC pad on the rail is a supply pin.
        assert rail.pins == (SupplyPin("U1", "1"), SupplyPin("U1", "2"), SupplyPin("U1", "4"))

    def test_ignores_signal_caps_resistors_and_supply_to_supply_caps(self):
        groups = identify_decoupling_groups(_mcu_nets())
        refs = {c.reference for g in groups for c in g.caps}
        assert "C3" not in refs  # SIG -> GND: not a supply net
        assert "C5" not in refs  # +5V -> +3V3_B: no ground pad
        assert "R1" not in refs  # not a capacitor
        assert {g.net for g in groups} == {"+3V3", "+5V"}

    def test_pin_types_drop_non_supply_pins(self):
        types = {
            ("U1", "1"): ("power_in", "VDD"),
            ("U1", "2"): ("power_in", "VDD"),
            ("U1", "4"): ("input", "EN"),
        }
        rail = next(g for g in identify_decoupling_groups(_mcu_nets(), types) if g.net == "+3V3")
        assert rail.pins == (SupplyPin("U1", "1"), SupplyPin("U1", "2"))

    def test_pin_types_mark_unconventionally_named_rail(self):
        nets = [
            Net("MCU_SUPPLY", [("U1", "1"), ("C1", "1")]),
            Net("GND", [("U1", "2"), ("C1", "2")]),
        ]
        assert identify_decoupling_groups(nets) == []
        groups = identify_decoupling_groups(nets, {("U1", "1"): ("power_in", "VDD")})
        assert [g.net for g in groups] == ["MCU_SUPPLY"]

    def test_no_ic_or_no_cap_means_no_group(self):
        nets = [
            Net("+3V3", [("J1", "1"), ("C1", "1")]),
            Net("GND", [("J1", "2"), ("C1", "2")]),
        ]
        assert identify_decoupling_groups(nets) == []


# ---------------------------------------------------------------------------
# Assignment
# ---------------------------------------------------------------------------


def _group(n_caps: int, n_pins: int) -> DecouplingGroup:
    return DecouplingGroup(
        net="+3V3",
        caps=tuple(DecouplingCap(f"C{i}", "1", "2") for i in range(1, n_caps + 1)),
        pins=tuple(SupplyPin("U1", str(i)) for i in range(1, n_pins + 1)),
    )


class TestAssignCapsToPins:
    def test_spreads_caps_one_per_pin(self):
        # Both caps are nearest pin 1, but pin 2 must not go without.
        positions = {
            ("U1", "1"): (0.0, 0.0),
            ("U1", "2"): (10.0, 0.0),
            ("C1", "1"): (1.0, 0.0),
            ("C2", "1"): (2.0, 0.0),
        }
        pairs = {
            c.reference: (p.pad, d) for c, p, d in assign_caps_to_pins(_group(2, 2), positions)
        }
        assert pairs["C1"] == ("1", pytest.approx(1.0))
        assert pairs["C2"] == ("2", pytest.approx(8.0))

    def test_extra_caps_double_up_only_after_every_pin_has_one(self):
        positions = {
            ("U1", "1"): (0.0, 0.0),
            ("U1", "2"): (10.0, 0.0),
            ("C1", "1"): (0.5, 0.0),
            ("C2", "1"): (1.0, 0.0),
            ("C3", "1"): (1.5, 0.0),
        }
        pairs = assign_caps_to_pins(_group(3, 2), positions)
        loads: dict[str, int] = {}
        for _, pin, _ in pairs:
            loads[pin.pad] = loads.get(pin.pad, 0) + 1
        assert loads == {"1": 2, "2": 1}

    def test_max_per_pin_leaves_extra_caps_unassigned(self):
        positions = {
            ("U1", "1"): (0.0, 0.0),
            ("U1", "2"): (10.0, 0.0),
            ("C1", "1"): (0.5, 0.0),
            ("C2", "1"): (1.0, 0.0),
            ("C3", "1"): (1.5, 0.0),
        }
        pairs = assign_caps_to_pins(_group(3, 2), positions, max_per_pin=1)
        # C3 is the cap nearest pin 2 once C1 holds pin 1; C2 is left over.
        assert sorted((c.reference, p.pad) for c, p, _ in pairs) == [("C1", "1"), ("C3", "2")]

    def test_fewer_caps_than_pins_takes_closest_pairs(self):
        positions = {
            ("U1", "1"): (0.0, 0.0),
            ("U1", "2"): (10.0, 0.0),
            ("U1", "3"): (20.0, 0.0),
            ("C1", "1"): (19.0, 0.0),
        }
        [(cap, pin, dist)] = assign_caps_to_pins(_group(1, 3), positions)
        assert (cap.reference, pin.pad, dist) == ("C1", "3", pytest.approx(1.0))

    def test_missing_positions_are_skipped(self):
        assert assign_caps_to_pins(_group(2, 2), {("U1", "1"): (0.0, 0.0)}) == []


# ---------------------------------------------------------------------------
# Cost term
# ---------------------------------------------------------------------------

_RULES = DesignRuleSet()
_BOARD = BoardOutline(0.0, 0.0, 50.0, 50.0)
_SIZES = {"U1": (6.0, 6.0), "C1": (2.0, 1.0)}
_NETS = [
    Net("+3V3", [("U1", "1"), ("C1", "1")]),
    Net("GND", [("U1", "2"), ("C1", "2")]),
]
_GROUPS = identify_decoupling_groups(_NETS)


def _placements(cap_x: float) -> list[ComponentPlacement]:
    return [ComponentPlacement("U1", 10.0, 10.0), ComponentPlacement("C1", cap_x, 10.0)]


def _pads(cap_x: float) -> dict[tuple[str, str], tuple[float, float]]:
    return {
        ("U1", "1"): (13.0, 10.0),
        ("U1", "2"): (7.0, 10.0),
        ("C1", "1"): (cap_x - 1.0, 10.0),
        ("C1", "2"): (cap_x + 1.0, 10.0),
    }


class TestDecouplingCost:
    def test_distance_is_cap_supply_pad_to_assigned_pin(self):
        assert compute_decoupling_distance(_GROUPS, _placements(20.0), _pads(20.0)) == (
            pytest.approx(6.0)
        )

    def test_falls_back_to_component_centres(self):
        assert compute_decoupling_distance(_GROUPS, _placements(20.0)) == pytest.approx(10.0)

    def test_dormant_without_groups(self):
        assert compute_decoupling_distance(None, _placements(20.0), _pads(20.0)) == 0.0
        assert compute_decoupling_distance([], _placements(20.0), _pads(20.0)) == 0.0

    def test_evaluate_placement_reports_and_weights_the_term(self):
        config = PlacementCostConfig(decoupling_weight=3.0)
        base = evaluate_placement(_placements(20.0), _NETS, _RULES, _BOARD, config, _SIZES)
        with_term = evaluate_placement(
            _placements(20.0),
            _NETS,
            _RULES,
            _BOARD,
            config,
            _SIZES,
            decoupling_groups=_GROUPS,
            decoupling_pad_positions=_pads(20.0),
        )
        assert base.breakdown.decoupling == 0.0
        assert with_term.breakdown.decoupling == pytest.approx(6.0)
        assert with_term.total == pytest.approx(base.total + 18.0)
        # Soft term: never part of feasibility.
        assert with_term.is_feasible == base.is_feasible

    def test_closer_cap_scores_better(self):
        def total(cap_x: float) -> float:
            return evaluate_placement(
                _placements(cap_x),
                _NETS,
                _RULES,
                _BOARD,
                PlacementCostConfig(),
                _SIZES,
                decoupling_groups=_GROUPS,
                decoupling_pad_positions=_pads(cap_x),
            ).breakdown.decoupling

        assert total(16.0) < total(20.0) < total(30.0)

    def test_lexicographic_feasible_branch_includes_term(self):
        config = PlacementCostConfig(mode=CostMode.LEXICOGRAPHIC, decoupling_weight=2.0)
        near = evaluate_placement(
            _placements(16.0),
            _NETS,
            _RULES,
            _BOARD,
            config,
            _SIZES,
            decoupling_groups=_GROUPS,
            decoupling_pad_positions=_pads(16.0),
        )
        assert near.is_feasible
        expected = (
            config.wirelength_weight * near.breakdown.wirelength
            + config.area_weight * near.breakdown.area
            + config.decoupling_weight * near.breakdown.decoupling
        )
        assert near.total == pytest.approx(expected)

    def test_zero_weight_disables_term(self):
        score = evaluate_placement(
            _placements(20.0),
            _NETS,
            _RULES,
            _BOARD,
            PlacementCostConfig(decoupling_weight=0.0),
            _SIZES,
            decoupling_groups=_GROUPS,
            decoupling_pad_positions=_pads(20.0),
        )
        assert score.breakdown.decoupling == 0.0


class TestRotationAwareBoxes:
    def test_rotated_part_overlap_uses_swapped_extent(self):
        sizes = {"A": (10.0, 1.0), "B": (1.0, 1.0)}
        # B sits 3 mm below A's centre: clear of a flat A, inside a turned A.
        flat = [ComponentPlacement("A", 0.0, 0.0, 0.0), ComponentPlacement("B", 0.0, 3.0)]
        turned = [ComponentPlacement("A", 0.0, 0.0, 90.0), ComponentPlacement("B", 0.0, 3.0)]
        assert compute_overlap(flat, sizes) == 0.0
        assert compute_overlap(turned, sizes) > 0.0


# ---------------------------------------------------------------------------
# Snap pass
# ---------------------------------------------------------------------------


def _snap_fixture():
    components = [
        ComponentDef(
            "U1",
            pads=(
                PadDef("1", 3.0, 0.0, 1.0, 0.5),
                PadDef("2", -3.0, 0.0, 1.0, 0.5),
            ),
            width=7.0,
            height=1.0,
        ),
        ComponentDef(
            "C1",
            pads=(PadDef("1", -1.0, 0.0, 1.0, 1.2), PadDef("2", 1.0, 0.0, 1.0, 1.2)),
            width=3.0,
            height=1.2,
        ),
    ]
    vector = encode(
        [
            PlacedComponent("U1", 20.0, 20.0, 0.0, 0),
            PlacedComponent("C1", 40.0, 40.0, 0.0, 0),
        ]
    )
    config = PlacementCostConfig(mode=CostMode.LEXICOGRAPHIC)
    sizes = {c.reference: (c.width, c.height) for c in components}

    def score_fn(vec: PlacementVector):
        placed = decode(vec, components)
        return evaluate_placement(
            [ComponentPlacement(p.reference, p.x, p.y, p.rotation) for p in placed],
            _NETS,
            _RULES,
            _BOARD,
            config,
            sizes,
            decoupling_groups=_GROUPS,
            decoupling_pad_positions=build_pad_position_map(placed),
        )

    return components, vector, config, score_fn


class TestSnapDecouplingCaps:
    def test_moves_cap_beside_its_pin_without_violations(self):
        components, vector, config, score_fn = _snap_fixture()
        before = score_fn(vector)
        new_vector, moves = snap_decoupling_caps(
            vector, components, _GROUPS, _BOARD, score_fn, config, margin_mm=0.5
        )
        after = score_fn(new_vector)

        assert [(m.cap, m.pin) for m in moves] == [("C1", "U1.1")]
        assert moves[0].after_mm <= 3.0 < moves[0].before_mm
        assert after.is_feasible
        assert after.breakdown.decoupling < before.breakdown.decoupling
        # U1 never moves.
        np.testing.assert_array_equal(new_vector.data[:4], vector.data[:4])

        # Real pad geometry keeps the margin.
        placed = {p.reference: p for p in decode(new_vector, components)}

        def box(ref):
            pads = placed[ref].pads
            return (
                min(p.x - p.size_x / 2 for p in pads),
                min(p.y - p.size_y / 2 for p in pads),
                max(p.x + p.size_x / 2 for p in pads),
                max(p.y + p.size_y / 2 for p in pads),
            )

        u, c = box("U1"), box("C1")
        gap = max(c[0] - u[2], u[0] - c[2], c[1] - u[3], u[1] - c[3])
        assert gap >= 0.5 - 1e-9

    def test_courtyard_extents_keep_courtyards_apart(self):
        components, vector, config, score_fn = _snap_fixture()
        # U1's courtyard reaches 3 mm past its pad row on every side.
        extents = {"U1": (-6.5, -3.5, 6.5, 3.5), "C1": (-1.75, -0.85, 1.75, 0.85)}
        free, _ = snap_decoupling_caps(vector, components, _GROUPS, _BOARD, score_fn, config)
        kept, moves = snap_decoupling_caps(
            vector, components, _GROUPS, _BOARD, score_fn, config, extents=extents
        )
        assert moves
        placed = {p.reference: p for p in decode(kept, components)}
        cx, cy = placed["C1"].x, placed["C1"].y
        u_box = (20.0 - 6.5, 20.0 - 3.5, 20.0 + 6.5, 20.0 + 3.5)
        rot = placed["C1"].rotation % 180
        hx, hy = (1.75, 0.85) if rot == 0 else (0.85, 1.75)
        c_box = (cx - hx, cy - hy, cx + hx, cy + hy)
        overlap = (
            c_box[0] < u_box[2]
            and u_box[0] < c_box[2]
            and c_box[1] < u_box[3]
            and u_box[1] < c_box[3]
        )
        assert not overlap
        # Without extents the cap sits closer, inside U1's courtyard.
        assert decode(free, components)[1].x != cx or decode(free, components)[1].y != cy

    def test_no_move_when_no_free_spot(self):
        components, vector, config, score_fn = _snap_fixture()
        # A margin wider than the board leaves no legal spot anywhere.
        new_vector, moves = snap_decoupling_caps(
            vector, components, _GROUPS, _BOARD, score_fn, config, margin_mm=30.0
        )
        assert moves == []
        np.testing.assert_array_equal(new_vector.data, vector.data)

    def test_pairs_report_matches_cost(self):
        components, vector, _config, score_fn = _snap_fixture()
        placed = decode(vector, components)
        pairs = decoupling_pairs(
            _GROUPS,
            [ComponentPlacement(p.reference, p.x, p.y, p.rotation) for p in placed],
            build_pad_position_map(placed),
        )
        assert sum(d for *_, d in pairs) == pytest.approx(score_fn(vector).breakdown.decoupling)


# ---------------------------------------------------------------------------
# optimize-placement wiring
# ---------------------------------------------------------------------------

_DECAP_PCB = """\
(kicad_pcb (version 20230101) (generator "test")
  (general (thickness 1.6))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
  )
  (setup
    (pad_to_mask_clearance 0.05)
  )
  (net 0 "")
  (net 1 "+3V3")
  (net 2 "GND")
  (net 3 "EN")
  (footprint "SOIC" (layer "F.Cu")
    (at 15.0 15.0 0)
    (property "Reference" "U1")
    (pad "1" smd rect (at 3.0 0.0) (size 1.0 0.5) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "+3V3") (pinfunction "VDD") (pintype "power_in"))
    (pad "2" smd rect (at -3.0 0.0) (size 1.0 0.5) (layers "F.Cu" "F.Paste" "F.Mask") (net 2 "GND") (pinfunction "VSS") (pintype "power_in"))
    (pad "3" smd rect (at 0.0 1.0) (size 1.0 0.5) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "+3V3") (pinfunction "EN") (pintype "input"))
  )
  (footprint "C_0805" (layer "F.Cu")
    (at 26.0 26.0 0)
    (property "Reference" "C1")
    (pad "1" smd rect (at -1.0 0.0) (size 1.0 1.2) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "+3V3"))
    (pad "2" smd rect (at 1.0 0.0) (size 1.0 1.2) (layers "F.Cu" "F.Paste" "F.Mask") (net 2 "GND"))
  )
  (gr_line (start 0 0) (end 30 0) (layer "Edge.Cuts") (width 0.05))
  (gr_line (start 30 0) (end 30 30) (layer "Edge.Cuts") (width 0.05))
  (gr_line (start 30 30) (end 0 30) (layer "Edge.Cuts") (width 0.05))
  (gr_line (start 0 30) (end 0 0) (layer "Edge.Cuts") (width 0.05))
)
"""


@pytest.fixture
def decap_pcb(tmp_path: Path) -> Path:
    path = tmp_path / "decap.kicad_pcb"
    path.write_text(_DECAP_PCB)
    return path


class TestOptimizePlacementWiring:
    @pytest.fixture(autouse=True)
    def _needs_cmaes(self):
        pytest.importorskip("cmaes", reason="cmaes not installed")

    def test_parse_weights_decoupling_key(self):
        from kicad_tools.cli.optimize_placement_cmd import _parse_weights

        assert _parse_weights(None).decoupling_weight == 2.0
        assert _parse_weights('{"decoupling": 0}').decoupling_weight == 0.0

    def test_pin_types_reach_identification(self, decap_pcb):
        from kicad_tools.cli.optimize_placement_cmd import (
            _build_decoupling_context,
            _parse_weights,
            _read_board_data,
        )

        _, nets, *_ = _read_board_data(str(decap_pcb))
        groups = _build_decoupling_context(str(decap_pcb), nets, _parse_weights(None), quiet=True)
        assert groups is not None
        # U1.3 is EN (typed input) and is not a decoupling target.
        assert [(g.net, g.pins) for g in groups] == [("+3V3", (SupplyPin("U1", "1"),))]
        disabled = _parse_weights('{"decoupling": 0}')
        assert _build_decoupling_context(str(decap_pcb), nets, disabled, quiet=True) is None

    def test_with_sides_pins_side_flags(self):
        from kicad_tools.cli.optimize_placement_cmd import _with_sides

        vec = PlacementVector(data=np.array([1.0, 2.0, 1.0, 1.0, 3.0, 4.0, 0.0, 1.0]))
        out = _with_sides(vec, [0, 1])
        assert list(out.data) == [1.0, 2.0, 1.0, 0.0, 3.0, 4.0, 0.0, 1.0]
        assert vec.data[3] == 1.0  # input untouched

    def test_dry_run_json_reports_assignment(self, decap_pcb, capsys):
        from kicad_tools.cli.optimize_placement_cmd import run_optimize_placement

        assert run_optimize_placement(str(decap_pcb), dry_run=True, as_json=True) == 0
        doc = json.loads(capsys.readouterr().out)
        assert doc["scores"]["current"]["breakdown"]["decoupling"] > 0
        [entry] = doc["decoupling"]
        assert (entry["cap"], entry["pin"], entry["net"]) == ("C1", "U1.1", "+3V3")

    def test_optimize_pulls_cap_onto_its_pin(self, decap_pcb, tmp_path, capsys):
        from kicad_tools.cli.optimize_placement_cmd import run_optimize_placement

        out = tmp_path / "out.kicad_pcb"
        rc = run_optimize_placement(
            str(decap_pcb),
            output_path=str(out),
            seed_method="current",
            max_iterations=30,
            as_json=True,
            allow_infeasible=True,
        )
        assert rc == 0
        doc = json.loads(capsys.readouterr().out)
        [entry] = doc["decoupling"]
        assert entry["distance_mm"] <= 3.0
        assert (
            doc["scores"]["final"]["breakdown"]["decoupling"]
            < doc["scores"]["initial"]["breakdown"]["decoupling"]
        )


# ---------------------------------------------------------------------------
# Physics placer (``kct placement optimize --cluster``)
# ---------------------------------------------------------------------------


class TestPhysicsClusterSprings:
    def test_power_cluster_springs_spread_caps_over_supply_pins(self):
        from kicad_tools.optim.components import (
            ClusterType,
            Component,
            FunctionalCluster,
            Pin,
        )
        from kicad_tools.optim.geometry import Polygon
        from kicad_tools.optim.placement import PlacementOptimizer

        optimizer = PlacementOptimizer(Polygon.rectangle(50, 50, 100, 100))
        ic = Component(
            ref="U1",
            x=50,
            y=50,
            pins=[
                Pin(number="1", x=45, y=50, net=1, net_name="+3V3"),
                Pin(number="2", x=55, y=50, net=1, net_name="+3V3"),
                Pin(number="3", x=50, y=45, net=2, net_name="GND"),
            ],
        )
        # Both caps start nearest pin 1.
        caps = [
            Component(
                ref=ref,
                x=x,
                y=50,
                pins=[
                    Pin(number="1", x=x - 1, y=50, net=1, net_name="+3V3"),
                    Pin(number="2", x=x + 1, y=50, net=2, net_name="GND"),
                ],
            )
            for ref, x in (("C1", 40.0), ("C2", 42.0))
        ]
        for comp in (ic, *caps):
            optimizer.add_component(comp)

        optimizer.add_cluster(
            FunctionalCluster(cluster_type=ClusterType.POWER, anchor="U1", members=["C1", "C2"])
        )
        springs = {s.comp2_ref: (s.pin1_num, s.pin2_num) for s in optimizer.springs if s.net == -1}
        # Supply pad (1) of each cap to a distinct U1 supply pin; the nearer
        # cap (C2) keeps pin 1, so C1 goes to pin 2 rather than crowding it.
        assert springs == {"C2": ("1", "1"), "C1": ("2", "1")}
