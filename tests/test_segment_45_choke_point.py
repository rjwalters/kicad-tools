"""Issue #3907: by-construction 45-degree enforcement at the emission choke point.

``Segment.to_sexp`` is the single serialization point every
router-emitted segment flows through.  These tests pin the
by-construction guard (``verify_segment_45`` /
``OffAngleSegmentError``), the enforcement toggle, and the subgrid
escape emitter that now doglegs off-angle stubs at construction time.
"""

from __future__ import annotations

import pytest

from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import (
    Segment,
    enable_segment_45_enforcement,
    is_segment_45_enforcement_enabled,
    segment_45_enforcement_disabled,
)
from kicad_tools.router.quantize import (
    ANGLE_TOL_DEG,
    OffAngleSegmentError,
    off_angle_degrees,
    verify_segment_45,
)


class TestVerifySegment45:
    def test_axis_aligned_passes(self) -> None:
        verify_segment_45(0.0, 0.0, 5.0, 0.0)
        verify_segment_45(1.0, 2.0, 1.0, 9.0)

    def test_diagonal_passes(self) -> None:
        verify_segment_45(0.0, 0.0, 3.0, 3.0)
        verify_segment_45(0.0, 0.0, -2.5, 2.5)

    def test_zero_length_passes(self) -> None:
        # No direction -- nothing to quantize.
        verify_segment_45(4.2, 4.2, 4.2, 4.2)

    def test_sub_tolerance_rounding_passes(self) -> None:
        # Off by less than a rounding step at 4dp -> serialized as legal.
        verify_segment_45(0.0, 0.0, 3.0, 3.00001)

    def test_off_angle_raises(self) -> None:
        with pytest.raises(OffAngleSegmentError) as exc:
            verify_segment_45(139.75, 108.0, 139.5, 107.5)
        assert exc.value.off_deg == pytest.approx(18.4349, abs=1e-3)

    def test_error_carries_context(self) -> None:
        with pytest.raises(OffAngleSegmentError) as exc:
            verify_segment_45(0.0, 0.0, 3.0, 1.0, context="net 7 on F.Cu")
        assert "net 7 on F.Cu" in str(exc.value)

    def test_checks_serialized_4dp_coordinates(self) -> None:
        # Analytic angle is legal-ish but the 4dp serialization is what
        # the census reads; verify the guard rounds like ``to_sexp``.
        # 1e-6 perturbation vanishes at 4dp -> legal.
        verify_segment_45(0.0, 0.0, 2.0, 2.000001)


class TestSegmentToSexpEnforcement:
    def test_legal_segment_serializes(self) -> None:
        s = Segment(0.0, 0.0, 1.0, 1.0, 0.2, Layer.F_CU, net=3)
        text = s.to_sexp()
        assert text.startswith("(segment")
        assert "(net 3)" in text

    def test_off_angle_segment_raises_by_default(self) -> None:
        s = Segment(0.0, 0.0, 3.0, 1.0, 0.2, Layer.F_CU, net=4)
        with pytest.raises(OffAngleSegmentError):
            s.to_sexp()

    def test_enforcement_toggle_round_trips(self) -> None:
        assert is_segment_45_enforcement_enabled() is True
        s = Segment(0.0, 0.0, 3.0, 1.0, 0.2, Layer.F_CU, net=4)
        try:
            enable_segment_45_enforcement(False)
            assert is_segment_45_enforcement_enabled() is False
            # Now the skewed segment serializes without raising.
            assert s.to_sexp().startswith("(segment")
        finally:
            enable_segment_45_enforcement(True)
        assert is_segment_45_enforcement_enabled() is True

    def test_context_manager_scopes_and_restores(self) -> None:
        s = Segment(0.0, 0.0, 3.0, 1.0, 0.2, Layer.F_CU, net=4)
        assert is_segment_45_enforcement_enabled() is True
        with segment_45_enforcement_disabled():
            assert is_segment_45_enforcement_enabled() is False
            s.to_sexp()  # no raise inside the scope
        assert is_segment_45_enforcement_enabled() is True
        with pytest.raises(OffAngleSegmentError):
            s.to_sexp()

    def test_context_manager_restores_on_exception(self) -> None:
        assert is_segment_45_enforcement_enabled() is True
        with pytest.raises(RuntimeError), segment_45_enforcement_disabled():
            raise RuntimeError("boom")
        assert is_segment_45_enforcement_enabled() is True

    def test_serialized_output_is_within_tolerance(self) -> None:
        # Every legal segment that serializes must be within ANGLE_TOL_DEG
        # of the 45-set as WRITTEN (4dp).
        s = Segment(1.2345, 6.7891, 4.2345, 9.7891, 0.2, Layer.F_CU, net=1)
        text = s.to_sexp()
        assert text  # serialized without raising
        assert off_angle_degrees(4.2345 - 1.2345, 9.7891 - 6.7891) <= ANGLE_TOL_DEG


