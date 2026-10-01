"""Post-route self-check must report cross-net copper overlap (issue #5862).

Background
----------
``kct route`` on the normalized Arduino Nano (``benchmarks/external``
slug ``srj18_arduino_nano``) wrote a board that ``kicad-cli pcb drc
--refill-zones`` reports with 21 ``shorting_items`` while the router's own
post-route self-check reported a clean clearance check.  Reproducing the
run and classifying every short by geometry (the DRC JSON names both
items of each pair) showed they were **not** the zone-copper class the
issue originally suspected: 13 of 14 were a routed track crossing a
foreign-net **pad** of U3 -- the fine-pitch TQFP whose inter-pad corridor
``RoutingGrid._relax_same_component_clearance`` (#2452) unblocks.

Two independent defects produced the silent pass:

1. :func:`validate_routes` classified those pad violations as
   ``component_inherent=True``.  That flag means "the component's own
   geometry forces a sub-clearance gap", and it excludes the violation
   from the printed summary, from the ``drc_verify_and_nudge`` repair
   pass and from the CLI's violation accounting.  It was applied purely
   on "same component + relaxation active", with no gap-sign test -- so a
   trace running *through* a neighbouring pin's metal (a hard short at a
   negative gap) was excused exactly like a 0.15 mm near-miss.
   ``RoutingGrid._same_component_carveout_mode`` has enforced the
   ``clearance >= 0`` boundary since #5166; this validator silently did
   not.
2. The CLI gated its exit code on ``obstacle_type == "segment"`` only, so
   even a *reported* pad short left the run on exit 0 / 2.

A third, latent gap in the same validator: every other quadrant widens
its obstacle universe with ``router.existing_routes`` (segment-to-via,
via-to-pad, via-to-via all do), but segment-to-segment walked
``router.routes`` against itself only -- so a new trace laid across a
foreign trace preserved by ``--preserve-existing`` / ``--nets`` produced
no violation at all.

These tests pin all three, plus the non-regression direction: a positive
but sub-clearance gap on a relaxed component is still excused.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import kicad_tools.router.io as router_io
from kicad_tools.cli.route_cmd import main as route_main
from kicad_tools.router.core import Autorouter
from kicad_tools.router.io import (
    ClearanceViolation,
    count_shorting_violations,
    format_clearance_violations,
    validate_routes,
)
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Route, Segment, Via
from kicad_tools.router.rules import DesignRules


def _rules() -> DesignRules:
    return DesignRules(trace_width=0.2, trace_clearance=0.2, grid_resolution=0.1)


def _router_with_relaxed_qfp() -> Autorouter:
    """Two pads of U3 on different nets, U3's corridor relaxed (#2452).

    Pad geometry mirrors a fine-pitch QFP land: 1.0 x 0.3 mm, 5 mm apart
    on the same row.  Both nets attach to U3, which is what makes the
    ``component_inherent`` predicate eligible at all.
    """
    router = Autorouter(width=50, height=50, rules=_rules())
    router.add_component(
        "U3",
        [
            {"number": "1", "x": 5.0, "y": 10.0, "width": 1.0, "height": 0.3, "net": 1},
            {"number": "2", "x": 10.0, "y": 10.0, "width": 1.0, "height": 0.3, "net": 2},
        ],
    )
    router.grid._relaxed_clearance_refs.add("U3")
    return router


class TestTraceThroughForeignPadIsNeverComponentInherent:
    """The Arduino Nano defect: track over a foreign pad of a relaxed part."""

    def test_overlap_is_reported_not_excused(self) -> None:
        router = _router_with_relaxed_qfp()
        # Net 1 escape running east along the pad row, straight through
        # U3 pad 2's metal (net 2).  Edge-to-edge gap is -0.1 mm.
        router.routes.append(
            Route(
                net=1,
                net_name="D4",
                segments=[Segment(x1=5.0, y1=10.0, x2=12.0, y2=10.0, layer=Layer.F_CU, width=0.2)],
                vias=[],
            )
        )

        violations = validate_routes(router)
        pad_violations = [v for v in violations if v.obstacle_type == "pad"]

        assert len(pad_violations) == 1, (
            "A trace crossing a foreign-net pad must produce exactly one pad violation."
        )
        v = pad_violations[0]
        assert v.distance < 0.0, "copper overlaps -- the gap must be negative"
        assert v.is_short is True
        assert v.component_inherent is False, (
            "Issue #5862: overlapping copper is a SHORT. No component "
            "geometry forces a trace through a neighbouring pin's metal, "
            "so the same-component relaxation must not excuse it."
        )
        assert count_shorting_violations(violations) == 1

    def test_short_is_named_in_the_formatted_summary(self) -> None:
        """The excused classification previously hid the short entirely."""
        router = _router_with_relaxed_qfp()
        router.routes.append(
            Route(
                net=1,
                net_name="D4",
                segments=[Segment(x1=5.0, y1=10.0, x2=12.0, y2=10.0, layer=Layer.F_CU, width=0.2)],
                vias=[],
            )
        )

        summary = format_clearance_violations(validate_routes(router))

        assert "SHORT" in summary
        assert "component-inherent" not in summary, (
            "Before #5862 this summary said only 'Info: 1 component-inherent "
            "pad spacing(s) excluded' for a trace routed through pad metal."
        )

    def test_via_through_foreign_pad_is_also_a_short(self) -> None:
        """The via-to-pad quadrant carried the identical carve-out."""
        router = _router_with_relaxed_qfp()
        router.routes.append(
            Route(
                net=1,
                net_name="D4",
                segments=[],
                vias=[
                    Via(
                        x=10.0,
                        y=10.0,
                        diameter=0.6,
                        drill=0.3,
                        layers=(Layer.F_CU, Layer.B_CU),
                        net=1,
                    )
                ],
            )
        )

        violations = [v for v in validate_routes(router) if v.obstacle_type == "pad"]

        assert violations, "a via dropped on top of a foreign pad must be reported"
        assert all(v.is_short for v in violations)
        assert all(not v.component_inherent for v in violations)

    def test_positive_subclearance_gap_is_still_excused(self) -> None:
        """Non-regression for #2452 / #3545 / #5166.

        The relaxation exists so a corridor route between a fine-pitch
        part's own pads is not rejected for a sub-clearance *but
        positive* gap.  Only the overlap case changed in #5862.
        """
        router = _router_with_relaxed_qfp()
        # Trace parallel to the pad row, 0.25 mm north of pad 2's centre:
        # the rect half-height is 0.15 and the trace half-width 0.1, so
        # the gap is 0.25 - 0.15 - 0.1 = 0.0... nudge it to a clear
        # positive 0.05 mm by standing 0.30 mm off.
        router.routes.append(
            Route(
                net=1,
                net_name="D4",
                segments=[Segment(x1=8.0, y1=9.70, x2=12.0, y2=9.70, layer=Layer.F_CU, width=0.2)],
                vias=[],
            )
        )

        pad_violations = [v for v in validate_routes(router) if v.obstacle_type == "pad"]

        assert len(pad_violations) == 1
        v = pad_violations[0]
        assert v.distance > 0.0, "fixture must model a near-miss, not an overlap"
        assert v.distance < v.required, "fixture must still be below the required gap"
        assert v.component_inherent is True, (
            "A positive sub-clearance gap on a relaxed component stays "
            "component-inherent (#2452 / #3545 / #5166)."
        )
        assert count_shorting_violations(validate_routes(router)) == 0


class TestOverlappingDifferentNetSegments:
    """Acceptance criterion 1 of issue #5862, both obstacle universes."""

    def test_two_routed_segments_overlapping_are_reported(self) -> None:
        router = Autorouter(width=50, height=50, rules=_rules())
        router.add_component(
            "J1",
            [
                {"number": "1", "x": 5.0, "y": 20.0, "width": 1.0, "height": 1.0, "net": 1},
                {"number": "2", "x": 20.0, "y": 20.0, "width": 1.0, "height": 1.0, "net": 2},
            ],
        )
        router.routes.append(
            Route(
                net=1,
                net_name="NET1",
                segments=[Segment(x1=6.0, y1=20.0, x2=16.0, y2=20.0, layer=Layer.F_CU, width=0.25)],
                vias=[],
            )
        )
        router.routes.append(
            Route(
                net=2,
                net_name="NET2",
                segments=[
                    Segment(x1=10.0, y1=15.0, x2=10.0, y2=25.0, layer=Layer.F_CU, width=0.25)
                ],
                vias=[],
            )
        )

        violations = validate_routes(router)
        seg_violations = [v for v in violations if v.obstacle_type == "segment"]

        assert len(seg_violations) == 1
        assert seg_violations[0].is_short is True
        assert count_shorting_violations(violations) == 1

    def test_new_segment_over_preserved_foreign_segment_is_reported(self) -> None:
        """The segment-to-segment quadrant ignored ``existing_routes``.

        Every sibling quadrant already widens its obstacle set with
        preserved copper ("Include pre-existing routes so new segments
        are checked against old vias"); this one did not, so a
        ``--preserve-existing`` run could lay a new trace straight across
        a preserved foreign trace and report zero seg-seg violations.
        """
        router = Autorouter(width=50, height=50, rules=_rules())
        router.add_component(
            "J1",
            [
                {"number": "1", "x": 5.0, "y": 20.0, "width": 1.0, "height": 1.0, "net": 1},
                {"number": "2", "x": 20.0, "y": 20.0, "width": 1.0, "height": 1.0, "net": 2},
            ],
        )
        router.existing_routes.append(
            Route(
                net=2,
                net_name="PRESERVED",
                segments=[
                    Segment(x1=10.0, y1=15.0, x2=10.0, y2=25.0, layer=Layer.F_CU, width=0.25)
                ],
                vias=[],
            )
        )
        router.routes.append(
            Route(
                net=1,
                net_name="NEW",
                segments=[Segment(x1=6.0, y1=20.0, x2=16.0, y2=20.0, layer=Layer.F_CU, width=0.25)],
                vias=[],
            )
        )

        violations = validate_routes(router)
        seg_violations = [v for v in violations if v.obstacle_type == "segment"]

        assert len(seg_violations) == 1, (
            "Issue #5862: a new trace crossing preserved foreign copper "
            "must be reported by the segment-to-segment quadrant."
        )
        v = seg_violations[0]
        assert v.net == 1, "the NEW (mutable) route is the reported offender"
        assert v.obstacle_net == 2
        assert v.is_short is True
        assert count_shorting_violations(violations) == 1

    def test_preserved_segment_on_another_layer_is_not_a_violation(self) -> None:
        """The new quadrant must not become a false-positive source."""
        router = Autorouter(width=50, height=50, rules=_rules())
        router.add_component(
            "J1",
            [
                {"number": "1", "x": 5.0, "y": 20.0, "width": 1.0, "height": 1.0, "net": 1},
                {"number": "2", "x": 20.0, "y": 20.0, "width": 1.0, "height": 1.0, "net": 2},
            ],
        )
        router.existing_routes.append(
            Route(
                net=2,
                net_name="PRESERVED",
                segments=[
                    Segment(x1=10.0, y1=15.0, x2=10.0, y2=25.0, layer=Layer.B_CU, width=0.25)
                ],
                vias=[],
            )
        )
        router.routes.append(
            Route(
                net=1,
                net_name="NEW",
                segments=[Segment(x1=6.0, y1=20.0, x2=16.0, y2=20.0, layer=Layer.F_CU, width=0.25)],
                vias=[],
            )
        )

        assert [v for v in validate_routes(router) if v.obstacle_type == "segment"] == []

    def test_preserved_same_net_segment_is_not_a_violation(self) -> None:
        router = Autorouter(width=50, height=50, rules=_rules())
        router.add_component(
            "J1",
            [
                {"number": "1", "x": 5.0, "y": 20.0, "width": 1.0, "height": 1.0, "net": 1},
                {"number": "2", "x": 20.0, "y": 20.0, "width": 1.0, "height": 1.0, "net": 1},
            ],
        )
        router.existing_routes.append(
            Route(
                net=1,
                net_name="NET1",
                segments=[
                    Segment(x1=10.0, y1=15.0, x2=10.0, y2=25.0, layer=Layer.F_CU, width=0.25)
                ],
                vias=[],
            )
        )
        router.routes.append(
            Route(
                net=1,
                net_name="NET1",
                segments=[Segment(x1=6.0, y1=20.0, x2=16.0, y2=20.0, layer=Layer.F_CU, width=0.25)],
                vias=[],
            )
        )

        assert [v for v in validate_routes(router) if v.obstacle_type == "segment"] == []


