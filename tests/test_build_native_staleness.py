"""Tests for ``kct build-native`` staleness detection (Issue #3621).

``build_native`` used to short-circuit whenever a matching-version
``router_cpp.*.so`` was installed, printing ``C++ backend installed
successfully!`` while compiling nothing.  This caused dev cycles to validate
against a stale binary when the C++ source was edited without a version bump.

These tests pin the mtime-based auto-rebuild behavior:

* When the C++ source is newer than the installed ``.so`` the command must
  fall through to a real rebuild (default behavior, no ``--force`` needed).
* When the ``.so`` is up to date the command skips and reports ``SKIPPED``
  (no longer the misleading ``installed successfully!``).

They exercise the pure decision helpers and the short-circuit branch of
``build_native`` with mocks -- no cmake / compiler is invoked.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest import mock

import kicad_tools.cli.build_native_cmd as bnc


def _make_so(router_dir: Path, mtime: float) -> Path:
    """Create a fake installed router_cpp .so with a fixed mtime."""
    so_file = router_dir / "router_cpp.cpython-311-fake.so"
    so_file.write_bytes(b"\x00")
    os.utime(so_file, (mtime, mtime))
    return so_file


def _make_cpp_source(cpp_dir: Path, name: str, mtime: float) -> Path:
    """Create a fake C++ source/header file with a fixed mtime."""
    src_dir = cpp_dir / "src"
    src_dir.mkdir(parents=True, exist_ok=True)
    source = src_dir / name
    source.write_text("// fake\n")
    os.utime(source, (mtime, mtime))
    return source


class TestNewestCppSourceMtime:
    def test_returns_none_when_no_sources(self, tmp_path: Path) -> None:
        assert bnc._newest_cpp_source_mtime(tmp_path) is None

    def test_returns_newest_across_extensions(self, tmp_path: Path) -> None:
        _make_cpp_source(tmp_path, "pathfinder.cpp", 100.0)
        (tmp_path / "include").mkdir()
        hpp = tmp_path / "include" / "grid.hpp"
        hpp.write_text("// header\n")
        os.utime(hpp, (300.0, 300.0))
        # CMakeLists.txt is also a build input
        cmake = tmp_path / "CMakeLists.txt"
        cmake.write_text("cmake\n")
        os.utime(cmake, (200.0, 200.0))

        assert bnc._newest_cpp_source_mtime(tmp_path) == 300.0


class TestIsSoStale:
    def test_false_when_no_so(self, tmp_path: Path, monkeypatch) -> None:
        cpp_dir = tmp_path / "cpp"
        _make_cpp_source(cpp_dir, "pathfinder.cpp", 100.0)
        monkeypatch.setattr(bnc, "_get_cpp_source_dir", lambda: cpp_dir)
        # router_dir has no .so
        assert bnc._is_so_stale(tmp_path) is False

    def test_false_when_so_newer(self, tmp_path: Path, monkeypatch) -> None:
        router_dir = tmp_path / "router"
        router_dir.mkdir()
        cpp_dir = router_dir / "cpp"
        _make_cpp_source(cpp_dir, "pathfinder.cpp", 100.0)
        _make_so(router_dir, 200.0)  # so newer than source
        monkeypatch.setattr(bnc, "_get_cpp_source_dir", lambda: cpp_dir)

        assert bnc._is_so_stale(router_dir) is False

    def test_true_when_source_newer(self, tmp_path: Path, monkeypatch) -> None:
        router_dir = tmp_path / "router"
        router_dir.mkdir()
        cpp_dir = router_dir / "cpp"
        _make_so(router_dir, 100.0)
        _make_cpp_source(cpp_dir, "pathfinder.cpp", 200.0)  # source newer
        monkeypatch.setattr(bnc, "_get_cpp_source_dir", lambda: cpp_dir)

        assert bnc._is_so_stale(router_dir) is True

    def test_false_when_no_cpp_dir(self, tmp_path: Path, monkeypatch) -> None:
        router_dir = tmp_path / "router"
        router_dir.mkdir()
        _make_so(router_dir, 100.0)
        # No source tree (pip wheel without bundled sources)
        monkeypatch.setattr(bnc, "_get_cpp_source_dir", lambda: None)

        assert bnc._is_so_stale(router_dir) is False


def _patch_available(monkeypatch, available: bool) -> None:
    """Make the lazy ``is_cpp_available`` import resolve to a stub."""
    import kicad_tools.router.cpp_backend as cpp_backend

    monkeypatch.setattr(cpp_backend, "is_cpp_available", lambda: available)


class TestBuildNativeShortCircuit:
    def test_skips_and_marks_skipped_when_up_to_date(self, monkeypatch) -> None:
        _patch_available(monkeypatch, True)
        monkeypatch.setattr(bnc, "_is_so_stale", lambda _router_dir: False)
        fake_so = Path("/fake/router/router_cpp.cpython-311.so")
        monkeypatch.setattr(bnc, "_find_installed_so", lambda _router_dir: fake_so)

        result = bnc.build_native(force=False)

        assert result.success is True
        assert result.backend_installed is True
        assert result.skipped is True
        assert result.so_path == fake_so

        text = bnc.format_result_text(result)
        assert "SKIPPED rebuild" in text
        assert "installed successfully" not in text

    def test_rebuilds_when_source_is_stale(self, monkeypatch) -> None:
        _patch_available(monkeypatch, True)
        monkeypatch.setattr(bnc, "_is_so_stale", lambda _router_dir: True)

        # Stub out the heavy build steps so the test stays fast: make the
        # prerequisite check fail immediately *after* the short-circuit, which
        # proves we fell through instead of skipping.
        sentinel = mock.MagicMock(return_value=(False, "cmake stub: reached build path"))
        monkeypatch.setattr(bnc, "_check_cmake", sentinel)

        result = bnc.build_native(force=False)

        # Did NOT short-circuit: it reached the prerequisite checks.
        assert sentinel.call_count == 1
        assert result.skipped is False
        assert result.success is False
        assert result.error_message == "cmake stub: reached build path"

    def test_force_bypasses_short_circuit_entirely(self, monkeypatch) -> None:
        _patch_available(monkeypatch, True)
        # _is_so_stale must NOT be consulted when force=True.
        stale_spy = mock.MagicMock(return_value=False)
        monkeypatch.setattr(bnc, "_is_so_stale", stale_spy)
        sentinel = mock.MagicMock(return_value=(False, "cmake stub"))
        monkeypatch.setattr(bnc, "_check_cmake", sentinel)

        result = bnc.build_native(force=True)

        assert stale_spy.call_count == 0
        assert sentinel.call_count == 1
        assert result.skipped is False


class TestFormatResultText:
    def test_skipped_message_distinct_from_installed(self) -> None:
        skipped = bnc.BuildResult(success=True, backend_installed=True, skipped=True)
        installed = bnc.BuildResult(success=True, backend_installed=True, skipped=False)

        skipped_text = bnc.format_result_text(skipped)
        installed_text = bnc.format_result_text(installed)

        assert "SKIPPED rebuild" in skipped_text
        assert "installed successfully" not in skipped_text
        assert "installed successfully" in installed_text


def test_build_result_to_dict_includes_skipped() -> None:
    result = bnc.BuildResult(success=True, skipped=True)
    assert result.to_dict()["skipped"] is True
