"""The native fill must consume the clearance previously applied after fill."""

from pathlib import Path
from unittest.mock import Mock

import pytest

from kicad_tools.cli import runner

BOARD = """(kicad_pcb (version 20260206)
 (net 1 "GND")
 (zone (net 1) (layer "F.Cu")
  (connect_pads (clearance 0.2)) (min_thickness 0.2)))"""


def test_native_clearance_precedes_fill_and_preserves_factory_rules(tmp_path, monkeypatch):
    pcb = tmp_path / "board.kicad_pcb"
    pcb.write_text(BOARD)
    dru = pcb.with_suffix(".kicad_dru")
    original = '(version 1)\n(rule "Factory hole floor" (constraint hole_clearance (min 0.3mm)))\n'
    dru.write_text(original)
    monkeypatch.setattr(runner, "_normalize_pad_connection_in_place", lambda p: None)
    monkeypatch.setattr(runner, "_kicad_cli_has_fill_zones", lambda cli: False)

    def fill(path, output, cli):
        rules = dru.read_text()
        assert original in rules
        assert "A.Type == 'Zone'" in rules
        assert "A.NetName == 'GND'" in rules
        assert '(layer "F.Cu")' in rules
        assert "(constraint clearance (min 0.3mm))" in rules
        return runner.KiCadCLIResult(success=True, output_path=path)

    monkeypatch.setattr(runner, "_run_fill_zones_via_drc", fill)
    remediation = Mock()
    monkeypatch.setattr(runner, "_remediate_starved_thermal", remediation)
    assert runner.run_fill_zones(pcb, kicad_cli=Path("native"), native_clearance=True).success
    remediation.assert_called_once_with(pcb, Path("native"), settle=None)
    first = dru.read_bytes()
    runner.run_fill_zones(pcb, kicad_cli=Path("native"), native_clearance=True)
    assert dru.read_bytes() == first


def test_native_clearance_rejects_unsupported_separate_output(tmp_path):
    pcb = tmp_path / "board.kicad_pcb"
    pcb.write_text(BOARD)
    with pytest.raises(ValueError, match="in-place"):
        runner.run_fill_zones(
            pcb,
            output_path=tmp_path / "out.kicad_pcb",
            kicad_cli=Path("native"),
            native_clearance=True,
        )
    assert not pcb.with_suffix(".kicad_dru").exists()


def test_native_clearance_never_overrides_a_stronger_rule(tmp_path):
    from kicad_tools.zones.native_clearance import write_native_zone_clearance_rules

    pcb = tmp_path / "board.kicad_pcb"
    pcb.write_text(BOARD)
    dru = pcb.with_suffix(".kicad_dru")
    original = '(version 1)\n(rule "Stronger" (constraint clearance (min 0.5mm)))\n'
    dru.write_text(original)
    with pytest.raises(ValueError, match="stronger"):
        write_native_zone_clearance_rules(pcb)
    assert dru.read_text() == original


@pytest.fixture(autouse=True)
def explicit_project(tmp_path):
    import json

    (tmp_path / "board.kicad_pro").write_text(
        json.dumps(
            {
                "net_settings": {"classes": [{"name": "Default", "clearance": 0.2}]},
            }
        )
    )


def test_native_clearance_preserves_stronger_net_class(tmp_path):
    import json

    from kicad_tools.zones.native_clearance import write_native_zone_clearance_rules

    pcb = tmp_path / "board.kicad_pcb"
    pcb.write_text(BOARD)
    pcb.with_suffix(".kicad_pro").write_text(
        json.dumps(
            {
                "net_settings": {"classes": [{"name": "Default", "clearance": 0.6}]},
            }
        )
    )
    with pytest.raises(ValueError, match="stronger"):
        write_native_zone_clearance_rules(pcb)
    assert not pcb.with_suffix(".kicad_dru").exists()


def test_native_clearance_preserves_stronger_pad_clearance(tmp_path):
    from kicad_tools.zones.native_clearance import write_native_zone_clearance_rules

    pcb = tmp_path / "board.kicad_pcb"
    pcb.write_text(BOARD[:-1] + '\n(footprint "Probe" (pad "1" smd rect (clearance 0.6))))')
    with pytest.raises(ValueError, match="stronger"):
        write_native_zone_clearance_rules(pcb)
    assert not pcb.with_suffix(".kicad_dru").exists()
