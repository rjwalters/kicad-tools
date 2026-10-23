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


def test_check_reports_missing_library_as_one_stderr_note(tmp_path, models, monkeypatch, capsys):
    refs = [_fp(f"R{i}", body='(model "${KICAD10_3DMODEL_DIR}/part.step")') for i in range(5)]
    pcb = _write(tmp_path, *refs, _fp("U1"))
    assert [(v.severity, v.items) for v in _model_coverage_findings(pcb)] == [("warning", ("U1",))]
    assert "NOTE" not in capsys.readouterr().err
    _no_library(monkeypatch, tmp_path)
    findings = _model_coverage_findings(pcb)
    # The absent library is the environment's gap: one stderr note, never a
    # finding, so serialized reports are identical on every host.  The bare
    # footprint (``no_ref``) is a board defect everywhere and stays a warning.
    assert [(v.severity, v.items) for v in findings] == [("warning", ("U1",))]
    err = capsys.readouterr().err
    assert err.count("NOTE: 3D coverage not evaluated for 5 footprint(s)") == 1
    assert "${KICAD10_3DMODEL_DIR}" in err and "#7683" in err


def test_model_coverage_is_default_on():
    """#7685 ended the #7682 opt-in deferral: every board is covered, so
    ``model_coverage`` is part of the default ``kct check`` set."""
    from kicad_tools.cli.check_categories import CHECK_CATEGORIES, DEFERRED_OPT_IN_CATEGORIES

    assert "model_coverage" in CHECK_CATEGORIES
    assert frozenset() == DEFERRED_OPT_IN_CATEGORIES


def _committed_board_pcbs() -> list[Path]:
    root = Path(__file__).resolve().parents[1] / "boards"
    return sorted(
        p
        for p in root.glob("[0-9][0-9]-*/output/*.kicad_pcb")
        if p.is_file() and "manufacturing" not in p.parts
    )


@pytest.mark.parametrize("pcb", _committed_board_pcbs(), ids=lambda p: str(p.parent.parent.name))
def test_committed_boards_carry_a_body_ref_for_every_footprint(pcb):
    """The deferral's real trigger (#7685): a committed board has no host-independent
    gap -- every non-exempt footprint names a model (``no_ref``), and no body is a
    placeholder or hidden.  This fails on regeneration that drops the refs (board 00's
    2026-10-08 regeneration) instead of waiting for a pinned-set variable that
    nothing sets."""
    report = audit_pcb_models(pcb, env="host")
    host_independent = [
        (e.reference, e.klass)
        for e in report.entries
        if e.klass in {"no_ref", "placeholder", "hidden_only"}
    ]
    assert host_independent == []


def test_default_category_set_runs_model_coverage(tmp_path, models):
    """With no deferral, every default caller (``kct check``,
    ``run_check_rule_set``, ``kct readiness``) runs the category."""
    from unittest.mock import MagicMock

    from kicad_tools.cli.check_cmd import run_selected_checks

    pcb = _write(tmp_path, _fp("U1"))
    coverage: dict = {}
    for only_set in (None, {"model_coverage"}):
        results = run_selected_checks(MagicMock(), only_set, set(), pcb_path=pcb, coverage=coverage)
        found = [(v.severity, v.items) for v in results.violations if v.rule_id == "model_coverage"]
        assert found == [("warning", ("U1",))], only_set
        assert coverage["model_coverage"].state == "checked"
        coverage.clear()


def _check_cli(pcb: Path, *extra: str) -> tuple[int, dict]:
    from kicad_tools.cli import check_cmd

    report = pcb.with_suffix(".check.json")
    code = check_cmd.main([str(pcb), "--format", "json", "--output", str(report), *extra])
    return code, json.loads(report.read_text())


def _coverage_rules(report: dict) -> list[str]:
    return [v["rule_id"] for v in report["violations"] if v["rule_id"] == "model_coverage"]


def test_check_cli_strict_unchanged_by_default_and_opt_in_flag(tmp_path, models):
    """Plain ``kct check`` runs ``model_coverage`` since #7685 (default-on); the
    ``--3d-models`` flag is a kept-for-compatibility no-op (issue #7682)."""
    pcb = _write(tmp_path, _fp("U1"))
    _, default = _check_cli(pcb, "--strict")
    assert _coverage_rules(default) == ["model_coverage"]
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
    assert _coverage_rules(json.loads(report.read_text())) == ["model_coverage"]


