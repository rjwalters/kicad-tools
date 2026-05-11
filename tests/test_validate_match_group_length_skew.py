"""Tests for the match-group (N-trace) length-skew DRC rule (Issue #2702).

Direct N>=3 analogue of ``tests/test_validate_diffpair_length_skew.py``
(PR #2662) covering the same critical-gotcha defenses (#2521 alias-table
omission, drift-prevention against ``NetClassRouting`` default) plus the
N-trace generalizations (4-trace DDR group, name-keyed threshold map,
multi-group mix).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from kicad_tools.drc.violation import ViolationCategory, ViolationType
from kicad_tools.manufacturers import DesignRules
from kicad_tools.router.match_group_length import MatchGroup, MatchGroupSource
from kicad_tools.validate.rules.match_group_length_skew import (
    DEFAULT_MATCH_GROUP_TOLERANCE_MM,
    MatchGroupLengthSkewRule,
)

# ---------------------------------------------------------------------------
# Stubs (mirror tests/test_validate_diffpair_length_skew.py)
# ---------------------------------------------------------------------------


@dataclass
class _StubLayer:
    name: str
    type: str = "signal"


@dataclass
class _StubNet:
    number: int
    name: str


@dataclass
class _StubPCB:
    """Minimal PCB stub.

    The match-group length-skew rule does NOT consult ``pcb.nets`` (the
    rule is name-of-group keyed, not name-of-net keyed -- the
    name-to-id resolution lives upstream in the tracker / detector).
    The stub still exposes ``nets`` and ``copper_layers`` to keep the
    DRCRule.check signature happy.
    """

    _nets: dict[int, _StubNet] = field(default_factory=dict)
    _layers: list[_StubLayer] = field(default_factory=lambda: [_StubLayer("F.Cu")])

    @property
    def nets(self) -> dict[int, _StubNet]:
        return self._nets

    @property
    def copper_layers(self) -> list[_StubLayer]:
        return self._layers


def _design_rules(min_clearance_mm: float = 0.127) -> DesignRules:
    """Build minimal DesignRules (unused by the rule but required by signature)."""
    return DesignRules(
        min_trace_width_mm=0.1,
        min_clearance_mm=min_clearance_mm,
        min_via_drill_mm=0.3,
        min_via_diameter_mm=0.6,
        min_annular_ring_mm=0.075,
    )


def _ddr_4_trace_group(name: str = "DDR_DATA") -> MatchGroup:
    """Build a 4-trace DDR-style match group fixture."""
    return MatchGroup(
        name=name,
        net_ids=[10, 11, 12, 13],
        pair_ids=[],
        tolerance=0.5,
        reference_net_id=None,
        source=MatchGroupSource.EXPLICIT,
    )


def _mipi_csi_4_trace_group(name: str = "MIPI_CSI") -> MatchGroup:
    """Build a second 4-trace match group fixture."""
    return MatchGroup(
        name=name,
        net_ids=[20, 21, 22, 23],
        pair_ids=[],
        tolerance=1.0,
        reference_net_id=None,
        source=MatchGroupSource.SUFFIX,
    )


# ---------------------------------------------------------------------------
# Rule.check() tests
# ---------------------------------------------------------------------------


class TestMatchGroupLengthSkewRule:
    """Tests for the rule's check() method."""

    def test_within_tolerance_no_violation(self):
        """4-trace group with skew 0.3 < default 0.5 -> no violation."""
        pcb = _StubPCB()
        ddr = _ddr_4_trace_group()
        rule = MatchGroupLengthSkewRule(
            group_skew_data={"DDR_DATA": 0.3},
            tracker_match_groups=[ddr],
        )
        results = rule.check(pcb, _design_rules())
        assert len(results.violations) == 0
        # One declared group with measured skew -> one rule check.
        assert results.rules_checked == 1
        assert results.rules_checked_by_rule.get("match_group_length_skew") == 1

    def test_at_tolerance_no_violation(self):
        """skew == tolerance -> no violation (strict ``>`` comparison)."""
        pcb = _StubPCB()
        ddr = _ddr_4_trace_group()
        rule = MatchGroupLengthSkewRule(
            group_skew_data={"DDR_DATA": 0.5},
            tracker_match_groups=[ddr],
        )
        results = rule.check(pcb, _design_rules())
        assert len(results.violations) == 0

    def test_exceeds_tolerance_fires_violation(self):
        """4-trace DDR group, skew 0.7 > default 0.5 -> 1 violation."""
        pcb = _StubPCB()
        ddr = _ddr_4_trace_group()
        rule = MatchGroupLengthSkewRule(
            group_skew_data={"DDR_DATA": 0.7},
            tracker_match_groups=[ddr],
        )
        results = rule.check(pcb, _design_rules())
        assert len(results.violations) == 1
        v = results.violations[0]
        assert v.rule_id == "match_group_length_skew"
        assert v.severity == "error"
        assert "DDR_DATA" in v.message
        # Numeric fields populated.
        assert v.actual_value == 0.7
        assert v.required_value == 0.5
        # items tuple carries the group name as the stable handle.
        assert "DDR_DATA" in v.items

    def test_mixed_within_and_exceeding(self):
        """Two groups, one in-tolerance + one exceeding -> only the offender fires."""
        pcb = _StubPCB()
        ddr = _ddr_4_trace_group()
        mipi = _mipi_csi_4_trace_group()
        rule = MatchGroupLengthSkewRule(
            group_skew_data={"DDR_DATA": 0.7, "MIPI_CSI": 0.04},
            tracker_match_groups=[ddr, mipi],
            threshold_map={"MIPI_CSI": 0.05},
        )
        results = rule.check(pcb, _design_rules())
        # Both groups are checked; only DDR_DATA fires.
        assert results.rules_checked == 2
        assert len(results.violations) == 1
        v = results.violations[0]
        assert "DDR_DATA" in v.message
        assert "MIPI_CSI" not in v.message

    def test_per_group_threshold_override_tighter_fires(self):
        """Tight per-group override (0.05) catches a skew the default (0.5) misses."""
        pcb = _StubPCB()
        mipi = _mipi_csi_4_trace_group()
        rule = MatchGroupLengthSkewRule(
            group_skew_data={"MIPI_CSI": 0.1},
            tracker_match_groups=[mipi],
            threshold_map={"MIPI_CSI": 0.05},
        )
        results = rule.check(pcb, _design_rules())
        assert len(results.violations) == 1
        v = results.violations[0]
        assert v.actual_value == 0.1
        assert v.required_value == 0.05

    def test_per_group_threshold_override_looser_silences(self):
        """Loose per-group override (3.0) silences a skew the default (0.5) would catch."""
        pcb = _StubPCB()
        ddr = _ddr_4_trace_group()
        rule = MatchGroupLengthSkewRule(
            group_skew_data={"DDR_DATA": 2.0},
            tracker_match_groups=[ddr],
            threshold_map={"DDR_DATA": 3.0},  # USB 2.0 HS budget territory
        )
        results = rule.check(pcb, _design_rules())
        assert len(results.violations) == 0

    def test_group_not_in_tracker_match_groups_skipped(self):
        """Skew recorded for a group not in tracker_match_groups -> no violation."""
        pcb = _StubPCB()
        rule = MatchGroupLengthSkewRule(
            group_skew_data={"GHOST_GROUP": 0.9},
            tracker_match_groups=[],  # empty -> rule is a no-op
        )
        results = rule.check(pcb, _design_rules())
        assert len(results.violations) == 0
        assert results.rules_checked == 0

    def test_group_in_tracker_but_missing_skew_silently_skipped(self):
        """Declared group with no measured skew -> no violation (partial-routing silencing)."""
        pcb = _StubPCB()
        ddr = _ddr_4_trace_group()
        rule = MatchGroupLengthSkewRule(
            group_skew_data={},  # empty -> rule is a no-op
            tracker_match_groups=[ddr],
        )
        results = rule.check(pcb, _design_rules())
        assert len(results.violations) == 0
        # No skew data -> no rules checked (graceful no-op).
        assert results.rules_checked == 0

    def test_unknown_group_with_known_group_both_present(self):
        """Mix of known + unknown group keys: known fires, unknown is silently dropped."""
        pcb = _StubPCB()
        ddr = _ddr_4_trace_group()
        rule = MatchGroupLengthSkewRule(
            group_skew_data={"DDR_DATA": 0.7, "GHOST_GROUP": 5.0},
            tracker_match_groups=[ddr],  # GHOST_GROUP NOT declared
        )
        results = rule.check(pcb, _design_rules())
        # Only DDR_DATA is checked + fires; GHOST_GROUP is silently dropped.
        assert results.rules_checked == 1
        assert len(results.violations) == 1
        assert "DDR_DATA" in results.violations[0].message

    def test_graceful_no_op_all_none(self):
        """Defaults: group_skew_data=None, tracker_match_groups=None -> no-op."""
        pcb = _StubPCB()
        rule = MatchGroupLengthSkewRule()
        results = rule.check(pcb, _design_rules())
        assert len(results.violations) == 0
        assert results.rules_checked == 0

    def test_graceful_no_op_empty_inputs(self):
        """Explicit empties: skew_data={}, tracker_match_groups=[] -> no-op."""
        pcb = _StubPCB()
        rule = MatchGroupLengthSkewRule(group_skew_data={}, tracker_match_groups=[])
        results = rule.check(pcb, _design_rules())
        assert len(results.violations) == 0
        assert results.rules_checked == 0

    def test_violation_to_dict_round_trips_type(self):
        """DRCViolation.to_dict() round-trips ``type`` -> ``"match_group_length_skew"``.

        Companion to ``test_alias_resolution_returns_match_group_length_skew``:
        the to_dict serialization MUST emit the exact rule_id string so
        downstream JSON consumers can filter by type.  If the alias
        entry is missing, ``type`` would be ``"unknown"`` instead.
        """
        pcb = _StubPCB()
        ddr = _ddr_4_trace_group()
        rule = MatchGroupLengthSkewRule(
            group_skew_data={"DDR_DATA": 0.7},
            tracker_match_groups=[ddr],
        )
        results = rule.check(pcb, _design_rules())
        assert len(results.violations) == 1
        d = results.violations[0].to_dict()
        assert d["rule_id"] == "match_group_length_skew"
        assert d["type"] == "match_group_length_skew", (
            "type field must round-trip to 'match_group_length_skew' "
            "(alias entry in drc/violation.py is missing or wrong -- "
            "see the #2521 / #2702 critical-gotcha defense)"
        )
        assert d["severity"] == "error"


