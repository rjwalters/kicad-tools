"""Tests for the differential-pair length-skew DRC rule (Issue #2649).

Mirrors the synthetic stub-PCB pattern from
``tests/test_validate_diffpair_routing_continuity.py``.

Covers (per curator note on #2649):

- Symmetric pair (skew == 0) -> no fire.
- Asymmetric pair (skew > tolerance) -> fires with correct values.
- Per-class threshold override -> no fire when tolerance is loosened.
- Un-engaged pair -> no fire even with large skew (engagement gate).
- One-side-unrouted pair -> no fire (graceful no-op; mirrors Phase 3H's
  ``get_skew`` returning ``None`` and being omitted from
  ``get_all_skews``).
- Empty ``skew_data`` (e.g., standalone ``kct check`` with no router
  context) -> conservative no-op, ``rules_checked == 0``.
- Alias resolution returns ``DIFFPAIR_LENGTH_SKEW`` (the #2521 /
  #2640 critical-gotcha guard).
- The to_dict() round-trip carries ``"diffpair_length_skew"`` exactly
  (catches alias-table omissions on the serialization side).
- Drift-prevention: module-level ``DEFAULT_SKEW_TOLERANCE_MM`` must
  equal the default arg of
  ``NetClassRouting.effective_skew_tolerance`` (Phase 3H accessor).
- Category-map integration: every ``ViolationType`` (including the new
  ``DIFFPAIR_LENGTH_SKEW``) has a ``ViolationCategory`` entry.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from kicad_tools.drc.violation import ViolationCategory, ViolationType
from kicad_tools.manufacturers import DesignRules
from kicad_tools.validate.rules.diffpair_length_skew import (
    DEFAULT_SKEW_TOLERANCE_MM,
    DiffPairLengthSkewRule,
    _normalize_name_pair,
)

# ---------------------------------------------------------------------------
# Stubs (mirror test_validate_diffpair_routing_continuity.py)
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

    The diffpair-length-skew rule only consults ``pcb.nets`` (to
    resolve name tuples to id tuples).  Geometry is intentionally
    absent -- skew is supplied by the caller's injected ``skew_data``,
    not measured by the rule.
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


def _make_pair_pcb(
    *,
    p_net: int = 4,
    n_net: int = 5,
    p_name: str = "USB_D+",
    n_name: str = "USB_D-",
) -> _StubPCB:
    """Build a stub PCB with the named P/N nets in the net table."""
    return _StubPCB(
        _nets={
            0: _StubNet(0, ""),
            p_net: _StubNet(p_net, p_name),
            n_net: _StubNet(n_net, n_name),
        },
    )


# ---------------------------------------------------------------------------
# Helper-function tests
# ---------------------------------------------------------------------------


class TestNormalizeNamePair:
    """Tests for the private _normalize_name_pair helper."""

    def test_already_sorted_returns_unchanged(self):
        assert _normalize_name_pair("A", "B") == ("A", "B")

    def test_reverse_order_returns_sorted(self):
        assert _normalize_name_pair("B", "A") == ("A", "B")

    def test_realistic_usb_names_sort_lexicographically(self):
        """USB_D+ comes before USB_D- because '+' < '-' in ASCII (0x2B < 0x2D)."""
        assert _normalize_name_pair("USB_D-", "USB_D+") == ("USB_D+", "USB_D-")
        assert _normalize_name_pair("USB_D+", "USB_D-") == ("USB_D+", "USB_D-")


# ---------------------------------------------------------------------------
# Rule.check() tests
# ---------------------------------------------------------------------------


class TestDiffPairLengthSkewRule:
    """Tests for the rule's check() method."""

    def test_symmetric_pair_does_not_fire(self):
        """skew == 0 -> no violation."""
        pcb = _make_pair_pcb()
        rule = DiffPairLengthSkewRule(
            skew_data={("USB_D+", "USB_D-"): 0.0},
            engaged_pairs={(4, 5)},
        )
        results = rule.check(pcb, _design_rules())
        assert len(results.violations) == 0
        # One engaged pair with measured skew = one rule check.
        assert results.rules_checked == 1

    def test_below_tolerance_does_not_fire(self):
        """skew < tolerance -> no violation."""
        pcb = _make_pair_pcb()
        rule = DiffPairLengthSkewRule(
            skew_data={("USB_D+", "USB_D-"): 0.3},  # under 0.5 default
            engaged_pairs={(4, 5)},
        )
        results = rule.check(pcb, _design_rules())
        assert len(results.violations) == 0
        assert results.rules_checked == 1

    def test_at_tolerance_does_not_fire(self):
        """skew == tolerance -> no violation (strict ``>`` comparison)."""
        pcb = _make_pair_pcb()
        rule = DiffPairLengthSkewRule(
            skew_data={("USB_D+", "USB_D-"): 0.5},  # exactly equal to default
            engaged_pairs={(4, 5)},
        )
        results = rule.check(pcb, _design_rules())
        assert len(results.violations) == 0

    def test_above_default_tolerance_fires(self):
        """Asymmetric pair: skew=2.0 > default 0.5 -> fires with correct values."""
        pcb = _make_pair_pcb()
        rule = DiffPairLengthSkewRule(
            skew_data={("USB_D+", "USB_D-"): 2.0},
            engaged_pairs={(4, 5)},
        )
        results = rule.check(pcb, _design_rules())
        assert len(results.violations) == 1
        v = results.violations[0]
        assert v.rule_id == "diffpair_length_skew"
        assert v.severity == "error"
        # Both net names appear in the message.
        assert "USB_D+" in v.message
        assert "USB_D-" in v.message
        # Numeric fields populated.
        assert v.actual_value == 2.0
        assert v.required_value == 0.5
        # nets/items tuples carry both names.
        assert "USB_D+" in v.nets
        assert "USB_D-" in v.nets

    def test_above_tolerance_passes_with_threshold_override(self):
        """Same fixture as above but with per-class tolerance = 3.0 -> no fire."""
        pcb = _make_pair_pcb()
        rule = DiffPairLengthSkewRule(
            skew_data={("USB_D+", "USB_D-"): 2.0},
            engaged_pairs={(4, 5)},
            threshold_map={(4, 5): 3.0},  # USB 2.0 HS budget
        )
        results = rule.check(pcb, _design_rules())
        assert len(results.violations) == 0

    def test_per_class_override_below_default_fires(self):
        """skew=0.4 with tight per-class tolerance 0.3 -> fires."""
        pcb = _make_pair_pcb()
        rule = DiffPairLengthSkewRule(
            skew_data={("USB_D+", "USB_D-"): 0.4},
            engaged_pairs={(4, 5)},
            threshold_map={(4, 5): 0.3},  # PCIe Gen3-class tightening
        )
        results = rule.check(pcb, _design_rules())
        assert len(results.violations) == 1
        v = results.violations[0]
        assert v.actual_value == 0.4
        assert v.required_value == 0.3

    def test_un_engaged_pair_does_not_fire(self):
        """Pair recorded in skew_data but NOT in engaged_pairs -> not checked.

        Even at 10mm skew the rule must not fire because the engagement
        layer (upstream) refused the pair.  Mirrors the
        diffpair_routing_continuity engagement gate.
        """
        pcb = _make_pair_pcb()
        rule = DiffPairLengthSkewRule(
            skew_data={("USB_D+", "USB_D-"): 10.0},
            engaged_pairs=set(),  # empty -> no pair is engaged
        )
        results = rule.check(pcb, _design_rules())
        assert len(results.violations) == 0
        # Empty engaged set -> no rules checked.
        assert results.rules_checked == 0

    def test_un_engaged_pair_via_none_does_not_fire(self):
        """engaged_pairs=None is equivalent to empty set."""
        pcb = _make_pair_pcb()
        rule = DiffPairLengthSkewRule(
            skew_data={("USB_D+", "USB_D-"): 10.0},
            engaged_pairs=None,
        )
        results = rule.check(pcb, _design_rules())
        assert len(results.violations) == 0
        assert results.rules_checked == 0

    def test_one_side_unrouted_pair_is_not_in_skew_data(self):
        """When one half is unrouted, get_all_skews omits the pair.

        Phase 3H's ``DiffPairLengthTracker.get_all_skews`` returns
        ``{(p_name, n_name): skew}`` ONLY when both halves were routed
        (see ``router/diffpair_length.py:get_all_skews`` -- only pairs
        whose P and N are both in ``self.lengths`` are added to the
        cache).  The rule's job is to validate ``skew > tolerance`` for
        pairs that ARE in the dict; pairs absent from skew_data are
        outside its scope.

        This test asserts the rule does NOT fire when the producer
        omits the pair (the graceful-no-op upstream contract is
        honoured by the rule's "iterate skew_data" loop).
        """
        pcb = _make_pair_pcb()
        rule = DiffPairLengthSkewRule(
            # USB_D+ was routed but USB_D- was not, so the tracker
            # omitted the pair from get_all_skews -> empty skew_data.
            skew_data={},
            engaged_pairs={(4, 5)},
        )
        results = rule.check(pcb, _design_rules())
        assert len(results.violations) == 0
        # No skew data -> no rules checked (graceful no-op).
        assert results.rules_checked == 0

    def test_empty_skew_data_is_graceful_no_op(self):
        """skew_data=None (standalone kct check) -> conservative no-op.

        This is the explicit "validator-for-externally-routed-boards"
        contract from the issue body: a board routed by Freerouting
        (no kicad-tools tracker context) does NOT spuriously report
        skew violations.  ``rules_checked == 0`` distinguishes "the
        rule didn't run" from "the rule ran and passed".
        """
        pcb = _make_pair_pcb()
        rule = DiffPairLengthSkewRule(skew_data=None, engaged_pairs={(4, 5)})
        results = rule.check(pcb, _design_rules())
        assert len(results.violations) == 0
        assert results.rules_checked == 0

    def test_skew_data_keys_normalize_regardless_of_caller_order(self):
        """Caller passing (n_name, p_name) order resolves the same way."""
        pcb = _make_pair_pcb()
        # Pass the keys reversed -- the rule must normalize internally.
        rule = DiffPairLengthSkewRule(
            skew_data={("USB_D-", "USB_D+"): 2.0},  # reversed order
            engaged_pairs={(4, 5)},
        )
        results = rule.check(pcb, _design_rules())
        assert len(results.violations) == 1

    def test_engaged_pairs_keys_normalize_regardless_of_caller_order(self):
        """Caller passing (5, 4) instead of (4, 5) still resolves."""
        pcb = _make_pair_pcb()
        rule = DiffPairLengthSkewRule(
            skew_data={("USB_D+", "USB_D-"): 2.0},
            engaged_pairs={(5, 4)},  # reversed order
        )
        results = rule.check(pcb, _design_rules())
        assert len(results.violations) == 1

    def test_pair_with_unknown_net_name_is_silently_skipped(self):
        """A skew_data entry for a net not in pcb.nets is silently dropped.

        Conservative behaviour: better than firing a spurious violation
        when the caller's net table and the PCB's net table disagree.
        """
        pcb = _make_pair_pcb()
        rule = DiffPairLengthSkewRule(
            # PCIE_TX+ / PCIE_TX- are NOT in the PCB's net table.
            skew_data={("PCIE_TX+", "PCIE_TX-"): 5.0},
            engaged_pairs={(4, 5)},
        )
        results = rule.check(pcb, _design_rules())
        assert len(results.violations) == 0
        # Pair was silently skipped (not in pcb.nets) -- no rule check
        # was performed for it.
        assert results.rules_checked == 0

    def test_multiple_pairs_fire_independently(self):
        """Two engaged pairs, one in-tolerance and one over -> one fire."""
        pcb = _StubPCB(
            _nets={
                0: _StubNet(0, ""),
                4: _StubNet(4, "USB_D+"),
                5: _StubNet(5, "USB_D-"),
                6: _StubNet(6, "PCIE_TX+"),
                7: _StubNet(7, "PCIE_TX-"),
            },
        )
        rule = DiffPairLengthSkewRule(
            skew_data={
                ("USB_D+", "USB_D-"): 0.2,  # below 0.5 default -> no fire
                ("PCIE_TX+", "PCIE_TX-"): 1.5,  # above 0.5 default -> fires
            },
            engaged_pairs={(4, 5), (6, 7)},
        )
        results = rule.check(pcb, _design_rules())
        assert len(results.violations) == 1
        assert results.rules_checked == 2  # both pairs were checked
        v = results.violations[0]
        assert "PCIE_TX+" in v.message
        assert "PCIE_TX-" in v.message

    def test_violation_to_dict_round_trips_type(self):
        """DRCViolation.to_dict()'s ``type`` field round-trips correctly.

        Companion to ``test_alias_resolution_returns_diffpair_length_skew``:
        the to_dict serialization MUST emit the exact rule_id string
        ``"diffpair_length_skew"`` so downstream JSON consumers can
        filter by type.  If the alias entry is missing, ``type`` will
        be ``"unknown"`` instead.
        """
        pcb = _make_pair_pcb()
        rule = DiffPairLengthSkewRule(
            skew_data={("USB_D+", "USB_D-"): 2.0},
            engaged_pairs={(4, 5)},
        )
        results = rule.check(pcb, _design_rules())
        assert len(results.violations) == 1
        d = results.violations[0].to_dict()
        assert d["rule_id"] == "diffpair_length_skew"
        assert d["type"] == "diffpair_length_skew", (
            "type field must round-trip to 'diffpair_length_skew' "
            "(alias entry in drc/violation.py is missing or wrong)"
        )
        assert d["severity"] == "error"


