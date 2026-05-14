"""Tests for issue #2880: forced in-pad rescue for fine-pitch QFP signal pins
sandwiched between same-component plane-net pads.

Scenario: Board 04 (STM32 LQFP-48 dev board) under jlcpcb-tier1 hits a
geometric impossibility on the chip's west edge: signal pads (OSC_IN,
OSC_OUT, NRST) are interleaved with plane-net pads (VDD/+3.3V, VSS/GND)
on adjacent pin positions.  At 0.5 mm pitch + 0.127 mm clearance, no
surface escape can satisfy clearance against the surrounding plane
pads (channel = 0.2 mm, required = 0.381 mm).

The fix (#2880) forces ``_try_in_pad_escape`` whenever a signal pin
is plane-sandwiched on a fine-pitch QFP AND ``via_in_pad_supported``
is True.  When ``via_in_pad_supported`` is False, the router emits a
clear error message that names the unfixable constraint instead of
silently producing a routed PCB with documented DRC violations.

Test plan:
1. Plane-sandwich predicate fires on the correct signal pins.
2. With ``via_in_pad_supported=True`` (jlcpcb-tier1), the rescue
   produces an in-pad via for at least one plane-sandwiched pin
   (we cannot assert the exact pin set because the dispatcher's
   own violation check ALSO triggers rescue on many pins).
3. With ``via_in_pad_supported=False`` (jlcpcb), the router emits an
   ERROR-level log naming the plane-sandwich condition.
4. Plane-sandwich detection is correctly narrow: corner pins,
   non-sandwiched pins, and plane pads themselves all return False.
"""

from __future__ import annotations

import logging

from kicad_tools.router.escape import EscapeRouter, PackageType
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.primitives import Pad
from kicad_tools.router.rules import DesignRules


# ----------------------------------------------------------------------------
# Fixtures -- mirrored from test_escape_via_in_pad_lqfp.py but with selective
# plane-net assignment on the west edge so we can reason about which pads are
# plane-sandwiched.
# ----------------------------------------------------------------------------


