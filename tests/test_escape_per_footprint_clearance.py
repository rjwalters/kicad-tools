"""Tests for issue #2755: per-footprint clearance check in escape routing.

The escape generators in ``EscapeRouter`` group pads by edge (QFP) or row
(SSOP) and drop plane-net pads (``net == 0``) before running the
segment-to-pad clearance check.  Before issue #2755 this meant:

  * An escape stub from the north edge of a TQFP could land on (or right
    next to) a pad on the east edge of the same package without being
    flagged.
  * A VCC / GND pad (filtered out because its net is 0) could be crossed
    by an escape stub for a neighbouring signal pin.

Both classes of mistake produce ``clearance_pad_segment`` violations in
the routed PCB.  The fix is to pass ``extra_pads`` (the rest of the
footprint's pads) to ``_segment_violates_pad_clearance``.

These tests pin the new behaviour by:

1. Driving ``_segment_violates_pad_clearance`` directly with a segment
   that obviously crosses a pad on a *different* edge of a TQFP and
   verifying it now returns ``True``.
2. Driving the same function with a segment that crosses a *plane-net*
   pad (the kind filtered out of the per-edge ``pads`` list) and
   verifying it now returns ``True``.
3. Exercising ``_other_footprint_pads`` and verifying it returns the
   complement of the row-pads against ``package.pads``.
"""

from kicad_tools.router.escape import EscapeRouter
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Pad, Segment
from kicad_tools.router.rules import DesignRules


def _make_router() -> EscapeRouter:
    """Construct an ``EscapeRouter`` with realistic JLCPCB-style rules."""
    rules = DesignRules(
        trace_width=0.2,
        trace_clearance=0.127,
        via_drill=0.3,
        via_diameter=0.6,
        grid_resolution=0.05,
        fine_pitch_clearance=0.127,
        fine_pitch_threshold=0.8,
        min_trace_width=0.127,
    )
    grid = RoutingGrid(
        width=40.0,
        height=40.0,
        rules=rules,
        origin_x=-20.0,
        origin_y=-20.0,
    )
    return EscapeRouter(grid, rules)


def _make_tqfp32_pads(
    pin_pitch: float = 0.8,
    pad_width: float = 0.5,
    pad_height: float = 1.2,
    body_size: float = 7.0,
    ref: str = "U1",
) -> tuple[list[Pad], list[Pad], list[Pad], list[Pad], list[Pad]]:
    """Build a TQFP-32 footprint matching the geometry described in the
    curator's analysis of board 03 (issue #2755).

    Returns: (all_pads, north_pads, south_pads, east_pads, west_pads).

    Pins are laid out CCW starting from south-west, KiCad-style:
        * pins 1-8: west edge (south -> north)
        * pins 9-16: north edge (west -> east)
        * pins 17-24: east edge (north -> south)
        * pins 25-32: south edge (east -> west)

    Pads on each edge are 1.2 mm long pointing OUTward (their *long*
    dimension is perpendicular to the edge), so e.g. west pads have
    width=1.2 height=0.5.  We pick nets so that pin 11 is on the north
    edge -- this matches the U1 in the issue (JOY_BTN at U1-11).
    """
    pads: list[Pad] = []
    west: list[Pad] = []
    north: list[Pad] = []
    east: list[Pad] = []
    south: list[Pad] = []

    pins_per_edge = 8
    edge_offset = body_size / 2  # +/- 3.5 mm
    total_span = (pins_per_edge - 1) * pin_pitch
    start_offset = -total_span / 2

    # West edge: x = -edge_offset, y increases (south->north)
    for i in range(pins_per_edge):
        p = Pad(
            x=-edge_offset,
            y=start_offset + i * pin_pitch,
            width=pad_height,
            height=pad_width,
            net=1 + i,
            net_name=f"NET{1 + i}",
            ref=ref,
            pin=str(1 + i),
            layer=Layer.F_CU,
        )
        west.append(p)
        pads.append(p)

    # North edge: y = +edge_offset, x increases (west->east)
    for i in range(pins_per_edge):
        p = Pad(
            x=start_offset + i * pin_pitch,
            y=edge_offset,
            width=pad_width,
            height=pad_height,
            net=9 + i,
            net_name=f"NET{9 + i}",
            ref=ref,
            pin=str(9 + i),
            layer=Layer.F_CU,
        )
        north.append(p)
        pads.append(p)

    # East edge: x = +edge_offset, y decreases (north->south)
    for i in range(pins_per_edge):
        p = Pad(
            x=edge_offset,
            y=-start_offset - i * pin_pitch,
            width=pad_height,
            height=pad_width,
            net=17 + i,
            net_name=f"NET{17 + i}",
            ref=ref,
            pin=str(17 + i),
            layer=Layer.F_CU,
        )
        east.append(p)
        pads.append(p)

    # South edge: y = -edge_offset, x decreases (east->west)
    for i in range(pins_per_edge):
        p = Pad(
            x=-start_offset - i * pin_pitch,
            y=-edge_offset,
            width=pad_width,
            height=pad_height,
            net=25 + i,
            net_name=f"NET{25 + i}",
            ref=ref,
            pin=str(25 + i),
            layer=Layer.F_CU,
        )
        south.append(p)
        pads.append(p)

    return pads, north, south, east, west


