"""``pcb prune`` honours the board's current-paths sidecar (Issue #9747).

A 2-layer, 1 oz board whose series power link is drawn as the same wide bar
on both outer layers between two through-hole pads carries a declared pulsed
path; either layer alone is about half the IPC-2221 width.  ``kct check``
counts the two layers as one parallel group and passes, but ``pcb prune``
ran its declared-current width test only when ``--current-paths`` was passed
explicitly -- so the readiness cleanup (and a plain prune) cut one bar and
the very next check failed with ``path_ampacity`` errors.

These tests pin the fixed contract: the sidecar a ``kct check`` on the board
would load also feeds prune's planning, auto-discovered exactly like check's
(with ``--no-current-paths`` to suppress it), while the declared-current
width test keeps both bars of a doubled link at 13.4 A and still allows the
cut at 1 A or with no sidecar at all.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

# 13.4 A continuous on 1 oz external copper needs ~10.772 mm (IPC-2221);
# 5.5 mm is about half of that, so either bar alone fails and only the
# parallel group passes.  At 1 A the requirement collapses below 5.5 mm and
# one bar suffices.
HIGH_CURRENT_A = 13.4
LOW_CURRENT_A = 1.0
BAR_WIDTH_MM = 5.5
REQUIRED_AT_HIGH_CURRENT_MM = pytest.approx(10.7719, rel=1e-3)

DOUBLED_BAR_PCB = f"""(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6))
  (paper "A4")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "PWR")
  (gr_rect (start 0 0) (end 200 120) (stroke (width 0.1) (type default))
    (fill none) (layer "Edge.Cuts"))
  (footprint "TH:Pad" (layer "F.Cu") (at 100 100)
    (fp_text reference "A" (at 0 -3) (layer "F.SilkS"))
    (pad "1" thru_hole circle (at 0 0) (size 1.7 1.7) (drill 1.0)
      (layers "*.Cu") (net 1 "PWR"))
  )
  (footprint "TH:Pad" (layer "F.Cu") (at 130 100)
    (fp_text reference "B" (at 0 -3) (layer "F.SilkS"))
    (pad "1" thru_hole circle (at 0 0) (size 1.7 1.7) (drill 1.0)
      (layers "*.Cu") (net 1 "PWR"))
  )
  (segment (start 100 100) (end 130 100) (width {BAR_WIDTH_MM}) (layer "F.Cu") (net 1)
    (uuid "00000000-0000-0000-0000-000000000001"))
  (segment (start 100 100) (end 130 100) (width {BAR_WIDTH_MM}) (layer "B.Cu") (net 1)
    (uuid "00000000-0000-0000-0000-000000000002"))
)
"""


def _doubled_bar_board(directory: Path, *, sidecar: float | None = HIGH_CURRENT_A) -> Path:
    """The issue's one-net reproduction: two THT pads, one bar per outer layer."""
    directory.mkdir(parents=True, exist_ok=True)
    pcb = directory / "b.kicad_pcb"
    pcb.write_text(DOUBLED_BAR_PCB)
    if sidecar is not None:
        _write_sidecar(directory / "current_paths.json", sidecar)
    return pcb


def _write_sidecar(path: Path, current_a: float) -> Path:
    path.write_text(
        json.dumps(
            {
                "paths": [
                    {
                        "name": "p",
                        "net": "PWR",
                        "source": {"ref": "A", "pad": "1"},
                        "sink": {"ref": "B", "pad": "1"},
                        "continuous_a": current_a,
                    }
                ]
            }
        )
    )
    return path


def _declarations(pcb: Path, sidecar: Path):
    from kicad_tools.pcb_prune import current_path_declarations

    return current_path_declarations(pcb, sidecar)


def _prune(pcb: Path, **kwargs):
    from kicad_tools.pcb_prune import prune_board

    kwargs.setdefault("verify_drc", False)
    kwargs.setdefault("apply", False)
    return prune_board(pcb, **kwargs)