def test_lcsc_content_changed_class(tmp_path, models):
    """A cached LCSC body whose sha256 differs from its fetch-time pin (#7686)."""
    import hashlib

    lcsc = tmp_path / "lcsc"
    lcsc.mkdir()
    body = b"ISO-10303-21; original"
    (lcsc / "C12345.step").write_bytes(b"ISO-10303-21; changed upstream")
    (lcsc / "C12345.meta.json").write_text(
        json.dumps({"lcsc": "C12345", "sha256": hashlib.sha256(body).hexdigest(), "source": "lcsc"})
    )
    (lcsc / "C777.step").write_bytes(body)
    (lcsc / "C777.meta.json").write_text(
        json.dumps({"lcsc": "C777", "sha256": hashlib.sha256(body).hexdigest(), "source": "lcsc"})
    )
    pcb = _write(
        tmp_path,
        _fp("R1", body='(model "${KCT_LCSC_3D_DIR}/C12345.step")'),
        _fp("R2", body='(model "${KCT_LCSC_3D_DIR}/C777.step")'),
    )
    classes = _classes(pcb)
    assert classes == {"R1": "lcsc_content_changed", "R2": "resolved"}
    assert "lcsc_content_changed" in model_audit.CLASSES


def test_attach_local_models_replaces_dangling_ref_and_is_idempotent(tmp_path, monkeypatch):
    """Project-local bodies (#7685) cover footprints whose stock STEP does not exist."""
    from kicad_tools.pcb.models3d import attach_local_models

    monkeypatch.delenv("KICAD_CONFIG_HOME", raising=False)
    for var in ("KICAD7", "KICAD8", "KICAD9", "KICAD10"):
        monkeypatch.setenv(f"{var}_3DMODEL_DIR", str(tmp_path / "no-library"))
    stock = '(model "${KICAD10_3DMODEL_DIR}/Connector.3dshapes/J.step")\n'
    pcb = _write(tmp_path, _fp("J1", lib_id="Connector:J", body=stock), _fp("R1"), extra="\n")
    shapes = tmp_path / "3dshapes"
    shapes.mkdir()
    (shapes / "J.wrl").write_text("#VRML V2.0 utf8\n")
    (shapes / "local_models.json").write_text(
        json.dumps({"Connector:J": "J.wrl", "Other:X": "X.wrl"})
    )

    assert attach_local_models(pcb) == ["Connector:J"]
    text = pcb.read_text()
    assert "${KIPRJMOD}/3dshapes/J.wrl" in text and "J.step" not in text
    assert _classes(pcb)["J1"] == "resolved"
    assert _classes(pcb)["R1"] == "no_ref"  # only mapped lib_ids change
    assert attach_local_models(pcb) == []  # idempotent
    assert pcb.read_text() == text


def test_attach_local_models_ignores_missing_manifest_or_body(tmp_path):
    from kicad_tools.pcb.models3d import attach_local_models

    pcb = _write(tmp_path, _fp("J1", lib_id="Connector:J"), extra="\n")
    before = pcb.read_text()
    assert attach_local_models(pcb) == []
    (tmp_path / "3dshapes").mkdir()
    (tmp_path / "3dshapes" / "local_models.json").write_text(
        json.dumps({"Connector:J": "gone.wrl"})
    )
    assert attach_local_models(pcb) == []  # never a dangling ref
    assert pcb.read_text() == before


def test_attach_local_models_covers_every_footprint_with_the_lib_id(tmp_path):
    from kicad_tools.pcb.models3d import attach_local_models

    pcb = _write(tmp_path, _fp("L1", lib_id="L:X"), _fp("R1"), _fp("L2", lib_id="L:X"), extra="\n")
    shapes = tmp_path / "3dshapes"
    shapes.mkdir()
    (shapes / "X.wrl").write_text("#VRML V2.0 utf8\n")
    (shapes / "local_models.json").write_text(json.dumps({"L:X": "X.wrl"}))

    assert attach_local_models(pcb) == ["L:X"]
    classes = _classes(pcb)
    assert classes["L1"] == classes["L2"] == "resolved"
    assert classes["R1"] == "no_ref"
    assert pcb.read_text().count('(model "${KIPRJMOD}/3dshapes/X.wrl"') == 2
    assert attach_local_models(pcb) == []  # idempotent, even with a repeat


def test_check_report_bytes_do_not_depend_on_a_model_library(tmp_path, monkeypatch, capsys):
    """Committed evidence must be identical with and without a 3D model library."""
    from kicad_tools.cli.check_cmd import main

    fp = _fp("R1", body='(model "${KICAD10_3DMODEL_DIR}/part.step")')
    pcb = _write(tmp_path, fp)
    monkeypatch.setenv("KCT_LCSC_3D_DIR", str(tmp_path / "lcsc"))
    monkeypatch.delenv("KICAD_CONFIG_HOME", raising=False)

    def report_bytes(model_dir: Path) -> str:
        for var in ("KICAD7", "KICAD8", "KICAD9", "KICAD10"):
            monkeypatch.setenv(f"{var}_3DMODEL_DIR", str(model_dir))
        out = tmp_path / f"report-{model_dir.name}.json"
        main([str(pcb), "--only", "model_coverage", "--format", "json", "--output", str(out)])
        capsys.readouterr()
        return out.read_text()

    library = tmp_path / "library"
    library.mkdir()
    (library / "part.step").touch()
    assert report_bytes(library) == report_bytes(tmp_path / "nonexistent")


