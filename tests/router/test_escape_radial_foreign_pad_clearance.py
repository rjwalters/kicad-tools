"""Tests for Issue #4991: ``_escape_radial`` board-wide foreign-pad clearance.

Curator analysis on Issue #4991 traced a real board02 (ATtiny85 charlieplex)
DRC failure -- 9 native KiCad DRC violations, including actual copper shorts
between C1's VCC pad and a GND via, and between R5's RESET pad and a VCC
via -- to an asymmetry between the escape-generation paths: the fine-pitch
dense-package escape helpers (``staggered_via_fanout``, ``_try_in_pad_escape``,
``_try_lateral_via_escape``) already accept an optional ``foreign_pads``
parameter and validate candidates against board-wide (cross-component) pad
copper, but ``_escape_radial`` -- the path ordinary (non-dense) passives like
0805 resistors/capacitors and 4-terminal Kelvin shunts fall through to --
only ever checked ``_compute_max_safe_escape_length`` against pads on the
SAME footprint via its ``package_pads`` argument.  A pad on a physically
adjacent but electrically unrelated component (e.g. a decoupling cap placed
right next to the pin it decouples, or a Kelvin-shunt sense pin routed close
to its current-sense amplifier) was invisible to the clipping search, so the
emitted escape segment could clip straight through it.

This module covers:

1. Unit-level: ``_compute_max_safe_escape_length`` with the new
   ``foreign_pads`` parameter -- board-wide pads must clip the candidate
   segment exactly like same-package pads, own-net foreign pads are exempt,
   and legacy behaviour (``foreign_pads=None``) is preserved byte-for-byte.
2. Integration: ``_escape_radial`` (via ``generate_escapes``) on a
   synthetic two-component board where a same-package-only check would
   have clipped a foreign-pad-violating segment as "unclipped".
3. Regression fixture (Issue #4991 acceptance criterion): a real
   Vishay WSK2512 four-terminal Kelvin-shunt land pattern (the RSH1
   footprint used on board09) with an asymmetrical four-pad layout,
   plus an adjacent foreign-net pad simulating the current-sense
   amplifier's SENSE_N pin routed close by for accurate Kelvin sensing.
4. Diagnostic-on-infeasible: when even a zero-useful-length stub would
   violate foreign-pad clearance, the pin's escape must be DROPPED
   (never emitted as invalid geometry), with a debug diagnostic logged.
"""

from __future__ import annotations

import logging

import pytest

from kicad_tools.router.escape import EscapeRouter, PackageType, is_dense_package
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Pad
from kicad_tools.router.rules import DesignRules

# ----------------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------------


def _jlcpcb_rules(**overrides) -> DesignRules:
    """Default JLCPCB-tier-0 design rules (no via-in-pad)."""
    kwargs = {
        "trace_width": 0.25,
        "trace_clearance": 0.127,
        "via_drill": 0.3,
        "via_diameter": 0.6,
        "via_clearance": 0.127,
        "grid_resolution": 0.05,
        "fine_pitch_clearance": 0.127,
        "fine_pitch_threshold": 0.8,
        "min_trace_width": None,
    }
    kwargs.update(overrides)
    return DesignRules(**kwargs)


def _make_router(rules: DesignRules | None = None, escape_clearance: float | None = None):
    if rules is None:
        rules = _jlcpcb_rules()
    grid = RoutingGrid(
        width=40.0,
        height=40.0,
        rules=rules,
        origin_x=-20.0,
        origin_y=-20.0,
    )
    return EscapeRouter(grid, rules, escape_clearance=escape_clearance), grid


def _wsk2512_kelvin_shunt_pads(ref: str = "RSH1", net_base: int = 1) -> list[Pad]:
    """Real pad geometry for ``Resistor_SMD:R_Shunt_Vishay_WSK2512_6332Metric_T1.19mm``.

    This is the RSH1 footprint used on board09 (USB-C PD power board) for
    the 10 mOhm current-shunt resistor: an asymmetrical four-terminal
    Kelvin-sense land pattern with two large power pads (pins 1 and 4)
    and two small offset sense pads (pins 2 and 3).  Coordinates are
    copied verbatim from the KiCad footprint library (component-local,
    unrotated).
    """
    return [
        Pad(
            x=-2.985,
            y=-0.635,
            width=2.29,
            height=2.03,
            net=net_base,
            net_name="VOUT_PRE",
            ref=ref,
            pin="1",
            layer=Layer.F_CU,
        ),
        Pad(
            x=-3.43,
            y=1.27,
            width=1.4,
            height=0.76,
            net=net_base + 1,
            net_name="KELVIN_P",
            ref=ref,
            pin="2",
            layer=Layer.F_CU,
        ),
        Pad(
            x=3.43,
            y=-1.27,
            width=1.4,
            height=0.76,
            net=net_base + 2,
            net_name="KELVIN_N",
            ref=ref,
            pin="3",
            layer=Layer.F_CU,
        ),
        Pad(
            x=2.985,
            y=0.635,
            width=2.29,
            height=2.03,
            net=net_base + 3,
            net_name="+5V_OUT",
            ref=ref,
            pin="4",
            layer=Layer.F_CU,
        ),
    ]


