"""Tests for the general ``.kct_waivers.json`` waiver mechanism (Issue #4417).

Covers the v2 loader, exact-set/order-insensitive matching (items + optional
nets), the central :func:`apply_waivers` post-check step, and the
``waiver_unused`` advisory for stale entries.
"""

from __future__ import annotations

import pytest

from kicad_tools.validate.rules.waivers import (
    WAIVER_UNUSED_RULE_ID,
    Waivers,
    apply_waivers,
    discover_waivers_sidecar,
    load_waivers,
    waivers_from_dict,
)
from kicad_tools.validate.violations import DRCResults, DRCViolation


def _v(rule_id="clearance_pad_pad", items=(), nets=(), severity="error"):
    return DRCViolation(
        rule_id=rule_id,
        severity=severity,
        message="x",
        items=tuple(items),
        nets=tuple(nets),
    )


class TestLoader:
    def test_v2_happy_path(self):
        w = waivers_from_dict(
            {
                "version": 2,
                "waivers": [
                    {
                        "rule": "courtyards_overlap",
                        "items": ["C52", "U10"],
                        "reason": "EE-mandated tight decoupling",
                        "issue": "chorus#18",
                    }
                ],
            }
        )
        assert len(w) == 1
        entry = w.entries[0]
        assert entry.rule == "courtyards_overlap"
        assert entry.items == frozenset({"C52", "U10"})
        assert entry.nets == frozenset()
        assert entry.reason == "EE-mandated tight decoupling"
        assert entry.issue == "chorus#18"

    def test_nets_only_entry(self):
        w = waivers_from_dict(
            {
                "version": 2,
                "waivers": [
                    {
                        "rule": "clearance_pad_pad",
                        "nets": ["GND", "VBUS"],
                        "reason": "documented star-ground tie",
                        "issue": "x#1",
                    }
                ],
            }
        )
        assert w.entries[0].items == frozenset()
        assert w.entries[0].nets == frozenset({"GND", "VBUS"})

    def test_missing_version_rejected(self):
        with pytest.raises(ValueError, match="missing the required 'version'"):
            waivers_from_dict({"waivers": []})

    def test_unsupported_version_rejected(self):
        with pytest.raises(ValueError, match="unsupported waivers version"):
            waivers_from_dict({"version": 1, "waivers": []})

    def test_non_object_rejected(self):
        with pytest.raises(ValueError, match="must be a JSON object"):
            waivers_from_dict([1, 2, 3])

    def test_missing_reason_rejected(self):
        with pytest.raises(ValueError, match="non-empty 'reason'"):
            waivers_from_dict(
                {
                    "version": 2,
                    "waivers": [{"rule": "r", "items": ["A", "B"], "issue": "x#1"}],
                }
            )

    def test_missing_issue_rejected(self):
        with pytest.raises(ValueError, match="non-empty 'issue'"):
            waivers_from_dict(
                {
                    "version": 2,
                    "waivers": [{"rule": "r", "items": ["A", "B"], "reason": "ok"}],
                }
            )

    def test_empty_rule_rejected(self):
        with pytest.raises(ValueError, match="non-empty string 'rule'"):
            waivers_from_dict(
                {
                    "version": 2,
                    "waivers": [{"rule": "", "items": ["A"], "reason": "r", "issue": "i"}],
                }
            )

    def test_no_items_or_nets_rejected(self):
        with pytest.raises(ValueError, match="at least one 'items' or 'nets'"):
            waivers_from_dict(
                {
                    "version": 2,
                    "waivers": [{"rule": "r", "reason": "r", "issue": "i"}],
                }
            )

    def test_items_non_string_rejected(self):
        with pytest.raises(ValueError, match="'items' entries must be non-empty"):
            waivers_from_dict(
                {
                    "version": 2,
                    "waivers": [{"rule": "r", "items": ["A", 3], "reason": "r", "issue": "i"}],
                }
            )


class TestLoadFromFile:
    def test_load_and_discover(self, tmp_path):
        pcb_path = tmp_path / "board.kicad_pcb"
        pcb_path.write_text("(kicad_pcb)")
        sidecar = tmp_path / ".kct_waivers.json"
        sidecar.write_text(
            '{"version": 2, "waivers": [{"rule": "r", "items": ["A", "B"], '
            '"reason": "ok", "issue": "x#1"}]}'
        )
        found = discover_waivers_sidecar(pcb_path)
        assert found == sidecar
        w = load_waivers(found)
        assert len(w) == 1

    def test_discover_returns_none_when_absent(self, tmp_path):
        pcb_path = tmp_path / "board.kicad_pcb"
        pcb_path.write_text("(kicad_pcb)")
        assert discover_waivers_sidecar(pcb_path) is None

    def test_load_malformed_json_raises(self, tmp_path):
        bad = tmp_path / ".kct_waivers.json"
        bad.write_text("{not json")
        with pytest.raises(ValueError, match="parsing waivers JSON"):
            load_waivers(bad)


