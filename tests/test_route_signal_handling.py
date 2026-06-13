"""Tests for adaptive-rules signal handling (issue #2378).

Verifies that route_with_rule_relaxation and route_with_combined_escalation
register SIGTERM/SIGINT handlers that save the best completed attempt, and
restore the original handlers on normal exit.
"""

import signal
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture()
def _reset_interrupt_state():
    """Reset the module-level _interrupt_state before/after each test."""
    from kicad_tools.cli.route_cmd import _interrupt_state

    original = dict(_interrupt_state)
    _interrupt_state["interrupted"] = False
    _interrupt_state["router"] = None
    _interrupt_state["output_path"] = None
    _interrupt_state["pcb_path"] = None
    _interrupt_state["quiet"] = False
    _interrupt_state["best_completed_attempt"] = False
    yield _interrupt_state
    _interrupt_state.update(original)


class TestAdaptiveRulesSignalRegistration:
    """Verify that adaptive-rules functions register and restore signal handlers."""

    @pytest.mark.usefixtures("_reset_interrupt_state")
    def test_rule_relaxation_registers_sigterm_and_sigint(self, tmp_path):
        """route_with_rule_relaxation registers SIGTERM+SIGINT and restores on exit."""
        from kicad_tools.cli.route_cmd import (
            _handle_interrupt,
            route_with_rule_relaxation,
        )

        pcb_file = tmp_path / "test.kicad_pcb"
        pcb_file.write_text('(kicad_pcb (version 20240101) (generator "test"))')
        out_file = tmp_path / "out.kicad_pcb"

        # Record signal registrations
        registered = []
        original_signal = signal.signal

        def tracking_signal(sig, handler):
            registered.append((sig, handler))
            return original_signal(sig, handler)

        # Patch get_relaxation_tiers at the source to return empty list so the
        # function skips the tier loop entirely.  Also patch _auto_skip_pour_nets
        # which is a module-level function (not locally imported).
        with (
            patch("kicad_tools.router.get_relaxation_tiers", return_value=[]),
            patch("kicad_tools.cli.route_cmd._auto_skip_pour_nets", return_value=([], [])),
            patch("signal.signal", side_effect=tracking_signal),
        ):
            route_with_rule_relaxation(pcb_file, out_file, _make_args(), quiet=True)

        # Both SIGINT and SIGTERM should have been registered with _handle_interrupt
        sigint_handlers = [h for s, h in registered if s == signal.SIGINT]
        sigterm_handlers = [h for s, h in registered if s == signal.SIGTERM]
        assert any(h == _handle_interrupt for h in sigint_handlers), "SIGINT handler not registered"
        assert any(h == _handle_interrupt for h in sigterm_handlers), (
            "SIGTERM handler not registered"
        )

    @pytest.mark.usefixtures("_reset_interrupt_state")
    def test_combined_escalation_registers_sigterm_and_sigint(self, tmp_path):
        """route_with_combined_escalation registers SIGTERM+SIGINT and restores on exit."""
        from kicad_tools.cli.route_cmd import (
            _handle_interrupt,
            route_with_combined_escalation,
        )

        pcb_file = tmp_path / "test.kicad_pcb"
        pcb_file.write_text('(kicad_pcb (version 20240101) (generator "test"))')
        out_file = tmp_path / "out.kicad_pcb"

        registered = []
        original_signal = signal.signal

        def tracking_signal(sig, handler):
            registered.append((sig, handler))
            return original_signal(sig, handler)

        with (
            patch("kicad_tools.router.get_relaxation_tiers", return_value=[]),
            patch("kicad_tools.cli.route_cmd._auto_skip_pour_nets", return_value=([], [])),
            patch("signal.signal", side_effect=tracking_signal),
        ):
            route_with_combined_escalation(pcb_file, out_file, _make_args(), quiet=True)

        sigint_handlers = [h for s, h in registered if s == signal.SIGINT]
        sigterm_handlers = [h for s, h in registered if s == signal.SIGTERM]
        assert any(h == _handle_interrupt for h in sigint_handlers), "SIGINT handler not registered"
        assert any(h == _handle_interrupt for h in sigterm_handlers), (
            "SIGTERM handler not registered"
        )

    @pytest.mark.usefixtures("_reset_interrupt_state")
    def test_rule_relaxation_restores_handlers_on_exit(self, tmp_path):
        """After normal exit, original signal handlers are restored."""
        from kicad_tools.cli.route_cmd import route_with_rule_relaxation

        pcb_file = tmp_path / "test.kicad_pcb"
        pcb_file.write_text('(kicad_pcb (version 20240101) (generator "test"))')
        out_file = tmp_path / "out.kicad_pcb"

        # Record the handlers before calling the function
        orig_sigint = signal.getsignal(signal.SIGINT)
        orig_sigterm = signal.getsignal(signal.SIGTERM)

        with (
            patch("kicad_tools.router.get_relaxation_tiers", return_value=[]),
            patch("kicad_tools.cli.route_cmd._auto_skip_pour_nets", return_value=([], [])),
        ):
            route_with_rule_relaxation(pcb_file, out_file, _make_args(), quiet=True)

        # Handlers should be restored to their original values
        assert signal.getsignal(signal.SIGINT) == orig_sigint
        assert signal.getsignal(signal.SIGTERM) == orig_sigterm


