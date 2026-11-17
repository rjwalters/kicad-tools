"""Tests for adaptive-grid-aware off-grid pad classification (Issue #2910).

Verifies that the off-grid check on the Autorouter recognises pads that fall
on the coarse routing grid by less than ``resolution / 10`` as on-grid,
recognises pads covered by an explicit :class:`FineZone` as adaptive-covered,
and recognises pads on a router-compatible component pitch as adaptive-covered
even when no fine zone has been pre-built.

The pre-fix off-grid check used a hard ``resolution / 10`` threshold against
the coarse grid only.  For 2.54mm-pitch through-hole connectors on a 0.1mm
coarse grid, the pad coordinate lands 0.030mm off-grid -- which exceeds the
threshold -- so the per-edge ``PADS_OFF_GRID`` emit fired and the rip-up
blacklist permanently excluded the net from recovery.  The fix consults
adaptive-grid coverage before classifying a pad as structurally unroutable.

Test matrix:

1. ``test_on_grid_pad_has_coverage`` -- on-grid pads always have coverage.
2. ``test_2p54mm_pitch_thp_has_implicit_coverage`` -- 2.54mm THT connectors
   (board 01's J1/J2) get implicit coverage via pitch-derived sub-grid.
3. ``test_fine_zone_covered_pad_has_coverage`` -- a 0.5mm BGA pad off the
   coarse grid but inside an explicit FineZone is covered.
4. ``test_unaligned_off_grid_pad_has_no_coverage`` -- a pad with neither
   FineZone nor compatible-pitch coverage remains structurally off-grid.
5. ``test_net_with_2p54mm_connector_not_blacklisted`` -- a net containing
   a 2.54mm-pitch pin header pad is not classified by
   ``_net_has_off_grid_pads`` once its per-edge emit is filtered.
"""

from __future__ import annotations

from kicad_tools.router.core import Autorouter
from kicad_tools.router.io import FineZone
from kicad_tools.router.rules import DesignRules


def _make_router(grid_resolution: float = 0.1) -> Autorouter:
    """Create a small Autorouter for testing.

    Uses a deliberately small footprint so the coarse grid is fast.  The
    pad-offset and adaptive-coverage checks are independent of board size.
    """
    rules = DesignRules(grid_resolution=grid_resolution)
    return Autorouter(width=30.0, height=30.0, origin_x=100.0, origin_y=100.0, rules=rules)


def _add_pad(
    router: Autorouter,
    ref: str,
    pin: str,
    x: float,
    y: float,
    net: int,
    net_name: str = "",
) -> None:
    """Add a single pad to the router via ``add_component``."""
    router.add_component(ref, [{"number": pin, "x": x, "y": y, "net": net, "net_name": net_name}])


def test_on_grid_pad_has_coverage():
    """Pads exactly on the coarse grid trivially have adaptive coverage."""
    router = _make_router(grid_resolution=0.1)
    _add_pad(router, "R1", "1", 105.0, 108.0, net=1, net_name="VIN")

    pad = router.pads[("R1", "1")]
    # On-grid means offset == 0 which is below the resolution/10 threshold,
    # so structurally off-grid is trivially False.
    assert router._pad_offset_from_coarse_grid(pad) == 0.0
    # _pad_has_adaptive_grid_coverage should still return True (a single
    # pad component has no pitch, but the on-grid alignment is exact and
    # _pad_offset_from_coarse_grid is below threshold -- the caller path
    # short-circuits before consulting coverage).


def test_2p54mm_pitch_thp_has_implicit_coverage():
    """A 2.54mm-pitch through-hole header pad is adaptive-covered.

    Board 01 (voltage divider) ships J1/J2 pin headers at 2.54mm pitch.
    Their pads land at e.g. (105.0, 111.23) -- 0.030mm off the 0.1mm
    coarse grid.  The pre-fix off-grid check classified them as
    structurally unroutable, blacklisting GND and VOUT before routing.

    Post-fix: the 2.54mm pitch yields a fine resolution (0.005mm via
    :func:`compute_subgrid_resolution`) that divides the 0.030mm offset
    exactly (6 fine cells), so the pad is recognised as adaptive-covered.
    """
    router = _make_router(grid_resolution=0.1)
    # Two pads of a 2.54mm-pitch pin header (J1.1 and J1.2 from board 01)
    _add_pad(router, "J1", "1", 105.0, 111.23, net=1, net_name="VIN")
    _add_pad(router, "J1", "2", 105.0, 113.77, net=3, net_name="GND")

    # Offsets are 0.03mm -- above resolution/10 == 0.01mm
    pad_a = router.pads[("J1", "1")]
    pad_b = router.pads[("J1", "2")]
    assert abs(router._pad_offset_from_coarse_grid(pad_a) - 0.03) < 1e-6
    assert abs(router._pad_offset_from_coarse_grid(pad_b) - 0.03) < 1e-6

    # Both pads must report adaptive-grid coverage via the implicit
    # pitch-derived path (no FineZone configured here).
    assert router._pad_has_adaptive_grid_coverage(pad_a), (
        "2.54mm-pitch THT pin header pad should be recognised as "
        "adaptive-grid-covered via its component pitch"
    )
    assert router._pad_has_adaptive_grid_coverage(pad_b), (
        "2.54mm-pitch THT pin header pad should be recognised as "
        "adaptive-grid-covered via its component pitch"
    )


