"""Tests for the ``reinforcement_incomplete`` DRC rule (Issue #8746).

Fixtures are built with the real ``reinforce_net`` (slices 1-3), so the rule is
checked against what the placer actually writes, then mutated one sub-check at
a time.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from kicad_tools.manufacturers import DesignRules
from kicad_tools.pcb.reinforce import reinforce_net
from kicad_tools.router.current_paths import CurrentPathSpec, PathEndpoint
from kicad_tools.schema.pcb import PCB
from kicad_tools.validate.checker import DRCChecker
from kicad_tools.validate.rules.reinforcement_incomplete import (
    ReinforcementIncompleteRule,
    reinforcement_waived_nets,
)
from kicad_tools.validate.rules.waivers import Waiver
from tests.test_pcb_reinforce import _add_th_pad_footprint, _straight_run_pcb

NET = "FUSED_LINE"


def _rules() -> DesignRules:
    return DesignRules(
        min_trace_width_mm=0.127,
        min_clearance_mm=0.127,
        min_via_drill_mm=0.2,
        min_via_diameter_mm=0.45,
        min_annular_ring_mm=0.13,
    )


def _spec(eligible: bool = True) -> CurrentPathSpec:
    return CurrentPathSpec(
        name="FORCE",
        net_name=NET,
        source=PathEndpoint("J1", "1"),
        sink=PathEndpoint("J2", "1"),
        continuous_a=10.0,
        reinforcement_eligible=eligible,
    )


def _board(length: float = 100.0) -> PCB:
    pcb = _straight_run_pcb(length=length)
    _add_th_pad_footprint(pcb, ref="J1", x=20.0, y=50.0, net=NET, drill=1.0, size=2.0)
    _add_th_pad_footprint(pcb, ref="J2", x=20.0 + length, y=50.0, net=NET, drill=1.0, size=2.0)
    return pcb


def _run(pcb: PCB, *, waived=(), eligible: bool = True, specs=None):
    rule = ReinforcementIncompleteRule(
        specs=[_spec(eligible)] if specs is None else specs, waived_nets=waived
    )
    return rule.check(pcb, _rules())


def _subchecks(results) -> list[str]:
    return sorted(v.items[1].split("@")[0] for v in results.violations)


def _reinforced(**kw) -> PCB:
    pcb = _board()
    reinforce_net(pcb, NET, **kw)
    return pcb


def _anchors(pcb: PCB):
    return sorted((v for v in pcb.vias if v.net_name == NET), key=lambda v: v.position[0])


class TestCompleteBoard:
    def test_ends_mode_yields_zero_findings(self):
        assert _subchecks(_run(_reinforced())) == []

    def test_ends_mode_after_save_load(self, tmp_path: Path):
        pcb = _reinforced()
        path = tmp_path / "b.kicad_pcb"
        pcb.save(path)
        # The helper footprints do not round-trip, so the declared path no
        # longer resolves: that is reported, not silently skipped (#8797).
        assert _subchecks(_run(PCB.load(path))) == ["skipped"]

    def test_back_layer_leg_is_complete(self):
        pcb = PCB.create(width=200, height=100, center=False)
        pcb.add_trace((20, 50), (120, 50), width=2.0, layer="B.Cu", net=NET)
        _add_th_pad_footprint(pcb, ref="J1", x=20.0, y=50.0, net=NET, drill=1.0, size=2.0)
        _add_th_pad_footprint(pcb, ref="J2", x=120.0, y=50.0, net=NET, drill=1.0, size=2.0)
        reinforce_net(pcb, NET)
        assert _subchecks(_run(pcb)) == []


class TestInactive:
    def test_no_specs_is_a_noop(self):
        assert _run(_board(), specs=[]).violations == []

    def test_ineligible_path_is_a_noop(self):
        assert _run(_board(), eligible=False).violations == []

    def test_unreinforced_eligible_path_warns_without_waiver(self):
        results = _run(_board())
        assert _subchecks(results) == ["a", "b"]
        assert {v.severity for v in results.violations} == {"warning"}

    def test_waiver_citing_reinforcement_makes_errors(self):
        results = _run(_board(), waived={NET})
        assert {v.severity for v in results.violations} == {"error"}


class TestSpacedMode:
    def test_spaced_fails_anchors_mask_and_stray(self):
        pcb = _board()
        reinforce_net(pcb, NET, anchor_mode="spaced", spacing_mm=15.0)
        results = _run(pcb)
        kinds = _subchecks(results)
        assert "a" in kinds and "b" in kinds and "stray" in kinds


class TestSingleMutations:
    def test_removing_an_end_anchor_fires_only_anchor_check(self):
        pcb = _reinforced()
        first = _anchors(pcb)[0]
        pcb.vias.remove(first)
        results = _run(pcb)
        assert _subchecks(results) == ["a"]
        assert "found 1 anchor" in results.violations[0].message
        assert NET in results.violations[0].message and "F.Cu" in results.violations[0].message

    def test_extra_mid_run_anchor_is_count_and_stray(self):
        pcb = _reinforced()
        a = _anchors(pcb)[0]
        pcb.add_via(
            70.0,
            50.0,
            size=a.size,
            drill=a.drill,
            layers=("F.Cu", "B.Cu"),
            net=NET,
            tenting=("no", "no"),
        )
        assert _subchecks(_run(pcb)) == ["a", "stray"]

    def test_removing_mask_graphic_fires_only_mask_check(self):
        pcb = _reinforced()
        pcb.graphic_lines[:] = [g for g in pcb.graphic_lines if g.layer != "F.Mask"]
        assert _subchecks(_run(pcb)) == ["b"]

    def test_gap_in_mask_opening_fires(self):
        pcb = _reinforced()
        (line,) = [g for g in pcb.graphic_lines if g.layer == "F.Mask"]
        a, b = sorted([line.start[0], line.end[0]])
        mid = (a + b) / 2
        pcb.graphic_lines[:] = [g for g in pcb.graphic_lines if g is not line]
        from kicad_tools.schema.pcb import GraphicLine

        y = line.start[1]
        pcb.graphic_lines.append(GraphicLine((a, y), (mid - 1, y), "F.Mask", 2.0))
        pcb.graphic_lines.append(GraphicLine((mid + 1, y), (b, y), "F.Mask", 2.0))
        results = _run(pcb)
        assert _subchecks(results) == ["b"]

    def test_split_but_touching_mask_pieces_are_continuous(self):
        pcb = _reinforced()
        (line,) = [g for g in pcb.graphic_lines if g.layer == "F.Mask"]
        a, b = sorted([line.start[0], line.end[0]])
        mid = (a + b) / 2
        pcb.graphic_lines[:] = [g for g in pcb.graphic_lines if g is not line]
        from kicad_tools.schema.pcb import GraphicLine

        y = line.start[1]
        pcb.graphic_lines.append(GraphicLine((a, y), (mid, y), "F.Mask", 2.0))
        pcb.graphic_lines.append(GraphicLine((mid, y), (b, y), "F.Mask", 2.0))
        assert _subchecks(_run(pcb)) == []

    def test_tented_anchor_fires_only_ring_check(self):
        pcb = _reinforced()
        _anchors(pcb)[1].tenting_back = "yes"
        results = _run(pcb)
        assert _subchecks(results) == ["c"]
        assert "back" in results.violations[0].message

    def test_small_via_layer_change_fires_only_d(self):
        pcb = PCB.create(width=200, height=120, center=False)
        _add_th_pad_footprint(pcb, ref="J1", x=20, y=50, net=NET, drill=1.0, size=2.0)
        _add_th_pad_footprint(pcb, ref="J2", x=120, y=50, net=NET, drill=1.0, size=2.0)
        pcb.add_trace((20, 50), (70, 50), width=3.0, layer="F.Cu", net=NET)
        pcb.add_via(70.0, 50.0, size=0.8, drill=0.4, layers=("F.Cu", "B.Cu"), net=NET)
        pcb.add_trace((70, 50), (120, 50), width=3.0, layer="B.Cu", net=NET)
        reinforce_net(pcb, NET, current_paths=[_spec()])
        results = _run(pcb)
        assert "d" in _subchecks(results)
        d = [v for v in results.violations if v.items[1].startswith("d@")]
        assert len(d) == 1 and "0.400" in d[0].message
        # The layer-change via is not mistaken for an anchor.
        assert "stray" not in _subchecks(results)

    def test_findings_carry_location_and_layer(self):
        pcb = _reinforced()
        pcb.vias.remove(_anchors(pcb)[0])
        (v,) = _run(pcb).violations
        assert v.location is not None and v.layer == "F.Cu" and v.nets == (NET,)


class TestWaiverMatching:
    @staticmethod
    def _w(rule: str, reason: str, nets=(NET,), key=None) -> Waiver:
        return Waiver(
            rule=rule,
            items=frozenset(),
            nets=frozenset(nets),
            reason=reason,
            issue="x#1",
            key=key,
        )

    @pytest.mark.parametrize("reason", ["wire carries current", "Buttress", "REINFORCED run"])
    def test_matching_reasons(self, reason):
        assert reinforcement_waived_nets([self._w("path_ampacity", reason)]) == {NET}

    def test_non_matching_reason_or_rule(self):
        assert not reinforcement_waived_nets([self._w("ampacity", "by design")])
        assert not reinforcement_waived_nets([self._w("clearance", "wire")])

    def test_keyed_entry_net_extracted(self):
        w = self._w("ampacity", "wire", nets=(), key="ampacity|a|VBUS,GND|F.Cu")
        assert reinforcement_waived_nets([w]) == {"VBUS", "GND"}


class TestRegistration:
    def test_checker_method_and_category(self):
        from kicad_tools.cli.check_categories import CHECK_CATEGORIES
        from kicad_tools.validate import DRCChecker

        assert "reinforcement_incomplete" in CHECK_CATEGORIES
        assert "check_reinforcement_incomplete" in DRCChecker.CHECK_ALL_METHODS
        checker = DRCChecker(_reinforced(), current_path_specs=[_spec()])
        assert checker.check_reinforcement_incomplete().violations == []
        bare = DRCChecker(_board())
        assert bare.check_reinforcement_incomplete().violations == []


class TestSlidAnchorsAndMaskCircles:
    """Issue #8797."""

    def _slid(self):
        pcb = _board()
        dy = 0.3 + 1.0
        pcb.add_trace((15, 50 + dy), (30, 50 + dy), width=0.2, layer="F.Cu", net="OTHER")
        reinforce_net(pcb, NET, net_class_clearances={"OTHER": 0.4})
        return pcb

    def test_slid_anchor_yields_no_finding(self):
        pcb = self._slid()
        assert min(v.position[0] for v in _anchors(pcb)) > 30.0
        assert _subchecks(_run(pcb)) == []

    def test_slid_anchor_beyond_window_is_single_finding(self):
        pcb = self._slid()
        rule = ReinforcementIncompleteRule(specs=[_spec(True)], max_end_shift_mm=1.0)
        results = rule.check(pcb, _rules())
        assert _subchecks(results) == ["a"]

    def test_tented_via_with_mask_circles_is_not_reported(self):
        from kicad_tools.schema.pcb import BoardGraphic

        pcb = _reinforced()
        for v in _anchors(pcb):
            v.tenting_front = v.tenting_back = "yes"
        assert "c" in _subchecks(_run(pcb))
        for v in _anchors(pcb):
            for layer in ("F.Mask", "B.Mask"):
                pcb.graphics.append(
                    BoardGraphic(
                        graphic_type="circle",
                        layer=layer,
                        stroke_width=0.0,
                        center=v.position,
                        end=(v.position[0] + v.size / 2.0, v.position[1]),
                        fill="yes",
                    )
                )
        assert "c" not in _subchecks(_run(pcb))

    def test_mid_leg_layer_change_via_is_not_a_third_anchor(self):
        pcb = _reinforced()
        a = _anchors(pcb)[0]
        pcb.add_via(
            70.0,
            50.0,
            size=a.size,
            drill=a.drill,
            layers=("F.Cu", "B.Cu"),
            net=NET,
            tenting=("no", "no"),
        )
        pcb.add_trace((70.0, 50.0), (70.0, 80.0), width=0.5, layer="B.Cu", net=NET)
        assert _subchecks(_run(pcb)) == []

    def test_unresolved_path_is_reported_not_silent(self):
        pcb = _reinforced()
        import dataclasses

        spec = dataclasses.replace(_spec(True), source=PathEndpoint("NOPE", "1"))
        results = _run(pcb, specs=[spec])
        assert _subchecks(results) == ["skipped"]
        assert "unresolved" in results.violations[0].message


