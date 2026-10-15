"""A killed or failed kicad-cli is never a clean DRC (Issue #6724).

``run_geometric_drc`` used to pre-create its report file, ignore kicad-cli's
exit status and check only that the file existed.  When the OOM killer killed
kicad-cli, the empty pre-created file parsed as a report with 0 violations and
0 unconnected items, so ``kct route``'s pour oracle printed ``0 -> 0
(converged)`` on boards KiCad calls unconnected.

These tests stub kicad-cli to be killed, exit non-zero, or leave a missing,
empty or truncated report, and check that every DRC/ERC call site reports a
failure instead of a clean result.  They also keep the happy path honest.
"""

from __future__ import annotations

import json
import signal
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from kicad_tools.drc import geometric
from kicad_tools.drc.geometric import (
    REASON_CRASH,
    REASON_NO_REPORT,
    REASON_OK,
    run_geometric_drc,
)

FAKE_CLI = Path("/fake/kicad-cli")

#: A complete kicad-cli 10 JSON DRC report with one unconnected pour link.
VALID_REPORT = json.dumps(
    {
        "$schema": "https://schemas.kicad.org/drc.v1.json",
        "coordinate_units": "mm",
        "date": "2026-10-15T00:00:00+0000",
        "kicad_version": "10.0.6",
        "source": "board.kicad_pcb",
        "violations": [],
        "unconnected_items": [
            {
                "description": "Missing connection between items",
                "items": [
                    {
                        "description": "Pad 1 [GND] of C1 on F.Cu",
                        "pos": {"x": 10.0, "y": 10.0},
                        "uuid": "00000000-0000-0000-0000-000000000001",
                    },
                    {
                        "description": "Pad 2 [GND] of C2 on F.Cu",
                        "pos": {"x": 20.0, "y": 10.0},
                        "uuid": "00000000-0000-0000-0000-000000000002",
                    },
                ],
                "severity": "error",
                "type": "unconnected_items",
            }
        ],
        "schematic_parity": [],
    }
)
TRUNCATED_REPORT = VALID_REPORT[: len(VALID_REPORT) // 2]


def _fake_kicad_cli(monkeypatch, module, *, returncode: int, report: str | None):
    """Stub ``module.subprocess.run`` as one kicad-cli invocation.

    ``report`` is written to the ``--output`` path (``None`` writes nothing).
    The stub also records whether that path already existed when kicad-cli
    started, which is how the old pre-created-report bug looked.
    """
    seen: dict = {}

    def fake_run(cmd, *args, **kwargs):
        text = kwargs.get("text", False)
        if "--help" in cmd:
            # The ``--refill-zones`` capability probe: answer as KiCad 10.
            help_text = "  --refill-zones          Refill zones before running DRC\n"
            return subprocess.CompletedProcess(
                cmd, 0, help_text if text else help_text.encode(), "" if text else b""
            )
        out_flag = "--output" if "--output" in cmd else "-o"
        out = Path(cmd[cmd.index(out_flag) + 1])
        seen["cmd"] = list(cmd)
        seen["pre_existing"] = out.exists()
        if report is not None:
            out.write_text(report)
        empty = "" if text else b""
        return subprocess.CompletedProcess(cmd, returncode, empty, empty)

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    return seen


# ---------------------------------------------------------------------------
# run_geometric_drc
# ---------------------------------------------------------------------------


class TestRunGeometricDrc:
    def test_killed_without_report_is_a_crash(self, monkeypatch, tmp_path):
        seen = _fake_kicad_cli(monkeypatch, geometric, returncode=-signal.SIGKILL, report=None)

        res = run_geometric_drc(tmp_path / "b.kicad_pcb", kicad_cli=FAKE_CLI)

        assert seen["pre_existing"] is False, "the report must not be pre-created"
        assert res.ran is False
        assert res.reason == REASON_CRASH
        assert res.failed is True
        assert "SIGKILL" in (res.note or "")
        assert res.error_count == 0 and res.unconnected_count == 0

    def test_killed_after_writing_a_report_is_still_a_crash(self, monkeypatch, tmp_path):
        """A complete-looking report does not redeem a signal kill."""
        _fake_kicad_cli(monkeypatch, geometric, returncode=-signal.SIGKILL, report=VALID_REPORT)

        res = run_geometric_drc(tmp_path / "b.kicad_pcb", kicad_cli=FAKE_CLI)

        assert res.ran is False
        assert res.reason == REASON_CRASH

    def test_exit_3_is_a_load_failure(self, monkeypatch, tmp_path):
        """Exit 3 is kicad-cli's "failed to load board" status."""
        _fake_kicad_cli(monkeypatch, geometric, returncode=3, report=None)

        res = run_geometric_drc(tmp_path / "b.kicad_pcb", kicad_cli=FAKE_CLI)

        assert res.ran is False
        assert res.reason == geometric.REASON_LOAD_FAILED
        assert res.failed is True
        assert res.returncode == 3
        note = res.note or ""
        assert "could not load the board" in note
        assert "file format newer" in note
        assert "memory" not in note and "killed" not in note

    def test_missing_report_is_no_report(self, monkeypatch, tmp_path):
        _fake_kicad_cli(monkeypatch, geometric, returncode=0, report=None)

        res = run_geometric_drc(tmp_path / "b.kicad_pcb", kicad_cli=FAKE_CLI)

        assert res.ran is False
        assert res.reason == REASON_NO_REPORT
        assert res.failed is True

    def test_empty_report_is_no_report(self, monkeypatch, tmp_path):
        _fake_kicad_cli(monkeypatch, geometric, returncode=0, report="")

        res = run_geometric_drc(tmp_path / "b.kicad_pcb", kicad_cli=FAKE_CLI)

        assert res.ran is False
        assert res.reason == REASON_NO_REPORT

    def test_truncated_report_is_no_report(self, monkeypatch, tmp_path):
        _fake_kicad_cli(monkeypatch, geometric, returncode=0, report=TRUNCATED_REPORT)

        res = run_geometric_drc(tmp_path / "b.kicad_pcb", kicad_cli=FAKE_CLI)

        assert res.ran is False
        assert res.reason == REASON_NO_REPORT

    def test_clean_run_still_reports(self, monkeypatch, tmp_path):
        """No regression on the happy path: the report is read as before."""
        seen = _fake_kicad_cli(monkeypatch, geometric, returncode=0, report=VALID_REPORT)

        res = run_geometric_drc(tmp_path / "b.kicad_pcb", kicad_cli=FAKE_CLI)

        assert res.ran is True
        assert res.reason == REASON_OK
        assert res.failed is False
        assert res.error_count == 0
        assert res.unconnected_count == 1
        out = Path(seen["cmd"][seen["cmd"].index("--output") + 1])
        assert not out.exists(), "the temporary report directory is cleaned up"

    def test_documented_violations_exit_code_with_report_is_ok(self, monkeypatch, tmp_path):
        """5 is kicad-cli's "violations found" status (--exit-code-violations)."""
        _fake_kicad_cli(monkeypatch, geometric, returncode=5, report=VALID_REPORT)

        res = run_geometric_drc(tmp_path / "b.kicad_pcb", kicad_cli=FAKE_CLI)

        assert res.ran is True
        assert res.unconnected_count == 1

    @pytest.mark.skipif(sys.platform == "win32", reason="needs POSIX signals")
    def test_real_process_killed_by_signal(self, tmp_path):
        """An actual executable that SIGKILLs itself, through the real subprocess."""
        cli = tmp_path / "kicad-cli"
        cli.write_text("#!/bin/sh\nkill -9 $$\n")
        cli.chmod(cli.stat().st_mode | stat.S_IXUSR)

        res = run_geometric_drc(tmp_path / "b.kicad_pcb", kicad_cli=cli)

        assert res.ran is False
        assert res.reason == REASON_CRASH
        assert "SIGKILL" in (res.note or "")


class TestSavedFillCheck:
    def test_killed_saved_fill_run_is_failed(self, monkeypatch, tmp_path):
        _fake_kicad_cli(monkeypatch, geometric, returncode=-signal.SIGKILL, report=None)

        check = geometric.check_saved_fill(tmp_path / "b.kicad_pcb", kicad_cli=FAKE_CLI)

        assert check.ran is False
        assert check.failed is True
        assert check.ok is False


def test_describe_returncode():
    assert "SIGKILL" in geometric.describe_returncode(-9)
    assert "out of memory" in geometric.describe_returncode(-9)
    assert "SIGSEGV" in geometric.describe_returncode(-11)
    load = geometric.describe_returncode(3)
    assert "could not load the board" in load and "file format newer" in load
    assert "memory" not in load
    assert geometric.describe_returncode(7) == "kicad-cli exited with status 7"
    usage = geometric.describe_returncode(
        geometric.KICAD_CLI_ERR_ARGS, stdout="Unknown argument: --refill-zones\nUsage: drc"
    )
    assert "does not support --refill-zones" in usage and "newer KiCad" in usage
    assert "memory" not in usage and "killed" not in usage


def test_describe_returncode_exit_1_without_unknown_argument_is_neutral():
    """Exit 1 alone is not proof of an unsupported option (Judge, PR #6746)."""
    plain = geometric.describe_returncode(geometric.KICAD_CLI_ERR_ARGS)
    assert plain == "kicad-cli exited with status 1 (usage or launch error)"
    other = geometric.describe_returncode(
        geometric.KICAD_CLI_ERR_ARGS, stderr="Traceback: wrapper failed: cannot open display"
    )
    assert other == "kicad-cli exited with status 1 (usage or launch error)"
    assert geometric.classify_returncode(1, "", "boom") == geometric.REASON_EXIT_ERROR
    assert (
        geometric.classify_returncode(1, "Unknown argument: --x", "")
        == geometric.REASON_UNSUPPORTED
    )
    assert geometric.classify_returncode(3) == geometric.REASON_LOAD_FAILED
    assert geometric.classify_returncode(-9) == REASON_CRASH


# ---------------------------------------------------------------------------
# KiCad 8/9: no ``--refill-zones`` (Judge review on PR #6746)
# ---------------------------------------------------------------------------

#: The usage text KiCad 9.0.9's ``pcb drc`` prints (on stdout, exit 1) for an
#: option it does not know; it writes no report.
KICAD9_HELP = (
    "Usage: drc [--help] [--output OUTPUT_FILE] [--define-var KEY=VALUE] "
    "[--format FORMAT] [--all-track-errors] [--schematic-parity] [--units UNITS] "
    "[--severity-all] [--severity-error] [--severity-warning] "
    "[--severity-exclusions] [--exit-code-violations] INPUT_FILE\n"
)


def _script_kicad_cli(tmp_path: Path, *, rejects: tuple[str, ...], help_text: str) -> Path:
    """A real executable kicad-cli stand-in, driven through real ``subprocess``.

    ``--help`` prints ``help_text``.  Any argument in ``rejects`` makes it
    behave like KiCad 9 given an unknown option: ``Unknown argument: <opt>``
    plus usage on stdout, exit 1, no report.  Otherwise it writes
    :data:`VALID_REPORT` to ``--output`` and exits 0.  Every argv is logged.
    """
    log = tmp_path / "argv.log"
    cli = tmp_path / "kicad-cli"
    cli.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        f"log, rejects, help_text, report = {str(log)!r}, {rejects!r}, {help_text!r}, "
        f"{VALID_REPORT!r}\n"
        "argv = sys.argv[1:]\n"
        "open(log, 'a').write(json.dumps(argv) + '\\n')\n"
        "if '--help' in argv:\n"
        "    sys.stdout.write(help_text); sys.exit(0)\n"
        "for arg in argv:\n"
        "    if arg in rejects:\n"
        "        sys.stdout.write('Unknown argument: ' + arg + '\\n' + help_text); sys.exit(1)\n"
        "open(argv[argv.index('--output') + 1], 'w').write(report)\n"
    )
    cli.chmod(cli.stat().st_mode | stat.S_IXUSR)
    return cli


