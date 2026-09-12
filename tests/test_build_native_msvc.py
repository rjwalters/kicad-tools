"""Tests for MSVC cl.exe detection in ``kct build-native`` (Windows support).

These tests exercise ``_find_msvc`` and the MSVC branch of ``_check_compiler``
with mocks -- no real compiler or vswhere is invoked.
"""

from __future__ import annotations

import subprocess
from unittest import mock

import kicad_tools.cli.build_native_cmd as bnc


class TestFindMsvc:
    def test_returns_none_when_no_cl_and_no_vswhere(self, monkeypatch):
        monkeypatch.setattr(bnc.shutil, "which", lambda name: None)
        # Default vswhere path looks non-existent -> no vswhere anywhere.
        monkeypatch.setattr(bnc.Path, "exists", lambda self: False)
        assert bnc._find_msvc() is None

    def test_cl_on_path_takes_priority(self, monkeypatch):
        fake_cl = r"C:\vctools\bin\cl.exe"
        monkeypatch.setattr(
            bnc.shutil,
            "which",
            lambda name: fake_cl if name == "cl" else None,
        )
        assert bnc._find_msvc() == fake_cl

    def test_vswhere_fallback_returns_cl_path(self, monkeypatch):
        # cl is not on PATH and vswhere isn't on PATH either, so _find_msvc
        # must fall back to the well-known default vswhere.exe location.
        fake_cl = r"C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Tools\MSVC\14.43.0\bin\Hostx64\x64\cl.exe"

        monkeypatch.setattr(bnc.shutil, "which", lambda name: None)
        monkeypatch.setattr(bnc.Path, "exists", lambda self: True)

        def fake_run(args, **kwargs):
            m = mock.MagicMock()
            m.returncode = 0
            m.stdout = fake_cl + "\n"
            return m

        monkeypatch.setattr(bnc.subprocess, "run", fake_run)

        assert bnc._find_msvc() == fake_cl

    def test_vswhere_failure_returns_none(self, monkeypatch):
        monkeypatch.setattr(bnc.shutil, "which", lambda name: None)
        monkeypatch.setattr(bnc.Path, "exists", lambda self: True)

        def failing_run(args, **kwargs):
            m = mock.MagicMock()
            m.returncode = 1
            m.stdout = ""
            return m

        monkeypatch.setattr(bnc.subprocess, "run", failing_run)

        assert bnc._find_msvc() is None

    def test_vswhere_timeout_returns_none(self, monkeypatch):
        monkeypatch.setattr(bnc.shutil, "which", lambda name: None)
        monkeypatch.setattr(bnc.Path, "exists", lambda self: True)

        def timeout_run(args, **kwargs):
            raise subprocess.TimeoutExpired(args, 10)

        monkeypatch.setattr(bnc.subprocess, "run", timeout_run)

        assert bnc._find_msvc() is None


class TestCheckCompilerMsvc:
    def test_msvc_accepted_when_unix_compilers_absent(self, monkeypatch):
        fake_cl = r"C:\BuildTools\cl.exe"
        # No clang++ / g++ on PATH; MSVC found via _find_msvc.
        monkeypatch.setattr(bnc.shutil, "which", lambda name: None)
        monkeypatch.setattr(bnc, "_find_msvc", lambda: fake_cl)

        ok, path = bnc._check_compiler()

        assert ok is True
        assert path == fake_cl

    def test_unix_compiler_preferred_over_msvc(self, monkeypatch):
        fake_cl = r"C:\BuildTools\cl.exe"
        fake_clangpp = "/usr/bin/clang++"

        def which(name):
            return fake_clangpp if name == "clang++" else None

        monkeypatch.setattr(bnc.shutil, "which", which)
        monkeypatch.setattr(bnc, "_find_msvc", lambda: fake_cl)
        monkeypatch.setattr(
            bnc.subprocess,
            "run",
            lambda *a, **k: mock.MagicMock(returncode=0),
        )

        ok, path = bnc._check_compiler()

        assert ok is True
        assert path == fake_clangpp

    def test_error_message_mentions_windows_when_all_absent(self, monkeypatch):
        monkeypatch.setattr(bnc.shutil, "which", lambda name: None)
        monkeypatch.setattr(bnc, "_find_msvc", lambda: None)

        ok, msg = bnc._check_compiler()

        assert ok is False
        assert msg is not None
        assert "Windows" in msg or "Visual Studio" in msg
