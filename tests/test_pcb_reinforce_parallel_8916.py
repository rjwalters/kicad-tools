"""``kct pcb reinforce --strategy wire|parallel|auto`` (Issue #8916).

Parallel copper for a short high-current link: the leg is copied onto the
opposite outer layer, tied to it at both ends and by stitching vias, and the
existing ``path_ampacity`` parallel-group detector judges the pair on its
summed cross-section. Boards are synthetic and in memory unless a test needs
the CLI (then the helper footprints, which do not survive a save, are left
out and the leg ends are free track ends).
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import pytest

from kicad_tools.cli.commands.pcb import run_pcb_command
from kicad_tools.manufacturers import DesignRules
from kicad_tools.pcb.reinforce import (
    COPY_PLACED,
    COPY_REFUSED,
    LEG_COPY,
    LEG_FULL,
    LEG_PARALLEL,
    LEG_UNANCHORED,
    RULE_COPY_GEOMETRY,
    STRATEGY_PARALLEL,
    STRATEGY_WIRE,
    ReinforceError,
    clear_anchors,
    reinforce_net,
    stitch_via_geometry,
)
from kicad_tools.pcb.reinforcement_sidecar import build_wire_records, load_wires, sidecar_path
from kicad_tools.router.current_paths import (
    CurrentPathSpec,
    PathEndpoint,
    audit_current_paths,
    resolve_current_path,
)
from kicad_tools.schema.pcb import PCB, Footprint, Pad
from kicad_tools.validate.rules.path_ampacity import PathAmpacityRule
from kicad_tools.validate.rules.reinforcement_incomplete import ReinforcementIncompleteRule
from tests.test_pcb_reinforce import _add_th_pad_footprint

NET = "PWR"


def _rules() -> DesignRules:
    return DesignRules(
        min_trace_width_mm=0.127,
        min_clearance_mm=0.127,
        min_via_drill_mm=0.3,
        min_via_diameter_mm=0.6,
        min_annular_ring_mm=0.13,
        min_hole_to_hole_mm=0.5,
        outer_copper_oz=2.0,
        inner_copper_oz=2.0,
    )


def _spec(current_a: float = 15.0, name: str = "FORCE", sink: str = "J2") -> CurrentPathSpec:
    return CurrentPathSpec(
        name=name,
        net_name=NET,
        source=PathEndpoint("J1", "1"),
        sink=PathEndpoint(sink, "1"),
        continuous_a=current_a,
        reinforcement_eligible=True,
    )


def _two_pad_leg(length: float = 12.0, width: float = 3.5, layer: str = "B.Cu") -> PCB:
    """A ``length`` mm leg on ``layer`` between two through-hole pads."""
    pcb = PCB.create(width=200, height=100, center=False)
    _add_th_pad_footprint(pcb, ref="J1", x=20.0, y=50.0, net=NET, drill=1.0, size=2.0)
    _add_th_pad_footprint(pcb, ref="J2", x=20.0 + length, y=50.0, net=NET, drill=1.0, size=2.0)
    pcb.add_trace((20.0, 50.0), (20.0 + length, 50.0), width=width, layer=layer, net=NET)
    return pcb


def _on(pcb: PCB, layer: str) -> list:
    return [s for s in pcb.segments if s.net_name == NET and s.layer == layer]


def _stitches(pcb: PCB) -> list:
    size, drill = stitch_via_geometry(_rules())
    return [v for v in pcb.vias if v.net_name == NET and abs(v.drill - drill) < 1e-6]


# --------------------------------------------------------------------------
# AC 1 / AC 2: the copy is a parallel group path_ampacity credits
# --------------------------------------------------------------------------


class TestParallelCopy:
    def test_two_pad_leg_gets_a_vertex_for_vertex_copy(self):
        pcb = _two_pad_leg()
        result = reinforce_net(pcb, NET, strategy="parallel", design_rules=_rules())
        (leg,) = result.legs
        assert leg.strategy == STRATEGY_PARALLEL
        assert leg.status == LEG_PARALLEL and leg.copy_status == COPY_PLACED
        assert leg.anchors == [] and result.placed == [] and result.openings == []
        (copy,) = _on(pcb, "F.Cu")
        assert {copy.start, copy.end} == {(20.0, 50.0), (32.0, 50.0)}
        assert copy.width == pytest.approx(3.5)
        # Both ends land on through-hole pads, so only interior stitching vias.
        vias = _stitches(pcb)
        assert len(vias) == 2
        assert all(abs(v.position[1] - 50.0) < 1e-9 and 20.0 < v.position[0] < 32.0 for v in vias)
        assert leg.copy is not None and len(leg.copy.stitch_vias) == 2

    def test_audit_reports_the_doubled_path_resolved(self):
        pcb = _two_pad_leg()
        reinforce_net(pcb, NET, strategy="parallel", design_rules=_rules())
        audit = audit_current_paths(pcb, [_spec()])
        (res,) = audit.resolutions
        assert res.ok, res.reason
        assert res.parallel_groups
        messages = [v.message for v in PathAmpacityRule([_spec()]).check(pcb, _rules()).violations]
        assert not any("ambiguous" in m for m in messages)

    def test_path_ampacity_fails_before_and_passes_after(self):
        # 15 A needs ~6.3 mm of 2 oz outer copper: one 3.5 mm trace fails,
        # two coincident 3.5 mm traces (summed cross-section) pass.
        pcb = _two_pad_leg()
        rule = PathAmpacityRule([_spec(15.0)])
        before = rule.check(pcb, _rules()).errors
        assert any("too narrow" in v.message for v in before)
        reinforce_net(pcb, NET, strategy="parallel", design_rules=_rules())
        assert rule.check(pcb, _rules()).errors == []

    def test_interior_stitching_vias_are_not_layer_changes(self):
        """A stitching via inside the doubled run carries no series current."""
        pcb = _two_pad_leg()
        reinforce_net(pcb, NET, strategy="parallel", design_rules=_rules())
        res = resolve_current_path(pcb, _spec())
        assert len(res.layer_changes) == 2  # still listed by the audit
        assert res.series_layer_changes == ()

    def test_copy_on_the_front_leg_goes_to_the_back(self):
        pcb = _two_pad_leg(layer="F.Cu")
        reinforce_net(pcb, NET, strategy="parallel", design_rules=_rules())
        assert len(_on(pcb, "B.Cu")) == 1

    def test_vertical_bent_leg_copy_keeps_every_vertex(self):
        """A leg along y with a 90 degree bend: the copy follows it exactly."""
        pcb = PCB.create(width=200, height=200, center=False)
        _add_th_pad_footprint(pcb, ref="J1", x=50.0, y=20.0, net=NET, drill=1.0, size=2.0)
        _add_th_pad_footprint(pcb, ref="J2", x=60.0, y=30.0, net=NET, drill=1.0, size=2.0)
        pcb.add_trace((50.0, 20.0), (50.0, 30.0), width=2.0, layer="B.Cu", net=NET)
        pcb.add_trace((50.0, 30.0), (60.0, 30.0), width=2.0, layer="B.Cu", net=NET)
        reinforce_net(pcb, NET, strategy="parallel", design_rules=_rules())
        front = sorted((s.start, s.end) for s in _on(pcb, "F.Cu"))
        back = sorted((s.start, s.end) for s in _on(pcb, "B.Cu"))
        assert front == back
        res = resolve_current_path(pcb, _spec(5.0))
        assert res.ok and res.parallel_groups

    def test_dry_run_writes_nothing(self):
        pcb = _two_pad_leg()
        n_seg, n_via = len(pcb.segments), len(pcb.vias)
        result = reinforce_net(pcb, NET, strategy="parallel", design_rules=_rules(), dry_run=True)
        assert result.copies and (len(pcb.segments), len(pcb.vias)) == (n_seg, n_via)


# --------------------------------------------------------------------------
# Ends that are not through-hole pads: tie vias
# --------------------------------------------------------------------------


def _smd_pad(pcb: PCB, ref: str, x: float, y: float, *, rotation: float = 0.0) -> None:
    net = pcb.add_net(NET)
    pad = Pad(
        number="1",
        type="smd",
        shape="rect",
        position=(0.0, 0.0),
        size=(1.0, 3.0),
        layers=["B.Cu", "B.Paste", "B.Mask"],
        net_number=net.number,
        net_name=NET,
        rotation=rotation,
    )
    pcb._footprints.append(
        Footprint(
            name="SMD:Pad",
            layer="B.Cu",
            position=(x, y),
            rotation=rotation,
            reference=ref,
            value="P",
            pads=[pad],
        )
    )


class TestTieVias:
    @pytest.mark.parametrize("rotation", [0.0, 90.0, 270.0])
    def test_smd_end_gets_a_tie_via_clear_of_the_pad(self, rotation):
        """The copy stops at a tie via just past each SMD pad, never in it.

        At 0 degrees the 1 x 3 mm pad's 0.5 mm half-width runs along the leg;
        at 90 / 270 its 1.5 mm half-length does, so the tie moves further in.
        """
        pcb = PCB.create(width=200, height=100, center=False)
        _smd_pad(pcb, "J1", 20.0, 50.0, rotation=rotation)
        _smd_pad(pcb, "J2", 40.0, 50.0, rotation=rotation)
        pcb.add_trace((20.0, 50.0), (40.0, 50.0), width=1.0, layer="B.Cu", net=NET)
        result = reinforce_net(pcb, NET, strategy="parallel", design_rules=_rules())
        (leg,) = result.legs
        assert leg.status == LEG_PARALLEL, leg.reason
        size, _ = stitch_via_geometry(_rules())
        half_along = 0.5 if rotation == 0.0 else 1.5
        ties = sorted(v.position[0] for v in _stitches(pcb))
        # Pad edge to via edge at least the copper clearance, at both ends.
        assert ties[0] - size / 2.0 >= 20.0 + half_along + _rules().min_clearance_mm - 1e-6
        assert ties[-1] + size / 2.0 <= 40.0 - half_along - _rules().min_clearance_mm + 1e-6
        (copy,) = _on(pcb, "F.Cu")
        assert sorted([copy.start[0], copy.end[0]]) == pytest.approx([ties[0], ties[-1]])

    def test_free_track_ends_are_tied_at_the_vertex(self):
        pcb = PCB.create(width=200, height=100, center=False)
        pcb.add_trace((20.0, 50.0), (32.0, 50.0), width=2.0, layer="F.Cu", net=NET)
        reinforce_net(pcb, NET, strategy="parallel", design_rules=_rules())
        xs = sorted(v.position[0] for v in _stitches(pcb))
        assert xs[0] == pytest.approx(20.0) and xs[-1] == pytest.approx(32.0)
        (copy,) = _on(pcb, "B.Cu")
        assert {copy.start, copy.end} == {(20.0, 50.0), (32.0, 50.0)}

    def test_stitch_pitch_respects_nominal_and_floor(self):
        pcb = _two_pad_leg(length=30.0, width=2.0)
        reinforce_net(pcb, NET, strategy="parallel", design_rules=_rules())
        xs = sorted(v.position[0] for v in _stitches(pcb))
        gaps = [b - a for a, b in zip([20.0, *xs], [*xs, 50.0], strict=True)]
        assert all(g <= 5.0 + 1e-6 for g in gaps), gaps
        assert len(xs) == 5


# --------------------------------------------------------------------------
# AC 3: auto chooses by the minimum wire length
# --------------------------------------------------------------------------


class TestAuto:
    @staticmethod
    def _two_legs() -> PCB:
        pcb = PCB.create(width=200, height=100, center=False)
        _add_th_pad_footprint(pcb, ref="J1", x=20.0, y=30.0, net=NET, drill=1.0, size=2.0)
        _add_th_pad_footprint(pcb, ref="J2", x=32.0, y=30.0, net=NET, drill=1.0, size=2.0)
        _add_th_pad_footprint(pcb, ref="J3", x=20.0, y=70.0, net=NET, drill=1.0, size=2.0)
        _add_th_pad_footprint(pcb, ref="J4", x=50.0, y=70.0, net=NET, drill=1.0, size=2.0)
        pcb.add_trace((20.0, 30.0), (32.0, 30.0), width=2.0, layer="F.Cu", net=NET)
        pcb.add_trace((20.0, 70.0), (50.0, 70.0), width=2.0, layer="F.Cu", net=NET)
        return pcb

    def test_short_leg_gets_a_copy_long_leg_gets_a_wire(self):
        pcb = self._two_legs()
        result = reinforce_net(pcb, NET, strategy="auto", all_runs=True, design_rules=_rules())
        by_len = {round(leg.length_mm): leg for leg in result.legs}
        short, long_ = by_len[12], by_len[30]
        assert short.strategy == STRATEGY_PARALLEL and short.status == LEG_PARALLEL
        assert short.anchors == [] and short.mask_status == ""
        assert long_.strategy == STRATEGY_WIRE and long_.status == LEG_FULL
        assert len(long_.anchors) == 2 and long_.mask_status == "opened"
        assert all(abs(a.y - 70.0) < 1e-9 for a in result.placed)
        back = _on(pcb, "B.Cu")
        assert len(back) == 1 and back[0].start[1] == 30.0

    def test_explicit_zero_minimum_makes_every_leg_a_wire(self):
        result = reinforce_net(
            self._two_legs(),
            NET,
            strategy="auto",
            all_runs=True,
            min_wire_length_mm=0.0,
            design_rules=_rules(),
        )
        assert {leg.strategy for leg in result.legs} == {STRATEGY_WIRE}
        assert result.copies == []

    def test_raised_minimum_moves_the_long_leg_to_parallel(self):
        result = reinforce_net(
            self._two_legs(),
            NET,
            strategy="auto",
            all_runs=True,
            min_wire_length_mm=40.0,
            design_rules=_rules(),
        )
        assert {leg.strategy for leg in result.legs} == {STRATEGY_PARALLEL}

    @pytest.mark.parametrize("strategy", ["auto", "parallel"])
    def test_spaced_mode_refuses_non_wire_strategies(self, strategy):
        """Stated rule: with ``--anchor-mode spaced`` (no legs, and no default
        minimum from resolve_min_wire_length_mm) auto/parallel is an error."""
        with pytest.raises(ReinforceError, match="anchor_mode 'ends'"):
            reinforce_net(self._two_legs(), NET, strategy=strategy, anchor_mode="spaced")

    def test_spaced_mode_auto_error_names_the_missing_threshold(self):
        with pytest.raises(ReinforceError, match="no default minimum wire length"):
            reinforce_net(self._two_legs(), NET, strategy="auto", anchor_mode="spaced")

    def test_unknown_strategy_is_an_error(self):
        with pytest.raises(ReinforceError, match="strategy must be one of"):
            reinforce_net(self._two_legs(), NET, strategy="triple")


class TestWireDefaultUnchanged:
    def test_default_equals_explicit_wire(self):
        a, b = _two_pad_leg(length=40.0), _two_pad_leg(length=40.0)
        ra = reinforce_net(a, NET, design_rules=_rules())
        rb = reinforce_net(b, NET, strategy="wire", design_rules=_rules())
        assert ra.strategy == rb.strategy == STRATEGY_WIRE
        assert [(p.x, p.y) for p in ra.placed] == [(p.x, p.y) for p in rb.placed]
        assert len(a.segments) == len(b.segments) and len(a.vias) == len(b.vias)
        assert ra.copies == [] and _on(a, "F.Cu") == []

    def test_wire_records_carry_no_strategy_key(self):
        result = reinforce_net(_two_pad_leg(length=40.0), NET, design_rules=_rules())
        (record,) = build_wire_records(result, 0.0, 0.0)
        assert "strategy" not in record


# --------------------------------------------------------------------------
# AC 4: a copy that cannot be placed whole is refused, nothing written
# --------------------------------------------------------------------------


class TestRefusal:
    def test_other_net_track_under_the_leg_refuses_the_copy(self):
        pcb = _two_pad_leg()
        pcb.add_trace((26.0, 40.0), (26.0, 60.0), width=0.5, layer="F.Cu", net="SIG")
        n_seg, n_via = len(pcb.segments), len(pcb.vias)
        result = reinforce_net(pcb, NET, strategy="parallel", design_rules=_rules())
        (leg,) = result.legs
        assert leg.status == LEG_UNANCHORED and leg.copy_status == COPY_REFUSED
        (refusal,) = result.refused_copies
        assert refusal.obstacle_net == "SIG"
        assert refusal.rule == "dru_class_floor"
        assert "SIG" in leg.reason and "dru_class_floor" in leg.reason
        assert (len(pcb.segments), len(pcb.vias)) == (n_seg, n_via)
        assert result.copies == []

    def test_other_net_copper_on_the_leg_s_own_layer_does_not_block(self):
        """Only the copy's layer (and through holes) matter to its track."""
        pcb = _two_pad_leg()
        pcb.add_trace((26.0, 52.2), (26.0, 60.0), width=0.5, layer="B.Cu", net="SIG")
        result = reinforce_net(pcb, NET, strategy="parallel", design_rules=_rules())
        assert result.legs[0].status == LEG_PARALLEL, result.legs[0].reason

    def test_voltage_map_requirement_refuses_a_copy_the_floor_allows(self):
        from kicad_tools.router.pairwise_clearance import PairwiseClearanceTable

        class _Table(PairwiseClearanceTable):
            def __init__(self):
                pass

            def required_clearance(self, a, b):
                return 3.0 if {a, b} == {NET, "HV"} else 0.0

        pcb = _two_pad_leg()
        pcb.add_trace((20.0, 53.5), (32.0, 53.5), width=0.5, layer="F.Cu", net="HV")
        assert reinforce_net(_two_pad_leg(), NET, strategy="parallel", design_rules=_rules()).copies
        result = reinforce_net(
            pcb, NET, strategy="parallel", design_rules=_rules(), pairwise_clearance=_Table()
        )
        (refusal,) = result.refused_copies
        assert refusal.rule == "voltage_map_pairwise" and refusal.obstacle_net == "HV"
        assert _on(pcb, "F.Cu") == []

    def test_other_net_via_blocking_every_stitch_position_refuses_whole(self):
        pcb = _two_pad_leg(length=12.0, width=0.5)
        # Foreign vias right on the run at each candidate window block the
        # interior stitching vias; the track would also be refused.
        for x in (24.0, 28.0):
            pcb.add_via(x, 50.0, size=0.6, drill=0.3, net="SIG")
        n_seg, n_via = len(pcb.segments), len(pcb.vias)
        result = reinforce_net(pcb, NET, strategy="parallel", design_rules=_rules())
        assert result.refused_copies and not result.copies
        assert (len(pcb.segments), len(pcb.vias)) == (n_seg, n_via)

    def test_inner_layer_leg_is_refused(self):
        pcb = PCB.create(width=200, height=100, layers=4, center=False)
        pcb.add_trace((20.0, 50.0), (32.0, 50.0), width=1.0, layer="In1.Cu", net=NET)
        result = reinforce_net(pcb, NET, strategy="parallel", design_rules=_rules())
        (refusal,) = result.refused_copies
        assert refusal.rule == RULE_COPY_GEOMETRY and "inner layer" in refusal.reason