class TestOtherFootprintPadsHelper:
    """``_other_footprint_pads`` returns the complement of the row-pads."""

    def test_complement_of_north_edge_includes_other_edges(self):
        router = _make_router()
        all_pads, north, _south, _east, _west = _make_tqfp32_pads()
        package = router.analyze_package(all_pads)

        extras = router._other_footprint_pads(package, north)

        north_ids = {id(p) for p in north}
        extra_ids = {id(p) for p in extras}

        # Extras must not include any north-edge pad.
        assert north_ids.isdisjoint(extra_ids), "North-edge pads must NOT appear in extras"
        # Extras must include the rest of the TQFP-32 pads (24 of them).
        assert len(extras) == len(all_pads) - len(north), (
            f"Expected {len(all_pads) - len(north)} extras, got {len(extras)}"
        )

    def test_complement_treats_plane_pads_as_extras(self):
        """Plane-net pads (net=0) that the QFP escape filter drops are
        returned as extras and therefore checked for clearance."""
        router = _make_router()
        all_pads, north, _south, _east, _west = _make_tqfp32_pads()
        # Mark the middle north-edge pad as a plane pad (net=0). The QFP
        # escape pass would normally filter this out of the per-edge list.
        plane_pad = all_pads[len(_make_tqfp32_pads()[1]) + 3]  # 4th north pad
        plane_pad.net = 0
        plane_pad.net_name = "GND"

        package = router.analyze_package(all_pads)
        # Simulate the QFP escape behavior: north list excludes net=0 pads.
        signal_north = [p for p in north if p.net != 0]

        extras = router._other_footprint_pads(package, signal_north)
        assert any(p is plane_pad for p in extras), (
            "Plane-net pad must be returned as an extra so the clearance check sees it"
        )


