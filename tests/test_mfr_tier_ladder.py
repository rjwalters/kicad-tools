"""Tests for manufacturer tier-escalation ladder (Issue #2881).

Covers:
- ``MFR_TIER_LADDERS`` registry contents per family.
- ``get_mfr_tier_ladder()`` API:
  - returns ladder by manufacturer family
  - case-insensitive
  - resolves aliases
  - raises on unknown manufacturer
  - single-tier families return a one-element list
- ``can_escalate_via_in_pad()`` and ``can_escalate_scalar()`` convergence
  guards (the canonical via-in-pad-gain detector and scalar-relaxation
  detector used by the ``--auto-mfr-tier`` loop).
- ``MfrLimits.cost_note`` field plumbing.
"""

import pytest

from kicad_tools.router.mfr_limits import (
    MFR_JLCPCB,
    MFR_JLCPCB_TIER1,
    MFR_OSHPARK,
    MFR_PCBWAY,
    MFR_TIER_LADDERS,
    MfrLimits,
    can_escalate_scalar,
    can_escalate_via_in_pad,
    get_mfr_limits,
    get_mfr_tier_ladder,
)


class TestMfrTierLaddersRegistry:
    """Tests for the MFR_TIER_LADDERS dict shape."""

    def test_jlcpcb_ladder_has_tier1(self):
        """jlcpcb family must escalate to jlcpcb-tier1."""
        assert MFR_TIER_LADDERS["jlcpcb"] == ["jlcpcb", "jlcpcb-tier1"]

    def test_jlcpcb_tier1_already_at_top(self):
        """jlcpcb-tier1 has no further escalation -- single-element ladder."""
        assert MFR_TIER_LADDERS["jlcpcb-tier1"] == ["jlcpcb-tier1"]

    def test_seeed_ladder_escalates_to_jlcpcb_tier1(self):
        """seeed (aliased to JLCPCB-compat) escalates to jlcpcb-tier1."""
        assert MFR_TIER_LADDERS["seeed"] == ["seeed", "jlcpcb-tier1"]
        assert MFR_TIER_LADDERS["seeed-fusion"] == ["seeed-fusion", "jlcpcb-tier1"]

    def test_pcbway_single_tier(self):
        """pcbway has no tighter tier registered today -- single entry."""
        assert MFR_TIER_LADDERS["pcbway"] == ["pcbway"]

    def test_oshpark_single_tier(self):
        """oshpark has no tighter tier registered today -- single entry."""
        assert MFR_TIER_LADDERS["oshpark"] == ["oshpark"]

    def test_all_ladder_entries_resolve_to_known_manufacturers(self):
        """Every tier name in every ladder must exist in MFR_LIMITS."""
        for family, ladder in MFR_TIER_LADDERS.items():
            for tier_name in ladder:
                # Should not raise
                get_mfr_limits(tier_name)


class TestGetMfrTierLadder:
    """Tests for the public ``get_mfr_tier_ladder()`` function."""

    def test_jlcpcb_default_ladder(self):
        assert get_mfr_tier_ladder("jlcpcb") == ["jlcpcb", "jlcpcb-tier1"]

    def test_oshpark_single_tier_ladder(self):
        """Single-tier families return a one-element ladder."""
        assert get_mfr_tier_ladder("oshpark") == ["oshpark"]

    def test_case_insensitive(self):
        """Lookup is case-insensitive (matches get_mfr_limits())."""
        assert get_mfr_tier_ladder("JLCPCB") == ["jlcpcb", "jlcpcb-tier1"]
        assert get_mfr_tier_ladder("JlCpCb") == ["jlcpcb", "jlcpcb-tier1"]
        assert get_mfr_tier_ladder("OSHPark") == ["oshpark"]

    def test_alias_resolution(self):
        """Aliases (e.g., jlcpcb_tier1) resolve to canonical names."""
        # jlcpcb_tier1 -> jlcpcb-tier1 (single-element ladder at the top)
        assert get_mfr_tier_ladder("jlcpcb_tier1") == ["jlcpcb-tier1"]
        # jlcpcb-capabilityplus -> jlcpcb-tier1
        assert get_mfr_tier_ladder("jlcpcb-capabilityplus") == ["jlcpcb-tier1"]

    def test_unknown_manufacturer_raises(self):
        """Unknown manufacturer name raises ValueError (via get_mfr_limits)."""
        with pytest.raises(ValueError):
            get_mfr_tier_ladder("not-a-real-manufacturer")

    def test_returns_copy_not_internal_reference(self):
        """Caller can mutate returned list without breaking the registry."""
        ladder = get_mfr_tier_ladder("jlcpcb")
        ladder.append("evil")
        # Registry should be untouched
        assert MFR_TIER_LADDERS["jlcpcb"] == ["jlcpcb", "jlcpcb-tier1"]