def _logged_argvs(tmp_path: Path) -> list[list[str]]:
    lines = (tmp_path / "argv.log").read_text().splitlines()
    return [json.loads(line) for line in lines]


@pytest.mark.skipif(sys.platform == "win32", reason="needs an executable script")
class TestKicadWithoutRefillZones:
    """KiCad 8/9's ``pcb drc`` rejects ``--refill-zones`` (exit 1, usage text,
    no report).  The DRC must still run there -- without the flag -- instead
    of failing every run (or, before #6724, silently reading as clean).
    """

    def test_geometric_drc_runs_without_refill_on_kicad9(self, tmp_path):
        cli = _script_kicad_cli(tmp_path, rejects=("--refill-zones",), help_text=KICAD9_HELP)

        res = run_geometric_drc(tmp_path / "b.kicad_pcb", kicad_cli=cli)

        assert res.ran is True, res.note
        assert res.reason == REASON_OK
        assert res.failed is False
        assert res.unconnected_count == 1
        drc_runs = [argv for argv in _logged_argvs(tmp_path) if "--help" not in argv]
        assert len(drc_runs) == 1
        assert "--refill-zones" not in drc_runs[0]

    def test_refill_probe_is_cached(self, tmp_path):
        cli = _script_kicad_cli(tmp_path, rejects=("--refill-zones",), help_text=KICAD9_HELP)

        for _ in range(3):
            assert run_geometric_drc(tmp_path / "b.kicad_pcb", kicad_cli=cli).ran is True

        probes = [argv for argv in _logged_argvs(tmp_path) if "--help" in argv]
        assert len(probes) == 1

    def test_kicad10_still_gets_refill_zones(self, tmp_path):
        help10 = KICAD9_HELP + "  --refill-zones          Refill zones before running DRC\n"
        cli = _script_kicad_cli(tmp_path, rejects=(), help_text=help10)

        res = run_geometric_drc(tmp_path / "b.kicad_pcb", kicad_cli=cli)

        assert res.ran is True
        drc_runs = [argv for argv in _logged_argvs(tmp_path) if "--help" not in argv]
        assert "--refill-zones" in drc_runs[0]

    def test_saved_fill_check_runs_on_kicad9(self, tmp_path):
        cli = _script_kicad_cli(tmp_path, rejects=("--refill-zones",), help_text=KICAD9_HELP)

        check = geometric.check_saved_fill(tmp_path / "b.kicad_pcb", kicad_cli=cli)

        assert check.ran is True and check.failed is False

    def test_run_drc_drops_refill_zones_on_kicad9(self, tmp_path):
        from kicad_tools.cli import runner

        cli = _script_kicad_cli(tmp_path, rejects=("--refill-zones",), help_text=KICAD9_HELP)

        res = runner.run_drc(
            tmp_path / "b.kicad_pcb",
            output_path=tmp_path / "out.json",
            schematic_parity=False,
            kicad_cli=cli,
            refill_zones=True,
        )

        assert res.success is True, res.stderr
        drc_runs = [argv for argv in _logged_argvs(tmp_path) if "--help" not in argv]
        assert "--refill-zones" not in drc_runs[0]

    def test_rejected_option_is_a_usage_error_not_a_crash(self, tmp_path):
        """Exit 1 names the unsupported option; it never blames memory."""
        cli = _script_kicad_cli(tmp_path, rejects=("--severity-all",), help_text=KICAD9_HELP)

        res = run_geometric_drc(tmp_path / "b.kicad_pcb", kicad_cli=cli)

        assert res.ran is False
        assert res.reason == geometric.REASON_UNSUPPORTED
        assert res.failed is True, "an unchecked board is still a failure"
        note = res.note or ""
        assert "does not support --severity-all" in note
        assert "newer KiCad" in note
        assert "memory" not in note and "crash" not in note and "killed" not in note

    def test_route_gate_usage_error_does_not_blame_memory(self, monkeypatch, tmp_path, capsys):
        from unittest.mock import MagicMock, patch

        import kicad_tools.cli.runner as runner_mod
        from kicad_tools.cli.route_cmd import run_post_route_drc

        cli = _script_kicad_cli(tmp_path, rejects=("--severity-all",), help_text=KICAD9_HELP)
        monkeypatch.setattr(runner_mod, "find_kicad_cli", lambda: cli)
        pcb = tmp_path / "board.kicad_pcb"
        pcb.write_text("(kicad_pcb)")
        clean = MagicMock(error_count=0, warning_count=0, errors=[], warnings=[])

        with (
            patch("kicad_tools.validate.DRCChecker") as checker_cls,
            patch("kicad_tools.schema.pcb.PCB"),
        ):
            checker_cls.return_value.check_all.return_value = clean
            errors, _warnings = run_post_route_drc(
                output_path=pcb, manufacturer="jlcpcb", layers=2, quiet=False
            )

        out = capsys.readouterr().out
        assert errors >= 1
        assert "DRC FAILURE" in out and "does not support --severity-all" in out
        assert "memory" not in out and "crash" not in out
        assert "DRC PASSED" not in out