# ----------------------------------------------------------------------------
# Unit tests: _compute_max_safe_escape_length foreign_pads parameter
# ----------------------------------------------------------------------------


class TestComputeMaxSafeEscapeLengthForeignPads:
    """Unit-level coverage of the new ``foreign_pads`` parameter."""

    def test_foreign_pads_none_preserves_legacy_behavior(self):
        """Omitting ``foreign_pads`` (the default) must be byte-identical
        to the pre-#4991 same-package-only behaviour."""
        router, _grid = _make_router()
        pad = Pad(
            x=0.0,
            y=0.0,
            width=0.3,
            height=0.3,
            net=1,
            net_name="A",
            ref="R1",
            pin="1",
            layer=Layer.F_CU,
        )
        # A foreign pad sitting directly in the launch path would clip
        # the segment IF checked -- but it must be ignored when
        # foreign_pads is not supplied at all.
        foreign = Pad(
            x=0.3,
            y=0.0,
            width=0.2,
            height=0.2,
            net=99,
            net_name="FOREIGN",
            ref="C1",
            pin="1",
            layer=Layer.F_CU,
        )
        result_no_foreign_arg = router._compute_max_safe_escape_length(
            pad=pad,
            dx=1.0,
            dy=0.0,
            trace_width=0.2,
            package_pads=[pad],
            min_clearance=0.127,
            max_length=2.0,
        )
        result_foreign_none = router._compute_max_safe_escape_length(
            pad=pad,
            dx=1.0,
            dy=0.0,
            trace_width=0.2,
            package_pads=[pad],
            min_clearance=0.127,
            max_length=2.0,
            foreign_pads=None,
        )
        del foreign  # unused when foreign_pads is not threaded through
        assert result_no_foreign_arg == pytest.approx(2.0)
        assert result_foreign_none == pytest.approx(2.0)

    def test_foreign_pad_on_launch_axis_clips_segment(self):
        """A foreign (different component) pad sitting directly along the
        launch direction must clip the segment exactly like a same-package
        neighbour would."""
        router, _grid = _make_router()
        pad = Pad(
            x=0.0,
            y=0.0,
            width=0.3,
            height=0.3,
            net=1,
            net_name="A",
            ref="R1",
            pin="1",
            layer=Layer.F_CU,
        )
        foreign = Pad(
            x=1.0,
            y=0.0,
            width=0.3,
            height=0.3,
            net=99,
            net_name="FOREIGN",
            ref="C1",
            pin="1",
            layer=Layer.F_CU,
        )
        # Same geometry as the same-package clipping test in
        # test_escape_pad_clearance_2756.py, but the obstacle is a
        # DIFFERENT component -- package_pads contains only the
        # originating pad.
        result = router._compute_max_safe_escape_length(
            pad=pad,
            dx=1.0,
            dy=0.0,
            trace_width=0.2,
            package_pads=[pad],
            min_clearance=0.127,
            max_length=2.0,
            foreign_pads=[foreign],
        )
        assert result < 0.85, f"Expected clip below foreign pad west edge (0.85mm), got {result}"
        assert result == pytest.approx(0.85 - 0.1 - 0.127, abs=1e-3)

    def test_foreign_pad_same_net_is_not_clipped_against(self):
        """A foreign pad on the SAME net (e.g. a stitched plane-pour
        anchor on another component) must never contribute to clipping."""
        router, _grid = _make_router()
        pad = Pad(
            x=0.0,
            y=0.0,
            width=0.3,
            height=0.3,
            net=1,
            net_name="GND",
            ref="R1",
            pin="1",
            layer=Layer.F_CU,
        )
        same_net_foreign = Pad(
            x=0.5,
            y=0.0,
            width=0.3,
            height=0.3,
            net=1,
            net_name="GND",
            ref="C1",
            pin="1",
            layer=Layer.F_CU,
        )
        result = router._compute_max_safe_escape_length(
            pad=pad,
            dx=1.0,
            dy=0.0,
            trace_width=0.2,
            package_pads=[pad],
            min_clearance=0.127,
            max_length=2.0,
            foreign_pads=[same_net_foreign],
        )
        assert result == pytest.approx(2.0)

    def test_foreign_pad_on_other_layer_does_not_clip(self):
        """A foreign pad on a different (non-PTH) layer must not clip --
        mirrors the same-package layer-filtering rule."""
        router, _grid = _make_router()
        pad = Pad(
            x=0.0,
            y=0.0,
            width=0.3,
            height=0.3,
            net=1,
            net_name="A",
            ref="R1",
            pin="1",
            layer=Layer.F_CU,
        )
        foreign_back = Pad(
            x=0.5,
            y=0.0,
            width=0.3,
            height=0.3,
            net=99,
            net_name="FOREIGN",
            ref="C1",
            pin="1",
            layer=Layer.B_CU,
        )
        result = router._compute_max_safe_escape_length(
            pad=pad,
            dx=1.0,
            dy=0.0,
            trace_width=0.2,
            package_pads=[pad],
            min_clearance=0.127,
            max_length=2.0,
            foreign_pads=[foreign_back],
        )
        assert result == pytest.approx(2.0)


