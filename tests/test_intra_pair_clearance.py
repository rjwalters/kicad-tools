"""Tests for the ``intra_pair_clearance`` field on ``NetClassRouting``.

Issue #2557 (Epic #2556 Phase 1A) — Type-system foundation for first-class
differential pair within-pair clearance support.

Phase 1A scope is limited to:

- The new field exists on ``NetClassRouting`` with default ``None``.
- The :meth:`NetClassRouting.effective_intra_pair_clearance` accessor falls
  back to ``clearance`` when the field is unset, and returns the explicit
  override otherwise.
- ``dataclasses.replace`` preserves the field correctly (no double source
  of truth).
- All eight predefined ``NetClassRouting`` instances default to ``None``.

Threading the new field into the pathfinder / cpp_backend at within-pair
diff-pair edges is **explicitly out of scope** for #2557 and is the topic
of Issue #2559 (Phase 1B).
"""

from __future__ import annotations

import dataclasses

import pytest

from kicad_tools.router.rules import (
    NET_CLASS_AUDIO,
    NET_CLASS_CLOCK,
    NET_CLASS_DEBUG,
    NET_CLASS_DEFAULT,
    NET_CLASS_DIGITAL,
    NET_CLASS_HIGH_CURRENT_SIGNAL,
    NET_CLASS_HIGH_SPEED,
    NET_CLASS_POWER,
    NetClassRouting,
)


class TestIntraPairClearanceField:
    """The field is present, typed correctly, and defaults to ``None``."""

    def test_field_exists_with_none_default(self):
        fields = {f.name: f for f in dataclasses.fields(NetClassRouting)}
        assert "intra_pair_clearance" in fields
        assert fields["intra_pair_clearance"].default is None

    def test_field_unset_by_default_on_construction(self):
        nc = NetClassRouting(name="Test")
        assert nc.intra_pair_clearance is None

    def test_field_can_be_set_explicitly(self):
        nc = NetClassRouting(name="USB", intra_pair_clearance=0.075)
        assert nc.intra_pair_clearance == 0.075


class TestEffectiveIntraPairClearance:
    """The accessor falls back to ``clearance`` when unset, else returns override."""

    def test_falls_back_to_clearance_when_unset(self):
        nc = NetClassRouting(name="Test", clearance=0.15)
        assert nc.intra_pair_clearance is None
        assert nc.effective_intra_pair_clearance() == 0.15

    def test_returns_override_when_set(self):
        nc = NetClassRouting(name="USB", clearance=0.15, intra_pair_clearance=0.075)
        assert nc.effective_intra_pair_clearance() == 0.075

    def test_override_can_be_larger_than_clearance(self):
        # Slightly unusual but valid: override is >= clearance.
        nc = NetClassRouting(name="WideDP", clearance=0.1, intra_pair_clearance=0.2)
        assert nc.effective_intra_pair_clearance() == 0.2

    def test_override_zero_is_returned_not_treated_as_unset(self):
        # Critical: the sentinel for "fall back to ``clearance``" is ``None``,
        # not falsy. An explicit ``0.0`` must be returned as ``0.0``.
        nc = NetClassRouting(name="ZeroOverride", clearance=0.2, intra_pair_clearance=0.0)
        assert nc.effective_intra_pair_clearance() == 0.0

    def test_falls_back_when_clearance_uses_default(self):
        # Default clearance is 0.2 per dataclass.
        nc = NetClassRouting(name="DefaultClear")
        assert nc.effective_intra_pair_clearance() == 0.2


class TestDataclassesReplaceRoundTrip:
    """``dataclasses.replace`` preserves and mutates the field correctly.

    This guards against the failure mode where defaulting the field to
    ``self.clearance`` (instead of ``None``) would fork two sources of
    truth — making any subsequent ``replace(..., clearance=X)`` leave the
    intra-pair value stale.
    """

    def test_replace_sets_only_intra_pair_clearance(self):
        # Use a fresh instance — the predefined NET_CLASS_HIGH_SPEED now sets
        # intra_pair_clearance=0.075 (Phase 1C config change).
        original = NetClassRouting(name="TestPair", clearance=0.15)
        replaced = dataclasses.replace(original, intra_pair_clearance=0.075)
        assert replaced.intra_pair_clearance == 0.075
        assert replaced.clearance == original.clearance
        assert replaced.name == original.name
        # Original unchanged.
        assert original.intra_pair_clearance is None

    def test_replace_changing_clearance_does_not_touch_intra_pair(self):
        # Backward-compat invariant: if a caller updates ``clearance`` via
        # ``dataclasses.replace`` and never sets ``intra_pair_clearance``,
        # the new ``clearance`` value is what the accessor returns.
        original = NetClassRouting(name="Tweak", clearance=0.2)
        replaced = dataclasses.replace(original, clearance=0.1)
        assert replaced.intra_pair_clearance is None
        assert replaced.effective_intra_pair_clearance() == 0.1

    def test_replace_with_independent_overrides(self):
        original = NetClassRouting(name="Pair", clearance=0.2, intra_pair_clearance=0.075)
        replaced = dataclasses.replace(original, clearance=0.1)
        # Explicit override is preserved; ``clearance`` change does NOT
        # leak into ``intra_pair_clearance``.
        assert replaced.intra_pair_clearance == 0.075
        assert replaced.effective_intra_pair_clearance() == 0.075


