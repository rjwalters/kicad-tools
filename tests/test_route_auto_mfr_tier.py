"""Tests for ``kct route --auto-mfr-tier`` CLI integration (Issue #2881).

Covers:
- Argparse validation: ``--auto-mfr-tier`` parses, default-off.
- ``--mfr-tier-ladder`` parses and overrides the default ladder.
- Unknown ladder entries raise a clear error.
- ``route_with_mfr_tier_escalation`` dispatch: walks the ladder, mutates
  ``args.manufacturer`` per attempt, stops on success.
- Trigger table: PIN_ACCESS escalates; BLOCKED_PATH / CONGESTION do not.
- Ladder termination: with ladder=[a,b,c], the loop runs at most len(ladder)
  outer iterations and never revisits a tier.
- Recommendation line is emitted when escalation actually moved off the
  starting tier.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


class TestArgparseFlagPlumbing:
    """Argparse plumbing for --auto-mfr-tier / --mfr-tier-ladder."""

    def _get_route_parser(self):
        """Construct the route subcommand parser from kicad_tools.cli.parser."""
        from kicad_tools.cli.parser import create_parser

        parser = create_parser()
        return parser

    def test_auto_mfr_tier_parses(self):
        """--auto-mfr-tier is a recognized boolean flag."""
        parser = self._get_route_parser()
        args = parser.parse_args(["route", "test.kicad_pcb", "--auto-mfr-tier"])
        assert args.auto_mfr_tier is True

    def test_auto_mfr_tier_default_off(self):
        """--auto-mfr-tier is opt-in (default False)."""
        parser = self._get_route_parser()
        args = parser.parse_args(["route", "test.kicad_pcb"])
        assert args.auto_mfr_tier is False

    def test_mfr_tier_ladder_parses(self):
        """--mfr-tier-ladder accepts a comma-separated value."""
        parser = self._get_route_parser()
        args = parser.parse_args(
            ["route", "test.kicad_pcb", "--auto-mfr-tier", "--mfr-tier-ladder", "jlcpcb,jlcpcb-tier1"]
        )
        assert args.mfr_tier_ladder == "jlcpcb,jlcpcb-tier1"

    def test_mfr_tier_ladder_default_none(self):
        parser = self._get_route_parser()
        args = parser.parse_args(["route", "test.kicad_pcb"])
        assert args.mfr_tier_ladder is None


class TestMfrTierEscalationDispatch:
    """Tests for route_with_mfr_tier_escalation control flow."""

    def _make_args(self, **overrides):
        """Build a minimal-but-complete args namespace."""
        base = SimpleNamespace(
            pcb="test.kicad_pcb",
            manufacturer="jlcpcb",
            auto_mfr_tier=True,
            mfr_tier_ladder=None,
            auto_layers=True,
            adaptive_rules=False,
            quiet=True,
            timeout=None,
            _wall_clock_deadline=None,
        )
        for k, v in overrides.items():
            setattr(base, k, v)
        return base

    def test_walks_default_jlcpcb_ladder(self):
        """When base jlcpcb fails, escalate to jlcpcb-tier1."""
        from kicad_tools.cli.route_cmd import route_with_mfr_tier_escalation

        args = self._make_args()

        # Stub route_with_layer_escalation to record per-tier args.manufacturer.
        seen_tiers: list[str] = []

        def fake_inner(*, pcb_path, output_path, args, quiet):
            seen_tiers.append(args.manufacturer)
            # Set the missed_via_in_pad_rescues signal so escalation triggers.
            mock_router = MagicMock()
            mock_router._escape_router = MagicMock()
            mock_router._escape_router.missed_via_in_pad_rescues = 3
            args._last_router = mock_router
            # Return failure (exit 2) on the first tier so we escalate.
            if args.manufacturer == "jlcpcb-tier1":
                return 0  # success on tier1 stops escalation
            return 2

        with patch(
            "kicad_tools.cli.route_cmd.route_with_layer_escalation",
            side_effect=fake_inner,
        ):
            from pathlib import Path

            rc = route_with_mfr_tier_escalation(
                pcb_path=Path("test.kicad_pcb"),
                output_path=Path("out.kicad_pcb"),
                args=args,
                quiet=True,
            )

        assert seen_tiers == ["jlcpcb", "jlcpcb-tier1"]
        assert rc == 0
        # Successful tier should remain on args.manufacturer
        assert args.manufacturer == "jlcpcb-tier1"

    def test_stops_when_first_tier_succeeds(self):
        """When the starting tier already routes successfully, no escalation."""
        from kicad_tools.cli.route_cmd import route_with_mfr_tier_escalation

        args = self._make_args()
        seen_tiers: list[str] = []

        def fake_inner(*, pcb_path, output_path, args, quiet):
            seen_tiers.append(args.manufacturer)
            return 0

        with patch(
            "kicad_tools.cli.route_cmd.route_with_layer_escalation",
            side_effect=fake_inner,
        ):
            from pathlib import Path

            rc = route_with_mfr_tier_escalation(
                pcb_path=Path("test.kicad_pcb"),
                output_path=Path("out.kicad_pcb"),
                args=args,
                quiet=True,
            )

        assert seen_tiers == ["jlcpcb"]  # only one attempt
        assert rc == 0

    def test_explicit_ladder_overrides_default(self):
        """--mfr-tier-ladder overrides the registered default."""
        from kicad_tools.cli.route_cmd import route_with_mfr_tier_escalation

        # Custom ladder skipping the default
        args = self._make_args(
            manufacturer="oshpark",
            mfr_tier_ladder="oshpark,jlcpcb,jlcpcb-tier1",
        )
        seen_tiers: list[str] = []

        def fake_inner(*, pcb_path, output_path, args, quiet):
            seen_tiers.append(args.manufacturer)
            mock_router = MagicMock()
            mock_router._escape_router = MagicMock()
            mock_router._escape_router.missed_via_in_pad_rescues = 1
            args._last_router = mock_router
            return 2  # always fail to walk the whole ladder

        with patch(
            "kicad_tools.cli.route_cmd.route_with_layer_escalation",
            side_effect=fake_inner,
        ):
            from pathlib import Path

            route_with_mfr_tier_escalation(
                pcb_path=Path("test.kicad_pcb"),
                output_path=Path("out.kicad_pcb"),
                args=args,
                quiet=True,
            )

        # Must walk the full custom ladder (each step has a capability or
        # scalar gain over the previous).
        # oshpark -> jlcpcb: scalar gain (6mil -> 5mil clearance/trace)
        # jlcpcb -> jlcpcb-tier1: via-in-pad capability gain
        assert seen_tiers == ["oshpark", "jlcpcb", "jlcpcb-tier1"]

    def test_unknown_tier_in_explicit_ladder_raises(self):
        """Garbage tier names in --mfr-tier-ladder are caught up front."""
        from kicad_tools.cli.route_cmd import route_with_mfr_tier_escalation

        args = self._make_args(
            mfr_tier_ladder="jlcpcb,not-a-real-tier,jlcpcb-tier1",
        )

        with pytest.raises(ValueError):
            from pathlib import Path

            route_with_mfr_tier_escalation(
                pcb_path=Path("test.kicad_pcb"),
                output_path=Path("out.kicad_pcb"),
                args=args,
                quiet=True,
            )

    def test_single_tier_ladder_runs_once_only(self):
        """oshpark has a single-element ladder -> exactly one inner call."""
        from kicad_tools.cli.route_cmd import route_with_mfr_tier_escalation

        args = self._make_args(manufacturer="oshpark")
        seen_tiers: list[str] = []

        def fake_inner(*, pcb_path, output_path, args, quiet):
            seen_tiers.append(args.manufacturer)
            return 2  # fail; loop should still not retry

        with patch(
            "kicad_tools.cli.route_cmd.route_with_layer_escalation",
            side_effect=fake_inner,
        ):
            from pathlib import Path

            route_with_mfr_tier_escalation(
                pcb_path=Path("test.kicad_pcb"),
                output_path=Path("out.kicad_pcb"),
                args=args,
                quiet=True,
            )

        assert seen_tiers == ["oshpark"]

    def test_ladder_terminates_max_one_visit_per_tier(self):
        """With ladder=[a,b,c], every tier is visited at most once.

        Issue #2881 acceptance criterion: "Tier ladder must terminate".
        """
        from kicad_tools.cli.route_cmd import route_with_mfr_tier_escalation

        args = self._make_args(
            mfr_tier_ladder="oshpark,jlcpcb,jlcpcb-tier1",
            manufacturer="oshpark",
        )
        seen_tiers: list[str] = []

        def fake_inner(*, pcb_path, output_path, args, quiet):
            seen_tiers.append(args.manufacturer)
            mock_router = MagicMock()
            mock_router._escape_router = MagicMock()
            mock_router._escape_router.missed_via_in_pad_rescues = 1
            args._last_router = mock_router
            return 2  # all attempts fail

        with patch(
            "kicad_tools.cli.route_cmd.route_with_layer_escalation",
            side_effect=fake_inner,
        ):
            from pathlib import Path

            route_with_mfr_tier_escalation(
                pcb_path=Path("test.kicad_pcb"),
                output_path=Path("out.kicad_pcb"),
                args=args,
                quiet=True,
            )

        # Every visited tier appears at most once.
        assert len(seen_tiers) == len(set(seen_tiers))
        # And the total never exceeds the ladder length.
        assert len(seen_tiers) <= 3


class TestConvergenceGuard:
    """Issue #2881: skip pure no-op tier swaps (no capability/scalar gain)."""

    def _make_args(self, **overrides):
        base = SimpleNamespace(
            pcb="test.kicad_pcb",
            manufacturer="jlcpcb",
            auto_mfr_tier=True,
            mfr_tier_ladder=None,
            auto_layers=True,
            adaptive_rules=False,
            quiet=True,
            timeout=None,
            _wall_clock_deadline=None,
        )
        for k, v in overrides.items():
            setattr(base, k, v)
        return base

    def test_skips_tier_with_no_gain(self):
        """When the next tier offers no capability/scalar gain, escalation
        is suppressed by the convergence guard."""
        from kicad_tools.cli.route_cmd import route_with_mfr_tier_escalation

        # Custom ladder where stage 2 == stage 1 functionally
        # (both single-tier oshpark -- can't actually happen via registry,
        # but tests the guard logic via explicit ladder).
        args = self._make_args(
            mfr_tier_ladder="oshpark,oshpark",
            manufacturer="oshpark",
        )
        seen_tiers: list[str] = []

        def fake_inner(*, pcb_path, output_path, args, quiet):
            seen_tiers.append(args.manufacturer)
            mock_router = MagicMock()
            mock_router._escape_router = MagicMock()
            mock_router._escape_router.missed_via_in_pad_rescues = 1
            args._last_router = mock_router
            return 2

        with patch(
            "kicad_tools.cli.route_cmd.route_with_layer_escalation",
            side_effect=fake_inner,
        ):
            from pathlib import Path

            route_with_mfr_tier_escalation(
                pcb_path=Path("test.kicad_pcb"),
                output_path=Path("out.kicad_pcb"),
                args=args,
                quiet=True,
            )

        # Only the first tier ran; convergence guard skipped the duplicate.
        assert seen_tiers == ["oshpark"]


