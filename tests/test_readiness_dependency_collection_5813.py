"""Readiness collects legitimate project inputs from outside the PCB directory.

Issue #5813: a schematic in a sibling ``schematics/`` directory and a custom
symbol library in a sibling ``symbols/`` directory are both valid KiCad project
inputs, but the snapshot/remap step rejected them before any useful gate ran:

    Readiness candidate was not published: '<project>/output_revc/board.kicad_sch'
      is not in the subpath of '<project>/manufacturing_attempt'
    Readiness candidate was not published: Project dependency is outside the
      package root: ${KIPRJMOD}/../symbols/custom.kicad_sym

These tests reproduce both shapes, prove they now succeed, and — in the same
file, because the two must never drift apart — prove that the containment
protections the collection is built on top of (#5391 / PR #5394) still refuse
missing files, undeclared out-of-root dependencies, symlink escapes and
unrepresentable ("ambiguous") mappings, leaving the sources untouched.
"""

from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

import pytest

from kicad_tools.cli import readiness_cmd as cmd
from tests.test_kct_readiness_cmd import FakeEngines

PCB_NAME = "board_routed.kicad_pcb"
PRO_NAME = "board_routed.kicad_pro"
OUTSIDE_URI = "${KIPRJMOD}/../symbols/custom.kicad_sym"


def sym_lib_table(uri: str) -> str:
    return (
        "(sym_lib_table\n"
        f'  (lib (name "custom") (type "KiCad") (uri "{uri}") (options "") (descr ""))\n'
        ")\n"
    )


def make_consumer_project(tmp_path: Path, *, uri: str = OUTSIDE_URI, nest: str = "") -> Path:
    """The layout from the issue: schematics/, symbols/ and pcb/ as siblings."""
    project = tmp_path / nest / "project" if nest else tmp_path / "project"
    board = project / "pcb"
    (board / "output").mkdir(parents=True)
    (project / "schematics").mkdir()
    (project / "symbols").mkdir()
    (board / PCB_NAME).write_text("(kicad_pcb original)\n")
    (board / PRO_NAME).write_text('{"board": {}}\n')
    (board / "sym-lib-table").write_text(sym_lib_table(uri))
    (project / "schematics" / "board.kicad_sch").write_text("(kicad_sch demo)\n")
    (project / "symbols" / "custom.kicad_sym").write_text("(kicad_symbol_lib custom)\n")
    return project


