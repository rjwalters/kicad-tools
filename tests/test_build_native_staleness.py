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

import importlib.machinery
import os
from pathlib import Path
from unittest import mock

import pytest

import kicad_tools.cli.build_native_cmd as bnc

# The extension suffix this interpreter's import machinery would pick, e.g.
# ``.cpython-312-darwin.so`` (POSIX) or ``.cp312-win_amd64.pyd`` (Windows).
RUNNING_ABI_SUFFIX = importlib.machinery.EXTENSION_SUFFIXES[0]

# Two suffixes this interpreter can never load, used to build multi-ABI
# fixtures.  Chosen to sort BOTH before and after the running suffix so the
# tests cannot pass by accident on glob/sort order.
FOREIGN_SUFFIXES = (".cpython-000-foreign.so", ".cpython-999-foreign.so")


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


# ---------------------------------------------------------------------------
# Issue #4589
# ---------------------------------------------------------------------------


def _touch_extension(router_dir: Path, suffix: str, mtime: float) -> Path:
    """Create ``router_cpp<suffix>`` with a fixed mtime."""
    router_dir.mkdir(parents=True, exist_ok=True)
    path = router_dir / f"router_cpp{suffix}"
    path.write_bytes(b"\x00")
    os.utime(path, (mtime, mtime))
    return path


class TestFindInstalledSoAbiAwareness:
    """``_find_installed_so`` must resolve the RUNNING interpreter's ABI.

    Before Issue #4589 it returned the first ``Path.glob`` hit -- filesystem
    order.  Measured on a checkout carrying 312/313/314 builds it returned
    ``router_cpp.cpython-313-darwin.so`` while the interpreter imported
    ``router_cpp.cpython-312-darwin.so``.
    """

    def test_prefers_running_interpreter_suffix(self, tmp_path: Path) -> None:
        router_dir = tmp_path / "router"
        for suffix in FOREIGN_SUFFIXES:
            _touch_extension(router_dir, suffix, 100.0)
        expected = _touch_extension(router_dir, RUNNING_ABI_SUFFIX, 100.0)

        assert bnc._find_installed_so(router_dir) == expected

    def test_prefers_running_suffix_regardless_of_creation_order(self, tmp_path: Path) -> None:
        """Create the ABI match FIRST so a first-glob-hit impl would pass."""
        router_dir = tmp_path / "router"
        expected = _touch_extension(router_dir, RUNNING_ABI_SUFFIX, 100.0)
        for suffix in FOREIGN_SUFFIXES:
            _touch_extension(router_dir, suffix, 100.0)

        assert bnc._find_installed_so(router_dir) == expected

    def test_deterministic_when_no_abi_match(self, tmp_path: Path) -> None:
        router_dir = tmp_path / "router"
        for suffix in FOREIGN_SUFFIXES:
            _touch_extension(router_dir, suffix, 100.0)

        found = bnc._find_installed_so(router_dir)
        # Alphabetically first by name -- deterministic, not glob order.
        assert found is not None
        assert found.name == sorted(f"router_cpp{s}" for s in FOREIGN_SUFFIXES)[0]
        # And repeated calls agree.
        assert bnc._find_installed_so(router_dir) == found

    def test_abi_only_treats_foreign_abi_as_not_installed(self, tmp_path: Path) -> None:
        router_dir = tmp_path / "router"
        for suffix in FOREIGN_SUFFIXES:
            _touch_extension(router_dir, suffix, 100.0)

        assert bnc._find_installed_so(router_dir, abi_only=True) is None

    def test_abi_only_returns_the_match_when_present(self, tmp_path: Path) -> None:
        router_dir = tmp_path / "router"
        _touch_extension(router_dir, FOREIGN_SUFFIXES[0], 100.0)
        expected = _touch_extension(router_dir, RUNNING_ABI_SUFFIX, 100.0)

        assert bnc._find_installed_so(router_dir, abi_only=True) == expected

    def test_returns_none_for_empty_dir(self, tmp_path: Path) -> None:
        assert bnc._find_installed_so(tmp_path) is None
        assert bnc._find_installed_so(tmp_path, abi_only=True) is None

    def test_matches_bare_suffix_extension(self, tmp_path: Path) -> None:
        """``router_cpp.so`` / ``router_cpp.pyd`` are legal names too.

        The old ``router_cpp.*.so`` glob could not match them at all.
        """
        router_dir = tmp_path / "router"
        bare = importlib.machinery.EXTENSION_SUFFIXES[-1]  # ".so" / ".pyd"
        expected = _touch_extension(router_dir, bare, 100.0)

        assert bnc._find_installed_so(router_dir) == expected