class TestTriggerTable:
    """Tests for the MFR_TIER_ESCALATION_TRIGGERS trigger table."""

    def test_pin_access_triggers_escalation(self):
        from kicad_tools.router.failure_analysis import (
            FailureCause,
            should_escalate_mfr_tier,
        )

        assert should_escalate_mfr_tier(FailureCause.PIN_ACCESS) is True

    def test_clearance_triggers_escalation_conditionally(self):
        """CLEARANCE triggers escalation; caller applies the scalar-gain guard."""
        from kicad_tools.router.failure_analysis import (
            FailureCause,
            should_escalate_mfr_tier,
        )

        assert should_escalate_mfr_tier(FailureCause.CLEARANCE) is True

    def test_blocked_path_does_not_trigger(self):
        """BLOCKED_PATH is a placement issue -- escalation cannot help."""
        from kicad_tools.router.failure_analysis import (
            FailureCause,
            should_escalate_mfr_tier,
        )

        assert should_escalate_mfr_tier(FailureCause.BLOCKED_PATH) is False

    def test_congestion_does_not_trigger(self):
        """CONGESTION is a layer issue -- --auto-layers handles it, not tiers."""
        from kicad_tools.router.failure_analysis import (
            FailureCause,
            should_escalate_mfr_tier,
        )

        assert should_escalate_mfr_tier(FailureCause.CONGESTION) is False

    def test_unknown_does_not_trigger(self):
        """UNKNOWN failures are algorithm bugs; escalation would mask them."""
        from kicad_tools.router.failure_analysis import (
            FailureCause,
            should_escalate_mfr_tier,
        )

        assert should_escalate_mfr_tier(FailureCause.UNKNOWN) is False

    @pytest.mark.parametrize(
        "cause_name,expected",
        [
            ("PIN_ACCESS", True),
            ("CLEARANCE", True),
            ("VIA_BLOCKED", True),
            ("BLOCKED_PATH", False),
            ("CONGESTION", False),
            ("LAYER_CONFLICT", False),
            ("KEEPOUT", False),
            ("ROUTING_ORDER", False),
            ("LENGTH_CONSTRAINT", False),
            ("DIFFERENTIAL_PAIR", False),
            ("UNKNOWN", False),
        ],
    )
    def test_trigger_table_full_coverage(self, cause_name, expected):
        """Parametrized check of the full trigger table per Issue #2881."""
        from kicad_tools.router.failure_analysis import (
            FailureCause,
            should_escalate_mfr_tier,
        )

        cause = getattr(FailureCause, cause_name)
        assert should_escalate_mfr_tier(cause) is expected