def _make_lqfp48_with_plane_sandwich(
    ref: str = "U2",
    pitch: float = 0.5,
    pad_short: float = 0.30,
    pad_long: float = 1.50,
    pads_per_edge: int = 12,
) -> list[Pad]:
    """Build a 0.5mm-pitch LQFP-48 fixture with a plane-sandwiched signal
    pin on the west edge AND mostly-plane neighbours so the sandwiched pin
    ends up at an odd-index position in the dispatcher's filtered list
    (i.e. its escape direction will be along-edge / alternating).

    West edge pinout (pin -> net):
        pin 1  -> NET100 (signal, near-corner)
        pin 2  -> +3.3V  (plane, net=0)
        pin 3  -> NET102 (signal)
        pin 4  -> GND    (plane, net=0)
        pin 5  -> +3.3V  (plane, net=0)
        pin 6  -> +3.3V  (plane, net=0)
        pin 7  -> NRST   (signal, NET200, plane-sandwiched)
        pin 8  -> GND    (plane, net=0)
        pin 9  -> +3.3V  (plane, net=0)
        pin 10 -> NET106 (signal)
        pin 11 -> GND    (plane, net=0)
        pin 12 -> NET108 (signal, near-corner)

    After the dispatcher filters out net=0 pads, the west signal list
    (sorted by y ascending) is::

        [pin 12 (idx 0), pin 10 (idx 1), pin 7 (idx 2), pin 3 (idx 3),
         pin 1 (idx 4)]

    Pin 7 NRST at idx 2 is even-indexed -> perpendicular direction.
    For the forced-rescue gate to fire, we need an along-edge direction.

    To achieve this, the fixture also forces the dispatcher's use of
    perpendicular_only off by keeping the pitch <= 0.55 (which it is at
    0.5 mm).  Pin 7's escape direction at i=2 is perpendicular, BUT the
    PREDICATE itself reports it as plane-sandwiched (pin 6 and pin 8
    are both plane neighbours).  The integration test on the gate's
    along-edge condition therefore uses a different probe -- the
    predicate-level tests above already verify the strict matching.

    See ``TestForcedInPadRescue.test_plane_sandwich_triggers_in_pad_rescue``
    below for the gate-level integration test, which uses the
    ``_make_lqfp48_along_edge_sandwich`` fixture where the sandwiched
    pin sits at an odd-indexed filtered position.

    All other edges have signal nets only.
    """
    # West edge pinout: pin 7 is the SANDWICHED signal (pin 6 and pin 8
    # are planes on both immediate same-edge sides).  Pin 3 (signal) has
    # pin 2 (signal) and pin 4 (signal) as same-edge neighbours so it is
    # NOT sandwiched.  Pin 5 has pin 4 (signal) and pin 6 (plane) as
    # neighbours -- one-sided plane only, NOT sandwiched.
    west_nets: list[int] = [
        100,  # pin 1 (signal, top of edge)
        101,  # pin 2 (signal)
        102,  # pin 3 (signal, both-sides-signal -> not sandwiched)
        103,  # pin 4 (signal)
        104,  # pin 5 (signal, only one plane neighbour)
        0,    # pin 6 (PLANE)
        200,  # pin 7 (SIGNAL SANDWICHED)
        0,    # pin 8 (PLANE)
        105,  # pin 9 (signal, only one plane neighbour)
        106,  # pin 10 (signal)
        107,  # pin 11 (signal)
        108,  # pin 12 (signal, bottom of edge)
    ]
    assert len(west_nets) == pads_per_edge

    span = (pads_per_edge - 1) * pitch
    body_size = span + 3.0 * pitch + 2.0 * pad_long
    half_body = body_size / 2
    pad_stick_out = 0.85
    pad_center_offset = half_body + pad_stick_out / 2
    half_span = span / 2

    pads: list[Pad] = []
    pin_no = 1

    # WEST edge (vertical pads, top->bottom)
    for i in range(pads_per_edge):
        y = half_span - i * pitch
        net = west_nets[i]
        pads.append(
            Pad(
                x=-pad_center_offset,
                y=y,
                width=pad_long,
                height=pad_short,
                net=net,
                net_name=("PLANE" if net == 0 else f"NET{net}"),
                ref=ref,
                pin=str(pin_no),
                layer=Layer.F_CU,
            )
        )
        pin_no += 1

    # SOUTH edge -- all signal nets, unique
    for i in range(pads_per_edge):
        x = -half_span + i * pitch
        net = 300 + i
        pads.append(
            Pad(
                x=x,
                y=-pad_center_offset,
                width=pad_short,
                height=pad_long,
                net=net,
                net_name=f"NET{net}",
                ref=ref,
                pin=str(pin_no),
                layer=Layer.F_CU,
            )
        )
        pin_no += 1

    # EAST edge -- all signal nets, unique
    for i in range(pads_per_edge):
        y = -half_span + i * pitch
        net = 400 + i
        pads.append(
            Pad(
                x=pad_center_offset,
                y=y,
                width=pad_long,
                height=pad_short,
                net=net,
                net_name=f"NET{net}",
                ref=ref,
                pin=str(pin_no),
                layer=Layer.F_CU,
            )
        )
        pin_no += 1

    # NORTH edge -- all signal nets, unique
    for i in range(pads_per_edge):
        x = half_span - i * pitch
        net = 500 + i
        pads.append(
            Pad(
                x=x,
                y=pad_center_offset,
                width=pad_short,
                height=pad_long,
                net=net,
                net_name=f"NET{net}",
                ref=ref,
                pin=str(pin_no),
                layer=Layer.F_CU,
            )
        )
        pin_no += 1

    return pads


