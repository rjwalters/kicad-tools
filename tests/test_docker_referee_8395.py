"""KCT_KICAD_DOCKER=1 routes prune / optimize-traces referees onto the pinned image (#8395)."""

import contextlib
from pathlib import Path

import pytest

from kicad_tools.cli import kicad_toolchain as kt
from kicad_tools.cli import optimize_cmd


@pytest.fixture
def entered(monkeypatch):
    calls = []

    @contextlib.contextmanager
    def fake(mounts=(), *, required_version=kt.PINNED_KICAD_VERSION):
        calls.append((list(mounts), required_version))
        yield

    monkeypatch.setattr(kt, "evidence_toolchain", fake)
    return calls


def test_helper_unset_does_not_enter(monkeypatch, entered):
    monkeypatch.delenv(kt.KICAD_DOCKER_ENV, raising=False)
    assert kt.run_pinned_if_docker(lambda: 7, [Path("/x")]) == 7
    assert entered == []


def test_helper_set_enters_without_version_pin(monkeypatch, entered):
    monkeypatch.setenv(kt.KICAD_DOCKER_ENV, "1")
    assert kt.run_pinned_if_docker(lambda: 0, [Path("/x")]) == 0
    assert entered == [([Path("/x")], None)]


def test_helper_environment_error_is_clear_failure(monkeypatch, capsys):
    monkeypatch.setenv(kt.KICAD_DOCKER_ENV, "1")

    @contextlib.contextmanager
    def boom(mounts=(), *, required_version=None):
        raise kt.KiCadEnvironmentError("docker missing")
        yield

    monkeypatch.setattr(kt, "evidence_toolchain", boom)
    ran = []
    assert kt.run_pinned_if_docker(lambda: ran.append(1) or 0) == 1
    assert not ran
    assert "docker missing" in capsys.readouterr().err


@pytest.mark.parametrize("set_env", [True, False])
def test_optimize_traces_wraps_iff_env(monkeypatch, entered, set_env, tmp_path):
    if set_env:
        monkeypatch.setenv(kt.KICAD_DOCKER_ENV, "1")
    else:
        monkeypatch.delenv(kt.KICAD_DOCKER_ENV, raising=False)
    monkeypatch.setattr(optimize_cmd, "_main", lambda argv=None: 0)
    out = tmp_path / "o" / "b.kicad_pcb"
    assert optimize_cmd.main([str(tmp_path / "b.kicad_pcb"), "-o", str(out)]) == 0
    if set_env:
        assert entered == [([tmp_path, out.parent], None)]
    else:
        assert entered == []