# --------------------------------------------------------------------------
# AC 6 / AC 7: sidecar record and reinforcement_incomplete
# --------------------------------------------------------------------------


class TestSidecarAndCompleteness:
    @staticmethod
    def _reinforced(strategy: str, length: float = 12.0):
        pcb = _two_pad_leg(length=length)
        result = reinforce_net(
            pcb, NET, strategy=strategy, current_paths=[_spec()], design_rules=_rules()
        )
        return pcb, result

    def test_short_parallel_leg_earns_a_record(self):
        """#8915 drops sub-minimum legs from the wire list; a copied one stays."""
        _, result = self._reinforced("auto")
        (record,) = build_wire_records(result, 0.0, 0.0)
        assert record["strategy"] == "parallel"
        assert record["copy_layer"] == "F.Cu" and record["copy_status"] == "placed"
        assert len(record["copy_segments"]) == 1 and len(record["stitch_vias"]) == 2
        assert record["anchors"] == []

    def test_parallel_leg_needs_no_wire(self):
        pcb, result = self._reinforced("auto")
        wires = build_wire_records(result, 0.0, 0.0)
        rule = ReinforcementIncompleteRule(specs=[_spec()], wires=wires)
        assert rule.check(pcb, _rules()).violations == []

    def test_without_the_record_the_same_copper_is_still_flagged(self):
        pcb, _ = self._reinforced("auto")
        kinds = {
            v.items[1].split("@")[0]
            for v in ReinforcementIncompleteRule(specs=[_spec()]).check(pcb, _rules()).violations
        }
        assert kinds == {"a", "b"}

    def test_record_whose_copy_was_deleted_is_not_trusted(self):
        pcb, result = self._reinforced("auto")
        wires = build_wire_records(result, 0.0, 0.0)
        pcb.remove_segments(_on(pcb, "F.Cu"))
        rule = ReinforcementIncompleteRule(specs=[_spec()], wires=wires)
        kinds = {v.items[1].split("@")[0] for v in rule.check(pcb, _rules()).violations}
        assert {"a", "b"} <= kinds

    def test_record_whose_original_was_deleted_is_not_trusted(self):
        """A lone copy is a plain leg again, not one half of a doubled one."""
        pcb, result = self._reinforced("auto")
        wires = build_wire_records(result, 0.0, 0.0)
        pcb.remove_segments(_on(pcb, "B.Cu"))
        rule = ReinforcementIncompleteRule(specs=[_spec()], wires=wires)
        kinds = {v.items[1].split("@")[0] for v in rule.check(pcb, _rules()).violations}
        assert {"a", "b"} <= kinds

    def test_wire_leg_still_needs_its_wire(self):
        pcb = _two_pad_leg(length=40.0)
        # A parallel record for a different leg of the net is no excuse.
        _, other = self._reinforced("auto")
        wires = build_wire_records(other, 0.0, 0.0)
        rule = ReinforcementIncompleteRule(specs=[_spec()], wires=wires)
        kinds = {v.items[1].split("@")[0] for v in rule.check(pcb, _rules()).violations}
        assert {"a", "b"} <= kinds

    def test_refused_copy_is_not_reinforcement(self):
        pcb = _two_pad_leg(length=40.0)
        pcb.add_trace((26.0, 40.0), (26.0, 60.0), width=0.5, layer="F.Cu", net="SIG")
        result = reinforce_net(
            pcb, NET, strategy="parallel", current_paths=[_spec()], design_rules=_rules()
        )
        (record,) = build_wire_records(result, 0.0, 0.0)
        assert record["strategy"] == "parallel" and record["copy_status"] == "refused"
        rule = ReinforcementIncompleteRule(specs=[_spec()], wires=[record])
        assert rule.check(pcb, _rules()).violations