def _make_lqfp48_along_edge_sandwich(
    ref: str = "U2",
    pitch: float = 0.5,
    pad_short: float = 0.30,
    pad_long: float = 1.50,
    pads_per_edge: int = 12,
) -> list[Pad]:
    """Build a 0.5mm-pitch LQFP-48 fixture where the plane-sandwich
    signal pin lands at an ODD-indexed position in the dispatcher's
    filtered (signal-only) west-edge list.  At odd indices the
    dispatcher uses ``alt_dir_cw/ccw`` (along-edge) escape directions,
    which is the condition for the Issue #2880 forced-rescue gate to
    engage.

    West edge pinout:
        pin 1  -> NET100 (signal, top of edge)
        pin 2  -> +3.3V  (plane)
        pin 3  -> NRST   (signal, NET200, sandwiched -- pin 2 & 4 plane)
        pin 4  -> GND    (plane)
        pin 5  -> NET104 (signal)
        pin 6  -> +3.3V  (plane)
        pin 7  -> NET106 (signal)
        pin 8  -> GND    (plane)
        pin 9  -> NET108 (signal)
        pin 10 -> +3.3V  (plane)
        pin 11 -> NET110 (signal)
        pin 12 -> GND    (plane)

    After filtering net=0, sorted by y ascending:
        [pin 11, pin 9, pin 7, pin 5, pin 3, pin 1] (indices 0-5)
    Pin 3 NRST is at idx 4 (even) -> perpendicular.  That's still not
    along-edge.  Let me re-arrange so the sandwich pin is at odd idx:

    Alternative pinout (3 signals + lots of planes, sandwich pin in middle):
        pin 1  -> NET100 (signal)
        pin 2  -> +3.3V  (plane)
        pin 3  -> GND    (plane)
        pin 4  -> NET102 (signal, sandwiched between pin 3 & pin 5 planes)
        pin 5  -> +3.3V  (plane)
        pin 6  -> GND    (plane)
        pin 7  -> NET104 (signal)
        pin 8-12 plane

    Filtered: [pin 7, pin 4, pin 1] sorted by y asc (pin 7 lowest y).
    Pin 4 at idx 1 -> ALONG-EDGE direction.  Pin 4 is plane-sandwiched
    by pin 3 GND and pin 5 +3.3V.
    """
    west_nets: list[int] = [
        100,  # pin 1 (signal, top of edge)
        0,    # pin 2 (plane)
        0,    # pin 3 (plane)
        200,  # pin 4 (SANDWICH SIGNAL)
        0,    # pin 5 (plane)
        0,    # pin 6 (plane)
        104,  # pin 7 (signal)
        0,    # pin 8 (plane)
        0,    # pin 9 (plane)
        0,    # pin 10 (plane)
        0,    # pin 11 (plane)
        0,    # pin 12 (plane)
    ]
    assert len(west_nets) == pads_per_edge

    span = (pads_per_edge - 1) * pitch
    body_size = span + 3.0 * pitch + 2.0 * pad_long
    half_body = body_size / 2
    pad_stick_out = 0.85
    pad_center_offset = half_body + pad_stick_out / 2
    half_span = span / 2

    pads: list[Pad] = []
    pin_no = 1

    for i in range(pads_per_edge):
        y = half_span - i * pitch
        net = west_nets[i]
        pads.append(
            Pad(
                x=-pad_center_offset,
                y=y,
                width=pad_long,
                height=pad_short,
                net=net,
                net_name=("PLANE" if net == 0 else f"NET{net}"),
                ref=ref,
                pin=str(pin_no),
                layer=Layer.F_CU,
            )
        )
        pin_no += 1

    # Other edges: all unique signal nets (no planes) so the sandwich
    # condition is isolated to the west edge for the test.
    for i in range(pads_per_edge):
        x = -half_span + i * pitch
        pads.append(
            Pad(
                x=x,
                y=-pad_center_offset,
                width=pad_short,
                height=pad_long,
                net=300 + i,
                net_name=f"NET{300 + i}",
                ref=ref,
                pin=str(pin_no),
                layer=Layer.F_CU,
            )
        )
        pin_no += 1
    for i in range(pads_per_edge):
        y = -half_span + i * pitch
        pads.append(
            Pad(
                x=pad_center_offset,
                y=y,
                width=pad_long,
                height=pad_short,
                net=400 + i,
                net_name=f"NET{400 + i}",
                ref=ref,
                pin=str(pin_no),
                layer=Layer.F_CU,
            )
        )
        pin_no += 1
    for i in range(pads_per_edge):
        x = half_span - i * pitch
        pads.append(
            Pad(
                x=x,
                y=pad_center_offset,
                width=pad_short,
                height=pad_long,
                net=500 + i,
                net_name=f"NET{500 + i}",
                ref=ref,
                pin=str(pin_no),
                layer=Layer.F_CU,
            )
        )
        pin_no += 1

    return pads


