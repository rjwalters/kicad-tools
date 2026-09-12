"""Tests for ``kct build-native`` installing the placement C++ extension too.

Issue #5240: the root ``CMakeLists.txt`` build always compiles
``placement_cpp`` alongside ``router_cpp`` -- they are sibling subdirectories
of the same CMake project -- but ``build_native()`` only ever searched the
build directory for ``router_cpp.*.so`` and copied that one into
``src/kicad_tools/router/``. The freshly built ``placement_cpp`` extension
was silently discarded with the temp build directory on every invocation, so
``PlacementOptimizer`` (the force-directed placement engine used by
``kct optimize-placement`` and every board's CI placement step) always fell
back to its pure-Python ``_compute_component_repulsion_cpu`` path -- 10-100x
slower than the C++ path the exact same build already compiled -- with no
error or warning anywhere.

Measured impact in this checkout: ``tests/test_optim.py::TestCourtyardAware
Clamping::test_courtyard_stays_within_board_after_optimization`` (500
iterations, 20 components) went from ~64s (Python fallback, matching the
issue's ~80s hosted-CI diagnostic profile) to ~0.55s once the placement
extension was actually installed -- a ~116x speedup on this single test, with
no test-code or fixture changes.

These tests exercise the pure decision helpers and the tail of
``build_native`` with mocks -- no cmake / compiler is invoked, mirroring the
conventions in ``test_build_native_staleness.py``.
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


class TestPlacementDirHelpers:
    def test_get_placement_dir_is_sibling_of_router(self) -> None:
        placement_dir = bnc._get_placement_dir()
        assert placement_dir.name == "placement"
        assert placement_dir.parent == bnc._get_package_root()

    def test_get_placement_cpp_source_dir_returns_none_when_absent(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setattr(bnc, "_get_package_root", lambda: tmp_path)
        assert bnc._get_placement_cpp_source_dir() is None

    def test_get_placement_cpp_source_dir_returns_path_when_present(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setattr(bnc, "_get_package_root", lambda: tmp_path)
        cpp_dir = tmp_path / "placement" / "cpp"
        cpp_dir.mkdir(parents=True)
        assert bnc._get_placement_cpp_source_dir() == cpp_dir


# ---------------------------------------------------------------------------
# Generalized extension-candidate discovery (module_name parameter)
# ---------------------------------------------------------------------------


def _touch(directory: Path, name: str, mtime: float) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_bytes(b"\x00")
    os.utime(path, (mtime, mtime))
    return path


class TestFindInstalledSoModuleName:
    def test_defaults_to_router_cpp(self, tmp_path: Path) -> None:
        expected = _touch(tmp_path, f"router_cpp{RUNNING_ABI_SUFFIX}", 100.0)
        assert bnc._find_installed_so(tmp_path) == expected

    def test_finds_placement_cpp_when_named(self, tmp_path: Path) -> None:
        # A router_cpp file in the same directory must NOT be picked up when
        # asking for placement_cpp -- the two extensions coexist.
        _touch(tmp_path, f"router_cpp{RUNNING_ABI_SUFFIX}", 100.0)
        expected = _touch(tmp_path, f"placement_cpp{RUNNING_ABI_SUFFIX}", 100.0)

        found = bnc._find_installed_so(tmp_path, module_name="placement_cpp")

        assert found == expected

    def test_placement_cpp_not_found_when_only_router_present(self, tmp_path: Path) -> None:
        _touch(tmp_path, f"router_cpp{RUNNING_ABI_SUFFIX}", 100.0)
        assert bnc._find_installed_so(tmp_path, module_name="placement_cpp") is None

    def test_abi_only_applies_to_placement_too(self, tmp_path: Path) -> None:
        _touch(tmp_path, "placement_cpp.cpython-000-foreign.so", 100.0)
        assert bnc._find_installed_so(tmp_path, module_name="placement_cpp", abi_only=True) is None


class TestIsSoStaleModuleName:
    def test_placement_stale_when_source_newer(self, tmp_path: Path) -> None:
        placement_dir = tmp_path / "placement"
        cpp_dir = placement_dir / "cpp"
        _touch(placement_dir, f"placement_cpp{RUNNING_ABI_SUFFIX}", 100.0)
        src_dir = cpp_dir / "src"
        src_dir.mkdir(parents=True)
        source = src_dir / "force_engine.cpp"
        source.write_text("// fake\n")
        os.utime(source, (200.0, 200.0))

        assert (
            bnc._is_so_stale(placement_dir, module_name="placement_cpp", cpp_source_dir=cpp_dir)
            is True
        )

    def test_placement_not_stale_when_so_newer(self, tmp_path: Path) -> None:
        placement_dir = tmp_path / "placement"
        cpp_dir = placement_dir / "cpp"
        src_dir = cpp_dir / "src"
        src_dir.mkdir(parents=True)
        source = src_dir / "force_engine.cpp"
        source.write_text("// fake\n")
        os.utime(source, (100.0, 100.0))
        _touch(placement_dir, f"placement_cpp{RUNNING_ABI_SUFFIX}", 200.0)

        assert (
            bnc._is_so_stale(placement_dir, module_name="placement_cpp", cpp_source_dir=cpp_dir)
            is False
        )

    def test_router_default_behavior_unchanged_without_kwargs(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Calling with the original positional-only signature must still
        consult ``_get_cpp_source_dir`` (router), not require the new
        keyword args -- back-compat for existing callers/tests."""
        router_dir = tmp_path / "router"
        cpp_dir = router_dir / "cpp"
        _touch(router_dir, f"router_cpp{RUNNING_ABI_SUFFIX}", 100.0)
        src_dir = cpp_dir / "src"
        src_dir.mkdir(parents=True)
        source = src_dir / "pathfinder.cpp"
        source.write_text("// fake\n")
        os.utime(source, (200.0, 200.0))
        monkeypatch.setattr(bnc, "_get_cpp_source_dir", lambda: cpp_dir)

        assert bnc._is_so_stale(router_dir) is True