# --------------------------------------------------------------------------
# Idempotency
# --------------------------------------------------------------------------


class TestIdempotent:
    @pytest.mark.parametrize("gated", [False, True])
    def test_rerun_adds_no_copper_and_no_second_record(self, gated):
        pcb = _two_pad_leg()
        kw = {"current_paths": [_spec()]} if gated else {}
        first = reinforce_net(pcb, NET, strategy="parallel", design_rules=_rules(), **kw)
        n_seg, n_via = len(pcb.segments), len(pcb.vias)
        # The re-run knows its own copper from the record, never from its size.
        records = build_wire_records(first, 0, 0)
        second = reinforce_net(
            pcb, NET, strategy="parallel", design_rules=_rules(), sidecar_wires=records, **kw
        )
        assert (len(pcb.segments), len(pcb.vias)) == (n_seg, n_via)
        assert [leg.status for leg in second.legs if leg.status != "unhandled"][0] == LEG_PARALLEL
        if gated:
            assert sorted(leg.status for leg in second.legs) == sorted([LEG_PARALLEL, LEG_COPY])
        assert build_wire_records(first, 0, 0) == build_wire_records(second, 0, 0)


# --------------------------------------------------------------------------
# Ownership is provenance (the record), never size or shape (PR #8945 review)
# --------------------------------------------------------------------------


