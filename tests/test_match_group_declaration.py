"""Match-group declaration field tests (Issue #2687, Epic #2661 Phase 1A).

This module tests the **declaration-ergonomics** foundation of Phase 1 of
Epic #2661: ``NetClassRouting.length_match_group`` /
``length_match_reference`` / ``length_match_tolerance_mm`` fields plus
their ``effective_*`` accessors.

Mirrors the pattern of:

* ``tests/test_diffpair_length.py::TestEffectiveSkewTolerance`` -- the
  per-class round-trip + default-fallback semantics template.
* ``tests/test_diffpair_length.py::TestDriftPrevention`` -- the
  cross-link / two-copies-of-the-same-default drift-prevention template.

This is types-only.  No routing/measurement behavior is exercised here;
Phase 1B (#2688) tests the ``MatchGroupTracker`` consumption.
"""

from __future__ import annotations

from dataclasses import fields

from kicad_tools.reasoning.vocabulary import RoutingPriority
from kicad_tools.router.rules import NetClassRouting

# =============================================================================
# 1. length_match_group field declaration + round-trip
# =============================================================================


class TestLengthMatchGroupField:
    """Field declaration, default, and dataclass round-trip semantics."""

    def test_default_is_none(self):
        nc = NetClassRouting(name="Default")
        assert nc.length_match_group is None

    def test_explicit_value_round_trips(self):
        nc = NetClassRouting(name="DDR", length_match_group="DDR_DATA")
        assert nc.length_match_group == "DDR_DATA"

    def test_field_round_trip_via_repr_and_equality(self):
        nc1 = NetClassRouting(name="MIPI", length_match_group="MIPI_CSI")
        nc2 = NetClassRouting(name="MIPI", length_match_group="MIPI_CSI")
        assert nc1 == nc2
        assert nc1.length_match_group == "MIPI_CSI"
        assert "length_match_group='MIPI_CSI'" in repr(nc1)

    def test_multiple_classes_may_share_group_name(self):
        # The lanes of a MIPI bus may live in different per-pair classes
        # that all share a single ``length_match_group="MIPI_CSI"``.  The
        # field doesn't enforce uniqueness; that is a higher-layer concern
        # (Phase 1C detection).
        lane0 = NetClassRouting(name="MIPI_CSI_LANE0", length_match_group="MIPI_CSI")
        lane1 = NetClassRouting(name="MIPI_CSI_LANE1", length_match_group="MIPI_CSI")
        assert lane0.length_match_group == lane1.length_match_group == "MIPI_CSI"

    def test_field_has_correct_type_annotation(self):
        # Drift guard: the field annotation must remain ``str | None`` so
        # the cross-link with ``RoutingPriority.length_match_group``
        # documented in the field's docstring stays valid.
        field_map = {f.name: f for f in fields(NetClassRouting)}
        assert field_map["length_match_group"].type == (str | None)
        assert field_map["length_match_group"].default is None


# =============================================================================
# 2. length_match_reference field declaration + round-trip + sentinel
# =============================================================================


class TestLengthMatchReferenceField:
    """Field declaration, default, sentinel passthrough, and round-trip."""

    def test_default_is_none(self):
        # ``None`` means "use longest in group" -- the documented default
        # semantics.  No magic string at the field layer.
        nc = NetClassRouting(name="Default")
        assert nc.length_match_reference is None

    def test_explicit_net_name_round_trips(self):
        # "Pace-car" mode: explicit net name as the reference.
        nc = NetClassRouting(name="DDR", length_match_reference="DQS_P")
        assert nc.length_match_reference == "DQS_P"

    def test_clock_sentinel_passes_through(self):
        # Forward-compat: Phase 1A accepts the ``"clock"`` sentinel but
        # does not interpret it.  Phase 2/3 wires protocol-aware lookup.
        nc = NetClassRouting(name="MIPI", length_match_reference="clock")
        assert nc.length_match_reference == "clock"

    def test_field_round_trip_via_dataclass_construction(self):
        nc1 = NetClassRouting(name="HS", length_match_reference="DQS_P")
        nc2 = NetClassRouting(name="HS", length_match_reference="DQS_P")
        assert nc1 == nc2
        assert "length_match_reference='DQS_P'" in repr(nc1)

    def test_field_has_correct_type_annotation(self):
        field_map = {f.name: f for f in fields(NetClassRouting)}
        assert field_map["length_match_reference"].type == (str | None)
        assert field_map["length_match_reference"].default is None


