"""Tests for pipeline CLI arg parsing (parser.py) and forwarding (commands/pipeline.py).

Verifies that --no-cache, --clear-cache, --sch/--schematic, and the full set of
--step choices are exposed through the top-level CLI parser and correctly
forwarded to the internal pipeline_cmd.main() entrypoint.
"""

from __future__ import annotations

import argparse
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from kicad_tools.cli.commands.pipeline import run_pipeline_command
from kicad_tools.cli.parser import create_parser

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_pipeline_args(argv: list[str]) -> argparse.Namespace:
    """Parse argv through the top-level CLI parser as if running ``kct pipeline ...``."""
    parser = create_parser()
    return parser.parse_args(["pipeline", *argv])


# ---------------------------------------------------------------------------
# --no-cache / --clear-cache parsing
# ---------------------------------------------------------------------------


class TestCacheFlagsParsing:
    """Verify --no-cache and --clear-cache are accepted by the CLI parser."""

    def test_no_cache_parsed(self):
        ns = _parse_pipeline_args(["board.kicad_pcb", "--no-cache"])
        assert ns.pipeline_no_cache is True

    def test_clear_cache_parsed(self):
        ns = _parse_pipeline_args(["board.kicad_pcb", "--clear-cache"])
        assert ns.pipeline_clear_cache is True

    def test_both_cache_flags_parsed(self):
        ns = _parse_pipeline_args(["board.kicad_pcb", "--no-cache", "--clear-cache"])
        assert ns.pipeline_no_cache is True
        assert ns.pipeline_clear_cache is True

    def test_cache_flags_default_false(self):
        ns = _parse_pipeline_args(["board.kicad_pcb"])
        assert ns.pipeline_no_cache is False
        assert ns.pipeline_clear_cache is False


# ---------------------------------------------------------------------------
# --sch / --schematic parsing
# ---------------------------------------------------------------------------


class TestSchFlagParsing:
    """Verify --sch and --schematic are accepted by the CLI parser."""

    def test_sch_short_form(self):
        ns = _parse_pipeline_args(["board.kicad_pcb", "--sch", "my.kicad_sch"])
        assert ns.pipeline_sch == "my.kicad_sch"

    def test_schematic_long_form(self):
        ns = _parse_pipeline_args(["board.kicad_pcb", "--schematic", "my.kicad_sch"])
        assert ns.pipeline_sch == "my.kicad_sch"

    def test_sch_default_none(self):
        ns = _parse_pipeline_args(["board.kicad_pcb"])
        assert ns.pipeline_sch is None


# ---------------------------------------------------------------------------
# --step choices updated to 13 steps
# ---------------------------------------------------------------------------


class TestStepChoices:
    """Verify all 13 PipelineStep values are accepted by --step."""

    ALL_STEPS = [
        "erc",
        "fix-erc",
        "fix-silkscreen",
        "route",
        "stitch",
        "fix-vias",
        "fix-drc",
        "optimize",
        "zones",
        "zones-refill",
        "audit",
        "report",
        "export",
    ]

    @pytest.mark.parametrize("step", ALL_STEPS)
    def test_step_accepted(self, step: str):
        ns = _parse_pipeline_args(["board.kicad_pcb", "--step", step])
        assert ns.pipeline_step == step

    def test_invalid_step_rejected(self):
        with pytest.raises(SystemExit):
            _parse_pipeline_args(["board.kicad_pcb", "--step", "nonexistent"])


# ---------------------------------------------------------------------------
# Forwarding in run_pipeline_command
# ---------------------------------------------------------------------------