def _at(pcb: PCB, x: float, y: float) -> bool:
    return any(math.dist(v.position, (x, y)) < 1e-6 for v in pcb.vias)


def _leg_to_router_via(*, redundant: bool) -> PCB:
    """A B.Cu leg from TH pad J1 to a pre-existing router via of the stitch size.

    The via carries the net on to J2 on F.Cu. With ``redundant`` a thin B.Cu
    detour also reaches J2's side, so the via is not the net's only layer link.
    """
    size, drill = stitch_via_geometry(_rules())
    pcb = PCB.create(width=200, height=100, center=False)
    _add_th_pad_footprint(pcb, ref="J1", x=20.0, y=50.0, net=NET, drill=1.0, size=2.0)
    _add_th_pad_footprint(pcb, ref="J2", x=32.0, y=70.0, net=NET, drill=1.0, size=2.0)
    pcb.add_trace((20.0, 50.0), (32.0, 50.0), width=2.0, layer="B.Cu", net=NET)
    pcb.add_via(32.0, 50.0, size=size, drill=drill, layers=("F.Cu", "B.Cu"), net=NET)
    pcb.add_trace((32.0, 50.0), (32.0, 70.0), width=2.0, layer="F.Cu", net=NET)
    if redundant:
        pcb.add_trace((20.0, 50.0), (20.0, 70.0), width=0.5, layer="F.Cu", net=NET)
        pcb.add_trace((20.0, 70.0), (32.0, 70.0), width=0.5, layer="F.Cu", net=NET)
    return pcb