class TestRecordedEndShift:
    """Issue #8803: the sidecar carries the --max-end-shift the board was built with."""

    BOUND = 12.0  # default for this 40 mm leg is 10 mm; anchor slides to x~34.7

    def _slid(self, bound=BOUND):
        pcb = _board(length=40.0)
        pcb.add_trace((15, 51.3), (34, 51.3), width=0.2, layer="F.Cu", net="OTHER")
        outcome = reinforce_net(
            pcb, NET, net_class_clearances={"OTHER": 0.4}, max_end_shift_mm=bound
        )
        return pcb, outcome

    def _wires(self, outcome, bound="keep"):
        from kicad_tools.pcb.reinforcement_sidecar import build_wire_records

        wires = build_wire_records(outcome, 0.0, 0.0)
        if bound != "keep":
            for w in wires:
                if bound is None:
                    w.pop("max_end_shift_mm", None)
                else:
                    w["max_end_shift_mm"] = bound
        return wires

    def _check(self, pcb, wires, **kw):
        rule = ReinforcementIncompleteRule(specs=[_spec(True)], wires=wires, **kw)
        return _subchecks(rule.check(pcb, _rules()))

    def test_bound_recorded_in_wire_records(self):
        _, outcome = self._slid()
        assert all(r["max_end_shift_mm"] == self.BOUND for r in self._wires(outcome))
        _, default = self._slid(bound=None)
        assert all(r["max_end_shift_mm"] is None for r in self._wires(default))

    def test_recorded_bound_suppresses_end_window_finding(self):
        pcb, outcome = self._slid()
        # Same geometry, no recorded bound: still reported.
        assert self._check(pcb, []) == ["a"]
        assert self._check(pcb, self._wires(outcome)) == []

    def test_recorded_bound_beats_constructor_fallback(self):
        pcb, outcome = self._slid()
        assert self._check(pcb, self._wires(outcome), max_end_shift_mm=1.0) == []

    def test_recorded_zero_is_a_real_bound(self):
        pcb, outcome = self._slid()
        assert self._check(pcb, self._wires(outcome, bound=0.0)) == ["a"]

    def test_legacy_sidecar_without_key_behaves_as_before(self):
        pcb, outcome = self._slid()
        assert self._check(pcb, self._wires(outcome, bound=None)) == ["a"]