# --- Issue #7743: host-library-dependent refs are `unknown`, from the board alone ---

_LIB = '(model "${KICAD10_3DMODEL_DIR}/part.step")'
_LCSC = '(model "${KCT_LCSC_3D_DIR}/C12345.step")'
_PRJ = '(model "${KIPRJMOD}/3dshapes/X.wrl")'
_EMBED = '(model "kicad-embed://body.step")'
_HOST_LIB_REASON = "host_library_refs ({n} footprint(s); per-host detail: kct pcb model-audit)"


def _host_library_board(tmp_path: Path) -> Path:
    return _write(
        tmp_path,
        _fp("R1", body=_LIB),  # counts
        _fp("R2", body=_LCSC),  # counts: LCSC cache variable
        _fp("R3", body=_PRJ),  # project-local: no
        _fp("R4", body=_EMBED),  # embedded (dangling here): no
        _fp("R5", body=_LIB + _PRJ),  # one project-local ref suffices: no
        _fp("R6", body=_LIB + _EMBED),  # one embedded ref suffices: no
        _fp("MH1", lib_id="MountingHole:MountingHole_3.2mm", body=_LIB),  # exempt: no
        _fp("U1"),  # no_ref: no
        _fp("R7", body='(model "${KICAD10_3DMODEL_DIR}/part.step" (hide yes))'),  # hidden_only
        _fp("R8", body='(model "3dshapes/rel.step")'),  # literal relative path: no
        _fp("R9", body='(model "${KICAD10_3DMODEL_DIR}/x.step" (hide yes))' + _LIB),  # counts
    )


def _dependent(pcb: Path) -> list[str]:
    return [e.reference for e in model_audit.host_library_dependent(audit_pcb_models(pcb).entries)]


def test_is_library_rooted_ref_is_text_only():
    rooted = model_audit.is_library_rooted_ref
    assert rooted("${KICAD10_3DMODEL_DIR}/Package_SO.3dshapes/SOIC-8.step")
    assert rooted("$(KICAD8_3DMODEL_DIR)/x.step")
    assert rooted("${KCT_LCSC_3D_DIR}/C1.step")
    assert not rooted("${KIPRJMOD}/3dshapes/x.wrl")
    assert not rooted("kicad-embed://x.step")
    assert not rooted("3dshapes/x.step")
    assert not rooted("/abs/x.step")
    assert not rooted("")


def test_host_library_dependent_counts_only_all_library_rooted_footprints(
    tmp_path, models, monkeypatch
):
    pcb = _host_library_board(tmp_path)
    expected = ["R1", "R2", "R9"]
    assert _dependent(pcb) == expected
    # Same answer with the library variables unset or naming a missing dir.
    for var in ("KICAD7", "KICAD8", "KICAD9", "KICAD10"):
        monkeypatch.delenv(f"{var}_3DMODEL_DIR", raising=False)
    monkeypatch.delenv("KCT_LCSC_3D_DIR", raising=False)
    assert _dependent(pcb) == expected
    _no_library(monkeypatch, tmp_path)
    assert _dependent(pcb) == expected
    # And under the pinned and explicit-dir environments.
    assert [
        e.reference
        for e in model_audit.host_library_dependent(audit_pcb_models(pcb, env="pinned").entries)
    ] == expected
    assert [
        e.reference
        for e in model_audit.host_library_dependent(audit_pcb_models(pcb, model_dir=models).entries)
    ] == expected
    # Not serialized: ``kct pcb model-audit`` JSON is unchanged.
    assert all("library_rooted" not in e.to_dict() for e in audit_pcb_models(pcb).entries)


def _model_coverage_entry(pcb: Path, tag: str) -> tuple[int, dict]:
    from kicad_tools.cli import check_cmd

    out = pcb.with_name(f"{tag}.check.json")
    code = check_cmd.main(
        [str(pcb), "--only", "model_coverage", "--format", "json", "--output", str(out)]
    )
    return code, json.loads(out.read_text())["coverage"]["model_coverage"]