class TestPreExistingCopper:
    @pytest.mark.parametrize("redundant", [False, True])
    def test_leg_end_on_a_stitch_size_router_via_is_an_existing_tie(self, redundant):
        pcb = _leg_to_router_via(redundant=redundant)
        result = reinforce_net(pcb, NET, strategy="parallel", design_rules=_rules(), layer="B.Cu")
        (copy,) = result.copies
        # Both ends are already tied by a barrel: nothing placed at (32, 50),
        # and the router via is in no record.
        assert all(math.dist(p, (32.0, 50.0)) > 1e-6 for p in copy.stitch_vias)
        (leg,) = [leg for leg in result.legs if leg.copy is copy]
        assert "a tie via" not in leg.reason
        records = build_wire_records(result, 0.0, 0.0)
        listed = [(p["x"], p["y"]) for w in records for p in w.get("stitch_vias") or []]
        assert (32.0, 50.0) not in listed

        cleared = clear_anchors(pcb, NET, sidecar_wires=records, design_rules=_rules())
        # The copy goes whole (it is never the net's only link) ...
        assert cleared.copies_refused == [] and len(cleared.copies_removed) == 1
        assert [(s.start, s.end) for s in _on(pcb, "B.Cu")] == [((20.0, 50.0), (32.0, 50.0))]
        # ... and the board's own via stays.
        assert _at(pcb, 32.0, 50.0)
        assert len(pcb.vias) == 1

    def test_clear_never_removes_a_via_a_record_does_not_name(self):
        pcb = _leg_to_router_via(redundant=True)
        before = sorted(v.position for v in pcb.vias)
        result = reinforce_net(pcb, NET, strategy="parallel", design_rules=_rules(), layer="B.Cu")
        assert result.copies and len(pcb.vias) > len(before)
        records = build_wire_records(result, 0.0, 0.0)
        clear_anchors(pcb, NET, sidecar_wires=records, design_rules=_rules())
        assert sorted(v.position for v in pcb.vias) == before

    def test_rerun_with_the_record_is_idempotent(self):
        pcb = _leg_to_router_via(redundant=False)
        first = reinforce_net(pcb, NET, strategy="parallel", design_rules=_rules(), layer="B.Cu")
        records = build_wire_records(first, 0.0, 0.0)
        n_seg, n_via = len(pcb.segments), len(pcb.vias)
        second = reinforce_net(
            pcb,
            NET,
            strategy="parallel",
            design_rules=_rules(),
            layer="B.Cu",
            sidecar_wires=records,
        )
        assert (len(pcb.segments), len(pcb.vias)) == (n_seg, n_via)

        def copy_records(recs):
            return [w for w in recs if w.get("strategy") == "parallel"]

        assert copy_records(build_wire_records(second, 0.0, 0.0)) == copy_records(records)
        (record,) = copy_records(records)
        assert record["copy_status"] == "placed" and len(record["stitch_vias"]) >= 1

    def test_pre_existing_copy_layer_track_is_neither_rewritten_nor_recorded(self):
        pcb = _two_pad_leg()
        # Someone already doubled the leg by hand on F.Cu, same width.
        pcb.add_trace((20.0, 50.0), (32.0, 50.0), width=3.5, layer="F.Cu", net=NET)
        result = reinforce_net(pcb, NET, strategy="parallel", design_rules=_rules(), layer="B.Cu")
        records = build_wire_records(result, 0.0, 0.0)
        assert all(not w.get("copy_segments") for w in records)
        clear_anchors(pcb, NET, sidecar_wires=records, design_rules=_rules())
        assert len(_on(pcb, "F.Cu")) == 1