class TestSegmentClearanceAgainstExtraPads:
    """``_segment_violates_pad_clearance(extra_pads=...)`` flags violations
    against pads on other edges and against plane-net pads."""

    def test_segment_crossing_other_edge_pad_is_flagged(self):
        """A diagonal escape stub from a north pad that crosses an east-edge
        pad must be reported as violating clearance when ``extra_pads`` is
        passed (was previously silent because east pads were in a different
        bucket).
        """
        router = _make_router()
        all_pads, north, _south, east, _west = _make_tqfp32_pads()

        # Source pad: rightmost north pad (closest to the east edge).
        source = north[-1]
        # Target a pad on the east edge (top-most -- nearest the source).
        victim = east[0]

        # Construct a segment from the source pad center to a point that
        # lands very close to ``victim`` (well inside its half-extent).
        seg = Segment(
            x1=source.x,
            y1=source.y,
            x2=victim.x,
            y2=victim.y,
            width=0.2,
            layer=Layer.F_CU,
            net=source.net,
            net_name=source.net_name,
        )

        # Without extras: only the north-row is checked -- victim is on the
        # east row, so the function under the OLD behavior would not see
        # it.  This is the bug.
        old_result = router._segment_violates_pad_clearance(
            seg,
            len(north) - 1,
            north,
            router.rules.trace_clearance,
        )
        assert old_result is False, (
            "Sanity: row-only check should miss the east-edge pad (this is the pre-#2755 behavior)"
        )

        # With extras (post-#2755 fix): the east-edge pad must be flagged.
        extras = router._other_footprint_pads(
            router.analyze_package(all_pads),
            north,
        )
        new_result = router._segment_violates_pad_clearance(
            seg,
            len(north) - 1,
            north,
            router.rules.trace_clearance,
            extra_pads=extras,
        )
        assert new_result is True, (
            "Issue #2755: escape stub crossing a pad on a DIFFERENT edge "
            "of the same footprint must be flagged as a clearance violation"
        )

    def test_segment_crossing_plane_pad_is_flagged(self):
        """A stub running through a VCC/GND pad (filtered out as net=0)
        must be flagged once ``extra_pads`` is supplied.

        We park the segment on the EAST edge (where there are no signal
        pads in the simulated north row) so that the only way the check
        can flag the violation is via the ``extra_pads`` path -- i.e. the
        actual #2755 code path for plane/other-edge pads.
        """
        router = _make_router()
        all_pads, north, _south, east, _west = _make_tqfp32_pads()

        # Pick an EAST-edge pad and mark it as a plane pad (net=0).
        plane_pad = east[0]
        plane_pad.net = 0
        plane_pad.net_name = "GND"

        # Simulate the QFP filter: signal-only east pads (the source list
        # for an escape pass running along the east edge).
        signal_east = [p for p in east if p.net != 0]
        source = signal_east[0]

        # Construct a segment that ends right on top of plane_pad
        # (which is no longer in ``signal_east`` because net=0).
        seg = Segment(
            x1=source.x,
            y1=source.y,
            x2=plane_pad.x,
            y2=plane_pad.y,
            width=0.2,
            layer=Layer.F_CU,
            net=source.net,
            net_name=source.net_name,
        )

        # Pre-fix behaviour: plane_pad is not in signal_east, so the
        # row-only check misses it.
        old_result = router._segment_violates_pad_clearance(
            seg,
            0,
            signal_east,
            router.rules.trace_clearance,
        )
        assert old_result is False, "Sanity: pre-#2755 row-only check should miss a plane-net pad"

        # Post-fix behaviour: extras include plane_pad -> violation found.
        extras = router._other_footprint_pads(
            router.analyze_package(all_pads),
            signal_east,
        )
        new_result = router._segment_violates_pad_clearance(
            seg,
            0,
            signal_east,
            router.rules.trace_clearance,
            extra_pads=extras,
        )
        assert new_result is True, (
            "Issue #2755: escape stub crossing a plane-net pad must be "
            "flagged as a clearance violation"
        )

    def test_source_pad_skipped_by_identity(self):
        """The source pad must be skipped even if it appears in both the
        row list and the extras (defensive against caller mistakes)."""
        router = _make_router()
        all_pads, north, _south, _east, _west = _make_tqfp32_pads()

        source = north[0]
        seg = Segment(
            x1=source.x,
            y1=source.y,
            x2=source.x,
            y2=source.y + 1.0,  # short outward stub
            width=0.2,
            layer=Layer.F_CU,
            net=source.net,
            net_name=source.net_name,
        )

        # Put source in extras as well as in pads -- it must still be
        # skipped by object identity (no false positive).
        result = router._segment_violates_pad_clearance(
            seg,
            0,
            north,
            router.rules.trace_clearance,
            extra_pads=[source],
        )
        assert result is False, (
            "Source pad must be skipped by identity even if it appears "
            "in both ``pads`` and ``extra_pads``"
        )

    def test_segment_not_crossing_anything_is_not_flagged(self):
        """A clean escape stub that clears every pad on the footprint
        must NOT be flagged (no false positives)."""
        router = _make_router()
        all_pads, north, _south, _east, _west = _make_tqfp32_pads()

        source = north[0]
        # Escape straight outward (north), well clear of every pad.
        seg = Segment(
            x1=source.x,
            y1=source.y,
            x2=source.x,
            y2=source.y + 5.0,
            width=0.2,
            layer=Layer.F_CU,
            net=source.net,
            net_name=source.net_name,
        )

        extras = router._other_footprint_pads(
            router.analyze_package(all_pads),
            north,
        )
        result = router._segment_violates_pad_clearance(
            seg,
            0,
            north,
            router.rules.trace_clearance,
            extra_pads=extras,
        )
        assert result is False, "Clean outward escape stub must not be flagged"

    def test_extra_pads_layer_filter(self):
        """Pads on a different layer must not produce false positives."""
        router = _make_router()
        all_pads, north, _south, _east, _west = _make_tqfp32_pads()

        # Move the east-edge pad to the back layer so it is on a
        # different layer than the segment.
        for p in _east:
            p.layer = Layer.B_CU

        source = north[-1]
        victim = _east[0]
        seg = Segment(
            x1=source.x,
            y1=source.y,
            x2=victim.x,
            y2=victim.y,
            width=0.2,
            layer=Layer.F_CU,
            net=source.net,
            net_name=source.net_name,
        )

        extras = router._other_footprint_pads(
            router.analyze_package(all_pads),
            north,
        )
        result = router._segment_violates_pad_clearance(
            seg,
            len(north) - 1,
            north,
            router.rules.trace_clearance,
            extra_pads=extras,
        )
        # The east pad is on B.Cu now -- it must NOT be flagged as a
        # clearance violation against an F.Cu segment.
        assert result is False, "Pads on a different layer must not be flagged"
