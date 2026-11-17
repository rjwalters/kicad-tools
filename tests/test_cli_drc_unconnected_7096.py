"""``kct drc`` must not pass a board with error-severity unconnected items (Issue #7096)."""

from __future__ import annotations

import json
from pathlib import Path

from kicad_tools.cli import drc_cmd

ITEM = {
    "description": "Pad 1 [N1] of R1 on F.Cu",
    "uuid": "aaaaaaaa-0000-0000-0000-000000000001",
    "pos": {"x": 1.0, "y": 1.0},
}


def _report(unconnected: list[dict], violations: list[dict] | None = None) -> dict:
    return {
        "$schema": "https://schemas.kicad.org/drc.v1.json",
        "source": "board.kicad_pcb",
        "date": "2026-10-18T00:00:00+00:00",
        "kicad_version": "10.0.5",
        "coordinate_units": "mm",
        "violations": violations or [],
        "unconnected_items": unconnected,
        "schematic_parity": [],
    }


def _open(severity: str = "error") -> dict:
    return {
        "type": "unconnected_items",
        "severity": severity,
        "description": "Missing connection between items",
        "items": [ITEM, {**ITEM, "uuid": "bbbbbbbb-0000-0000-0000-000000000002"}],
    }


def _write(tmp_path: Path, data: dict) -> str:
    path = tmp_path / "board-drc.json"
    path.write_text(json.dumps(data))
    return str(path)


