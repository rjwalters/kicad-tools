"""Tests for ``kct repair-creepage`` (Issue #8545)."""

from __future__ import annotations

import json

import pytest

from kicad_tools._shapely import has_shapely
from kicad_tools.cli.parser import create_parser
from kicad_tools.cli.repair_creepage_cmd import build_census_fn, run_repair_creepage_command
from kicad_tools.creepage.repair import CreepageRepairer
from kicad_tools.schema.pcb import PCB

from .fixtures import _HEADER, _OUTLINE, _footprint

pytestmark = pytest.mark.skipif(not has_shapely(), reason="creepage requires shapely")

SEG_UUID = "11111111-1111-1111-1111-111111111111"
VIA_UUID = "22222222-2222-2222-2222-222222222222"


def _board(*, sig_pad: bool = False, locked: bool = False, via: bool = False) -> str:
    """1.5 mm L_MAINS riser from the U1 pad toward the right, GND pad 1.25 mm below it.

    Track edge y=110.75, GND pad top y=112 -> 1.25 mm; with ``--min 1.4`` the
    shortfall is 0.15 mm.  ``sig_pad`` adds a SIG pad 1.42 mm above the track
    (passing, but a +y... move away from GND would push the track into it).
    """
    header = _HEADER.replace('(net 2 "GND")', '(net 2 "GND")\n  (net 3 "SIG")')
    parts = [header, _OUTLINE, _footprint("U1", 110, 110, 1, "L_MAINS")]
    parts.append(_footprint("U2", 113, 113, 2, "GND"))
    if sig_pad:
        parts.append(_footprint("U3", 113, 106.83, 3, "SIG"))
    if via:
        parts.append(
            f'  (via (at 118 110.9) (size 1.0) (drill 0.4) (layers "F.Cu" "B.Cu") '
            f'(net 1) (uuid "{VIA_UUID}"))\n'
        )
        parts.append(_footprint("U4", 118, 113, 2, "GND"))
    else:
        lock = " (locked yes)" if locked else ""
        parts.append(
            f'  (segment (start 110 110) (end 116 110) (width 1.5) (layer "F.Cu") (net 1)'
            f'{lock} (uuid "{SEG_UUID}"))\n'
        )
    parts.append(")\n")
    return "".join(parts)


def _setup(tmp_path, source, extra=()):
    pcb = tmp_path / "board.kicad_pcb"
    pcb.write_text(source)
    ncm = tmp_path / "ncm.json"
    ncm.write_text(json.dumps({"L_MAINS": {"name": "HV"}}))
    argv = [
        "repair-creepage",
        str(pcb),
        "--net-class-map",
        str(ncm),
        "--min",
        "1.4",
        "--format",
        "json",
        *extra,
    ]
    return pcb, argv


def _run(argv, drc_fn=None):
    return run_repair_creepage_command(create_parser().parse_args(argv), drc_fn=drc_fn)


def _census(pcb_path, argv):
    args = create_parser().parse_args(argv)
    return build_census_fn(args)(PCB.load(pcb_path))


def _seg_attrs(pcb_path):
    pcb = PCB.load(pcb_path)
    return [(s.start, s.end, s.width) for s in pcb.segments]


def test_wide_trace_shortfall_is_repaired_keeping_width(tmp_path, capsys):
    pcb, argv = _setup(tmp_path, _board())
    assert not _census(pcb, argv).passed
    rc = _run(argv)
    out = json.loads(capsys.readouterr().out)
    assert rc == 0, out
    assert out["failing_before"] == 1 and out["failing_after"] == 0
    assert len(out["moves"]) == 1
    move = out["moves"][0]
    assert move["item_type"] == "segment" and move["distance_mm"] < 0.2 + 0.05
    assert _census(pcb, argv).passed
    ((start, end, width),) = _seg_attrs(pcb)
    assert width == 1.5
    # PCB coordinates are board-relative (origin 100,100).  The whole segment
    # translated up by < 0.25 mm; the start joint is still inside the U1 pad
    # (y 9..11) and x is unchanged.
    assert start[1] == end[1] and 9.0 < start[1] < 10.0 and 10.0 - start[1] < 0.25
    assert (start[0], end[0]) == (10.0, 16.0)