class TestIsSoStaleUsesAbiMatch:
    """``_is_so_stale`` must compare against the .so this interpreter loads."""

    def test_stale_when_abi_match_is_old_but_foreign_abi_is_new(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        router_dir = tmp_path / "router"
        cpp_dir = router_dir / "cpp"
        _touch_extension(router_dir, RUNNING_ABI_SUFFIX, 100.0)  # ours: OLD
        for suffix in FOREIGN_SUFFIXES:
            _touch_extension(router_dir, suffix, 900.0)  # foreign: NEW
        _make_cpp_source(cpp_dir, "pathfinder.cpp", 500.0)
        monkeypatch.setattr(bnc, "_get_cpp_source_dir", lambda: cpp_dir)

        # A foreign-ABI-blind implementation sees a 900.0 .so and reports
        # "up to date", skipping a rebuild this interpreter genuinely needs.
        assert bnc._is_so_stale(router_dir) is True

    def test_not_stale_when_abi_match_is_new_but_foreign_abi_is_old(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        router_dir = tmp_path / "router"
        cpp_dir = router_dir / "cpp"
        _touch_extension(router_dir, RUNNING_ABI_SUFFIX, 900.0)  # ours: NEW
        for suffix in FOREIGN_SUFFIXES:
            _touch_extension(router_dir, suffix, 100.0)  # foreign: OLD
        _make_cpp_source(cpp_dir, "pathfinder.cpp", 500.0)
        monkeypatch.setattr(bnc, "_get_cpp_source_dir", lambda: cpp_dir)

        # A foreign-ABI-blind implementation sees a 100.0 .so and forces a
        # needless multi-minute rebuild.
        assert bnc._is_so_stale(router_dir) is False


class TestAtomicInstall:
    """The .so must be renamed into place, never rewritten in place."""

    def test_replaces_inode_instead_of_truncating(self, tmp_path: Path) -> None:
        target = tmp_path / "router" / f"router_cpp{RUNNING_ABI_SUFFIX}"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"OLD-CONTENT-THAT-IS-DLOPENED")
        old_inode = target.stat().st_ino

        source = tmp_path / "build" / f"router_cpp{RUNNING_ABI_SUFFIX}"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"NEW")

        bnc._install_extension_atomically(source, target)

        assert target.read_bytes() == b"NEW"
        # A new inode proves os.replace() was used: an in-place copy2 would
        # have truncated the file a running process still has mapped.
        assert target.stat().st_ino != old_inode

    def test_creates_target_when_absent(self, tmp_path: Path) -> None:
        target = tmp_path / "router" / f"router_cpp{RUNNING_ABI_SUFFIX}"
        source = tmp_path / "build" / f"router_cpp{RUNNING_ABI_SUFFIX}"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"NEW")

        bnc._install_extension_atomically(source, target)

        assert target.read_bytes() == b"NEW"

    def test_leaves_no_temp_file_on_failure(self, tmp_path: Path) -> None:
        target = tmp_path / "router" / f"router_cpp{RUNNING_ABI_SUFFIX}"
        target.parent.mkdir(parents=True)
        missing_source = tmp_path / "build" / "does-not-exist.so"

        with pytest.raises(OSError):
            bnc._install_extension_atomically(missing_source, target)

        assert list(target.parent.iterdir()) == []