def test_unrouted_report_fails(tmp_path, capsys):
    rc = drc_cmd.main([_write(tmp_path, _report([_open()])), "--allow-incomplete"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "PASSED" not in out


def test_routed_report_passes(tmp_path):
    assert drc_cmd.main([_write(tmp_path, _report([])), "--allow-incomplete"]) == 0


def test_ignore_unconnected_restores_clearance_only(tmp_path):
    path = _write(tmp_path, _report([_open()]))
    assert drc_cmd.main([path, "--allow-incomplete", "--ignore-unconnected"]) == 0


def test_warning_severity_unconnected_does_not_fail(tmp_path):
    path = _write(tmp_path, _report([_open("warning")]))
    assert drc_cmd.main([path, "--allow-incomplete"]) == 0


def test_json_carries_unconnected_count(tmp_path, capsys):
    path = _write(tmp_path, _report([_open(), _open()]))
    rc = drc_cmd.main([path, "--allow-incomplete", "--format", "json"])
    data = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert data["summary"]["unconnected"] == 2
    assert data["summary"]["errors"] == 2


def test_json_ignore_unconnected_counts_zero(tmp_path, capsys):
    path = _write(tmp_path, _report([_open()]))
    rc = drc_cmd.main([path, "--allow-incomplete", "--format", "json", "--ignore-unconnected"])
    data = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert data["summary"]["unconnected"] == 0


def test_summary_format_fails(tmp_path, capsys):
    path = _write(tmp_path, _report([_open()]))
    assert drc_cmd.main([path, "--allow-incomplete", "--format", "summary"]) == 1
    assert "unconnected_items" in capsys.readouterr().out


def test_errors_only_still_fails(tmp_path):
    path = _write(tmp_path, _report([_open()]))
    assert drc_cmd.main([path, "--allow-incomplete", "--errors-only"]) == 1


def test_filter_config_does_not_drop_unconnected(tmp_path):
    cfg = tmp_path / "filters.toml"
    cfg.write_text('[[drc.filters]]\ntype = "clearance"\naction = "ignore"\n')
    path = _write(tmp_path, _report([_open()]))
    assert drc_cmd.main([path, "--allow-incomplete", "--filter-config", str(cfg)]) == 1


def test_mfr_mode_fails_on_unconnected(tmp_path, capsys):
    path = _write(tmp_path, _report([_open()]))
    assert drc_cmd.main([path, "--mfr", "jlcpcb", "--allow-incomplete"]) == 1
    assert "UNCONNECTED ITEMS" in capsys.readouterr().out
    rc = drc_cmd.main([path, "--mfr", "jlcpcb", "--allow-incomplete", "--ignore-unconnected"])
    assert rc == 0


def test_mfr_json_reports_unconnected(tmp_path, capsys):
    path = _write(tmp_path, _report([_open(), _open()]))
    rc = drc_cmd.main([path, "--mfr", "jlcpcb", "--allow-incomplete", "--format", "json"])
    data = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert data["exit_code"] == 1
    assert data["summary"]["unconnected"] == 2
    assert len(data["unconnected_items"]) == 2
    assert data["unconnected_items"][0]["type_str"] == "unconnected_items"


def test_mfr_json_ignore_unconnected(tmp_path, capsys):
    path = _write(tmp_path, _report([_open()]))
    rc = drc_cmd.main(
        [path, "--mfr", "jlcpcb", "--allow-incomplete", "--format", "json", "--ignore-unconnected"]
    )
    data = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert data["summary"]["unconnected"] == 0
    assert data["unconnected_items"] == []


def test_mfr_json_routed_board_passes(tmp_path, capsys):
    path = _write(tmp_path, _report([]))
    rc = drc_cmd.main([path, "--mfr", "jlcpcb", "--allow-incomplete", "--format", "json"])
    data = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert data["summary"]["unconnected"] == 0


# A text-format (``kicad-cli pcb drc --format report``) report of the same
# unrouted board: its unconnected entries sit in the flat violations list.
RPT = """** Drc report for board.kicad_pcb **
** Created on 2026-10-18T00:00:00 **

** Found 0 DRC violations **

** Found 2 unconnected pads **
[unconnected_items]: Missing connection between items
    Local override; error
    @(141.0000 mm, 86.2300 mm): PTH pad 1 [VCC] of J1
    @(147.5000 mm, 85.5000 mm): Pad 1 [VCC] of R1 on F.Cu
[unconnected_items]: Missing connection between items
    Local override; error
    @(141.0000 mm, 88.7700 mm): PTH pad 2 [GND] of J1
    @(154.7300 mm, 87.5000 mm): PTH pad 1 [GND] of D1

** Found 0 Footprint errors **

** End of Report **
"""


def _write_rpt(tmp_path: Path) -> str:
    path = tmp_path / "board-drc.rpt"
    path.write_text(RPT)
    return str(path)


def test_mfr_json_rpt_does_not_double_count(tmp_path, capsys):
    path = _write_rpt(tmp_path)
    rc = drc_cmd.main([path, "--mfr", "jlcpcb", "--allow-incomplete", "--format", "json"])
    data = json.loads(capsys.readouterr().out)
    assert rc == 1
    # The two text-report entries are judged once, as connection failures.
    assert data["summary"]["incompatible"] == 2
    assert data["summary"]["unconnected"] == 2
    assert len(data["unconnected_items"]) == 2


def test_rpt_plain_json_does_not_double_count(tmp_path, capsys):
    path = _write_rpt(tmp_path)
    rc = drc_cmd.main([path, "--allow-incomplete", "--format", "json"])
    data = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert data["summary"]["errors"] == 2
    assert data["summary"]["unconnected"] == 2


def _sidecar(tmp_path: Path) -> str:
    path = tmp_path / "waivers.json"
    entries = [
        {
            "rule": "unconnected_items",
            "items": ["J1", "R1"],
            "nets": ["VCC"],
            "reason": "bring-up jumper wire",
            "issue": "#7096",
        },
        {
            "rule": "unconnected_items",
            "items": ["J1", "D1"],
            "nets": ["GND"],
            "reason": "bring-up jumper wire",
            "issue": "#7096",
        },
    ]
    path.write_text(json.dumps({"version": 2, "waivers": entries}))
    return str(path)


def _rpt_like_json(tmp_path: Path) -> str:
    def item(desc: str, n: int) -> dict:
        return {"description": desc, "uuid": f"{n:08d}-0000-0000-0000-000000000000"}

    def missing(a: str, b: str, n: int) -> dict:
        return {
            "type": "unconnected_items",
            "severity": "error",
            "description": "Missing connection between items",
            "items": [item(a, n), item(b, n + 1)],
        }

    return _write(
        tmp_path,
        _report(
            [
                missing("PTH pad 1 [VCC] of J1", "Pad 1 [VCC] of R1 on F.Cu", 1),
                missing("PTH pad 2 [GND] of J1", "PTH pad 1 [GND] of D1", 3),
            ]
        ),
    )


def test_waivers_cover_json_unconnected_items(tmp_path, capsys):
    path = _rpt_like_json(tmp_path)
    rc = drc_cmd.main(
        [path, "--waivers", _sidecar(tmp_path), "--allow-incomplete", "--format", "json"]
    )
    data = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert "unused_waivers" not in data
    assert data["summary"]["errors"] == 0
    assert data["summary"]["waived"] == 2


def test_waivers_cover_rpt_unconnected_items(tmp_path, capsys):
    path = _write_rpt(tmp_path)
    rc = drc_cmd.main(
        [path, "--waivers", _sidecar(tmp_path), "--allow-incomplete", "--format", "json"]
    )
    data = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert "unused_waivers" not in data
    assert data["summary"]["waived"] == 2


def test_partial_waivers_still_fail(tmp_path, capsys):
    path = _rpt_like_json(tmp_path)
    sidecar = Path(_sidecar(tmp_path))
    data = json.loads(sidecar.read_text())
    data["waivers"] = data["waivers"][:1]
    sidecar.write_text(json.dumps(data))
    rc = drc_cmd.main([path, "--waivers", str(sidecar), "--allow-incomplete"])
    assert rc == 1


def test_warning_severity_unconnected_is_listed_and_strict_trips(tmp_path, capsys):
    # A JSON report reads like the .rpt: a downgraded connectivity item is a
    # warning -- listed, non-blocking, but --strict trips on it.
    path = _write(tmp_path, _report([_open("warning")]))
    rc = drc_cmd.main([path, "--allow-incomplete", "--format", "json"])
    data = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert data["summary"]["warnings"] == 1
    assert drc_cmd.main([path, "--allow-incomplete", "--strict"]) == 2