class TestForwarding:
    """Verify commands/pipeline.py forwards new flags into sub_argv."""

    def _capture_argv(self, args_dict: dict) -> list[str]:
        """Build a Namespace from *args_dict*, call run_pipeline_command, capture forwarded argv."""
        ns = SimpleNamespace(**args_dict)
        captured: list[str] = []

        def _fake_main(argv):
            captured.extend(argv)
            return 0

        with patch("kicad_tools.cli.pipeline_cmd.main", _fake_main):
            run_pipeline_command(ns)
        return captured

    def test_no_cache_forwarded(self):
        argv = self._capture_argv(
            {
                "pipeline_input": "b.kicad_pcb",
                "pipeline_no_cache": True,
                "pipeline_clear_cache": False,
                "pipeline_sch": None,
                "global_quiet": False,
            }
        )
        assert "--no-cache" in argv

    def test_clear_cache_forwarded(self):
        argv = self._capture_argv(
            {
                "pipeline_input": "b.kicad_pcb",
                "pipeline_no_cache": False,
                "pipeline_clear_cache": True,
                "pipeline_sch": None,
                "global_quiet": False,
            }
        )
        assert "--clear-cache" in argv

    def test_both_cache_flags_forwarded(self):
        argv = self._capture_argv(
            {
                "pipeline_input": "b.kicad_pcb",
                "pipeline_no_cache": True,
                "pipeline_clear_cache": True,
                "pipeline_sch": None,
                "global_quiet": False,
            }
        )
        assert "--no-cache" in argv
        assert "--clear-cache" in argv

    def test_sch_forwarded(self):
        argv = self._capture_argv(
            {
                "pipeline_input": "b.kicad_pcb",
                "pipeline_no_cache": False,
                "pipeline_clear_cache": False,
                "pipeline_sch": "my.kicad_sch",
                "global_quiet": False,
            }
        )
        assert "--sch" in argv
        idx = argv.index("--sch")
        assert argv[idx + 1] == "my.kicad_sch"

    def test_no_flags_when_defaults(self):
        argv = self._capture_argv(
            {
                "pipeline_input": "b.kicad_pcb",
                "pipeline_no_cache": False,
                "pipeline_clear_cache": False,
                "pipeline_sch": None,
                "global_quiet": False,
            }
        )
        assert "--no-cache" not in argv
        assert "--clear-cache" not in argv
        assert "--sch" not in argv


# ---------------------------------------------------------------------------
# --route-skip-threshold parsing + forwarding
# ---------------------------------------------------------------------------


class TestRouteSkipThresholdParsing:
    """Verify --route-skip-threshold is exposed by the top-level CLI parser."""

    def test_default_is_95(self):
        ns = _parse_pipeline_args(["board.kicad_pcb"])
        assert ns.pipeline_route_skip_threshold == 95.0

    def test_custom_value_parsed(self):
        ns = _parse_pipeline_args(["board.kicad_pcb", "--route-skip-threshold", "80.0"])
        assert ns.pipeline_route_skip_threshold == 80.0

    def test_value_is_float(self):
        ns = _parse_pipeline_args(["board.kicad_pcb", "--route-skip-threshold", "50"])
        assert isinstance(ns.pipeline_route_skip_threshold, float)
        assert ns.pipeline_route_skip_threshold == 50.0


class TestRouteSkipThresholdForwarding:
    """Verify commands/pipeline.py forwards --route-skip-threshold into sub_argv."""

    def _capture_argv(self, args_dict: dict) -> list[str]:
        ns = SimpleNamespace(**args_dict)
        captured: list[str] = []

        def _fake_main(argv):
            captured.extend(argv)
            return 0

        with patch("kicad_tools.cli.pipeline_cmd.main", _fake_main):
            run_pipeline_command(ns)
        return captured

    def test_default_threshold_not_forwarded(self):
        """When threshold equals 95.0 default, the flag is omitted from sub_argv."""
        argv = self._capture_argv(
            {
                "pipeline_input": "b.kicad_pcb",
                "pipeline_route_skip_threshold": 95.0,
                "global_quiet": False,
            }
        )
        assert "--route-skip-threshold" not in argv

    def test_custom_threshold_forwarded(self):
        argv = self._capture_argv(
            {
                "pipeline_input": "b.kicad_pcb",
                "pipeline_route_skip_threshold": 80.0,
                "global_quiet": False,
            }
        )
        assert "--route-skip-threshold" in argv
        idx = argv.index("--route-skip-threshold")
        assert argv[idx + 1] == "80.0"

    def test_round_trip_parser_to_main(self):
        """Full chain: top-level parser -> shim -> pipeline_cmd.main argv."""
        ns = _parse_pipeline_args(["board.kicad_pcb", "--route-skip-threshold", "75.0"])
        # Forwarding picks up the parsed namespace value.
        captured: list[str] = []

        def _fake_main(argv):
            captured.extend(argv)
            return 0

        with patch("kicad_tools.cli.pipeline_cmd.main", _fake_main):
            # The top-level namespace lacks `global_quiet` by default; supply it.
            ns.global_quiet = False
            run_pipeline_command(ns)
        assert "--route-skip-threshold" in captured
        idx = captured.index("--route-skip-threshold")
        assert captured[idx + 1] == "75.0"