def _fake_cli(tmp_path: Path, *, help_body: str, drc_body: str, help_text: str = "") -> Path:
    """A kicad-cli stand-in whose ``--help`` and DRC behaviour are scripted.

    ``help_body`` / ``drc_body`` are Python statements run for ``--help`` and
    for a DRC invocation; ``help_text`` is available to them.  Every argv is
    logged to ``argv.log``.
    """
    log = tmp_path / "argv.log"
    cli = tmp_path / "kicad-cli"
    cli.write_text(
        f"#!{sys.executable}\n"
        "import json, os, signal, sys\n"
        f"log, help_text, report = {str(log)!r}, {help_text!r}, {VALID_REPORT!r}\n"
        "argv = sys.argv[1:]\n"
        "open(log, 'a').write(json.dumps(argv) + '\\n')\n"
        "if '--help' in argv:\n"
        + "".join(f"    {line}\n" for line in help_body.splitlines())
        + "".join(f"{line}\n" for line in drc_body.splitlines())
    )
    cli.chmod(cli.stat().st_mode | stat.S_IXUSR)
    return cli


_WRITE_REPORT = "open(argv[argv.index('--output') + 1], 'w').write(report)"


@pytest.mark.skipif(sys.platform == "win32", reason="needs an executable script")
class TestExitStatusWording:
    """Exit 1 is only an "unsupported option" when kicad-cli says so, and a
    board-load failure (exit 3) is not blamed on memory."""

    def test_exit1_without_unknown_argument_is_a_plain_failure(self, tmp_path):
        cli = _fake_cli(
            tmp_path,
            help_body="sys.stdout.write(help_text); sys.exit(0)",
            help_text=KICAD9_HELP,
            drc_body=(
                "sys.stderr.write('Traceback: wrapper failed: cannot open display\\n')\nsys.exit(1)"
            ),
        )

        res = run_geometric_drc(tmp_path / "b.kicad_pcb", kicad_cli=cli)

        assert res.ran is False and res.failed is True
        assert res.reason == geometric.REASON_EXIT_ERROR
        note = res.note or ""
        assert "exited with status 1" in note
        assert "cannot open display" in note
        assert "Upgrade" not in note and "does not support" not in note

    def test_describe_returncode_1_is_neutral(self):
        text = geometric.describe_returncode(1)
        assert "status 1" in text
        assert "newer KiCad" not in text and "does not support" not in text

    def test_describe_returncode_3_names_the_board_load_failure(self):
        text = geometric.describe_returncode(3)
        assert "could not load the board" in text
        assert "newer than this kicad-cli" in text and "upgrade KiCad" in text
        assert "memory" not in text

    def test_route_gate_board_load_failure_says_upgrade_not_memory(
        self, monkeypatch, tmp_path, capsys
    ):
        from unittest.mock import MagicMock, patch

        import kicad_tools.cli.runner as runner_mod
        from kicad_tools.cli.route_cmd import run_post_route_drc

        cli = _fake_cli(
            tmp_path,
            help_body="sys.stdout.write(help_text); sys.exit(0)",
            help_text=KICAD9_HELP,
            drc_body="sys.stderr.write('Failed to load board\\n'); sys.exit(3)",
        )
        monkeypatch.setattr(runner_mod, "find_kicad_cli", lambda: cli)
        pcb = tmp_path / "board.kicad_pcb"
        pcb.write_text("(kicad_pcb)")
        clean = MagicMock(error_count=0, warning_count=0, errors=[], warnings=[])

        with (
            patch("kicad_tools.validate.DRCChecker") as checker_cls,
            patch("kicad_tools.schema.pcb.PCB"),
        ):
            checker_cls.return_value.check_all.return_value = clean
            errors, _warnings = run_post_route_drc(
                output_path=pcb, manufacturer="jlcpcb", layers=2, quiet=False
            )

        out = capsys.readouterr().out
        assert errors >= 1
        assert "could not load the board file" in out and "upgrade KiCad" in out
        assert "memory" not in out and "DRC PASSED" not in out