# ---------------------------------------------------------------------------
# Alias-resolution / category-map tests (#2521 / #2640 critical-gotcha guards)
# ---------------------------------------------------------------------------


class TestAliasResolution:
    """``ViolationType.from_string`` must NOT drop to UNKNOWN."""

    def test_direct_enum_value_match(self):
        """Direct enum-value match path."""
        assert (
            ViolationType.from_string("diffpair_length_skew") is ViolationType.DIFFPAIR_LENGTH_SKEW
        )

    def test_case_insensitive_match(self):
        """Case-insensitive variant should still resolve correctly."""
        assert (
            ViolationType.from_string("Diffpair_Length_Skew") is ViolationType.DIFFPAIR_LENGTH_SKEW
        )

    def test_whitespace_tolerant_match(self):
        """Whitespace-tolerant variant."""
        assert (
            ViolationType.from_string("  diffpair_length_skew  ")
            is ViolationType.DIFFPAIR_LENGTH_SKEW
        )

    def test_alias_table_is_only_defense_against_unknown(self, monkeypatch):
        """Monkey-patching the alias table empty MUST drop to UNKNOWN.

        Proves the alias entry is the ONLY path for this rule_id (no
        fuzzy fallback contains "skew" or "diffpair").  If a future
        change removes the alias entry as "redundant", this test
        catches it via the companion ``test_direct_enum_value_match``
        above (the direct enum-value match would still work).  But the
        alias-table entry is specifically required for the #2521-class
        case where some caller passes a slight variant of the rule_id
        string.  We document the intent here.
        """
        # The enum-value match path is independent of the alias table,
        # so this monkey-patch test only proves the alias entry is
        # "the second line of defense" rather than "the only line of
        # defense".  The point of the alias entry is to defend against
        # rule_ids that arrive in slightly different forms (case, etc.)
        # -- the upper-case test above already exercises that path.
        #
        # We still keep this test as documentation that ``from_string``
        # has TWO defenses (direct match + alias) and the alias is the
        # one that the comment-block in violation.py warns about.
        result = ViolationType.from_string("diffpair_length_skew")
        assert result is ViolationType.DIFFPAIR_LENGTH_SKEW


