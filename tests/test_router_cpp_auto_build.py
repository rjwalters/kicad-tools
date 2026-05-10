"""Tests for ensure_cpp_backend_available (Issue #2549).

These tests cover the silent auto-build helper that consolidates the four
backend-selection blocks in route_cmd.py.  They focus on the decision tree
(opt-out flags, env vars, toolchain detection) and graceful failure
handling -- not on actually invoking cmake.
"""

from __future__ import annotations

import subprocess
from unittest import mock

import pytest

from kicad_tools.router import cpp_backend


class _FakeBuildResult:
    """Minimal stand-in for ``BuildResult`` used by the helper."""

    def __init__(
        self,
        *,
        success: bool = True,
        error_message: str | None = None,
    ) -> None:
        self.success = success
        self.error_message = error_message
        self.steps_completed: list[str] = []
        self.warnings: list[str] = []
        self.so_path = None
        self.backend_installed = success


@pytest.fixture
def force_cpp_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pretend the C++ backend is not loaded yet so auto-build paths run."""
    monkeypatch.setattr(cpp_backend, "_CPP_AVAILABLE", False)
    monkeypatch.setattr(cpp_backend, "_CPP_IMPORT_ERROR", "stub: not built")


@pytest.fixture
def toolchain_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pretend cmake + clang++ are on PATH."""
    monkeypatch.setattr(cpp_backend, "_toolchain_available", lambda: True)


