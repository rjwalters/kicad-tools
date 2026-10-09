"""A verification skipped for ``--timeout`` is reported as UNVERIFIED (Issue #6273).

``kct route --timeout`` reserves time for the finishing stages, but when the
time left still cannot cover the post-route DRC (or the #5785 stranded-pour
check's first KiCad run), that check is skipped.  A skipped check is not a
passed one: the run must not print "SUCCESS" / "DRC passed", its JSON verdict
must not be ``"success"`` and it must not exit 0.  It exits
:data:`route_cmd.EXIT_UNVERIFIED` (10) with ``summary.verdict ==
"unverified"`` instead.  These tests pin that at the verdict level -- banner,
JSON document and exit code of a real ``kct route`` run -- on both the direct
(``--no-auto-layers``) and the default escalation path.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from kicad_tools.cli import main as kct_main
from kicad_tools.cli import route_cmd
from kicad_tools.router.oracle_completion import (
    STOP_DEADLINE,
    STOP_NOT_RUN,
    OracleCompletionResult,
)

PCB = Path(__file__).resolve().parent / "fixtures" / "projects" / "test_project.kicad_pcb"

# Deterministic and fast; DRC is deliberately NOT skipped (no --skip-drc).
BASE = [
    "--no-optimize",
    "--no-sync-check",
    "--no-auto-pour",
    "--no-placement-feedback",
    "--oracle-rounds",
    "0",
    "--no-current-paths",
]

PATHS = {
    "direct (--no-auto-layers)": ["--no-auto-layers"],
    "escalation (default)": [],
}


def _route(tmp_path: Path, capsys, extra: list[str]) -> tuple[int, str]:
    pcb = tmp_path / PCB.name
    shutil.copy(PCB, pcb)
    rc = kct_main(["route", str(pcb), "-o", str(tmp_path / "out.kicad_pcb"), *BASE, *extra])
    return rc, capsys.readouterr().out


@pytest.fixture
def no_time_for_drc(monkeypatch):
    """The post-route DRC finds no --timeout budget left and skips itself."""
    monkeypatch.setattr(
        route_cmd,
        "_post_route_drc_budget",
        lambda args: {"time_left": 0.0, "drc_cost_estimate": 0.0},
    )


@pytest.fixture
def stranded_pour_unknown(monkeypatch):
    """The pour-oracle stage ran out of time before its first KiCad DRC."""

    def _stage(output_path, *, args, quiet=False):
        args._stranded_pour_links = 0
        args._allow_stranded_pour_pads = bool(getattr(args, "allow_stranded_pour_pads", False))
        args._stranded_pour_unknown = True
        return 0

    monkeypatch.setattr(route_cmd, "_complete_pour_nets_with_oracle", _stage)


# =============================================================================
# Verdict level: banner, JSON verdict and exit code
# =============================================================================


@pytest.mark.parametrize("extra", list(PATHS.values()), ids=list(PATHS))
def test_baseline_run_with_drc_is_still_a_success(tmp_path, capsys, extra) -> None:
    """Control: the same board with DRC actually run exits 0."""
    rc, out = _route(tmp_path, capsys, extra)
    assert rc == 0, out
    assert "UNVERIFIED" not in out


@pytest.mark.parametrize("extra", list(PATHS.values()), ids=list(PATHS))
def test_drc_skipped_for_time_banner_and_exit(tmp_path, capsys, no_time_for_drc, extra) -> None:
    rc, out = _route(tmp_path, capsys, extra)

    assert rc == route_cmd.EXIT_UNVERIFIED == 10
    assert "SKIPPED" in out
    assert "DRC passed" not in out
    assert "SUCCESS:" not in out
    assert "UNVERIFIED:" in out
    assert "post-route DRC" in out
    assert "kct check" in out


@pytest.mark.parametrize("extra", list(PATHS.values()), ids=list(PATHS))
def test_drc_skipped_for_time_json_verdict(tmp_path, capsys, no_time_for_drc, extra) -> None:
    rc, out = _route(tmp_path, capsys, [*extra, "--format", "json", "--quiet"])

    assert rc == route_cmd.EXIT_UNVERIFIED
    doc = json.loads(out)  # the whole of stdout is one document (#5938)
    assert doc["summary"]["verdict"] == "unverified"


@pytest.mark.parametrize("extra", list(PATHS.values()), ids=list(PATHS))
def test_stranded_pour_unknown_is_unverified(
    tmp_path, capsys, stranded_pour_unknown, extra
) -> None:
    rc, out = _route(tmp_path, capsys, extra)

    assert rc == route_cmd.EXIT_UNVERIFIED
    assert "SUCCESS:" not in out
    assert "UNVERIFIED:" in out
    assert "stranded-pour check" in out


def test_stranded_pour_unknown_ignored_when_pads_are_advisory(
    tmp_path, capsys, stranded_pour_unknown
) -> None:
    """--allow-stranded-pour-pads makes the check advisory, so nothing is unverified."""
    rc, out = _route(tmp_path, capsys, ["--no-auto-layers", "--allow-stranded-pour-pads"])
    assert rc == 0, out
    assert "UNVERIFIED" not in out


# =============================================================================
# The pour-oracle stage records "unknown", not 0, on a deadline skip
# =============================================================================


def _stub_pour_stage(monkeypatch, result: OracleCompletionResult) -> None:
    import kicad_tools.cli.runner as runner_mod
    import kicad_tools.cli.stitch_cmd as stitch_mod
    import kicad_tools.core.sexp_file as sexp_mod
    import kicad_tools.router.oracle_completion as oracle_mod

    monkeypatch.setattr(runner_mod, "find_kicad_cli", lambda: Path("/usr/bin/kicad-cli"))
    monkeypatch.setattr(sexp_mod, "load_pcb", lambda _p: object())
    monkeypatch.setattr(stitch_mod, "find_all_plane_nets", lambda _doc: ["GND"])
    monkeypatch.setattr(route_cmd, "_make_pour_oracle", lambda _args: lambda _p: None)
    monkeypatch.setattr(oracle_mod, "run_oracle_completion", lambda *a, **k: result)


def _stage_args(**kw) -> SimpleNamespace:
    base = {
        "dry_run": False,
        "oracle_rounds": None,
        "allow_stranded_pour_pads": False,
        "timeout": None,
    }
    base.update(kw)
    return SimpleNamespace(**base)


def test_deadline_before_first_drc_marks_status_unknown(monkeypatch, tmp_path, capsys):
    _stub_pour_stage(
        monkeypatch, OracleCompletionResult(ran=False, stop_reason=STOP_DEADLINE, note="no time")
    )
    args = _stage_args()
    n = route_cmd._run_pour_oracle_stage(tmp_path / "b.kicad_pcb", args=args)

    assert n == 0
    assert args._stranded_pour_unknown is True
    assert route_cmd._unverified_checks(args) == ["stranded-pour check"]
    assert "UNKNOWN" in capsys.readouterr().out


def test_kicad_cli_not_run_is_not_a_deadline_skip(monkeypatch, tmp_path):
    """Only the deadline makes the status unknown; other skips keep #5785's behaviour."""
    _stub_pour_stage(
        monkeypatch, OracleCompletionResult(ran=False, stop_reason=STOP_NOT_RUN, note="x")
    )
    args = _stage_args()
    route_cmd._run_pour_oracle_stage(tmp_path / "b.kicad_pcb", args=args, quiet=True)
    assert args._stranded_pour_unknown is False
    assert route_cmd._unverified_checks(args) == []


