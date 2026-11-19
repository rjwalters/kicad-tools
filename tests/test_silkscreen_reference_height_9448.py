"""Declared reference-designator height (Issue #9448).

A project may declare ``requirements.manufacturing.silkscreen_min_reference_height_mm``
(>= 0.8 mm).  It relaxes the ``silkscreen_text_height`` floor for reference
designators only -- in ``kct check`` and in the generated ``.kicad_dru`` -- while
labels and board text keep the fab floor.  Without a declaration nothing changes.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from kicad_tools.cli.kicad_toolchain import kicad_cli_env, scratch_config_home
from kicad_tools.manufacturers import get_profile
from kicad_tools.manufacturers.dru_generator import generate_dru
from kicad_tools.schema.pcb import PCB
from kicad_tools.sync.discover import resolve_declared_reference_height_for_pcb
from kicad_tools.validate import DRCChecker
from kicad_tools.validate.rules.silkscreen import check_silkscreen_text_height

KICAD_CLI = shutil.which("kicad-cli")


def _board(ref_height: float = 0.8, label_height: float = 0.9) -> str:
    """20x20 mm board: designator R1 and one functional label, both on F.Silkscreen."""
    return f"""\
(kicad_pcb (version 20240108) (generator "pcbnew") (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (36 "B.SilkS" user "B.Silkscreen")
    (37 "F.SilkS" user "F.Silkscreen")
    (44 "Edge.Cuts" user)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (gr_rect (start 100 100) (end 120 120) (stroke (width 0.1) (type default)) (fill none) (layer "Edge.Cuts"))
  (gr_text "LABEL" (at 110 105) (layer "F.SilkS")
    (effects (font (size {label_height} {label_height}) (thickness 0.15))))
  (footprint "Test:R" (layer "F.Cu") (at 110 112)
    (property "Reference" "R1" (at 0 0 0) (layer "F.SilkS")
      (effects (font (size {ref_height} {ref_height}) (thickness 0.15))))
    (property "Value" "10k" (at 0 2 0) (layer "F.Fab")
      (effects (font (size 1 1) (thickness 0.15))))
  )
)
"""


def _load(tmp_path: Path, **kwargs: float) -> PCB:
    path = tmp_path / "board.kicad_pcb"
    path.write_text(_board(**kwargs))
    return PCB.load(str(path))


def _height_items(pcb: PCB, **checker_kwargs: float) -> list[str]:
    checker = DRCChecker(pcb, manufacturer="jlcpcb", layers=2, **checker_kwargs)
    results = check_silkscreen_text_height(pcb, checker.design_rules)
    return [item for v in results.violations for item in v.items]


JLC = get_profile("jlcpcb").get_design_rules(2)


# --- DesignRules -----------------------------------------------------------


def test_fab_floor_is_one_millimetre():
    assert JLC.min_silkscreen_height_mm == 1.0


def test_no_declaration_keeps_fab_floor():
    assert JLC.declared_reference_height_mm is None
    assert JLC.min_silkscreen_reference_height_mm == 1.0
    assert not JLC.has_reference_height_deviation


def test_declaration_only_relaxes():
    relaxed = replace(JLC, declared_reference_height_mm=0.8)
    assert relaxed.min_silkscreen_reference_height_mm == 0.8
    assert relaxed.has_reference_height_deviation
    stricter = replace(JLC, declared_reference_height_mm=1.2)
    assert stricter.min_silkscreen_reference_height_mm == 1.0
    assert not stricter.has_reference_height_deviation


# --- checker ---------------------------------------------------------------


def test_checker_without_declaration_flags_designator_and_label(tmp_path: Path):
    items = _height_items(_load(tmp_path))
    assert "R1 (reference)" in items
    assert "LABEL" in items


def test_checker_with_declaration_flags_only_the_label(tmp_path: Path):
    items = _height_items(_load(tmp_path), declared_reference_height_mm=0.8)
    assert items == ["LABEL"]


def test_designator_below_declared_height_still_flagged(tmp_path: Path):
    items = _height_items(_load(tmp_path, ref_height=0.7), declared_reference_height_mm=0.8)
    assert "R1 (reference)" in items


def test_hidden_designator_is_not_flagged(tmp_path: Path):
    path = tmp_path / "board.kicad_pcb"
    path.write_text(
        _board(ref_height=0.5).replace(
            '(property "Reference" "R1" (at 0 0 0) (layer "F.SilkS")',
            '(property "Reference" "R1" (at 0 0 0) (layer "F.SilkS") (hide yes)',
        )
    )
    items = _height_items(PCB.load(str(path)), declared_reference_height_mm=0.8)
    assert "R1 (reference)" not in items


# --- generated .kicad_dru ---------------------------------------------------


def test_dru_unchanged_without_declaration():
    dru = generate_dru(JLC, "JLCPCB")
    assert dru.count("text_height") == 1
    assert "Silkscreen Reference Height" not in dru
    assert "A.Text == A.Reference" not in dru


def test_dru_splits_height_rule_with_declaration():
    dru = generate_dru(replace(JLC, declared_reference_height_mm=0.8), "JLCPCB")
    assert dru.count("text_height") == 2
    assert "!(A.Text == A.Reference)" in dru
    assert 'rule "Silkscreen Reference Height - JLCPCB"' in dru
    assert "(constraint text_height (min 0.8mm))" in dru
    assert "(constraint text_height (min 1.0mm))" in dru


def test_dru_ignores_declaration_at_or_above_fab_floor():
    assert generate_dru(replace(JLC, declared_reference_height_mm=1.0), "JLCPCB") == generate_dru(
        JLC, "JLCPCB"
    )


# --- spec / discovery --------------------------------------------------------


def _write_spec(directory: Path, value: object) -> Path:
    (directory / "project.kct").write_text(
        "requirements:\n  manufacturing:\n    target_fab: jlcpcb\n"
        f"    silkscreen_min_reference_height_mm: {value}\n"
    )
    return directory / "board.kicad_pcb"


def test_discover_reads_declaration(tmp_path: Path):
    assert resolve_declared_reference_height_for_pcb(_write_spec(tmp_path, 0.8)) == 0.8


def test_discover_none_without_spec(tmp_path: Path):
    assert resolve_declared_reference_height_for_pcb(tmp_path / "board.kicad_pcb") is None


@pytest.mark.parametrize("value", ["0.7", "'big'", "true"])
def test_discover_ignores_invalid_declaration(tmp_path: Path, value: str):
    assert resolve_declared_reference_height_for_pcb(_write_spec(tmp_path, value)) is None


def test_schema_rejects_below_minimum():
    from pydantic import ValidationError

    from kicad_tools.spec.schema import ManufacturingRequirements

    assert (
        ManufacturingRequirements(silkscreen_min_reference_height_mm=0.8)
    ).silkscreen_min_reference_height_mm == 0.8
    assert ManufacturingRequirements().silkscreen_min_reference_height_mm is None
    with pytest.raises(ValidationError):
        ManufacturingRequirements(silkscreen_min_reference_height_mm=0.7)


# --- kicad-cli referee --------------------------------------------------------


def _kicad_height_findings(tmp_path: Path, dru: str) -> list[tuple[str, str]]:
    pcb = tmp_path / "ref.kicad_pcb"
    pcb.write_text(_board())
    pcb.with_suffix(".kicad_dru").write_text(dru)
    report = tmp_path / "drc.json"
    with scratch_config_home() as home:
        subprocess.run(
            [
                KICAD_CLI,
                "pcb",
                "drc",
                "--format",
                "json",
                "--severity-all",
                "-o",
                str(report),
                str(pcb),
            ],
            env=kicad_cli_env(home),
            capture_output=True,
            text=True,
            timeout=120,
        )
    return [
        (v["description"], v["items"][0]["description"])
        for v in json.loads(report.read_text())["violations"]
        if v["type"] == "text_height"
    ]


@pytest.mark.skipif(KICAD_CLI is None, reason="kicad-cli not installed")
def test_kicad_cli_and_kct_check_agree_on_declared_board(tmp_path: Path):
    findings = _kicad_height_findings(
        tmp_path, generate_dru(replace(JLC, declared_reference_height_mm=0.8), "JLCPCB")
    )
    assert len(findings) == 1, findings
    assert "Silkscreen Height" in findings[0][0]
    assert "LABEL" in findings[0][1]
    # kct check reports the same single finding.
    assert _height_items(_load(tmp_path), declared_reference_height_mm=0.8) == ["LABEL"]


@pytest.mark.skipif(KICAD_CLI is None, reason="kicad-cli not installed")
def test_kicad_cli_without_declaration_flags_both(tmp_path: Path):
    findings = _kicad_height_findings(tmp_path, generate_dru(JLC, "JLCPCB"))
    assert sorted(item for _, item in findings) == [
        "PCB text 'LABEL' on F.Silkscreen",
        "Reference field of R1",
    ]


# --- kct init (review of PR #9451) -------------------------------------------


def _init_dru(directory: Path) -> str:
    from kicad_tools.cli.init_cmd import init_project

    # ``init_project(target=<dir>/proj)`` writes ``proj.kicad_dru`` into ``<dir>``.
    assert init_project(target=str(directory / "proj"), manufacturer="jlcpcb", layers=2) == 0
    return (directory / "proj.kicad_dru").read_text(encoding="utf-8")


def test_init_emits_split_rule_beside_declaring_spec(tmp_path: Path):
    project = tmp_path
    _write_spec(project, 0.8)
    assert 'rule "Silkscreen Reference Height - JLCPCB"' in _init_dru(project)


def test_init_rerun_keeps_split_rule(tmp_path: Path):
    project = tmp_path
    _write_spec(project, 0.8)
    _init_dru(project)
    assert 'rule "Silkscreen Reference Height - JLCPCB"' in _init_dru(project)


def test_init_without_declaration_has_no_split_rule(tmp_path: Path):
    project = tmp_path
    assert "Silkscreen Reference Height" not in _init_dru(project)


# --- review round 2: invalid values, other texts, wiring, report ------------

from kicad_tools.manufacturers.base import is_valid_declared_reference_height  # noqa: E402


@pytest.mark.parametrize(
    "value", [float("nan"), float("inf"), float("-inf"), "0.8", True, 0.79, None]
)
def test_shared_validator_rejects_invalid(value):
    assert not is_valid_declared_reference_height(value)


@pytest.mark.parametrize("value", [0.8, 1, 1.5])
def test_shared_validator_accepts_valid(value):
    assert is_valid_declared_reference_height(value)


@pytest.mark.parametrize("value", [".nan", ".inf", "-.inf", "'0.8'", "true"])
def test_discover_ignores_non_finite_and_non_numeric(tmp_path: Path, value: str):
    assert resolve_declared_reference_height_for_pcb(_write_spec(tmp_path, value)) is None


@pytest.mark.parametrize("value", [float("nan"), float("inf"), "0.8", True])
def test_schema_rejects_non_finite_and_non_numeric(value):
    from pydantic import ValidationError

    from kicad_tools.spec.schema import ManufacturingRequirements

    with pytest.raises(ValidationError):
        ManufacturingRequirements(silkscreen_min_reference_height_mm=value)


def test_design_rules_nan_declaration_keeps_fab_floor():
    rules = replace(JLC, declared_reference_height_mm=float("nan"))
    assert rules.min_silkscreen_reference_height_mm == 1.0
    assert not rules.has_reference_height_deviation


def test_nan_declaration_does_not_disable_designator_check(tmp_path: Path):
    items = _height_items(
        _load(tmp_path, ref_height=0.1), declared_reference_height_mm=float("nan")
    )
    assert "R1 (reference)" in items


def test_value_and_user_text_on_silk_keep_fab_floor_under_declaration(tmp_path: Path):
    path = tmp_path / "board.kicad_pcb"
    text = _board().replace(
        '(property "Value" "10k" (at 0 2 0) (layer "F.Fab")\n'
        "      (effects (font (size 1 1) (thickness 0.15))))",
        '(property "Value" "10k" (at 0 2 0) (layer "F.SilkS")\n'
        "      (effects (font (size 0.85 0.85) (thickness 0.15))))\n"
        '    (fp_text user "NOTE" (at 0 4 0) (layer "F.SilkS")\n'
        "      (effects (font (size 0.85 0.85) (thickness 0.15))))",
    )
    assert "0.85" in text
    path.write_text(text)
    items = _height_items(PCB.load(str(path)), declared_reference_height_mm=0.8)
    assert "R1 (reference)" not in items
    assert "LABEL" in items
    assert any("value" in i.lower() or "10k" in i for i in items), items
    assert len(items) >= 3, items


def _declaring_project(tmp_path: Path) -> Path:
    (tmp_path / "board.kicad_pcb").write_text(_board())
    _write_spec(tmp_path, 0.8)
    return tmp_path / "board.kicad_pcb"


def test_build_check_rule_set_honours_project_kct(tmp_path: Path):
    from kicad_tools.cli.check_cmd import build_check_rule_set

    pcb = _declaring_project(tmp_path)
    rules = build_check_rule_set(pcb, manufacturer="jlcpcb", layers=2)
    items = [
        i
        for v in rules.checker.check_all().violations
        if v.rule_id == "silkscreen_text_height"
        for i in v.items
    ]
    assert "R1 (reference)" not in items and "LABEL" in items


def test_kct_check_cli_reports_declaration(tmp_path: Path, capsys):
    from kicad_tools.cli.check_cmd import main as check_main

    pcb = _declaring_project(tmp_path)
    check_main([str(pcb), "--mfr", "jlcpcb", "--only", "silkscreen", "--format", "json"])
    data = json.loads(capsys.readouterr().out)
    items = [i for v in data["violations"] for i in v.get("items", [])]
    assert "R1 (reference)" not in items and "LABEL" in items
    assert data["declared_deviations"] == [
        {
            "rule": "silkscreen_text_height",
            "scope": "reference designators",
            "declared_min_height_mm": 0.8,
            "fab_min_height_mm": 1.0,
            "source": "project.kct requirements.manufacturing.silkscreen_min_reference_height_mm",
        }
    ]
    check_main([str(pcb), "--mfr", "jlcpcb", "--only", "silkscreen"])
    assert "Declared deviation: silkscreen_text_height for reference designators" in (
        capsys.readouterr().out
    )


def test_kct_check_cli_no_declaration_no_deviation(tmp_path: Path, capsys):
    from kicad_tools.cli.check_cmd import main as check_main

    pcb = tmp_path / "board.kicad_pcb"
    pcb.write_text(_board())
    check_main([str(pcb), "--mfr", "jlcpcb", "--only", "silkscreen", "--format", "json"])
    assert "declared_deviations" not in json.loads(capsys.readouterr().out)


def test_kct_check_emit_dru_honours_project_kct(tmp_path: Path):
    from kicad_tools.cli.check_cmd import main as check_main

    pcb = _declaring_project(tmp_path)
    check_main([str(pcb), "--mfr", "jlcpcb", "--only", "silkscreen", "--emit-dru"])
    assert 'rule "Silkscreen Reference Height' in pcb.with_suffix(".kicad_dru").read_text()


def test_write_drc_constraints_honours_project_kct(tmp_path: Path):
    from kicad_tools.manufacturers.project_generator import write_drc_constraints

    pcb = _declaring_project(tmp_path)
    write_drc_constraints(pcb, JLC, manufacturer_id="jlcpcb", layers=2)
    assert 'rule "Silkscreen Reference Height' in pcb.with_suffix(".kicad_dru").read_text()


def test_readiness_records_declaration_in_manifest_and_readme(tmp_path: Path):
    from kicad_tools.cli import readiness_cmd as cmd
    from tests.test_kct_readiness_cmd import FakeEngines, make_board

    board = make_board(tmp_path)
    assert (
        cmd.main([str(board), "--mfr", "jlcpcb", "--generate"], engines=FakeEngines(board).bundle())
        == 0
    )
    args = cmd.build_parser().parse_args([str(board), "--mfr", "jlcpcb"])
    opts, _ = cmd.resolve_options(args)
    assert cmd._declared_reference_height_deviation(opts) is None
    cmd._write_full_manifest(opts, {})
    manifest = json.loads((opts.output_dir / "manifest.json").read_text())
    assert "declared_deviations" not in manifest
    assert "DECLARED DEVIATION" not in cmd._write_readme(opts, {}, []).read_text()

    _write_spec(opts.pcb.parent, 0.8)
    deviation = cmd._declared_reference_height_deviation(opts)
    assert deviation is not None and deviation["declared_min_height_mm"] == 0.8
    cmd._write_full_manifest(opts, {})
    manifest = json.loads((opts.output_dir / "manifest.json").read_text())
    assert manifest["declared_deviations"] == [deviation]
    assert "DECLARED DEVIATION" in cmd._write_readme(opts, {}, []).read_text()