@pytest.fixture
def no_env_optout(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure KICAD_TOOLS_NO_AUTO_BUILD is not set."""
    monkeypatch.delenv("KICAD_TOOLS_NO_AUTO_BUILD", raising=False)


def _patch_build_native(monkeypatch: pytest.MonkeyPatch, fn) -> mock.MagicMock:
    """Install a mock ``build_native`` and return the mock for assertions."""
    m = mock.MagicMock(side_effect=fn)
    # The helper imports ``build_native`` from build_native_cmd lazily
    # inside ``_attempt_auto_build``.  We patch the original symbol so the
    # late import resolves to our mock.
    import kicad_tools.cli.build_native_cmd as bnc

    monkeypatch.setattr(bnc, "build_native", m)
    return m


def _patch_reload_to_succeed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``_reload_cpp_backend`` flip the global to True without doing IO."""

    def _fake_reload() -> bool:
        cpp_backend._CPP_AVAILABLE = True
        return True

    monkeypatch.setattr(cpp_backend, "_reload_cpp_backend", _fake_reload)


class TestBackendPython:
    """``--backend python`` must never trigger auto-build (AG4)."""

    def test_python_skips_auto_build(
        self, force_cpp_unavailable, toolchain_present, no_env_optout, monkeypatch
    ):
        m = _patch_build_native(monkeypatch, lambda **_: _FakeBuildResult(success=True))

        ok, force_python, exit_code = cpp_backend.ensure_cpp_backend_available(
            backend="python", quiet=True
        )

        assert ok is True
        assert force_python is True
        assert exit_code is None
        assert m.call_count == 0


class TestBackendCpp:
    """``--backend cpp`` preserves the existing hard-error behavior (AG5)."""

    def test_cpp_unavailable_with_disallowed_build_returns_exit_1(
        self, force_cpp_unavailable, toolchain_present, no_env_optout, monkeypatch
    ):
        m = _patch_build_native(monkeypatch, lambda **_: _FakeBuildResult(success=True))

        ok, force_python, exit_code = cpp_backend.ensure_cpp_backend_available(
            backend="cpp", quiet=True, allow_auto_build=False
        )

        assert ok is False
        assert force_python is False
        assert exit_code == 1
        assert m.call_count == 0

    def test_cpp_with_failed_build_returns_exit_1(
        self, force_cpp_unavailable, toolchain_present, no_env_optout, monkeypatch
    ):
        m = _patch_build_native(
            monkeypatch,
            lambda **_: _FakeBuildResult(success=False, error_message="cmake missing"),
        )

        ok, force_python, exit_code = cpp_backend.ensure_cpp_backend_available(
            backend="cpp", quiet=True
        )

        assert ok is False
        assert exit_code == 1
        assert m.call_count == 1


class TestBackendAuto:
    """``--backend auto`` (default) is the auto-build path (AG1, AG10)."""

    def test_auto_build_runs_when_so_missing_and_toolchain_present(
        self, force_cpp_unavailable, toolchain_present, no_env_optout, monkeypatch
    ):
        _patch_reload_to_succeed(monkeypatch)
        m = _patch_build_native(monkeypatch, lambda **_: _FakeBuildResult(success=True))

        ok, force_python, exit_code = cpp_backend.ensure_cpp_backend_available(
            backend="auto", quiet=True
        )

        assert ok is True
        assert force_python is False
        assert exit_code is None
        assert m.call_count == 1
        # build_native must be called with force=False (idempotent path)
        kwargs = m.call_args.kwargs
        assert kwargs.get("force") is False

    def test_auto_skips_when_already_available(self, monkeypatch, no_env_optout):
        # Pretend the backend is already loaded
        monkeypatch.setattr(cpp_backend, "_CPP_AVAILABLE", True)
        m = _patch_build_native(monkeypatch, lambda **_: _FakeBuildResult(success=True))

        ok, force_python, exit_code = cpp_backend.ensure_cpp_backend_available(
            backend="auto", quiet=True
        )

        assert ok is True
        assert force_python is False
        assert exit_code is None
        # No build attempt should occur when backend is already available
        assert m.call_count == 0


class TestNoAutoBuildEnv:
    """``KICAD_TOOLS_NO_AUTO_BUILD=1`` opts out (AG6, AG12)."""

    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
    def test_env_opt_out_skips_build(
        self, force_cpp_unavailable, toolchain_present, monkeypatch, value
    ):
        monkeypatch.setenv("KICAD_TOOLS_NO_AUTO_BUILD", value)
        m = _patch_build_native(monkeypatch, lambda **_: _FakeBuildResult(success=True))

        ok, force_python, exit_code = cpp_backend.ensure_cpp_backend_available(
            backend="auto", quiet=True
        )

        assert ok is True
        assert force_python is False
        assert exit_code is None
        assert m.call_count == 0

    @pytest.mark.parametrize("value", ["", "0", "false", "no", "off"])
    def test_env_falsy_does_not_opt_out(
        self, force_cpp_unavailable, toolchain_present, monkeypatch, value
    ):
        monkeypatch.setenv("KICAD_TOOLS_NO_AUTO_BUILD", value)
        _patch_reload_to_succeed(monkeypatch)
        m = _patch_build_native(monkeypatch, lambda **_: _FakeBuildResult(success=True))

        cpp_backend.ensure_cpp_backend_available(backend="auto", quiet=True)
        assert m.call_count == 1


class TestAllowAutoBuildKwarg:
    """``allow_auto_build=False`` opts out programmatically."""

    def test_disallow_skips_build(
        self, force_cpp_unavailable, toolchain_present, no_env_optout, monkeypatch
    ):
        m = _patch_build_native(monkeypatch, lambda **_: _FakeBuildResult(success=True))

        ok, force_python, exit_code = cpp_backend.ensure_cpp_backend_available(
            backend="auto", quiet=True, allow_auto_build=False
        )

        assert ok is True
        assert force_python is False
        assert m.call_count == 0


class TestToolchainDetection:
    """No cmake / no compiler -> skip build silently (AG2, AG11)."""

    def test_missing_toolchain_skips_build(self, force_cpp_unavailable, no_env_optout, monkeypatch):
        monkeypatch.setattr(cpp_backend, "_toolchain_available", lambda: False)
        m = _patch_build_native(monkeypatch, lambda **_: _FakeBuildResult(success=True))

        ok, force_python, exit_code = cpp_backend.ensure_cpp_backend_available(
            backend="auto", quiet=True
        )

        assert ok is True
        assert force_python is False
        assert m.call_count == 0


class TestGracefulFailure:
    """Build failures must never crash route (AG13)."""

    def test_build_failure_falls_through_to_python(
        self, force_cpp_unavailable, toolchain_present, no_env_optout, monkeypatch
    ):
        m = _patch_build_native(
            monkeypatch,
            lambda **_: _FakeBuildResult(success=False, error_message="cmake fail"),
        )

        ok, force_python, exit_code = cpp_backend.ensure_cpp_backend_available(
            backend="auto", quiet=True
        )

        # Routing continues with Python fallback (force_python stays False --
        # the existing call site picks up Python because is_cpp_available()
        # is still False after the failed build).
        assert ok is True
        assert force_python is False
        assert exit_code is None
        assert m.call_count == 1

    def test_build_timeout_falls_through_to_python(
        self, force_cpp_unavailable, toolchain_present, no_env_optout, monkeypatch
    ):
        def _raise_timeout(**_):
            raise subprocess.TimeoutExpired(cmd="cmake", timeout=600)

        m = _patch_build_native(monkeypatch, _raise_timeout)

        ok, force_python, exit_code = cpp_backend.ensure_cpp_backend_available(
            backend="auto", quiet=True
        )

        assert ok is True
        assert force_python is False
        assert exit_code is None
        assert m.call_count == 1

    def test_build_permission_error_falls_through_to_python(
        self, force_cpp_unavailable, toolchain_present, no_env_optout, monkeypatch
    ):
        def _raise_perm(**_):
            raise PermissionError("read-only filesystem")

        m = _patch_build_native(monkeypatch, _raise_perm)

        ok, force_python, exit_code = cpp_backend.ensure_cpp_backend_available(
            backend="auto", quiet=True
        )

        assert ok is True
        assert force_python is False
        assert exit_code is None
        assert m.call_count == 1


class TestStaleSoTriggersRebuild:
    """Stale .so / wrong cpython tag goes through the same auto-rebuild path (AG7)."""

    def test_stale_so_triggers_rebuild(self, toolchain_present, no_env_optout, monkeypatch):
        # Simulate a stale .so: import "succeeded" but BUILD_VERSION
        # mismatch turned _CPP_AVAILABLE off.  The downstream code
        # path is identical to the "missing .so" case from the
        # helper's perspective -- both manifest as
        # is_cpp_available() == False.
        monkeypatch.setattr(cpp_backend, "_CPP_AVAILABLE", False)
        monkeypatch.setattr(
            cpp_backend,
            "_CPP_IMPORT_ERROR",
            "router_cpp build version 1 does not match required 2",
        )
        _patch_reload_to_succeed(monkeypatch)
        m = _patch_build_native(monkeypatch, lambda **_: _FakeBuildResult(success=True))

        ok, force_python, exit_code = cpp_backend.ensure_cpp_backend_available(
            backend="auto", quiet=True
        )

        assert ok is True
        assert m.call_count == 1


class TestRouteCmdCallsHelper:
    """All four call sites in route_cmd.py route through the helper (AG8, AG14)."""

    def test_all_route_cmd_call_sites_use_helper(self):
        """Static check: route_cmd.py imports/calls ensure_cpp_backend_available exactly 4 times."""
        import pathlib

        path = pathlib.Path(cpp_backend.__file__).parent.parent / "cli" / "route_cmd.py"
        text = path.read_text()
        # The helper is called once per backend-selection block.
        # Four functions: route_with_layer_escalation, route_with_rule_relaxation,
        # route_with_combined_escalation, and main().
        n_calls = text.count("ensure_cpp_backend_available(")
        assert n_calls >= 4, (
            f"Expected at least 4 calls to ensure_cpp_backend_available "
            f"in route_cmd.py, got {n_calls}"
        )

        # And the old inline blocks must be gone.
        assert "Error: C++ backend requested but not available" not in text, (
            "Old inline backend-selection block still present in route_cmd.py"
        )
        assert "WARNING: C++ router backend not installed" not in text, (
            "Old inline Python-fallback warning still present in route_cmd.py"
        )


class TestReloadCppBackend:
    """Issue #2594: ``_reload_cpp_backend()`` must flush import caches.

    The auto-build path writes a fresh ``router_cpp.*.so`` to disk and
    then calls ``_reload_cpp_backend()``.  Before this fix,
    ``importlib.reload(cpp_backend)`` alone did not pick up the new
    ``.so`` because:

      1. The original failed ``from . import router_cpp`` left a stale
         entry in ``sys.modules`` for ``kicad_tools.router.router_cpp``
         that Python re-used on subsequent imports.
      2. The parent package's ``FileFinder`` had a cached directory
         listing that did not contain the freshly-written ``.so``.

    These tests pin the cache-flushing behavior so future regressions
    surface as test failures rather than as the silent
    "C++ build succeeded but module reload did not pick it up" warning.
    """

    def test_reload_pops_stale_router_cpp_from_sys_modules(self, monkeypatch):
        """``_reload_cpp_backend()`` removes the stale router_cpp entry.

        Simulates the fresh-checkout scenario: a sentinel object is
        planted in ``sys.modules`` under ``kicad_tools.router.router_cpp``
        to mimic Python's negative-import cache from the original failed
        ``from . import router_cpp`` at startup.  After
        ``_reload_cpp_backend()`` runs, that sentinel must be gone --
        otherwise the reloaded ``cpp_backend`` would re-use it instead
        of running the module finder against the freshly-written .so.
        """
        import sys

        sentinel = object()
        monkeypatch.setitem(sys.modules, "kicad_tools.router.router_cpp", sentinel)

        cpp_backend._reload_cpp_backend()

        # After reload, the stale sentinel MUST be gone.  Either the
        # entry has been re-populated by a successful import (real .so
        # on disk) or it has been removed entirely (no .so).  In either
        # case the sentinel object itself must not survive.
        assert sys.modules.get("kicad_tools.router.router_cpp") is not sentinel

    def test_reload_calls_invalidate_caches(self, monkeypatch):
        """``_reload_cpp_backend()`` invokes ``importlib.invalidate_caches``.

        Without this call, Python's ``FileFinder`` keeps its cached
        directory listing for ``kicad_tools/router/`` from before the
        build wrote ``router_cpp.*.so``, so the reloaded ``cpp_backend``
        misses the freshly-installed file.
        """
        import importlib

        called = {"count": 0}
        original = importlib.invalidate_caches

        def _spy() -> None:
            called["count"] += 1
            original()

        monkeypatch.setattr(importlib, "invalidate_caches", _spy)

        cpp_backend._reload_cpp_backend()

        assert called["count"] >= 1, (
            "_reload_cpp_backend() must call importlib.invalidate_caches() "
            "before reload(cpp_backend) so the freshly-written router_cpp.*.so "
            "is visible to the parent package's FileFinder"
        )

    def test_reload_recovers_when_router_cpp_was_set_to_none(self, monkeypatch):
        """Fresh-checkout simulation: cpp_backend.router_cpp == None, .so on disk.

        This mirrors the in-process state immediately after
        ``_attempt_auto_build`` finishes writing the .so but before
        ``_reload_cpp_backend()`` runs.  If the C++ extension *is* on
        disk in this test run (i.e., ``is_cpp_available()`` was True
        before we patched it), the reload must recover it.

        This is the strongest available regression test: it actually
        exercises the import system's cache-flushing on the live tree
        without requiring a build invocation.  The test is skipped on
        environments that genuinely have no .so on disk.
        """
        if not cpp_backend.is_cpp_available():
            import pytest

            pytest.skip("C++ backend not available on this environment")

        # Stash the live state so we can restore it.
        live_router_cpp = cpp_backend.router_cpp
        live_available = cpp_backend._CPP_AVAILABLE
        live_error = cpp_backend._CPP_IMPORT_ERROR

        try:
            # Force the in-process flags into the "post-failed-import"
            # state that the auto-build path would see.
            monkeypatch.setattr(cpp_backend, "_CPP_AVAILABLE", False)
            monkeypatch.setattr(cpp_backend, "router_cpp", None)
            monkeypatch.setattr(
                cpp_backend, "_CPP_IMPORT_ERROR", "stub: simulated startup failure"
            )

            recovered = cpp_backend._reload_cpp_backend()

            assert recovered is True, (
                "_reload_cpp_backend() must recover the C++ backend when the "
                ".so is present on disk (Issue #2594). It did not -- this is "
                "the regression that produces the 'C++ build succeeded but "
                "module reload did not pick it up' warning."
            )
            assert cpp_backend.is_cpp_available() is True
            assert cpp_backend.router_cpp is not None
        finally:
            # Restore live state regardless of pass/fail so subsequent
            # tests in the session see the real backend.
            cpp_backend._CPP_AVAILABLE = live_available
            cpp_backend.router_cpp = live_router_cpp
            cpp_backend._CPP_IMPORT_ERROR = live_error
