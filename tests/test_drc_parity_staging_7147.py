"""``kct drc`` schematic parity on ``<base>_routed.kicad_pcb`` boards (Issue #7147).

kicad-cli 10.0.6 ``pcb drc --schematic-parity`` has no ``--schematic`` option:
it only reads ``<pcb-stem>.kicad_sch`` next to the board.  Every fleet board
signs off ``<base>_routed.kicad_pcb`` while the schematic is
``<base>.kicad_sch``, so parity never ran and ``kct drc`` exited 2 (NOT RUN).

``run_drc`` now resolves the schematic (``resolve_schematic_for_pcb`` strips
``_routed``, or ``--schematic PATH``) and, when it is not already the board's
own ``<stem>.kicad_sch``, runs kicad-cli on a temporary mirror of the board
folder with the schematic linked under the board's name.

Most tests mock the kicad-cli subprocess; the integration tests at the bottom
run the real kicad-cli on scratch copies of boards 00 and 02.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from kicad_tools.cli import drc_cmd, runner
from kicad_tools.cli import main as cli_main
from kicad_tools.drc.parity import (
    PARITY_FAILED,
    PARITY_NOT_RUN,
    PARITY_PASSED,
    parity_reason_ambiguous,
    parity_reason_netlist_failed,
    parity_reason_no_schematic,
)

PARITY_LINE = (
    "Failed to fetch schematic netlist for parity tests.\n"
    "Schematic parity tests require a fully annotated schematic.\n"
)

REPORT = {
    "source": "foo_routed.kicad_pcb",
    "date": "2026-10-18T00:00:00+0000",
    "kicad_version": "10.0.6",
    "coordinate_units": "mm",
    "violations": [],
    "unconnected_items": [],
    "schematic_parity": [],
}

PARITY_WARNING = {
    "description": "Pad net (LED_ANODE) doesn't match net given by schematic (/LED_ANODE)",
    "items": [{"description": "PTH pad 2 [LED_ANODE] of D1", "pos": {"x": 1.0, "y": 2.0}}],
    "severity": "warning",
    "type": "net_conflict",
}


class FakeCli:
    """Patched kicad-cli: records the PCB path and a snapshot of its folder."""

    def __init__(self, monkeypatch, *, stdout: str = "", parity=None):
        self.pcbs: list[Path] = []
        self.listings: list[dict[str, Path | None]] = []
        self.stdout = stdout
        self.parity = list(parity or [])
        monkeypatch.setattr(runner, "run_kicad_drc_command", self._run)
        monkeypatch.setattr(runner, "find_kicad_cli", lambda: Path("/fake/kicad-cli"))
        monkeypatch.setattr(drc_cmd, "find_kicad_cli", lambda: Path("/fake/kicad-cli"))

    def _run(self, build_cmd, kicad_cli, *, refill_zones, timeout=None, text=False):
        argv = build_cmd(False)
        pcb = Path(argv[-1])
        self.pcbs.append(pcb)
        self.listings.append(
            {
                e.name: (e.resolve() if e.is_symlink() else None)
                for e in sorted(pcb.parent.iterdir())
            }
        )
        out = Path(argv[argv.index("--output") + 1])
        out.write_text(json.dumps(dict(REPORT, schematic_parity=self.parity)))
        return subprocess.CompletedProcess(argv, 0, stdout=self.stdout, stderr=""), False


@pytest.fixture
def board(tmp_path: Path) -> Path:
    """A routed board folder: ``foo_routed.kicad_pcb`` + ``foo.kicad_sch`` + siblings."""
    d = tmp_path / "output"
    d.mkdir()
    (d / "foo_routed.kicad_pcb").write_text("(kicad_pcb (version 20240108))\n")
    (d / "foo.kicad_sch").write_text("(kicad_sch)\n")
    (d / "foo.kicad_pro").write_text("{}\n")
    (d / "foo_routed.kicad_dru").write_text("(version 1)\n")
    (d / "foo_routed.kicad_prl").write_text("{}\n")
    (d / "footprints").mkdir()
    (d / "footprints" / "X.kicad_mod").write_text("(footprint X)\n")
    return d / "foo_routed.kicad_pcb"


# --- run_drc staging ----------------------------------------------------------


def test_routed_board_is_staged_with_schematic_under_board_name(monkeypatch, board):
    cli = FakeCli(monkeypatch)
    result = runner.run_drc(board)

    staged_pcb = cli.pcbs[0]
    assert staged_pcb.parent != board.parent
    assert staged_pcb.name == board.name
    listing = cli.listings[0]
    assert listing["foo_routed.kicad_sch"] == (board.parent / "foo.kicad_sch").resolve()
    # Every other sibling is mirrored (``${KIPRJMOD}`` is the staging dir) ...
    for name in ("foo_routed.kicad_pcb", "foo.kicad_sch", "foo.kicad_pro", "footprints"):
        assert name in listing, name
    assert listing["foo_routed.kicad_dru"] == (board.parent / "foo_routed.kicad_dru").resolve()
    # ... except the per-run ``.kicad_prl`` kicad-cli writes.
    assert "foo_routed.kicad_prl" not in listing
    # The staging dir is gone afterwards.
    assert not staged_pcb.parent.exists()

    assert result.success and result.parity_skipped is False
    assert result.parity_schematic == board.parent / "foo.kicad_sch"
    assert result.parity_skip_reason is None


def test_staging_setup_error_reports_parity_not_run(monkeypatch, board):
    cli = FakeCli(monkeypatch)

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(runner, "stage_pcb_for_parity", boom)
    result = runner.run_drc(board)

    assert cli.pcbs == [board]
    assert result.parity_skipped is True
    assert "disk full" in (result.parity_skip_reason or "")


def test_board_folder_is_never_written(monkeypatch, board):
    before = sorted(p.name for p in board.parent.iterdir())
    FakeCli(monkeypatch)
    runner.run_drc(board)
    assert sorted(p.name for p in board.parent.iterdir()) == before


def test_native_stem_schematic_is_not_staged(monkeypatch, board):
    native = board.with_suffix(".kicad_sch")
    native.write_text("(kicad_sch)\n")
    (board.parent / "foo.kicad_sch").unlink()
    cli = FakeCli(monkeypatch)
    result = runner.run_drc(board)
    assert cli.pcbs == [board]
    assert result.parity_schematic == native


def test_explicit_schematic_wins_over_native_sibling(monkeypatch, board, tmp_path):
    board.with_suffix(".kicad_sch").write_text("(kicad_sch native)\n")
    other_dir = tmp_path / "elsewhere"
    other_dir.mkdir()
    explicit = other_dir / "top.kicad_sch"
    explicit.write_text("(kicad_sch top)\n")
    (other_dir / "child.kicad_sch").write_text("(kicad_sch child)\n")
    (other_dir / "foo.kicad_sch").write_text("(kicad_sch collision)\n")

    cli = FakeCli(monkeypatch)
    result = runner.run_drc(board, schematic=explicit)

    assert cli.pcbs[0] != board
    listing = cli.listings[0]
    assert listing["foo_routed.kicad_sch"] == explicit.resolve()
    # Sub-sheets of an out-of-folder schematic are linked so sheet refs resolve,
    # without shadowing a board-folder file of the same name.
    assert listing["child.kicad_sch"] == (other_dir / "child.kicad_sch").resolve()
    assert listing["foo.kicad_sch"] == (board.parent / "foo.kicad_sch").resolve()
    assert result.parity_schematic == explicit


def test_no_schematic_is_not_staged_and_names_the_cause(monkeypatch, board):
    (board.parent / "foo.kicad_sch").unlink()
    cli = FakeCli(monkeypatch, stdout=PARITY_LINE)
    result = runner.run_drc(board)
    assert cli.pcbs == [board]
    assert result.parity_skipped is True
    assert result.parity_schematic is None
    assert result.parity_skip_reason == parity_reason_no_schematic(board.name)
    assert "--schematic PATH" in result.parity_skip_reason


def test_ambiguous_schematics_name_the_cause(monkeypatch, tmp_path):
    pcb = tmp_path / "board_v2.kicad_pcb"
    pcb.write_text("(kicad_pcb)\n")
    for stem in ("a", "b"):
        (tmp_path / f"{stem}.kicad_pro").write_text("{}\n")
        (tmp_path / f"{stem}.kicad_sch").write_text("(kicad_sch)\n")
    FakeCli(monkeypatch, stdout=PARITY_LINE)
    result = runner.run_drc(pcb)
    assert result.parity_skip_reason == parity_reason_ambiguous(pcb.name)


def test_staged_but_netlist_failed_names_the_schematic(monkeypatch, board):
    FakeCli(monkeypatch, stdout=PARITY_LINE)
    result = runner.run_drc(board)
    sch = board.parent / "foo.kicad_sch"
    assert result.parity_skipped is True
    assert result.parity_skip_reason == parity_reason_netlist_failed(str(sch))
    assert "fully annotated" in result.parity_skip_reason


def test_parity_off_never_resolves_or_stages(monkeypatch, board):
    import kicad_tools.sync.discover as discover

    def boom(*_a, **_k):
        raise AssertionError("schematic must not be resolved without parity")

    monkeypatch.setattr(discover, "resolve_schematic_for_pcb", boom)
    cli = FakeCli(monkeypatch)
    result = runner.run_drc(board, schematic_parity=False)
    assert cli.pcbs == [board]
    assert result.parity_schematic is None


def test_copy_fallback_when_symlinks_unavailable(monkeypatch, board):
    def no_symlink(self, *_a, **_k):
        raise OSError("symlinks not permitted")

    monkeypatch.setattr(Path, "symlink_to", no_symlink)
    cli = FakeCli(monkeypatch)
    result = runner.run_drc(board)
    listing = cli.listings[0]
    assert "foo_routed.kicad_sch" in listing and listing["foo_routed.kicad_sch"] is None
    assert "footprints" in listing
    assert result.parity_schematic == board.parent / "foo.kicad_sch"


# --- kct drc CLI --------------------------------------------------------------


def test_cli_json_records_schematic_used(monkeypatch, capsys, board):
    FakeCli(monkeypatch, parity=[PARITY_WARNING])
    rc = drc_cmd.main([str(board), "--format", "json"])
    data = json.loads(capsys.readouterr().out)
    assert rc == 0  # warning-only parity passes by default
    assert data["schematic_parity"]["status"] == PARITY_FAILED
    assert data["schematic_parity"]["schematic"] == str(board.parent / "foo.kicad_sch")


def test_cli_warning_only_parity_exits_2_under_strict(monkeypatch, board):
    FakeCli(monkeypatch, parity=[PARITY_WARNING])
    assert drc_cmd.main([str(board)]) == 0
    assert drc_cmd.main([str(board), "--strict"]) == 2


def test_cli_explicit_schematic_is_used(monkeypatch, capsys, board, tmp_path):
    explicit = tmp_path / "chosen.kicad_sch"
    explicit.write_text("(kicad_sch)\n")
    cli = FakeCli(monkeypatch)
    rc = drc_cmd.main([str(board), "--format", "json", "--schematic", str(explicit)])
    data = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert cli.listings[0]["foo_routed.kicad_sch"] == explicit.resolve()
    assert data["schematic_parity"]["status"] == PARITY_PASSED
    assert data["schematic_parity"]["schematic"] == str(explicit)


def test_cli_missing_explicit_schematic_exits_1(monkeypatch, capsys, board, tmp_path):
    cli = FakeCli(monkeypatch)
    rc = drc_cmd.main([str(board), "--schematic", str(tmp_path / "nope.kicad_sch")])
    assert rc == 1
    assert cli.pcbs == []
    assert "schematic not found" in capsys.readouterr().err


def test_cli_schematic_ignored_for_report_input(monkeypatch, capsys, tmp_path):
    report = tmp_path / "r.json"
    report.write_text(json.dumps(REPORT))
    rc = drc_cmd.main([str(report), "--schematic", str(tmp_path / "x.kicad_sch")])
    assert rc == 2  # unchanged report semantics (NOT RUN: unknown)
    assert "--schematic is ignored for report input" in capsys.readouterr().err


def test_cli_no_schematic_reason_and_exit(monkeypatch, capsys, board):
    (board.parent / "foo.kicad_sch").unlink()
    FakeCli(monkeypatch, stdout=PARITY_LINE)
    rc = drc_cmd.main([str(board)])
    out = capsys.readouterr().out
    assert rc == 2  # the #7077 rule is unchanged
    line = next(ln for ln in out.splitlines() if ln.startswith("Schematic parity:"))
    assert line == f"Schematic parity: NOT RUN ({parity_reason_no_schematic(board.name)})"
    assert "next to the board" not in out
    assert drc_cmd.main([str(board), "--allow-incomplete"]) == 0


def test_kct_drc_forwards_schematic(monkeypatch, board, tmp_path):
    explicit = tmp_path / "chosen.kicad_sch"
    explicit.write_text("(kicad_sch)\n")
    cli = FakeCli(monkeypatch)
    assert cli_main(["drc", str(board), "--schematic", str(explicit)]) == 0
    assert cli.listings[0]["foo_routed.kicad_sch"] == explicit.resolve()


# --- real kicad-cli -----------------------------------------------------------

BOARDS = Path(__file__).resolve().parents[1] / "boards"


def _snapshot(folder: Path) -> dict[str, tuple[str, int]]:
    """Name -> (content hash, mtime_ns) for everything under ``folder``."""
    import hashlib

    snap: dict[str, tuple[str, int]] = {}
    for p in sorted(folder.rglob("*")):
        rel = str(p.relative_to(folder))
        if p.is_file():
            snap[rel] = (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
        else:
            snap[rel] = ("dir", 0)
    return snap


def _board_copy(tmp_path: Path, board: str) -> Path:
    if runner.find_kicad_cli() is None:
        pytest.skip("kicad-cli not installed")
    dst = tmp_path / board / "output"
    shutil.copytree(
        BOARDS / board / "output",
        dst,
        ignore=shutil.ignore_patterns("*.kicad_prl", "fp-info-cache", "*-backups"),
    )
    pcbs = sorted(dst.glob("*_routed.kicad_pcb"))
    assert len(pcbs) == 1
    return pcbs[0]


@pytest.mark.integration
def test_real_board02_routed_parity_passes(tmp_path, capsys):
    pcb = _board_copy(tmp_path, "02-charlieplex-led")
    before = _snapshot(pcb.parent)
    rc = drc_cmd.main([str(pcb), "--format", "json"])
    data = json.loads(capsys.readouterr().out)
    assert data["schematic_parity"]["status"] == PARITY_PASSED
    assert data["schematic_parity"]["schematic"] == str(pcb.parent / "charlieplex_3x3.kicad_sch")
    assert rc == 0
    # Staging must never write into the board folder.
    assert _snapshot(pcb.parent) == before


@pytest.mark.integration
def test_real_board00_routed_parity_warnings(tmp_path, capsys):
    pcb = _board_copy(tmp_path, "00-simple-led")
    before = _snapshot(pcb.parent)
    rc = drc_cmd.main([str(pcb), "--format", "json"])
    data = json.loads(capsys.readouterr().out)
    parity = data["schematic_parity"]
    assert parity["status"] == PARITY_FAILED
    assert (parity["error_count"], parity["warning_count"]) == (0, 2)
    assert rc == 0
    assert drc_cmd.main([str(pcb), "--strict"]) == 2
    assert parity["status"] != PARITY_NOT_RUN
    # Staging must never write into the board folder.
    assert _snapshot(pcb.parent) == before