def inventory(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def run(
    project: Path, *extra: str, generate: bool = True, schematic: str | None = "schematics"
) -> tuple[int, dict]:
    """Run readiness on ``project/pcb`` and return ``(exit_code, report)``."""
    board = project / "pcb"
    argv = [str(board), "--mfr", "jlcpcb", "-o", str(board / "output" / "manufacturing")]
    if generate:
        argv.append("--generate")
    if schematic is not None:
        argv += ["--sch", str(project / schematic / "board.kicad_sch")]
    argv += list(extra)
    code = cmd.main(argv, engines=FakeEngines(board).bundle())
    name = "readiness.json" if code == 0 and generate else None
    if name is None:
        name = (
            "readiness-attempt/readiness.json"
            if generate
            else "readiness-verification/readiness.json"
        )
    report_path = board / "output" / name
    return code, json.loads(report_path.read_text()) if report_path.is_file() else {}


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# The two reproductions from the issue
# ---------------------------------------------------------------------------


def test_sibling_schematic_and_symbol_library_are_collected(tmp_path):
    """Both failures from the issue body are gone, and the evidence binds both files."""
    project = make_consumer_project(tmp_path)

    code, report = run(project)

    assert report["blockers"] == []
    assert code == 0
    assert report["status"] == "ready"

    collected_sch = "kct-collected/schematics/board.kicad_sch"
    collected_sym = "kct-collected/symbols/custom.kicad_sym"
    # Hashed exactly like an in-root file: same `inputs` map, same digest.
    assert report["inputs"][collected_sch] == digest(project / "schematics/board.kicad_sch")
    assert report["inputs"][collected_sym] == digest(project / "symbols/custom.kicad_sym")
    assert {record["source"] for record in report["collected_dependencies"]} == {
        "schematics/board.kicad_sch",
        "symbols/custom.kicad_sym",
    }
    for record in report["collected_dependencies"]:
        assert report["inputs"][record["path"]] == record["sha256"]

    board = project / "pcb"
    assert (board / collected_sch).read_text() == "(kicad_sch demo)\n"
    assert (board / collected_sym).read_text() == "(kicad_symbol_lib custom)\n"


def test_exported_project_opens_without_the_surrounding_checkout(tmp_path):
    """The staged package carries the collected copies and names them itself."""
    project = make_consumer_project(tmp_path)

    assert run(project)[0] == 0

    archive = project / "pcb/output/manufacturing/kicad_project.zip"
    with zipfile.ZipFile(archive) as zf:
        names = set(zf.namelist())
        table = zf.read("sym-lib-table").decode()
    assert "kct-collected/symbols/custom.kicad_sym" in names
    assert "kct-collected/schematics/board.kicad_sch" in names
    # Rewritten inside the package only, so the export resolves standalone...
    assert '"${KIPRJMOD}/kct-collected/symbols/custom.kicad_sym"' in table
    # ...while the checkout keeps pointing at the shared library it declared, so
    # a later edit there is still seen (and re-collected) by the next run.
    assert OUTSIDE_URI in (project / "pcb/sym-lib-table").read_text()


def test_collection_never_rewrites_the_named_sources(tmp_path):
    project = make_consumer_project(tmp_path)
    outside = {
        name: data
        for name, data in inventory(project).items()
        if name.startswith(("schematics/", "symbols/"))
    }

    assert run(project)[0] == 0

    for name, data in outside.items():
        assert (project / name).read_bytes() == data


def test_generated_package_verifies_without_changing_shipped_bytes(tmp_path):
    project = make_consumer_project(tmp_path)
    assert run(project)[0] == 0
    before = inventory(project)

    code, report = run(project, generate=False)

    assert (code, report["status"]) == (0, "ready")
    # Verification adds its own diagnostics directory, but may not touch a byte
    # of the shipped release or of the collected sources it re-checked.
    for name, data in before.items():
        assert (project / name).read_bytes() == data


def test_edited_shared_library_is_re_collected_not_stale(tmp_path):
    project = make_consumer_project(tmp_path)
    assert run(project)[0] == 0

    (project / "symbols/custom.kicad_sym").write_text("(kicad_symbol_lib custom v2)\n")
    code, report = run(project)

    assert code == 0
    collected = project / "pcb/kct-collected/symbols/custom.kicad_sym"
    assert collected.read_text() == "(kicad_symbol_lib custom v2)\n"
    assert report["inputs"]["kct-collected/symbols/custom.kicad_sym"] == digest(collected)


def test_schematic_hierarchy_is_collected_with_its_sub_sheets(tmp_path):
    project = make_consumer_project(tmp_path)
    (project / "schematics/sub").mkdir()
    (project / "schematics/sub/child.kicad_sch").write_text("(kicad_sch child)\n")
    (project / "schematics/board.kicad_sch").write_text(
        '(kicad_sch demo (sheet (property "Sheetfile" "sub/child.kicad_sch" (at 0 0 0))))\n'
    )

    code, report = run(project)

    assert code == 0
    child = "kct-collected/schematics/sub/child.kicad_sch"
    assert report["inputs"][child] == digest(project / "schematics/sub/child.kicad_sch")
    # The sub-sheet keeps its position relative to the sheet that names it.
    assert (project / "pcb" / child).is_file()


# ---------------------------------------------------------------------------
# Containment: the wider root must not weaken any existing refusal
# ---------------------------------------------------------------------------


def preflight_error(project: Path, *extra: str, schematic: str | None = "schematics") -> str:
    """Run readiness expecting a refusal *before* anything is written."""
    board = project / "pcb"
    before = inventory(project)
    argv = [str(board), "--mfr", "jlcpcb", "--generate"]
    if schematic is not None:
        argv += ["--sch", str(project / schematic / "board.kicad_sch")]
    argv += list(extra)
    args = cmd.build_parser().parse_args(argv)
    options, error = cmd.resolve_options(args)
    assert options is None, "expected a refusal"
    assert cmd.main(argv, engines=FakeEngines(board).bundle()) == 2
    assert inventory(project) == before, "a refused run must not touch the sources"
    return str(error)


def test_dependency_outside_the_project_root_is_still_refused(tmp_path):
    project = make_consumer_project(tmp_path, uri="${KIPRJMOD}/../../outside/evil.kicad_sym")
    (tmp_path / "outside").mkdir()
    (tmp_path / "outside/evil.kicad_sym").write_text("(kicad_symbol_lib evil)\n")

    error = preflight_error(project)

    assert "outside the package root" in error
    assert "--project-root" in error


def test_symlink_escape_from_inside_the_project_root_is_still_refused(tmp_path):
    project = make_consumer_project(tmp_path)
    (tmp_path / "outside").mkdir()
    (tmp_path / "outside/custom.kicad_sym").write_text("(kicad_symbol_lib evil)\n")
    (project / "symbols/custom.kicad_sym").unlink()
    (project / "symbols/custom.kicad_sym").symlink_to(tmp_path / "outside/custom.kicad_sym")

    error = preflight_error(project)

    assert "outside the package root" in error


def test_missing_dependency_is_still_refused(tmp_path):
    project = make_consumer_project(tmp_path, uri="${KIPRJMOD}/../symbols/absent.kicad_sym")

    assert "Project dependency is missing" in preflight_error(project)


def test_undeclared_out_of_root_dependency_is_refused_without_a_named_input(tmp_path):
    """No named input, no widening: the board directory stays the boundary."""
    project = make_consumer_project(tmp_path)
    (project / "pcb/board.kicad_sch").write_text("(kicad_sch demo)\n")

    error = preflight_error(project, schematic=None)

    assert "outside the package root" in error


def test_sheet_reference_back_into_the_pcb_directory_is_refused_as_ambiguous(tmp_path):
    project = make_consumer_project(tmp_path)
    (project / "pcb/sub.kicad_sch").write_text("(kicad_sch sub)\n")
    (project / "schematics/board.kicad_sch").write_text(
        '(kicad_sch demo (sheet (property "Sheetfile" "../pcb/sub.kicad_sch" (at 0 0 0))))\n'
    )

    error = preflight_error(project)

    assert "Ambiguous dependency mapping" in error


def test_named_input_too_far_outside_needs_an_explicit_project_root(tmp_path):
    project = make_consumer_project(tmp_path, nest="w/x")
    remote = tmp_path / "remote"
    remote.mkdir(parents=True)
    (remote / "board.kicad_sch").write_text("(kicad_sch demo)\n")
    board = project / "pcb"
    argv = [
        str(board),
        "--mfr",
        "jlcpcb",
        "--generate",
        "--sch",
        str(remote / "board.kicad_sch"),
    ]
    before = inventory(project)

    _options, error = cmd.resolve_options(cmd.build_parser().parse_args(argv))

    assert "--project-root" in str(error)
    assert inventory(project) == before


def test_explicit_project_root_must_contain_the_board(tmp_path):
    project = make_consumer_project(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    error = preflight_error(project, "--project-root", str(elsewhere))

    assert "does not contain the board directory" in error


def test_explicit_project_root_widens_beyond_the_implicit_limit(tmp_path):
    """The escape hatch the refusal names actually works."""
    project = make_consumer_project(tmp_path, nest="w/x")
    remote = tmp_path / "shared/rev_c/schematics"
    remote.mkdir(parents=True)
    (remote / "board.kicad_sch").write_text("(kicad_sch demo)\n")
    board = project / "pcb"

    code = cmd.main(
        [
            str(board),
            "--mfr",
            "jlcpcb",
            "--generate",
            "-o",
            str(board / "output" / "manufacturing"),
            "--sch",
            str(remote / "board.kicad_sch"),
            "--project-root",
            str(tmp_path),
        ],
        engines=FakeEngines(board).bundle(),
    )

    assert code == 0
    report = json.loads((board / "output/readiness.json").read_text())
    collected = "kct-collected/shared/rev_c/schematics/board.kicad_sch"
    assert report["inputs"][collected] == digest(remote / "board.kicad_sch")


def test_failed_generation_with_collection_preserves_the_previous_release(tmp_path):
    """PR #5394's transactional preservation still holds on the collection path."""
    project = make_consumer_project(tmp_path)
    assert run(project)[0] == 0
    before = inventory(project)
    board = project / "pcb"

    code = cmd.main(
        [
            str(board),
            "--mfr",
            "jlcpcb",
            "--generate",
            "-o",
            str(board / "output" / "manufacturing"),
            "--sch",
            str(project / "schematics/board.kicad_sch"),
        ],
        engines=FakeEngines(board, native_errors=1).bundle(),
    )

    assert code != 0
    for name, data in before.items():
        assert (project / name).read_bytes() == data


@pytest.mark.parametrize("uri", ["${KIPRJMOD}/local.kicad_sym", "local.kicad_sym"])
def test_in_directory_dependencies_are_untouched_by_collection(tmp_path, uri):
    """A library beside the PCB is packaged as before — no collected copy at all."""
    project = make_consumer_project(tmp_path, uri=uri)
    (project / "pcb/local.kicad_sym").write_text("(kicad_symbol_lib local)\n")

    code, report = run(project)

    assert code == 0
    assert report["inputs"]["local.kicad_sym"] == digest(project / "pcb/local.kicad_sym")
    assert not any(name.startswith("kct-collected/symbols") for name in report["inputs"])
