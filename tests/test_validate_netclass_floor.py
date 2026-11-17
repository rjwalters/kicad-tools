"""Tests for the ``netclass_floor`` DRC rule (Issue #5875).

The rule compares a ``.kicad_pro``'s declared netclass defaults against the
active manufacturer profile's floors, so a board whose netclass is
guaranteed to produce illegal copper fails ``kct check --mfr`` *before* any
copper exists.

Covers both halves of the acceptance criteria: a below-floor netclass fires
(naming class, field, actual and required), a conforming netclass is silent,
non-JLCPCB profiles resolve their own floors, and the 1-layer-vs-2+-layer
severity branch for the three via findings.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kicad_tools.validate.rules.netclass_floor import (
    NETCLASS_FLOOR_RULE_IDS,
    RULE_ANNULAR_RING,
    RULE_CLEARANCE,
    RULE_TRACK_WIDTH,
    RULE_VIA_DIAMETER,
    RULE_VIA_DRILL,
    check_netclass_floor,
)

# The exact netclass every ``circuit-json-to-kicad`` project carries
# (Issue #5875 / the tscircuit interop gate, Issue #5847): below JLCPCB's
# floor on track_width, clearance, via_diameter, via_drill and the implied
# annular ring all at once.
TSCIRCUIT_NETCLASS = {
    "name": "Default",
    "track_width": 0.1,
    "via_diameter": 0.3,
    "via_drill": 0.2,
    "clearance": 0.1,
}

# KiCad/kicad-tools stock Default -- conforming at the jlcpcb 2-layer tier
# (0.127 trace/space, 0.3 drill, 0.6 OD, 0.15 ring).
CONFORMING_NETCLASS = {
    "name": "Default",
    "track_width": 0.25,
    "via_diameter": 0.6,
    "via_drill": 0.3,
    "clearance": 0.15,
}


class MockDesignRules:
    """Only the five floors this rule reads (mirrors test_validate_dimensions)."""

    def __init__(
        self,
        min_trace_width_mm: float = 0.127,
        min_clearance_mm: float = 0.127,
        min_via_drill_mm: float = 0.3,
        min_via_diameter_mm: float = 0.6,
        min_annular_ring_mm: float = 0.15,
    ):
        self.min_trace_width_mm = min_trace_width_mm
        self.min_clearance_mm = min_clearance_mm
        self.min_via_drill_mm = min_via_drill_mm
        self.min_via_diameter_mm = min_via_diameter_mm
        self.min_annular_ring_mm = min_annular_ring_mm


def _write_project(
    tmp_path: Path,
    classes: list[dict] | None,
    *,
    patterns: list[dict] | None = None,
    stem: str = "board",
) -> Path:
    """Write ``<stem>.kicad_pro`` and return the sibling ``.kicad_pcb`` path.

    The PCB file itself is never parsed by the rule, so it is not created --
    only its path is used to derive the project path.
    """
    project: dict = {"board": {}, "meta": {"version": 1}}
    if classes is not None:
        net_settings: dict = {"classes": classes, "meta": {"version": 3}}
        if patterns is not None:
            net_settings["netclass_patterns"] = patterns
        project["net_settings"] = net_settings
    (tmp_path / f"{stem}.kicad_pro").write_text(json.dumps(project), encoding="utf-8")
    return tmp_path / f"{stem}.kicad_pcb"


class TestBelowFloorNetclassFires:
    """AC 1 + 2: below-floor netclass fires, with full detail."""

    def test_tscircuit_netclass_fires_on_all_five_axes(self, tmp_path: Path):
        pcb_path = _write_project(tmp_path, [TSCIRCUIT_NETCLASS])

        results = check_netclass_floor(pcb_path, MockDesignRules(), copper_layers=2)

        assert {v.rule_id for v in results.violations} == set(NETCLASS_FLOOR_RULE_IDS)
        # All five are fab-blocking on a 2-layer board.
        assert results.error_count == 5
        assert results.warning_count == 0

    def test_finding_names_class_field_actual_and_required(self, tmp_path: Path):
        pcb_path = _write_project(tmp_path, [TSCIRCUIT_NETCLASS])

        results = check_netclass_floor(pcb_path, MockDesignRules(), copper_layers=2)
        by_rule = {v.rule_id: v for v in results.violations}

        ring = by_rule[RULE_ANNULAR_RING]
        # (0.30 - 0.20) / 2 == 0.05 against the 0.15 floor.
        assert ring.actual_value == pytest.approx(0.05)
        assert ring.required_value == pytest.approx(0.15)
        assert "Default" in ring.message
        assert "annular ring" in ring.message
        assert "netclass:Default" in ring.items

        width = by_rule[RULE_TRACK_WIDTH]
        assert width.actual_value == pytest.approx(0.1)
        assert width.required_value == pytest.approx(0.127)
        assert "track_width" in width.message

        clearance = by_rule[RULE_CLEARANCE]
        assert clearance.actual_value == pytest.approx(0.1)
        assert clearance.required_value == pytest.approx(0.127)

        assert by_rule[RULE_VIA_DIAMETER].actual_value == pytest.approx(0.3)
        assert by_rule[RULE_VIA_DIAMETER].required_value == pytest.approx(0.6)
        assert by_rule[RULE_VIA_DRILL].actual_value == pytest.approx(0.2)
        assert by_rule[RULE_VIA_DRILL].required_value == pytest.approx(0.3)

    def test_fires_with_no_copper_on_the_board(self, tmp_path: Path):
        """The whole point: the rule never looks at segments or vias.

        It is handed only a path, so an unrouted board (or one whose
        ``.kicad_pcb`` does not even exist yet) still reports the floor
        violation.
        """
        pcb_path = _write_project(tmp_path, [TSCIRCUIT_NETCLASS])
        assert not pcb_path.exists()

        results = check_netclass_floor(pcb_path, MockDesignRules(), copper_layers=2)

        assert results.error_count == 5

    def test_each_offending_class_reported_separately(self, tmp_path: Path):
        pcb_path = _write_project(
            tmp_path,
            [CONFORMING_NETCLASS, {**TSCIRCUIT_NETCLASS, "name": "HS"}],
        )

        results = check_netclass_floor(pcb_path, MockDesignRules(), copper_layers=2)

        assert results.error_count == 5
        assert all("netclass:HS" in v.items for v in results.violations)

    def test_pattern_assigned_class_is_checked_and_patterns_listed(self, tmp_path: Path):
        """A below-floor class reached only via ``netclass_patterns``."""
        pcb_path = _write_project(
            tmp_path,
            [CONFORMING_NETCLASS, {**TSCIRCUIT_NETCLASS, "name": "SIG"}],
            patterns=[
                {"netclass": "SIG", "pattern": "/USB_*"},
                {"netclass": "Default", "pattern": "GND"},
            ],
        )

        results = check_netclass_floor(pcb_path, MockDesignRules(), copper_layers=2)

        assert results.error_count == 5
        for violation in results.violations:
            assert violation.items[0] == "netclass:SIG"
            assert "/USB_*" in violation.items
            assert "GND" not in violation.items


class TestConformingNetclassIsSilent:
    """AC 3: a conforming netclass produces no finding."""

    def test_conforming_netclass_no_findings(self, tmp_path: Path):
        pcb_path = _write_project(tmp_path, [CONFORMING_NETCLASS])

        results = check_netclass_floor(pcb_path, MockDesignRules(), copper_layers=2)

        assert list(results.violations) == []
        # The rule still reports that it ran -- a vacuous pass is
        # distinguishable from "the rule never engaged".
        assert results.rules_checked_by_rule == dict.fromkeys(NETCLASS_FLOOR_RULE_IDS, 1)

    def test_exactly_at_the_floor_is_conforming(self, tmp_path: Path):
        """Equality passes (DRC_TOLERANCE guards float representation)."""
        pcb_path = _write_project(
            tmp_path,
            [
                {
                    "name": "Default",
                    "track_width": 0.127,
                    "clearance": 0.127,
                    "via_diameter": 0.6,
                    "via_drill": 0.3,
                }
            ],
        )

        results = check_netclass_floor(pcb_path, MockDesignRules(), copper_layers=2)

        assert list(results.violations) == []


class TestViaSeverityBranch:
    """The issue's open question: when is a via-hostile netclass blocking?"""

    VIA_RULES = {RULE_VIA_DIAMETER, RULE_VIA_DRILL, RULE_ANNULAR_RING}

    def test_single_layer_board_downgrades_via_findings_to_warnings(self, tmp_path: Path):
        pcb_path = _write_project(tmp_path, [TSCIRCUIT_NETCLASS])

        results = check_netclass_floor(pcb_path, MockDesignRules(), copper_layers=1)

        # Copper on one layer is still copper -> trace/space stay blocking.
        assert {v.rule_id for v in results.errors} == {RULE_TRACK_WIDTH, RULE_CLEARANCE}
        # A 1-layer board cannot carry a via -> advisory only.
        assert {v.rule_id for v in results.warnings} == self.VIA_RULES
        assert all("1-layer" in v.message for v in results.warnings)

    @pytest.mark.parametrize("copper_layers", [2, 4, 6])
    def test_multi_layer_board_keeps_via_findings_blocking(
        self, tmp_path: Path, copper_layers: int
    ):
        pcb_path = _write_project(tmp_path, [TSCIRCUIT_NETCLASS])

        results = check_netclass_floor(pcb_path, MockDesignRules(), copper_layers=copper_layers)

        assert results.warning_count == 0
        assert results.error_count == 5

    def test_relaxed_profile_floors_clear_the_same_netclass(self, tmp_path: Path):
        """A board whose reviewed process permits the declared via passes.

        The floors are read off the SAME ``DesignRules`` instance the
        ``dimension_*`` rules use -- which ``check_cmd`` has already run the
        board's fabrication overrides through -- so a legitimately relaxed
        process (board 04's reviewed paid 0.15mm drill option) reports
        nothing instead of a false positive.
        """
        reviewed = MockDesignRules(
            min_via_drill_mm=0.15,
            min_via_diameter_mm=0.30,
            min_annular_ring_mm=0.075,
        )
        pcb_path = _write_project(
            tmp_path,
            [
                {
                    "name": "Default",
                    "track_width": 0.127,
                    "clearance": 0.127,
                    "via_diameter": 0.3,
                    "via_drill": 0.15,
                }
            ],
        )

        assert list(check_netclass_floor(pcb_path, reviewed, copper_layers=2).violations) == []
        # The stock floors still reject it.
        assert check_netclass_floor(pcb_path, MockDesignRules(), 2).error_count == 3