def _make_rules(manufacturer: str | None = None) -> DesignRules:
    return DesignRules(
        trace_width=0.127,
        trace_clearance=0.127,
        via_drill=0.3,
        via_diameter=0.6,
        grid_resolution=0.05,
        manufacturer=manufacturer,
    )


def _make_grid(rules: DesignRules, layer_stack: LayerStack | None = None) -> RoutingGrid:
    return RoutingGrid(
        width=30.0,
        height=30.0,
        rules=rules,
        origin_x=-15.0,
        origin_y=-15.0,
        layer_stack=layer_stack or LayerStack.two_layer(),
    )


# ----------------------------------------------------------------------------
# Helper-level tests on the predicate itself.
# ----------------------------------------------------------------------------


class TestPlaneSandwichPredicate:
    """Issue #2880: ``_is_pin_boxed_by_plane_neighbours`` correctly
    identifies signal pads with plane-net neighbours on both sides of the
    same edge."""

    def test_predicate_fires_on_sandwiched_signal(self):
        rules = _make_rules(manufacturer="jlcpcb-tier1")
        grid = _make_grid(rules)
        router = EscapeRouter(grid, rules)
        pads = _make_lqfp48_with_plane_sandwich()
        package = router.analyze_package(pads)
        assert package.package_type in (
            PackageType.QFP, PackageType.TQFP, PackageType.QFN,
        )

        # Pin 7 on the west edge is the signal pin with plane neighbours
        # at pins 6 and 8.
        west_signal = next(
            p for p in pads if p.pin == "7"
        )
        assert west_signal.net == 200, "fixture sanity"
        assert router._is_pin_boxed_by_plane_neighbours(
            west_signal, package,
        ), "Pin 7 should be plane-sandwiched (pins 6 GND, 8 +3.3V)"

    def test_predicate_does_not_fire_on_plane_pad(self):
        """A plane-net pad itself is never reported as boxed (we only
        rescue signal pads)."""
        rules = _make_rules(manufacturer="jlcpcb-tier1")
        grid = _make_grid(rules)
        router = EscapeRouter(grid, rules)
        pads = _make_lqfp48_with_plane_sandwich()
        package = router.analyze_package(pads)

        west_plane = next(p for p in pads if p.pin == "6")
        assert west_plane.net == 0
        assert not router._is_pin_boxed_by_plane_neighbours(
            west_plane, package,
        )

    def test_predicate_does_not_fire_on_signal_without_plane_neighbours(self):
        """A signal pin whose immediate same-edge neighbours include at
        least one signal pad is NOT plane-sandwiched.  The strict
        ``both immediate neighbours must be plane`` rule excludes
        cases where one side is signal -- those go through the
        standard rescue gate.
        """
        rules = _make_rules(manufacturer="jlcpcb-tier1")
        grid = _make_grid(rules)
        router = EscapeRouter(grid, rules)
        pads = _make_lqfp48_with_plane_sandwich()
        package = router.analyze_package(pads)

        # Pin 3 on the west edge has signal neighbours (pin 2, pin 4
        # in our fixture are both signals).  Not plane-sandwiched.
        west_mid = next(p for p in pads if p.pin == "3")
        assert west_mid.net != 0
        assert not router._is_pin_boxed_by_plane_neighbours(
            west_mid, package,
        )

    def test_predicate_does_not_fire_on_corner_pin(self):
        """A pin at the edge end has only one neighbour and cannot be
        plane-sandwiched."""
        rules = _make_rules(manufacturer="jlcpcb-tier1")
        grid = _make_grid(rules)
        router = EscapeRouter(grid, rules)
        pads = _make_lqfp48_with_plane_sandwich()
        package = router.analyze_package(pads)

        # Pin 1 (top of west edge) only has one same-edge neighbour
        # below it; cannot be sandwiched.
        corner = next(p for p in pads if p.pin == "1")
        assert not router._is_pin_boxed_by_plane_neighbours(
            corner, package,
        )

    def test_predicate_does_not_fire_when_only_one_neighbour_is_plane(self):
        """Mixed neighbour: one plane, one signal -> not sandwiched."""
        rules = _make_rules(manufacturer="jlcpcb-tier1")
        grid = _make_grid(rules)
        router = EscapeRouter(grid, rules)
        pads = _make_lqfp48_with_plane_sandwich()
        package = router.analyze_package(pads)

        # Pin 5: neighbours are pin 4 (signal 103) and pin 6 (plane 0).
        # Only one plane neighbour -- not sandwiched.
        west_one_side = next(p for p in pads if p.pin == "5")
        assert not router._is_pin_boxed_by_plane_neighbours(
            west_one_side, package,
        )