# ---------------------------------------------------------------------------
# _placement_backend_up_to_date
# ---------------------------------------------------------------------------


class TestPlacementBackendUpToDate:
    def test_true_when_no_placement_source_tree(self, monkeypatch) -> None:
        """A source-less installed wheel has nothing to build -- treated as
        up to date, not stale, so it never blocks the router short-circuit."""
        monkeypatch.setattr(bnc, "_get_placement_cpp_source_dir", lambda: None)
        assert bnc._placement_backend_up_to_date() is True

    def test_false_when_source_present_but_not_importable(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setattr(bnc, "_get_placement_cpp_source_dir", lambda: tmp_path)
        monkeypatch.setitem(
            __import__("sys").modules,
            "kicad_tools.placement.cpp_backend",
            None,
        )
        assert bnc._placement_backend_up_to_date() is False

    def test_false_when_available_but_stale(self, tmp_path: Path, monkeypatch) -> None:
        import kicad_tools.placement.cpp_backend as placement_cpp_backend

        monkeypatch.setattr(bnc, "_get_placement_cpp_source_dir", lambda: tmp_path)
        monkeypatch.setattr(placement_cpp_backend, "is_cpp_available", lambda: True)
        monkeypatch.setattr(bnc, "_is_so_stale", lambda *_a, **_k: True)

        assert bnc._placement_backend_up_to_date() is False

    def test_true_when_available_and_not_stale(self, tmp_path: Path, monkeypatch) -> None:
        import kicad_tools.placement.cpp_backend as placement_cpp_backend

        monkeypatch.setattr(bnc, "_get_placement_cpp_source_dir", lambda: tmp_path)
        monkeypatch.setattr(placement_cpp_backend, "is_cpp_available", lambda: True)
        monkeypatch.setattr(bnc, "_is_so_stale", lambda *_a, **_k: False)

        assert bnc._placement_backend_up_to_date() is True

    def test_false_when_not_available(self, tmp_path: Path, monkeypatch) -> None:
        import kicad_tools.placement.cpp_backend as placement_cpp_backend

        monkeypatch.setattr(bnc, "_get_placement_cpp_source_dir", lambda: tmp_path)
        monkeypatch.setattr(placement_cpp_backend, "is_cpp_available", lambda: False)

        assert bnc._placement_backend_up_to_date() is False


# ---------------------------------------------------------------------------
# Short-circuit integration: router-up-to-date must NOT skip a placement build
# ---------------------------------------------------------------------------


def _patch_router_available(monkeypatch, available: bool) -> None:
    import kicad_tools.router.cpp_backend as cpp_backend

    monkeypatch.setattr(cpp_backend, "is_cpp_available", lambda: available)


class TestShortCircuitConsultsPlacement:
    def test_falls_through_to_rebuild_when_placement_stale_but_router_ok(self, monkeypatch) -> None:
        """This is the exact bug (#5240): router alone was up to date, so
        the command skipped rebuilding and never noticed placement_cpp was
        missing/stale. The short circuit must fall through in this case."""
        _patch_router_available(monkeypatch, True)
        monkeypatch.setattr(bnc, "_is_so_stale", lambda _router_dir: False)
        monkeypatch.setattr(bnc, "_placement_backend_up_to_date", lambda: False)

        sentinel = mock.MagicMock(return_value=(False, "cmake stub: reached build path"))
        monkeypatch.setattr(bnc, "_check_cmake", sentinel)

        result = bnc.build_native(force=False)

        assert sentinel.call_count == 1, (
            "build_native() skipped rebuilding even though the placement "
            "extension was reported stale/missing -- this is the exact "
            "silent-fallback bug #5240 fixes."
        )
        assert result.skipped is False

    def test_skips_when_both_router_and_placement_up_to_date(self, monkeypatch) -> None:
        _patch_router_available(monkeypatch, True)
        monkeypatch.setattr(bnc, "_is_so_stale", lambda _router_dir: False)
        monkeypatch.setattr(bnc, "_placement_backend_up_to_date", lambda: True)
        monkeypatch.setattr(bnc, "_drc_backend_up_to_date", lambda: True)
        fake_router_so = Path("/fake/router/router_cpp.cpython-311.so")
        fake_placement_so = Path("/fake/placement/placement_cpp.cpython-311.so")

        def _fake_find(directory, *, abi_only=False, module_name="router_cpp"):
            return fake_placement_so if module_name == "placement_cpp" else fake_router_so

        monkeypatch.setattr(bnc, "_find_installed_so", _fake_find)

        result = bnc.build_native(force=False)

        assert result.skipped is True
        assert result.so_path == fake_router_so
        assert result.placement_so_path == fake_placement_so


# ---------------------------------------------------------------------------
# Tail of build_native(): installing whatever the build produced
# ---------------------------------------------------------------------------


def _stub_build_pipeline(monkeypatch, tmp_path: Path, *, include_placement_so: bool) -> Path:
    """Stub every heavy step of ``build_native`` so only the tail runs.

    Mirrors ``test_build_native_staleness.py``'s ``_stub_build_pipeline``,
    extended with a toggle for whether the fake build directory also
    contains a freshly built ``placement_cpp`` extension (as a real root-
    CMakeLists.txt build always does) or not (the router-only source_dir
    fallback for a source-less installed wheel).
    """
    package_root = tmp_path / "pkg"
    (package_root / "router").mkdir(parents=True)

    source_dir = tmp_path / "src"
    source_dir.mkdir()
    (source_dir / "CMakeLists.txt").write_text("cmake\n")

    build_dir = tmp_path / "build"
    build_dir.mkdir()
    (build_dir / f"router_cpp{RUNNING_ABI_SUFFIX}").write_bytes(b"FRESHLY-BUILT-ROUTER")
    if include_placement_so:
        (build_dir / f"placement_cpp{RUNNING_ABI_SUFFIX}").write_bytes(b"FRESHLY-BUILT-PLACEMENT")

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


class TestBuildNativeInstallsPlacementExtension:
    def test_installs_placement_so_alongside_router(
        self, tmp_path: Path, monkeypatch, isolated_replace_flag
    ) -> None:
        package_root = _stub_build_pipeline(monkeypatch, tmp_path, include_placement_so=True)
        _stub_router_probe(monkeypatch, isolated_replace_flag)

        result = bnc.build_native(force=True)

        assert result.success is True
        assert result.warnings == []
        expected_placement_path = package_root / "placement" / f"placement_cpp{RUNNING_ABI_SUFFIX}"
        assert result.placement_so_path == expected_placement_path
        assert expected_placement_path.read_bytes() == b"FRESHLY-BUILT-PLACEMENT"
        assert any("placement backend" in step.lower() for step in result.steps_completed)

    def test_router_install_unaffected_when_placement_so_absent(
        self, tmp_path: Path, monkeypatch, isolated_replace_flag
    ) -> None:
        """Router-only source_dir fallback (pip-installed, source-less
        wheel): the build never compiles a placement extension, so there is
        nothing to install. This must not be treated as an error."""
        package_root = _stub_build_pipeline(monkeypatch, tmp_path, include_placement_so=False)
        _stub_router_probe(monkeypatch, isolated_replace_flag)

        result = bnc.build_native(force=True)

        assert result.success is True
        assert result.backend_installed is True
        assert result.warnings == []
        assert result.placement_so_path is None
        router_so = package_root / "router" / f"router_cpp{RUNNING_ABI_SUFFIX}"
        assert router_so.read_bytes() == b"FRESHLY-BUILT-ROUTER"

    def test_placement_install_failure_warns_but_does_not_fail_the_build(
        self, tmp_path: Path, monkeypatch, isolated_replace_flag
    ) -> None:
        _stub_build_pipeline(monkeypatch, tmp_path, include_placement_so=True)
        _stub_router_probe(monkeypatch, isolated_replace_flag)

        real_install = bnc._install_extension_atomically

        def _flaky_install(source: Path, target: Path) -> None:
            if target.parent.name == "placement":
                raise OSError("disk full (simulated)")
            real_install(source, target)

        monkeypatch.setattr(bnc, "_install_extension_atomically", _flaky_install)

        result = bnc.build_native(force=True)

        # The router install (unaffected) still succeeds; the placement
        # failure is surfaced as a warning, not a hard build failure.
        assert result.success is True
        assert result.backend_installed is True
        assert result.placement_so_path is None
        assert len(result.warnings) == 1
        assert "disk full" in result.warnings[0]
        assert "Placement C++ extension" in result.warnings[0]


# ---------------------------------------------------------------------------
# BuildResult / format_result_text surface the placement extension
# ---------------------------------------------------------------------------


class TestBuildResultPlacementField:
    def test_to_dict_includes_placement_so_path(self) -> None:
        result = bnc.BuildResult(success=True, placement_so_path=Path("/pkg/placement/x.so"))
        assert result.to_dict()["placement_so_path"] == "/pkg/placement/x.so"

    def test_to_dict_placement_so_path_none_by_default(self) -> None:
        result = bnc.BuildResult(success=True)
        assert result.to_dict()["placement_so_path"] is None


class TestFormatResultTextPlacementLine:
    def test_installed_success_mentions_placement_extension(self) -> None:
        result = bnc.BuildResult(
            success=True,
            backend_installed=True,
            so_path=Path("/pkg/router/router_cpp.so"),
            placement_so_path=Path("/pkg/placement/placement_cpp.so"),
        )
        text = bnc.format_result_text(result)
        assert "Placement extension: placement_cpp.so" in text

    def test_skipped_mentions_placement_extension(self) -> None:
        result = bnc.BuildResult(
            success=True,
            backend_installed=True,
            skipped=True,
            so_path=Path("/pkg/router/router_cpp.so"),
            placement_so_path=Path("/pkg/placement/placement_cpp.so"),
        )
        text = bnc.format_result_text(result)
        assert "SKIPPED rebuild" in text
        assert "Placement extension: placement_cpp.so" in text

    def test_no_placement_line_when_absent(self) -> None:
        result = bnc.BuildResult(
            success=True, backend_installed=True, so_path=Path("/pkg/router/router_cpp.so")
        )
        text = bnc.format_result_text(result)
        assert "Placement extension" not in text


# ---------------------------------------------------------------------------
# --check reports placement status without disturbing the router contract
# ---------------------------------------------------------------------------


class TestCheckReportsPlacementStatus:
    def test_check_reports_placement_available(self, monkeypatch, capsys) -> None:
        from kicad_tools.placement import cpp_backend as placement_cpp_backend
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
            placement_cpp_backend,
            "get_backend_info",
            lambda: {"available": True, "version": "2.1.0"},
        )

        assert bnc.main(["--check"]) == 0
        out = capsys.readouterr().out
        # The documented router contract (CLAUDE.md) must be unchanged.
        assert "C++ backend: available (version 1.0.0)" in out
        assert "Placement backend: available (version 2.1.0)" in out

    def test_check_reports_placement_unavailable_without_affecting_exit_code(
        self, monkeypatch, capsys
    ) -> None:
        from kicad_tools.placement import cpp_backend as placement_cpp_backend
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
            placement_cpp_backend,
            "get_backend_info",
            lambda: {"available": False, "unavailable_reason": "not built"},
        )

        # Exit code stays governed by the ROUTER backend alone.
        assert bnc.main(["--check"]) == 0
        out = capsys.readouterr().out
        assert "C++ backend: available (version 1.0.0)" in out
        assert "Placement backend: not installed" in out
        assert "not built" in out