def _four_layer_wire_board(*, center: bool = False) -> PCB:
    """Wire-reinforced B.Cu run on a 4-layer board (Issue #8866, net A shape).

    ``center=True`` places the outline in the middle of the sheet, giving the
    board a genuine non-zero ``PCB.board_origin`` while leaving every
    board-relative coordinate below unchanged (Issue #8912).
    """
    pcb = PCB.create(width=200, height=100, layers=4, center=center)
    pcb.add_trace((20, 50), (120, 50), width=2.0, layer="B.Cu", net=NET)
    _add_th_pad_footprint(pcb, ref="J1", x=20.0, y=50.0, net=NET, drill=1.0, size=2.0)
    _add_th_pad_footprint(pcb, ref="J2", x=120.0, y=50.0, net=NET, drill=1.0, size=2.0)
    reinforce_net(pcb, NET)
    return pcb


class TestWireRunWithOrdinaryTaps:
    """Issue #8866: a mask-strip wire run is checked, not skipped, despite taps."""

    def test_non_wire_taps_on_other_layers_do_not_cause_a_skip(self):
        pcb = _four_layer_wire_board()
        # Thin B.Cu tap, ordinary via, inner-layer and F.Cu copper on the same net.
        pcb.add_trace((50, 50), (50, 58), width=0.25, layer="B.Cu", net=NET)
        pcb.add_via(50, 58, size=0.8, drill=0.4, net=NET)
        pcb.add_trace((50, 58), (60, 58), width=0.25, layer="In1.Cu", net=NET)
        pcb.add_trace((60, 58), (60, 66), width=0.25, layer="F.Cu", net=NET)
        assert _subchecks(_run(pcb)) == []  # checked and complete

    def test_checked_run_still_reports_a_missing_mask_strip(self):
        pcb = _four_layer_wire_board()
        pcb.add_trace((50, 50), (50, 58), width=0.25, layer="B.Cu", net=NET)
        pcb.graphic_lines[:] = [g for g in pcb.graphic_lines if g.layer != "B.Mask"]
        assert _subchecks(_run(pcb)) == ["b"]  # checked, and the gap is reported

    def test_tap_closing_a_loop_is_checked_from_the_mask_strip(self):
        """Issue #8886: ambiguous copper no longer hides a real wire run."""
        pcb = _ambiguous_wire_board()
        assert _resolution_status(pcb) == "ambiguous"  # skipped before the fix
        assert _subchecks(_run(pcb)) == []  # located from strip + anchors, complete

    def test_ambiguous_net_still_reports_a_missing_mask_strip(self):
        pcb = _ambiguous_wire_board()
        strips = [g for g in pcb.graphic_lines if g.layer == "B.Mask"]
        pcb.graphic_lines[:] = [g for g in pcb.graphic_lines if g.layer != "B.Mask"]
        # Anchors remain, so the wire is still located (from them) -> one skip, not 'no wire'.
        assert strips
        results = _run(pcb)
        assert _subchecks(results) == ["skipped"]
        assert "loop/parallel return path" in results.violations[0].message

    def test_ambiguous_net_reports_a_removed_anchor(self):
        pcb = _ambiguous_wire_board()
        victim = _anchors(pcb)[0]
        pcb.vias[:] = [v for v in pcb.vias if v is not victim]
        assert _subchecks(_run(pcb)) == ["a"]