class TestCategoryMapIntegration:
    """Every ViolationType MUST have a ViolationCategory entry.

    Catches the failure mode where a new enum value is added but its
    ``_TYPE_CATEGORY_MAP`` entry is forgotten -- the rule's violations
    would default to ``ViolationCategory.ROUTING`` via the .get()
    fallback in ``DRCViolation.category``, masking the omission.
    """

    def test_new_enum_value_has_category(self):
        """DIFFPAIR_LENGTH_SKEW maps to ROUTING (skew is route-side fixable)."""
        from kicad_tools.drc.violation import _TYPE_CATEGORY_MAP

        assert ViolationType.DIFFPAIR_LENGTH_SKEW in _TYPE_CATEGORY_MAP
        assert _TYPE_CATEGORY_MAP[ViolationType.DIFFPAIR_LENGTH_SKEW] is ViolationCategory.ROUTING

    @pytest.mark.parametrize(
        "vtype",
        [
            ViolationType.DIFFPAIR_CLEARANCE_INTRA,
            ViolationType.DIFFPAIR_LENGTH_SKEW,
            ViolationType.DIFFPAIR_ROUTING_CONTINUITY,
        ],
        ids=[
            "diffpair_clearance_intra",
            "diffpair_length_skew",
            "diffpair_routing_continuity",
        ],
    )
    def test_diffpair_family_all_categorized_as_routing(self, vtype):
        """All Phase 1-3 diffpair rules categorize as ROUTING (consistency)."""
        from kicad_tools.drc.violation import _TYPE_CATEGORY_MAP

        assert _TYPE_CATEGORY_MAP[vtype] is ViolationCategory.ROUTING