def _successful_drc(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make prune's DRC verification succeed without kicad-cli (#9747 test plan).

    A removal control that silently skipped verification could pass for the
    wrong reason when kicad-cli is unavailable, so the control runs against a
    verifier that reports a clean, unchanged board.
    """
    import kicad_tools.pcb_prune as mod

    monkeypatch.setattr(mod, "_drc_signature", lambda path: frozenset())


def _run_check(pcb: Path, report: Path, extra: list[str] | None = None) -> int:
    from kicad_tools.cli.check_cmd import main

    argv = [
        str(pcb),
        "--mfr",
        "jlcpcb",
        "--only",
        "path_ampacity",
        "--output",
        str(report),
        "--format",
        "json",
        "--allow-incomplete",
    ]
    if extra:
        argv.extend(extra)
    return main(argv)


def _path_ampacity_errors(report: Path) -> list[dict]:
    data = json.loads(report.read_text())
    return [
        v
        for v in data["violations"]
        if v.get("rule_id") == "path_ampacity" and v.get("severity") == "error"
    ]


def _cli_prune(pcb: Path, *extra: str) -> int:
    from kicad_tools.cli.commands.pcb import _run_prune_command
    from kicad_tools.cli.parser import create_parser

    args = create_parser().parse_args(["pcb", "prune", str(pcb), "--format", "json", *extra])
    return _run_prune_command(args, pcb)


def _cli_removed(stdout: str) -> int:
    return json.loads(stdout)["removed"]["segments"]


# ---------------------------------------------------------------------------
# The committed board and prune's declared-current width test
# ---------------------------------------------------------------------------


def test_committed_doubled_bar_passes_path_ampacity(tmp_path: Path):
    """The board as committed passes: the two layers are one parallel group."""
    pcb = _doubled_bar_board(tmp_path / "board")
    report = tmp_path / "report.json"

    assert _run_check(pcb, report) == 0
    assert _path_ampacity_errors(report) == []


def test_declarations_compute_the_ipc2221_width_at_the_stackup(tmp_path: Path):
    pcb = _doubled_bar_board(tmp_path / "board")
    specs, declared_nets, required_widths = _declarations(
        pcb, tmp_path / "board" / "current_paths.json"
    )

    assert declared_nets == {"PWR"}
    assert [spec.net_name for spec in specs] == ["PWR"]
    assert required_widths["PWR"]["F.Cu"] == REQUIRED_AT_HIGH_CURRENT_MM
    assert required_widths["PWR"]["B.Cu"] == REQUIRED_AT_HIGH_CURRENT_MM


def test_prune_with_declarations_keeps_both_bars_and_cites_the_declared_width(tmp_path: Path):
    """At 13.4 A neither bar may go: the survivor is narrower than 10.77 mm."""
    pcb = _doubled_bar_board(tmp_path / "board")
    specs, declared_nets, required_widths = _declarations(pcb, pcb.parent / "current_paths.json")

    _, result = _prune(
        pcb,
        declared_nets=declared_nets,
        required_widths=required_widths,
        current_paths=specs,
        wide=None,
    )

    assert result.stats["segments_removed"] == 0
    reasons = [loop["reason"] for loop in result.uncut_loops]
    assert len(reasons) == 2  # one per layer's bar
    assert all(
        "narrower than the declared current needs (10.8 mm on" in reason for reason in reasons
    )
    assert any("F.Cu" in reason for reason in reasons)
    assert any("B.Cu" in reason for reason in reasons)


def test_prune_without_sidecar_removes_the_duplicate_bar(tmp_path, monkeypatch):
    """Control (guards against over-blocking): no sidecar, the bar still goes."""
    _successful_drc(monkeypatch)
    pcb = _doubled_bar_board(tmp_path / "board", sidecar=None)

    _, result = _prune(pcb, verify_drc=True, allow_default_rules=True)

    assert result.stats["segments_removed"] == 1
    assert result.drc_verified  # a real verifier accepted the removal


def test_prune_at_1a_one_bar_suffices_and_the_cut_is_eligible(tmp_path, monkeypatch):
    """A fixed high-current cutoff would keep both bars for the wrong reason."""
    _successful_drc(monkeypatch)
    pcb = _doubled_bar_board(tmp_path / "board", sidecar=LOW_CURRENT_A)
    specs, declared_nets, required_widths = _declarations(pcb, pcb.parent / "current_paths.json")
    assert required_widths["PWR"]["F.Cu"] < BAR_WIDTH_MM

    _, result = _prune(
        pcb,
        verify_drc=True,
        declared_nets=declared_nets,
        required_widths=required_widths,
        current_paths=specs,
        wide=None,
        allow_default_rules=True,
    )

    assert result.stats["segments_removed"] == 1
    assert result.drc_verified


def test_pruned_board_still_passes_path_ampacity(tmp_path: Path):
    """Prune with declarations leaves a board the check still accepts."""
    pcb = _doubled_bar_board(tmp_path / "board")
    specs, declared_nets, required_widths = _declarations(pcb, pcb.parent / "current_paths.json")
    pruned, result = _prune(
        pcb,
        declared_nets=declared_nets,
        required_widths=required_widths,
        current_paths=specs,
        wide=None,
        apply=True,
    )
    out = tmp_path / "cleaned.kicad_pcb"
    pruned.save(out)
    assert result.stats["segments_removed"] == 0

    report = tmp_path / "report.json"
    assert (
        _run_check(out, report, extra=["--current-paths", str(pcb.parent / "current_paths.json")])
        == 0
    )
    assert _path_ampacity_errors(report) == []


# ---------------------------------------------------------------------------
# CLI: the same sidecar `kct check` loads, with an opt-out
# ---------------------------------------------------------------------------


def test_cli_auto_discovers_the_sidecar_and_keeps_both_bars(tmp_path, capsys):
    pcb = _doubled_bar_board(tmp_path / "board")

    assert _cli_prune(pcb, "--dry-run", "--no-drc") == 0

    captured = capsys.readouterr()
    assert "[INFO] auto-loaded current-paths sidecar:" in captured.err
    assert "current_paths.json" in captured.err
    assert _cli_removed(captured.out) == 0


def test_cli_no_current_paths_restores_the_removal(tmp_path, capsys):
    pcb = _doubled_bar_board(tmp_path / "board")

    assert _cli_prune(pcb, "--dry-run", "--no-drc", "--no-current-paths") == 0

    captured = capsys.readouterr()
    assert "auto-loaded" not in captured.err
    assert _cli_removed(captured.out) == 1


def test_cli_explicit_sidecar_wins_over_the_adjacent_one(tmp_path, capsys):
    """An explicit low-current sidecar beats the adjacent 13.4 A one."""
    pcb = _doubled_bar_board(tmp_path / "board")
    explicit = _write_sidecar(tmp_path / "explicit.json", LOW_CURRENT_A)

    assert _cli_prune(pcb, "--dry-run", "--no-drc", "--current-paths", str(explicit)) == 0

    captured = capsys.readouterr()
    assert "auto-loaded" not in captured.err  # explicit is not echoed as auto
    assert _cli_removed(captured.out) == 1


def test_cli_conflicting_current_paths_flags_are_an_error(tmp_path, capsys):
    pcb = _doubled_bar_board(tmp_path / "board")

    rc = _cli_prune(
        pcb,
        "--dry-run",
        "--no-drc",
        "--current-paths",
        str(pcb.parent / "current_paths.json"),
        "--no-current-paths",
    )

    assert rc == 1
    err = capsys.readouterr().err
    assert "--no-current-paths cannot be combined with --current-paths" in err


def test_cli_malformed_adjacent_sidecar_warns_and_prunes_without_it(tmp_path, capsys):
    """Same degrade-gracefully contract as `kct check`'s auto-discovery."""
    pcb = _doubled_bar_board(tmp_path / "board")
    (pcb.parent / "current_paths.json").write_text("{not json")

    assert _cli_prune(pcb, "--dry-run", "--no-drc") == 0

    captured = capsys.readouterr()
    assert "malformed current-paths sidecar" in captured.err
    assert _cli_removed(captured.out) == 1


def test_cli_malformed_explicit_sidecar_is_fatal(tmp_path, capsys):
    pcb = _doubled_bar_board(tmp_path / "board", sidecar=None)
    bogus = tmp_path / "bogus.json"
    bogus.write_text("{not json")

    rc = _cli_prune(pcb, "--dry-run", "--no-drc", "--current-paths", str(bogus))

    assert rc == 1
    assert "Error loading current paths" in capsys.readouterr().err
