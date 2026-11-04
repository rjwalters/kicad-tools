"""``kct pcb transplant SRC DST --nets ...`` (issue #8311)."""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest

from kicad_tools.cli import main as kct_main
from kicad_tools.cli.pcb_transplant import run_pcb_transplant
from kicad_tools.pcb.diff import diff_pcbs
from kicad_tools.pcb.transplant import TransplantError, transplant_nets
from kicad_tools.schema.pcb import PCB

REPO = Path(__file__).resolve().parents[1]
BOARD = REPO / "boards/01-voltage-divider/output/voltage_divider_routed.kicad_pcb"


def _renumber(text: str) -> str:
    """Swap net numbers 1 (VIN) and 2 (VOUT) everywhere."""
    swap = {"1": "2", "2": "1"}
    return re.sub(r"\(net ([12])(?=[\s)])", lambda m: f"(net {swap[m.group(1)]}", text)


def _make(tmp_path: Path, *, src_edit=None) -> tuple[Path, Path]:
    dst = tmp_path / "dst.kicad_pcb"
    shutil.copyfile(BOARD, dst)
    text = _renumber(BOARD.read_text())  # SRC numbers nets differently
    if src_edit:
        text = src_edit(text)
    src = tmp_path / "src.kicad_pcb"
    src.write_text(text)
    return src, dst


def _widen_vin(text: str) -> str:
    """Widen the first VIN (after renumber: net 2) segment's width."""
    return re.sub(
        r"(\(segment\n(?:(?!\t\)).)*?)\(width 0\.5\)((?:(?!\t\)).)*?\(net 2\))",
        r"\1(width 0.75)\2",
        text,
        count=1,
        flags=re.S,
    )


def _changed_nets(a: PCB, b: PCB, kinds=("segment", "via", "arc")) -> set[str]:
    r = diff_pcbs(a, b)
    return {e.net for e in r.entries if e.status != "unchanged" and e.kind in kinds}


def test_transplant_swaps_only_named_net(tmp_path):
    src, dst = _make(tmp_path, src_edit=_widen_vin)
    assert src.read_text() != _renumber(BOARD.read_text())
    out = tmp_path / "out.kicad_pcb"
    before = dst.read_bytes()
    rc = run_pcb_transplant(str(src), str(dst), nets=["VIN"], output=str(out))
    assert rc == 0
    assert dst.read_bytes() == before  # DST untouched
    res, d = PCB.load(out), PCB.load(dst)
    assert _changed_nets(d, res) == {"VIN"}
    assert not [
        e
        for e in diff_pcbs(PCB.load(src), res).entries
        if e.net == "VIN" and e.kind in ("segment", "via") and e.status != "unchanged"
    ]
    # Net numbers were remapped to DST's numbering (VIN is net 1 there).
    nums = {s.net_number for s in res.segments if s.net_name == "VIN" or s.net_number == 1}
    assert nums == {1}


def test_other_content_byte_identical(tmp_path):
    src, dst = _make(tmp_path)
    out = tmp_path / "out.kicad_pcb"
    assert run_pcb_transplant(str(src), str(dst), nets=["VOUT"], output=str(out)) == 0
    # Same copper both sides => same board apart from ordering of the VOUT blocks.
    assert not _changed_nets(PCB.load(dst), PCB.load(out))


def test_missing_net_errors_and_writes_nothing(tmp_path, capsys):
    src, dst = _make(tmp_path)
    out = tmp_path / "out.kicad_pcb"
    assert run_pcb_transplant(str(src), str(dst), nets=["NOPE"], output=str(out)) == 1
    assert not out.exists()
    assert "NOPE" in capsys.readouterr().err


def test_moved_pad_refused(tmp_path, capsys):
    def move_pad(text: str) -> str:
        # Shift footprint R1 by 1 mm in SRC only.
        return re.sub(
            r'(\(footprint "[^"]*"\n(?:(?!\(at ).)*?\(at )(\S+) ',
            lambda m: f"{m.group(1)}{float(m.group(2)) + 1:g} ",
            text,
            count=1,
            flags=re.S,
        )

    src, dst = _make(tmp_path, src_edit=move_pad)
    out = tmp_path / "out.kicad_pcb"
    rc = run_pcb_transplant(str(src), str(dst), nets=["VIN", "VOUT", "GND"], output=str(out))
    assert rc == 1
    assert not out.exists()
    assert "pad positions differ" in capsys.readouterr().err


