"""Explicit ``--auto-layers`` must survive the outer ``kct route`` dispatcher.

Regression tests for issue #4502.  The outer parser (``cli/parser.py``)
previously declared ``--auto-layers`` as a ``BooleanOptionalAction`` with
``default=True``, which collapsed "user explicitly typed ``--auto-layers``"
and "user typed nothing" into the same ``args.auto_layers is True``.
``run_route_command`` therefore forwarded only ``--no-auto-layers``, so the
inner ``route_cmd`` argv-sniffing sites --
``_apply_complete_mode_defaults`` (which disables auto-layers under
``--complete`` "unless you pass ``--auto-layers``") and the
``--auto-layers``/``--layers`` conflict check -- could never observe the
user's explicit override.

These tests drive the **real** dispatcher (``create_parser()`` ->
``run_route_command()`` -> mocked ``route_cmd.main``) rather than a
hand-built ``SimpleNamespace``, which is precisely why the existing unit
tests in ``tests/test_layer_escalation.py`` did not catch the bug.
"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest


@pytest.fixture
def pcb_file(tmp_path):
    """A placeholder board path -- ``route_cmd.main`` is mocked in most tests."""
    pcb = tmp_path / "board.kicad_pcb"
    pcb.write_text("(kicad_pcb (version 20221018) (generator pcbnew))")
    return pcb


def _dispatch(argv):
    """Parse ``argv`` with the real outer parser and capture the inner argv."""
    from kicad_tools.cli.commands.routing import run_route_command
    from kicad_tools.cli.parser import create_parser

    parser = create_parser()
    args = parser.parse_args(argv)
    with patch("kicad_tools.cli.route_cmd.main") as mock_main:
        mock_main.return_value = 0
        run_route_command(args)
        return args, mock_main.call_args[0][0]


class TestExplicitAutoLayersForwarding:
    """The outer parser's tri-state value reaches the inner argv intact."""

    def test_explicit_auto_layers_survives_outer_dispatcher(self, pcb_file):
        """Issue #4502: an explicit --auto-layers on the outer ``kct route``
        parser must reach the inner route_cmd.main() argv, not collapse into
        the default and get silently dropped."""
        args, sub_argv = _dispatch(
            ["route", str(pcb_file), "--complete", "--auto-layers", "--dry-run", "--quiet"]
        )

        assert args.auto_layers is True
        assert "--auto-layers" in sub_argv
        assert "--no-auto-layers" not in sub_argv

    def test_unset_auto_layers_is_none_and_forwards_nothing(self, pcb_file):
        """No flag => tri-state ``None`` and nothing forwarded, so the inner
        parser's own ``default=True`` still supplies the effective default."""
        args, sub_argv = _dispatch(["route", str(pcb_file), "--dry-run", "--quiet"])

        assert args.auto_layers is None
        assert "--auto-layers" not in sub_argv
        assert "--no-auto-layers" not in sub_argv

    def test_explicit_no_auto_layers_still_forwarded(self, pcb_file):
        """``--no-auto-layers`` keeps working exactly as before."""
        args, sub_argv = _dispatch(
            ["route", str(pcb_file), "--no-auto-layers", "--dry-run", "--quiet"]
        )

        assert args.auto_layers is False
        assert "--no-auto-layers" in sub_argv
        assert "--auto-layers" not in sub_argv


class TestCompleteModeRespectsExplicitOverride:
    """``_apply_complete_mode_defaults`` sees the forwarded token (#4502/#4477)."""

    @staticmethod
    def _apply(sub_argv, auto_layers):
        """Run the inner ``--complete`` defaults against a forwarded argv."""
        from kicad_tools.cli.route_cmd import _apply_complete_mode_defaults

        args = SimpleNamespace(
            complete=True,
            preserve_existing=False,
            route_engine="grid",
            auto_layers=auto_layers,
            quiet=False,
        )
        # Stand-in for the inner parser: only ``get_default`` is consulted.
        parser = SimpleNamespace(get_default=lambda name: {"route_engine": "grid"}[name])
        _apply_complete_mode_defaults(args, parser, sub_argv)
        return args

    def test_complete_keeps_explicit_auto_layers(self, pcb_file, capsys):
        """``--complete --auto-layers`` no longer prints the misleading
        'disabling --auto-layers ... pass --auto-layers to override' notice."""
        _args, sub_argv = _dispatch(
            ["route", str(pcb_file), "--complete", "--auto-layers", "--dry-run"]
        )
        capsys.readouterr()  # discard dispatcher output

        inner = self._apply(sub_argv, auto_layers=True)

        assert inner.auto_layers is True
        assert "disabling --auto-layers" not in capsys.readouterr().out

    def test_complete_without_flag_still_disables_auto_layers(self, pcb_file, capsys):
        """No explicit flag => ``--complete`` still disables auto-layers (#4477)."""
        _args, sub_argv = _dispatch(["route", str(pcb_file), "--complete", "--dry-run"])
        capsys.readouterr()  # discard dispatcher output

        # The inner parser's own default=True is what --complete overrides.
        inner = self._apply(sub_argv, auto_layers=True)

        assert inner.auto_layers is False
        assert "disabling --auto-layers" in capsys.readouterr().out

    def test_complete_with_no_auto_layers_still_disabled(self, pcb_file, capsys):
        """``--complete --no-auto-layers`` stays disabled, silently."""
        _args, sub_argv = _dispatch(
            ["route", str(pcb_file), "--complete", "--no-auto-layers", "--dry-run"]
        )
        capsys.readouterr()  # discard dispatcher output

        inner = self._apply(sub_argv, auto_layers=False)

        assert inner.auto_layers is False
        assert "disabling --auto-layers" not in capsys.readouterr().out


class TestAutoLayersLayersConflictThroughDispatcher:
    """The ``--auto-layers`` + ``--layers`` conflict check now fires (#4502)."""

    def test_conflict_tokens_reach_inner_argv(self, pcb_file):
        _args, sub_argv = _dispatch(
            ["route", str(pcb_file), "--auto-layers", "--layers", "4", "--dry-run", "--quiet"]
        )

        assert "--auto-layers" in sub_argv
        assert "--layers" in sub_argv

    def test_conflict_error_fires_in_route_main(self, pcb_file, capsys):
        """End-to-end: the forwarded argv makes ``route_cmd.main`` reject the
        explicit ``--auto-layers`` + ``--layers`` combination instead of
        silently letting ``--layers`` win."""
        from kicad_tools.cli import route_cmd

        rc = route_cmd.main([str(pcb_file), "--auto-layers", "--layers", "4", "--dry-run"])

        assert rc == 1
        assert "--auto-layers cannot be used with --layers" in capsys.readouterr().err
