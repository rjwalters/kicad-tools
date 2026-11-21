"""Default silk-legalisation wiring across the pipeline (Issue #9799, #9795 phase 3).

``kct route`` / ``route-auto`` / ``optimize-placement`` / ``export`` run the
shared :func:`~kicad_tools.drc.repair_silkscreen.legalize_silk` pass on the
board they publish, after copper is written and before any gate judges silk.
The command output names the pass's result and every unresolved item; vias
are never clipped, moved or reported.  ``--no-silk-fix`` on each command
leaves authored silk byte-identical, and route-then-export is idempotent.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from kicad_tools._shapely import has_shapely

pytestmark = pytest.mark.skipif(not has_shapely(), reason="shapely not installed")

from kicad_tools.cli import main as kct_main  # noqa: E402
from kicad_tools.schema.pcb import PCB  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "projects"

#: Authored defects injected into the shared test-project board: a
#: sub-minimum silk line (jlcpcb floor 0.15mm), a via a silk line crosses
#: (vias are never touched, #9795) and an unclippable arc over a PTH drill.
DEFECTS = """
	(gr_line (start 105 55) (end 115 55) (stroke (width 0.08) (type default)) (layer "F.SilkS") (uuid "silk-thin"))
	(gr_line (start 146 57) (end 150 57) (stroke (width 0.15) (type default)) (layer "F.SilkS") (uuid "silk-over-via"))
	(via (at 148 57) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 2) (uuid "via-under-silk"))
	(footprint "Test:PTH" (layer "F.Cu") (uuid "fp-u1-uuid") (at 130 58)
		(property "Reference" "U9" (at 0 -2.2) (layer "F.SilkS") (uuid "ref-u9"))
		(property "Value" "PTH" (at 0 2.2) (layer "F.Fab") (uuid "val-u9"))
		(fp_arc (start -1 0) (mid 0 -1) (end 1 0) (stroke (width 0.15) (type default)) (layer "F.SilkS") (uuid "arc-unfix"))
		(pad "1" thru_hole circle (at 0 -1) (size 1.2 1.2) (drill 0.8) (layers "*.Cu") (net 1 "VIN") (uuid "pad-u9"))
	)
