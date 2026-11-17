"""The post-fill remediation pass must not re-run the fill it was just handed (Issue #5617).

``run_fill_zones()`` fills zones with ``kicad-cli pcb drc --refill-zones
--save-board`` (the DRC fallback -- the only branch any KiCad 8/9/10 takes,
since ``pcb fill-zones`` does not exist), then hands the freshly filled,
saved board straight to :func:`_remediate_starved_thermal`, whose pass 0
opened by running *the same command again* against the unchanged file. That
second launch is the single most expensive redundant step in each of Board
06's five zone-fill phases (``9b``, ``10c`` x2, ``12b``, ``13b``) -- see
``docs/diagnostics/issue-5617/redundant-remediation-refill.md`` for the
traced measurement and the byte-identical-output evidence.

These tests assert the *shape* of the fix rather than wall-clock time, in the
style of ``test_kicad_cli_capability_cache.py`` (Issue #5566):

* the skip is requested exactly when the caller can prove the board on disk
  was just refilled and saved -- and **not** on the native ``fill-zones``
  branch, nor against a kicad-cli whose DRC lacks ``--refill-zones``;
* the skip removes exactly one ``kicad-cli`` launch, the refilling one;
* it applies to pass 0 only -- once remediation mutates the board, every
  later pass refills again, because there the refill is load-bearing.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from kicad_tools.cli import runner


@pytest.fixture(autouse=True)
def _clear_capability_caches():
    runner._kicad_cli_has_fill_zones.cache_clear()
    runner._kicad_drc_supports_refill.cache_clear()
    yield
    runner._kicad_cli_has_fill_zones.cache_clear()
    runner._kicad_drc_supports_refill.cache_clear()


def _fake_completed(stdout: str = "", returncode: int = 0) -> MagicMock:
    completed = MagicMock()
    completed.returncode = returncode
    completed.stdout = stdout
    completed.stderr = ""
    return completed


def _board(tmp_path: Path) -> Path:
    pcb = tmp_path / "board.kicad_pcb"
    pcb.write_text("(kicad_pcb (version 20260206))")
    return pcb


def _install_fake_cli(
    monkeypatch,
    *,
    has_fill_zones: bool,
    supports_refill: bool,
    commands: list[list[str]],
) -> None:
    """Route every ``kicad-cli`` launch to a fake that records its argv.

    ``--help`` probes answer per the two capability flags; every real
    invocation writes an empty JSON report so the caller sees a successful
    run with no violations.
    """

    def fake_run(cmd, capture_output=True, text=True, **kwargs):
        commands.append(list(cmd))
        if "--help" in cmd:
            if "fill-zones" in cmd:
                return _fake_completed(stdout="fill-zones" if has_fill_zones else "")
            return _fake_completed(stdout="--refill-zones" if supports_refill else "")
        if "--output" in cmd:
            Path(cmd[cmd.index("--output") + 1]).write_text("{}")
        return _fake_completed()

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    monkeypatch.setattr(runner, "_snapshot_net_declarations", lambda _p: [])
    monkeypatch.setattr(runner, "_snapshot_element_nets", lambda _p: {})
    monkeypatch.setattr(runner, "_restore_net_declarations", lambda *a, **k: None)
    monkeypatch.setattr(runner, "_normalize_pad_connection_in_place", lambda _p: None)


@pytest.mark.parametrize(
    ("has_fill_zones", "supports_refill", "expected_skip"),
    [
        # The universal case today: no `pcb fill-zones` subcommand exists, so
        # the fill went through `drc --refill-zones --save-board` -- the board
        # on disk is already freshly filled and saved.
        (False, True, True),
        # A hypothetical future kicad-cli with a native `pcb fill-zones`: that
        # branch makes no claim about a saved refill, so pass 0 still refills.
        (True, True, False),
        # KiCad 8/9-style DRC without `--refill-zones`: the fill relied on the
        # implicit refill and never passed `--save-board`; keep the refill.
        (False, False, False),
    ],
)
def test_skip_is_requested_only_when_the_fill_already_refilled_and_saved(
    monkeypatch, tmp_path, has_fill_zones, supports_refill, expected_skip
):
    pcb = _board(tmp_path)
    cli = tmp_path / "kicad-cli"
    commands: list[list[str]] = []
    _install_fake_cli(
        monkeypatch,
        has_fill_zones=has_fill_zones,
        supports_refill=supports_refill,
        commands=commands,
    )
    remediation = MagicMock()
    monkeypatch.setattr(runner, "_remediate_starved_thermal", remediation)

    assert runner.run_fill_zones(pcb, kicad_cli=cli).success

    remediation.assert_called_once()
    assert remediation.call_args.kwargs["skip_first_refill"] is expected_skip


def test_skip_removes_exactly_one_launch_and_it_is_the_refilling_one(monkeypatch, tmp_path):
    """The surviving pass-0 launch is the read-only DRC, not the refill."""
    cli = tmp_path / "kicad-cli"

    def launches(skip: bool) -> list[list[str]]:
        commands: list[list[str]] = []
        _install_fake_cli(
            monkeypatch, has_fill_zones=False, supports_refill=True, commands=commands
        )
        runner._remediate_starved_thermal(
            _board(tmp_path), cli, settle=None, skip_first_refill=skip
        )
        return [c for c in commands if "--help" not in c]

    unskipped = launches(False)
    skipped = launches(True)

    assert len(unskipped) == 2
    assert [("--refill-zones" in c) for c in unskipped] == [True, False]
    assert len(skipped) == 1
    assert "--refill-zones" not in skipped[0]
    assert "--save-board" not in skipped[0]


def test_skip_applies_to_pass_zero_only(monkeypatch, tmp_path):
    """A pass that mutates the board must still refill on the next iteration.

    Pass 0 reports one starved pad, so remediation forces it solid and loops;
    that override only reaches the shipped copper through a real refill, so
    pass 1 must launch one -- ``skip_first_refill`` is not a global opt-out.
    """
    import kicad_tools.core.sexp_file as sexp_file
    import kicad_tools.sexp as sexp
    import kicad_tools.zones.fill_clearance as fill_clearance

    cli = tmp_path / "kicad-cli"
    pcb = _board(tmp_path)
    commands: list[list[str]] = []
    _install_fake_cli(monkeypatch, has_fill_zones=False, supports_refill=True, commands=commands)

    reports: list[int] = []

    def starved(_report):
        reports.append(1)
        return {"pad-uuid"} if len(reports) == 1 else set()

    monkeypatch.setattr(fill_clearance, "starved_thermal_pad_uuids", starved)
    monkeypatch.setattr(fill_clearance, "isolated_copper_zone_uuids", lambda _r: set())
    monkeypatch.setattr(fill_clearance, "force_solid_on_pads_by_uuid", lambda _d, pads: len(pads))
    monkeypatch.setattr(fill_clearance, "force_solid_on_isolated_island_pads", lambda _d, _z: 0)
    monkeypatch.setattr(sexp, "parse_file", lambda _p: object())
    monkeypatch.setattr(sexp_file, "save_pcb", lambda _d, _p: None)

    runner._remediate_starved_thermal(pcb, cli, settle=None, skip_first_refill=True)

    real = [c for c in commands if "--help" not in c]
    refills = [c for c in real if "--refill-zones" in c]
    # pass 0: read-only DRC (refill skipped) -> one pad forced solid
    # pass 1: refill (bakes the override in) + read-only DRC -> clean, return
    assert len(real) == 3
    assert len(refills) == 1, "the post-mutation pass must still refill"
    assert "--refill-zones" not in real[0]
    assert "--refill-zones" in real[1]
