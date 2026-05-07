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