def _stub_build_pipeline(monkeypatch, tmp_path: Path) -> Path:
    """Stub every heavy step of ``build_native`` so only the tail runs.

    Returns the fake package root; the installed extension lands in
    ``<root>/router/``.
    """
    package_root = tmp_path / "pkg"
    (package_root / "router").mkdir(parents=True)

    source_dir = tmp_path / "src"
    source_dir.mkdir()
    (source_dir / "CMakeLists.txt").write_text("cmake\n")

    build_dir = tmp_path / "build"
    build_dir.mkdir()
    (build_dir / f"router_cpp{RUNNING_ABI_SUFFIX}").write_bytes(b"FRESHLY-BUILT")

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
    """Keep ``note_extension_replaced()`` from leaking into other tests."""
    from kicad_tools.router import cpp_backend

    monkeypatch.setattr(cpp_backend, "_EXTENSION_REPLACED", False)
    return cpp_backend


class TestPostBuildVerificationUsesFreshInterpreter:
    """Issue #4589: verification must ask a fresh interpreter, not reload.

    An already-``dlopen``'d C extension cannot be re-imported from a replaced
    file in the same process, so the old in-process reload probe reported on
    the PRE-build extension: a false negative on a version-stale rebuild
    ("Extension installed but not loading correctly", contradicted by
    ``--check``) and a false positive on an mtime-stale rebuild (success
    reported against code that was never loaded).
    """

    def test_reports_success_when_fresh_interpreter_sees_backend(
        self, tmp_path: Path, monkeypatch, isolated_replace_flag
    ) -> None:
        cpp_backend = isolated_replace_flag
        _stub_build_pipeline(monkeypatch, tmp_path)

        calls: list[dict] = []

        def _probe(**kwargs):
            calls.append(kwargs)
            return {
                "available": True,
                "version": "1.0.0",
                "probe": {"mode": "subprocess", "interpreter": "/py", "failed": False},
            }

        monkeypatch.setattr(cpp_backend, "probe_backend_info", _probe)

        result = bnc.build_native(force=True)

        assert result.success is True
        assert result.backend_installed is True
        assert result.warnings == []
        # The probe must be forced into a subprocess: the in-process view is
        # stale by construction once we have overwritten the .so.
        assert calls == [{"allow_in_process": False}]
        assert "installed successfully" in bnc.format_result_text(result)

    def test_marks_extension_replaced_so_later_probes_do_not_trust_memory(
        self, tmp_path: Path, monkeypatch, isolated_replace_flag
    ) -> None:
        cpp_backend = isolated_replace_flag
        _stub_build_pipeline(monkeypatch, tmp_path)
        monkeypatch.setattr(
            cpp_backend,
            "probe_backend_info",
            lambda **_: {"available": True, "probe": {"interpreter": "/py"}},
        )

        assert cpp_backend.extension_replaced_in_process() is False
        bnc.build_native(force=True)
        assert cpp_backend.extension_replaced_in_process() is True

    def test_failure_warning_names_reason_path_and_interpreter(
        self, tmp_path: Path, monkeypatch, isolated_replace_flag
    ) -> None:
        cpp_backend = isolated_replace_flag
        package_root = _stub_build_pipeline(monkeypatch, tmp_path)
        monkeypatch.setattr(
            cpp_backend,
            "probe_backend_info",
            lambda **_: {
                "available": False,
                "unavailable_reason": "router_cpp build version 20 does not match required 19",
                "probe": {
                    "mode": "subprocess",
                    "interpreter": "/opt/py/bin/python",
                    "failed": False,
                },
            },
        )

        result = bnc.build_native(force=True)

        assert result.success is True  # the build itself succeeded
        assert result.backend_installed is False
        assert len(result.warnings) == 1
        warning = result.warnings[0]
        assert "build version 20 does not match required 19" in warning
        assert str(package_root / "router" / f"router_cpp{RUNNING_ABI_SUFFIX}") in warning
        assert "/opt/py/bin/python" in warning
        # Generic, uninformative wording must be gone.
        assert "not loading correctly" not in warning

        text = bnc.format_result_text(result)
        assert "Reason:" in text
        assert "Extension:" in text
        assert "Interpreter:" in text

    def test_probe_that_cannot_run_does_not_claim_extension_is_broken(
        self, tmp_path: Path, monkeypatch, isolated_replace_flag
    ) -> None:
        cpp_backend = isolated_replace_flag
        _stub_build_pipeline(monkeypatch, tmp_path)
        monkeypatch.setattr(
            cpp_backend,
            "probe_backend_info",
            lambda **_: {
                "available": False,
                "unavailable_reason": "backend probe did not run: TimeoutExpired: 60s",
                "probe": {
                    "mode": "subprocess",
                    "interpreter": "/opt/py/bin/python",
                    "failed": True,
                    "error": "TimeoutExpired: 60s",
                },
            },
        )

        result = bnc.build_native(force=True)

        assert result.success is True
        warning = result.warnings[0]
        assert "probe did not run" in warning
        assert "TimeoutExpired" in warning
        # Must NOT assert the extension is broken -- we do not know.
        assert "cannot load it" not in warning

    def test_verification_payload_is_exposed_in_json(
        self, tmp_path: Path, monkeypatch, isolated_replace_flag
    ) -> None:
        cpp_backend = isolated_replace_flag
        _stub_build_pipeline(monkeypatch, tmp_path)
        payload = {
            "available": True,
            "build_version": 18,
            "probe": {"mode": "subprocess", "interpreter": "/py", "failed": False},
        }
        monkeypatch.setattr(cpp_backend, "probe_backend_info", lambda **_: payload)

        result = bnc.build_native(force=True)

        assert result.to_dict()["verification"] == payload


