"""CLI tests for ``kct net-status --crossings`` (Issue #6339). Read-only."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from kicad_tools.cli.net_status_cmd import main

REPO = Path(__file__).resolve().parent.parent
BOARD05 = REPO / "boards/05-bldc-motor-controller/output/bldc_controller.kicad_pcb"
BOARD00 = REPO / "boards/00-simple-led/output/simple_led.kicad_pcb"

pytestmark = pytest.mark.skipif(not BOARD05.exists(), reason="board 05 not present")


def _json(capsys, *argv):
    rc = main([str(BOARD05), "--crossings", "--format", "json", *argv])
    assert rc == 0
    return json.loads(capsys.readouterr().out)


def test_board05_reports_en_b_pwm_c_transposition(capsys):
    d = _json(capsys)
    bundle = next(
        b
        for b in d["bundles"]
        if {"PWM_B", "EN_B", "PWM_C", "EN_C"} <= set(b["order_a"])
        and {b["ref_a"], b["ref_b"]} == {"U1", "U2"}
    )
    assert bundle["min_inversions"] == 1
    assert bundle["inverted_pairs"] == [["PWM_C", "EN_B"]]
    assert bundle["entries"]["aligned"] == 1 and bundle["entries"]["mirrored"] == 5


def test_board05_reports_gnd_sense_phase_interleave(capsys):
    d = _json(capsys)
    hub = next(h for h in d["hubs"] if h["ref"] == "U2" and h["side"] == "left")
    interleaved = [
        i for i in hub["interleaved"] if "GND_SENSE" in i["separated_by"] and "PHASE_B" in i["nets"]
    ]
    assert interleaved and interleaved[0]["exit"] == "left"


def test_text_form(capsys):
    assert main([str(BOARD05), "--crossings"]) == 0
    out = capsys.readouterr().out
    assert "PWM_C/EN_B" in out and "GND_SENSE" in out


def test_signal_layers_flag(capsys):
    assert _json(capsys, "--signal-layers", "2")["signal_layers"] == 2


def test_net_filter(capsys):
    d = _json(capsys, "--net", "EN_B")
    assert d["bundles"] and all("EN_B" in b["order_a"] for b in d["bundles"])


def test_default_output_has_no_crossing_content(capsys):
    main([str(BOARD05), "--format", "json"])
    plain = json.loads(capsys.readouterr().out)
    assert "bundles" not in plain and "signal_layers" not in plain
    main([str(BOARD05)])
    assert "Bundle crossing" not in capsys.readouterr().out


def test_analysis_is_read_only():
    before = BOARD05.read_bytes()
    main([str(BOARD05), "--crossings"])
    assert BOARD05.read_bytes() == before
    status = subprocess.run(
        ["git", "status", "--porcelain", "boards/"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    )
    assert status.stdout == ""