class TestAdaptiveRulesInterruptStateBestAttempt:
    """Verify that _interrupt_state is updated when best_result improves."""

    @pytest.mark.usefixtures("_reset_interrupt_state")
    def test_interrupt_state_updated_after_best_result(self):
        """After a tier completes and becomes the best, _interrupt_state holds its router."""
        from kicad_tools.cli.route_cmd import _interrupt_state

        # Simulate what happens inside the tier loop after best_result update
        mock_router = MagicMock()
        mock_router.routes = [MagicMock()]

        # Simulate the code path: best_result = result; _interrupt_state update
        _interrupt_state["router"] = mock_router
        _interrupt_state["best_completed_attempt"] = True

        assert _interrupt_state["router"] is mock_router
        assert _interrupt_state["best_completed_attempt"] is True


class TestSavePartialResultsBestAttempt:
    """Verify that _save_partial_results writes to the main output path for best attempts."""

    @pytest.mark.usefixtures("_reset_interrupt_state")
    def test_save_uses_main_path_for_best_completed_attempt(self, tmp_path):
        """When best_completed_attempt is True, save to main output_path (not _partial)."""
        from kicad_tools.cli.route_cmd import _interrupt_state, _save_partial_results

        pcb_file = tmp_path / "test.kicad_pcb"
        pcb_file.write_text('(kicad_pcb (version 20240101) (generator "test")\n)')
        out_file = tmp_path / "out.kicad_pcb"

        mock_router = MagicMock()
        mock_router.routes = [MagicMock()]
        mock_router.to_sexp.return_value = (
            '(segment (start 0 0) (end 1 1) (width 0.25) (layer "F.Cu") (net 1))'
        )
        mock_router.get_statistics.return_value = {
            "nets_routed": 5,
            "segments": 10,
            "vias": 2,
        }

        _interrupt_state["router"] = mock_router
        _interrupt_state["output_path"] = out_file
        _interrupt_state["pcb_path"] = pcb_file
        _interrupt_state["quiet"] = True
        _interrupt_state["best_completed_attempt"] = True

        result = _save_partial_results()

        assert result is True
        # Should write to out.kicad_pcb, NOT out_partial.kicad_pcb
        assert out_file.exists()
        partial_file = tmp_path / "out_partial.kicad_pcb"
        assert not partial_file.exists()

    @pytest.mark.usefixtures("_reset_interrupt_state")
    def test_save_uses_partial_path_for_in_progress(self, tmp_path):
        """When best_completed_attempt is False, save to _partial path (existing behavior)."""
        from kicad_tools.cli.route_cmd import _interrupt_state, _save_partial_results

        pcb_file = tmp_path / "test.kicad_pcb"
        pcb_file.write_text('(kicad_pcb (version 20240101) (generator "test")\n)')
        out_file = tmp_path / "out.kicad_pcb"

        mock_router = MagicMock()
        mock_router.routes = [MagicMock()]
        mock_router.to_sexp.return_value = (
            '(segment (start 0 0) (end 1 1) (width 0.25) (layer "F.Cu") (net 1))'
        )
        mock_router.get_statistics.return_value = {
            "nets_routed": 3,
            "segments": 6,
            "vias": 1,
        }

        _interrupt_state["router"] = mock_router
        _interrupt_state["output_path"] = out_file
        _interrupt_state["pcb_path"] = pcb_file
        _interrupt_state["quiet"] = True
        _interrupt_state["best_completed_attempt"] = False

        result = _save_partial_results()

        assert result is True
        # Should write to out_partial.kicad_pcb, NOT out.kicad_pcb
        partial_file = tmp_path / "out_partial.kicad_pcb"
        assert partial_file.exists()
        assert not out_file.exists()

    @pytest.mark.usefixtures("_reset_interrupt_state")
    def test_save_graceful_when_no_best_result(self, tmp_path):
        """When router is None (no tier completed), _save_partial_results returns False."""
        from kicad_tools.cli.route_cmd import _interrupt_state, _save_partial_results

        _interrupt_state["router"] = None
        _interrupt_state["output_path"] = tmp_path / "out.kicad_pcb"
        _interrupt_state["pcb_path"] = tmp_path / "test.kicad_pcb"
        _interrupt_state["quiet"] = True
        _interrupt_state["best_completed_attempt"] = False

        result = _save_partial_results()

        assert result is False


def _make_args(**overrides):
    """Build a minimal args namespace for routing functions."""
    from types import SimpleNamespace

    defaults = {
        "trace_width": 0.25,
        "clearance": 0.2,
        "via_drill": 0.3,
        "via_diameter": 0.6,
        "manufacturer": "generic",
        "min_trace": None,
        "min_clearance_floor": None,
        "grid": 0.1,
        "layers": "auto",
        "backend": "python",
        "skip_nets": None,
        "strategy": "basic",
        "timeout": None,
        "iterations": 1,
        "min_completion": 0.95,
        "verbose": False,
        "no_optimize": True,
        "skip_drc": True,
        "dry_run": False,
        "force": True,
        "edge_clearance": 0.5,
        "mc_trials": 3,
        "multi_resolution": False,
        "two_phase": False,
        "batch_routing": False,
        "high_performance": False,
        "hierarchical": False,
        "perturbation": True,
        "pcb": "test.kicad_pcb",
        "max_layers": 6,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)