@pytest.mark.skipif(sys.platform == "win32", reason="needs an executable script")
class TestRefillProbeIsDefinitive:
    """A killed, failed or timed-out ``--help`` probe must not be cached as
    "no ``--refill-zones``", nor silently drop the refill from a KiCad 10 run."""

    HELP10 = KICAD9_HELP + "  --refill-zones          Refill zones before running DRC\n"

    def test_killed_probe_does_not_turn_kicad10_into_a_no_refill_run(self, tmp_path):
        cli = _fake_cli(
            tmp_path,
            help_body="os.kill(os.getpid(), signal.SIGKILL)",
            drc_body=_WRITE_REPORT,
        )

        res = run_geometric_drc(tmp_path / "b.kicad_pcb", kicad_cli=cli)

        assert res.ran is True, res.note
        drc_runs = [argv for argv in _logged_argvs(tmp_path) if "--help" not in argv]
        assert "--refill-zones" in drc_runs[0], "unknown support must not drop the refill"

    def test_killed_probe_on_kicad9_retries_without_refill(self, tmp_path):
        """Issue #6775: the ``Unknown argument`` rejection is retried once
        without ``--refill-zones`` instead of failing the DRC."""
        cli = _fake_cli(
            tmp_path,
            help_body="os.kill(os.getpid(), signal.SIGKILL)",
            help_text=KICAD9_HELP,
            drc_body=(
                "if '--refill-zones' in argv:\n"
                "    sys.stdout.write('Unknown argument: --refill-zones\\n'); sys.exit(1)\n"
                + _WRITE_REPORT
            ),
        )

        res = run_geometric_drc(tmp_path / "b.kicad_pcb", kicad_cli=cli)

        assert res.ran is True and res.reason == REASON_OK, res.note
        drc_runs = [argv for argv in _logged_argvs(tmp_path) if "--help" not in argv]
        assert len(drc_runs) == 2
        assert "--refill-zones" in drc_runs[0] and "--refill-zones" not in drc_runs[1]

    def test_failed_probe_is_not_cached(self, tmp_path):
        from kicad_tools.cli import runner

        cli = _fake_cli(
            tmp_path,
            help_body="sys.exit(2)",
            drc_body=_WRITE_REPORT,
        )

        assert runner._kicad_drc_supports_refill(cli) is False
        assert runner._kicad_drc_supports_refill.inconclusive(cli) is True
        assert runner._kicad_drc_supports_refill(cli) is False
        probes = [argv for argv in _logged_argvs(tmp_path) if "--help" in argv]
        assert len(probes) == 2, "an inconclusive probe must be retried"

    def test_empty_help_is_not_a_definitive_no(self, tmp_path):
        from kicad_tools.cli import runner

        cli = _fake_cli(tmp_path, help_body="sys.exit(0)", drc_body=_WRITE_REPORT)

        assert runner._kicad_drc_supports_refill(cli) is False
        assert runner._kicad_drc_supports_refill.inconclusive(cli) is True

    def test_timed_out_probe_is_inconclusive(self, monkeypatch, tmp_path):
        from kicad_tools.cli import runner

        def hang(*_a, **_k):
            raise subprocess.TimeoutExpired("kicad-cli", 30)

        monkeypatch.setattr(runner.subprocess, "run", hang)

        assert runner._kicad_drc_supports_refill(tmp_path / "kicad-cli") is False
        assert runner._kicad_drc_supports_refill.inconclusive(tmp_path / "kicad-cli") is True

    def test_probe_has_a_timeout_and_counts_against_the_budget(self, monkeypatch, tmp_path):
        from kicad_tools.cli import runner

        seen: list[float | None] = []
        real_run = subprocess.run

        def spy(cmd, *a, **k):
            seen.append(k.get("timeout"))
            return real_run(cmd, *a, **k)

        monkeypatch.setattr(runner.subprocess, "run", spy)
        cli = _fake_cli(
            tmp_path,
            help_body="sys.stdout.write(help_text); sys.exit(0)",
            help_text=self.HELP10,
            drc_body=_WRITE_REPORT,
        )

        res = run_geometric_drc(tmp_path / "b.kicad_pcb", kicad_cli=cli, timeout=50)

        assert res.ran is True
        probe_timeout, run_timeout = seen
        assert probe_timeout == runner._REFILL_PROBE_TIMEOUT_S
        assert run_timeout is not None and run_timeout <= 50


# ---------------------------------------------------------------------------
# Exit-status classification, probe robustness, per-cause advice
# (second Judge review on PR #6746)
# ---------------------------------------------------------------------------

KICAD10_HELP = KICAD9_HELP + "  --refill-zones          Refill zones before running DRC\n"


def _flex_kicad_cli(
    tmp_path: Path,
    *,
    help_text: str = KICAD10_HELP,
    kill_help: bool = False,
    rejects: tuple[str, ...] = (),
    drc_exit: int = 0,
    drc_stderr: str = "",
) -> Path:
    """Like :func:`_script_kicad_cli`, plus a SIGKILLed ``--help`` probe and a
    DRC that fails with ``drc_exit`` / ``drc_stderr`` (writing no report)."""
    log = tmp_path / "argv.log"
    cli = tmp_path / "kicad-cli"
    cli.write_text(
        f"#!{sys.executable}\n"
        "import json, os, signal, sys\n"
        f"log, rejects, help_text, report = {str(log)!r}, {rejects!r}, {help_text!r}, "
        f"{VALID_REPORT!r}\n"
        f"kill_help, drc_exit, drc_stderr = {kill_help!r}, {drc_exit!r}, {drc_stderr!r}\n"
        "argv = sys.argv[1:]\n"
        "open(log, 'a').write(json.dumps(argv) + '\\n')\n"
        "if '--help' in argv:\n"
        "    if kill_help:\n"
        "        os.kill(os.getpid(), signal.SIGKILL)\n"
        "    sys.stdout.write(help_text); sys.exit(0)\n"
        "for arg in argv:\n"
        "    if arg in rejects:\n"
        "        sys.stdout.write('Unknown argument: ' + arg + '\\n' + help_text); sys.exit(1)\n"
        "if drc_exit:\n"
        "    sys.stderr.write(drc_stderr); sys.exit(drc_exit)\n"
        "open(argv[argv.index('--output') + 1], 'w').write(report)\n"
    )
    cli.chmod(cli.stat().st_mode | stat.S_IXUSR)
    return cli


