"""CLI tests for the phase-2 standard-derived creepage flags (Issue #4332).

Covers the new ``--standard`` / ``--pollution-degree`` / ``--working-voltage``
/ ``--material-group`` flags: derived-requirement census output (no ``--min``),
both-flags precedence, structured JSON provenance, loud errors for
over-range / undocumented lookups, the "provide --standard or --min" guard,
and phase-1 backward compatibility (``--min``-only output unchanged).
"""

from __future__ import annotations

import json

import pytest

from kicad_tools._shapely import has_shapely
from kicad_tools.cli.commands.creepage import run_creepage_command
from kicad_tools.cli.parser import create_parser

from .fixtures import board_source

pytestmark = pytest.mark.skipif(not has_shapely(), reason="creepage requires shapely")


def _write(tmp_path, source, name="board.kicad_pcb"):
    p = tmp_path / name
    p.write_text(source)
    return p


def _hv_map_file(tmp_path):
    p = tmp_path / "net_class_map.json"
    p.write_text(json.dumps({"L_MAINS": {"name": "HV"}}))
    return p


def _run(argv):
    args = create_parser().parse_args(argv)
    return run_creepage_command(args)


# ---------------------------------------------------------------------------
# Derived-requirement census (no --min)
# ---------------------------------------------------------------------------


def test_standard_derives_requirement_without_min(tmp_path, capsys):
    pcb = _write(tmp_path, board_source(with_slot=False))
    ncm = _hv_map_file(tmp_path)
    rc = _run(
        [
            "creepage",
            str(pcb),
            "--net-class-map",
            str(ncm),
            "--standard",
            "iec62368",
            "--working-voltage",
            "170",
            "--pollution-degree",
            "2",
        ]
    )
    out = capsys.readouterr().out
    assert rc == 0  # 18 mm gaps clear the ~2 mm derived requirement
    assert "IEC 62368-1" in out
    assert "Derived required creepage" in out
    # The 170 V RMS step-up lands on the 200 V row -> 2.0 mm (in EE envelope).
    assert "2.000 mm" in out