class TestCanEscalateViaInPad:
    """Tests for the canonical via-in-pad-gain detector."""

    def test_jlcpcb_to_tier1_gains_via_in_pad(self):
        """The whole point of the registry: jlcpcb -> jlcpcb-tier1 unlocks via-in-pad."""
        assert can_escalate_via_in_pad("jlcpcb", "jlcpcb-tier1") is True

    def test_tier1_to_tier1_no_gain(self):
        """Same tier -> same tier is not a gain."""
        assert can_escalate_via_in_pad("jlcpcb-tier1", "jlcpcb-tier1") is False

    def test_pcbway_already_has_via_in_pad(self):
        """pcbway already supports via-in-pad; no further capability to gain."""
        # Going from pcbway (with VIP) to anything else is not a VIP-gain.
        assert can_escalate_via_in_pad("pcbway", "jlcpcb-tier1") is False
        assert can_escalate_via_in_pad("pcbway", "pcbway") is False

    def test_jlcpcb_to_pcbway_gains_via_in_pad(self):
        """jlcpcb (no VIP) -> pcbway (VIP) is a capability gain."""
        assert can_escalate_via_in_pad("jlcpcb", "pcbway") is True

    def test_unknown_manufacturer_returns_false(self):
        """Unknown manufacturer names -> False (no escalation)."""
        assert can_escalate_via_in_pad("not-real", "jlcpcb") is False
        assert can_escalate_via_in_pad("jlcpcb", "not-real") is False


class TestCanEscalateScalar:
    """Tests for the scalar-relaxation detector (clearance/trace/via)."""

    def test_jlcpcb_to_tier1_no_scalar_gain(self):
        """jlcpcb and jlcpcb-tier1 have identical scalar limits -- this is the
        Issue #2881 convergence guard test case. Tier escalation is meaningful
        ONLY through ``can_escalate_via_in_pad``."""
        assert can_escalate_scalar("jlcpcb", "jlcpcb-tier1") is False

    def test_oshpark_to_jlcpcb_gains_scalar(self):
        """oshpark (6mil/6mil) -> jlcpcb (5mil/5mil) is a scalar gain."""
        assert can_escalate_scalar("oshpark", "jlcpcb") is True

    def test_jlcpcb_to_pcbway_gains_via_drill(self):
        """jlcpcb (0.3 drill) -> pcbway (0.2 drill) is a via-drill gain."""
        assert can_escalate_scalar("jlcpcb", "pcbway") is True

    def test_same_manufacturer_no_gain(self):
        """Same -> same is not a gain on any axis."""
        assert can_escalate_scalar("jlcpcb", "jlcpcb") is False
        assert can_escalate_scalar("oshpark", "oshpark") is False

    def test_unknown_manufacturer_returns_false(self):
        assert can_escalate_scalar("not-real", "jlcpcb") is False
        assert can_escalate_scalar("jlcpcb", "not-real") is False


class TestCostNote:
    """Tests for the MfrLimits.cost_note field."""

    def test_cost_note_defaults_to_none(self):
        """cost_note is None by default (for tiers without a cost story)."""
        assert MFR_JLCPCB.cost_note is None
        assert MFR_OSHPARK.cost_note is None
        assert MFR_PCBWAY.cost_note is None

    def test_jlcpcb_tier1_has_cost_note(self):
        """The Capability+ tier has a cost note describing the surcharge."""
        assert MFR_JLCPCB_TIER1.cost_note is not None
        # Should mention surcharge in some form
        assert "surcharge" in MFR_JLCPCB_TIER1.cost_note.lower()

    def test_cost_note_field_accepts_custom_string(self):
        """Custom MfrLimits with a cost_note string works as expected."""
        custom = MfrLimits(
            name="custom",
            min_trace=0.1,
            min_clearance=0.1,
            min_via_drill=0.2,
            min_via_annular=0.1,
            cost_note="Adds $50 per order",
        )
        assert custom.cost_note == "Adds $50 per order"


class TestConvergenceGuardCase:
    """Issue #2881 acceptance: explicit convergence-guard test case.

    jlcpcb and jlcpcb-tier1 differ ONLY in ``via_in_pad_supported``; their
    scalar limits are identical.  The escalation loop must honor this:
    -- can_escalate_via_in_pad() must return True
    -- can_escalate_scalar()    must return False
    -- the loop should still escalate (driven by the capability gain).
    """

    def test_jlcpcb_to_tier1_only_capability_gain(self):
        """Identical scalars -> escalation is meaningful only via capability."""
        assert can_escalate_via_in_pad("jlcpcb", "jlcpcb-tier1") is True
        assert can_escalate_scalar("jlcpcb", "jlcpcb-tier1") is False

    def test_jlcpcb_and_tier1_scalar_limits_identical(self):
        """Sanity check: the convergence guard test case is real."""
        assert MFR_JLCPCB.min_clearance == MFR_JLCPCB_TIER1.min_clearance
        assert MFR_JLCPCB.min_trace == MFR_JLCPCB_TIER1.min_trace
        assert MFR_JLCPCB.min_via_drill == MFR_JLCPCB_TIER1.min_via_drill
        # The only difference is the capability flag:
        assert MFR_JLCPCB.via_in_pad_supported is False
        assert MFR_JLCPCB_TIER1.via_in_pad_supported is True
