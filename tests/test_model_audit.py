"""Tests for the 3D model coverage audit (issue #7682)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kicad_tools.cli.commands.pcb import run_pcb_command
from kicad_tools.cli.parser import create_parser
from kicad_tools.pcb import model_audit
from kicad_tools.pcb.model_audit import audit_pcb_models, expected_pin_count


def _fp(ref: str, lib_id: str = "Lib:Part", body: str = "", attr: str = "") -> str:
    return (
        f'(footprint "{lib_id}" {attr} (property "Reference" "{ref}")\n'
        f'  (pad "1" smd rect (at 0 0)) (pad "2" smd rect (at 1 0))\n{body})'
    )


def _write(tmp_path: Path, *footprints: str, extra: str = "") -> Path:
    pcb = tmp_path / "board.kicad_pcb"
    pcb.write_text("(kicad_pcb " + extra + "\n".join(footprints) + ")")
    return pcb


@pytest.fixture
def models(tmp_path, monkeypatch):
    """A model directory exposed as the host environment."""
    model_dir = tmp_path / "models"
    model_dir.mkdir()
    (model_dir / "part.step").touch()
    (model_dir / "Generic_Box.step").touch()
    for var in ("KICAD7", "KICAD8", "KICAD9", "KICAD10"):
        monkeypatch.setenv(f"{var}_3DMODEL_DIR", str(model_dir))
    monkeypatch.setenv("KCT_LCSC_3D_DIR", str(tmp_path / "lcsc"))
    monkeypatch.delenv("KICAD_CONFIG_HOME", raising=False)
    return model_dir


def _classes(pcb: Path, **kw) -> dict[str, str]:
    report = audit_pcb_models(pcb, **kw)
    return {e.reference: e.klass for e in report.entries}


def test_every_class(tmp_path, models):
    ok = '(model "${KICAD10_3DMODEL_DIR}/part.step")'
    pcb = _write(
        tmp_path,
        _fp("R1", body=ok),
        _fp("R2", body='(model "${KICAD10_3DMODEL_DIR}/gone.step")'),
        _fp("R3"),
        _fp("R4", body='(model "${NOPE_DIR}/part.step")'),
        _fp("R5", body='(model "${KICAD10_3DMODEL_DIR}/part.step" (hide yes))'),
        _fp("R6", body='(model "kicad-embed://blob.step")'),
        _fp("R7", body='(model "${KCT_LCSC_3D_DIR}/C12345.step")'),
        _fp("R8", body='(model "${KICAD10_3DMODEL_DIR}/Generic_Box.step")'),
        _fp("R9", body='(model "kicad-embed://missing.step")'),
        extra=('(embedded_files (file (name "blob.step") (type model) (data |QUJD|)))\n'),
    )
    assert _classes(pcb) == {
        "R1": "resolved",
        "R2": "dangling",
        "R3": "no_ref",
        "R4": "unresolved_var",
        "R5": "hidden_only",
        "R6": "embedded",
        "R7": "lcsc_cache_miss",
        "R8": "placeholder",
        "R9": "dangling",
    }


@pytest.mark.parametrize(
    "lib_id,attr",
    [
        ("MountingHole:MountingHole_3.2mm_M3", ""),
        ("Lib:NetTie-2_SMD_Pad0.5mm", ""),
        ("Connector:TestPoint_Pad_D1.0mm", ""),
        ("Lib:Fiducial_1mm_Mask2mm", ""),
        ("Lib:Anything", "(attr board_only)"),
        ("Lib:Anything", "(attr smd exclude_from_bom)"),
    ],
)
def test_exempt_families_and_attrs(tmp_path, models, lib_id, attr):
    pcb = _write(tmp_path, _fp("X1", lib_id=lib_id, attr=attr))
    report = audit_pcb_models(pcb)
    assert report.entries[0].klass == "exempt"
    assert report.gaps == []
    assert report.total == 0
    assert report.percent is None


def test_percent_excludes_exempt(tmp_path, models):
    pcb = _write(
        tmp_path,
        _fp("R1", body='(model "${KICAD10_3DMODEL_DIR}/part.step")'),
        _fp("R2"),
        _fp("H1", lib_id="MountingHole:MH"),
    )
    report = audit_pcb_models(pcb)
    assert (report.covered, report.total, report.percent) == (1, 2, 50.0)
    assert [g.reference for g in report.gaps] == ["R2"]


def test_empty_board(tmp_path, models):
    report = audit_pcb_models(_write(tmp_path))
    assert report.entries == []
    assert report.percent is None


def test_pinned_env_resolves_nothing_host_would(tmp_path, models):
    pcb = _write(tmp_path, _fp("R1", body='(model "${KICAD10_3DMODEL_DIR}/part.step")'))
    assert _classes(pcb) == {"R1": "resolved"}
    assert _classes(pcb, env="pinned") == {"R1": "dangling"}


def test_pinned_env_still_sees_project_files_and_embedded(tmp_path, models):
    (tmp_path / "local.step").touch()
    pcb = _write(
        tmp_path,
        _fp("R1", body='(model "${KIPRJMOD}/local.step")'),
        _fp("R2", body='(model "kicad-embed://b.step")'),
        extra='(embedded_files (file (name "b.step") (data |QQ==|)))',
    )
    assert _classes(pcb, env="pinned") == {"R1": "resolved", "R2": "embedded"}


def test_model_dir_overrides_env(tmp_path, models, monkeypatch):
    other = tmp_path / "other"
    other.mkdir()
    (other / "x.step").touch()
    pcb = _write(tmp_path, _fp("R1", body='(model "${KICAD10_3DMODEL_DIR}/x.step")'))
    assert _classes(pcb) == {"R1": "dangling"}
    assert _classes(pcb, model_dir=other) == {"R1": "resolved"}


def test_hidden_model_beside_enabled_one_is_resolved(tmp_path, models):
    body = (
        '(model "${KICAD10_3DMODEL_DIR}/gone.step" (hide yes))'
        '(model "${KICAD10_3DMODEL_DIR}/part.step")'
    )
    assert _classes(_write(tmp_path, _fp("R1", body=body))) == {"R1": "resolved"}


def test_pin_count_heuristic(tmp_path, models):
    (models / "PinHeader_1x04_P2.54mm_Vertical.step").touch()
    pads = "".join(f'(pad "{i}" thru_hole circle (at {i} 0))' for i in range(1, 6))
    fp = (
        '(footprint "Connector:PinHeader_1x05_P2.54mm_Vertical" '
        '(property "Reference" "J4")'
        + pads
        + '(model "${KICAD10_3DMODEL_DIR}/PinHeader_1x04_P2.54mm_Vertical.step"))'
    )
    entry = audit_pcb_models(_write(tmp_path, fp)).entries[0]
    assert entry.klass == "resolved"
    assert "4 pins" in entry.suspect and "5 pads" in entry.suspect


def test_pin_count_heuristic_ignores_body_sizes():
    assert expected_pin_count("PinHeader_2x03_P2.54mm.step") == 6
    assert expected_pin_count("SOIC-8_3.9x4.9mm_P1.27mm.step") is None


def test_exempt_data_is_documented():
    assert {"board_only", "exclude_from_bom"} <= model_audit.EXEMPT_ATTRS
    assert {"MountingHole", "NetTie", "TestPoint", "Fiducial"} <= set(
        model_audit.EXEMPT_LIB_PATTERNS
    )


def test_model_audit_cli_json(tmp_path, models, capsys):
    pcb = _write(tmp_path, _fp("R1", body='(model "${KICAD10_3DMODEL_DIR}/part.step")'), _fp("R2"))
    args = create_parser().parse_args(["pcb", "model-audit", str(pcb), "--format", "json"])
    assert run_pcb_command(args) == 0
    data = json.loads(capsys.readouterr().out)
    assert (data["covered"], data["total"], data["env"]) == (1, 2, "host")
    assert data["counts"]["no_ref"] == 1


def test_model_audit_cli_pinned_is_zero(tmp_path, models, capsys):
    pcb = _write(tmp_path, _fp("R1", body='(model "${KICAD10_3DMODEL_DIR}/part.step")'))
    args = create_parser().parse_args(["pcb", "model-audit", str(pcb), "--env", "pinned"])
    assert run_pcb_command(args) == 0
    assert "0/1 (0%)" in capsys.readouterr().out


def test_render_reports_partial_for_bare_footprints(tmp_path, models):
    from kicad_tools.cli import runner

    pcb = _write(tmp_path, _fp("R1"), _fp("R2"))
    legacy = runner.inspect_pcb_models(pcb)
    # The ref walk alone still says "passed" with nothing checked.
    assert legacy == {"status": "passed", "checked_models": 0, "unresolved_models": []}


# -- Issue #7682 follow-up: host-independent gates ---------------------------


def _model_coverage_findings(pcb: Path) -> list:
    from unittest.mock import MagicMock

    from kicad_tools.cli.check_cmd import run_selected_checks

    results = run_selected_checks(MagicMock(), {"model_coverage"}, set(), pcb_path=pcb)
    return [v for v in results.violations if v.rule_id == "model_coverage"]


def _no_library(monkeypatch, tmp_path) -> None:
    for var in ("KICAD7", "KICAD8", "KICAD9", "KICAD10"):
        monkeypatch.setenv(f"{var}_3DMODEL_DIR", str(tmp_path / "nonexistent"))


def test_missing_library_root_is_marked(tmp_path, models, monkeypatch):
    pcb = _write(
        tmp_path,
        _fp("R1", body='(model "${KICAD10_3DMODEL_DIR}/part.step")'),
        _fp("R2", body='(model "${NOPE_DIR}/part.step")'),
        _fp("R3", body='(model "${KICAD10_3DMODEL_DIR}/gone.step")'),
    )
    # Library present: R3 is a real dangling ref, R2's variable is undefined.
    report = audit_pcb_models(pcb)
    assert {e.reference: e.missing_root for e in report.gaps} == {"R2": "NOPE_DIR", "R3": None}
    _no_library(monkeypatch, tmp_path)
    report = audit_pcb_models(pcb)
    assert {e.reference: e.missing_root for e in report.gaps} == {
        "R1": "KICAD10_3DMODEL_DIR",
        "R2": "NOPE_DIR",
        "R3": "KICAD10_3DMODEL_DIR",
    }
    # The class is unchanged; only its cause is recorded.
    assert _classes(pcb)["R1"] == "dangling"


def test_check_collapses_missing_library_into_one_info(tmp_path, models, monkeypatch):
    refs = [_fp(f"R{i}", body='(model "${KICAD10_3DMODEL_DIR}/part.step")') for i in range(5)]
    pcb = _write(tmp_path, *refs, _fp("U1"))
    assert [(v.severity, v.items) for v in _model_coverage_findings(pcb)] == [("warning", ("U1",))]
    _no_library(monkeypatch, tmp_path)
    findings = _model_coverage_findings(pcb)
    # One informational finding for the absent library; the bare footprint
    # (``no_ref``) is a board defect on every host and stays a warning.
    assert sorted((v.severity, v.items) for v in findings) == [
        ("info", ("R0", "R1", "R2", "R3", "R4")),
        ("warning", ("U1",)),
    ]
    info = next(v for v in findings if v.severity == "info")
    assert "not evaluated" in info.message and "${KICAD10_3DMODEL_DIR}" in info.message
    assert "#7683" in info.message
    assert not info.is_warning


def test_opt_in_deferral_expires_with_pinned_model_set():
    """``model_coverage`` stays out of the default set only until #7683.

    When ``PINNED_MODEL_DIR`` is set this fails: drop ``model_coverage`` from
    ``DEFERRED_OPT_IN_CATEGORIES`` (making it default-on) and re-record board
    evidence then (#7683/#7685).
    """
    from kicad_tools.cli.check_categories import CHECK_CATEGORIES, DEFERRED_OPT_IN_CATEGORIES

    assert model_audit.PINNED_MODEL_DIR is None, "#7683 landed: drop the #7682 opt-in deferral"
    assert {"model_coverage"} == DEFERRED_OPT_IN_CATEGORIES
    assert set(CHECK_CATEGORIES) >= DEFERRED_OPT_IN_CATEGORIES


def test_default_category_set_excludes_model_coverage(tmp_path, models):
    """The deferral lives in ``run_selected_checks``: every default caller
    (``kct check``, ``run_check_rule_set``, ``kct readiness``) skips it."""
    from unittest.mock import MagicMock

    from kicad_tools.cli.check_cmd import run_selected_checks

    pcb = _write(tmp_path, _fp("U1"))
    coverage: dict = {}
    only = {"model_coverage"}
    for only_set, opt_in, expected in [
        (None, frozenset(), []),
        (None, {"model_coverage"}, [("warning", ("U1",))]),
        (only, frozenset(), [("warning", ("U1",))]),
    ]:
        results = run_selected_checks(
            MagicMock(), only_set, set(), pcb_path=pcb, opt_in=opt_in, coverage=coverage
        )
        found = [(v.severity, v.items) for v in results.violations if v.rule_id == "model_coverage"]
        assert found == expected, (only_set, opt_in)
        if only_set is None and not opt_in:
            assert coverage["model_coverage"].reason == "opt_in:--3d-models"


def _check_cli(pcb: Path, *extra: str) -> tuple[int, dict]:
    from kicad_tools.cli import check_cmd

    report = pcb.with_suffix(".check.json")
    code = check_cmd.main([str(pcb), "--format", "json", "--output", str(report), *extra])
    return code, json.loads(report.read_text())


def _coverage_rules(report: dict) -> list[str]:
    return [v["rule_id"] for v in report["violations"] if v["rule_id"] == "model_coverage"]


def test_check_cli_strict_unchanged_by_default_and_opt_in_flag(tmp_path, models):
    """Plain ``kct check --strict`` never sees ``model_coverage``;
    ``--3d-models`` / ``--only model_coverage`` opt in (issue #7682)."""
    pcb = _write(tmp_path, _fp("U1"))
    _, default = _check_cli(pcb, "--strict")
    assert _coverage_rules(default) == []
    _, flagged = _check_cli(pcb, "--3d-models")
    assert _coverage_rules(flagged) == ["model_coverage"]
    _, only = _check_cli(pcb, "--only", "model_coverage")
    assert _coverage_rules(only) == ["model_coverage"]
    code, strict = _check_cli(pcb, "--only", "model_coverage", "--strict")
    assert code == 2 and _coverage_rules(strict) == ["model_coverage"]


def test_top_level_kct_check_forwards_3d_models_flag(tmp_path, models):
    """``kct check --3d-models`` goes through the top-level parser too."""
    from kicad_tools.cli import main as kct_main

    pcb = _write(tmp_path, _fp("U1"))
    report = tmp_path / "top.json"
    kct_main(["check", str(pcb), "--3d-models", "--format", "json", "--output", str(report)])
    assert _coverage_rules(json.loads(report.read_text())) == ["model_coverage"]
    kct_main(["check", str(pcb), "--format", "json", "--output", str(report)])
    assert _coverage_rules(json.loads(report.read_text())) == []