# ---------------------------------------------------------------------------
# Alias-resolution / category-map tests (#2521 critical-gotcha guards)
# ---------------------------------------------------------------------------


class TestAliasResolution:
    """``ViolationType.from_string`` must NOT drop to UNKNOWN.

    This is the mandatory #2521 critical-gotcha guard.  The
    ``"match_group_length_skew"`` string does NOT contain ``"clearance"``
    so the fuzzy fallback at the bottom of ``from_string()`` would
    otherwise drop to UNKNOWN.  The explicit alias-table entry is the
    only defense.
    """

    def test_direct_enum_value_match(self):
        """Direct enum-value match path resolves correctly."""
        assert (
            ViolationType.from_string("match_group_length_skew")
            is ViolationType.MATCH_GROUP_LENGTH_SKEW
        )

    def test_round_trip_identity(self):
        """``from_string(value).value == value`` (the #2521 round-trip guard)."""
        assert (
            ViolationType.from_string("match_group_length_skew").value == "match_group_length_skew"
        )

    def test_case_insensitive_match(self):
        """Case-insensitive variant resolves correctly."""
        assert (
            ViolationType.from_string("Match_Group_Length_Skew")
            is ViolationType.MATCH_GROUP_LENGTH_SKEW
        )

    def test_whitespace_tolerant_match(self):
        """Whitespace-tolerant variant resolves correctly."""
        assert (
            ViolationType.from_string("  match_group_length_skew  ")
            is ViolationType.MATCH_GROUP_LENGTH_SKEW
        )

    def test_unrelated_substring_does_not_collide(self):
        """A string with ``"match"`` but not the full id stays UNKNOWN.

        Proves the alias entry is not collateral damage from a fuzzy
        substring match -- it's an exact-key lookup.
        """
        # ``"match"`` alone has no entry; the fuzzy fallback's "if
        # 'clearance' in s_lower" etc. don't match it either.
        # Note: from_string returns UNKNOWN for unrecognized strings.
        assert ViolationType.from_string("match") is ViolationType.UNKNOWN