def _drc_runs(tmp_path: Path) -> list[list[str]]:
    return [argv for argv in _logged_argvs(tmp_path) if "--help" not in argv]


def _probes(tmp_path: Path) -> list[list[str]]:
    return [argv for argv in _logged_argvs(tmp_path) if "--help" in argv]


def _route_gate_output(monkeypatch, tmp_path, cli: Path, capsys) -> tuple[int, str]:
    from unittest.mock import MagicMock, patch

    import kicad_tools.cli.runner as runner_mod
    from kicad_tools.cli.route_cmd import run_post_route_drc

    monkeypatch.setattr(runner_mod, "find_kicad_cli", lambda: cli)
    pcb = tmp_path / "board.kicad_pcb"
    pcb.write_text("(kicad_pcb)")
    clean = MagicMock(error_count=0, warning_count=0, errors=[], warnings=[])
    with (
        patch("kicad_tools.validate.DRCChecker") as checker_cls,
        patch("kicad_tools.schema.pcb.PCB"),
    ):
        checker_cls.return_value.check_all.return_value = clean
        errors, _warnings = run_post_route_drc(
            output_path=pcb, manufacturer="jlcpcb", layers=2, quiet=False
        )
    return errors, capsys.readouterr().out


@pytest.mark.skipif(sys.platform == "win32", reason="needs an executable script")
class TestExitOneIsNotAlwaysUnsupported:
    """Exit 1 without an ``Unknown argument`` line is a generic failure."""

    STDERR = "Traceback: wrapper failed: cannot open display\n"

    def test_geometric_exit_1_unrelated_error(self, tmp_path):
        cli = _flex_kicad_cli(tmp_path, drc_exit=1, drc_stderr=self.STDERR)

        res = run_geometric_drc(tmp_path / "b.kicad_pcb", kicad_cli=cli)

        assert res.ran is False
        assert res.failed is True
        assert res.reason == geometric.REASON_EXIT_ERROR
        assert res.reason != geometric.REASON_UNSUPPORTED
        note = res.note or ""
        assert "exited with status 1" in note
        assert "cannot open display" in note, "the stderr tail must be kept"
        assert "newer KiCad" not in note and "does not support" not in note
        # Not mistaken for a --refill-zones rejection: no retry.
        assert len(_drc_runs(tmp_path)) == 1

    def test_run_drc_exit_1_unrelated_error(self, tmp_path):
        from kicad_tools.cli import runner

        cli = _flex_kicad_cli(tmp_path, drc_exit=1, drc_stderr=self.STDERR)

        res = runner.run_drc(
            tmp_path / "b.kicad_pcb",
            output_path=tmp_path / "out.json",
            schematic_parity=False,
            kicad_cli=cli,
        )

        assert res.success is False
        assert "exited with status 1" in res.stderr
        assert "cannot open display" in res.stderr
        assert "newer KiCad" not in res.stderr and "usage error" not in res.stderr

    def test_route_gate_exit_1_does_not_say_upgrade(self, monkeypatch, tmp_path, capsys):
        cli = _flex_kicad_cli(tmp_path, drc_exit=1, drc_stderr=self.STDERR)

        errors, out = _route_gate_output(monkeypatch, tmp_path, cli, capsys)

        assert errors >= 1
        assert "DRC FAILURE" in out and "cannot open display" in out
        assert "Upgrade KiCad" not in out and "upgrade KiCad" not in out
        assert "memory" not in out
        assert "DRC PASSED" not in out


@pytest.mark.skipif(sys.platform == "win32", reason="needs an executable script")
class TestRefillProbeRobustness:
    """A killed/timed-out ``--help`` probe is never cached as "no refill"."""

    def test_killed_probe_still_refills_on_kicad10(self, tmp_path, caplog):
        from kicad_tools.cli import runner

        cli = _flex_kicad_cli(tmp_path, kill_help=True)

        assert runner._kicad_drc_supports_refill(cli) is False
        assert runner._kicad_drc_supports_refill.inconclusive(cli) is True
        res = run_geometric_drc(tmp_path / "b.kicad_pcb", kicad_cli=cli)

        assert res.ran is True and res.reason == REASON_OK
        runs = _drc_runs(tmp_path)
        assert len(runs) == 1
        assert "--refill-zones" in runs[0], "an unknown probe must not drop the refill"
        # Not cached: the next call probes again.
        run_geometric_drc(tmp_path / "b.kicad_pcb", kicad_cli=cli)
        assert len(_probes(tmp_path)) >= 3

    def test_killed_probe_on_kicad9_retries_without_refill(self, tmp_path, caplog):
        import logging

        cli = _flex_kicad_cli(
            tmp_path, kill_help=True, rejects=("--refill-zones",), help_text=KICAD9_HELP
        )

        with caplog.at_level(logging.WARNING):
            res = run_geometric_drc(tmp_path / "b.kicad_pcb", kicad_cli=cli)

        assert res.ran is True and res.reason == REASON_OK
        runs = _drc_runs(tmp_path)
        assert len(runs) == 2
        assert "--refill-zones" in runs[0] and "--refill-zones" not in runs[1]
        assert any(
            r.levelno == logging.WARNING and "WITHOUT --refill-zones" in r.getMessage()
            for r in caplog.records
        )
        # The rejection is definitive and memoised: the next run goes straight
        # to the no-refill command.
        run_geometric_drc(tmp_path / "b.kicad_pcb", kicad_cli=cli)
        runs = _drc_runs(tmp_path)
        assert len(runs) == 3 and "--refill-zones" not in runs[2]

    def test_definitive_kicad9_probe_warns_when_running_without_refill(self, tmp_path, caplog):
        import logging

        cli = _flex_kicad_cli(tmp_path, rejects=("--refill-zones",), help_text=KICAD9_HELP)

        with caplog.at_level(logging.WARNING):
            res = run_geometric_drc(tmp_path / "b.kicad_pcb", kicad_cli=cli)

        assert res.ran is True
        assert any("WITHOUT --refill-zones" in r.getMessage() for r in caplog.records)

    def test_probe_has_a_timeout_and_timeouts_are_not_cached(self, monkeypatch):
        from kicad_tools.cli import runner

        seen: list = []

        def fake_run(cmd, *args, **kwargs):
            seen.append(kwargs.get("timeout"))
            raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout") or 0)

        monkeypatch.setattr(runner.subprocess, "run", fake_run)

        assert runner._kicad_drc_supports_refill(FAKE_CLI) is False
        assert runner._kicad_drc_supports_refill(FAKE_CLI) is False
        assert runner._kicad_drc_supports_refill.inconclusive(FAKE_CLI) is True
        assert seen == [runner._REFILL_PROBE_TIMEOUT_S] * 2
        assert runner._REFILL_PROBE_TIMEOUT_S <= 30

    def test_failed_probe_status_is_not_cached(self, monkeypatch):
        from kicad_tools.cli import runner

        calls: list = []

        def fake_run(cmd, *args, **kwargs):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, -signal.SIGKILL, "", "")

        monkeypatch.setattr(runner.subprocess, "run", fake_run)

        assert runner._kicad_drc_supports_refill(FAKE_CLI) is False
        assert runner._kicad_drc_supports_refill(FAKE_CLI) is False
        assert runner._kicad_drc_supports_refill.inconclusive(FAKE_CLI) is True
        assert len(calls) == 2