def test_move_that_worsens_another_pair_is_rolled_back(tmp_path, capsys):
    source = _board(sig_pad=True)
    pcb, argv = _setup(tmp_path, source)
    rc = _run(argv)
    out = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert out["moves"] == []
    assert any("SIG" in r["reason"] and "worsen" in r["reason"] for r in out["rejected"])
    assert len(out["unrepaired"]) == 1
    assert pcb.read_text() == source  # rolled back, nothing written


def test_drc_regression_rolls_back(tmp_path, capsys):
    source = _board()
    pcb, argv = _setup(tmp_path, source)
    counts = iter([0, 3])
    rc = _run(argv, drc_fn=lambda _text: next(counts))
    out = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert any("DRC regression" in r["reason"] for r in out["rejected"])
    assert pcb.read_text() == source


def test_drc_unavailable_is_reported_not_silent(tmp_path, capsys):
    _pcb, argv = _setup(tmp_path, _board())
    rc = _run(argv, drc_fn=lambda _text: None)
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert any("DRC regression check NOT run" in n for n in out["notes"])


def test_dry_run_lists_witness_and_writes_nothing(tmp_path, capsys):
    source = _board()
    pcb, argv = _setup(tmp_path, source, ["--dry-run"])
    out_path = tmp_path / "out.kicad_pcb"
    rc = _run([*argv, "-o", str(out_path)])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["dry_run"] is True
    w = out["moves"][0]["witness"]
    assert len(w["point_a_mm"]) == 2 and len(w["point_b_mm"]) == 2
    assert pcb.read_text() == source
    assert not out_path.exists()


def test_dry_run_text_output_shows_witness(tmp_path, capsys):
    _pcb, argv = _setup(tmp_path, _board(), ["--dry-run"])
    argv[argv.index("--format") + 1] = "text"
    assert _run(argv) == 0
    assert "witness A(" in capsys.readouterr().out


def test_locked_track_is_never_moved(tmp_path, capsys):
    source = _board(locked=True)
    pcb, argv = _setup(tmp_path, source)
    rc = _run(argv)
    out = json.loads(capsys.readouterr().out)
    assert rc == 1 and out["moves"] == []
    assert "locked" in out["unrepaired"][0]["reason"]
    assert pcb.read_text() == source


def test_shortfall_beyond_max_displacement_exits_nonzero(tmp_path, capsys):
    _pcb, argv = _setup(tmp_path, _board(), ["--max-displacement", "0.05"])
    rc = _run(argv)
    out = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert "max-displacement" in out["unrepaired"][0]["reason"]


def test_via_only_repair(tmp_path, capsys):
    pcb, argv = _setup(tmp_path, _board(via=True), ["--min", "1.0"])
    # later --min wins in argparse
    assert not _census(pcb, argv).passed
    rc = _run(argv)
    out = json.loads(capsys.readouterr().out)
    assert rc == 0, out
    assert out["moves"][0]["item_type"] == "via"
    assert _census(pcb, argv).passed


def test_waived_pairs_are_not_moved(tmp_path):
    pcb, argv = _setup(tmp_path, _board())
    args = create_parser().parse_args(argv)
    base = build_census_fn(args)

    def waive_all(p):
        report = base(p)
        for pair in report.pairs:
            pair.waived = True
        return report

    result = CreepageRepairer(pcb).repair(waive_all)
    assert result.failing_before == 0 and result.moves == []


def test_from_report_selects_pairs(tmp_path, capsys):
    pcb, argv = _setup(tmp_path, _board())
    report = tmp_path / "creepage.json"
    report.write_text(
        json.dumps(
            {"pairs": [{"net_a": "L_MAINS", "net_b": "GND", "kind": "conductor", "pass": False}]}
        )
    )
    rc = _run([*argv, "--from-report", str(report)])
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["failing_after"] == 0
    assert _census(pcb, argv).passed


def test_missing_threshold_is_usage_error(tmp_path, capsys):
    pcb = tmp_path / "b.kicad_pcb"
    pcb.write_text(_board())
    assert _run(["repair-creepage", str(pcb)]) == 2
