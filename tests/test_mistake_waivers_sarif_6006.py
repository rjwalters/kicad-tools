"""detect-mistakes evidence-bound waivers and SARIF output (issue #6006)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

jsonschema = pytest.importorskip("jsonschema")  # dev extra; skip under a plain `uv sync`

from kicad_tools.cli import check_cmd, mistakes_cmd
from kicad_tools.explain.mistake_waivers import (
    annotate_mistakes,
    apply_mistake_waivers,
    split_components,
)
from kicad_tools.explain.mistakes import Mistake, MistakeCategory, mistake_rule_id
from kicad_tools.validate.evidence import (
    EVIDENCE_HASH_PREFIX,
    EVIDENCE_HASH_VERSION,
    MISTAKE_EVIDENCE_HASH_PREFIX,
    MISTAKE_EVIDENCE_HASH_VERSION,
    MISTAKE_MEASUREMENT_RECIPE,
    current_evidence_hash_version,
    is_outdated_evidence_hash,
)
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
    evidence_fingerprint_name,
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
        "measurements": {"distance_mm": 13.3},
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
        a, b, c = _m(), _m(), _m(measurements={"distance_mm": 12.9})
        annotate_mistakes([a], None)
        annotate_mistakes([b], None)
        annotate_mistakes([c], None)
        assert a.key == b.key == c.key == BYPASS_C1
        assert a.evidence_hash == b.evidence_hash
        assert a.evidence_hash.startswith(MISTAKE_EVIDENCE_HASH_PREFIX)
        # The measured value is evidence: 13.3 mm -> 12.9 mm is new.
        assert c.evidence_hash != a.evidence_hash
        # Wording outside the numbers (title) is not.
        d = _m(title="Bypass cap far away")
        annotate_mistakes([d], None)
        assert d.evidence_hash == a.evidence_hash

    def test_rewording_the_explanation_keeps_the_hash(self):
        """Explanation text -- digits included -- is never hashed (judge, PR #6056)."""
        a = _m()
        reworded = _m(
            explanation=(
                "Rewritten copy: C1 sits 13.3mm away from pin 7 of U1 on net +5V0; "
                "keep it under 2.5mm (rule of thumb for 1A at 2oz, 100nF, 0402)."
            ),
            fix_suggestion="Move C1 to within 1mm of U1 pin 14.",
        )
        annotate_mistakes([a], None)
        annotate_mistakes([reworded], None)
        assert reworded.evidence_hash == a.evidence_hash

    def test_measurements_are_canonicalized(self):
        a = _m(measurements={"segment_count": 2, "min_width_mm": 0.25})
        b = _m(measurements={"min_width_mm": 0.25, "segment_count": 2.0})
        annotate_mistakes([a], None)
        annotate_mistakes([b], None)
        assert a.evidence_hash == b.evidence_hash

    def test_changing_a_ref_or_net_changes_the_hash(self):
        """Subjects stay evidence through the key, not through scraped text."""
        a = _m()
        other_ref = _m(components=["C2", "U1"])
        other_net = _m(components=["+5V"], rule_id="mistake.power_trace_width")
        other_net2 = _m(components=["+3V3"], rule_id="mistake.power_trace_width")
        for m in (a, other_ref, other_net, other_net2):
            annotate_mistakes([m], None)
        assert other_ref.evidence_hash != a.evidence_hash
        assert other_net.evidence_hash != other_net2.evidence_hash

    def test_real_checks_report_structured_measurements(self, board):
        from kicad_tools.explain.mistakes import MistakeDetector
        from kicad_tools.schema.pcb import PCB

        mistakes = MistakeDetector().detect(PCB.load(str(board)))
        by_rule = {m.rule_id: m for m in mistakes}
        bypass = by_rule["mistake.bypass_cap_distance"]
        assert set(bypass.measurements) == {"distance_mm"}
        # The quoted distance, at the quoted precision -- not the 3.0 mm limit,
        # the pin number or the digits of "C1" / "U1" / "+3V3".
        assert f"{bypass.measurements['distance_mm']:.1f}mm" in bypass.explanation
        power = by_rule["mistake.power_trace_width"]
        assert set(power.measurements) == {"min_width_mm", "segment_count"}
        assert f"{power.measurements['min_width_mm']:.2f}mm" in power.explanation

    def test_mistake_hashes_have_their_own_version(self):
        assert f"{MISTAKE_EVIDENCE_HASH_VERSION}:" == MISTAKE_EVIDENCE_HASH_PREFIX
        assert (
            f"{EVIDENCE_HASH_VERSION}.{MISTAKE_MEASUREMENT_RECIPE}"
        ) == MISTAKE_EVIDENCE_HASH_VERSION
        current = MISTAKE_EVIDENCE_HASH_PREFIX + "0"
        assert not is_outdated_evidence_hash(current)
        assert current_evidence_hash_version(current) == MISTAKE_EVIDENCE_HASH_VERSION
        # Either part of the compound version moving makes a mistake hash outdated ...
        assert is_outdated_evidence_hash(f"ev0.{MISTAKE_MEASUREMENT_RECIPE}:0")
        assert is_outdated_evidence_hash(f"{EVIDENCE_HASH_VERSION}.m0:0")
        # ... while kct check hashes are judged against their own version only.
        assert not is_outdated_evidence_hash(EVIDENCE_HASH_PREFIX + "0")
        assert current_evidence_hash_version(EVIDENCE_HASH_PREFIX + "0") == EVIDENCE_HASH_VERSION
        assert evidence_fingerprint_name(current) == f"kctEvidence/{MISTAKE_EVIDENCE_HASH_VERSION}"

    def test_outdated_measurement_recipe_is_reported_as_such(self):
        reviewed = _m()
        adapters = annotate_mistakes([reviewed], None)
        old = f"{EVIDENCE_HASH_VERSION}.m0:" + reviewed.evidence_hash.split(":", 1)[1]
        waivers = waivers_from_dict(
            {
                "version": 3,
                "waivers": [
                    {
                        "key": reviewed.key,
                        "evidence_hash": old,
                        "reason": "r",
                        "reviewer": "ee",
                        "date": "2026-10-06",
                    }
                ],
            }
        )
        outcome = apply_mistake_waivers([reviewed], adapters, waivers.for_mistakes())
        assert not reviewed.waived
        assert reviewed.to_dict()["stale_waiver_cause"] == "outdated_evidence_version"
        assert MISTAKE_EVIDENCE_HASH_VERSION in outcome.advisories[0].message

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
                        "evidence_hash": MISTAKE_EVIDENCE_HASH_PREFIX + "0",
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
        assert m["evidence_hash"].startswith(MISTAKE_EVIDENCE_HASH_PREFIX)
        assert isinstance(m["measurements"], dict)
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
        assert r["fingerprints"] == {f"kctEvidence/{EVIDENCE_HASH_VERSION}": v["evidence_hash"]}
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
        name = f"kctEvidence/{MISTAKE_EVIDENCE_HASH_VERSION}"
        assert r["fingerprints"][name] == by_key[key]["evidence_hash"]
    # Mistake locations are reported in sheet coordinates, like kct check:
    # the fixture's board origin is (100, 100) and C1 sits at (115, 120).
    c1 = next(r for r in run["results"] if r["partialFingerprints"][KEY_FINGERPRINT] == BYPASS_C1)
    loc = c1["locations"][0]["properties"]["boardLocation"]
    assert (loc["x"], loc["y"]) == (115.0, 120.0)
    assert loc["frame"] == "sheet"


def test_sarif_builder_unit():
    findings = [
        {
            "rule_id": "r1",
            "severity": "info",
            "message": "m",
            "items": ["U1.3"],
            "nets": [],
            "key": "r1|U1.3||",
            "evidence_hash": EVIDENCE_HASH_PREFIX + "abc",
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


def test_absent_results_carry_no_line_region(tmp_path):
    """A resolved finding is gone from the new file: no line to point at."""
    pcb = tmp_path / "b.kicad_pcb"
    pcb.write_text(
        '(kicad_pcb\n\t(footprint "R"\n\t\t(at 1 2)\n'
        '\t\t(property "Reference" "R7" (at 0 0))\n\t)\n)\n'
    )
    row = {"rule_id": "r1", "severity": "error", "message": "m", "items": ["R7"], "key": "r1|R7||"}
    diff = {"introduced": [row], "resolved": [row]}
    log = diff_sarif_log(diff, tool_name="t", artifact=pcb)
    _validate_sarif(log)
    new, absent = log["runs"][0]["results"]
    assert new["locations"][0]["physicalLocation"]["region"] == {"startLine": 2}
    assert "region" not in absent["locations"][0]["physicalLocation"]
    assert absent["locations"][0]["logicalLocations"][0]["name"] == "R7"


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