class TestCopyRecognition:
    def test_straight_chord_across_a_bent_leg_is_not_its_copy(self):
        """Both ends of the F.Cu leg lie on the B.Cu U; its body does not."""
        pcb = PCB.create(width=200, height=100, center=False)
        for a, b in (((32.0, 50.0), (60.0, 50.0)), ((60.0, 50.0), (60.0, 70.0))):
            pcb.add_trace(a, b, width=2.0, layer="B.Cu", net=NET)
        pcb.add_trace((60.0, 70.0), (32.0, 70.0), width=2.0, layer="B.Cu", net=NET)
        pcb.add_trace((32.0, 50.0), (32.0, 70.0), width=2.0, layer="F.Cu", net=NET)
        result = reinforce_net(
            pcb,
            NET,
            strategy="parallel",
            design_rules=_rules(),
            all_runs=True,
            layer="B.Cu",
        )
        front = [leg for leg in result.legs if leg.layer == "F.Cu"]
        assert front and all(leg.status != LEG_COPY for leg in front)

    def test_true_copy_is_still_recognised(self):
        from kicad_tools.pcb.reinforce import _coincident

        bent = [(32.0, 50.0), (60.0, 50.0), (60.0, 70.0), (32.0, 70.0)]
        assert _coincident(bent, bent, 1.0)
        assert _coincident([(40.0, 50.0), (60.0, 50.0), (60.0, 60.0)], bent, 1.0)
        assert not _coincident([(32.0, 50.0), (32.0, 70.0)], bent, 1.0)