"""


def _defect_board(directory: Path, name: str = "board.kicad_pcb") -> Path:
    """The shared test project plus the authored silk defects above."""
    directory.mkdir(parents=True, exist_ok=True)
    for suffix in (".kicad_sch", ".kicad_pro"):
        shutil.copy(FIXTURE / f"test_project{suffix}", directory / f"board{suffix}")
    source = (FIXTURE / "test_project.kicad_pcb").read_text()
    closing = source.rstrip().rfind(")")
    pcb = directory / name
    pcb.write_text(source[:closing] + DEFECTS + source[closing:])
    return pcb


def _silk_lines(path: Path) -> list[tuple[str, float, float, float, float]]:
    """``(uuid, width, x1, y1, x2, y2)`` of every board-level F.SilkS line."""
    board = PCB.load(str(path))
    out = []
    for graphic in board.graphics:
        if graphic.layer == "F.SilkS" and graphic.graphic_type == "line":
            out.append(
                (
                    str(graphic.uuid),
                    float(graphic.stroke_width or 0.0),
                    graphic.start[0],
                    graphic.start[1],
                    graphic.end[0],
                    graphic.end[1],
                )
            )
    return out


def _silk_widths(path: Path) -> list[float]:
    """Stroke widths of every board-level F.SilkS line."""
    return [entry[1] for entry in _silk_lines(path)]


def _mock_assembly(monkeypatch, tmp_path: Path) -> None:
    """Replace AssemblyPackage.export so no kicad-cli / parts data is needed."""
    from kicad_tools.export import assembly

    def fake_export(self, output_dir=None):
        od = Path(output_dir) if output_dir else self.config.output_dir
        od.mkdir(parents=True, exist_ok=True)
        bom_path = od / f"bom_{self.manufacturer}.csv"
        bom_path.write_text("Comment,Designator,Footprint,LCSC Part #\n")
        cpl_path = od / f"cpl_{self.manufacturer}.csv"
        cpl_path.write_text("Designator,Val,Package,Mid X,Mid Y,Rotation,Layer\n")
        return assembly.AssemblyPackageResult(output_dir=od, bom_path=bom_path, pnp_path=cpl_path)

    monkeypatch.setattr(assembly.AssemblyPackage, "export", fake_export)


def _export_json(board: Path, out_dir: Path, capsys, *extra: str) -> dict:
    capsys.readouterr()
    assert (
        kct_main(
            [
                "export",
                str(board),
                "--mfr",
                "jlcpcb",
                "-o",
                str(out_dir),
                "--no-report",
                "--no-project-zip",
                "--skip-preflight",
                "--format",
                "json",
                *extra,
            ]
        )
        == 0
    )
    return json.loads(capsys.readouterr().out)


# ---------------------------------------------------------------------------
# kct route
# ---------------------------------------------------------------------------


def test_route_runs_pass_and_names_unresolved(tmp_path: Path, capsys):
    board = _defect_board(tmp_path / "run")
    out = tmp_path / "run" / "routed.kicad_pcb"

    rc = kct_main(["route", str(board), "-o", str(out), "--seed", "7", "--skip-drc"])

    assert rc == 0
    text = capsys.readouterr().out
    assert "--- Silk legalisation (default pass, jlcpcb) ---" in text
    assert "unresolved [silk_over_hole]" in text
    assert "fp_arc" in text  # the unfixable item is named

    widths = _silk_widths(out)
    assert widths  # the authored lines survived the write
    assert all(w >= 0.15 for w in widths)  # ...and meet the jlcpcb floor


def test_route_reports_no_unresolved_for_via_only_crossing(tmp_path: Path, capsys):
    board = _defect_board(tmp_path / "run")
    out = tmp_path / "run" / "routed.kicad_pcb"

    rc = kct_main(["route", str(board), "-o", str(out), "--seed", "7", "--skip-drc"])

    assert rc == 0
    text = capsys.readouterr().out
    unresolved = [line for line in text.splitlines() if "unresolved [" in line]
    # Only the PTH arc is unresolved: the via under silk is never reported.
    assert len(unresolved) == 1
    assert "via" not in unresolved[0]
    # ...and the silk line over the via kept its authored geometry (untouched).
    (over_via,) = [
        entry
        for entry in _silk_lines(out)
        if entry[2] == pytest.approx(146.0) and entry[3] == pytest.approx(57.0)
    ]
    assert over_via[1] == pytest.approx(0.15)


def test_route_no_silk_fix_leaves_authored_silk(tmp_path: Path, capsys):
    board = _defect_board(tmp_path / "run")
    out = tmp_path / "run" / "routed.kicad_pcb"

    rc = kct_main(
        ["route", str(board), "-o", str(out), "--seed", "7", "--skip-drc", "--no-silk-fix"]
    )

    assert rc == 0
    assert "Silk legalisation" not in capsys.readouterr().out
    # Authored silk records byte-identical: the sub-minimum line survives with
    # its authored geometry (only the requested copper was added).
    (thin,) = [entry for entry in _silk_lines(out) if entry[2] == pytest.approx(105.0)]
    assert thin[1] == pytest.approx(0.08)
    assert (thin[4], thin[5]) == (115.0, 55.0)


# ---------------------------------------------------------------------------
# Idempotence across the pipeline: route, then export
# ---------------------------------------------------------------------------


def test_route_then_export_reports_zero_additional_edits(tmp_path: Path, monkeypatch, capsys):
    board = _defect_board(tmp_path / "run")
    out = tmp_path / "run" / "routed.kicad_pcb"
    assert kct_main(["route", str(board), "-o", str(out), "--seed", "7", "--skip-drc"]) == 0
    routed_silk = sorted(_silk_lines(out))
    before = out.read_bytes()

    _mock_assembly(monkeypatch, tmp_path)
    doc = _export_json(out, tmp_path / "run" / "manufacturing", capsys)

    assert doc["silk_fix"]["applied"] is True
    assert doc["silk_fix"]["total_fixed"] == 0  # zero additional edits
    assert doc["silk_fix"]["unresolved"]  # the arc is still named, never waived
    assert sorted(_silk_lines(out)) == routed_silk  # routed silk preserved
    assert out.read_bytes() == before  # a zero-edit board is not re-saved


def test_export_repairs_authored_defect_in_place(tmp_path: Path, monkeypatch, capsys):
    board = _defect_board(tmp_path / "run")

    _mock_assembly(monkeypatch, tmp_path)
    doc = _export_json(board, tmp_path / "run" / "manufacturing", capsys)

    assert doc["silk_fix"]["applied"] is True
    assert doc["silk_fix"]["total_fixed"] >= 1
    assert doc["silk_fix"]["unresolved"]
    assert all(w >= 0.15 for w in _silk_widths(board))  # checks, plots and
    # hashes below read these same repaired bytes.


def test_export_no_silk_fix_leaves_board_byte_identical(tmp_path: Path, monkeypatch, capsys):
    board = _defect_board(tmp_path / "run")
    before = board.read_bytes()

    _mock_assembly(monkeypatch, tmp_path)
    doc = _export_json(board, tmp_path / "run" / "manufacturing", capsys, "--no-silk-fix")

    assert "silk_fix" not in doc
    assert board.read_bytes() == before  # authored silk byte-identical


def test_export_non_default_profile_min_width(tmp_path: Path, monkeypatch, capsys):
    """A profile whose silk minimum differs widens to ITS floor, not jlcpcb's."""
    board = _defect_board(tmp_path / "run")

    _mock_assembly(monkeypatch, tmp_path)
    capsys.readouterr()
    assert (
        kct_main(
            [
                "export",
                str(board),
                "--mfr",
                "oshpark",
                "-o",
                str(tmp_path / "run" / "mfg"),
                "--no-report",
                "--no-project-zip",
                "--skip-preflight",
                "--format",
                "json",
            ]
        )
        == 0
    )
    doc = json.loads(capsys.readouterr().out)
    assert doc["silk_fix"]["applied"] is True

    # oshpark's 2-layer minimum silk width is 0.127mm (jlcpcb's is 0.15).
    (thin,) = [entry for entry in _silk_lines(board) if entry[2] == pytest.approx(105.0)]
    assert thin[1] == pytest.approx(0.127)