class TestProfileFloorsAreTheProfilesOwn:
    """AC 4: non-JLCPCB profiles resolve their own floors."""

    @pytest.mark.parametrize("mfr", ["jlcpcb", "oshpark", "pcbway", "seeed"])
    def test_real_profiles_flag_the_tscircuit_netclass(self, tmp_path: Path, mfr: str):
        from kicad_tools.manufacturers import get_profile

        design_rules = get_profile(mfr).get_design_rules(layers=2)
        pcb_path = _write_project(tmp_path, [TSCIRCUIT_NETCLASS], stem=f"b_{mfr}")

        results = check_netclass_floor(pcb_path, design_rules, copper_layers=2)

        assert results.error_count > 0
        # Every required_value comes from THIS profile, not a hardcoded table.
        by_rule = {v.rule_id: v for v in results.violations}
        assert by_rule[RULE_TRACK_WIDTH].required_value == pytest.approx(
            design_rules.min_trace_width_mm
        )
        assert by_rule[RULE_ANNULAR_RING].required_value == pytest.approx(
            design_rules.min_annular_ring_mm
        )

    def test_a_looser_profile_accepts_what_a_stricter_one_rejects(self, tmp_path: Path):
        """The floors are per-profile data, not a constant."""
        strict = MockDesignRules(min_trace_width_mm=0.2)
        loose = MockDesignRules(min_trace_width_mm=0.05)
        netclass = {**CONFORMING_NETCLASS, "track_width": 0.1}
        pcb_path = _write_project(tmp_path, [netclass])

        assert {v.rule_id for v in check_netclass_floor(pcb_path, strict, 2).violations} == {
            RULE_TRACK_WIDTH
        }
        assert list(check_netclass_floor(pcb_path, loose, 2).violations) == []


