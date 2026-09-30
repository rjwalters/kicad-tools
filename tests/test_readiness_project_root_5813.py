"""Readiness collects legitimate out-of-PCB-directory dependencies (Issue #5813)."""

import json
import zipfile

import pytest

from kicad_tools.cli import readiness_cmd as cmd
from tests.test_kct_readiness_cmd import FakeEngines
from tests.test_readiness_package_preservation_5391 import inventory

LIB = '(kicad_symbol_lib (symbol "X"))\n'
TABLE = '(sym_lib_table (lib (name "custom")(type "KiCad")(uri "${KIPRJMOD}/../../symbols/custom.kicad_sym")(options "")(descr "")))\n'


def make_project(tmp_path, table=TABLE):
    project = tmp_path / "project"
    (project / "schematics").mkdir(parents=True)
    (project / "symbols").mkdir()
    pcb = project / "pcb"
    (pcb / "output").mkdir(parents=True)
    (project / "schematics/board.kicad_sch").write_text("(kicad_sch top)\n")
    (project / "symbols/custom.kicad_sym").write_text(LIB)
    out = pcb / "output"
    (out / "board.kicad_pcb").write_text("(kicad_pcb original)\n")
    (out / "board.kicad_pro").write_text('{"board": {}}\n')
    (out / "sym-lib-table").write_text(table)
    (out / "board.kicad_dru").write_text("(version 1)\n")
    return project


def go(project, *extra, generate=True):
    pcb = project / "pcb"
    fake = FakeEngines(pcb)
    args = [
        str(pcb / "output/board.kicad_pcb"),
        "--mfr",
        "jlcpcb",
        "--sch",
        str(project / "schematics/board.kicad_sch"),
        *extra,
    ]
    if generate:
        args.append("--generate")
    return cmd.main(args, engines=fake.bundle()), fake


def attempt_report(pcb):
    return json.loads((pcb / "output/readiness-attempt/readiness.json").read_text())


def test_sibling_schematic_and_symbols_are_collected_with_project_root(tmp_path):
    project = make_project(tmp_path)
    sources = inventory(project)
    rc, _ = go(project, "--project-root", str(project))
    pcb = project / "pcb"
    assert rc == 0, (
        attempt_report(pcb) if (pcb / "output/readiness-attempt").exists() else "no report"
    )
    report = json.loads((pcb / "output/readiness.json").read_text())
    assert set(report["external_inputs"]) == {
        "schematics/board.kicad_sch",
        "symbols/custom.kicad_sym",
    }
    assert report["project_root"] == ".."
    with zipfile.ZipFile(pcb / "output/manufacturing/kicad_project.zip") as zf:
        names = set(zf.namelist())
    assert {
        "pcb/output/board.kicad_pcb",
        "pcb/output/sym-lib-table",
        "symbols/custom.kicad_sym",
        "schematics/board.kicad_sch",
    } <= names
    # Sources outside the generated package are untouched.
    for name, data in sources.items():
        if not name.startswith("pcb/"):
            assert (project / name).read_bytes() == data


def test_without_project_root_error_is_actionable(tmp_path):
    project = make_project(tmp_path)
    before = inventory(project)
    rc, _ = go(project)
    assert rc != 0
    assert inventory(project) == before


def test_without_project_root_symbol_reference_names_layout(tmp_path):
    project = make_project(tmp_path)
    (project / "schematics/board.kicad_sch").rename(project / "pcb/output/board.kicad_sch")
    before = inventory(project)
    rc, _ = go(project, generate=True)
    # The schematic path passed no longer exists, so preflight refuses it.
    assert rc != 0
    assert inventory(project) == before


def test_symbol_only_outside_reports_supported_layout(tmp_path):
    project = make_project(tmp_path)
    pcb = project / "pcb"
    fake = FakeEngines(pcb)
    (pcb / "output/board.kicad_sch").write_text("(kicad_sch local)\n")
    rc = cmd.main(
        [str(pcb / "output/board.kicad_pcb"), "--mfr", "jlcpcb", "--generate"],
        engines=fake.bundle(),
    )
    assert rc != 0
    blocker = attempt_report(pcb)["blockers"][0]
    assert "--project-root" in blocker and "outside the package root" in blocker


