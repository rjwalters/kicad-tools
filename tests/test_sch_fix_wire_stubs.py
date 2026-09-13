"""Safety and round-trip regressions for wire-stub repair."""

import json
import stat
from pathlib import Path

import pytest

from kicad_tools.schema import Schematic


def design(tmp_path, *, start=(0, 0), end=(5.08, 0), pins=((7.62, 0),), extra=""):
    path = tmp_path / "board.kicad_sch"
    symbols = "".join(
        f"""(symbol (lib_id "Test:P") (at {x} {y} 0)
        (unit 1) (uuid "s{i}") (property "Reference" "U{i}"))"""
        for i, (x, y) in enumerate(pins)
    )
    path.write_text(f"""(kicad_sch (version 20250114) (generator "test")
      (uuid "root") (paper "A4")
      (lib_symbols (symbol "Test:P" (symbol "P_1_1"
        (pin passive line (at 0 0 0) (length 0)
          (name "P") (number "1")))))
      {symbols}
      (wire (pts (xy {start[0]} {start[1]}) (xy {end[0]} {end[1]}))
        (stroke (width 0.123456789) (type default)) (uuid "w1")
        (future "preserve me" 0.123456789)) {extra})""")
    return path


@pytest.mark.parametrize(
    "start,end,pin,index",
    [
        ((0, 0), (5.08, 0), (7.62, 0), 1),
        ((5.08, 0), (0, 0), (7.62, 0), 0),
        ((0, 0), (0, 5.08), (0, 7.62), 1),
        ((0, 5.08), (0, 0), (0, 7.62), 0),
    ],
)
def test_apply_and_idempotent(tmp_path, capsys, start, end, pin, index):
    from kicad_tools.cli.sch_fix_wire_stubs import main

    path = design(tmp_path, start=start, end=end, pins=(pin,))
    assert main([str(path), "--format", "json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert len(result["applied"]) == 1
    wire = Schematic.load(path).wires[0]
    assert (wire.start, wire.end)[index] == pin
    content = path.read_bytes()
    assert b"0.123456789" in content and b"preserve me" in content
    assert main([str(path), "--format", "json"]) == 0
    assert json.loads(capsys.readouterr().out)["applied"] == []
    assert path.read_bytes() == content


@pytest.mark.parametrize("fmt", ["text", "json"])
def test_dry_run(tmp_path, capsys, fmt):
    from kicad_tools.cli.sch_fix_wire_stubs import main

    path = design(tmp_path)
    before = path.read_bytes()
    assert main([str(path), "--dry-run", "--format", fmt]) == 0
    output = capsys.readouterr().out
    assert "planned" in output.lower()
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "pins",
    [
        ((7.7, 0),),
        ((2.54, 0),),
        ((5.08, 2.54),),
        ((7.62, 0), (5.08, 2.54)),
        ((5.08, 2.54), (7.62, 0)),
    ],
)
def test_unsafe_and_non_grid_unchanged(tmp_path, pins):
    from kicad_tools.cli.sch_fix_wire_stubs import main

    path = design(tmp_path, pins=pins)
    before = path.read_bytes()
    assert main([str(path)]) == 0
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "extra",
    [
        '(junction (at 6.35 0) (diameter 0) (color 0 0 0 0) (uuid "j"))',
        '(label "N" (at 6.35 0 0) (uuid "l"))',
        '(no_connect (at 6.35 0) (uuid "n"))',
        '(wire (pts (xy 6.35 -1) (xy 6.35 1)) (uuid "cross"))',
        '(wire (pts (xy 7.62 -1) (xy 7.62 1)) (uuid "target"))',
        '(sheet (at 0 0) (size 1 1) (uuid "s") (property "Sheetname" "c") '
        '(property "Sheetfile" "child.kicad_sch") (pin "N" input (at 6.35 0 0)))',
    ],
)
def test_intervening_anchors(tmp_path, extra):
    from kicad_tools.cli.sch_fix_wire_stubs import main

    child = tmp_path / "child.kicad_sch"
    child.write_text('(kicad_sch (version 20250114) (uuid "child"))')
    path = design(tmp_path, extra=extra)
    before = path.read_bytes()
    assert main([str(path)]) == 0
    assert path.read_bytes() == before


def test_intervening_subgrid_pin(tmp_path):
    from kicad_tools.cli.sch_fix_wire_stubs import main

    path = design(tmp_path, pins=((7.62, 0), (6.35, 0)))
    before = path.read_bytes()
    assert main([str(path)]) == 0
    assert path.read_bytes() == before


@pytest.mark.parametrize("replacement", ["", '(uuid "w1") (uuid "w1")'])
def test_missing_duplicate_identity(tmp_path, replacement):
    from kicad_tools.cli.sch_fix_wire_stubs import main

    path = design(tmp_path)
    path.write_text(path.read_text().replace('(uuid "w1")', replacement))
    before = path.read_bytes()
    assert main([str(path)]) == 0
    assert path.read_bytes() == before