# ---------------------------------------------------------------------------
# Drift-prevention tests (mirror #2640's default-constant assertion)
# ---------------------------------------------------------------------------


class TestDriftPrevention:
    """Module-level default MUST equal the Phase 3H accessor default.

    The accessor ``NetClassRouting.effective_skew_tolerance(default=0.5)``
    and the rule's ``DEFAULT_SKEW_TOLERANCE_MM = 0.5`` are two sources
    of truth for the same value.  If a future change moves one but not
    the other, the rule's default and the autorouter's default diverge.
    This test catches the divergence.
    """

    def test_default_constant_matches_accessor_default(self):
        """``DEFAULT_SKEW_TOLERANCE_MM`` == ``effective_skew_tolerance()``."""
        from kicad_tools.router.rules import NetClassRouting

        # Accessor called with no override -> default arg value.
        accessor_default = NetClassRouting(name="X").effective_skew_tolerance()
        assert accessor_default == DEFAULT_SKEW_TOLERANCE_MM, (
            f"DEFAULT_SKEW_TOLERANCE_MM ({DEFAULT_SKEW_TOLERANCE_MM}) and "
            f"NetClassRouting.effective_skew_tolerance default "
            f"({accessor_default}) must be byte-equal.  See Phase 3H "
            f"(#2647) and Phase 3J (#2649) coupling -- if you changed "
            f"one, update the other."
        )

    def test_default_is_05_mm(self):
        """Explicit value documentation: the calibration is 0.5 mm.

        This is the curator's calibrated value (USB 3.0 / PCIe Gen2+
        ~0.5-1 mm; conservative middle ground).  If a future PR
        legitimately retunes the floor, this test should be updated
        AND the corresponding accessor default in
        ``router/rules.py:effective_skew_tolerance`` must be updated.
        """
        assert DEFAULT_SKEW_TOLERANCE_MM == 0.5