@pytest.mark.skipif(sys.platform == "win32", reason="needs an executable script")
class TestLoadFailureAndRouteAdvice:
    def test_exit_3_load_failure_message(self, tmp_path):
        cli = _flex_kicad_cli(tmp_path, drc_exit=3, drc_stderr="Failed to load board\n")

        res = run_geometric_drc(tmp_path / "b.kicad_pcb", kicad_cli=cli)

        assert res.reason == geometric.REASON_LOAD_FAILED
        note = res.note or ""
        assert "could not load the board" in note
        assert "file format newer" in note
        assert "Failed to load board" in note  # kicad-cli's own stderr
        assert "memory" not in note

    def test_route_gate_load_failure_does_not_blame_memory(self, monkeypatch, tmp_path, capsys):
        cli = _flex_kicad_cli(tmp_path, drc_exit=3, drc_stderr="Failed to load board\n")

        errors, out = _route_gate_output(monkeypatch, tmp_path, cli, capsys)

        assert errors >= 1
        assert "could not load the board file" in out
        assert "newer KiCad file format" in out
        assert "memory" not in out
        assert "DRC PASSED" not in out

    @pytest.mark.parametrize(
        ("reason", "returncode", "must", "must_not"),
        [
            (
                geometric.REASON_UNSUPPORTED,
                1,
                ["Upgrade KiCad"],
                ["memory"],
            ),
            (
                geometric.REASON_LOAD_FAILED,
                3,
                ["could not load the board file", "upgrade KiCad"],
                ["memory"],
            ),
            (
                REASON_CRASH,
                -signal.SIGKILL,
                ["SIGKILL", "enough memory"],
                ["pgrade KiCad"],
            ),
            (
                REASON_CRASH,
                -signal.SIGSEGV,
                ["error output is quoted above"],
                ["memory", "pgrade KiCad"],
            ),
            (
                geometric.REASON_EXIT_ERROR,
                1,
                ["error output is quoted above"],
                ["memory", "pgrade KiCad"],
            ),
            (
                REASON_NO_REPORT,
                None,
                ["error output is quoted above"],
                ["memory", "pgrade KiCad"],
            ),
        ],
    )
    def test_route_advice_is_per_reason(self, reason, returncode, must, must_not):
        from kicad_tools.cli.route_cmd import _native_drc_failure_advice

        geo = geometric.GeometricDRCResult(ran=False, reason=reason, returncode=returncode)
        text = " ".join(_native_drc_failure_advice(geo))

        for needle in must:
            assert needle in text, (needle, text)
        for needle in must_not:
            assert needle not in text, (needle, text)


# ---------------------------------------------------------------------------
# The kct route pour oracle and its stranded-pour gate
# ---------------------------------------------------------------------------


class TestPourOracle:
    def test_killed_oracle_does_not_converge(self, monkeypatch, tmp_path):
        """Boards 03/04: a killed first DRC printed ``0 -> 0 (converged)``."""
        from kicad_tools.router.oracle_completion import STOP_NOT_RUN, run_oracle_completion

        _fake_kicad_cli(monkeypatch, geometric, returncode=-signal.SIGKILL, report=None)
        pcb = tmp_path / "b.kicad_pcb"
        pcb.write_text("(kicad_pcb)")

        def closer(*_a, **_k):  # pragma: no cover - must not be reached
            raise AssertionError("the closer ran on an unmeasured board")

        result = run_oracle_completion(
            pcb,
            oracle=lambda p: run_geometric_drc(p, kicad_cli=FAKE_CLI),
            closer=closer,
            nets={"GND"},
        )

        assert result.ran is False
        assert result.converged is False
        assert result.stop_reason == STOP_NOT_RUN
        assert "SIGKILL" in (result.note or "")

    def test_pour_stage_reports_unverified_not_zero(self, monkeypatch, tmp_path, capsys):
        """The stage records "unknown" so the route exits unverified, not 0."""
        import kicad_tools.cli.runner as runner_mod
        import kicad_tools.cli.stitch_cmd as stitch_mod
        import kicad_tools.core.sexp_file as sexp_mod
        from kicad_tools.cli import route_cmd

        _fake_kicad_cli(monkeypatch, geometric, returncode=-signal.SIGKILL, report=None)
        monkeypatch.setattr(runner_mod, "find_kicad_cli", lambda: FAKE_CLI)
        monkeypatch.setattr(sexp_mod, "load_pcb", lambda _p: object())
        monkeypatch.setattr(stitch_mod, "find_all_plane_nets", lambda _doc: ["GND"])
        monkeypatch.setattr(
            route_cmd,
            "_make_pour_oracle",
            lambda _args: lambda p: run_geometric_drc(p, kicad_cli=FAKE_CLI),
        )
        pcb = tmp_path / "b.kicad_pcb"
        pcb.write_text("(kicad_pcb)")
        args = SimpleNamespace(
            dry_run=False, oracle_rounds=None, allow_stranded_pour_pads=False, timeout=None
        )

        n = route_cmd._run_pour_oracle_stage(pcb, args=args)

        out = capsys.readouterr().out
        assert n == 0
        assert "converged" not in out
        assert "UNKNOWN" in out
        assert args._stranded_pour_unknown is True
        assert route_cmd._unverified_checks(args) == ["stranded-pour check"]
        assert route_cmd._unverified_exit(0, args) == route_cmd.EXIT_UNVERIFIED

        route_cmd._print_unverified_banner(args, pcb, "routed")
        banner = capsys.readouterr().out
        assert "kicad-cli failed during the stranded-pour check" in banner
        assert "Skipped for --timeout" not in banner


