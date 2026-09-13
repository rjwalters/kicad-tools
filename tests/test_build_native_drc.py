"""Tests for ``kct build-native`` building and installing the DRC extension.

Issue #5240: ``src/kicad_tools/drc/cpp`` (the C++ pad-to-pad clearance
checker, added in #1719) was never added as a subdirectory of the root
``CMakeLists.txt`` -- unlike ``placement_cpp`` (#5256), which was compiled by
the root build but silently discarded, ``drc_cpp`` was never compiled at
all. ``IncrementalDRC`` (used by placement's courtyard/clearance checks, the
manufacturable-baseline board tests, and every board's CI DRC pass) always
ran its pure-Python ``_check_pair_clearance_python`` loop, for the
extension's entire history, with no error or warning anywhere.

Measured impact in this checkout: a synthetic 100-pad-per-footprint pair
(QFP-class part, the realistic worst case for the O(P1 x P2) inner loop)
went from a ~20.6ms median Python call to a ~0.27ms median C++ call -- a
~77x speedup, with identical results (sub-micron floating-point precision
difference only, well within the existing epsilon tolerance). A typical
2-pad passive pair shows a far smaller ~1.2x win, since per-call nanobind
marshalling overhead dominates at that pad count -- the extension helps most
on high-pin-count parts (QFN/QFP/BGA), not simple resistors/capacitors.

These tests exercise the pure decision helpers and the tail of
``build_native`` with mocks -- no cmake / compiler is invoked, mirroring the
conventions in ``test_build_native_staleness.py`` and
``test_build_native_placement.py``.
"""

from __future__ import annotations

import importlib.machinery
import os
from pathlib import Path
from unittest import mock

import pytest

import kicad_tools.cli.build_native_cmd as bnc

RUNNING_ABI_SUFFIX = importlib.machinery.EXTENSION_SUFFIXES[0]


# ---------------------------------------------------------------------------
# Path/discovery helpers
# ---------------------------------------------------------------------------