# --------------------------------------------------------------------------
# AC 5: CLI report, sidecar, --clear / --replace
# --------------------------------------------------------------------------


def _args(pcb_path: Path, **kw) -> argparse.Namespace:
    base = {
        "pcb_command": "reinforce",
        "pcb": str(pcb_path),
        "net": NET,
        "wire_gauge": 16,
        "layer": None,
        "output": None,
        "format": "text",
        "dry_run": False,
    }
    base.update(kw)
    return argparse.Namespace(**base)


@pytest.fixture
def board(tmp_path: Path) -> Path:
    p = tmp_path / "b.kicad_pcb"
    pcb = PCB.create(width=200, height=100, center=False)
    pcb.add_trace((20.0, 30.0), (32.0, 30.0), width=2.0, layer="F.Cu", net=NET)
    pcb.add_trace((20.0, 70.0), (60.0, 70.0), width=2.0, layer="F.Cu", net=NET)
    pcb.save(p)
    return p


def _back(path: Path) -> list:
    return [s for s in PCB.load(path).segments if s.layer == "B.Cu"]


class TestCli:
    def test_json_names_the_strategy_per_leg(self, board, capsys):
        args = _args(board, strategy="auto", all_runs=True, format="json")
        assert run_pcb_command(args) == 0
        doc = json.loads(capsys.readouterr().out)
        assert doc["strategy"] == "auto" and doc["copies_placed"] == 1
        strategies = sorted((round(leg["length_mm"]), leg["strategy"]) for leg in doc["legs"])
        assert strategies == [(12, "parallel"), (40, "wire")]
        short = next(leg for leg in doc["legs"] if leg["strategy"] == "parallel")
        assert short["copy"]["layer"] == "B.Cu" and short["copy_status"] == "placed"

    def test_text_names_the_strategy_per_leg(self, board, capsys):
        assert run_pcb_command(_args(board, strategy="auto", all_runs=True)) == 0
        out = capsys.readouterr().out
        assert "Strategy:       auto" in out
        assert "parallel [parallel]" in out and "2/2 anchored [full]" in out
        assert "Parallel copies: 1 placed, 0 refused" in out

    def test_sidecar_records_the_copy_and_clear_removes_it(self, board):
        assert run_pcb_command(_args(board, strategy="parallel")) == 0
        wires = load_wires(sidecar_path(board))
        (record,) = [w for w in wires if w.get("strategy") == "parallel"]
        assert record["copy_layer"] == "B.Cu"
        assert len(_back(board)) == 1
        n_vias = len(PCB.load(board).vias)
        assert n_vias == len(record["stitch_vias"]) >= 2

        assert run_pcb_command(_args(board, clear=True)) == 0
        after = PCB.load(board)
        assert [s for s in after.segments if s.layer == "B.Cu"] == []
        assert after.vias == []
        assert [w for w in load_wires(sidecar_path(board)) if w.get("strategy")] == []

    def test_replace_reinstates_without_duplicates(self, board):
        assert run_pcb_command(_args(board, strategy="parallel")) == 0
        first = board.read_bytes()
        records = load_wires(sidecar_path(board))
        assert run_pcb_command(_args(board, strategy="parallel", replace=True)) == 0
        assert len(_back(board)) == 1
        assert load_wires(sidecar_path(board)) == records
        assert board.read_bytes().count(b"(segment") == first.count(b"(segment")

    def test_rerun_is_idempotent_on_disk(self, board):
        assert run_pcb_command(_args(board, strategy="parallel")) == 0
        pcb_bytes, side_bytes = board.read_bytes(), sidecar_path(board).read_bytes()
        assert run_pcb_command(_args(board, strategy="parallel")) == 0
        assert sidecar_path(board).read_bytes() == side_bytes
        assert board.read_bytes().count(b"(segment") == pcb_bytes.count(b"(segment")
        assert board.read_bytes().count(b"(via") == pcb_bytes.count(b"(via")

    def test_spaced_auto_exits_with_an_error(self, board, capsys):
        assert run_pcb_command(_args(board, strategy="auto", anchor_mode="spaced")) == 1
        assert "anchor_mode 'ends'" in capsys.readouterr().err

    def test_help_documents_the_spaced_rule(self):
        from kicad_tools.cli.parser import create_parser

        parser = create_parser()
        sub = next(a for a in parser._actions if getattr(a, "choices", None) and "pcb" in a.choices)
        pcb_sub = sub.choices["pcb"]
        reinforce = next(
            a for a in pcb_sub._actions if getattr(a, "choices", None) and "reinforce" in a.choices
        ).choices["reinforce"]
        action = next(a for a in reinforce._actions if "--strategy" in a.option_strings)
        assert action.default == "wire" and set(action.choices) == {"wire", "parallel", "auto"}
        assert "--anchor-mode spaced" in action.help and "error" in action.help


def test_clear_anchors_api_removes_only_recorded_copies():
    """``clear_anchors`` matches recorded copper only, never shape alone."""
    pcb = _two_pad_leg()
    result = reinforce_net(pcb, NET, strategy="parallel", design_rules=_rules())
    records = build_wire_records(result, 0.0, 0.0)
    # Hand-drawn doubling elsewhere on the net is not in the record.
    pcb.add_trace((20.0, 80.0), (32.0, 80.0), width=1.0, layer="F.Cu", net=NET)
    cleared = clear_anchors(pcb, NET, sidecar_wires=records)
    assert [c.segments for c in cleared.copies_removed] == [1]
    assert [c.vias for c in cleared.copies_removed] == [2]
    assert [(s.start, s.end) for s in _on(pcb, "F.Cu")] == [((20.0, 80.0), (32.0, 80.0))]
    assert math.isclose(len(_stitches(pcb)), 0)
