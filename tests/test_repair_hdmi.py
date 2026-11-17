"""The historical width correction must not hide changed copper contacts."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "repair_hdmi", ROOT / "boards/07-matchgroup-test/repair_hdmi.py"
)
assert SPEC and SPEC.loader
repair = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(repair)


def sidecar():
    return {name: {"name": "HDMI_TMDS_LANES", "trace_width": 0.225} for name in repair.TMDS_NETS}


def board(track_y=0.0):
    return f"""(kicad_pcb (version 20240108) (generator "test")
      (layers (0 "F.Cu" signal) (31 "B.Cu" signal))
      (net 1 "TMDS_D0_P") (net 2 "DQS_P")
      (footprint "test" (layer "F.Cu") (at 0 0)
        (property "Reference" "J1")
        (property "Value" "quoted (width 0.375) text")
        (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 1 "TMDS_D0_P")))
      (footprint "test" (layer "F.Cu") (at 4 0)
        (property "Reference" "U4")
        (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 1 "TMDS_D0_P")))
      (segment (start 0 {track_y}) (end 4 {track_y}) (width 0.375)
        (layer "F.Cu") (net 1) (uuid "10000000-0000-0000-0000-000000000001"))
      (segment (start 0 4) (end 4 4) (width 0.375) (layer "F.Cu") (net 2))
      (via (at 0 4) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 2)))"""


def test_only_tmds_width_token_changes_with_quotes_and_crlf():
    text = board().replace("\n", "\r\n")
    result, counts = repair.retarget_widths(text, sidecar())
    expected = text.replace("(end 4 0.0) (width 0.375)", "(end 4 0.0) (width 0.225)")
    assert result == expected
    assert sum(counts.values()) == 1
    assert counts["TMDS_D0_P"] == 1


def test_kicad10_named_net_is_retargeted():
    text = board().replace('(net 1 "TMDS_D0_P") (net 2 "DQS_P")', "")
    text = text.replace("(net 1)", '(net "TMDS_D0_P")').replace("(net 2)", '(net "DQS_P")')
    result, counts = repair.retarget_widths(text, sidecar())
    assert sum(counts.values()) == 1
    assert result.count("(width 0.225)") == 1
    assert '(width 0.375) (layer "F.Cu") (net "DQS_P")' in result


@pytest.mark.parametrize(
    "fault", ["unknown_net", "wrong_width", "wrong_class", "wrong_target", "extra_net"]
)
def test_unreviewed_inputs_are_refused(fault):
    text, rules = board(), sidecar()
    if fault == "unknown_net":
        text = text.replace("(net 1)", "(net 99)")
    elif fault == "wrong_width":
        text = text.replace("(width 0.375)", "(width 0.4)")
    elif fault == "wrong_class":
        rules["TMDS_D0_P"]["name"] = "DDR_DQS"
    elif fault == "wrong_target":
        rules["TMDS_D0_P"]["trace_width"] = 0.15
    else:
        rules["TMDS_EXTRA"] = rules["TMDS_D0_P"]
    with pytest.raises(ValueError):
        repair.retarget_widths(text, rules)


@pytest.mark.parametrize("loses_contact", [False, True])
def test_real_copper_contact_loss_blocks_promotion(tmp_path, loses_contact):
    original = tmp_path / "before.kicad_pcb"
    candidate = tmp_path / "after.kicad_pcb"
    text = board()
    if loses_contact:
        # Two trace caps overlap across a 0.30 mm gap at width 0.375;
        # after narrowing to 0.225 the physical copper no longer touches.
        # Pads are centered on their traces, avoiding pad-erosion ambiguity.
        text = text.replace("(at 4 0)", "(at 4 0.3)").replace("(end 4 0.0)", "(end 2 0.0)")
        text = (
            text[:-1] + '(segment (start 2 0.3) (end 4 0.3) (width 0.375) (layer "F.Cu") (net 1)))'
        )
    original.write_text(text)
    candidate.write_text(repair.retarget_widths(original.read_text(), sidecar())[0])
    before = repair.analyzer_components(original)
    after = repair.analyzer_components(candidate)
    assert before == [["J1.1", "U4.1"]]
    if loses_contact:
        assert after == [["J1.1"], ["U4.1"]]
        with pytest.raises(RuntimeError, match="physical pad components changed"):
            repair.require_same_components(before, after, "analyzer")
    else:
        repair.require_same_components(before, after, "analyzer")


def test_native_open_identities_and_error_multiplicity_are_not_counts():
    def report(uuid):
        return {
            "kicad_version": "10.0.6",
            "violations": [],
            "unconnected_items": [
                {"type": "unconnected_items", "severity": "error", "items": [{"uuid": uuid}]}
            ],
        }

    before = repair.native_signatures(report("pad-A"))
    after = repair.native_signatures(report("pad-B"))
    assert len(before["opens"]) == len(after["opens"])
    with pytest.raises(RuntimeError):
        repair.require_same_components(before, after, "native opens")
    with pytest.raises(RuntimeError, match="lacks unconnected_items"):
        repair.native_signatures({"kicad_version": "10.0.6", "violations": []})


def test_kct_preserves_existing_defects_but_rejects_different_open():
    errors = [
        {"rule_id": "connectivity", "severity": "error", "nets": [name]}
        for name in sorted(repair.KNOWN_OPENS)
    ]
    report = {"violations": errors, "summary": {"errors": 4}}
    assert repair.check_signatures(report)["open_nets"] == sorted(repair.KNOWN_OPENS)
    report["violations"][0]["nets"] = ["TMDS_D2_P"]
    with pytest.raises(RuntimeError, match="Unexpected historical open nets"):
        repair.check_signatures(report)


def test_actual_fixture_retarget_is_idempotent_and_preserves_other_bytes():
    fixture = ROOT / "boards/07-matchgroup-test/regression-fixture"
    text = (fixture / "matchgroup_test_routed.kicad_pcb").read_text()
    result, counts = repair.retarget_widths(
        text, json.loads((fixture / "net_class_map.json").read_text())
    )
    assert sum(counts.values()) in (0, 35)
    # Reversing only the modified character spans recovers all original bytes.
    changes = [(a, b) for a, b in zip(text, result, strict=True) if a != b]
    assert changes == [("3", "2"), ("7", "2")] * sum(counts.values())
    repeated, counts = repair.retarget_widths(
        result, json.loads((fixture / "net_class_map.json").read_text())
    )
    assert repeated == result
    assert sum(counts.values()) == 0


def test_check_error_exit_is_distinct_from_a_failed_command(tmp_path):
    log = tmp_path / "check.log"
    command = [sys.executable, "-c", "raise SystemExit(2)"]
    assert repair.run_command(command, log, (0, 2)) == 2
    assert json.loads(log.read_text().splitlines()[0]) == command
    with pytest.raises(RuntimeError, match=r"Command failed \(1\)"):
        repair.run_command([sys.executable, "-c", "raise SystemExit(1)"], log, (0, 2))