def _ambiguous_wire_board(*, center: bool = False) -> PCB:
    """A mask-strip wire on B.Cu whose net also has a loop-closing tap (Issue #8886)."""
    pcb = _four_layer_wire_board(center=center)
    pcb.add_trace((50, 50), (50, 58), width=0.25, layer="B.Cu", net=NET)
    pcb.add_trace((50, 58), (70, 58), width=0.25, layer="B.Cu", net=NET)
    pcb.add_trace((70, 58), (70, 50), width=0.25, layer="B.Cu", net=NET)
    return pcb


def _resolution_status(pcb: PCB) -> str:
    from kicad_tools.router.current_paths import resolve_current_path

    return resolve_current_path(pcb, _spec()).status


class TestNoWireAndSkipLocation:
    """Issue #8886: one finding for a wire-less declaration; located skips."""

    @staticmethod
    def _wireless() -> PCB:
        pcb = _ambiguous_wire_board()
        pcb.graphic_lines[:] = [g for g in pcb.graphic_lines if g.layer != "B.Mask"]
        pcb.vias[:] = [v for v in pcb.vias if v.net_name != NET]
        return pcb

    def test_declared_eligible_without_wire_is_one_finding(self):
        pcb = self._wireless()
        assert _resolution_status(pcb) == "ambiguous"
        results = _run(pcb)
        assert _subchecks(results) == ["no_wire"]
        assert "no wire is present" in results.violations[0].message

    def test_no_wire_path_has_no_courtyard_findings(self):
        from kicad_tools.validate.rules.reinforcement_courtyard import (
            ReinforcementCourtyardRule,
        )

        pcb = self._wireless()
        assert list(ReinforcementCourtyardRule(specs=[_spec()]).check(pcb, _rules())) == []

    def test_skipped_finding_is_on_the_path_and_rounded(self):
        pcb = _reinforced()
        import dataclasses

        spec = dataclasses.replace(_spec(True), source=PathEndpoint("NOPE", "1"))
        (v,) = _run(pcb, specs=[spec]).violations
        assert v.location != (0.0, 0.0)
        assert all(round(c, 3) == c for c in v.location)

    def test_skip_message_rounds_witness_coordinates(self):
        from kicad_tools.validate.rules.reinforcement_incomplete import _round_floats

        assert _round_floats("at (10.799999999999997, 10.8)") == "at (10.800, 10.8)"


