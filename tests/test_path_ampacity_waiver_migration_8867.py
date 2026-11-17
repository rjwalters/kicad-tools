"""Unused empty-layer ``path_ampacity`` waivers name the per-layer migration (#8867)."""

from __future__ import annotations

from kicad_tools.validate import DRCResults, DRCViolation
from kicad_tools.validate.rules.waivers import (
    PATH_AMPACITY_PER_LAYER_CAUSE,
    WAIVER_UNUSED_RULE_ID,
    apply_waivers,
    waivers_from_dict,
)


def _amp(layer: str, nets=("VBUS",), items=("Path1",)) -> DRCViolation:
    return DRCViolation(
        rule_id="path_ampacity",
        severity="error",
        message="too much current",
        layer=layer,
        items=items,
        nets=nets,
    )


def _waivers(key: str):
    return waivers_from_dict(
        {
            "version": 3,
            "waivers": [
                {
                    "key": key,
                    "evidence_hash": "ev3:abc",
                    "reason": "reviewed",
                    "reviewer": "ee",
                    "date": "2026-10-06",
                }
            ],
        }
    )


def _run(key: str, findings):
    results = DRCResults()
    for f in findings:
        results.add(f)
    apply_waivers(results, _waivers(key))
    return results


def _unused(results):
    return [v for v in results.violations if v.rule_id == WAIVER_UNUSED_RULE_ID]


def test_old_key_gets_per_layer_cause_and_findings_stay_errors():
    results = _run(
        "path_ampacity|Path1|VBUS|",
        [_amp("F.Cu"), _amp("B.Cu")],
    )
    (adv,) = _unused(results)
    assert adv.to_dict()["stale_waiver_cause"] == PATH_AMPACITY_PER_LAYER_CAUSE
    assert "#8797" in adv.message
    assert "path_ampacity|Path1|VBUS|F.Cu" in adv.message
    assert "path_ampacity|Path1|VBUS|B.Cu" in adv.message
    assert "geometry" not in adv.message
    amp = [v for v in results.violations if v.rule_id == "path_ampacity"]
    assert len(amp) == 2 and all(v.is_error for v in amp)


def test_hint_key_list_is_capped():
    findings = [_amp(f"L{i}") for i in range(8)]
    (adv,) = _unused(_run("path_ampacity|Path1|VBUS|", findings))
    assert "(+3 more)" in adv.message


def test_unrelated_waiver_gets_no_cause():
    (adv,) = _unused(_run("path_ampacity|Other|GND|", [_amp("F.Cu")]))
    assert "stale_waiver_cause" not in adv.to_dict()
    assert "#8797" not in adv.message


def test_waiver_with_layer_is_unchanged():
    (adv,) = _unused(_run("path_ampacity|Path1|VBUS|In1.Cu", [_amp("F.Cu")]))
    assert "stale_waiver_cause" not in adv.to_dict()