class TestCarveOuts:
    """Every degenerate input is a silent pass, never a crash."""

    def test_no_pcb_path_is_a_noop(self):
        results = check_netclass_floor(None, MockDesignRules(), copper_layers=2)
        assert list(results.violations) == []

    def test_missing_project_file(self, tmp_path: Path):
        results = check_netclass_floor(
            tmp_path / "absent.kicad_pcb", MockDesignRules(), copper_layers=2
        )
        assert list(results.violations) == []

    def test_project_without_net_settings_does_not_synthesize_a_default(self, tmp_path: Path):
        """A project with no netclass settings must report nothing.

        ``core.project_file.get_netclass_definitions`` *materializes* a stock
        ``Default`` class when none exists; reporting against that would
        flag a netclass the project never declared.
        """
        pcb_path = _write_project(tmp_path, None)

        results = check_netclass_floor(pcb_path, MockDesignRules(), copper_layers=2)

        assert list(results.violations) == []

    def test_empty_classes_list(self, tmp_path: Path):
        pcb_path = _write_project(tmp_path, [])
        results = check_netclass_floor(pcb_path, MockDesignRules(), copper_layers=2)
        assert list(results.violations) == []

    def test_malformed_project_json(self, tmp_path: Path):
        (tmp_path / "board.kicad_pro").write_text("{not json", encoding="utf-8")
        results = check_netclass_floor(
            tmp_path / "board.kicad_pcb", MockDesignRules(), copper_layers=2
        )
        assert list(results.violations) == []

    def test_absent_and_non_numeric_fields_are_skipped(self, tmp_path: Path):
        pcb_path = _write_project(
            tmp_path,
            [{"name": "Sparse", "track_width": None, "clearance": "0.1"}],
        )

        results = check_netclass_floor(pcb_path, MockDesignRules(), copper_layers=2)

        # ``track_width: null`` and the missing via fields are uncheckable;
        # the string clearance is coerced and still flagged.
        assert {v.rule_id for v in results.violations} == {RULE_CLEARANCE}

    def test_unnamed_class_is_skipped(self, tmp_path: Path):
        pcb_path = _write_project(tmp_path, [{"track_width": 0.01}])
        results = check_netclass_floor(pcb_path, MockDesignRules(), copper_layers=2)
        assert list(results.violations) == []