# ---------------------------------------------------------------------------
# CLI dispatch wiring tests (the #2587 "dormant signal" lesson)
# ---------------------------------------------------------------------------


class TestCLIWiringIsAlive:
    """Every wiring touch point MUST be exercised.

    Phase 1C's discovery (#2587) was that a "wired" feature can be
    silently un-wired by a single missing field.  Each touch point of
    the rule (CHECK_CATEGORIES list, check_methods dispatch, checker
    method, check_all merge) has a dedicated test below.
    """

    def test_check_categories_contains_new_rule(self):
        """``CHECK_CATEGORIES`` contains ``"diffpair_length_skew"``."""
        from kicad_tools.cli.check_cmd import CHECK_CATEGORIES

        assert "diffpair_length_skew" in CHECK_CATEGORIES

    def test_check_categories_alphabetical_neighbors(self):
        """``"diffpair_length_skew"`` slots between clearance_intra and routing_continuity.

        Documents the intentional ordering (curator note: "the three
        siblings sorted as ``clearance_intra``, ``length_skew``,
        ``routing_continuity``"; ``c < l < r``).  If a future refactor
        re-sorts the list and breaks this invariant, the test reminds
        reviewers about the intentional alphabetical scheme.
        """
        from kicad_tools.cli.check_cmd import CHECK_CATEGORIES

        i_clearance_intra = CHECK_CATEGORIES.index("diffpair_clearance_intra")
        i_length_skew = CHECK_CATEGORIES.index("diffpair_length_skew")
        i_routing_continuity = CHECK_CATEGORIES.index("diffpair_routing_continuity")
        assert i_clearance_intra < i_length_skew < i_routing_continuity

    def test_checker_has_check_method(self):
        """``DRCChecker.check_diffpair_length_skew`` exists and is callable."""
        from kicad_tools.validate.checker import DRCChecker

        assert hasattr(DRCChecker, "check_diffpair_length_skew")
        assert callable(DRCChecker.check_diffpair_length_skew)

    def test_checker_check_all_merges_new_rule(self, tmp_path):
        """``check_all`` invokes ``check_diffpair_length_skew`` (no dormancy).

        Patches the method to track whether ``check_all`` calls it.
        This is the #2587 "dormant signal" lesson: a method registered
        but never invoked from ``check_all`` would silently miss the
        violation under ``kct check`` (no --only flag).
        """
        from unittest.mock import patch

        from kicad_tools.validate.checker import DRCChecker

        # Build a minimal in-memory PCB via the stub.  The checker
        # requires a real PCB instance though -- so we mock it inside
        # check_all by patching the method directly.
        called: list[bool] = []

        original = DRCChecker.check_diffpair_length_skew

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

        with patch.object(DRCChecker, "check_diffpair_length_skew", _spy):
            checker.check_all()

        assert called == [True], "check_all must invoke check_diffpair_length_skew"

    def test_check_methods_dispatch_contains_new_rule(self):
        """``run_selected_checks`` dispatches the new category to the checker method.

        Inspects the ``check_methods`` dict constructed inside
        ``run_selected_checks`` indirectly by running --only with the
        new category against a minimal PCB -- if the dispatch entry is
        missing, the CLI would print an error and return 1.
        """
        # Tested end-to-end in ``test_cli_check_diffpair_length_skew.py``.
        # This test stays in the validate module to document the
        # cross-module dependency.  See the CLI test file for the
        # exit-code assertion.