class TestMatching:
    def test_items_order_insensitive(self):
        w = waivers_from_dict(
            {
                "version": 2,
                "waivers": [{"rule": "r", "items": ["A", "B"], "reason": "ok", "issue": "x#1"}],
            }
        )
        assert w.match(_v(rule_id="r", items=("B", "A"))) is not None
        assert w.match(_v(rule_id="r", items=("A", "B"))) is not None

    def test_exact_set_rejects_subset(self):
        w = waivers_from_dict(
            {
                "version": 2,
                "waivers": [{"rule": "r", "items": ["A", "B"], "reason": "ok", "issue": "x#1"}],
            }
        )
        # A 3-item finding must NOT be waived by a 2-item entry.
        assert w.match(_v(rule_id="r", items=("A", "B", "C"))) is None

    def test_rule_id_must_match(self):
        w = waivers_from_dict(
            {
                "version": 2,
                "waivers": [{"rule": "r", "items": ["A", "B"], "reason": "ok", "issue": "x#1"}],
            }
        )
        assert w.match(_v(rule_id="other", items=("A", "B"))) is None

    def test_nets_match(self):
        w = waivers_from_dict(
            {
                "version": 2,
                "waivers": [
                    {
                        "rule": "clearance_pad_pad",
                        "nets": ["GND", "VBUS"],
                        "reason": "ok",
                        "issue": "x#1",
                    }
                ],
            }
        )
        assert w.match(_v(nets=("VBUS", "GND"))) is not None
        assert w.match(_v(nets=("GND",))) is None

    def test_items_and_nets_both_constrained(self):
        w = waivers_from_dict(
            {
                "version": 2,
                "waivers": [
                    {
                        "rule": "r",
                        "items": ["A", "B"],
                        "nets": ["N1"],
                        "reason": "ok",
                        "issue": "x#1",
                    }
                ],
            }
        )
        assert w.match(_v(rule_id="r", items=("A", "B"), nets=("N1",))) is not None
        # items match but nets do not
        assert w.match(_v(rule_id="r", items=("A", "B"), nets=("N2",))) is None


class TestApplyWaivers:
    def _waivers(self, *entries):
        return waivers_from_dict({"version": 2, "waivers": list(entries)})

    def test_marks_only_matching_waived(self):
        results = DRCResults(
            violations=[
                _v(rule_id="r", items=("A", "B")),
                _v(rule_id="r", items=("C", "D")),
            ]
        )
        waivers = self._waivers({"rule": "r", "items": ["A", "B"], "reason": "ok", "issue": "x#1"})
        apply_waivers(results, waivers)
        # A/B waived, C/D untouched, no unused advisory.
        assert results.waived_count == 1
        assert results.error_count == 1
        waived = results.waived[0]
        assert set(waived.items) == {"A", "B"}
        assert waived.waiver_reason == "ok"
        assert waived.waiver_issue == "x#1"
        assert waived.severity == "error"  # underlying severity preserved

    def test_unused_waiver_emits_info(self):
        results = DRCResults(violations=[_v(rule_id="r", items=("A", "B"))])
        waivers = self._waivers({"rule": "r", "items": ["X", "Y"], "reason": "ok", "issue": "x#9"})
        apply_waivers(results, waivers)
        assert results.waived_count == 0
        assert results.error_count == 1
        unused = [v for v in results.violations if v.rule_id == WAIVER_UNUSED_RULE_ID]
        assert len(unused) == 1
        assert unused[0].severity == "info"
        assert "x#9" in unused[0].message

    def test_already_waived_untouched(self):
        pre_waived = DRCViolation(
            rule_id="courtyards_overlap",
            severity="error",
            message="waived by per-rule path",
            items=("A", "B"),
            waived=True,
            waiver_reason="courtyard",
            waiver_issue="c#1",
        )
        results = DRCResults(violations=[pre_waived])
        # A general waiver targeting the same pair must NOT re-waive / clobber.
        waivers = self._waivers(
            {
                "rule": "courtyards_overlap",
                "items": ["A", "B"],
                "reason": "general",
                "issue": "g#1",
            }
        )
        apply_waivers(results, waivers)
        assert results.waived_count == 1
        # Original reason preserved; general entry counts as unused.
        assert results.waived[0].waiver_reason == "courtyard"
        unused = [v for v in results.violations if v.rule_id == WAIVER_UNUSED_RULE_ID]
        assert len(unused) == 1

    def test_empty_waivers_noop(self):
        results = DRCResults(violations=[_v(rule_id="r", items=("A", "B"))])
        apply_waivers(results, Waivers())
        assert results.error_count == 1
        assert results.waived_count == 0
        assert all(v.rule_id != WAIVER_UNUSED_RULE_ID for v in results.violations)

    def test_one_entry_waives_multiple_findings(self):
        # Same ref-set on front and back -> two findings, one entry waives both.
        results = DRCResults(
            violations=[
                _v(rule_id="courtyards_overlap", items=("A", "B")),
                _v(rule_id="courtyards_overlap", items=("A", "B")),
            ]
        )
        waivers = self._waivers(
            {
                "rule": "courtyards_overlap",
                "items": ["A", "B"],
                "reason": "ok",
                "issue": "x#1",
            }
        )
        apply_waivers(results, waivers)
        assert results.waived_count == 2
        assert all(v.rule_id != WAIVER_UNUSED_RULE_ID for v in results.violations)