def test_standard_json_carries_provenance(tmp_path, capsys):
    pcb = _write(tmp_path, board_source(with_slot=False))
    ncm = _hv_map_file(tmp_path)
    rc = _run(
        [
            "creepage",
            str(pcb),
            "--net-class-map",
            str(ncm),
            "--standard",
            "iec60664",
            "--working-voltage",
            "170",
            "--pollution-degree",
            "2",
            "--material-group",
            "IIIa",
            "--format",
            "json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["standard"] == "iec60664"
    assert payload["pollution_degree"] == 2
    assert payload["material_group"] == "IIIa"
    assert payload["required_creepage_mm"] == 2.0
    assert payload["required_clearance_mm"] == 0.2  # PD2 floor
    cp = payload["creepage_provenance"]
    assert cp["table_id"] == "Table F.4"
    assert cp["voltage_row_used_v"] == 200.0
    assert "disclaimer" in cp
    # Every pair carries the derived requirement + governing bound.
    for pair in payload["pairs"]:
        assert pair["required_creepage_mm"] == 2.0
        assert pair["governing_bound"] == "derived"
        assert pair["required_clearance_mm"] == 0.2
        assert "clearance_pass" in pair
        assert "provenance" in pair


def test_standard_fail_when_requirement_not_met(tmp_path, capsys):
    pcb = _write(tmp_path, board_source(with_slot=False))
    ncm = _hv_map_file(tmp_path)
    # 1000 V PD3 IIIa -> 16 mm required creepage; the 18 mm pad gap clears it
    # but the board-edge pair (~11 mm) does not.
    rc = _run(
        [
            "creepage",
            str(pcb),
            "--net-class-map",
            str(ncm),
            "--standard",
            "iec60664",
            "--working-voltage",
            "1000",
            "--pollution-degree",
            "3",
        ]
    )
    out = capsys.readouterr().out
    assert rc == 1
    assert "FAIL" in out


# ---------------------------------------------------------------------------
# Precedence: both --min and --standard supplied
# ---------------------------------------------------------------------------


def test_precedence_min_stricter_than_derived(tmp_path, capsys):
    pcb = _write(tmp_path, board_source(with_slot=False))
    ncm = _hv_map_file(tmp_path)
    rc = _run(
        [
            "creepage",
            str(pcb),
            "--net-class-map",
            str(ncm),
            "--standard",
            "iec62368",
            "--working-voltage",
            "170",
            "--pollution-degree",
            "2",
            "--min",
            "100",  # far stricter than the 2 mm derived value
            "--format",
            "json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert rc == 1  # 100 mm cannot be met by an 18 mm gap
    for pair in payload["pairs"]:
        assert pair["governing_bound"] == "manual (--min)"


def test_precedence_derived_stricter_than_min(tmp_path, capsys):
    pcb = _write(tmp_path, board_source(with_slot=False))
    ncm = _hv_map_file(tmp_path)
    rc = _run(
        [
            "creepage",
            str(pcb),
            "--net-class-map",
            str(ncm),
            "--standard",
            "iec62368",
            "--working-voltage",
            "170",
            "--pollution-degree",
            "2",
            "--min",
            "0.5",  # weaker than the 2 mm derived value
            "--format",
            "json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    for pair in payload["pairs"]:
        assert pair["governing_bound"] == "derived"


# ---------------------------------------------------------------------------
# Loud errors
# ---------------------------------------------------------------------------


def test_neither_standard_nor_min_errors(tmp_path, capsys):
    pcb = _write(tmp_path, board_source(with_slot=False))
    ncm = _hv_map_file(tmp_path)
    rc = _run(["creepage", str(pcb), "--net-class-map", str(ncm)])
    err = capsys.readouterr().err
    assert rc == 1
    assert "provide either --standard" in err


def test_standard_without_voltage_errors(tmp_path, capsys):
    pcb = _write(tmp_path, board_source(with_slot=False))
    ncm = _hv_map_file(tmp_path)
    rc = _run(["creepage", str(pcb), "--net-class-map", str(ncm), "--standard", "iec60664"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "requires both --working-voltage and --pollution-degree" in err


def test_over_range_voltage_fails_loud(tmp_path, capsys):
    pcb = _write(tmp_path, board_source(with_slot=False))
    ncm = _hv_map_file(tmp_path)
    rc = _run(
        [
            "creepage",
            str(pcb),
            "--net-class-map",
            str(ncm),
            "--standard",
            "iec60664",
            "--working-voltage",
            "5000",
            "--pollution-degree",
            "2",
        ]
    )
    err = capsys.readouterr().err
    assert rc == 1
    assert "standard-table lookup failed" in err
    assert "exceeds the highest tabulated row" in err


def test_undocumented_combo_fails_loud(tmp_path, capsys):
    pcb = _write(tmp_path, board_source(with_slot=False))
    ncm = _hv_map_file(tmp_path)
    rc = _run(
        [
            "creepage",
            str(pcb),
            "--net-class-map",
            str(ncm),
            "--standard",
            "iec60664",
            "--working-voltage",
            "200",
            "--pollution-degree",
            "3",
            "--material-group",
            "IIIb",
        ]
    )
    err = capsys.readouterr().err
    assert rc == 1
    assert "standard-table lookup failed" in err


# ---------------------------------------------------------------------------
# Backward compatibility: --min-only output unchanged (phase-1 schema)
# ---------------------------------------------------------------------------


def test_min_only_json_schema_unchanged(tmp_path, capsys):
    pcb = _write(tmp_path, board_source(with_slot=False))
    ncm = _hv_map_file(tmp_path)
    _run(["creepage", str(pcb), "--net-class-map", str(ncm), "--min", "1.5", "--format", "json"])
    payload = json.loads(capsys.readouterr().out)
    # Exactly the phase-1 top-level keys -- no phase-2 fields leak in.
    assert set(payload.keys()) == {
        "board",
        "net_class",
        "min_mm",
        "hv_nets",
        "pair_count",
        "pairs",
        "passed",
    }
    for pair in payload["pairs"]:
        assert set(pair.keys()) == {
            "net_a",
            "net_b",
            "kind",
            "layer",
            "clearance_mm",
            "creepage_mm",
            "margin_mm",
            "pass",
        }