class TestPostRouteDrc:
    def test_crashed_native_drc_fails_without_strict(self, monkeypatch, tmp_path, capsys):
        """A killed kicad-cli is not a KiCad-less environment: no soft PASS."""
        from unittest.mock import MagicMock, patch

        import kicad_tools.cli.runner as runner_mod
        from kicad_tools.cli.route_cmd import run_post_route_drc

        monkeypatch.setattr(runner_mod, "find_kicad_cli", lambda: FAKE_CLI)
        _fake_kicad_cli(monkeypatch, geometric, returncode=-signal.SIGKILL, report=None)
        pcb = tmp_path / "board.kicad_pcb"
        pcb.write_text("(kicad_pcb)")
        clean = MagicMock(error_count=0, warning_count=0, errors=[], warnings=[])

        with (
            patch("kicad_tools.validate.DRCChecker") as checker_cls,
            patch("kicad_tools.schema.pcb.PCB"),
        ):
            checker_cls.return_value.check_all.return_value = clean
            errors, _warnings = run_post_route_drc(
                output_path=pcb, manufacturer="jlcpcb", layers=2, quiet=False
            )

        out = capsys.readouterr().out
        assert errors >= 1
        assert "DRC FAILURE" in out and "SIGKILL" in out
        assert "DRC PASSED" not in out


# ---------------------------------------------------------------------------
# The recipe pipeline gate
# ---------------------------------------------------------------------------


class TestRecipeGate:
    @pytest.mark.parametrize("require_drc", [True, False])
    def test_crashed_drc_fails_the_gate(self, monkeypatch, tmp_path, require_drc):
        """require_drc=False excuses a missing kicad-cli, never a crashed one."""
        from kicad_tools.recipes.gate import evaluate_pipeline_gate

        _fake_kicad_cli(monkeypatch, geometric, returncode=-signal.SIGKILL, report=None)
        monkeypatch.setattr("kicad_tools.cli.runner.find_kicad_cli", lambda: FAKE_CLI)
        pcb = tmp_path / "board_routed.kicad_pcb"
        pcb.write_text("(kicad_pcb)")

        res = evaluate_pipeline_gate(pcb, route_ok=True, require_drc=require_drc)

        assert res.drc_ran is False
        assert res.drc_ok is False
        assert any("geometric DRC failed" in r for r in res.reasons)


# ---------------------------------------------------------------------------
# cli/runner.py
# ---------------------------------------------------------------------------


class TestRunnerRunDrc:
    @pytest.mark.parametrize(
        ("returncode", "report"),
        [
            (-signal.SIGKILL, None),
            (-signal.SIGKILL, VALID_REPORT),
            (3, None),
            (0, None),
            (0, ""),
            (0, TRUNCATED_REPORT),
        ],
        ids=["killed", "killed-with-report", "exit-3", "no-report", "empty", "truncated"],
    )
    def test_failures_are_not_success(self, monkeypatch, tmp_path, returncode, report):
        from kicad_tools.cli import runner

        _fake_kicad_cli(monkeypatch, runner, returncode=returncode, report=report)

        res = runner.run_drc(tmp_path / "b.kicad_pcb", kicad_cli=FAKE_CLI)

        assert res.success is False
        assert "DRC failed" in res.stderr

    def test_report_is_not_pre_created(self, monkeypatch, tmp_path):
        from kicad_tools.cli import runner

        seen = _fake_kicad_cli(monkeypatch, runner, returncode=0, report=VALID_REPORT)

        res = runner.run_drc(tmp_path / "b.kicad_pcb", kicad_cli=FAKE_CLI)

        assert seen["pre_existing"] is False
        assert res.success is True
        assert res.output_path is not None and res.output_path.read_text() == VALID_REPORT
        res.output_path.unlink()

    def test_stale_report_at_output_path_is_not_reused(self, monkeypatch, tmp_path):
        from kicad_tools.cli import runner

        out = tmp_path / "drc.json"
        out.write_text(VALID_REPORT)  # from an earlier, successful run
        _fake_kicad_cli(monkeypatch, runner, returncode=-signal.SIGKILL, report=None)

        res = runner.run_drc(tmp_path / "b.kicad_pcb", output_path=out, kicad_cli=FAKE_CLI)

        assert res.success is False
        assert not out.exists()

    def test_text_report_happy_path(self, monkeypatch, tmp_path):
        from kicad_tools.cli import runner

        _fake_kicad_cli(monkeypatch, runner, returncode=0, report="** Drc report **\n")

        res = runner.run_drc(tmp_path / "b.kicad_pcb", format="report", kicad_cli=FAKE_CLI)

        assert res.success is True
        assert res.output_path is not None
        res.output_path.unlink()


class TestRunnerOtherSites:
    def test_run_erc_killed_with_partial_report_fails(self, monkeypatch, tmp_path):
        from kicad_tools.cli import runner

        _fake_kicad_cli(monkeypatch, runner, returncode=-signal.SIGKILL, report='{"sheets": [')

        res = runner.run_erc(tmp_path / "b.kicad_sch", kicad_cli=FAKE_CLI)

        assert res.success is False

    def test_run_erc_empty_report_with_exit_0_fails(self, monkeypatch, tmp_path):
        from kicad_tools.cli import runner

        _fake_kicad_cli(monkeypatch, runner, returncode=0, report=None)

        res = runner.run_erc(tmp_path / "b.kicad_sch", kicad_cli=FAKE_CLI)

        assert res.success is False

    def test_run_refill_zones_killed_fails(self, monkeypatch, tmp_path):
        from kicad_tools.cli import runner

        monkeypatch.setattr(runner, "_kicad_drc_supports_refill", lambda _cli: True)
        _fake_kicad_cli(monkeypatch, runner, returncode=-signal.SIGKILL, report=TRUNCATED_REPORT)
        pcb = tmp_path / "b.kicad_pcb"
        pcb.write_text("(kicad_pcb (version 20240108) (generator pcbnew))")

        res = runner.run_refill_zones(pcb, kicad_cli=FAKE_CLI)

        assert res.success is False
        assert "SIGKILL" in res.stderr

    def test_fill_zones_via_drc_killed_fails_and_restores(self, monkeypatch, tmp_path):
        from kicad_tools.cli import runner

        board = "(kicad_pcb (version 20240108) (generator pcbnew))\n"
        pcb = tmp_path / "b.kicad_pcb"
        pcb.write_text(board)
        monkeypatch.setattr(runner, "_kicad_drc_supports_refill", lambda _cli: True)

        def fake_run(cmd, *a, **k):
            Path(cmd[cmd.index("--output") + 1]).write_text(TRUNCATED_REPORT)
            Path(cmd[-1]).write_text("(kicad_pcb (versi")  # killed mid-save
            return subprocess.CompletedProcess(cmd, -signal.SIGKILL, "", "")

        monkeypatch.setattr(runner.subprocess, "run", fake_run)

        res = runner._run_fill_zones_via_drc(pcb, None, FAKE_CLI)

        assert res.success is False
        assert "SIGKILL" in res.stderr
        assert pcb.read_text() == board

    def test_fill_zones_via_drc_timeout_restores(self, monkeypatch, tmp_path):
        """A timeout mid ``--save-board`` restores the board too (Judge, PR #6746)."""
        from kicad_tools.cli import runner

        board = "(kicad_pcb (version 20240108) (generator pcbnew))\n"
        pcb = tmp_path / "b.kicad_pcb"
        pcb.write_text(board)
        monkeypatch.setattr(runner, "_kicad_drc_supports_refill", lambda _cli: True)

        def fake_run(cmd, *a, **k):
            Path(cmd[-1]).write_text("(kicad_pcb (versi")  # timed out mid-save
            raise subprocess.TimeoutExpired(cmd, k.get("timeout") or 1)

        monkeypatch.setattr(runner.subprocess, "run", fake_run)

        res = runner._run_fill_zones_via_drc(pcb, None, FAKE_CLI, timeout=1)

        assert res.success is False
        assert pcb.read_text() == board


