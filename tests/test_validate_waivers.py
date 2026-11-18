"""Tests for the general ``.kct_waivers.json`` waiver mechanism (Issue #4417).

Covers the v2 loader, exact-set/order-insensitive matching (items + optional
nets), the central :func:`apply_waivers` post-check step, and the
``waiver_unused`` advisory for stale entries.
"""

from __future__ import annotations

import pytest

from kicad_tools.validate.rules.waivers import (
    MAX_INVALIDATED_LISTED,
    WAIVER_UNUSED_RULE_ID,
    InvalidatedWaiver,
    Waivers,
    apply_waivers,
    discover_waivers_sidecar,
    format_invalidated_waivers,
    invalidated_waiver_keys,
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


# ── Issue #9331: invalidated-waiver report for pcb prune / fix-drc ─────────


def _kv(
    rule_id="clearance_segment_segment",
    items=("Trace@F.Cu:w0.25:100/100~110/100",),
    nets=("GND",),
    layer="F.Cu",
    severity="error",
    waived=False,
    evidence_hash=None,
):
    from dataclasses import replace

    v = _v(rule_id=rule_id, items=items, nets=nets, severity=severity)
    v = replace(v, layer=layer, waived=waived)
    if evidence_hash:
        v = replace(v, evidence_hash=evidence_hash)
    return v


def _keyed_waivers(key: str, evidence_hash: str) -> Waivers:
    return waivers_from_dict(
        {
            "version": 3,
            "waivers": [
                {
                    "key": key,
                    "evidence_hash": evidence_hash,
                    "reason": "reviewed overlap",
                    "reviewer": "rj",
                    "date": "2026-11-18",
                }
            ],
        }
    )


class TestInvalidatedWaiverKeys:
    KEY_A = (
        "clearance_segment_segment|Trace@F.Cu:w0.25:100/100.15~110/100.15,"
        "Trace@F.Cu:w0.25:100/100~102/100|+3.3V,GND|F.Cu"
    )
    KEY_A_MOVED = (
        "clearance_segment_segment|Trace@F.Cu:w0.25:100/100.15~110/100.15,"
        "Trace@F.Cu:w0.25:100/100~102.5/100|+3.3V,GND|F.Cu"
    )

    def _entry_for(self, key: str, finding) -> Waivers:
        from kicad_tools.validate.evidence import compute_evidence_hash

        return _keyed_waivers(key, compute_evidence_hash(finding))

    def _finding_for(self, key: str):
        """A finding whose ``key`` equals ``key`` (rule|items|nets|layer)."""
        rule, items, nets, layer = key.split("|")
        return _kv(
            rule_id=rule,
            items=tuple(items.split(",")) if items else (),
            nets=tuple(nets.split(",")) if nets else (),
            layer=layer or None,
        )

    def test_key_gone_is_invalidated_without_candidate(self):
        before = [self._finding_for(self.KEY_A)]
        w = self._entry_for(self.KEY_A, before[0])
        out = invalidated_waiver_keys(before, [], w)
        assert len(out) == 1
        assert out[0].key == self.KEY_A
        assert out[0].rule == "clearance_segment_segment"
        assert out[0].rewaive_candidate is None  # the finding was fixed

    def test_key_persists_not_reported(self):
        before = [self._finding_for(self.KEY_A)]
        w = self._entry_for(self.KEY_A, before[0])
        assert invalidated_waiver_keys(before, [self._finding_for(self.KEY_A)], w) == []

    def test_previously_unused_not_reported(self):
        w = self._entry_for(self.KEY_A, self._finding_for(self.KEY_A))
        # The entry never matched anything before the edit either.
        assert invalidated_waiver_keys([], [], w) == []

    def test_unique_rewaive_candidate_named(self):
        before = [self._finding_for(self.KEY_A)]
        w = self._entry_for(self.KEY_A, before[0])
        after = [self._finding_for(self.KEY_A_MOVED)]
        out = invalidated_waiver_keys(before, after, w)
        assert len(out) == 1
        assert out[0].rewaive_candidate == self.KEY_A_MOVED

    def test_ambiguous_candidates_not_named(self):
        before = [self._finding_for(self.KEY_A)]
        w = self._entry_for(self.KEY_A, before[0])
        # Two after-state findings share rule/nets/layer/item kinds.
        after = [
            self._finding_for(self.KEY_A_MOVED),
            self._finding_for(
                "clearance_segment_segment|Trace@F.Cu:w0.25:105/100~110/100,"
                "Trace@F.Cu:w0.25:100/100.15~110/100.15|+3.3V,GND|F.Cu"
            ),
        ]
        out = invalidated_waiver_keys(before, after, w)
        assert len(out) == 1
        assert out[0].rewaive_candidate is None

    def test_already_waived_before_finding_not_counted(self):
        before = [self._finding_for(self.KEY_A)]
        waived_before = [_kv_replace_waived(before[0])]
        w = self._entry_for(self.KEY_A, before[0])
        assert invalidated_waiver_keys(waived_before, [], w) == []

    def test_stale_evidence_not_used_before(self):
        from dataclasses import replace

        finding = replace(self._finding_for(self.KEY_A), evidence_hash="ev3:other")
        w = self._entry_for(self.KEY_A, self._finding_for(self.KEY_A))
        # The entry names the key but its evidence no longer matches: it did
        # not waive anything before, so the edit is not what orphaned it.
        assert invalidated_waiver_keys([finding], [], w) == []

    def test_finding_persists_same_key_stale_evidence_names_candidate(self):
        from dataclasses import replace

        before = [self._finding_for(self.KEY_A)]
        w = self._entry_for(self.KEY_A, before[0])
        # Same key after the edit, different evidence (e.g. a moved neighbour
        # changed the measured value): the entry is stale -> invalidated, and
        # the surviving finding is the re-waive candidate.
        after = [replace(self._finding_for(self.KEY_A), evidence_hash="ev3:new")]
        out = invalidated_waiver_keys(before, after, w)
        assert len(out) == 1
        assert out[0].rewaive_candidate == self.KEY_A

    def test_legacy_entry_still_matching_not_reported(self):
        w = waivers_from_dict(
            {
                "version": 2,
                "waivers": [
                    {
                        "rule": "courtyards_overlap",
                        "items": ["C52", "U10"],
                        "reason": "ok",
                        "issue": "x#1",
                    }
                ],
            }
        )
        before = [_v(rule_id="courtyards_overlap", items=("C52", "U10"))]
        after = [_v(rule_id="courtyards_overlap", items=("C52", "U10"))]
        assert invalidated_waiver_keys(before, after, w) == []

    def test_legacy_entry_items_changed_is_invalidated(self):
        w = waivers_from_dict(
            {
                "version": 2,
                "waivers": [
                    {
                        "rule": "courtyards_overlap",
                        "items": ["C52", "U10"],
                        "reason": "ok",
                        "issue": "x#1",
                    }
                ],
            }
        )
        before = [_v(rule_id="courtyards_overlap", items=("C52", "U10"))]
        after = [_v(rule_id="courtyards_overlap", items=("C52", "U11"))]
        out = invalidated_waiver_keys(before, after, w)
        assert len(out) == 1
        assert out[0].key is None
        assert out[0].rule == "courtyards_overlap"
        assert out[0].rewaive_candidate is None  # refs changed, kinds differ


def _kv_replace_waived(v):
    from dataclasses import replace

    return replace(v, waived=True, waiver_reason="courtyard")


class TestFormatInvalidatedWaivers:
    def test_empty_is_silent(self):
        assert format_invalidated_waivers([]) == []

    def test_lists_key_rule_and_candidate(self):
        items = [
            InvalidatedWaiver(
                key="r|a|n|F.Cu",
                rule="r",
                scope="key='r|a|n|F.Cu'",
                rewaive_candidate="r|a'|n|F.Cu",
            )
        ]
        lines = format_invalidated_waivers(items)
        assert len(lines) == 2
        assert "1 waiver key(s) invalidated" in lines[0]
        assert "r|a|n|F.Cu" in lines[1] and "(rule r)" in lines[1]
        assert "re-waive candidate: r|a'|n|F.Cu" in lines[1]

    def test_legacy_entry_uses_scope(self):
        items = [InvalidatedWaiver(key=None, rule="r", scope="items=['A', 'B']")]
        lines = format_invalidated_waivers(items)
        assert "items=['A', 'B']" in lines[1]

    def test_caps_with_more_tail(self):
        items = [InvalidatedWaiver(key=f"r|a{i}|n|F.Cu", rule="r", scope=f"s{i}") for i in range(7)]
        lines = format_invalidated_waivers(items)
        assert "7 waiver key(s) invalidated" in lines[0]
        assert sum(1 for ln in lines if ln.startswith("  - ")) == MAX_INVALIDATED_LISTED
        assert lines[-1] == f"  (+{7 - MAX_INVALIDATED_LISTED} more)"