# ----------------------------------------------------------------------------
# Integration tests: _escape_radial board-wide awareness
# ----------------------------------------------------------------------------


class TestEscapeRadialForeignPadAwareness:
    """``generate_escapes`` -> ``_escape_radial`` must respect board-wide
    foreign-component pad clearance, not just same-package clearance."""

    def test_radial_escape_clips_against_adjacent_foreign_component(self):
        """Two ordinary 0805-class components placed close together (the
        board02 C1/R5 pattern): a radial escape from one must not clip
        the other's pad."""
        rules = _jlcpcb_rules()
        router, grid = _make_router(rules, escape_clearance=1.0)

        # R1: simple 2-pad resistor at the origin.
        r1_pads = [
            Pad(
                x=-0.9,
                y=0.0,
                width=1.2,
                height=1.4,
                net=1,
                net_name="VCC",
                ref="R1",
                pin="1",
                layer=Layer.F_CU,
            ),
            Pad(
                x=0.9,
                y=0.0,
                width=1.2,
                height=1.4,
                net=2,
                net_name="RESET",
                ref="R1",
                pin="2",
                layer=Layer.F_CU,
            ),
        ]
        # C1: adjacent 0805 capacitor placed east of R1's pin 2, close
        # enough that R1 pin 2's radial (EAST) escape launch would clip
        # C1's west-facing pad without board-wide awareness.
        c1_pads = [
            Pad(
                x=2.3,
                y=0.0,
                width=1.2,
                height=1.4,
                net=3,
                net_name="GND",
                ref="C1",
                pin="1",
                layer=Layer.F_CU,
            ),
            Pad(
                x=3.8,
                y=0.0,
                width=1.2,
                height=1.4,
                net=1,
                net_name="VCC",
                ref="C1",
                pin="2",
                layer=Layer.F_CU,
            ),
        ]
        for pad in r1_pads + c1_pads:
            grid.add_pad(pad)

        assert not is_dense_package(
            r1_pads, trace_width=rules.trace_width, clearance=rules.trace_clearance
        ), "R1 must dispatch to _escape_radial, not a dense-package path"

        info = router.analyze_package(r1_pads)
        assert info.package_type not in (
            PackageType.BGA,
            PackageType.QFP,
            PackageType.QFN,
            PackageType.TQFP,
            PackageType.SSOP,
            PackageType.TSSOP,
            PackageType.SOP,
        )

        escapes = router.generate_escapes(info)

        clearance = rules.trace_clearance
        for escape in escapes:
            for seg in escape.segments:
                for other in c1_pads:
                    if other.layer != seg.layer and not other.through_hole:
                        continue
                    gap = router._segment_to_pad_edge_gap(seg, other)
                    assert gap >= clearance - 1e-6, (
                        f"Issue #4991 regression: radial escape from "
                        f"{escape.pad.ref}.{escape.pad.pin} clips foreign "
                        f"pad {other.ref}.{other.pin}: gap={gap:.4f}mm, "
                        f"required={clearance:.4f}mm"
                    )

    def test_without_foreign_awareness_the_same_geometry_would_violate(self):
        """Regression guard: demonstrate that the SAME geometry, checked
        the pre-#4991 way (same-package pads only), would have accepted
        a clearance-violating segment.  This pins the bug the integration
        test above proves is fixed."""
        rules = _jlcpcb_rules()
        router, _grid = _make_router(rules, escape_clearance=1.0)

        pin2 = Pad(
            x=0.9,
            y=0.0,
            width=1.2,
            height=1.4,
            net=2,
            net_name="RESET",
            ref="R1",
            pin="2",
            layer=Layer.F_CU,
        )
        pin1 = Pad(
            x=-0.9,
            y=0.0,
            width=1.2,
            height=1.4,
            net=1,
            net_name="VCC",
            ref="R1",
            pin="1",
            layer=Layer.F_CU,
        )
        foreign_c1_pin1 = Pad(
            x=2.3,
            y=0.0,
            width=1.2,
            height=1.4,
            net=3,
            net_name="GND",
            ref="C1",
            pin="1",
            layer=Layer.F_CU,
        )

        # Pre-#4991 call shape: no foreign_pads at all.
        legacy_safe_length = router._compute_max_safe_escape_length(
            pad=pin2,
            dx=1.0,
            dy=0.0,
            trace_width=rules.trace_width,
            package_pads=[pin1, pin2],
            min_clearance=rules.trace_clearance,
            max_length=1.0,
        )
        assert legacy_safe_length == pytest.approx(1.0), (
            "Precondition: with no same-package obstacle to the east, the "
            "legacy (same-package-only) helper must have returned the "
            "full unclipped length -- proving it was blind to C1."
        )

        # The "legacy" unclipped segment actually violates clearance
        # against the foreign pad it never checked.
        from kicad_tools.router.primitives import Segment

        legacy_segment = Segment(
            x1=pin2.x,
            y1=pin2.y,
            x2=pin2.x + legacy_safe_length,
            y2=pin2.y,
            width=rules.trace_width,
            layer=Layer.F_CU,
            net=pin2.net,
            net_name=pin2.net_name,
        )
        gap = router._segment_to_pad_edge_gap(legacy_segment, foreign_c1_pin1)
        assert gap < rules.trace_clearance, (
            f"Expected the legacy (foreign-blind) escape to violate "
            f"clearance against C1 -- gap={gap:.4f}mm, "
            f"clearance={rules.trace_clearance:.4f}mm. If this fails, the "
            f"synthetic geometry no longer reproduces Issue #4991."
        )

        # With foreign_pads supplied (the #4991 fix), the same call clips
        # to a length that respects clearance.
        fixed_safe_length = router._compute_max_safe_escape_length(
            pad=pin2,
            dx=1.0,
            dy=0.0,
            trace_width=rules.trace_width,
            package_pads=[pin1, pin2],
            min_clearance=rules.trace_clearance,
            max_length=1.0,
            foreign_pads=[foreign_c1_pin1],
        )
        assert fixed_safe_length < legacy_safe_length
        fixed_segment = Segment(
            x1=pin2.x,
            y1=pin2.y,
            x2=pin2.x + fixed_safe_length,
            y2=pin2.y,
            width=rules.trace_width,
            layer=Layer.F_CU,
            net=pin2.net,
            net_name=pin2.net_name,
        )
        fixed_gap = router._segment_to_pad_edge_gap(fixed_segment, foreign_c1_pin1)
        assert fixed_gap >= rules.trace_clearance - 1e-6