class TestNonZeroBoardOriginLocations:
    """Issue #8912: ``[no_wire]`` / ``[skipped]`` locations are not double-offset.

    Rules build ``DRCViolation.location`` in the board-relative frame and
    ``DRCChecker._absolutize`` adds ``board_origin`` back exactly once. These
    assertions therefore run through the public ``check_reinforcement_incomplete``
    -- asserting the sheet position against a bare ``rule.check()`` would be
    inverted (it would pass while the rule double-offsets).
    """

    @staticmethod
    def _wireless(*, center: bool) -> PCB:
        pcb = _ambiguous_wire_board(center=center)
        pcb.graphic_lines[:] = [g for g in pcb.graphic_lines if g.layer != "B.Mask"]
        pcb.vias[:] = [v for v in pcb.vias if v.net_name != NET]
        return pcb

    @staticmethod
    def _located(pcb: PCB, spec: CurrentPathSpec) -> tuple[str, tuple[float, float]]:
        """``(sub-check, sheet-absolute location)`` of the rule's single finding."""
        checker = DRCChecker(pcb, layers=4, current_path_specs=[spec])
        (v,) = checker.check_reinforcement_incomplete().violations
        return v.items[1].split("@")[0], v.location

    def test_no_wire_location_is_the_sheet_position_of_the_path(self):
        """The J1 pad sits at board-relative (20, 50) on both boards."""
        flat = self._wireless(center=False)
        shifted = self._wireless(center=True)
        ox, oy = shifted.board_origin
        assert (ox, oy) != (0.0, 0.0), "fixture must have a non-zero board origin"

        kind_flat, loc_flat = self._located(flat, _spec())
        kind_shifted, loc_shifted = self._located(shifted, _spec())
        assert kind_flat == kind_shifted == "no_wire"
        # One origin, not two: the location tracks the board, not 2x the shift.
        assert loc_shifted == pytest.approx((loc_flat[0] + ox, loc_flat[1] + oy))

    def test_skipped_location_is_the_sheet_position_of_the_path(self):
        spec = dataclasses.replace(_spec(True), source=PathEndpoint("NOPE", "1"))
        flat = _ambiguous_wire_board(center=False)
        shifted = _ambiguous_wire_board(center=True)
        ox, oy = shifted.board_origin

        kind_flat, loc_flat = self._located(flat, spec)
        kind_shifted, loc_shifted = self._located(shifted, spec)
        assert kind_flat == kind_shifted == "skipped"
        assert loc_shifted == pytest.approx((loc_flat[0] + ox, loc_flat[1] + oy))

    def test_no_wire_location_lands_inside_the_board_outline(self):
        """A double-offset location falls outside the outline -- unclickable."""
        pcb = self._wireless(center=True)
        ox, oy = pcb.board_origin
        _, (x, y) = self._located(pcb, _spec())
        assert ox <= x <= ox + 200.0, f"x={x} outside [{ox}, {ox + 200.0}]"
        assert oy <= y <= oy + 100.0, f"y={y} outside [{oy}, {oy + 100.0}]"

    def test_net_without_copper_reports_the_board_origin(self):
        """``_on_path``'s no-copper fallback absolutizes to the board origin.

        Both endpoints must be unresolvable *and* the net stripped of copper to
        reach that branch -- a resolved pad short-circuits it.
        """
        pcb = self._wireless(center=True)
        net = pcb.get_net_by_name(NET)
        assert net is not None
        pcb.segments[:] = [s for s in pcb.segments if s.net_number != net.number]
        spec = dataclasses.replace(
            _spec(True), source=PathEndpoint("NOPE", "1"), sink=PathEndpoint("NOPE", "2")
        )
        kind, loc = self._located(pcb, spec)
        assert kind == "skipped"
        # Board-relative (0, 0) is the board origin -- not (0, 0), not 2x the shift.
        assert loc == pytest.approx(tuple(pcb.board_origin))

    def test_zero_origin_locations_are_unchanged(self):
        """The common case keeps reporting board-relative == sheet coordinates."""
        pcb = self._wireless(center=False)
        assert tuple(pcb.board_origin) == (0.0, 0.0)
        assert self._located(pcb, _spec()) == ("no_wire", pytest.approx((20.0, 50.0)))


