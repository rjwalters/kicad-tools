"""The session fixture that routes kicad-cli via the pinned image (#8779)."""

from __future__ import annotations

import contextlib

import pytest

from kicad_tools.cli import kicad_toolchain as tc
from tests import kicad_docker_session as sess


def _stub(monkeypatch):
    calls = []

    @contextlib.contextmanager
    def fake(*args, **kwargs):
        calls.append(1)
        yield "toolchain"

    monkeypatch.setattr(tc, "evidence_toolchain", fake)
    return calls


def test_noop_without_env(monkeypatch):
    monkeypatch.delenv(tc.KICAD_DOCKER_ENV, raising=False)
    calls = _stub(monkeypatch)
    with sess.pinned_session() as toolchain:
        assert toolchain is None
    assert calls == []


def test_activates_with_env(monkeypatch):
    monkeypatch.setenv(tc.KICAD_DOCKER_ENV, "1")
    calls = _stub(monkeypatch)
    with sess.pinned_session() as toolchain:
        assert toolchain == "toolchain"
    assert calls == [1]


def test_env_error_propagates_not_host_fallback(monkeypatch):
    monkeypatch.setenv(tc.KICAD_DOCKER_ENV, "1")

    @contextlib.contextmanager
    def failing(*args, **kwargs):
        raise tc.KiCadEnvironmentError("docker is not on PATH")
        yield

    monkeypatch.setattr(tc, "evidence_toolchain", failing)
    with pytest.raises(tc.KiCadEnvironmentError), sess.pinned_session():
        pass


def test_header_names_pinned_image_under_env(monkeypatch):
    monkeypatch.setenv(tc.KICAD_DOCKER_ENV, "1")
    (line,) = sess.report_header()
    assert tc.PINNED_KICAD_VERSION in line and "pinned image" in line


def test_header_names_host_cli_without_env(monkeypatch):
    monkeypatch.delenv(tc.KICAD_DOCKER_ENV, raising=False)
    (line,) = sess.report_header()
    assert line.startswith("kicad-cli: ") and "host" in line