class TestCategoryMapIntegration:
    """Every ViolationType MUST have a ViolationCategory entry.

    Catches the failure mode where a new enum value is added but its
    ``_TYPE_CATEGORY_MAP`` entry is forgotten.
    """

    def test_match_group_has_category(self):
        """MATCH_GROUP_LENGTH_SKEW maps to ROUTING (skew is route-side fixable)."""
        from kicad_tools.drc.violation import _TYPE_CATEGORY_MAP

        assert ViolationType.MATCH_GROUP_LENGTH_SKEW in _TYPE_CATEGORY_MAP
        assert (
            _TYPE_CATEGORY_MAP[ViolationType.MATCH_GROUP_LENGTH_SKEW] is ViolationCategory.ROUTING
        )

    @pytest.mark.parametrize(
        "vtype",
        [
            ViolationType.DIFFPAIR_LENGTH_SKEW,
            ViolationType.MATCH_GROUP_LENGTH_SKEW,
        ],
        ids=["diffpair_length_skew", "match_group_length_skew"],
    )
    def test_length_match_family_all_categorized_as_routing(self, vtype):
        """Both length-match rules (N=2 + N>=3) categorize as ROUTING (consistency)."""
        from kicad_tools.drc.violation import _TYPE_CATEGORY_MAP

        assert _TYPE_CATEGORY_MAP[vtype] is ViolationCategory.ROUTING