# =============================================================================
# 3. length_match_tolerance_mm field declaration + accessor
# =============================================================================


class TestLengthMatchToleranceField:
    """Field declaration + ``effective_length_match_tolerance`` accessor.

    Bundled into Phase 1A from the Phase 1B follow-up because the field
    mirrors the :attr:`skew_tolerance_mm` shape and Phase 1B will need it
    immediately.  Mirrors :meth:`effective_skew_tolerance` semantics.
    """

    def test_default_is_none(self):
        nc = NetClassRouting(name="Default")
        assert nc.length_match_tolerance_mm is None

    def test_default_accessor_returns_half_mm(self):
        # The accessor default arg is 0.5 (mirrors
        # ``effective_skew_tolerance``); the future Phase 2G DRC rule's
        # ``DEFAULT_MATCH_GROUP_TOLERANCE_MM`` constant must equal this.
        nc = NetClassRouting(name="Default")
        assert nc.effective_length_match_tolerance() == 0.5

    def test_override_returns_explicit_value(self):
        nc = NetClassRouting(name="DDR_DATA", length_match_tolerance_mm=0.1)
        assert nc.effective_length_match_tolerance() == 0.1
        # Explicit default arg must be ignored when the field is set.
        assert nc.effective_length_match_tolerance(default=99.0) == 0.1

    def test_default_arg_overrides_module_default_when_field_unset(self):
        nc = NetClassRouting(name="Default")
        assert nc.effective_length_match_tolerance(default=1.0) == 1.0


# =============================================================================
# 4. effective_length_match_group accessor
# =============================================================================


class TestEffectiveLengthMatchGroup:
    """Parameter-free accessor; returns the field as-is."""

    def test_default_unset_returns_none(self):
        nc = NetClassRouting(name="Default")
        assert nc.effective_length_match_group() is None

    def test_explicit_value_passes_through(self):
        nc = NetClassRouting(name="DDR", length_match_group="DDR_DATA")
        assert nc.effective_length_match_group() == "DDR_DATA"

    def test_acceptance_criterion_ddr_data_round_trip(self):
        # Acceptance: ``NetClassRouting(name="DDR", length_match_group="DDR_DATA")``
        # round-trips through ``effective_length_match_group()`` returning
        # ``"DDR_DATA"``.
        nc = NetClassRouting(name="DDR", length_match_group="DDR_DATA")
        assert nc.effective_length_match_group() == "DDR_DATA"


# =============================================================================
# 5. effective_length_match_reference accessor
# =============================================================================


class TestEffectiveLengthMatchReference:
    """Parameter-free accessor; returns the field as-is."""

    def test_default_unset_returns_none(self):
        # Acceptance: ``NetClassRouting(name="DDR").effective_length_match_reference()
        # is None`` (default = longest).
        nc = NetClassRouting(name="DDR")
        assert nc.effective_length_match_reference() is None

    def test_explicit_pace_car_net_passes_through(self):
        # Acceptance: ``NetClassRouting(name="DDR", length_match_reference="DQS_P")
        # .effective_length_match_reference() == "DQS_P"``.
        nc = NetClassRouting(name="DDR", length_match_reference="DQS_P")
        assert nc.effective_length_match_reference() == "DQS_P"

    def test_clock_sentinel_passes_through_unchanged(self):
        # Forward-compat: the sentinel is preserved by the accessor so
        # Phase 2/3 protocol-aware consumers can pattern-match on it.
        nc = NetClassRouting(name="MIPI", length_match_reference="clock")
        assert nc.effective_length_match_reference() == "clock"