# ---------------------------------------------------------------------------
# kct route-auto
# ---------------------------------------------------------------------------


def test_route_auto_legalizes_published_output(tmp_path: Path, capsys):
    board = _defect_board(tmp_path / "run")
    out = tmp_path / "run" / "auto.kicad_pcb"

    capsys.readouterr()
    rc = kct_main(["route-auto", str(board), "--net", "NET1", "-o", str(out), "--format", "json"])

    assert rc == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["silk_fix"]["applied"] is True
    assert doc["silk_fix"]["total_fixed"] >= 1
    assert [u["kind"] for u in doc["silk_fix"]["unresolved"]] == ["silk_over_hole"]
    assert all(w >= 0.15 for w in _silk_widths(out))


def test_route_auto_no_silk_fix_preserves_authored_silk(tmp_path: Path, capsys):
    board = _defect_board(tmp_path / "run")
    out = tmp_path / "run" / "auto.kicad_pcb"

    capsys.readouterr()
    rc = kct_main(
        [
            "route-auto",
            str(board),
            "--net",
            "NET1",
            "-o",
            str(out),
            "--format",
            "json",
            "--no-silk-fix",
        ]
    )

    assert rc == 0
    doc = json.loads(capsys.readouterr().out)
    assert "silk_fix" not in doc
    (thin,) = [entry for entry in _silk_lines(out) if entry[2] == pytest.approx(105.0)]
    assert thin[1] == pytest.approx(0.08)


def test_route_auto_without_output_leaves_input_alone(tmp_path: Path, capsys):
    """A run with no board to publish must leave the input alone."""
    board = _defect_board(tmp_path / "run")
    before = board.read_bytes()

    capsys.readouterr()
    rc = kct_main(["route-auto", str(board), "--net", "NET1", "--format", "json"])

    assert rc == 0
    doc = json.loads(capsys.readouterr().out)
    assert "silk_fix" not in doc
    assert board.read_bytes() == before


# ---------------------------------------------------------------------------
# kct optimize-placement
# ---------------------------------------------------------------------------


def test_optimize_placement_legalizes_written_board(tmp_path: Path, capsys):
    board = _defect_board(tmp_path / "run")
    out = tmp_path / "run" / "placed.kicad_pcb"

    rc = kct_main(["optimize-placement", str(board), "-o", str(out), "--max-iterations", "3"])

    assert rc == 0
    text = capsys.readouterr().out
    assert "--- Silk legalisation (default pass" in text
    assert "unresolved [silk_over_hole]" in text
    assert all(w >= 0.15 for w in _silk_widths(out))