def test_dry_run_writes_nothing_and_reports_diff(tmp_path):
    src, dst = _make(tmp_path, src_edit=_widen_vin)
    out = tmp_path / "out.kicad_pcb"
    rc = run_pcb_transplant(str(src), str(dst), nets=["VIN"], output=str(out), dry_run=True)
    assert rc == 2
    assert not out.exists()


def test_dry_run_no_change_exit_0(tmp_path):
    src, dst = _make(tmp_path)
    assert run_pcb_transplant(str(src), str(dst), nets=["VIN"], output=None, dry_run=True) == 0


def test_output_required_and_not_dst(tmp_path):
    src, dst = _make(tmp_path)
    assert run_pcb_transplant(str(src), str(dst), nets=["VIN"], output=None) == 1
    assert run_pcb_transplant(str(src), str(dst), nets=["VIN"], output=str(dst)) == 1


def test_empty_src_copper_removes_dst_copper(tmp_path, capsys):
    src, dst = _make(tmp_path)
    s = PCB.load(src)
    s.remove_segments([x for x in s.segments if x.net_number == 2])  # VIN in SRC
    s.remove_vias([v for v in s.vias if v.net_number == 2])
    s.save(src)
    assert any(x.net_number == 1 for x in PCB.load(dst).segments)
    out = tmp_path / "out.kicad_pcb"
    assert run_pcb_transplant(str(src), str(dst), nets=["VIN"], output=str(out)) == 0
    res = PCB.load(out)
    assert not [x for x in res.segments if x.net_number == 1]
    assert "SRC has no copper" in capsys.readouterr().out


def test_library_rejects_unknown_net(tmp_path):
    src, dst = _make(tmp_path)
    with pytest.raises(TransplantError):
        transplant_nets(PCB.load(src), PCB.load(dst), ["NOPE"])


def test_drc_skipped_without_kicad_cli(tmp_path, monkeypatch, capsys):
    import kicad_tools.cli.runner as runner

    monkeypatch.setattr(runner, "find_kicad_cli", lambda: None)
    src, dst = _make(tmp_path)
    out = tmp_path / "out.kicad_pcb"
    assert run_pcb_transplant(str(src), str(dst), nets=["VIN"], output=str(out), drc=True) == 0
    assert "skipped" in capsys.readouterr().err


def test_cli_dispatch(tmp_path):
    src, dst = _make(tmp_path)
    out = tmp_path / "out.kicad_pcb"
    rc = kct_main(["pcb", "transplant", str(src), str(dst), "--nets", "VIN,VOUT", "-o", str(out)])
    assert rc == 0 and out.exists()


def test_format_json_single_document(tmp_path, capsys):
    import json

    src, dst = _make(tmp_path, src_edit=_widen_vin)
    out = tmp_path / "out.kicad_pcb"
    rc = run_pcb_transplant(str(src), str(dst), nets=["VIN"], output=str(out), fmt="json")
    assert rc == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["output"] == str(out)
    assert doc["nets"]["VIN"]["src_has_copper"] is True


def test_format_json_error_document(tmp_path, capsys):
    import json

    src, dst = _make(tmp_path)
    rc = run_pcb_transplant(str(src), str(dst), nets=["NOPE"], output=None, fmt="json")
    assert rc == 1
    assert "error" in json.loads(capsys.readouterr().out)


def test_format_json_dry_run(tmp_path, capsys):
    import json

    src, dst = _make(tmp_path, src_edit=_widen_vin)
    rc = run_pcb_transplant(str(src), str(dst), nets=["VIN"], output=None, dry_run=True, fmt="json")
    assert rc == 2
    doc = json.loads(capsys.readouterr().out)
    assert doc["dry_run"] is True and doc["diff"] is not None
