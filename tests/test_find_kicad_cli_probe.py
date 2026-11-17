"""Tests for find_kicad_cli() probing and the opt-in container fallback (#5903)."""

import stat
from pathlib import Path

import pytest

from kicad_tools.cli import runner


def _stub(tmp_path: Path, body: str) -> Path:
    d = tmp_path / "bin"
    d.mkdir(exist_ok=True)
    p = d / "kicad-cli"
    p.write_text("#!/bin/sh\n" + body + "\n")
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return d


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    real = runner.find_kicad_cli
    real.cache_clear()
    monkeypatch.setattr(runner, "KICAD_CLI_PROBE_TIMEOUT", 0.5)
    yield
    real.cache_clear()


def _only_path(monkeypatch, d: Path):
    monkeypatch.setenv("PATH", f"{d}:/usr/bin:/bin")
    monkeypatch.setattr(runner.Path, "exists", lambda self: False)
    # shutil.which does not use Path.exists, so PATH lookup still works.


def test_healthy_binary_returned(tmp_path, monkeypatch):
    d = _stub(tmp_path, "echo 10.0.0")
    _only_path(monkeypatch, d)
    assert runner.find_kicad_cli() == d / "kicad-cli"


def test_hanging_binary_treated_as_missing(tmp_path, monkeypatch):
    d = _stub(tmp_path, "sleep 30")
    _only_path(monkeypatch, d)
    assert runner.find_kicad_cli() is None


def test_nonzero_exit_treated_as_missing(tmp_path, monkeypatch):
    d = _stub(tmp_path, "exit 3")
    _only_path(monkeypatch, d)
    assert runner.find_kicad_cli() is None


def test_result_is_memoized(tmp_path, monkeypatch):
    # A generous budget: a timed-out probe is (correctly) not memoized (#5932),
    # so a loaded xdist worker must not turn this into a re-probe.
    monkeypatch.setattr(runner, "KICAD_CLI_PROBE_TIMEOUT", 10)
    d = _stub(tmp_path, "echo 10.0.0")
    _only_path(monkeypatch, d)
    calls = []
    real = runner._probe_kicad_cli
    monkeypatch.setattr(
        runner, "_probe_kicad_cli", lambda p, **kw: calls.append(p) or real(p, **kw)
    )
    runner.find_kicad_cli()
    runner.find_kicad_cli()
    assert len(calls) == 1


def test_docker_fallback_opt_in(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "find_kicad_cli", lambda: None)
    monkeypatch.setattr(runner.shutil, "which", lambda n: "/usr/bin/docker")
    pcb = tmp_path / "a.kicad_pcb"
    pcb.write_text("x")
    out = tmp_path / "o.svg"
    captured = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        out.write_text("<svg/>")

        class R:
            stdout = stderr = ""
            returncode = 0

        return R()

    monkeypatch.setattr(runner.subprocess, "run", fake_run)

    monkeypatch.delenv(runner.KICAD_DOCKER_ENV, raising=False)
    res = runner.run_pcb_export_svg(pcb, out, ["F.Cu"])
    assert not res.success and "cmd" not in captured

    monkeypatch.setenv(runner.KICAD_DOCKER_ENV, "1")
    res = runner.run_pcb_export_svg(pcb, out, ["F.Cu"])
    assert res.success
    cmd = captured["cmd"]
    assert cmd[:3] == ["docker", "run", "--rm"]
    assert "kicad-cli" in cmd and "/work/a.kicad_pcb" in cmd