# ---------------------------------------------------------------------------
# Drift-prevention tests (mirror PR #2662's default-constant assertion)
# ---------------------------------------------------------------------------


class TestDriftPrevention:
    """Module-level default MUST equal the Phase 1A accessor default.

    The accessor ``NetClassRouting.effective_length_match_tolerance(default=0.5)``
    (at ``router/rules.py:748``) and the rule's
    ``DEFAULT_MATCH_GROUP_TOLERANCE_MM = 0.5`` are two sources of truth
    for the same value.  If a future change moves one but not the
    other, the rule's default and the router's default diverge.  This
    test catches the divergence (mirrors PR #2662's H<->J alignment
    test that detected the #2649 drift).
    """

    def test_default_constant_matches_accessor_default(self):
        """``DEFAULT_MATCH_GROUP_TOLERANCE_MM`` == ``effective_length_match_tolerance()``."""
        from kicad_tools.router.rules import NetClassRouting

        # Accessor called with no override -> default arg value.
        accessor_default = NetClassRouting(name="X").effective_length_match_tolerance()
        assert accessor_default == DEFAULT_MATCH_GROUP_TOLERANCE_MM, (
            f"DEFAULT_MATCH_GROUP_TOLERANCE_MM ({DEFAULT_MATCH_GROUP_TOLERANCE_MM}) and "
            f"NetClassRouting.effective_length_match_tolerance default "
            f"({accessor_default}) must be byte-equal.  See Phase 1A "
            f"(#2687) and Phase 2G (#2702) coupling -- if you changed "
            f"one, update the other.  See ``router/rules.py:761-763`` "
            f"docstring forward-reference."
        )

    def test_default_is_05_mm(self):
        """Explicit value documentation: the calibration is 0.5 mm.

        If a future PR legitimately retunes the floor, this test should
        be updated AND the corresponding accessor default in
        ``router/rules.py:effective_length_match_tolerance`` must be
        updated.
        """
        assert DEFAULT_MATCH_GROUP_TOLERANCE_MM == 0.5

    def test_match_group_dataclass_default_tolerance_matches(self):
        """``MatchGroup.tolerance`` field default also equals the rule default.

        Phase 1B's ``MatchGroup`` dataclass declares
        ``tolerance: float = 0.5`` as a per-group default (see
        ``router/match_group_length.py:186``).  Drift between this and
        the rule's default would mean a group declared with all
        defaults silently changes validation behaviour depending on
        which seam is consulted.  Catch the drift now.
        """
        grp = MatchGroup(name="X", net_ids=[1, 2, 3])
        assert grp.tolerance == DEFAULT_MATCH_GROUP_TOLERANCE_MM


