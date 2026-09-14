"""Tests for MSVC cl.exe detection in ``kct build-native`` (Windows support).

These tests exercise ``_find_msvc`` and the MSVC branch of ``_check_compiler``
with mocks -- no real compiler or vswhere is invoked.
"""

from __future__ import annotations

import subprocess
from types import SimpleNamespace
from unittest import mock

import pytest

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


@pytest.mark.parametrize(
    ("platform", "generator", "msvc", "expected_platform"),
    [
        ("win32", None, True, "x64"),
        ("win32", "Visual Studio 17 2022", True, "x64"),
        ("win32", "Ninja", True, None),
        ("win32", "Ninja Multi-Config", True, None),
        ("win32", "NMake Makefiles", True, None),
        ("win32", "Unix Makefiles", True, None),
        ("win32", None, False, None),
        ("linux", None, True, None),
    ],
)
def test_build_native_respects_generator_platform_support(
    monkeypatch, tmp_path, platform, generator, msvc, expected_platform
):
    """Exercise the configure invocation without compiling a native extension."""
    (tmp_path / "CMakeLists.txt").write_text("cmake_minimum_required(VERSION 3.15)\n")
    if generator is None:
        monkeypatch.delenv("CMAKE_GENERATOR", raising=False)
    else:
        monkeypatch.setenv("CMAKE_GENERATOR", generator)
    monkeypatch.setattr(bnc, "sys", SimpleNamespace(platform=platform, executable="python"))
    monkeypatch.setattr(bnc, "_get_package_root", lambda: tmp_path)
    monkeypatch.setattr(bnc, "_get_project_root", lambda: tmp_path)
    monkeypatch.setattr(bnc, "_check_cmake", lambda: (True, "cmake"))
    # A clang installation can coexist with MSVC; detection is not selection
    # of a CMake generator, so this must still honor CMAKE_GENERATOR.
    monkeypatch.setattr(bnc, "_check_compiler", lambda: (True, "clang++"))
    monkeypatch.setattr(bnc, "_find_msvc", lambda: "C:/VS/cl.exe" if msvc else None)
    monkeypatch.setattr(bnc, "_install_nanobind", lambda verbose: (True, None))
    monkeypatch.setattr(bnc, "_get_nanobind_cmake_dir", lambda: tmp_path)
    configure = mock.Mock(
        return_value=subprocess.CompletedProcess([], 1, stderr="configure probe stopped")
    )
    monkeypatch.setattr(bnc.subprocess, "run", configure)

    result = bnc.build_native(force=True)

    assert result.error_message == "cmake configure failed: configure probe stopped"
    configure.assert_called_once()
    args = configure.call_args.args[0]
    if expected_platform is None:
        assert "-A" not in args
    else:
        assert args[args.index("-A") + 1] == expected_platform