class TestCheckModeSharesTheProbeAndNeverBuilds:
    """``--check`` must use the same probe and compile nothing."""

    def test_check_uses_shared_probe(self, monkeypatch, capsys) -> None:
        from kicad_tools.router import cpp_backend

        monkeypatch.setattr(
            cpp_backend,
            "probe_backend_info",
            lambda **_: {
                "available": True,
                "version": "1.0.0",
                "build_version": 18,
                "required_build_version": 18,
                "extension_path": f"/pkg/router/router_cpp{RUNNING_ABI_SUFFIX}",
                "probe": {"mode": "in-process", "interpreter": "/py", "failed": False},
            },
        )

        assert bnc.main(["--check"]) == 0
        out = capsys.readouterr().out
        assert "C++ backend: available (version 1.0.0)" in out
        # Issue #4589: version() is not the number the staleness guard checks.
        assert "build version: 18 (required: 18)" in out
        assert f"router_cpp{RUNNING_ABI_SUFFIX}" in out

    def test_check_reports_the_probe_reason_when_unavailable(self, monkeypatch, capsys) -> None:
        from kicad_tools.router import cpp_backend

        monkeypatch.setattr(
            cpp_backend,
            "probe_backend_info",
            lambda **_: {
                "available": False,
                "unavailable_reason": "router_cpp build version 20 does not match required 19",
                "probe": {"mode": "in-process", "interpreter": "/py", "failed": False},
            },
        )

        assert bnc.main(["--check"]) == 1
        out = capsys.readouterr().out
        assert "C++ backend: not installed" in out
        assert "build version 20 does not match required 19" in out

    def test_check_never_calls_build_native(self, monkeypatch) -> None:
        """Regression guard: do not "fix" --check by making it compile."""
        from kicad_tools.router import cpp_backend

        monkeypatch.setattr(
            cpp_backend,
            "probe_backend_info",
            lambda **_: {"available": True, "version": "1.0.0"},
        )
        boom = mock.MagicMock(side_effect=AssertionError("--check must not build"))
        monkeypatch.setattr(bnc, "build_native", boom)
        # Any compiler/cmake invocation would also be a build.
        monkeypatch.setattr(
            bnc.subprocess,
            "run",
            mock.MagicMock(side_effect=AssertionError("--check must not run subprocesses")),
        )

        assert bnc.main(["--check"]) == 0
        assert boom.call_count == 0

    def test_check_and_post_build_verification_call_the_same_helper(self) -> None:
        """Static guard: both paths resolve through ``probe_backend_info``."""
        source = Path(bnc.__file__).read_text()
        # Post-build verification (forced fresh interpreter) ...
        assert "cpp_module.probe_backend_info(allow_in_process=False)" in source
        # ... and --check, through the same helper.
        assert "info = probe_backend_info()" in source
        # The in-process reload probe must be gone from the build path: it is
        # what let the two commands disagree (Issue #4589).
        code = "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("#"))
        assert "importlib.reload(" not in code
        assert 'sys.modules.pop("kicad_tools.router.router_cpp"' not in code