class TestPredefinedInstanceDefaults:
    """All eight predefined ``NetClassRouting`` instances default to ``None``.

    Phase 1A does not yet populate non-None values for any predefined
    class. Setting an explicit value (e.g., for ``NET_CLASS_HIGH_SPEED``
    or the inline ``DIFFERENTIAL`` class in net_class.py) is left to
    Issue #2559 / a follow-up so the type-system change can land
    independently of any behavior change.
    """

    @pytest.mark.parametrize(
        "net_class",
        [
            NET_CLASS_POWER,
            NET_CLASS_HIGH_CURRENT_SIGNAL,
            NET_CLASS_CLOCK,
            # NET_CLASS_HIGH_SPEED intentionally omitted — Phase 1C
            # (issue #2559) sets intra_pair_clearance=0.075 on this class
            # to enable tighter within-pair clearance for diff pairs.
            NET_CLASS_AUDIO,
            NET_CLASS_DIGITAL,
            NET_CLASS_DEBUG,
            NET_CLASS_DEFAULT,
        ],
    )
    def test_predefined_instance_intra_pair_is_none(self, net_class):
        assert net_class.intra_pair_clearance is None

    def test_high_speed_has_phase1c_intra_pair_clearance(self):
        # Phase 1C (Epic #2556) configures NET_CLASS_HIGH_SPEED with
        # intra_pair_clearance=0.075 so USB_D+/USB_D- on board 03 fits
        # the J1 row A 0.5mm pitch without -0.200mm overlap.
        assert NET_CLASS_HIGH_SPEED.intra_pair_clearance == 0.075
        assert NET_CLASS_HIGH_SPEED.effective_intra_pair_clearance() == 0.075

    @pytest.mark.parametrize(
        "net_class",
        [
            NET_CLASS_POWER,
            NET_CLASS_HIGH_CURRENT_SIGNAL,
            NET_CLASS_CLOCK,
            # NET_CLASS_HIGH_SPEED omitted (Phase 1C — see above).
            NET_CLASS_AUDIO,
            NET_CLASS_DIGITAL,
            NET_CLASS_DEBUG,
            NET_CLASS_DEFAULT,
        ],
    )
    def test_predefined_accessor_falls_back_to_clearance(self, net_class):
        # With ``intra_pair_clearance is None`` everywhere, the accessor
        # is exactly the existing ``clearance`` value — preserving
        # pre-#2557 behavior at every predefined call site.
        assert net_class.effective_intra_pair_clearance() == net_class.clearance


class TestInlineConstructionSites:
    """Inline ``NetClassRouting`` constructions in net_class.py default to None.

    Per the curator's note, three inline mappings live in
    ``src/kicad_tools/router/net_class.py`` (NetClass.GROUND at L529,
    NetClass.DIFFERENTIAL at L543, NetClass.RF at L552). They are
    constructed inside ``apply_net_class_rules``; reproduce the same
    construction here to verify the new field defaults to ``None``.
    """

    def test_ground_inline_default(self):
        ground = NetClassRouting(
            name="Ground",
            priority=1,
            trace_width=0.5,
            clearance=0.2,
            via_size=0.8,
            cost_multiplier=0.7,
            zone_priority=20,
            zone_connection="solid",
            is_pour_net=True,
        )
        assert ground.intra_pair_clearance is None
        assert ground.effective_intra_pair_clearance() == 0.2

    def test_differential_inline_default(self):
        # Phase 1A intentionally leaves DIFFERENTIAL with ``None`` so this
        # purely-typing change cannot regress behavior. A non-None default
        # for DIFFERENTIAL is a separate, behavior-changing knob.
        differential = NetClassRouting(
            name="Differential",
            priority=2,
            trace_width=0.15,
            clearance=0.15,
            cost_multiplier=0.85,
            length_critical=True,
        )
        assert differential.intra_pair_clearance is None
        assert differential.effective_intra_pair_clearance() == 0.15

    def test_rf_inline_default(self):
        rf = NetClassRouting(
            name="RF",
            priority=2,
            trace_width=0.2,
            clearance=0.2,
            cost_multiplier=0.9,
            length_critical=True,
            noise_sensitive=True,
        )
        assert rf.intra_pair_clearance is None
        assert rf.effective_intra_pair_clearance() == 0.2


class TestCorePyRuntimeOverride:
    """``core.py:2376`` constructs a runtime ``NetClassRouting`` per net.

    The construction site does not pass ``intra_pair_clearance``, so the
    field must default to ``None``. Mirror that construction shape here
    to lock in backward compatibility.
    """

    def test_runtime_matrix_override_defaults_to_none(self):
        override = NetClassRouting(
            name="matrix_NET_FOO",
            preferred_layers=[0, 1],
        )
        assert override.intra_pair_clearance is None
        assert override.effective_intra_pair_clearance() == override.clearance
