"""Issue #2891: end-to-end behaviour of the auto-mfr-tier ERROR-log
suppression flag.

Verifies that ``route_with_mfr_tier_escalation``:

1. Sets ``args._auto_mfr_tier_in_progress = True`` for every
   non-final tier attempt (so the inner ``EscapeRouter`` demotes
   the #2880 ERROR to DEBUG -- false alarm; outer wrapper retries).
2. Sets ``args._auto_mfr_tier_in_progress = False`` for the FINAL
   tier attempt (so a fully-exhausted ladder surfaces the ERROR).
3. Clears the flag after the wrapper returns (so reuse of ``args``
   by unrelated callers isn't surprised).
4. Handles the degenerate single-tier ladder edge case correctly:
   that single tier IS the final tier, so the flag must be False
   for it.

These properties are tested at the wrapper-level (stubbing the
inner ``route_with_layer_escalation``) -- the actual log demotion
in ``EscapeRouter`` is covered by
``tests/router/test_escape_qfp_plane_sandwich.py::TestAutoMfrTierLogSuppression``.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


def _make_args(**overrides) -> SimpleNamespace:
    """Build a minimal-but-complete args namespace for the wrapper."""
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


class TestAutoMfrTierEscalationFlagPlumbing:
    """The wrapper must set ``_auto_mfr_tier_in_progress`` so the inner
    ``DesignRules`` construction (in ``route_with_layer_escalation`` /
    related entry points) can forward it onto
    ``DesignRules.auto_mfr_tier_in_progress`` -- which the
    ``EscapeRouter`` reads to gate the #2880 ERROR demotion."""

    def test_non_final_tiers_set_flag_true(self):
        """Walk a two-tier ladder (jlcpcb -> jlcpcb-tier1).  The first
        tier is non-final so its inner call must see the flag set;
        the second tier is final so its inner call must see the flag
        cleared (so a real failure would still surface the ERROR)."""
        from kicad_tools.cli.route_cmd import route_with_mfr_tier_escalation

        args = _make_args()
        seen: list[tuple[str, bool]] = []

        def fake_inner(*, pcb_path, output_path, args, quiet):
            seen.append(
                (args.manufacturer, getattr(args, "_auto_mfr_tier_in_progress", False))
            )
            # Set missed_via_in_pad_rescues so escalation actually fires.
            mock_router = MagicMock()
            mock_router._escape_router = MagicMock()
            mock_router._escape_router.missed_via_in_pad_rescues = 3
            args._last_router = mock_router
            return 2  # always fail to walk the whole ladder

        with patch(
            "kicad_tools.cli.route_cmd.route_with_layer_escalation",
            side_effect=fake_inner,
        ):
            route_with_mfr_tier_escalation(
                pcb_path=Path("test.kicad_pcb"),
                output_path=Path("out.kicad_pcb"),
                args=args,
                quiet=True,
            )

        # Expect: ("jlcpcb", True), ("jlcpcb-tier1", False)
        assert len(seen) == 2, f"Expected both tiers to be attempted; got {seen}"
        assert seen[0] == ("jlcpcb", True), (
            f"First (non-final) tier must run with escalation-in-progress=True; got {seen[0]}"
        )
        assert seen[1] == ("jlcpcb-tier1", False), (
            "Final tier must run with escalation-in-progress=False so the "
            f"#2880 ERROR re-surfaces on ladder exhaustion; got {seen[1]}"
        )

    def test_flag_cleared_after_wrapper_returns(self):
        """Reuse of ``args`` by unrelated callers must not see the
        escalation flag leaking through."""
        from kicad_tools.cli.route_cmd import route_with_mfr_tier_escalation

        args = _make_args()

        def fake_inner(*, pcb_path, output_path, args, quiet):
            mock_router = MagicMock()
            mock_router._escape_router = MagicMock()
            mock_router._escape_router.missed_via_in_pad_rescues = 3
            args._last_router = mock_router
            return 2  # always fail

        with patch(
            "kicad_tools.cli.route_cmd.route_with_layer_escalation",
            side_effect=fake_inner,
        ):
            route_with_mfr_tier_escalation(
                pcb_path=Path("test.kicad_pcb"),
                output_path=Path("out.kicad_pcb"),
                args=args,
                quiet=True,
            )

        assert getattr(args, "_auto_mfr_tier_in_progress", False) is False, (
            "Wrapper must clear the escalation flag on exit so callers that "
            "reuse `args` aren't surprised by log demotion in unrelated paths."
        )

    def test_single_tier_ladder_runs_with_flag_false(self):
        """Degenerate ladder edge case: when the ladder collapses to a
        single tier (e.g. user passes ``--mfr-tier-ladder jlcpcb``), that
        single tier IS the final tier and must run with the flag
        cleared so the #2880 ERROR is NOT suppressed."""
        from kicad_tools.cli.route_cmd import route_with_mfr_tier_escalation

        args = _make_args(
            manufacturer="jlcpcb",
            mfr_tier_ladder="jlcpcb",  # explicit single-element ladder
        )
        seen: list[tuple[str, bool]] = []

        def fake_inner(*, pcb_path, output_path, args, quiet):
            seen.append(
                (args.manufacturer, getattr(args, "_auto_mfr_tier_in_progress", False))
            )
            mock_router = MagicMock()
            mock_router._escape_router = MagicMock()
            mock_router._escape_router.missed_via_in_pad_rescues = 3
            args._last_router = mock_router
            return 2  # failure

        with patch(
            "kicad_tools.cli.route_cmd.route_with_layer_escalation",
            side_effect=fake_inner,
        ):
            route_with_mfr_tier_escalation(
                pcb_path=Path("test.kicad_pcb"),
                output_path=Path("out.kicad_pcb"),
                args=args,
                quiet=True,
            )

        assert seen == [("jlcpcb", False)], (
            "Single-tier ladder: the lone tier IS the final tier and must "
            f"run with escalation-in-progress=False; got {seen}"
        )

    def test_success_on_first_tier_runs_with_flag_appropriately(self):
        """When the starting tier succeeds, no escalation happens.  In a
        two-tier ladder, the first tier is NOT the final tier, so the
        flag is set when its inner call runs.  This is the "best case"
        path: success on tier 1 stops escalation and the suppressed
        diagnostic (if any) was never relevant to the user."""
        from kicad_tools.cli.route_cmd import route_with_mfr_tier_escalation

        args = _make_args()
        seen: list[tuple[str, bool]] = []

        def fake_inner(*, pcb_path, output_path, args, quiet):
            seen.append(
                (args.manufacturer, getattr(args, "_auto_mfr_tier_in_progress", False))
            )
            return 0  # success

        with patch(
            "kicad_tools.cli.route_cmd.route_with_layer_escalation",
            side_effect=fake_inner,
        ):
            rc = route_with_mfr_tier_escalation(
                pcb_path=Path("test.kicad_pcb"),
                output_path=Path("out.kicad_pcb"),
                args=args,
                quiet=True,
            )

        assert rc == 0
        # Only one inner call; it was the first tier (non-final), so the
        # flag was set when the call ran.  The fact that it succeeded
        # means the demotion was harmless.
        assert seen == [("jlcpcb", True)], (
            f"Expected one call with flag=True on non-final tier; got {seen}"
        )
        # Flag must be cleared on exit even on the success path.
        assert getattr(args, "_auto_mfr_tier_in_progress", False) is False