# =============================================================================
# 6. Drift-prevention: RoutingPriority field cross-link
# =============================================================================


class TestDriftPrevention:
    """Cross-link drift-prevention between router-layer and reasoning-layer.

    A pre-existing ``length_match_group: str | None = None`` field lives
    on a DIFFERENT dataclass at
    :class:`kicad_tools.reasoning.vocabulary.RoutingPriority`.  These are
    NOT in conflict -- they sit on different classes (router-layer vs
    reasoning-layer).  The router-layer field
    (:class:`NetClassRouting.length_match_group`) is AUTHORITATIVE.

    These tests assert both fields share type annotation and default so
    if either side changes type or default, this test fails fast and the
    docstring cross-link must be re-verified.  Mirrors the #2521 / #2640
    alias-drift failure-mode lessons.
    """

    def test_router_field_type_annotation(self):
        field_map = {f.name: f for f in fields(NetClassRouting)}
        assert field_map["length_match_group"].type == (str | None)

    def test_reasoning_field_type_annotation(self):
        field_map = {f.name: f for f in fields(RoutingPriority)}
        assert field_map["length_match_group"].type == (str | None)

    def test_router_and_reasoning_field_alignment(self):
        # Both fields must share type annotation (``str | None``) AND
        # default (``None``).  If either diverges, this assertion fails
        # and the docstring cross-link must be re-verified.
        router_fields = {f.name: f for f in fields(NetClassRouting)}
        reasoning_fields = {f.name: f for f in fields(RoutingPriority)}
        router_field = router_fields["length_match_group"]
        reasoning_field = reasoning_fields["length_match_group"]
        assert router_field.type == (str | None)
        assert reasoning_field.type == (str | None)
        assert router_field.type == reasoning_field.type
        assert router_field.default is None
        assert reasoning_field.default is None

    def test_accessor_tolerance_default_is_half_mm(self):
        # The literal 0.5 must appear in exactly two places per repo
        # (mirroring the #2647 / #2649 drift-prevention contract): the
        # accessor default arg here, and the future Phase 2G rule's
        # ``DEFAULT_MATCH_GROUP_TOLERANCE_MM`` constant.  Any third copy
        # is drift.
        nc = NetClassRouting(name="Default")
        assert nc.effective_length_match_tolerance() == 0.5

    def test_tolerance_accessor_matches_phase_2g_constant_when_available(self):
        # Best-effort cross-check: if Phase 2G has already landed, import
        # the constant and assert byte-for-byte equality.  Until then the
        # accessor-default assertion above anchors the 0.5 unilaterally.
        try:
            from kicad_tools.validate.rules.match_group_length_skew import (
                DEFAULT_MATCH_GROUP_TOLERANCE_MM,
            )
        except ImportError:
            # Phase 2G not yet merged -- the accessor-default assertion
            # above anchors 0.5 unilaterally.
            return
        nc = NetClassRouting(name="Default")
        assert nc.effective_length_match_tolerance() == DEFAULT_MATCH_GROUP_TOLERANCE_MM


# =============================================================================
# 7. Backward-compatibility -- no regressions for callers that don't set fields
# =============================================================================


class TestBackwardCompatibility:
    """Callers that don't set the new fields see no behavior change."""

    def test_no_field_changes_when_not_set(self):
        # The default-constructed dataclass keeps all pre-#2687 fields at
        # their previous defaults.  This guards against accidental
        # collateral default flips.
        nc = NetClassRouting(name="X")
        assert nc.length_match_group is None
        assert nc.length_match_reference is None
        assert nc.length_match_tolerance_mm is None
        # Spot-check unrelated fields stay at their documented defaults.
        assert nc.priority == 5
        assert nc.trace_width == 0.2
        assert nc.clearance == 0.2
        assert nc.skew_tolerance_mm is None
        assert nc.diffpair_partner is None

    def test_construction_with_only_required_args_succeeds(self):
        # The new fields are optional (default ``None``) so no caller
        # signature changes.
        nc = NetClassRouting(name="X")
        assert nc.name == "X"