class TestNameUnfixableConstraint:
    """Tests for the named-constraint diagnostic helper."""

    def test_pin_access_names_via_in_pad(self):
        """PIN_ACCESS on a non-VIP manufacturer names the via_in_pad capability."""
        from kicad_tools.router.failure_analysis import (
            FailureCause,
            name_unfixable_constraint,
        )

        msg = name_unfixable_constraint(
            FailureCause.PIN_ACCESS,
            manufacturer="jlcpcb",
            component_ref="U2",
            pin="7",
        )
        assert "U2 pin 7" in msg
        assert "jlcpcb" in msg
        assert "via-in-pad" in msg.lower() or "via_in_pad" in msg

    def test_clearance_names_clearance(self):
        from kicad_tools.router.failure_analysis import (
            FailureCause,
            name_unfixable_constraint,
        )

        msg = name_unfixable_constraint(
            FailureCause.CLEARANCE,
            manufacturer="jlcpcb",
        )
        assert "clearance" in msg.lower()

    def test_unknown_cause_returns_generic_message(self):
        from kicad_tools.router.failure_analysis import (
            FailureCause,
            name_unfixable_constraint,
        )

        msg = name_unfixable_constraint(FailureCause.UNKNOWN, manufacturer="jlcpcb")
        # Should still produce a non-empty string
        assert isinstance(msg, str) and msg