class TestDeterminism:
    """Findings are ordered, so CI diffs are byte-identical run to run."""

    def test_findings_sorted_by_class_then_rule(self, tmp_path: Path):
        pcb_path = _write_project(
            tmp_path,
            [
                {**TSCIRCUIT_NETCLASS, "name": "Zebra"},
                {**TSCIRCUIT_NETCLASS, "name": "Alpha"},
            ],
        )

        results = check_netclass_floor(pcb_path, MockDesignRules(), copper_layers=2)
        keys = [(v.items[0], v.rule_id) for v in results.violations]

        assert keys == sorted(keys)
        assert keys[0][0] == "netclass:Alpha"


class TestWiring:
    """Category/rule-id registration the report layers key off of."""

    def test_rule_ids_are_categorized_as_manufacturing(self):
        from kicad_tools.validate.checker import DRCChecker

        for rule_id in NETCLASS_FLOOR_RULE_IDS:
            assert DRCChecker.RULE_CATEGORY[rule_id] == DRCChecker.CATEGORY_MANUFACTURING
            assert DRCChecker.category_for_rule(rule_id) == DRCChecker.CATEGORY_MANUFACTURING

    @pytest.mark.parametrize("rule_id", NETCLASS_FLOOR_RULE_IDS)
    def test_rule_ids_never_resolve_to_unknown(self, rule_id: str):
        from kicad_tools.drc.violation import ViolationType

        assert ViolationType.from_string(rule_id) != ViolationType.UNKNOWN

    def test_category_is_registered_in_the_cli(self):
        from kicad_tools.cli.check_cmd import CHECK_CATEGORIES

        assert "netclass_floor" in CHECK_CATEGORIES

    def test_to_dict_round_trips(self, tmp_path: Path):
        pcb_path = _write_project(tmp_path, [TSCIRCUIT_NETCLASS])

        results = check_netclass_floor(pcb_path, MockDesignRules(), copper_layers=2)
        payload = [v.to_dict() for v in results.violations]

        assert len(payload) == 5
        for entry in payload:
            assert entry["type"] != "unknown"
            assert entry["severity"] == "error"
            assert entry["required_value"] is not None