# ----------------------------------------------------------------------------
# Regression fixture: board09 four-terminal Kelvin-shunt land pattern
# ----------------------------------------------------------------------------


class TestBoard09KelvinShuntForeignPadRegression:
    """Issue #4991 acceptance criterion: a board09-style four-terminal
    Kelvin-shunt (RSH1, ``R_Shunt_Vishay_WSK2512_6332Metric_T1.19mm``)
    regression case using the real asymmetrical four-pad land pattern.

    On board09, RSH1's KELVIN_N sense pin (pin 3) sits close to the
    INA226 current-sense amplifier's SENSE_N pin -- close Kelvin-sense
    routing is the whole point of a 4-terminal shunt.  Before #4991,
    RSH1's radial escape for pin 3 could clip straight through SENSE_N's
    pad because ``_escape_radial`` never saw board-wide foreign pads.
    """

    def _build_scenario(self, foreign_x: float, foreign_size: float = 0.4):
        rules = _jlcpcb_rules(trace_width=0.25)
        router, grid = _make_router(rules, escape_clearance=1.0)
        rsh1_pads = _wsk2512_kelvin_shunt_pads()

        # Foreign pad simulating the INA226's SENSE_N pin (a different
        # component, different net), placed east of RSH1 pin 3
        # (KELVIN_N) -- pin 3's quadrant-outward radial escape direction
        # is EAST (dx=3.43 dominates dy=-1.27 relative to the package
        # center at the origin).
        foreign_sense_n = Pad(
            x=foreign_x,
            y=-1.27,
            width=foreign_size,
            height=foreign_size,
            net=50,
            net_name="SENSE_N",
            ref="U3",
            pin="9",
            layer=Layer.F_CU,
        )

        for pad in [*rsh1_pads, foreign_sense_n]:
            grid.add_pad(pad)

        return router, rules, rsh1_pads, foreign_sense_n

    def test_rsh1_is_not_classified_dense(self):
        """Sanity precondition: the WSK2512 land pattern must dispatch to
        ``_escape_radial`` (the code path this issue fixes), not a
        dense-package path."""
        rules = _jlcpcb_rules()
        rsh1_pads = _wsk2512_kelvin_shunt_pads()
        assert not is_dense_package(
            rsh1_pads, trace_width=rules.trace_width, clearance=rules.trace_clearance
        )

    def test_kelvin_n_escape_respects_foreign_sense_pad_clearance(self):
        """With the foreign SENSE_N pad close enough to intersect the
        unclipped launch path, every emitted escape segment must still
        keep full clearance from it."""
        router, rules, rsh1_pads, foreign_sense_n = self._build_scenario(foreign_x=4.5)

        info = router.analyze_package(rsh1_pads)
        escapes = router.generate_escapes(info)
        assert escapes, "Expected at least one pin to escape successfully"

        clearance = rules.trace_clearance
        for escape in escapes:
            for seg in escape.segments:
                if foreign_sense_n.layer != seg.layer and not foreign_sense_n.through_hole:
                    continue
                gap = router._segment_to_pad_edge_gap(seg, foreign_sense_n)
                assert gap >= clearance - 1e-6, (
                    f"Issue #4991 regression: {escape.pad.ref}.{escape.pad.pin} "
                    f"({escape.pad.net_name}) escape clips foreign pad "
                    f"{foreign_sense_n.ref}.{foreign_sense_n.pin} "
                    f"({foreign_sense_n.net_name}): gap={gap:.4f}mm, "
                    f"required={clearance:.4f}mm"
                )

        # KELVIN_N (pin 3) specifically must have been clipped shorter
        # than the unclipped request -- proving the foreign pad was the
        # binding constraint, not merely absent from the scene.
        kelvin_n_escape = next(
            (e for e in escapes if e.pad.pin == "3"),
            None,
        )
        if kelvin_n_escape is not None:
            seg = kelvin_n_escape.segments[0]
            import math

            length = math.hypot(seg.x2 - seg.x1, seg.y2 - seg.y1)
            assert length < router.escape_clearance, (
                "Expected KELVIN_N's escape to be clipped shorter than the "
                "unclipped request by the foreign SENSE_N pad"
            )

    def test_genuinely_infeasible_clearance_drops_pin_not_invalid_geometry(self, caplog):
        """When the foreign pad sits so close that even a minimally
        useful stub would violate clearance, the pin must be DROPPED
        (no escape emitted) rather than shipping clearance-violating
        geometry -- with an explicit diagnostic logged."""
        router, rules, rsh1_pads, foreign_sense_n = self._build_scenario(foreign_x=3.9)

        info = router.analyze_package(rsh1_pads)
        with caplog.at_level(logging.DEBUG, logger="kicad_tools.router.escape"):
            escapes = router.generate_escapes(info)

        kelvin_n_escape = next((e for e in escapes if e.pad.pin == "3"), None)
        assert kelvin_n_escape is None, (
            "Expected KELVIN_N's escape to be dropped rather than emit a "
            "clearance-violating stub against the very close foreign pad"
        )

        # Every escape that WAS emitted (the other 3 pins) must still
        # respect clearance against the foreign pad.
        clearance = rules.trace_clearance
        for escape in escapes:
            for seg in escape.segments:
                gap = router._segment_to_pad_edge_gap(seg, foreign_sense_n)
                assert gap >= clearance - 1e-6

        # An explicit diagnostic was logged for the dropped pin (never a
        # silent drop) -- Issue #4991 acceptance criterion.
        assert any(
            "4991" in record.message or "Issue #4991" in record.message for record in caplog.records
        ) or any("skipped" in record.message.lower() for record in caplog.records), (
            "Expected an explicit debug diagnostic for the dropped KELVIN_N "
            "escape, got: " + "\n".join(r.message for r in caplog.records)
        )