class TestStitchedDoubledCopper:
    """Issue #8866, net B: doubled F.Cu/B.Cu copper, no mask opening.

    Eligibility is *declared* in the current-paths sidecar, never inferred from
    the copper. Undeclared, the net yields no finding. Declared
    ``reinforcement_eligible: true`` with no sidecar record, the rule is
    working as designed and reports the missing anchors / mask opening on it.

    Issue #8916 changed two things here, deliberately. (d) no longer fires on
    the via at (70, 50): it pins the two branches together mid-run, so the
    path does not change layer there (``PathResolution.series_layer_changes``).
    And once the sidecar records the doubling as a parallel copy, the legs
    need no wire at all.
    """

    @staticmethod
    def _board() -> PCB:
        pcb = PCB.create(width=200, height=100, layers=4, center=False)
        for layer in ("F.Cu", "B.Cu"):
            pcb.add_trace((20, 50), (120, 50), width=2.6, layer=layer, net=NET)
        _add_th_pad_footprint(pcb, ref="J1", x=20.0, y=50.0, net=NET, drill=1.0, size=2.0)
        _add_th_pad_footprint(pcb, ref="J2", x=120.0, y=50.0, net=NET, drill=1.0, size=2.0)
        pcb.add_via(70, 50, size=1.2, drill=0.6, net=NET)
        return pcb

    def test_undeclared_doubled_copper_has_no_finding(self):
        assert _run(self._board(), eligible=False).violations == []

    def test_declared_eligible_doubled_copper_is_flagged_as_designed(self):
        results = _run(self._board(), eligible=True)
        assert set(_subchecks(results)) == {"a", "b"}

    def test_mid_run_stitching_via_is_not_a_layer_change(self):
        """Was ``d`` before #8916: the via sits inside both branches."""
        assert "d" not in _subchecks(_run(self._board(), eligible=True))

    def test_recorded_parallel_copy_needs_no_wire(self):
        """The same copper, recorded by ``--strategy parallel``, is clean."""
        record = {
            "net": NET,
            "layer": "B.Cu",
            "kind": "run",
            "leg_start": {"x": 20.0, "y": 50.0},
            "leg_end": {"x": 120.0, "y": 50.0},
            "anchors": [],
            "wire_gauge_awg": 16,
            "status": "parallel",
            "strategy": "parallel",
            "copy_status": "placed",
            "copy_layer": "F.Cu",
            "polyline": [{"x": 20.0, "y": 50.0}, {"x": 120.0, "y": 50.0}],
            "copy_segments": [
                {"start": {"x": 20.0, "y": 50.0}, "end": {"x": 120.0, "y": 50.0}, "width_mm": 2.6}
            ],
            "stitch_vias": [{"x": 70.0, "y": 50.0}],
        }
        rule = ReinforcementIncompleteRule(specs=[_spec(True)], wires=[record])
        assert rule.check(self._board(), _rules()).violations == []


