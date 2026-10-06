"""find_kicad_cli must not memoize a transient probe timeout as "absent" (#5932)."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from kicad_tools.cli import runner


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    runner.find_kicad_cli.cache_clear()
    monkeypatch.delenv(runner.KICAD_CLI_PROBE_TIMEOUT_ENV, raising=False)
    monkeypatch.setattr(runner, "KICAD_CLI_PROBE_TIMEOUT", 0.5)
    yield
    runner.find_kicad_cli.cache_clear()


FAKE = Path("/fake/kicad-cli")


class _ScriptedProbe:
    """Stand-in for ``_probe_kicad_cli`` returning scripted verdicts."""

    def __init__(self, verdicts):
        self.verdicts = list(verdicts)
        self.timeouts: list[float] = []

    def __call__(self, path, timeout=None):
        self.timeouts.append(timeout)
        return self.verdicts.pop(0)


def _one_candidate(monkeypatch, probe):
    monkeypatch.setattr(runner, "_kicad_cli_candidates", lambda: [FAKE])
    monkeypatch.setattr(runner, "_probe_kicad_cli", probe)


def test_timeout_then_success_is_retried_within_one_call(monkeypatch):
    probe = _ScriptedProbe([None, True])
    _one_candidate(monkeypatch, probe)

    assert runner.find_kicad_cli() == FAKE
    # The retry used a longer budget than the first attempt.
    assert probe.timeouts == [0.5, 0.5 * runner.KICAD_CLI_PROBE_RETRY_FACTOR]
    # The success is memoized: no further probing.
    assert runner.find_kicad_cli() == FAKE
    assert len(probe.timeouts) == 2


def test_persistent_timeout_is_not_cached_as_absent(monkeypatch):
    # Both attempts of the first call time out; the next call succeeds.
    probe = _ScriptedProbe([None, None, True])
    _one_candidate(monkeypatch, probe)

    assert runner.find_kicad_cli() is None
    lookup = runner.last_kicad_cli_lookup()
    assert lookup is not None and lookup.probe_failed
    assert lookup.status == runner.KICAD_CLI_PROBE_FAILED
    assert "probe failed" in lookup.reason and "not installed" not in lookup.reason
    # No stale negative cache entry.
    assert runner._lookup_cache == []

    # The next call re-probes and now finds it.
    assert runner.find_kicad_cli() == FAKE
    assert len(probe.timeouts) == 3
    assert runner.locate_kicad_cli().found


def test_definitive_not_found_is_cached(monkeypatch):
    calls = []

    def candidates():
        calls.append(1)
        return []

    monkeypatch.setattr(runner, "_kicad_cli_candidates", candidates)
    assert runner.find_kicad_cli() is None
    assert runner.find_kicad_cli() is None
    assert len(calls) == 1
    assert runner.locate_kicad_cli().status == runner.KICAD_CLI_NOT_FOUND
    assert "not installed" in runner.kicad_cli_unavailable_message()


def test_broken_binary_is_definitive_and_not_retried(monkeypatch):
    probe = _ScriptedProbe([False])
    _one_candidate(monkeypatch, probe)
    assert runner.find_kicad_cli() is None
    assert runner.locate_kicad_cli().status == runner.KICAD_CLI_NOT_FOUND
    assert len(probe.timeouts) == 1


def test_probe_classifies_outcomes(monkeypatch, tmp_path):
    def raise_(exc):
        def run(*a, **kw):
            raise exc

        return run

    monkeypatch.setattr(
        runner.subprocess, "run", raise_(runner.subprocess.TimeoutExpired(["x"], 1))
    )
    assert runner._probe_kicad_cli(FAKE) is None
    monkeypatch.setattr(runner.subprocess, "run", raise_(FileNotFoundError()))
    assert runner._probe_kicad_cli(FAKE) is False
    monkeypatch.setattr(runner.subprocess, "run", raise_(OSError(35, "EAGAIN")))
    assert runner._probe_kicad_cli(FAKE) is None


def test_env_overrides_probe_timeout(monkeypatch):
    monkeypatch.setenv(runner.KICAD_CLI_PROBE_TIMEOUT_ENV, "30")
    assert runner._probe_timeout() == 30.0
    monkeypatch.setenv(runner.KICAD_CLI_PROBE_TIMEOUT_ENV, "junk")
    assert runner._probe_timeout() == 0.5


def test_real_slow_first_start_is_found(tmp_path, monkeypatch):
    """A binary that stalls only on its first start is found via the retry."""
    marker = tmp_path / "warm"
    d = tmp_path / "bin"
    d.mkdir()
    cli = d / "kicad-cli"
    cli.write_text(
        f'#!/bin/sh\nif [ ! -e "{marker}" ]; then touch "{marker}"; exec sleep 30; fi\necho 10.0.0\n'
    )
    cli.chmod(cli.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(runner, "_kicad_cli_candidates", lambda: [cli])
    monkeypatch.setattr(runner, "KICAD_CLI_PROBE_TIMEOUT", 3)

    assert runner.find_kicad_cli() == cli
    assert runner.locate_kicad_cli().found


def test_route_zone_fill_warns_loudly_on_probe_failure(monkeypatch, tmp_path, capsys):
    from kicad_tools.cli import route_cmd

    _one_candidate(monkeypatch, _ScriptedProbe([None, None]))
    pcb = tmp_path / "b.kicad_pcb"
    pcb.write_text("(kicad_pcb (zone (net 1)))")

    route_cmd._fill_zones_after_route(pcb, quiet=True)

    err = capsys.readouterr().err
    assert "WARNING: zone fill skipped" in err
    assert "probe failed" in err
    assert "not installed" not in err


def test_route_zone_fill_warns_when_not_installed(monkeypatch, tmp_path, capsys):
    from kicad_tools.cli import route_cmd

    monkeypatch.setattr(runner, "_kicad_cli_candidates", lambda: [])
    pcb = tmp_path / "b.kicad_pcb"
    pcb.write_text("(kicad_pcb (zone (net 1)))")

    route_cmd._fill_zones_after_route(pcb, quiet=False)

    out, err = capsys.readouterr()
    assert "Zone fill: skipped (kicad-cli not installed)" in out
    assert "WARNING: zone fill skipped" in err


def test_pipeline_zones_reports_probe_failure_distinctly(monkeypatch, tmp_path, capsys):
    from rich.console import Console

    from kicad_tools.cli.pipeline_cmd import (
        PipelineContext,
        _run_step_zones,
        _run_step_zones_refill,
    )

    pcb = tmp_path / "b.kicad_pcb"
    pcb.write_text("(kicad_pcb)")
    ctx = PipelineContext(pcb_file=pcb, quiet=True)

    _one_candidate(monkeypatch, _ScriptedProbe([None, None, None, None]))
    for step in (_run_step_zones, _run_step_zones_refill):
        result = step(ctx, Console(quiet=True))
        assert result.skipped
        assert "probe failed" in result.message
        assert "not installed" not in result.message
    err = capsys.readouterr().err
    assert "WARNING: zones fill skipped" in err
    assert "WARNING: zones refill skipped" in err
