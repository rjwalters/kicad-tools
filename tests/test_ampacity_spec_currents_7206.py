"""Spec-declared currents must reach the ampacity gate (Issue #7206).

``project.kct`` declaring a current while no net class carries
``target_ampacity`` used to read ``ampacity: skipped:no_declared_targets``,
a non-gap state, so a necked motor phase signed off.  It is now a blocking
``unknown``; mapping the current onto nets (``target_ampacity``) makes the
rule measure it.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from kicad_tools.cli.check_cmd import main as check_main
from kicad_tools.cli.readiness_cmd import _blocking_unknown_coverage
from kicad_tools.sync.discover import resolve_declared_currents_for_pcb
from kicad_tools.validate import coverage as cov

REPO = Path(__file__).resolve().parents[1]
BOARD05 = REPO / "boards" / "05-bldc-motor-controller" / "output"


def _pcb(tmp_path: Path, kct: str | None) -> Path:
    pcb = tmp_path / "board.kicad_pcb"
    pcb.write_text("(kicad_pcb)")
    if kct is not None:
        (tmp_path / "project.kct").write_text(kct)
    return pcb


class TestResolver:
    def test_interface_current_max(self, tmp_path):
        pcb = _pcb(
            tmp_path,
            "intent:\n  interfaces:\n    - name: USB\n      current_max: 100mA\n",
        )
        assert resolve_declared_currents_for_pcb(pcb) == ["interfaces[USB].current_max"]

    def test_input_current_any_key(self, tmp_path):
        pcb = _pcb(
            tmp_path,
            "requirements:\n  electrical:\n    input:\n      current:\n        max: 500mA\n",
        )
        assert resolve_declared_currents_for_pcb(pcb) == ["input.current"]

    def test_output_current_max_need_not_parse(self, tmp_path):
        pcb = _pcb(
            tmp_path,
            "requirements:\n  electrical:\n    outputs:\n"
            '      - rail: LED current\n        current_max: "4.6mA per LED at 5V"\n',
        )
        assert resolve_declared_currents_for_pcb(pcb) == ["outputs[LED current].current_max"]

    def test_extra_current_key_dict_and_string(self, tmp_path):
        pcb = _pcb(
            tmp_path,
            "requirements:\n  electrical:\n    motor_current:\n      continuous: 0.5\n"
            "      peak: 1.0\n    led_current: 10mA\n    input_voltage: 5V\n",
        )
        assert resolve_declared_currents_for_pcb(pcb) == ["motor_current", "led_current"]

    def test_parent_directory_project_kct(self, tmp_path):
        (tmp_path / "output").mkdir()
        (tmp_path / "project.kct").write_text(
            "requirements:\n  electrical:\n    led_current: 1mA\n"
        )
        assert resolve_declared_currents_for_pcb(tmp_path / "output" / "b.kicad_pcb") == [
            "led_current"
        ]

    @pytest.mark.parametrize("kct", [None, "::: not yaml [", "- a\n- b\n", "intent: 3\n"])
    def test_missing_or_unreadable_spec_declares_nothing(self, tmp_path, kct):
        assert resolve_declared_currents_for_pcb(_pcb(tmp_path, kct)) == []

    def test_no_current_declared(self, tmp_path):
        pcb = _pcb(tmp_path, "requirements:\n  electrical:\n    input_voltage: 5V\n")
        assert resolve_declared_currents_for_pcb(pcb) == []


def _checker(declared, ncm=None):
    return SimpleNamespace(
        pcb=SimpleNamespace(segments=[], zones=[], nets={}),
        net_class_map=ncm,
        declared_spec_currents=declared,
    )


class TestCoverage:
    def test_declared_current_without_target_is_blocking_unknown(self):
        result = cov.precheck("ampacity", _checker(["motor_current", "led_current"]))
        assert result.state == cov.UNKNOWN
        assert result.blocking
        assert result.status == (
            "unknown:needs_input:target_ampacity (project.kct declares 2 current requirement(s))"
        )

    def test_no_declared_current_stays_skipped(self):
        assert cov.precheck("ampacity", _checker([])).status == "skipped:no_declared_targets"

    def test_target_declared_is_checked(self):
        ncm = {"VCC": SimpleNamespace(target_ampacity=0.1)}
        pcb = SimpleNamespace(segments=[SimpleNamespace(net_name="VCC")], zones=[])
        checker = SimpleNamespace(pcb=pcb, net_class_map=ncm, declared_spec_currents=["x"])
        assert cov.precheck("ampacity", checker) is None

    def test_blocking_unknown_names_ampacity(self):
        unknown = cov.precheck("ampacity", _checker(["x"]))
        report = {"coverage": cov.coverage_to_dict({"ampacity": unknown})}
        named = _blocking_unknown_coverage(report)
        assert len(named) == 1 and named[0].startswith("ampacity (needs_input:target_ampacity")


def _run_check(pcb: Path, *extra: str, capsys) -> dict:
    check_main([str(pcb), "--mfr", "jlcpcb", "--format", "json", "--only", "ampacity", *extra])
    return json.loads(capsys.readouterr().out)


def _board05_copy(tmp_path: Path, *, mutate_layer: str | None = None) -> Path:
    for name in ("bldc_controller_routed.kicad_pcb", "net_class_map.json", "project.kct"):
        shutil.copy2(BOARD05 / name, tmp_path / name)
    pcb = tmp_path / "bldc_controller_routed.kicad_pcb"
    if mutate_layer:
        text = pcb.read_text()
        pattern = re.compile(
            r"(\(segment\s+\(start [^)]*\)\s+\(end [^)]*\)\s+\(width )[\d.]+(\)\s+"
            rf'\(layer "{re.escape(mutate_layer)}"\)\s+\(net "PHASE_A"\))'
        )
        text, n = pattern.subn(r"\g<1>0.2\g<2>", text, count=1)
        assert n == 1
        pcb.write_text(text)
    return pcb


class TestCheckCli:
    def test_spec_current_without_target_reports_unknown(self, tmp_path, capsys):
        (tmp_path / "project.kct").write_text(
            "requirements:\n  electrical:\n    led_current: 1mA\n"
        )
        shutil.copy2(BOARD05 / "bldc_controller_routed.kicad_pcb", tmp_path / "b.kicad_pcb")
        data = _run_check(tmp_path / "b.kicad_pcb", "--no-net-class-map", capsys=capsys)
        entry = data["coverage"]["ampacity"]
        assert entry["state"] == "unknown"
        assert entry["blocking"] is True
        assert "needs_input:target_ampacity" in entry["status"]

    def test_board05_committed_is_checked_and_clean(self, tmp_path, capsys):
        data = _run_check(_board05_copy(tmp_path), capsys=capsys)
        assert data["coverage"]["ampacity"]["state"] == "checked"
        assert data["summary"]["errors"] == 0

    def test_board05_inner_layer_neck_is_an_ampacity_error(self, tmp_path, capsys):
        data = _run_check(_board05_copy(tmp_path, mutate_layer="In2.Cu"), capsys=capsys)
        assert data["summary"]["errors"] == 1
        assert data["verdict"] == "fail"

    def test_board05_outer_layer_neck_is_correctly_compliant(self, tmp_path, capsys):
        """0.2 mm on F.Cu clears IPC-2221 at the declared 0.5 A continuous (needs 0.115 mm).

        Pinned so nobody "fixes" this by treating the 1.0 A trip as sustained.
        """
        data = _run_check(_board05_copy(tmp_path, mutate_layer="F.Cu"), capsys=capsys)
        assert data["summary"]["errors"] == 0


def test_board05_publishes_ampacity_target_on_motor_power():
    ncm = json.loads((BOARD05.parent / "redesign" / "net_class_map.json").read_text())
    motor = {n: c for n, c in ncm.items() if c["name"] == "Motor power"}
    assert set(motor) == {"VIN", "FUSED", "VM", "PHASE_A", "PHASE_B", "PHASE_C", "GND_SENSE"}
    assert all(c["target_ampacity"] == 0.5 and c["neck_trace_width"] == 0.3 for c in motor.values())
    assert (BOARD05 / "net_class_map.json").read_text() == (
        BOARD05.parent / "redesign" / "net_class_map.json"
    ).read_text()
