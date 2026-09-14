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