class TestDrcDirHelpers:
    def test_get_drc_dir_is_sibling_of_router(self) -> None:
        drc_dir = bnc._get_drc_dir()
        assert drc_dir.name == "drc"
        assert drc_dir.parent == bnc._get_package_root()

    def test_get_drc_cpp_source_dir_returns_none_when_absent(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setattr(bnc, "_get_package_root", lambda: tmp_path)
        assert bnc._get_drc_cpp_source_dir() is None

    def test_get_drc_cpp_source_dir_returns_path_when_present(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setattr(bnc, "_get_package_root", lambda: tmp_path)
        cpp_dir = tmp_path / "drc" / "cpp"
        cpp_dir.mkdir(parents=True)
        assert bnc._get_drc_cpp_source_dir() == cpp_dir


def _touch(directory: Path, name: str, mtime: float) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_bytes(b"\x00")
    os.utime(path, (mtime, mtime))
    return path


class TestFindInstalledSoDrcModuleName:
    def test_finds_drc_cpp_when_named(self, tmp_path: Path) -> None:
        # A router_cpp file in the same directory must NOT be picked up when
        # asking for drc_cpp -- the extensions coexist.
        _touch(tmp_path, f"router_cpp{RUNNING_ABI_SUFFIX}", 100.0)
        expected = _touch(tmp_path, f"drc_cpp{RUNNING_ABI_SUFFIX}", 100.0)

        found = bnc._find_installed_so(tmp_path, module_name="drc_cpp")

        assert found == expected

    def test_drc_cpp_not_found_when_only_router_present(self, tmp_path: Path) -> None:
        _touch(tmp_path, f"router_cpp{RUNNING_ABI_SUFFIX}", 100.0)
        assert bnc._find_installed_so(tmp_path, module_name="drc_cpp") is None


class TestIsSoStaleDrcModuleName:
    def test_drc_stale_when_source_newer(self, tmp_path: Path) -> None:
        drc_dir = tmp_path / "drc"
        cpp_dir = drc_dir / "cpp"
        _touch(drc_dir, f"drc_cpp{RUNNING_ABI_SUFFIX}", 100.0)
        src_dir = cpp_dir / "src"
        src_dir.mkdir(parents=True)
        source = src_dir / "drc_clearance.cpp"
        source.write_text("// fake\n")
        os.utime(source, (200.0, 200.0))

        assert bnc._is_so_stale(drc_dir, module_name="drc_cpp", cpp_source_dir=cpp_dir) is True

    def test_drc_not_stale_when_so_newer(self, tmp_path: Path) -> None:
        drc_dir = tmp_path / "drc"
        cpp_dir = drc_dir / "cpp"
        src_dir = cpp_dir / "src"
        src_dir.mkdir(parents=True)
        source = src_dir / "drc_clearance.cpp"
        source.write_text("// fake\n")
        os.utime(source, (100.0, 100.0))
        _touch(drc_dir, f"drc_cpp{RUNNING_ABI_SUFFIX}", 200.0)

        assert bnc._is_so_stale(drc_dir, module_name="drc_cpp", cpp_source_dir=cpp_dir) is False


# ---------------------------------------------------------------------------
# _drc_backend_up_to_date
# ---------------------------------------------------------------------------


class TestDrcBackendUpToDate:
    def test_true_when_no_drc_source_tree(self, monkeypatch) -> None:
        """A source-less installed wheel has nothing to build -- treated as
        up to date, not stale, so it never blocks the router short-circuit."""
        monkeypatch.setattr(bnc, "_get_drc_cpp_source_dir", lambda: None)
        assert bnc._drc_backend_up_to_date() is True

    def test_false_when_source_present_but_not_importable(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setattr(bnc, "_get_drc_cpp_source_dir", lambda: tmp_path)
        monkeypatch.setitem(__import__("sys").modules, "kicad_tools.drc.cpp_backend", None)
        assert bnc._drc_backend_up_to_date() is False

    def test_false_when_available_but_stale(self, tmp_path: Path, monkeypatch) -> None:
        import kicad_tools.drc.cpp_backend as drc_cpp_backend

        monkeypatch.setattr(bnc, "_get_drc_cpp_source_dir", lambda: tmp_path)
        monkeypatch.setattr(drc_cpp_backend, "is_cpp_available", lambda: True)
        monkeypatch.setattr(bnc, "_is_so_stale", lambda *_a, **_k: True)

        assert bnc._drc_backend_up_to_date() is False

    def test_true_when_available_and_not_stale(self, tmp_path: Path, monkeypatch) -> None:
        import kicad_tools.drc.cpp_backend as drc_cpp_backend

        monkeypatch.setattr(bnc, "_get_drc_cpp_source_dir", lambda: tmp_path)
        monkeypatch.setattr(drc_cpp_backend, "is_cpp_available", lambda: True)
        monkeypatch.setattr(bnc, "_is_so_stale", lambda *_a, **_k: False)

        assert bnc._drc_backend_up_to_date() is True

    def test_false_when_not_available(self, tmp_path: Path, monkeypatch) -> None:
        import kicad_tools.drc.cpp_backend as drc_cpp_backend

        monkeypatch.setattr(bnc, "_get_drc_cpp_source_dir", lambda: tmp_path)
        monkeypatch.setattr(drc_cpp_backend, "is_cpp_available", lambda: False)

        assert bnc._drc_backend_up_to_date() is False


# ---------------------------------------------------------------------------
# Short-circuit integration: router-up-to-date must NOT skip a drc build
# ---------------------------------------------------------------------------


def _patch_router_available(monkeypatch, available: bool) -> None:
    import kicad_tools.router.cpp_backend as cpp_backend

    monkeypatch.setattr(cpp_backend, "is_cpp_available", lambda: available)


class TestShortCircuitConsultsDrc:
    def test_falls_through_to_rebuild_when_drc_stale_but_router_and_placement_ok(
        self, monkeypatch
    ) -> None:
        """This is the exact bug (#5240): router and placement alone being
        up to date meant the command skipped rebuilding and never noticed
        drc_cpp was missing/stale. The short circuit must fall through."""
        _patch_router_available(monkeypatch, True)
        monkeypatch.setattr(bnc, "_is_so_stale", lambda _router_dir: False)
        monkeypatch.setattr(bnc, "_placement_backend_up_to_date", lambda: True)
        monkeypatch.setattr(bnc, "_drc_backend_up_to_date", lambda: False)

        sentinel = mock.MagicMock(return_value=(False, "cmake stub: reached build path"))
        monkeypatch.setattr(bnc, "_check_cmake", sentinel)

        result = bnc.build_native(force=False)

        assert sentinel.call_count == 1, (
            "build_native() skipped rebuilding even though the DRC "
            "extension was reported stale/missing -- this is the exact "
            "silent-fallback bug #5240 fixes."
        )
        assert result.skipped is False

    def test_skips_when_router_placement_and_drc_all_up_to_date(self, monkeypatch) -> None:
        _patch_router_available(monkeypatch, True)
        monkeypatch.setattr(bnc, "_is_so_stale", lambda _router_dir: False)
        monkeypatch.setattr(bnc, "_placement_backend_up_to_date", lambda: True)
        monkeypatch.setattr(bnc, "_drc_backend_up_to_date", lambda: True)
        fake_router_so = Path("/fake/router/router_cpp.cpython-311.so")
        fake_placement_so = Path("/fake/placement/placement_cpp.cpython-311.so")
        fake_drc_so = Path("/fake/drc/drc_cpp.cpython-311.so")

        def _fake_find(directory, *, abi_only=False, module_name="router_cpp"):
            if module_name == "placement_cpp":
                return fake_placement_so
            if module_name == "drc_cpp":
                return fake_drc_so
            return fake_router_so

        monkeypatch.setattr(bnc, "_find_installed_so", _fake_find)

        result = bnc.build_native(force=False)

        assert result.skipped is True
        assert result.so_path == fake_router_so
        assert result.placement_so_path == fake_placement_so
        assert result.drc_so_path == fake_drc_so


# ---------------------------------------------------------------------------
# Tail of build_native(): installing whatever the build produced
# ---------------------------------------------------------------------------


def _stub_build_pipeline(monkeypatch, tmp_path: Path, *, include_drc_so: bool) -> Path:
    """Stub every heavy step of ``build_native`` so only the tail runs.

    Mirrors ``test_build_native_placement.py``'s ``_stub_build_pipeline``,
    extended with a toggle for whether the fake build directory also
    contains a freshly built ``drc_cpp`` extension.
    """
    package_root = tmp_path / "pkg"
    (package_root / "router").mkdir(parents=True)

    source_dir = tmp_path / "src"
    source_dir.mkdir()
    (source_dir / "CMakeLists.txt").write_text("cmake\n")

    build_dir = tmp_path / "build"
    build_dir.mkdir()
    (build_dir / f"router_cpp{RUNNING_ABI_SUFFIX}").write_bytes(b"FRESHLY-BUILT-ROUTER")
    if include_drc_so:
        (build_dir / f"drc_cpp{RUNNING_ABI_SUFFIX}").write_bytes(b"FRESHLY-BUILT-DRC")

    monkeypatch.setattr(bnc, "_get_package_root", lambda: package_root)
    monkeypatch.setattr(bnc, "_check_cmake", lambda: (True, "/usr/bin/cmake"))
    monkeypatch.setattr(bnc, "_check_compiler", lambda: (True, "/usr/bin/clang++"))
    monkeypatch.setattr(bnc, "_install_nanobind", lambda verbose=False: (True, None))
    monkeypatch.setattr(bnc, "_get_nanobind_cmake_dir", lambda: tmp_path / "nanobind")
    monkeypatch.setattr(bnc, "_get_project_root", lambda: source_dir)
    monkeypatch.setattr(bnc.tempfile, "mkdtemp", lambda **_: str(build_dir))
    monkeypatch.setattr(bnc.shutil, "rmtree", lambda *_a, **_k: None)
    monkeypatch.setattr(
        bnc.subprocess, "run", lambda *_a, **_k: mock.MagicMock(returncode=0, stderr="")
    )
    return package_root


@pytest.fixture
def isolated_replace_flag(monkeypatch):
    from kicad_tools.router import cpp_backend

    monkeypatch.setattr(cpp_backend, "_EXTENSION_REPLACED", False)
    return cpp_backend


def _stub_router_probe(monkeypatch, isolated_replace_flag) -> None:
    monkeypatch.setattr(
        isolated_replace_flag,
        "probe_backend_info",
        lambda **_: {
            "available": True,
            "version": "1.0.0",
            "probe": {"mode": "subprocess", "interpreter": "/py", "failed": False},
        },
    )


class TestBuildNativeInstallsDrcExtension:
    def test_installs_drc_so_alongside_router(
        self, tmp_path: Path, monkeypatch, isolated_replace_flag
    ) -> None:
        package_root = _stub_build_pipeline(monkeypatch, tmp_path, include_drc_so=True)
        _stub_router_probe(monkeypatch, isolated_replace_flag)

        result = bnc.build_native(force=True)

        assert result.success is True
        assert result.warnings == []
        expected_drc_path = package_root / "drc" / f"drc_cpp{RUNNING_ABI_SUFFIX}"
        assert result.drc_so_path == expected_drc_path
        assert expected_drc_path.read_bytes() == b"FRESHLY-BUILT-DRC"
        assert any("drc backend" in step.lower() for step in result.steps_completed)

    def test_router_install_unaffected_when_drc_so_absent(
        self, tmp_path: Path, monkeypatch, isolated_replace_flag
    ) -> None:
        """Router-only source_dir fallback (pip-installed, source-less
        wheel): the build never compiles a drc extension, so there is
        nothing to install. This must not be treated as an error."""
        package_root = _stub_build_pipeline(monkeypatch, tmp_path, include_drc_so=False)
        _stub_router_probe(monkeypatch, isolated_replace_flag)

        result = bnc.build_native(force=True)

        assert result.success is True
        assert result.backend_installed is True
        assert result.warnings == []
        assert result.drc_so_path is None
        router_so = package_root / "router" / f"router_cpp{RUNNING_ABI_SUFFIX}"
        assert router_so.read_bytes() == b"FRESHLY-BUILT-ROUTER"

    def test_drc_install_failure_warns_but_does_not_fail_the_build(
        self, tmp_path: Path, monkeypatch, isolated_replace_flag
    ) -> None:
        _stub_build_pipeline(monkeypatch, tmp_path, include_drc_so=True)
        _stub_router_probe(monkeypatch, isolated_replace_flag)

        real_install = bnc._install_extension_atomically

        def _flaky_install(source: Path, target: Path) -> None:
            if target.parent.name == "drc":
                raise OSError("disk full (simulated)")
            real_install(source, target)

        monkeypatch.setattr(bnc, "_install_extension_atomically", _flaky_install)

        result = bnc.build_native(force=True)

        # The router install (unaffected) still succeeds; the drc failure is
        # surfaced as a warning, not a hard build failure.
        assert result.success is True
        assert result.backend_installed is True
        assert result.drc_so_path is None
        assert len(result.warnings) == 1
        assert "disk full" in result.warnings[0]
        assert "DRC C++ extension" in result.warnings[0]


# ---------------------------------------------------------------------------
# BuildResult / format_result_text surface the drc extension
# ---------------------------------------------------------------------------


class TestBuildResultDrcField:
    def test_to_dict_includes_drc_so_path(self) -> None:
        result = bnc.BuildResult(success=True, drc_so_path=Path("/pkg/drc/x.so"))
        assert result.to_dict()["drc_so_path"] == "/pkg/drc/x.so"

    def test_to_dict_drc_so_path_none_by_default(self) -> None:
        result = bnc.BuildResult(success=True)
        assert result.to_dict()["drc_so_path"] is None


class TestFormatResultTextDrcLine:
    def test_installed_success_mentions_drc_extension(self) -> None:
        result = bnc.BuildResult(
            success=True,
            backend_installed=True,
            so_path=Path("/pkg/router/router_cpp.so"),
            drc_so_path=Path("/pkg/drc/drc_cpp.so"),
        )
        text = bnc.format_result_text(result)
        assert "DRC extension: drc_cpp.so" in text

    def test_skipped_mentions_drc_extension(self) -> None:
        result = bnc.BuildResult(
            success=True,
            backend_installed=True,
            skipped=True,
            so_path=Path("/pkg/router/router_cpp.so"),
            drc_so_path=Path("/pkg/drc/drc_cpp.so"),
        )
        text = bnc.format_result_text(result)
        assert "SKIPPED rebuild" in text
        assert "DRC extension: drc_cpp.so" in text

    def test_no_drc_line_when_absent(self) -> None:
        result = bnc.BuildResult(
            success=True, backend_installed=True, so_path=Path("/pkg/router/router_cpp.so")
        )
        text = bnc.format_result_text(result)
        assert "DRC extension" not in text


# ---------------------------------------------------------------------------
# --check reports drc status without disturbing the router contract
# ---------------------------------------------------------------------------


class TestCheckReportsDrcStatus:
    def test_check_reports_drc_available(self, monkeypatch, capsys) -> None:
        from kicad_tools.drc import cpp_backend as drc_cpp_backend
        from kicad_tools.router import cpp_backend as router_cpp_backend

        monkeypatch.setattr(
            router_cpp_backend,
            "probe_backend_info",
            lambda **_: {
                "available": True,
                "version": "1.0.0",
                "probe": {"mode": "in-process", "interpreter": "/py", "failed": False},
            },
        )
        monkeypatch.setattr(
            drc_cpp_backend,
            "get_backend_info",
            lambda: {"available": True, "version": "1.0.0"},
        )

        assert bnc.main(["--check"]) == 0
        out = capsys.readouterr().out
        # The documented router contract (CLAUDE.md) must be unchanged.
        assert "C++ backend: available (version 1.0.0)" in out
        assert "DRC backend: available (version 1.0.0)" in out

    def test_check_reports_drc_unavailable_without_affecting_exit_code(
        self, monkeypatch, capsys
    ) -> None:
        from kicad_tools.drc import cpp_backend as drc_cpp_backend
        from kicad_tools.router import cpp_backend as router_cpp_backend

        monkeypatch.setattr(
            router_cpp_backend,
            "probe_backend_info",
            lambda **_: {
                "available": True,
                "version": "1.0.0",
                "probe": {"mode": "in-process", "interpreter": "/py", "failed": False},
            },
        )
        monkeypatch.setattr(
            drc_cpp_backend,
            "get_backend_info",
            lambda: {"available": False, "unavailable_reason": "not built"},
        )

        # Exit code stays governed by the ROUTER backend alone.
        assert bnc.main(["--check"]) == 0
        out = capsys.readouterr().out
        assert "C++ backend: available (version 1.0.0)" in out
        assert "DRC backend: not installed" in out
        assert "not built" in out