# =============================================================================
# Helpers
# =============================================================================


class TestUnverifiedHelpers:
    def test_record_is_assigned_not_latched(self):
        args = SimpleNamespace()
        route_cmd._record_post_route_drc(args, route_cmd.DRC_SKIPPED_FOR_TIME[0])
        assert route_cmd._unverified_checks(args) == ["post-route DRC"]
        # A later attempt whose DRC ran clears it.
        route_cmd._record_post_route_drc(args, 0)
        assert route_cmd._unverified_checks(args) == []

    def test_failed_to_run_is_not_a_time_skip(self):
        args = SimpleNamespace()
        route_cmd._record_post_route_drc(args, -1)
        assert route_cmd._unverified_checks(args) == []

    def test_only_exit_0_becomes_unverified(self):
        args = SimpleNamespace(_drc_skipped_for_time=True)
        assert route_cmd._unverified_exit(0, args) == route_cmd.EXIT_UNVERIFIED
        for rc in (1, 2, 3, 4, 7, 8):
            assert route_cmd._unverified_exit(rc, args) == rc
        assert route_cmd._unverified_exit(0, SimpleNamespace()) == 0

    def test_verdict_for_exit(self):
        assert route_cmd._verdict_for_exit(0) == "success"
        assert route_cmd._verdict_for_exit(route_cmd.EXIT_UNVERIFIED) == "unverified"
        assert route_cmd._verdict_for_exit(3) == "failed"

    def test_exit_code_is_documented(self):
        import inspect

        src = inspect.getsource(route_cmd)
        assert "# 10 = UNVERIFIED" in src
