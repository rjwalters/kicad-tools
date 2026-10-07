"""detect-mistakes evidence-bound waivers and SARIF output (issue #6006)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import jsonschema
import pytest

from kicad_tools.cli import check_cmd, mistakes_cmd
from kicad_tools.explain.mistake_waivers import (
    annotate_mistakes,
    apply_mistake_waivers,
    split_components,
)
from kicad_tools.explain.mistakes import Mistake, MistakeCategory, mistake_rule_id
from kicad_tools.validate.rules.waivers import (
    WAIVER_STALE_RULE_ID,
    WAIVER_UNUSED_RULE_ID,
    is_mistake_rule,
    waivers_from_dict,
)
from kicad_tools.validate.sarif import (
    KEY_FINGERPRINT,
    TextAnchors,
    diff_sarif_log,
    sarif_level,
    sarif_log,
)

FIXTURES = Path(__file__).parent / "fixtures"
PROJECTS = FIXTURES / "projects"
SARIF_SCHEMA = json.loads((FIXTURES / "schemas" / "sarif-schema-2.1.0.json").read_text())

BYPASS_C1 = "mistake.bypass_cap_distance|C1,U1||"


def _validate_sarif(log: dict) -> None:
    """Validate against the vendored OASIS SARIF 2.1.0 schema (no network)."""
    jsonschema.Draft4Validator(SARIF_SCHEMA).validate(log)
    assert log["version"] == "2.1.0"


@pytest.fixture
def board(tmp_path) -> Path:
    """A scratch copy of a board with bypass-cap and power-trace mistakes."""
    dest = tmp_path / "proj"
    shutil.copytree(PROJECTS, dest)
    return dest / "multilayer_zones.kicad_pcb"


@pytest.fixture
def check_board(tmp_path) -> Path:
    dest = tmp_path / "chk"
    shutil.copytree(PROJECTS, dest)
    return dest / "test_project.kicad_pcb"


def _move(pcb: Path, ref_uuid: str, old: str, new: str) -> None:
    text = pcb.read_text()
    marker = f'(uuid "{ref_uuid}")\n\t\t(at {old})'
    assert marker in text
    pcb.write_text(text.replace(marker, f'(uuid "{ref_uuid}")\n\t\t(at {new})'))


def _mistakes_json(capsys, *argv: str) -> tuple[int, dict]:
    code = mistakes_cmd.main([*argv, "--format", "json"])
    return code, json.loads(capsys.readouterr().out)


def _mistakes_sarif(capsys, *argv: str) -> tuple[int, dict]:
    code = mistakes_cmd.main([*argv, "--format", "sarif"])
    return code, json.loads(capsys.readouterr().out)


def _m(**kw) -> Mistake:
    base = {
        "category": MistakeCategory.BYPASS_CAP,
        "severity": "warning",
        "title": "Bypass capacitor too far from power pin",
        "components": ["C1", "U1"],
        "explanation": "C1 is 13.3mm from U1 pin 2 (+3V3).",
        "fix_suggestion": "Move C1.",
        "location": (15.0, 20.0),
        "rule_id": "mistake.bypass_cap_distance",
    }
    base.update(kw)
    return Mistake(**base)


# ---------------------------------------------------------------------------
# Keys and evidence
# ---------------------------------------------------------------------------


class TestMistakeIdentity:
    def test_rule_id_from_check_class(self):
        class BypassCapDistanceCheck:
            pass

        class ViaInPadCheck:
            pass

        class Custom:
            rule_id = "my_rule"

        assert mistake_rule_id(BypassCapDistanceCheck()) == "mistake.bypass_cap_distance"
        assert mistake_rule_id(ViaInPadCheck()) == "mistake.via_in_pad"
        assert mistake_rule_id(Custom()) == "mistake.my_rule"
        assert is_mistake_rule("mistake.via_in_pad")
        assert not is_mistake_rule("clearance_pad_pad")

    def test_components_split_into_items_nets_layer(self):
        items, nets, layer = split_components(["U1", "GND", "F.Cu", "pad 3"], {"GND"})
        assert items == ("U1", "pad 3")
        assert nets == ("GND",)
        assert layer == "F.Cu"

    def test_key_is_stable_and_hash_tracks_measurements(self):
        a, b, c = _m(), _m(), _m(explanation="C1 is 12.9mm from U1 pin 2 (+3V3).")
        annotate_mistakes([a], None)
        annotate_mistakes([b], None)
        annotate_mistakes([c], None)
        assert a.key == b.key == c.key == BYPASS_C1
        assert a.evidence_hash == b.evidence_hash
        assert a.evidence_hash.startswith("ev2:")
        # The reported measurement is evidence: 13.3 mm -> 12.9 mm is new.
        assert c.evidence_hash != a.evidence_hash
        # Wording outside the numbers (title) is not.
        d = _m(title="Bypass cap far away")
        annotate_mistakes([d], None)
        assert d.evidence_hash == a.evidence_hash

    def test_waiver_applies_and_goes_stale(self):
        reviewed = _m()
        adapters = annotate_mistakes([reviewed], None)
        waivers = waivers_from_dict(
            {
                "version": 3,
                "waivers": [
                    {
                        "key": reviewed.key,
                        "evidence_hash": reviewed.evidence_hash,
                        "reason": "r",
                        "reviewer": "ee",
                        "date": "2026-10-06",
                    }
                ],
            }
        )
        outcome = apply_mistake_waivers([reviewed], adapters, waivers.for_mistakes())
        assert reviewed.waived and reviewed.waiver_reason == "r"
        assert not outcome.advisories

        moved = _m(location=(16.0, 20.0))
        adapters = annotate_mistakes([moved], None)
        outcome = apply_mistake_waivers([moved], adapters, waivers.for_mistakes())
        assert not moved.waived
        assert moved.stale_waiver_hash == reviewed.evidence_hash
        assert [a.rule_id for a in outcome.advisories] == [WAIVER_STALE_RULE_ID]
        assert "kct detect-mistakes --waive" in outcome.advisories[0].message

    def test_waivers_are_scoped_per_command(self):
        waivers = waivers_from_dict(
            {
                "version": 3,
                "waivers": [
                    {
                        "key": BYPASS_C1,
                        "evidence_hash": "ev2:0",
                        "reason": "r",
                        "reviewer": "ee",
                        "date": "2026-10-06",
                    },
                    {"rule": "clearance_pad_pad", "items": ["U1"], "reason": "r", "issue": "x#1"},
                ],
            }
        )
        assert [e.rule for e in waivers.for_check().entries] == ["clearance_pad_pad"]
        assert [e.rule for e in waivers.for_mistakes().entries] == ["mistake.bypass_cap_distance"]
        assert not waivers.for_mistakes(rules={"mistake.via_in_pad"}).entries


# ---------------------------------------------------------------------------
# CLI round trip (acceptance criterion 1)
# ---------------------------------------------------------------------------


def test_json_findings_carry_key_and_evidence_hash(board, capsys):
    _, data = _mistakes_json(capsys, str(board))
    assert data["mistakes"]
    for m in data["mistakes"]:
        assert m["key"].startswith(m["rule_id"] + "|")
        assert m["rule_id"].startswith("mistake.")
        assert m["evidence_hash"].startswith("ev2:")
        assert m["waived"] is False
    assert {c["rule_id"] for c in data["coverage"]} >= {"mistake.bypass_cap_distance"}
    assert data["waiver_findings"] == []


def test_waiver_round_trip_goes_stale_when_geometry_changes(board, capsys):
    _, before = _mistakes_json(capsys, str(board))
    assert any(m["key"] == BYPASS_C1 for m in before["mistakes"])

    code, waived = _mistakes_json(
        capsys,
        str(board),
        "--waive",
        BYPASS_C1,
        "--waive-reason",
        "accepted for test",
        "--waive-reviewer",
        "pytest",
    )
    sidecar = board.with_name("multilayer_zones.kct-waivers.json")
    assert sidecar.is_file()
    entry = json.loads(sidecar.read_text())["waivers"][0]
    assert entry["key"] == BYPASS_C1
    target = next(m for m in waived["mistakes"] if m["key"] == BYPASS_C1)
    assert target["waived"] and target["status"] == "waived"
    assert target["evidence_hash"] == entry["evidence_hash"]
    assert waived["summary"]["waived"] == 1
    assert waived["summary"]["warnings"] == before["summary"]["warnings"] - 1

    # Re-running without --waive keeps the finding waived (sidecar discovered).
    _, again = _mistakes_json(capsys, str(board))
    assert next(m for m in again["mistakes"] if m["key"] == BYPASS_C1)["waived"]

    # kct check shares the sidecar but must not report the mistake entry as unused.
    check_cmd.main([str(board), "--format", "json"])
    check = json.loads(capsys.readouterr().out)
    assert not any(v["rule_id"] == WAIVER_UNUSED_RULE_ID for v in check["violations"])

    # Move C1: the finding keeps its key but its evidence changed -> stale.
    _move(board, "fp-c1-uuid", "115 120", "116 120")
    code, after = _mistakes_json(capsys, str(board), "--strict")
    returned = next(m for m in after["mistakes"] if m["key"] == BYPASS_C1)
    assert not returned["waived"]
    assert returned["waiver_status"] == "stale"
    assert returned["stale_waiver_evidence_hash"] == entry["evidence_hash"]
    assert returned["stale_waiver_cause"] == "evidence_changed"
    assert after["summary"]["stale_waivers"] == 1
    assert [w["rule_id"] for w in after["waiver_findings"]] == [WAIVER_STALE_RULE_ID]
    assert code == 2  # the stale warning fails --strict


def test_category_filter_does_not_report_other_waivers_unused(board, capsys):
    mistakes_cmd.main(
        [str(board), "--waive", BYPASS_C1, "--waive-reason", "r", "--waive-reviewer", "me"]
    )
    capsys.readouterr()
    _, data = _mistakes_json(capsys, str(board), "--category", "power_trace")
    assert data["waiver_findings"] == []


def test_waive_unknown_key_is_an_error(board, capsys):
    code = mistakes_cmd.main(
        [str(board), "--waive", "nope|||", "--waive-reason", "r", "--waive-reviewer", "me"]
    )
    assert code == 1
    assert "names no current finding" in capsys.readouterr().err


def test_waive_requires_reason_and_reviewer(board):
    with pytest.raises(SystemExit):
        mistakes_cmd.main([str(board), "--waive", BYPASS_C1])


# ---------------------------------------------------------------------------
# SARIF (acceptance criterion 2)
# ---------------------------------------------------------------------------


def test_sarif_level_mapping():
    assert sarif_level("error") == "error"
    assert sarif_level("warning") == "warning"
    assert sarif_level("info") == "note"


def test_check_sarif_validates_with_stable_fingerprints(check_board, capsys):
    check_cmd.main([str(check_board), "--format", "json"])
    report = json.loads(capsys.readouterr().out)
    check_cmd.main([str(check_board), "--format", "sarif"])
    log = json.loads(capsys.readouterr().out)
    _validate_sarif(log)

    run = log["runs"][0]
    assert run["tool"]["driver"]["name"] == "kct check"
    results = run["results"]
    assert len(results) == len(report["violations"])
    for v, r in zip(report["violations"], results, strict=True):
        assert r["ruleId"] == v["rule_id"]
        assert r["level"] == sarif_level(v["severity"])
        assert r["partialFingerprints"][KEY_FINGERPRINT] == v["key"]
        assert r["fingerprints"] == {"kctEvidence/ev2": v["evidence_hash"]}
        assert run["tool"]["driver"]["rules"][r["ruleIndex"]]["id"] == v["rule_id"]
        if v["location"]:
            board_loc = r["locations"][0]["properties"]["boardLocation"]
            assert [board_loc["x"], board_loc["y"]] == v["location"]
            assert board_loc["units"] == "mm"

    # Same board, same fingerprints.
    check_cmd.main([str(check_board), "--format", "sarif"])
    again = json.loads(capsys.readouterr().out)
    assert [(r["partialFingerprints"], r["fingerprints"]) for r in again["runs"][0]["results"]] == [
        (r["partialFingerprints"], r["fingerprints"]) for r in results
    ]

    # A footprint-naming finding points at the footprint's line in the board.
    d1 = next(r for r in results if "D1" in r["partialFingerprints"][KEY_FINGERPRINT])
    line = d1["locations"][0]["physicalLocation"]["region"]["startLine"]
    assert check_board.read_text().splitlines()[line - 1].lstrip().startswith("(footprint ")


def test_check_sarif_marks_waived_findings_suppressed(check_board, capsys):
    check_cmd.main([str(check_board), "--format", "json"])
    report = json.loads(capsys.readouterr().out)
    key = next(v["key"] for v in report["violations"] if "D1" in ",".join(v["items"]))
    check_cmd.main(
        [
            str(check_board),
            "--waive",
            key,
            "--waive-reason",
            "reviewed",
            "--waive-reviewer",
            "pytest",
            "--format",
            "sarif",
        ]
    )
    log = json.loads(capsys.readouterr().out)
    _validate_sarif(log)
    hits = [
        r for r in log["runs"][0]["results"] if r["partialFingerprints"][KEY_FINGERPRINT] == key
    ]
    assert hits
    for r in hits:
        assert r["suppressions"][0]["status"] == "accepted"
        assert "reviewed" in r["suppressions"][0]["justification"]


def test_check_diff_sarif_sets_baseline_state(check_board, tmp_path, capsys):
    old = tmp_path / "old" / "test_project.kicad_pcb"
    shutil.copytree(check_board.parent, old.parent)
    _move(check_board, "fp-d1-uuid", "140 50", "141 50")

    code = check_cmd.main(["--diff", str(old), str(check_board), "--format", "sarif"])
    log = json.loads(capsys.readouterr().out)
    _validate_sarif(log)
    assert code in (0, 2)
    run = log["runs"][0]
    assert run["properties"]["summary"]["changed"] >= 1
    states = {r["baselineState"] for r in run["results"]}
    assert "updated" in states
    assert states <= {"new", "updated", "absent"}


def test_detect_mistakes_sarif_validates(board, capsys):
    _, data = _mistakes_json(capsys, str(board))
    _, log = _mistakes_sarif(capsys, str(board))
    _validate_sarif(log)
    run = log["runs"][0]
    assert run["tool"]["driver"]["name"] == "kct detect-mistakes"
    by_key = {m["key"]: m for m in data["mistakes"]}
    for r in run["results"]:
        key = r["partialFingerprints"][KEY_FINGERPRINT]
        assert r["fingerprints"]["kctEvidence/ev2"] == by_key[key]["evidence_hash"]
    # Mistake locations are reported in sheet coordinates, like kct check:
    # the fixture's board origin is (100, 100) and C1 sits at (115, 120).
    c1 = next(r for r in run["results"] if r["partialFingerprints"][KEY_FINGERPRINT] == BYPASS_C1)
    loc = c1["locations"][0]["properties"]["boardLocation"]
    assert (loc["x"], loc["y"]) == (115.0, 120.0)


def test_sarif_builder_unit():
    findings = [
        {
            "rule_id": "r1",
            "severity": "info",
            "message": "m",
            "items": ["U1.3"],
            "nets": [],
            "key": "r1|U1.3||",
            "evidence_hash": "ev2:abc",
            "location": [1.0, 2.0],
            "closest_locations": [[1.0, 2.0], [3.0, 4.0]],
        },
        {"rule_id": "r1", "severity": "error", "message": "m2", "key": "r1|||"},
    ]
    log = sarif_log(findings, tool_name="t", artifact=None)
    _validate_sarif(log)
    run = log["runs"][0]
    assert run["tool"]["driver"]["rules"][0]["defaultConfiguration"]["level"] == "error"
    assert run["results"][0]["level"] == "note"
    assert len(run["results"][0]["relatedLocations"]) == 2
    assert "fingerprints" not in run["results"][1]

    diff = {
        "summary": {"introduced": 1},
        "introduced": [findings[0]],
        "changed": [{"key": "r1|||", "old": findings[1], "new": findings[1]}],
        "resolved": [findings[1]],
    }
    dlog = diff_sarif_log(diff, tool_name="t", artifact=None)
    _validate_sarif(dlog)
    assert [r["baselineState"] for r in dlog["runs"][0]["results"]] == ["new", "updated", "absent"]


def test_text_anchors(tmp_path):
    pcb = tmp_path / "b.kicad_pcb"
    pcb.write_text(
        '(kicad_pcb\n\t(net 0 "")\n\t(net 1 "GND")\n'
        '\t(footprint "R"\n\t\t(at 1 2)\n\t\t(property "Reference" "R7" (at 0 0))\n\t)\n)\n'
    )
    anchors = TextAnchors.from_file(pcb)
    assert anchors.line_for(["R7.2"], []) == 4
    assert anchors.line_for(["X9"], ["GND"]) == 3
    assert anchors.line_for([], []) is None
