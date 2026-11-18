"""Reviewed USB copper must remain 45-degree aligned through saved-plan replay."""

import json
import runpy
from pathlib import Path

import pytest

from kicad_tools.router.quantize import segment_angle_census
from kicad_tools.sexp import parse_string, serialize_sexp

BOARD = Path(__file__).resolve().parents[1] / "boards/03-usb-joystick"


@pytest.fixture
def routing():
    return runpy.run_path(str(BOARD / "routing_plan.py"))


def test_reviewed_plan_replays_aligned_usb_copper(routing, tmp_path):
    output = tmp_path / "replayed.kicad_pcb"
    assert routing["apply_plan"](BOARD / "output/usb_joystick.kicad_pcb", output)
    count, off_angle = segment_angle_census(output)
    assert count > 0
    assert off_angle == []
    assert routing["usb_geometry"](output)["branch_skew_mm"] < 0.5


def test_off_angle_replay_leaves_existing_output_untouched(routing, tmp_path):
    plan = json.loads((BOARD / "routing-plan.json").read_text())
    index = next(i for i, item in enumerate(plan["copper"]) if item.startswith("(segment"))
    segment = parse_string(plan["copper"][index])
    end = segment.find_child("end")
    end.set_value(1, end.get_float(1) + 0.0123)
    plan["copper"][index] = serialize_sexp(segment)
    plan_file = tmp_path / "invalid-plan.json"
    plan_file.write_text(json.dumps(plan))
    output = tmp_path / "replayed.kicad_pcb"
    output.write_text("previous reviewed output")
    with pytest.raises(ValueError, match="off-angle copper"):
        routing["apply_plan"](BOARD / "output/usb_joystick.kicad_pcb", output, plan_file)
    assert output.read_text() == "previous reviewed output"
    assert not (tmp_path / ".replayed.candidate.kicad_pcb").exists()


def test_capture_rejects_off_angle_copper_before_writing_plan(routing, tmp_path):
    pcb = tmp_path / "off-angle.kicad_pcb"
    pcb.write_text(
        "(kicad_pcb (segment (start 0 0) (end 1 0.1) "
        '(width 0.2) (layer "F.Cu") (net 1) (uuid "witness")))'
    )
    output = tmp_path / "plan.json"
    with pytest.raises(ValueError, match="off-angle copper"):
        routing["save_plan"](pcb, output)
    assert not output.exists()


# Issue #7033: net ids are per-board, so a plan must store net names.
_NUMERIC_BOARD = (
    '(kicad_pcb (net 0 "") (net 1 "GND") (net 2 "VBUS") '
    '(segment (start 0 0) (end 1 0) (width 0.2) (layer "F.Cu") (net 2) (uuid "a")) '
    '(via (at 1 0) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1) (uuid "b")))'
)


def _stub_board_checks(routing, monkeypatch):
    scope = routing["save_plan"].__globals__
    monkeypatch.setitem(scope, "fingerprint", lambda path: "stub")
    monkeypatch.setitem(scope, "usb_geometry", lambda path: {})


def test_capture_stores_net_names_not_ids(routing, tmp_path, monkeypatch):
    _stub_board_checks(routing, monkeypatch)
    pcb = tmp_path / "numeric.kicad_pcb"
    pcb.write_text(_NUMERIC_BOARD)
    plan_file = tmp_path / "plan.json"
    routing["save_plan"](pcb, plan_file)
    copper = json.loads(plan_file.read_text())["copper"]
    nets = [parse_string(item).find_child("net") for item in copper]
    assert [n.get_string(0) for n in nets] == ["VBUS", "GND"]
    assert all(len(n.children) == 1 for n in nets)


def test_capture_rejects_unknown_net_id(routing, tmp_path, monkeypatch):
    _stub_board_checks(routing, monkeypatch)
    pcb = tmp_path / "unknown.kicad_pcb"
    pcb.write_text(_NUMERIC_BOARD.replace("(net 2)", "(net 9)"))
    plan_file = tmp_path / "plan.json"
    with pytest.raises(ValueError, match="net id 9"):
        routing["save_plan"](pcb, plan_file)
    assert not plan_file.exists()


def test_apply_rejects_numeric_net_ref(routing, tmp_path, monkeypatch):
    _stub_board_checks(routing, monkeypatch)
    plan = {
        "schema_version": 1,
        "physical_sha256": "stub",
        "copper": ['(segment (start 0 0) (end 1 0) (width 0.2) (layer "F.Cu") (net 8))'],
        "silkscreen": {},
    }
    plan_file = tmp_path / "numeric-plan.json"
    plan_file.write_text(json.dumps(plan))
    pcb = tmp_path / "board.kicad_pcb"
    pcb.write_text(_NUMERIC_BOARD)
    with pytest.raises(ValueError, match="numeric net id 8"):
        routing["apply_plan"](pcb, tmp_path / "out.kicad_pcb", plan_file)


# Issue #9143: the recipe adds stock fiducials to the routed board after
# routing (#8532). They are netless assembly aids, so the reviewed physical
# contract must fingerprint identically with and without them -- otherwise
# every export/check gate would refuse the board the plan itself produced.
_STOCK_FIDUCIAL = """(footprint "Fiducial:Fiducial_1mm_Mask2mm"
		(layer "F.Cu")
		(at 100 100)
		(property "Reference" "FID9" (at 0 -1.95 0) (layer "F.SilkS"))
		(property "Value" "Fiducial" (at 0 1.95 0) (layer "F.Fab"))
		(pad "" smd circle
			(at 0 0)
			(size 1 1)
			(layers "F.Cu" "F.Mask")
			(solder_mask_margin 0.5)
			(clearance 0.6)
			(uuid "00000000-0000-0000-0000-000000000009"))
		(attr smd exclude_from_pos_files exclude_from_bom board_only))"""


def test_physical_contract_ignores_fiducial_footprints(routing, tmp_path):
    committed = BOARD / "output/usb_joystick_routed.kicad_pcb"
    doc_text = committed.read_text()
    with_fid = tmp_path / "with-fiducial.kicad_pcb"
    # Append inside the board: the final ')' closes (kicad_pcb).
    with_fid.write_text(doc_text[:-1] + _STOCK_FIDUCIAL + ")")
    assert routing["fingerprint"](with_fid) == routing["fingerprint"](committed)
    # A netted probe pad on a non-fiducial footprint is NOT exempt: the
    # contract still binds it (the netless exemption is fiducial-shaped only).
    probe = (
        _STOCK_FIDUCIAL.replace("Fiducial:Fiducial_1mm_Mask2mm", "Probe:Probe_1mm")
        .replace("FID9", "PRBE")
        .replace('(pad "" smd', '(pad "1" smd')
    )
    with_probe = tmp_path / "with-netted-probe.kicad_pcb"
    with_probe.write_text(doc_text[:-1] + probe + ")")
    assert routing["fingerprint"](with_probe) != routing["fingerprint"](committed)