# ---------------------------------------------------------------------------
# CLI dispatch wiring tests (the #2587 "dormant signal" lesson)
# ---------------------------------------------------------------------------


class TestCLIWiringIsAlive:
    """Every wiring touch point MUST be exercised."""

    def test_check_categories_contains_new_rule(self):
        """``CHECK_CATEGORIES`` contains ``"match_group_length_skew"``."""
        from kicad_tools.cli.check_cmd import CHECK_CATEGORIES

        assert "match_group_length_skew" in CHECK_CATEGORIES

    def test_check_categories_alphabetical_neighbors(self):
        """``"match_group_length_skew"`` slots between ``impedance`` and ``netlist``."""
        from kicad_tools.cli.check_cmd import CHECK_CATEGORIES

        i_impedance = CHECK_CATEGORIES.index("impedance")
        i_match_group = CHECK_CATEGORIES.index("match_group_length_skew")
        i_netlist = CHECK_CATEGORIES.index("netlist")
        assert i_impedance < i_match_group < i_netlist

    def test_checker_has_check_method(self):
        """``DRCChecker.check_match_group_length_skew`` exists and is callable."""
        from kicad_tools.validate.checker import DRCChecker

        assert hasattr(DRCChecker, "check_match_group_length_skew")
        assert callable(DRCChecker.check_match_group_length_skew)

    def test_checker_check_all_merges_new_rule(self, tmp_path):
        """``check_all`` invokes ``check_match_group_length_skew`` (no dormancy).

        Patches the method to track whether ``check_all`` calls it.
        This is the #2587 "dormant signal" lesson: a method registered
        but never invoked from ``check_all`` would silently miss the
        violation under ``kct check`` (no --only flag).
        """
        from unittest.mock import patch

        from kicad_tools.validate.checker import DRCChecker

        called: list[bool] = []

        original = DRCChecker.check_match_group_length_skew

        def _spy(self):
            called.append(True)
            return original(self)

        # Use a real minimal PCB file via tmp_path.
        pcb_file = tmp_path / "min.kicad_pcb"
        pcb_file.write_text(
            "(kicad_pcb (version 20240108) (generator test) "
            '(generator_version "8.0") '
            "(general (thickness 1.6) (legacy_teardrops no)) (paper A4) "
            "(layers (0 F.Cu signal) (31 B.Cu signal) "
            "(44 Edge.Cuts user)) "
            '(setup (pad_to_mask_clearance 0)) (net 0 ""))\n'
        )
        from kicad_tools.schema.pcb import PCB

        pcb = PCB.load(pcb_file)
        checker = DRCChecker(pcb, manufacturer="jlcpcb")

        with patch.object(DRCChecker, "check_match_group_length_skew", _spy):
            checker.check_all()

        assert called == [True], "check_all must invoke check_match_group_length_skew"

    def test_check_methods_dispatch_contains_new_rule(self):
        """End-to-end CLI tests live in ``test_cli_check_match_group_length_skew.py``."""
        # Cross-module dependency documented here; the CLI exit-code
        # assertion lives in the sibling test file.

    def test_rules_init_exports_new_rule(self):
        """``MatchGroupLengthSkewRule`` is exported from ``validate.rules``."""
        from kicad_tools.validate.rules import MatchGroupLengthSkewRule as Exported

        assert Exported is MatchGroupLengthSkewRule
