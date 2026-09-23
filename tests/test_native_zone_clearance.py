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
    # Issue #5617: skip_first_refill is False here -- ``_kicad_cli_has_fill_zones``
    # is monkeypatched True-free (returns False, i.e. the DRC-fallback branch is
    # "used"), but ``_kicad_drc_supports_refill(Path("native"))`` genuinely probes
    # a non-existent binary and returns False, so the caller cannot assert the
    # fill above already refilled+saved via ``--refill-zones --save-board``.
    remediation.assert_called_once_with(pcb, Path("native"), settle=None, skip_first_refill=False)
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


@pytest.mark.parametrize("strong_net,weak_net", [("A", "Z"), ("Z", "A")])
def test_adjacent_zone_rules_preserve_strongest_target(tmp_path, strong_net, weak_net):
    from kicad_tools.sexp import parse_string
    from kicad_tools.zones.native_clearance import write_native_zone_clearance_rules

    pcb = tmp_path / "board.kicad_pcb"
    pcb.write_text(f'''(kicad_pcb (version 20260206)
      (zone (net "{strong_net}") (layer "F.Cu")
        (connect_pads (clearance 0.5)) (min_thickness 0.2))
      (zone (net "{weak_net}") (layer "F.Cu")
        (connect_pads (clearance 0.2)) (min_thickness 0.2)))''')
    write_native_zone_clearance_rules(pcb)
    dru = pcb.with_suffix(".kicad_dru")
    first = dru.read_bytes()
    rules = parse_string("(rules " + first.decode() + ")").find_all("rule")
    # Both generated conditions match this pair; native precedence chooses
    # the final one, which must retain the stronger 0.6 mm target.
    rules = list(rules)
    assert len(rules) == 2
    assert strong_net in rules[-1].find("condition").get_string(0)
    assert rules[-1].find("constraint").find("min").get_string(0) == "0.6mm"
    write_native_zone_clearance_rules(pcb)
    assert dru.read_bytes() == first


@pytest.mark.parametrize("strong_net,weak_net", [("A", "Z"), ("Z", "A")])
def test_native_refill_keeps_stronger_adjacent_zone_gap(tmp_path, strong_net, weak_net):
    import subprocess
    import uuid

    from kicad_tools.sexp import parse_file
    from kicad_tools.zones.native_clearance import write_native_zone_clearance_rules

    geometry = pytest.importorskip("shapely.geometry")
    ops = pytest.importorskip("shapely.ops")
    cli = runner.find_kicad_cli()
    if cli is None:
        pytest.skip("requires native KiCad")
    pcb = tmp_path / "board.kicad_pcb"
    zones = []
    for net, clearance, left, right in [(strong_net, 0.5, 1, 15), (weak_net, 0.2, 15, 29)]:
        zones.append(f'''(zone (net "{net}") (layer "F.Cu") (uuid "{uuid.uuid4()}")
          (hatch edge 0.5) (connect_pads (clearance {clearance})) (min_thickness 0.2)
          (fill yes (thermal_gap 0.2) (thermal_bridge_width 0.35) (island_removal_mode 0))
          (polygon (pts (xy {left} 1) (xy {right} 1) (xy {right} 29) (xy {left} 29))))''')
    pcb.write_text(
        """(kicad_pcb (version 20260206) (generator "pcbnew")
      (general (thickness 1.6)) (paper "A4")
      (layers (0 "F.Cu" signal) (2 "B.Cu" signal) (25 "Edge.Cuts" user))
      (setup (pad_to_mask_clearance 0))
      (gr_rect (start 0 0) (end 30 30) (stroke (width 0.05) (type default))
        (fill no) (layer "Edge.Cuts"))
    """
        + "\n".join(zones)
        + ")"
    )
    write_native_zone_clearance_rules(pcb)
    result = subprocess.run(
        [
            str(cli),
            "pcb",
            "drc",
            str(pcb),
            "--refill-zones",
            "--save-board",
            "--format",
            "json",
            "-o",
            str(tmp_path / "drc.json"),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    copper = []
    for zone in parse_file(pcb).find_all("zone"):
        polygons = []
        for fill in zone.find_all("filled_polygon"):
            points = fill.find("pts")
            polygons.append(
                geometry.Polygon(
                    [(point.get_float(0), point.get_float(1)) for point in points.find_all("xy")]
                )
            )
        copper.append(ops.unary_union(polygons))
    assert len(copper) == 2 and all(not shape.is_empty for shape in copper)
    # Measure actual filled copper, not the emitted rule text or DRC count.
    assert 0.6 <= copper[0].distance(copper[1]) < 0.62