def test_duplicate_wire_uuid(tmp_path):
    from kicad_tools.cli.sch_fix_wire_stubs import main

    path = design(tmp_path, extra='(wire (pts (xy 100 100) (xy 110 100)) (uuid "w1"))')
    before = path.read_bytes()
    assert main([str(path)]) == 0
    assert path.read_bytes() == before


def test_stale_endpoint(tmp_path, monkeypatch):
    from dataclasses import replace

    from kicad_tools.cli import sch_fix_wire_stubs as fix

    real = fix.find_wire_stubs
    monkeypatch.setattr(
        fix, "find_wire_stubs", lambda *a: [replace(f, wire_start=(99, 99)) for f in real(*a)]
    )
    path = design(tmp_path)
    before = path.read_bytes()
    assert fix.main([str(path)]) == 0
    assert path.read_bytes() == before


def sheets_extra(filename="child.kicad_sch"):
    return "".join(
        f'''(sheet (uuid "sheet{i}") (property "Sheetname" "child{i}")
        (property "Sheetfile" "{filename}"))'''
        for i in (1, 2)
    )


def test_hierarchy_deduplicates_and_uses_local_libraries(tmp_path, capsys):
    from kicad_tools.cli.sch_fix_wire_stubs import main

    path = design(tmp_path)
    child = tmp_path / "child.kicad_sch"
    # Same lib ID, different embedded geometry: child's pin is at 10.16.
    child.write_text(path.read_text().replace("(at 0 0 0) (length 0)", "(at 2.54 0 0) (length 0)"))
    path = design(tmp_path, extra=sheets_extra())
    assert main([str(path), "--format", "json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert len(result["applied"]) == 2
    assert Schematic.load(path).wires[0].end == (7.62, 0)
    assert Schematic.load(child).wires[0].end == (10.16, 0)


@pytest.mark.parametrize("missing", ["sheet", "library"])
def test_hierarchy_error_before_any_write(tmp_path, capsys, missing):
    from kicad_tools.cli.sch_fix_wire_stubs import main

    path = design(tmp_path, extra=sheets_extra())
    if missing == "library":
        child = tmp_path / "child.kicad_sch"
        child.write_text(
            '(kicad_sch (symbol (lib_id "missing") (at 0 0 0) (property "Reference" "U1")))'
        )
    before = path.read_bytes()
    assert main([str(path), "--format", "json"]) == 1
    assert json.loads(capsys.readouterr().out)["success"] is False
    assert path.read_bytes() == before


def test_competing_plans(tmp_path, capsys):
    from kicad_tools.cli.sch_fix_wire_stubs import main

    path = design(tmp_path, extra='(wire (pts (xy 7.62 7.62) (xy 7.62 2.54)) (uuid "w2"))')
    before = path.read_bytes()
    assert main([str(path), "--format", "json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["applied"] == []
    assert (
        sum(a["reason"] == "incompatible simultaneous extensions" for a in result["skipped"]) == 2
    )
    assert path.read_bytes() == before


def test_multiple_independent_repairs(tmp_path, capsys):
    from kicad_tools.cli.sch_fix_wire_stubs import main

    path = design(
        tmp_path,
        pins=((7.62, 0), (7.62, 20)),
        extra='(wire (pts (xy 0 20) (xy 5.08 20)) (uuid "w2"))',
    )
    assert main([str(path), "--format", "json"]) == 0
    assert len(json.loads(capsys.readouterr().out)["applied"]) == 2


def test_concurrent_edit_aborts_before_writes(tmp_path, monkeypatch, capsys):
    from kicad_tools.cli import sch_fix_wire_stubs as fix

    path = design(tmp_path)
    external = path.read_bytes() + b"\n"
    real = fix.find_wire_stubs

    def concurrent(*args):
        result = real(*args)
        path.write_bytes(external)
        return result

    monkeypatch.setattr(fix, "find_wire_stubs", concurrent)
    assert fix.main([str(path), "--format", "json"]) == 1
    result = json.loads(capsys.readouterr().out)
    assert "changed since planning" in result["error"]
    assert result["applied"] == []
    assert path.read_bytes() == external
    assert not list(tmp_path.glob(".board*"))


@pytest.mark.parametrize("phase", ["staging", "replacement"])
def test_second_write_failure_rolls_back(tmp_path, monkeypatch, capsys, phase):
    from kicad_tools.cli import sch_fix_wire_stubs as fix

    path = design(tmp_path)
    child = tmp_path / "child.kicad_sch"
    child.write_bytes(path.read_bytes())
    path = design(tmp_path, extra=sheets_extra())
    originals = {p: p.read_bytes() for p in (path, child)}
    real = fix.os.replace
    calls = 0

    def fail_second(*args):
        nonlocal calls
        is_original = Path(args[1]) in originals
        if is_original == (phase == "replacement"):
            calls += 1
            if calls == 2:
                if phase == "replacement":
                    assert any(p.read_bytes() != data for p, data in originals.items())
                raise OSError("write failed")
        return real(*args)

    monkeypatch.setattr(fix.os, "replace", fail_second)
    assert fix.main([str(path), "--format", "json"]) == 1
    result = json.loads(capsys.readouterr().out)
    assert "write failed" in result["error"]
    assert calls >= 2
    assert result["applied"] == []
    assert all(p.read_bytes() == data for p, data in originals.items())
    assert not list(tmp_path.glob(".*.kicad_sch.*"))


def test_parser_dispatch(tmp_path, capsys):
    from kicad_tools.cli.commands.schematic import run_sch_command
    from kicad_tools.cli.parser import create_parser

    path = design(tmp_path)
    args = create_parser().parse_args(
        ["sch", "fix-wire-stubs", str(path), "--dry-run", "--format", "json"]
    )
    assert run_sch_command(args) == 0
    assert json.loads(capsys.readouterr().out)["dry_run"] is True


def test_dnp_pin_still_blocks_extension(tmp_path):
    from kicad_tools.cli.sch_fix_wire_stubs import main

    path = design(tmp_path, pins=((7.62, 0), (6.35, 0)))
    path.write_text(path.read_text().replace('(uuid "s1")', '(dnp yes) (uuid "s1")'))
    before = path.read_bytes()
    assert main([str(path)]) == 0
    assert path.read_bytes() == before


@pytest.mark.parametrize("missing", [False, True])
def test_inherited_library(tmp_path, missing):
    from kicad_tools.cli.sch_fix_wire_stubs import main

    path = design(tmp_path)
    text = path.read_text().replace('(symbol "Test:P" (symbol', '(symbol "Test:Base" (symbol')
    text = text.replace("(lib_symbols ", '(lib_symbols (symbol "Test:P" (extends "Base")) ')
    if missing:
        text = text.replace('(extends "Base")', '(extends "Absent")')
    path.write_text(text)
    before = path.read_bytes()
    assert main([str(path)]) == int(missing)
    if missing:
        assert path.read_bytes() == before
    else:
        assert Schematic.load(path).wires[0].end == (7.62, 0)


def test_unsnapped_target_is_required(tmp_path, capsys):
    from kicad_tools.cli.sch_fix_wire_stubs import main

    path = design(tmp_path)
    path.write_text(path.read_text().replace("(at 0 0 0) (length 0)", "(at 0.1 0 0) (length 0)"))
    before = path.read_bytes()
    assert main([str(path), "--format", "json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["applied"] == []
    assert path.read_bytes() == before


def test_unplaced_unit_cannot_be_targeted(tmp_path):
    from kicad_tools.cli.sch_fix_wire_stubs import main

    path = design(tmp_path)
    path.write_text(path.read_text().replace('"P_1_1"', '"P_2_1"'))
    before = path.read_bytes()
    assert main([str(path)]) == 1
    assert path.read_bytes() == before


def test_high_precision_target_survives_serialization(tmp_path):
    from kicad_tools.cli.sch_fix_wire_stubs import main

    target = 100.123456789
    path = design(tmp_path, start=(target - 10.16, 0), end=(target - 2.54, 0), pins=((target, 0),))
    assert main([str(path)]) == 0
    assert Schematic.load(path).wires[0].end == (target, 0)


@pytest.mark.parametrize("unit_name", ["P_0_1", "P_1_2"])
def test_unsupported_library_geometry_fails_before_write(tmp_path, unit_name):
    from kicad_tools.cli.sch_fix_wire_stubs import main

    path = design(tmp_path)
    path.write_text(path.read_text().replace("P_1_1", unit_name))
    before = path.read_bytes()
    assert main([str(path)]) == 1
    assert path.read_bytes() == before


def test_duplicate_pin_numbers_do_not_hide_intervening_pin(tmp_path):
    from kicad_tools.cli.sch_fix_wire_stubs import main

    path = design(tmp_path)
    path.write_text(
        path.read_text().replace(
            '(symbol "P_1_1"',
            """(symbol "P_1_1"
        (pin passive line (at -1.27 0 0) (length 0) (name "other") (number "1"))""",
        )
    )
    before = path.read_bytes()
    assert main([str(path)]) == 1
    assert path.read_bytes() == before


@pytest.mark.parametrize("mode", [0o600, 0o640])
def test_apply_preserves_file_permissions(tmp_path, capsys, mode):
    from kicad_tools.cli.sch_fix_wire_stubs import main

    path = design(tmp_path)
    path.chmod(mode)
    assert main([str(path), "--format", "json"]) == 0
    assert json.loads(capsys.readouterr().out)["applied"]
    assert stat.S_IMODE(path.stat().st_mode) == mode