class TestCountShortingViolations:
    """The predicate the CLI's exit-code gate is built on."""

    @staticmethod
    def _violation(distance: float, **kwargs: object) -> ClearanceViolation:
        defaults: dict = {
            "segment_index": 0,
            "x1": 0.0,
            "y1": 0.0,
            "x2": 1.0,
            "y2": 0.0,
            "net": 1,
            "obstacle_type": "pad",
            "obstacle_net": 2,
            "distance": distance,
            "required": 0.2,
        }
        defaults.update(kwargs)
        return ClearanceViolation(**defaults)  # type: ignore[arg-type]

    @pytest.mark.parametrize("distance", [-0.5, -1e-9, 0.0])
    def test_non_positive_gap_is_a_short(self, distance: float) -> None:
        assert self._violation(distance).is_short is True
        assert count_shorting_violations([self._violation(distance)]) == 1

    @pytest.mark.parametrize("distance", [1e-9, 0.05, 0.19])
    def test_positive_subclearance_gap_is_not_a_short(self, distance: float) -> None:
        assert self._violation(distance).is_short is False
        assert count_shorting_violations([self._violation(distance)]) == 0

    def test_counts_every_obstacle_class(self) -> None:
        """The pre-#5862 CLI gate counted ``obstacle_type == "segment"`` only."""
        violations = [
            self._violation(-0.1, obstacle_type=kind)
            for kind in ("pad", "via", "segment", "fixed_copper")
        ]
        assert count_shorting_violations(violations) == 4