class TestSubgridEscapeDoglegsByConstruction:
    """The subgrid escape emitter (a named #3907 leak) now doglegs."""

    def test_off_angle_escape_route_is_45_legal(self) -> None:
        from kicad_tools.router.primitives import Pad
        from kicad_tools.router.subgrid import SubGridEscape

        pad = Pad(x=139.75, y=108.0, width=0.3, height=0.3, net=4, net_name="USB_D+")
        # An off-angle pad-centre -> grid-snap chord.
        seg = Segment(139.75, 108.0, 139.5, 107.5, 0.2, Layer.F_CU, net=4)
        # dogleg_mid as the emitter would compute it.
        from kicad_tools.router.quantize import dogleg_points

        pts = dogleg_points(139.75, 108.0, 139.5, 107.5)
        assert len(pts) == 3
        escape = SubGridEscape(
            pad=pad,
            segment=seg,
            grid_point=(0, 0),
            snap_point=(139.5, 107.5),
            dogleg_mid=pts[1],
        )

        # get_escape_routes must split into two 45-legal legs that
        # serialize cleanly through the choke point.
        from kicad_tools.router.subgrid import SubGridResult, SubGridRouter  # noqa: F401

        # Build a minimal SubGridResult carrying just this escape and call
        # the pure conversion via a lightweight object.
        result = SubGridResult(escapes=[escape])
        # get_escape_routes is an instance method but only touches
        # ``result``; call it on an unconfigured router shell.
        router = SubGridRouter.__new__(SubGridRouter)
        routes = SubGridRouter.get_escape_routes(router, result)
        assert len(routes) == 1
        segs = routes[0].segments
        assert len(segs) == 2, "off-angle escape should be a two-leg dogleg"
        for leg in segs:
            # Each leg is 45-legal by construction -> serializes cleanly.
            assert leg.to_sexp().startswith("(segment")
        # Endpoints preserved: first leg starts at pad, last ends at snap.
        assert (segs[0].x1, segs[0].y1) == (139.75, 108.0)
        assert (segs[-1].x2, segs[-1].y2) == (139.5, 107.5)
        # Legs meet at the shared dogleg vertex.
        assert (segs[0].x2, segs[0].y2) == (segs[1].x1, segs[1].y1)

    def test_axis_aligned_escape_stays_single_segment(self) -> None:
        from kicad_tools.router.primitives import Pad
        from kicad_tools.router.subgrid import (
            SubGridEscape,
            SubGridResult,
            SubGridRouter,
        )

        pad = Pad(x=10.0, y=10.0, width=0.3, height=0.3, net=2, net_name="N")
        seg = Segment(10.0, 10.0, 12.0, 10.0, 0.2, Layer.F_CU, net=2)
        escape = SubGridEscape(
            pad=pad,
            segment=seg,
            grid_point=(0, 0),
            snap_point=(12.0, 10.0),
            dogleg_mid=None,
        )
        result = SubGridResult(escapes=[escape])
        router = SubGridRouter.__new__(SubGridRouter)
        routes = SubGridRouter.get_escape_routes(router, result)
        assert len(routes[0].segments) == 1