# ---------------------------------------------------------------------------
# Other kicad-cli call sites
# ---------------------------------------------------------------------------


class TestOtherCallSites:
    def test_audit_erc_killed_does_not_pass(self, monkeypatch, tmp_path):
        import kicad_tools.audit.auditor as auditor_mod
        from kicad_tools.audit import ManufacturingAudit

        pcb = tmp_path / "board.kicad_pcb"
        pcb.write_text("(kicad_pcb)")
        (tmp_path / "board.kicad_sch").write_text("(kicad_sch)")
        _fake_kicad_cli(monkeypatch, auditor_mod, returncode=-signal.SIGKILL, report=None)

        status = ManufacturingAudit(pcb, manufacturer="jlcpcb")._check_erc()

        assert status.passed is False
        assert status.blocking_error_count >= 1
        assert "SIGKILL" in status.details

    def test_audit_erc_empty_report_does_not_pass(self, monkeypatch, tmp_path):
        import kicad_tools.audit.auditor as auditor_mod
        from kicad_tools.audit import ManufacturingAudit

        pcb = tmp_path / "board.kicad_pcb"
        pcb.write_text("(kicad_pcb)")
        (tmp_path / "board.kicad_sch").write_text("(kicad_sch)")
        _fake_kicad_cli(monkeypatch, auditor_mod, returncode=0, report=None)

        status = ManufacturingAudit(pcb, manufacturer="jlcpcb")._check_erc()

        assert status.passed is False

    def test_audit_geometric_merge_records_crash(self, monkeypatch, tmp_path):
        import kicad_tools.cli.runner as runner_mod
        from kicad_tools.audit import ManufacturingAudit
        from kicad_tools.audit.auditor import DRCStatus

        pcb = tmp_path / "board.kicad_pcb"
        pcb.write_text("(kicad_pcb)")
        monkeypatch.setattr(runner_mod, "find_kicad_cli", lambda: FAKE_CLI)
        _fake_kicad_cli(monkeypatch, geometric, returncode=-signal.SIGKILL, report=None)
        status = DRCStatus()

        ManufacturingAudit(pcb, manufacturer="jlcpcb")._merge_geometric_drc(status)

        assert status.geometric_drc_ran is False
        assert "SIGKILL" in (status.geometric_drc_note or "")

    def test_benchmark_metrics_crash_is_not_ran(self, monkeypatch, tmp_path):
        import kicad_tools.cli.runner as runner_mod
        from kicad_tools.benchmark.external.metrics import run_kicad_cli_drc

        monkeypatch.setattr(runner_mod, "find_kicad_cli", lambda: FAKE_CLI)
        _fake_kicad_cli(monkeypatch, geometric, returncode=-signal.SIGKILL, report=None)

        summary = run_kicad_cli_drc(tmp_path / "b.kicad_pcb")

        assert summary.ran is False
        assert "SIGKILL" in (summary.note or "")

    def test_sch_validate_erc_killed_is_reported(self, monkeypatch, tmp_path):
        from kicad_tools.cli import sch_validate

        monkeypatch.setattr(sch_validate, "find_kicad_cli", lambda: FAKE_CLI)
        _fake_kicad_cli(monkeypatch, sch_validate, returncode=-signal.SIGKILL, report=None)

        issues = sch_validate.run_erc(str(tmp_path / "b.kicad_sch"))

        assert any("ERC check failed" in i.message and "SIGKILL" in i.message for i in issues)

    def test_export_netlist_killed_fails(self, monkeypatch, tmp_path):
        from kicad_tools.cli import export_netlist

        out = tmp_path / "b.net"
        out.write_text("(export (version D))")  # stale, from an earlier run
        _fake_kicad_cli(monkeypatch, export_netlist, returncode=-signal.SIGKILL, report=None)

        ok, err = export_netlist.export_netlist(tmp_path / "b.kicad_sch", out, FAKE_CLI)

        assert ok is False
        assert "SIGKILL" in err

    def test_readiness_kicad_cli_killed_fails(self, monkeypatch, tmp_path):
        import kicad_tools.cli.runner as runner_mod
        from kicad_tools.cli import readiness_cmd

        out = tmp_path / "out.pdf"
        monkeypatch.setattr(runner_mod, "find_kicad_cli", lambda: FAKE_CLI)
        _fake_kicad_cli(monkeypatch, readiness_cmd, returncode=-signal.SIGKILL, report="%PDF-1.")

        run = readiness_cmd._run_kicad_cli(["sch", "export", "pdf", "--output", str(out)], out)

        assert run.ok is False
        assert "SIGKILL" in (run.detail or "")

    @pytest.mark.parametrize("kind", ["drc", "erc"])
    def test_preflight_empty_report_is_not_clean(self, tmp_path, kind):
        from kicad_tools.export.preflight import PreflightChecker, PreflightConfig

        pcb = tmp_path / "board.kicad_pcb"
        pcb.write_text("(kicad_pcb)")
        report = tmp_path / f"{kind}_report.json"
        report.write_text("")
        config = PreflightConfig(**{f"{kind}_report_path": report})
        checker = PreflightChecker(pcb, config=config)

        result = checker._check_drc() if kind == "drc" else checker._check_erc()

        assert result.status == "WARN"
        assert "empty" in result.message


def test_no_stray_temp_reports_left(monkeypatch, tmp_path):
    """The private report directory is removed on every exit path."""
    import tempfile

    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    for rc, report in ((-9, None), (0, ""), (0, TRUNCATED_REPORT), (0, VALID_REPORT)):
        _fake_kicad_cli(monkeypatch, geometric, returncode=rc, report=report)
        run_geometric_drc(tmp_path / "b.kicad_pcb", kicad_cli=FAKE_CLI)
    assert [p.name for p in tmp_path.iterdir()] == []