def test_check_json_reports_host_library_refs_unknown(tmp_path, models, monkeypatch, capsys):
    """AC4: unknown, non-blocking, same N with and without the library."""
    pcb = _write(tmp_path, _fp("R1", body=_LIB), _fp("R2", body=_LIB), _fp("R3", body=_PRJ + _LIB))
    (tmp_path / "3dshapes").mkdir()
    (tmp_path / "3dshapes" / "X.wrl").touch()
    code_lib, with_lib = _model_coverage_entry(pcb, "lib")
    _no_library(monkeypatch, tmp_path)
    code_none, without = _model_coverage_entry(pcb, "none")
    capsys.readouterr()
    assert with_lib == without
    assert with_lib["state"] == "unknown"
    assert with_lib["reason"] == _HOST_LIB_REASON.format(n=2)
    assert with_lib["blocking"] is False
    assert code_lib == code_none


def test_check_json_keeps_checked_without_library_refs(tmp_path, models, capsys):
    """AC5: project-local, embedded, exempt and no_ref footprints stay ``checked``."""
    pcb = _write(
        tmp_path,
        _fp("R1", body=_PRJ),
        _fp("R2", body=_EMBED),
        _fp("MH1", lib_id="MountingHole:MountingHole_3.2mm", body=_LIB),
        _fp("U1"),
    )
    _, entry = _model_coverage_entry(pcb, "local")
    capsys.readouterr()
    assert entry["state"] == "checked"
    assert entry["reason"] is None


def test_host_library_unknown_does_not_change_findings_or_strict(tmp_path, models, capsys):
    """AC7: per-footprint warnings, verdict and ``--strict`` exit codes are the
    same as for an otherwise-identical board whose body is project-local."""
    lib_dir = tmp_path / "lib"
    prj_dir = tmp_path / "prj"
    lib_dir.mkdir()
    prj_dir.mkdir()
    (prj_dir / "3dshapes").mkdir()
    (prj_dir / "3dshapes" / "X.wrl").touch()
    lib_pcb = _write(lib_dir, _fp("R1", body=_LIB), _fp("U1"))
    prj_pcb = _write(prj_dir, _fp("R1", body=_PRJ), _fp("U1"))
    for extra in ((), ("--strict",)):
        lib_code, lib_report = _check_cli(lib_pcb, "--only", "model_coverage", *extra)
        prj_code, prj_report = _check_cli(prj_pcb, "--only", "model_coverage", *extra)
        assert lib_code == prj_code, extra
        assert lib_report["verdict"] == prj_report["verdict"], extra
        assert _coverage_rules(lib_report) == _coverage_rules(prj_report) == ["model_coverage"]
        assert lib_report["coverage"]["model_coverage"]["state"] == "unknown"
        assert prj_report["coverage"]["model_coverage"]["state"] == "checked"
    assert lib_code == 2  # --strict still fails on the U1 no_ref warning
    capsys.readouterr()


def test_model_coverage_stays_non_blocking(tmp_path, models, capsys):
    """``kct readiness`` refuses only on *blocking* unknowns; this one is not."""
    from kicad_tools.cli.readiness_cmd import _blocking_unknown_coverage
    from kicad_tools.validate.coverage import BLOCKING_CATEGORIES

    assert "model_coverage" not in BLOCKING_CATEGORIES
    pcb = _write(tmp_path, _fp("R1", body=_LIB))
    _, report = _check_cli(pcb)
    capsys.readouterr()
    assert report["coverage"]["model_coverage"]["state"] == "unknown"
    assert not any(u.startswith("model_coverage") for u in _blocking_unknown_coverage(report))


def test_check_report_shows_host_library_refs_unknown(tmp_path, monkeypatch, capsys):
    """AC6: the bytes-test board reports ``unknown:host_library_refs`` on both hosts,
    and a library that exists but lacks the file yields the same coverage entry."""
    from kicad_tools.cli.check_cmd import main

    pcb = _write(tmp_path, _fp("R1", body='(model "${KICAD10_3DMODEL_DIR}/part.step")'))
    monkeypatch.setenv("KCT_LCSC_3D_DIR", str(tmp_path / "lcsc"))
    monkeypatch.delenv("KICAD_CONFIG_HOME", raising=False)

    def coverage(model_dir: Path) -> dict:
        for var in ("KICAD7", "KICAD8", "KICAD9", "KICAD10"):
            monkeypatch.setenv(f"{var}_3DMODEL_DIR", str(model_dir))
        out = tmp_path / f"report-{model_dir.name}.json"
        main([str(pcb), "--only", "model_coverage", "--format", "json", "--output", str(out)])
        capsys.readouterr()
        return json.loads(out.read_text())["coverage"]["model_coverage"]

    library = tmp_path / "library"
    library.mkdir()
    (library / "part.step").touch()
    empty_library = tmp_path / "empty-library"  # exists, lacks part.step
    empty_library.mkdir()
    entries = [coverage(d) for d in (library, tmp_path / "nonexistent", empty_library)]
    assert entries[0] == entries[1] == entries[2]
    assert entries[0]["state"] == "unknown"
    assert entries[0]["reason"] == _HOST_LIB_REASON.format(n=1)