def test_optimize_placement_no_silk_fix_bypasses_silk_edits(tmp_path: Path, capsys):
    board = _defect_board(tmp_path / "run")
    out = tmp_path / "run" / "placed.kicad_pcb"

    rc = kct_main(
        [
            "optimize-placement",
            str(board),
            "-o",
            str(out),
            "--max-iterations",
            "3",
            "--no-silk-fix",
        ]
    )

    assert rc == 0
    assert "Silk legalisation" not in capsys.readouterr().out
    # The opt-out bypasses the reference-move repairs too: the authored
    # sub-minimum line survives byte-identical (placement moved, silk not).
    (thin,) = [entry for entry in _silk_lines(out) if entry[2] == pytest.approx(105.0)]
    assert thin[1] == pytest.approx(0.08)


def test_optimize_placement_json_names_the_pass(tmp_path: Path, capsys):
    board = _defect_board(tmp_path / "run")
    out = tmp_path / "run" / "placed.kicad_pcb"

    capsys.readouterr()
    rc = kct_main(
        [
            "optimize-placement",
            str(board),
            "-o",
            str(out),
            "--max-iterations",
            "3",
            "--format",
            "json",
        ]
    )

    assert rc == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["silk_fix"]["applied"] is True
    assert doc["silk_fix"]["total_fixed"] >= 1
    assert doc["silk_fix"]["unresolved"]


# ---------------------------------------------------------------------------
# kct readiness --generate: the pass runs on the staged candidate BEFORE the
# gates judge silk, so checks, plots and hashes consume the repaired bytes.
# ---------------------------------------------------------------------------


def _readiness_board(tmp_path: Path) -> tuple[Path, Path]:
    """A ready board directory (fake engines) holding the real defect PCB."""
    from tests.test_kct_readiness_cmd import make_board

    board_dir = make_board(tmp_path)
    pcb = board_dir / "output" / "demo_routed.kicad_pcb"
    source = (FIXTURE / "test_project.kicad_pcb").read_text()
    closing = source.rstrip().rfind(")")
    pcb.write_text(source[:closing] + DEFECTS + source[closing:])
    return board_dir, pcb


def _preserving_engines(board_dir: Path):
    """FakeEngines whose refill keeps the candidate bytes (#9799).

    The shared fake's refill replaces the board with a stub line, which is
    fine for gate-order tests but leaves the default silk pass nothing real
    to legalise.  This subclass refills as a no-op and reports the board's
    own (empty) filled areas, so the zone_fill gate still passes.
    """
    from tests.test_kct_readiness_cmd import FakeEngines

    class PreservingEngines(FakeEngines):
        def refill(self, pcb: Path):
            self.calls.append("refill")
            from kicad_tools.cli.readiness_cmd import EngineRun

            return EngineRun(ok=True)

        def fill_areas(self, pcb: Path) -> dict[str, float]:
            return {}

    return PreservingEngines(board_dir)


def test_readiness_generate_legalizes_staged_candidate(tmp_path: Path):
    from tests.test_kct_readiness_cmd import run

    board_dir, pcb = _readiness_board(tmp_path)
    fake = _preserving_engines(board_dir)

    code, report = run(board_dir, fake)

    assert code == 0
    assert report["status"] == "ready"
    assert report["metrics"]["silk_fix"]["applied"] is True
    assert report["metrics"]["silk_fix"]["total_fixed"] >= 1
    assert [u["kind"] for u in report["metrics"]["silk_fix"]["unresolved"]] == ["silk_over_hole"]
    # The published board carries the repaired geometry the checks, plots and
    # hashes consumed.
    assert all(w >= 0.15 for w in _silk_widths(pcb))


def test_readiness_generate_no_silk_fix_keeps_candidate_silk(tmp_path: Path):
    from tests.test_kct_readiness_cmd import run

    board_dir, pcb = _readiness_board(tmp_path)

    code, report = run(board_dir, _preserving_engines(board_dir), "--no-silk-fix")

    assert code == 0
    assert report["status"] == "ready"
    assert report["metrics"]["silk_fix"] is None
    (thin,) = [entry for entry in _silk_lines(pcb) if entry[2] == pytest.approx(105.0)]
    assert thin[1] == pytest.approx(0.08)  # authored silk byte-identical