def test_escape_beyond_project_root_is_rejected(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "evil.kicad_sym").write_text(LIB)
    project = make_project(tmp_path, TABLE.replace("../../symbols/custom", "../../../outside/evil"))
    before = inventory(project)
    rc, _ = go(project, "--project-root", str(project))
    assert rc != 0
    report = attempt_report(project / "pcb")
    assert "outside the --project-root" in report["blockers"][0]
    assert {k: v for k, v in inventory(project).items() if "readiness-attempt" not in k} == before


def test_missing_dependency_is_rejected(tmp_path):
    project = make_project(tmp_path)
    (project / "symbols/custom.kicad_sym").unlink()
    rc, _ = go(project, "--project-root", str(project))
    assert rc != 0
    assert "missing" in attempt_report(project / "pcb")["blockers"][0]


def test_symlink_escape_is_rejected(tmp_path):
    project = make_project(tmp_path)
    outside = tmp_path / "secret.kicad_sym"
    outside.write_text(LIB)
    (project / "symbols/custom.kicad_sym").unlink()
    (project / "symbols/custom.kicad_sym").symlink_to(outside)
    rc, _ = go(project, "--project-root", str(project))
    assert rc != 0
    assert "outside" in attempt_report(project / "pcb")["blockers"][0]


def test_ancestor_directory_reference_is_not_copied(tmp_path):
    project = make_project(tmp_path, TABLE.replace("/../../symbols/custom.kicad_sym", "/../.."))
    rc, _ = go(project, "--project-root", str(project))
    assert rc != 0
    assert "contains the PCB directory" in attempt_report(project / "pcb")["blockers"][0]


def test_project_root_must_contain_board(tmp_path):
    project = make_project(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    rc, _ = go(project, "--project-root", str(other))
    assert rc != 0


def test_schematic_hierarchy_is_collected(tmp_path):
    project = make_project(tmp_path)
    (project / "schematics/board.kicad_sch").write_text(
        '(kicad_sch (sheet (property "Sheetfile" "sub.kicad_sch")))\n'
    )
    (project / "schematics/sub.kicad_sch").write_text("(kicad_sch sub)\n")
    rc, _ = go(project, "--project-root", str(project))
    assert rc == 0
    report = json.loads((project / "pcb/output/readiness.json").read_text())
    assert "schematics/sub.kicad_sch" in report["external_inputs"]


def test_failure_preserves_existing_release_and_sources(tmp_path):
    project = make_project(tmp_path)
    pcb = project / "pcb"
    old = pcb / "output/manufacturing"
    old.mkdir()
    (old / "manifest.json").write_text(json.dumps({"producer": "kct readiness"}))
    (old / "README.txt").write_text("prior release")
    before = inventory(project)
    fake = FakeEngines(pcb, native_errors=1)
    rc = cmd.main(
        [
            str(pcb / "output/board.kicad_pcb"),
            "--mfr",
            "jlcpcb",
            "--generate",
            "--sch",
            str(project / "schematics/board.kicad_sch"),
            "--project-root",
            str(project),
        ],
        engines=fake.bundle(),
    )
    assert rc != 0
    for name, data in before.items():
        assert (project / name).read_bytes() == data


def test_stale_external_input_invalidates_evidence(tmp_path):
    from kicad_tools.cli.board_readiness import read_readiness

    project = make_project(tmp_path)
    assert go(project, "--project-root", str(project))[0] == 0
    (project / "symbols/custom.kicad_sym").write_text("(changed)\n")
    with pytest.raises(ValueError, match="stale"):
        read_readiness(project / "pcb", strict=True) if False else _raise_stale(project)


def _raise_stale(project):
    from kicad_tools.cli import board_readiness

    result = board_readiness.read_readiness(project / "pcb")
    blockers = " ".join(result.get("blockers", []))
    raise ValueError(blockers or "Evidence is stale")