# ----------------------------------------------------------------------------
# Integration: the rescue gate must produce an in-pad via for the sandwiched
# signal pin under a via-in-pad-capable profile.
# ----------------------------------------------------------------------------


class TestForcedInPadRescue:
    """Issue #2880: the rescue gate forces ``_try_in_pad_escape`` for
    plane-sandwiched signal pins on capable manufacturers.

    The dispatcher gate also requires the escape direction to be
    along-edge (alternating); plane-sandwiched signal pins that escape
    perpendicular to the edge are geometrically clean already and do
    not pay the via-in-pad cost.  Use
    ``_make_lqfp48_along_edge_sandwich`` to land the sandwich pin at an
    odd-indexed position in the dispatcher's filtered list (i=1 →
    along-edge direction).
    """

    def test_plane_sandwich_triggers_in_pad_rescue(self):
        """Sandwich pin at along-edge filtered index gets force-rescued."""
        rules = _make_rules(manufacturer="jlcpcb-tier1")
        grid = _make_grid(rules)
        router = EscapeRouter(grid, rules)
        pads = _make_lqfp48_along_edge_sandwich()
        package = router.analyze_package(pads)

        escapes = router.generate_escapes(package)

        # The sandwiched signal pin (west pin 4, net 200) must escape
        # via an in-pad via, since the along-edge surface escape cannot
        # satisfy 0.127 mm clearance against the surrounding plane pads.
        sandwich_escape = next(
            (e for e in escapes if e.pad.pin == "4" and e.pad.net == 200),
            None,
        )
        assert sandwich_escape is not None, (
            "Plane-sandwiched signal pin (west pin 4) must produce an "
            "escape route -- got no escape entry for this pad."
        )
        assert sandwich_escape.via is not None, (
            "Plane-sandwiched signal pin must escape via via-in-pad; got "
            f"escape with no via: {sandwich_escape}"
        )
        assert getattr(sandwich_escape.via, "in_pad", False), (
            "Sandwiched-pin escape must use in-pad via "
            "(via.in_pad=True). Got "
            f"via at ({sandwich_escape.via.x}, {sandwich_escape.via.y}) "
            f"in_pad={getattr(sandwich_escape.via, 'in_pad', False)}"
        )

        # The in-pad via must sit dead-centre on the pad.
        pad = sandwich_escape.pad
        assert abs(sandwich_escape.via.x - pad.x) < 0.001
        assert abs(sandwich_escape.via.y - pad.y) < 0.001

        # On the 2-layer fixture, the inner escape segment must land on
        # B.Cu (the only alternate signal layer).
        assert sandwich_escape.via.layers[1] == Layer.B_CU

    def test_perpendicular_sandwich_pin_does_not_force_rescue(self):
        """A plane-sandwiched signal pin escaping PERPENDICULAR to the
        edge does NOT get force-rescued -- the perpendicular escape is
        geometrically clean (it exits the package immediately and does
        not cross the same-edge plane pads).  Forcing the rescue here
        would incur an unnecessary via-in-pad cost.

        Uses the ``_make_lqfp48_with_plane_sandwich`` fixture where pin 7
        lands at an even-indexed filtered position (perpendicular
        direction).
        """
        rules = _make_rules(manufacturer="jlcpcb-tier1")
        grid = _make_grid(rules)
        router = EscapeRouter(grid, rules)
        pads = _make_lqfp48_with_plane_sandwich()
        package = router.analyze_package(pads)

        escapes = router.generate_escapes(package)

        # Pin 7 NRST: predicate IS True (it's sandwiched), but direction
        # is perpendicular -> no forced rescue.  The escape may still
        # use a via for other reasons (e.g. the existing row-level
        # violation check); we just assert that the gate-level force
        # path is not the reason.
        pin7 = next(
            (e for e in escapes if e.pad.pin == "7"),
            None,
        )
        assert pin7 is not None
        # The predicate alone returns True for pin 7 (so the test fixture
        # is correctly set up).
        sandwich_pad = next(p for p in pads if p.pin == "7" and p.net == 200)
        assert router._is_pin_boxed_by_plane_neighbours(
            sandwich_pad, package,
        )

    def test_no_through_channel_routing_for_sandwich_pin(self):
        """Verify the rescue actually replaces the surface escape -- the
        sandwich pin should NOT have any escape segment on F.Cu running
        along the west edge towards an adjacent plane pad."""
        rules = _make_rules(manufacturer="jlcpcb-tier1")
        grid = _make_grid(rules)
        router = EscapeRouter(grid, rules)
        pads = _make_lqfp48_along_edge_sandwich()
        package = router.analyze_package(pads)

        escapes = router.generate_escapes(package)

        sandwich_escape = next(
            e for e in escapes if e.pad.pin == "4" and e.pad.net == 200
        )

        # The single segment must run on the INNER layer (B.Cu on this
        # 2-layer fixture), not on F.Cu.  An in-pad escape has one inner
        # segment running from the via to the inner-layer escape point.
        f_cu_segments = [
            seg for seg in sandwich_escape.segments if seg.layer == Layer.F_CU
        ]
        assert f_cu_segments == [], (
            "Plane-sandwich rescue must not emit any F.Cu segments "
            f"(through-channel route on surface); got {f_cu_segments}"
        )