class TestBelowMinWireLengthInteraction:
    """Issue #8915 asked what this rule reports once a declared-eligible path's
    only leg falls under the default 15 mm minimum. Measured: the path still
    resolves ``ok``, so it is checked from its resolved segments and yields the
    ``a`` (no anchor holes) + ``b`` (no mask opening) warnings -- **not**
    ``no_wire``, which only fires for an *unresolved* declared-eligible path.

    Issue #8916 gives that short leg somewhere to go: ``--strategy auto``
    copies it onto the opposite outer layer, the wire list records the copy,
    and ``a`` / ``b`` stop firing for it. A ``wire`` leg still earns them.
    """

    @staticmethod
    def _reinforced_short(length: float, **kw) -> PCB:
        pcb = _board(length=length)
        reinforce_net(pcb, NET, current_paths=[_spec(True)], **kw)
        return pcb

    def test_below_minimum_reports_a_and_b_not_no_wire(self):
        results = _run(self._reinforced_short(12.0))
        assert set(_subchecks(results)) == {"a", "b"}
        assert "no_wire" not in _subchecks(results)
        assert {v.severity for v in results.violations} == {"warning"}

    def test_pinning_the_minimum_off_leaves_the_path_clean(self):
        """The findings above are the minimum's doing, not the 12 mm leg's."""
        assert _subchecks(_run(self._reinforced_short(12.0, min_wire_length_mm=0.0))) == []

    def test_leg_above_the_minimum_is_clean(self):
        assert _subchecks(_run(self._reinforced_short(25.0))) == []

    def test_auto_strategy_copy_clears_a_and_b(self):
        from kicad_tools.pcb.reinforcement_sidecar import build_wire_records

        pcb = _board(length=12.0)
        result = reinforce_net(pcb, NET, current_paths=[_spec(True)], strategy="auto")
        assert [leg.strategy for leg in result.legs] == ["parallel"]
        wires = build_wire_records(result, 0.0, 0.0)
        rule = ReinforcementIncompleteRule(specs=[_spec(True)], wires=wires)
        assert _subchecks(rule.check(pcb, _rules())) == []

    def test_wire_strategy_short_leg_still_reports_a_and_b(self):
        from kicad_tools.pcb.reinforcement_sidecar import build_wire_records

        pcb = _board(length=12.0)
        result = reinforce_net(pcb, NET, current_paths=[_spec(True)], strategy="wire")
        rule = ReinforcementIncompleteRule(
            specs=[_spec(True)], wires=build_wire_records(result, 0.0, 0.0)
        )
        assert set(_subchecks(rule.check(pcb, _rules()))) == {"a", "b"}
