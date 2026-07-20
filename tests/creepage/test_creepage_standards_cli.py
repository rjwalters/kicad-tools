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


# ---------------------------------------------------------------------------
# Per-net voltage map + pairwise |dV| requirement (Issue #4371)
# ---------------------------------------------------------------------------


def _vmap_file(tmp_path, data, name="voltage_map.json"):
    p = tmp_path / name
    p.write_text(json.dumps(data))
    return p


def _run_map(tmp_path, capsys, vmap_data, *, fmt="json", pd="2", extra=None):
    pcb = _write(tmp_path, board_source(with_slot=False))
    ncm = _hv_map_file(tmp_path)
    vmap = _vmap_file(tmp_path, vmap_data)
    argv = [
        "creepage",
        str(pcb),
        "--net-class-map",
        str(ncm),
        "--standard",
        "iec60664",
        "--pollution-degree",
        pd,
        "--voltage-map",
        str(vmap),
        "--format",
        fmt,
    ]
    if extra:
        argv.extend(extra)
    rc = run_creepage_command(create_parser().parse_args(argv))
    return rc, capsys.readouterr()


def test_voltage_map_same_potential_passes(tmp_path, capsys):
    # L_MAINS and GND both at 90 V -> that pair requires ~0 and PASSes.
    rc, cap = _run_map(tmp_path, capsys, {"L_MAINS": 90, "GND": 90})
    assert rc == 0
    payload = json.loads(cap.out)
    cond = next(p for p in payload["pairs"] if p["kind"] == "conductor")
    assert cond["required_creepage_mm"] == 0.0
    assert cond["pass"] is True


def test_voltage_map_cross_domain_uses_real_delta(tmp_path, capsys):
    # L_MAINS 150 V vs GND (unmapped -> 0 V): the ~1 mm gap FAILs the 150 V req.
    rc, cap = _run_map(tmp_path, capsys, {"L_MAINS": 150})
    payload = json.loads(cap.out)
    cond = next(p for p in payload["pairs"] if p["kind"] == "conductor")
    assert cond["required_creepage_mm"] > 1.0
    assert cond["provenance"]["voltage"]["delta_v_v"] == 150.0


def test_voltage_map_json_report_fields(tmp_path, capsys):
    rc, cap = _run_map(tmp_path, capsys, {"L_MAINS": 150, "_edge_voltage": 5})
    payload = json.loads(cap.out)
    # Report-level scalar requirement / working voltage are null in map mode.
    assert payload["working_voltage_v"] is None
    assert payload["required_creepage_mm"] is None
    assert payload["voltage_source"] == "per-pair |dV| (voltage-map)"
    assert payload["voltage_map"] == {"L_MAINS": 150.0}
    assert payload["edge_voltage_v"] == 5.0


def test_voltage_map_table_header_mentions_per_pair(tmp_path, capsys):
    rc, cap = _run_map(tmp_path, capsys, {"L_MAINS": 150}, fmt="table")
    assert "per-pair |dV| (voltage-map" in cap.out


def test_voltage_map_without_working_voltage_is_allowed(tmp_path, capsys):
    # --working-voltage is NOT required when --voltage-map is supplied.
    rc, cap = _run_map(tmp_path, capsys, {"L_MAINS": 90, "GND": 90})
    assert rc == 0
    assert "requires both --working-voltage" not in cap.err


def test_voltage_map_requires_standard(tmp_path, capsys):
    pcb = _write(tmp_path, board_source(with_slot=False))
    vmap = _vmap_file(tmp_path, {"L_MAINS": 150})
    rc = _run(["creepage", str(pcb), "--voltage-map", str(vmap), "--min", "1.5"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "--voltage-map requires --standard" in err


def test_voltage_map_requires_pollution_degree(tmp_path, capsys):
    pcb = _write(tmp_path, board_source(with_slot=False))
    vmap = _vmap_file(tmp_path, {"L_MAINS": 150})
    rc = _run(["creepage", str(pcb), "--standard", "iec60664", "--voltage-map", str(vmap)])
    err = capsys.readouterr().err
    assert rc == 1
    assert "requires --pollution-degree" in err


def test_voltage_map_missing_file_fails_loud(tmp_path, capsys):
    pcb = _write(tmp_path, board_source(with_slot=False))
    rc = _run(
        [
            "creepage",
            str(pcb),
            "--standard",
            "iec60664",
            "--pollution-degree",
            "2",
            "--voltage-map",
            str(tmp_path / "does_not_exist.json"),
        ]
    )
    err = capsys.readouterr().err
    assert rc == 1
    assert "voltage-map file not found" in err


def test_voltage_map_malformed_json_fails_loud(tmp_path, capsys):
    pcb = _write(tmp_path, board_source(with_slot=False))
    bad = tmp_path / "bad.json"
    bad.write_text("{not valid json")
    rc = _run(
        [
            "creepage",
            str(pcb),
            "--standard",
            "iec60664",
            "--pollution-degree",
            "2",
            "--voltage-map",
            str(bad),
        ]
    )
    err = capsys.readouterr().err
    assert rc == 1
    assert "parsing voltage-map JSON" in err


def test_voltage_map_non_numeric_value_fails_loud(tmp_path, capsys):
    pcb = _write(tmp_path, board_source(with_slot=False))
    vmap = _vmap_file(tmp_path, {"L_MAINS": "high"})
    rc = _run(
        [
            "creepage",
            str(pcb),
            "--standard",
            "iec60664",
            "--pollution-degree",
            "2",
            "--voltage-map",
            str(vmap),
        ]
    )
    err = capsys.readouterr().err
    assert rc == 1
    assert "invalid voltage-map structure" in err


def test_voltage_map_over_range_delta_fails_loud(tmp_path, capsys):
    # A per-pair |ΔV| beyond the highest tabulated row must fail loud (never
    # silently pass a safety-critical audit).
    rc, cap = _run_map(tmp_path, capsys, {"L_MAINS": 1_000_000})
    assert rc == 1
    assert "standard-table lookup failed" in cap.err


def test_single_voltage_json_does_not_leak_map_keys(tmp_path, capsys):
    # Backward compat: the single --working-voltage (no map) phase-2 JSON must
    # NOT gain any of the #4371 map-mode keys.
    pcb = _write(tmp_path, board_source(with_slot=False))
    ncm = _hv_map_file(tmp_path)
    _run(
        [
            "creepage",
            str(pcb),
            "--net-class-map",
            str(ncm),
            "--standard",
            "iec60664",
            "--working-voltage",
            "150",
            "--pollution-degree",
            "2",
            "--format",
            "json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["working_voltage_v"] == 150
    assert payload["required_creepage_mm"] is not None
    for leaked in ("voltage_source", "voltage_map", "edge_voltage_v"):
        assert leaked not in payload