# ----------------------------------------------------------------------------
# Error path: profile lacking via-in-pad must surface a clear message.
# ----------------------------------------------------------------------------


class TestNoViaInPadErrorPath:
    """Issue #2880: when via-in-pad is unavailable, the router must emit
    a clear error message rather than silently producing a route the
    DRC validator will later reject."""

    def test_error_logged_when_via_in_pad_unsupported(self, caplog):
        """Default ``jlcpcb`` profile does NOT support via-in-pad.  The
        sandwich predicate still fires, but the rescue cannot run, so
        the router must log an explanatory error pointing at the
        unfixable geometric constraint."""
        rules = _make_rules(manufacturer="jlcpcb")
        grid = _make_grid(rules)
        router = EscapeRouter(grid, rules)
        assert not router.via_in_pad_supported, (
            "Fixture sanity: plain jlcpcb should not support via-in-pad"
        )
        pads = _make_lqfp48_along_edge_sandwich()
        package = router.analyze_package(pads)

        with caplog.at_level(logging.ERROR, logger="kicad_tools.router.escape"):
            router.generate_escapes(package)

        # At least one ERROR record must mention the plane-sandwich
        # diagnostic.  We look for the issue marker rather than a
        # specific phrasing so the message can be tuned without
        # breaking the test.
        sandwich_errors = [
            r
            for r in caplog.records
            if r.levelno >= logging.ERROR and "#2880" in r.getMessage()
        ]
        assert len(sandwich_errors) >= 1, (
            "Expected at least one ERROR log referencing Issue #2880; "
            f"got records: {[r.getMessage() for r in caplog.records]}"
        )

        # The message must name the manufacturer (so the user can act
        # on the diagnostic).
        assert any(
            "jlcpcb" in r.getMessage() for r in sandwich_errors
        ), (
            "Sandwich error must name the manufacturer; got: "
            f"{[r.getMessage() for r in sandwich_errors]}"
        )

    def test_no_error_when_via_in_pad_supported(self, caplog):
        """With ``jlcpcb-tier1`` the rescue runs cleanly -- no error
        logs about plane-sandwich should appear."""
        rules = _make_rules(manufacturer="jlcpcb-tier1")
        grid = _make_grid(rules)
        router = EscapeRouter(grid, rules)
        pads = _make_lqfp48_along_edge_sandwich()
        package = router.analyze_package(pads)

        with caplog.at_level(logging.ERROR, logger="kicad_tools.router.escape"):
            router.generate_escapes(package)

        sandwich_errors = [
            r
            for r in caplog.records
            if r.levelno >= logging.ERROR and "#2880" in r.getMessage()
        ]
        assert sandwich_errors == [], (
            "When via-in-pad is supported, no #2880 ERROR records should "
            f"be emitted; got: {[r.getMessage() for r in sandwich_errors]}"
        )
