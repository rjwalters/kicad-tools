"""Tests for :mod:`kicad_tools.router.via_in_pad_eligibility` (Issue #5201).

The escape router (:mod:`kicad_tools.router.escape`) and the post-route
auto-fix repair sweep (:mod:`kicad_tools.router.drc_nudge`) both resolve
via-in-pad eligibility through this module rather than the bare
``MfrLimits.via_in_pad_supported`` capability boolean, so an "eligible"
board always has a real, orderable
:class:`~kicad_tools.manufacturers.fabrication_process.FabricationProcess`
attached, and an "eligible" via always fits that process's published
geometric envelope. This file exercises the module directly (board-level
``resolve_process`` and per-via ``via_geometry_eligible``) -- the router
integration paths are covered separately in ``tests/test_drc_nudge.py``
(``TestViaInPadProcessEligibilityGate``) and
``tests/test_escape_missed_via_in_pad.py``.

Issue #5201 (reopened): also exercises the component-hole-census helpers
(``resolve_component_hole_context`` / ``via_in_pad_candidate_eligible``)
directly -- the FULL production-decision integration for these is
covered by ``tests/router/test_via_in_pad_component_hole_census.py``
(escape) and ``tests/test_drc_nudge.py::TestViaInPadComponentHoleCensus``
(repair sweep).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from kicad_tools.manufacturers.fabrication_process import (
    JLCPCB_TIER1_POFV_4L,
    PCBWAY_VIA_IN_PAD,
)
from kicad_tools.router.via_in_pad_eligibility import (
    ComponentHoleContext,
    resolve_component_hole_context,
    resolve_process,
    via_geometry_eligible,
    via_in_pad_candidate_eligible,
)


@dataclass
class _HolePad:
    """Minimal duck-typed pad for ``resolve_component_hole_context``."""

    x: float
    y: float
    ref: str = "H1"
    pin: str = "1"
    through_hole: bool = True
    drill: float = 0.3


class TestResolveProcess:
    """Board-level eligibility: a real process must exist for THIS board."""

    def test_no_manufacturer_is_not_eligible(self):
        """Missing context must never silently grant eligibility."""
        assert resolve_process(None, 4) is None
        assert resolve_process("", 4) is None

    def test_unknown_manufacturer_is_not_eligible(self):
        assert resolve_process("not-a-real-manufacturer", 4) is None

    def test_unknown_layer_count_is_not_eligible(self):
        """An unresolved board layer count must never silently grant
        eligibility, even for a manufacturer/tier that supports
        via-in-pad broadly."""
        assert resolve_process("jlcpcb-tier1", None) is None
        assert resolve_process("pcbway", None) is None

    def test_tier1_two_layer_has_no_eligible_process(self):
        """jlcpcb-tier1's POFV process requires >= 4 layers; the 2-layer
        configs deliberately carry no ``via_in_pad_process_id``."""
        assert resolve_process("jlcpcb-tier1", 2) is None

    def test_tier1_four_layer_resolves_pofv(self):
        assert resolve_process("jlcpcb-tier1", 4) is JLCPCB_TIER1_POFV_4L

    def test_tier1_six_layer_resolves_pofv(self):
        """Above the floor stays eligible."""
        assert resolve_process("jlcpcb-tier1", 6) is JLCPCB_TIER1_POFV_4L

    def test_pcbway_two_layer_resolves(self):
        """PCBWay publishes via-in-pad at every layer count."""
        assert resolve_process("pcbway", 2) is PCBWAY_VIA_IN_PAD

    def test_base_tier_has_no_capability_at_all(self):
        """Plain jlcpcb has no via-in-pad capability, at any layer count."""
        assert resolve_process("jlcpcb", 4) is None


class TestViaGeometryEligible:
    """Per-via eligibility: a resolved process does not certify every via."""

    def test_within_envelope_is_eligible(self):
        assert (
            via_geometry_eligible(
                JLCPCB_TIER1_POFV_4L,
                drill_mm=0.3,
                annular_ring_mm=0.15,
                nearest_other_hole_distance_mm=1.0,
            )
            is True
        )

    def test_drill_below_minimum_is_ineligible(self):
        # JLCPCB_TIER1_POFV_4L.min_via_drill_mm == 0.2
        assert (
            via_geometry_eligible(
                JLCPCB_TIER1_POFV_4L,
                drill_mm=0.1,
                annular_ring_mm=0.15,
                nearest_other_hole_distance_mm=1.0,
            )
            is False
        )

    def test_drill_above_maximum_is_ineligible(self):
        # JLCPCB_TIER1_POFV_4L.max_via_drill_mm == 0.5
        assert (
            via_geometry_eligible(
                JLCPCB_TIER1_POFV_4L,
                drill_mm=0.8,
                annular_ring_mm=0.15,
                nearest_other_hole_distance_mm=1.0,
            )
            is False
        )

    def test_annular_ring_below_minimum_is_ineligible(self):
        # JLCPCB_TIER1_POFV_4L.min_annular_ring_mm == 0.10
        assert (
            via_geometry_eligible(
                JLCPCB_TIER1_POFV_4L,
                drill_mm=0.3,
                annular_ring_mm=0.02,
                nearest_other_hole_distance_mm=1.0,
            )
            is False
        )

    def test_component_hole_too_close_is_ineligible(self):
        # JLCPCB_TIER1_POFV_4L.min_component_hole_distance_mm == 0.5
        assert (
            via_geometry_eligible(
                JLCPCB_TIER1_POFV_4L,
                drill_mm=0.3,
                annular_ring_mm=0.15,
                nearest_other_hole_distance_mm=0.1,
            )
            is False
        )

    def test_unknown_annular_ring_and_hole_distance_skip_those_checks(self):
        """A caller with only a drill diameter (e.g. the escape router's
        opportunistic-placement decision, which does not carry a
        board-wide PTH registry) still gets a best-effort answer rather
        than a spurious failure -- unchecked dimensions are not treated
        as failing."""
        assert (
            via_geometry_eligible(
                JLCPCB_TIER1_POFV_4L,
                drill_mm=0.3,
                annular_ring_mm=None,
                nearest_other_hole_distance_mm=None,
            )
            is True
        )

    def test_layer_count_is_never_reconsidered_here(self):
        """``resolve_process`` already proved the board-level layer-count
        floor is satisfied before a caller holds a resolved process --
        ``via_geometry_eligible`` must not re-fail a 2-layer-modeled via
        against a 4-layer-floor process; it only checks per-via geometry.
        This is implicit in the function signature (no ``layer_count``
        parameter) -- this test pins that a fully in-envelope via is
        eligible regardless of what layer count the caller might
        otherwise associate with it."""
        assert (
            via_geometry_eligible(
                JLCPCB_TIER1_POFV_4L,
                drill_mm=0.3,
                annular_ring_mm=0.15,
            )
            is True
        )


class TestResolveComponentHoleContext:
    """Issue #5201 (reopened): distinguishing unknown/incomplete census
    from a verified (possibly empty) one."""

    def test_none_all_pads_is_unknown(self):
        ctx = resolve_component_hole_context(0.0, 0.0, 0.3, all_pads=None)
        assert ctx == ComponentHoleContext(known=False, nearest_distance_mm=None)

    def test_empty_all_pads_is_verified_empty(self):
        ctx = resolve_component_hole_context(0.0, 0.0, 0.3, all_pads=[])
        assert ctx.known is True
        assert ctx.nearest_distance_mm == math.inf

    def test_non_through_hole_pads_are_ignored(self):
        """SMD pads never count as "other component holes"."""
        smd = _HolePad(x=0.4, y=0.0, through_hole=False)
        ctx = resolve_component_hole_context(0.0, 0.0, 0.3, all_pads=[smd])
        assert ctx.known is True
        assert ctx.nearest_distance_mm == math.inf

    def test_own_pad_is_excluded(self):
        """The candidate's own (ref, pin) never counts as a foreign hole,
        even if it happens to carry ``through_hole=True``."""
        own = _HolePad(x=0.0, y=0.0, ref="U5", pin="7", through_hole=True, drill=0.3)
        ctx = resolve_component_hole_context(
            0.0, 0.0, 0.3, all_pads=[own], exclude_ref="U5", exclude_pin="7"
        )
        assert ctx.known is True
        assert ctx.nearest_distance_mm == math.inf

    def test_nearest_distance_is_edge_to_edge(self):
        """0.4mm centre distance, 0.3mm via drill, 0.3mm hole drill ->
        0.4 - 0.15 - 0.15 = 0.1mm edge-to-edge."""
        hole = _HolePad(x=0.4, y=0.0, drill=0.3)
        ctx = resolve_component_hole_context(0.0, 0.0, 0.3, all_pads=[hole])
        assert ctx.known is True
        assert ctx.nearest_distance_mm is not None
        assert math.isclose(ctx.nearest_distance_mm, 0.1, abs_tol=1e-9)

    def test_unknown_drill_fails_closed_even_when_far(self):
        """A through-hole pad with drill<=0 makes the WHOLE census
        unknown, regardless of how far away it physically sits."""
        far_unknown = _HolePad(x=500.0, y=500.0, drill=0.0)
        ctx = resolve_component_hole_context(0.0, 0.0, 0.3, all_pads=[far_unknown])
        assert ctx == ComponentHoleContext(known=False, nearest_distance_mm=None)

    def test_picks_the_nearest_of_multiple_holes(self):
        near = _HolePad(x=0.4, y=0.0, ref="H1", drill=0.3)
        far = _HolePad(x=50.0, y=50.0, ref="H2", drill=0.3)
        ctx = resolve_component_hole_context(0.0, 0.0, 0.3, all_pads=[far, near])
        assert ctx.known is True
        assert ctx.nearest_distance_mm is not None
        assert math.isclose(ctx.nearest_distance_mm, 0.1, abs_tol=1e-9)


class TestViaInPadCandidateEligible:
    """Issue #5201 (reopened): the hole-context-aware wrapper must fail
    closed on ``known=False`` rather than fall through to
    ``via_geometry_eligible``'s "skip the check" behaviour."""

    def test_unknown_context_refuses_even_with_in_envelope_geometry(self):
        assert (
            via_in_pad_candidate_eligible(
                JLCPCB_TIER1_POFV_4L,
                drill_mm=0.3,
                annular_ring_mm=0.15,
                hole_context=ComponentHoleContext(known=False, nearest_distance_mm=None),
            )
            is False
        )

    def test_verified_empty_context_is_eligible(self):
        assert (
            via_in_pad_candidate_eligible(
                JLCPCB_TIER1_POFV_4L,
                drill_mm=0.3,
                annular_ring_mm=0.15,
                hole_context=ComponentHoleContext(known=True, nearest_distance_mm=math.inf),
            )
            is True
        )

    def test_verified_near_context_is_ineligible(self):
        assert (
            via_in_pad_candidate_eligible(
                JLCPCB_TIER1_POFV_4L,
                drill_mm=0.3,
                annular_ring_mm=0.15,
                hole_context=ComponentHoleContext(known=True, nearest_distance_mm=0.1),
            )
            is False
        )

    def test_verified_far_context_is_eligible(self):
        assert (
            via_in_pad_candidate_eligible(
                JLCPCB_TIER1_POFV_4L,
                drill_mm=0.3,
                annular_ring_mm=0.15,
                hole_context=ComponentHoleContext(known=True, nearest_distance_mm=1.0),
            )
            is True
        )
