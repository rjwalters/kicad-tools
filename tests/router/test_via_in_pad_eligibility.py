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
"""

from __future__ import annotations

from kicad_tools.manufacturers.fabrication_process import (
    JLCPCB_TIER1_POFV_4L,
    PCBWAY_VIA_IN_PAD,
)
from kicad_tools.router.via_in_pad_eligibility import (
    resolve_process,
    via_geometry_eligible,
)


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