def test_2p00mm_pitch_pad_has_implicit_coverage():
    """A 2.0mm-pitch SMD pair (board 01 R1/R2) is adaptive-covered.

    Board 01 0805 resistors have 2.0mm centre-to-centre pad pitch and the
    pad coordinates are integer mm -- 0805 pads sit at e.g. (115, 108) and
    (117, 108).  These are already on the 0.1mm coarse grid, so the
    off-grid check short-circuits at zero offset.  This test guards the
    implicit-coverage path doesn't trip on integer-mm coordinates.
    """
    router = _make_router(grid_resolution=0.1)
    _add_pad(router, "R1", "1", 114.0, 108.0, net=1, net_name="VIN")
    _add_pad(router, "R1", "2", 116.0, 108.0, net=2, net_name="VOUT")

    pad_a = router.pads[("R1", "1")]
    pad_b = router.pads[("R1", "2")]
    assert router._pad_offset_from_coarse_grid(pad_a) == 0.0
    assert router._pad_offset_from_coarse_grid(pad_b) == 0.0
    assert router._pad_has_adaptive_grid_coverage(pad_a)
    assert router._pad_has_adaptive_grid_coverage(pad_b)


def test_fine_zone_covered_pad_has_coverage():
    """A pad off the coarse grid but inside an explicit FineZone is covered.

    This is the canonical fine-pitch IC case (TSSOP/SSOP/BGA at 0.5mm-
    0.65mm pitch).  The CLI builds a :class:`FineZone` around the
    component before routing, and ``_pad_has_adaptive_grid_coverage``
    must recognise the zone.
    """
    router = _make_router(grid_resolution=0.1)
    # 0.5mm-pitch pad off the 0.1mm coarse grid
    # at (105.05, 108.05) -- 0.05mm off in both axes.
    _add_pad(router, "U1", "1", 105.05, 108.05, net=1, net_name="SIG1")
    _add_pad(router, "U1", "2", 105.55, 108.05, net=2, net_name="SIG2")

    # Build a FineZone covering the component at 0.05mm resolution with
    # offset matching the pads' 0.05mm sub-grid alignment.
    zone = FineZone(
        ref="U1",
        x_min=104.0,
        y_min=107.0,
        x_max=107.0,
        y_max=109.0,
        resolution=0.05,
        x_offset=0.05,
        y_offset=0.05,
    )
    router.fine_zones = [zone]

    pad = router.pads[("U1", "1")]
    assert router._pad_has_adaptive_grid_coverage(pad), (
        "Pad inside an explicit FineZone aligned to its position should "
        "be recognised as adaptive-grid-covered"
    )


def test_unaligned_off_grid_pad_has_no_coverage():
    """A pad with no FineZone and no compatible pitch remains off-grid.

    Issue #1605 regression guard: pads that are genuinely off any grid
    the router can synthesise must continue to be classified as off-grid
    so the per-edge PADS_OFF_GRID emit fires and the rip-up loop
    correctly excludes them.

    We construct a single-pad "component" (no neighbours -> no pitch in
    ``component_pitches`` -> no implicit coverage) at an arbitrary 0.073mm
    offset from the coarse grid.  No FineZone is configured.
    """
    router = _make_router(grid_resolution=0.1)
    _add_pad(router, "MH1", "1", 105.073, 108.073, net=0, net_name="")

    pad = router.pads[("MH1", "1")]
    # 0.073mm offset, above resolution/10 == 0.01mm
    assert router._pad_offset_from_coarse_grid(pad) > router.grid.resolution / 10
    # Single-pad component -> no pitch -> no implicit coverage
    assert pad.ref not in router.component_pitches
    # No FineZone configured
    assert router.fine_zones == []
    # So the pad must NOT have adaptive coverage
    assert not router._pad_has_adaptive_grid_coverage(pad), (
        "A pad with no FineZone and no compatible-pitch component must "
        "remain classified as off-grid (Issue #1605 regression guard)"
    )


def test_net_with_2p54mm_connector_not_blacklisted():
    """``_net_has_off_grid_pads`` returns False for adaptive-covered nets.

    Issue #2910 acceptance criterion #2: a net containing only
    adaptive-grid-covered pads (here, two 2.54mm-pitch pin header pads)
    is NOT classified as structurally off-grid.  This is the predicate
    the per-edge ``PADS_OFF_GRID`` emit, the Steiner-decomposition gate,
    and the tier-0 promotion logic all consult -- and they must agree
    that adaptive-covered nets are routable through normal means.
    """
    router = _make_router(grid_resolution=0.1)
    # J1 pin header (2.54mm pitch, 0.03mm off coarse grid)
    _add_pad(router, "J1", "1", 105.0, 111.23, net=1, net_name="VIN")
    _add_pad(router, "J1", "2", 105.0, 113.77, net=1, net_name="VIN")

    assert not router._net_has_off_grid_pads(1), (
        "A net whose only off-coarse-grid pads have adaptive-grid "
        "coverage must NOT be classified as structurally off-grid -- "
        "the per-edge PADS_OFF_GRID emit and tier-0 promotion logic "
        "both depend on this predicate to mirror the actual routability."
    )


def test_net_with_unaligned_pad_still_off_grid():
    """Issue #1605 regression: genuinely-unreachable pads still flip True.

    A pad with no FineZone and no compatible-pitch component must still
    be classified as structurally off-grid so the rip-up exclusion
    path at ``route_all_negotiated``'s ``off_grid_nets`` set correctly
    keeps the net out of the futile recovery loop.
    """
    router = _make_router(grid_resolution=0.1)
    # Single-pad component at a 0.073mm offset -- no pitch, no zone
    _add_pad(router, "MH1", "1", 105.073, 108.073, net=1, net_name="MOUNT")
    # Add a sibling pad on a different component to make the net 2-pin
    _add_pad(router, "R1", "1", 110.0, 110.0, net=1, net_name="MOUNT")

    assert router._net_has_off_grid_pads(1), (
        "A net with at least one genuinely off-grid pad (no FineZone, "
        "no compatible-pitch component) must still flip the predicate "
        "to True so the rip-up exclusion (Issue #1605) fires."
    )