_TRIVIAL_BOARD = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general
    (thickness 1.6)
  )
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (setup
    (pad_to_mask_clearance 0)
  )
  (net 0 "")
  (net 1 "NET1")
  (gr_rect (start 100 100) (end 120 110)
    (stroke (width 0.1) (type default))
    (fill none)
    (layer "Edge.Cuts")
  )
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000010")
    (at 105 105)
    (property "Reference" "R1" (at 0 -1.5 0) (layer "F.SilkS"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64) \
(layers "F.Cu" "F.Paste" "F.Mask") (net 1 "NET1"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64) \
(layers "F.Cu" "F.Paste" "F.Mask") (net 1 "NET1"))
  )
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000011")
    (at 115 105)
    (property "Reference" "R2" (at 0 -1.5 0) (layer "F.SilkS"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64) \
(layers "F.Cu" "F.Paste" "F.Mask") (net 1 "NET1"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64) \
(layers "F.Cu" "F.Paste" "F.Mask") (net 1 "NET1"))
  )
)
"""


class TestCliShortGate:
    """``kct route`` must not exit 0 while its own validation found a short.

    The pre-save block printed the violation list but folded only
    ``obstacle_type == "segment"`` into ``seg_seg_violation_count``, the
    sole clearance input to the exit code.  A pad short -- the entire
    Arduino Nano finding -- therefore left the run on exit 0.

    The board here is trivially routable (one two-pad net); the short is
    injected by stubbing :func:`validate_routes`, which keeps the test
    deterministic and bounded while exercising the real gate, printing
    and exit-code path.
    """

    @staticmethod
    def _pad_short() -> ClearanceViolation:
        return ClearanceViolation(
            segment_index=0,
            x1=104.0,
            y1=105.0,
            x2=116.0,
            y2=105.0,
            net=1,
            obstacle_type="pad",
            obstacle_net=2,
            distance=-0.1,
            required=0.2,
            net_name="NET1",
            obstacle_net_name="NET2",
            location=(110.0, 105.0),
            layer=Layer.F_CU,
        )

    def test_pad_short_forces_nonzero_exit_and_is_named(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        pcb = tmp_path / "trivial.kicad_pcb"
        pcb.write_text(_TRIVIAL_BOARD)
        out = tmp_path / "routed.kicad_pcb"

        monkeypatch.setattr(router_io, "validate_routes", lambda *a, **k: [self._pad_short()])

        rc = route_main([str(pcb), "-o", str(out), "--skip-drc", "--no-optimize"])

        assert rc != 0, (
            "Issue #5862: a run whose own pre-save validation found copper "
            "of two different nets overlapping must never exit 0."
        )
        assert rc == 3, "fully routed + dirty copper is the established exit-3 contract"
        stdout = capsys.readouterr().out
        assert "short(s)" in stdout
        assert "shorting_items" in stdout, "the message must name the DRC class it maps to"
        assert "NET1 vs NET2" in stdout

    def test_clean_validation_still_exits_zero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The gate must not become a false-failure source."""
        pcb = tmp_path / "trivial.kicad_pcb"
        pcb.write_text(_TRIVIAL_BOARD)
        out = tmp_path / "routed.kicad_pcb"

        monkeypatch.setattr(router_io, "validate_routes", lambda *a, **k: [])

        assert route_main([str(pcb), "-o", str(out), "--skip-drc", "--no-optimize"]) == 0

    def test_positive_subclearance_pad_gap_does_not_gate(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Only overlap gates; a pad near-miss keeps its historical behaviour."""
        pcb = tmp_path / "trivial.kicad_pcb"
        pcb.write_text(_TRIVIAL_BOARD)
        out = tmp_path / "routed.kicad_pcb"

        near_miss = ClearanceViolation(
            segment_index=0,
            x1=104.0,
            y1=105.0,
            x2=116.0,
            y2=105.0,
            net=1,
            obstacle_type="pad",
            obstacle_net=2,
            distance=0.15,
            required=0.2,
            net_name="NET1",
            obstacle_net_name="NET2",
            location=(110.0, 105.0),
            layer=Layer.F_CU,
        )
        monkeypatch.setattr(router_io, "validate_routes", lambda *a, **k: [near_miss])

        assert route_main([str(pcb), "-o", str(out), "--skip-drc", "--no-optimize"]) == 0
