"""CLI tests for ``kct creepage`` (Issue #4327, phase 1 MVP).

Covers exit codes (0 when all pairs clear ``--min``, non-zero when one is
below), the ``--format json`` census schema (all pairs present, per-pair pass
flag, distinct numeric clearance/creepage/margin), and the "no HV nets found"
path (clean exit 0, no crash).
"""

from __future__ import annotations

import json

import pytest

from kicad_tools._shapely import has_shapely
from kicad_tools.cli.commands.creepage import run_creepage_command
from kicad_tools.cli.parser import create_parser

from .fixtures import (
    board_benign_suspect_names_source,
    board_mains_named_source,
    board_no_hv_source,
    board_source,
)

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


def test_exit_zero_when_all_pairs_clear(tmp_path, capsys):
    pcb = _write(tmp_path, board_source(with_slot=False))
    ncm = _hv_map_file(tmp_path)
    rc = _run(["creepage", str(pcb), "--net-class-map", str(ncm), "--min", "1.5"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "PASS" in out
    assert "L_MAINS" in out
    # Distinction is explicit in the human output.
    assert "Clearance" in out and "Creepage" in out


def test_exit_nonzero_when_a_pair_below_min(tmp_path, capsys):
    pcb = _write(tmp_path, board_source(with_slot=False))
    ncm = _hv_map_file(tmp_path)
    # Require 100 mm creepage -- the 18 mm pad gap cannot satisfy it.
    rc = _run(["creepage", str(pcb), "--net-class-map", str(ncm), "--min", "100"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "FAIL" in out


def test_json_census_schema(tmp_path, capsys):
    pcb = _write(tmp_path, board_source(with_slot=True))
    ncm = _hv_map_file(tmp_path)
    rc = _run(
        ["creepage", str(pcb), "--net-class-map", str(ncm), "--min", "1.5", "--format", "json"]
    )
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0

    assert payload["net_class"] == "HV"
    assert payload["min_mm"] == 1.5
    assert payload["hv_nets"] == ["L_MAINS"]
    assert payload["pair_count"] == len(payload["pairs"])
    assert payload["pair_count"] >= 2  # HV-vs-GND and HV-vs-edge
    assert payload["passed"] is True

    kinds = {p["kind"] for p in payload["pairs"]}
    assert {"conductor", "edge"} <= kinds

    for pair in payload["pairs"]:
        # Full census: every pair carries numeric fields + a pass flag.
        assert isinstance(pair["clearance_mm"], (int, float))
        assert isinstance(pair["creepage_mm"], (int, float))
        assert isinstance(pair["margin_mm"], (int, float))
        assert isinstance(pair["pass"], bool)
        assert pair["creepage_mm"] >= pair["clearance_mm"] - 1e-6

    gnd = next(p for p in payload["pairs"] if p["kind"] == "conductor" and p["net_b"] == "GND")
    # The slot makes clearance and creepage genuinely different values.
    assert gnd["creepage_mm"] > gnd["clearance_mm"] + 2.0


def test_json_census_no_slot_equals(tmp_path, capsys):
    pcb = _write(tmp_path, board_source(with_slot=False))
    ncm = _hv_map_file(tmp_path)
    _run(["creepage", str(pcb), "--net-class-map", str(ncm), "--min", "1.5", "--format", "json"])
    payload = json.loads(capsys.readouterr().out)
    gnd = next(p for p in payload["pairs"] if p["kind"] == "conductor" and p["net_b"] == "GND")
    assert gnd["creepage_mm"] == pytest.approx(gnd["clearance_mm"], abs=1e-4)


def test_no_hv_nets_message_and_exit_zero(tmp_path, capsys):
    pcb = _write(tmp_path, board_no_hv_source())
    ncm = _hv_map_file(tmp_path)
    rc = _run(["creepage", str(pcb), "--net-class-map", str(ncm), "--min", "1.5"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "No 'HV' nets found" in out


def test_no_hv_nets_json_empty_census(tmp_path, capsys):
    pcb = _write(tmp_path, board_no_hv_source())
    ncm = _hv_map_file(tmp_path)
    rc = _run(
        ["creepage", str(pcb), "--net-class-map", str(ncm), "--min", "1.5", "--format", "json"]
    )
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["pairs"] == []
    assert payload["pair_count"] == 0
    assert payload["passed"] is True


# ---------------------------------------------------------------------------
# #4354 vacuity guard: a mains-looking board must never silently exit 0.
# ---------------------------------------------------------------------------


def test_mains_named_board_no_map_exits_nonzero(tmp_path, capsys):
    # Realistic invocation #1 from the report: NO net-class-map.  The broadened
    # HV name fallback classifies AC_LINE/AC_NEUTRAL/FUSED_LINE, the ~1 mm
    # AC_LINE<->GND gap fails the 250 V IIIa/PD2 requirement -> exit 1 (a real
    # measured failure), NOT a silent exit 0.
    pcb = _write(tmp_path, board_mains_named_source())
    rc = _run(
        [
            "creepage",
            str(pcb),
            "--standard",
            "iec60664",
            "--working-voltage",
            "250",
            "--pollution-degree",
            "2",
            "--material-group",
            "IIIa",
        ]
    )
    captured = capsys.readouterr()
    assert rc != 0
    assert "Nothing to audit -- exit 0" not in captured.out
    # The mains nets were actually classified and audited.
    assert "AC_LINE" in captured.out


def test_generic_map_hiding_mains_fires_vacuity_guard(tmp_path, capsys):
    # Realistic invocation #2 from the report: a GENERIC net-class-map that
    # names Power/Digital but never HV.  The mains nets are mapped away from HV,
    # so 0 HV nets resolve -- but the board is obviously mains, so the vacuity
    # guard fires with the distinct exit code 2 (NOT a silent 0).
    pcb = _write(tmp_path, board_mains_named_source())
    gmap = tmp_path / "generic_map.json"
    gmap.write_text(
        json.dumps(
            {
                "AC_LINE": {"name": "Power"},
                "AC_NEUTRAL": {"name": "Power"},
                "FUSED_LINE": {"name": "Power"},
            }
        )
    )
    rc = _run(
        [
            "creepage",
            str(pcb),
            "--net-class-map",
            str(gmap),
            "--standard",
            "iec60664",
            "--working-voltage",
            "250",
            "--pollution-degree",
            "2",
        ]
    )
    captured = capsys.readouterr()
    assert rc == 2  # distinct "HV unclassified" code, not 0 and not 1
    assert "Nothing to audit -- exit 0" not in captured.out
    assert "WARNING" in captured.err
    assert "AC_LINE" in captured.err
    assert "--net-class-map" in captured.err


def test_high_working_voltage_without_mains_names_fires_guard(tmp_path, capsys):
    # The repro board's real HV nets (TRK_POS/V_RSV_POS) do NOT match mains
    # name patterns.  A mains-level --working-voltage with 0 resolved HV nets
    # must still fire the guard (exit 2), never silent 0.
    pcb = _write(tmp_path, board_no_hv_source())
    rc = _run(
        [
            "creepage",
            str(pcb),
            "--standard",
            "iec60664",
            "--working-voltage",
            "250",
            "--pollution-degree",
            "2",
        ]
    )
    captured = capsys.readouterr()
    assert rc == 2
    assert "WARNING" in captured.err
    assert "250" in captured.err
    assert "Nothing to audit -- exit 0" not in captured.out


def test_low_voltage_non_hv_board_still_exits_zero(tmp_path, capsys):
    # Negative control: a genuinely low-voltage board (no mains names, working
    # voltage below the 50 V SELV boundary) must still exit 0 with the inert
    # "Nothing to audit" message -- no false alarms.
    pcb = _write(tmp_path, board_no_hv_source())
    rc = _run(
        [
            "creepage",
            str(pcb),
            "--standard",
            "iec60664",
            "--working-voltage",
            "24",
            "--pollution-degree",
            "2",
        ]
    )
    captured = capsys.readouterr()
    assert rc == 0
    assert "Nothing to audit -- exit 0" in captured.out
    assert "WARNING" not in captured.err


def test_benign_suspect_named_board_exits_zero(tmp_path, capsys):
    # Issue #4365: a board whose only suspect-shaped nets are benign
    # (SPI_LINE / HOT_SWAP / PRIMARY_CLK) with no mains-level working voltage
    # must NOT trip the #4354 vacuity guard -- the tightened MAINS_NAME_RE no
    # longer flags bare LINE / HOT / PRIMARY tokens, so this exits 0.
    pcb = _write(tmp_path, board_benign_suspect_names_source())
    rc = _run(
        [
            "creepage",
            str(pcb),
            "--standard",
            "iec60664",
            "--working-voltage",
            "24",
            "--pollution-degree",
            "2",
        ]
    )
    captured = capsys.readouterr()
    assert rc == 0
    assert "Nothing to audit -- exit 0" in captured.out
    assert "WARNING" not in captured.err


def test_missing_pcb_file_errors(tmp_path, capsys):
    rc = _run(["creepage", str(tmp_path / "nope.kicad_pcb"), "--min", "1.5"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "not found" in err


def test_missing_net_class_map_errors(tmp_path, capsys):
    pcb = _write(tmp_path, board_source(with_slot=False))
    rc = _run(
        ["creepage", str(pcb), "--net-class-map", str(tmp_path / "absent.json"), "--min", "1.5"]
    )
    err = capsys.readouterr().err
    assert rc == 1
    assert "net-class-map file not found" in err
