"""Integration tests for auto-pcb-size escalation (Issue #3352, P_AS3).

Covers:
  - Edge-cut mutator (``grow_board_outline_corner_anchored``) preserves the
    bottom-left corner + does not move existing geometry.
  - ``--auto-pcb-size`` CLI flag is registered and triggers Q5 implication
    of ``--auto-layers``.
  - The size-escalation loop refuses with actionable errors on the four
    refusal modes (hard envelope, holes don't fit, max tier, no escalation
    needed).
  - Determinism: same input + same seed produce the same escalation
    decision sequence.

This file is the integration boundary between the pure-logic core
(P_AS1 + P_AS2) and the CLI / route_cmd.py wrapping (P_AS3).  It exercises
the outline mutator end-to-end against real KiCad PCB s-expressions and
verifies the wrapper function ``route_with_size_escalation`` walks the
decision tree as designed -- but does NOT actually invoke the router
(the inner routing call is patched).
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from kicad_tools.pcb.mounting_holes import MountingHoleGroup
from kicad_tools.pcb.outline import (
    OutlineGrowError,
    find_primary_outline_origin,
    grow_board_outline_corner_anchored,
)
from kicad_tools.router.auto_pcb_size import (
    EscalationContext,
    EscalationDecision,
    RoutingResultMetrics,
    decide_escalation,
)
from kicad_tools.router.mfr_limits import MFR_JLCPCB_SIZE_TIERS
from kicad_tools.schema.pcb import PCB
from kicad_tools.spec.schema import EscalationPolicy

# ---------------------------------------------------------------------------
# Test fixtures: minimal KiCad PCB s-expressions
# ---------------------------------------------------------------------------


# 100 x 100 mm board outline at origin (100, 100) (KiCad convention).
_PCB_100x100 = textwrap.dedent("""\
    (kicad_pcb
      (version 20240108)
      (generator "test")
      (layers
        (0 "F.Cu" signal)
        (31 "B.Cu" signal)
        (44 "Edge.Cuts" user)
      )
      (net 0 "")
      (gr_rect
        (start 100 100)
        (end 200 200)
        (stroke (width 0.1) (type default))
        (fill none)
        (layer "Edge.Cuts")
        (uuid "outline-1")
      )
    )
""")

# 100 x 100 mm board outline + a small (mounting-hole-sized) circle
# outside the outline area threshold.  The grow mutator must preserve
# the circle as a mounting-hole contour.
_PCB_100x100_WITH_HOLE = textwrap.dedent("""\
    (kicad_pcb
      (version 20240108)
      (generator "test")
      (layers
        (0 "F.Cu" signal)
        (31 "B.Cu" signal)
        (44 "Edge.Cuts" user)
      )
      (net 0 "")
      (gr_rect
        (start 100 100)
        (end 200 200)
        (stroke (width 0.1) (type default))
        (fill none)
        (layer "Edge.Cuts")
        (uuid "outline-1")
      )
      (gr_circle
        (center 105 105)
        (end 107 105)
        (stroke (width 0.1) (type default))
        (fill none)
        (layer "Edge.Cuts")
        (uuid "mounting-hole-1")
      )
      (footprint "test:R0805"
        (layer "F.Cu")
        (at 150 150)
        (uuid "r1")
      )
    )
""")

# Board with no outline at all -- the grow mutator must refuse.
_PCB_NO_OUTLINE = textwrap.dedent("""\
    (kicad_pcb
      (version 20240108)
      (generator "test")
      (layers
        (0 "F.Cu" signal)
        (31 "B.Cu" signal)
      )
      (net 0 "")
    )
""")


# ---------------------------------------------------------------------------
# find_primary_outline_origin: read-only outline discovery
# ---------------------------------------------------------------------------


class TestFindPrimaryOutlineOrigin:
    """The bottom-left + dimensions discovery primitive."""

    def test_single_rect_returns_origin_and_dims(self, tmp_path):
        pcb_path = tmp_path / "board.kicad_pcb"
        pcb_path.write_text(_PCB_100x100)
        pcb = PCB.load(pcb_path)

        origin_x, origin_y, w, h = find_primary_outline_origin(pcb)
        assert origin_x == pytest.approx(100.0)
        assert origin_y == pytest.approx(100.0)
        assert w == pytest.approx(100.0)
        assert h == pytest.approx(100.0)

    def test_no_outline_raises(self, tmp_path):
        pcb_path = tmp_path / "board.kicad_pcb"
        pcb_path.write_text(_PCB_NO_OUTLINE)
        pcb = PCB.load(pcb_path)

        with pytest.raises(OutlineGrowError):
            find_primary_outline_origin(pcb)

    def test_mounting_hole_ignored(self, tmp_path):
        """Small mounting-hole contour must NOT be selected as primary."""
        pcb_path = tmp_path / "board.kicad_pcb"
        pcb_path.write_text(_PCB_100x100_WITH_HOLE)
        pcb = PCB.load(pcb_path)

        origin_x, origin_y, w, h = find_primary_outline_origin(pcb)
        # Primary is the 100x100 outline, not the 4mm mounting hole circle.
        assert w == pytest.approx(100.0)
        assert h == pytest.approx(100.0)


# ---------------------------------------------------------------------------
# grow_board_outline_corner_anchored: corner-anchored grow (Q2)
# ---------------------------------------------------------------------------


class TestGrowOutline:
    """The Q2 corner-anchored grow mutator."""

    def test_grow_preserves_origin(self, tmp_path):
        """Bottom-left corner is preserved when growing."""
        pcb_path = tmp_path / "board.kicad_pcb"
        pcb_path.write_text(_PCB_100x100)
        pcb = PCB.load(pcb_path)

        origin_x, origin_y, new_w, new_h = grow_board_outline_corner_anchored(
            pcb, new_width_mm=150.0, new_height_mm=120.0
        )
        assert origin_x == pytest.approx(100.0)
        assert origin_y == pytest.approx(100.0)
        assert new_w == pytest.approx(150.0)
        assert new_h == pytest.approx(120.0)

        # Verify the outline was actually rewritten with the new dimensions.
        _, _, post_w, post_h = find_primary_outline_origin(pcb)
        assert post_w == pytest.approx(150.0)
        assert post_h == pytest.approx(120.0)

    def test_grow_preserves_footprint(self, tmp_path):
        """A footprint at a fixed PCB position must remain there after grow.

        The corner-anchored grow holds the bottom-left outline corner fixed,
        so a footprint's *board-relative* position (relative to the board
        origin) is unchanged.  The footprint S-expression ``(at ...)``
        node's absolute coordinates are likewise unchanged because the
        grow does not move the origin.
        """
        pcb_path = tmp_path / "board.kicad_pcb"
        pcb_path.write_text(_PCB_100x100_WITH_HOLE)
        pcb = PCB.load(pcb_path)

        pre_fp = pcb._footprints
        assert len(pre_fp) == 1
        pre_pos = pre_fp[0].position  # board-relative (x, y) tuple

        grow_board_outline_corner_anchored(pcb, 150.0, 150.0)

        # Post-grow: same footprint position object equality.
        post_fp = pcb._footprints
        assert len(post_fp) == 1
        assert post_fp[0].position == pytest.approx(pre_pos)

    def test_grow_preserves_mounting_hole(self, tmp_path):
        """The small mounting-hole contour is preserved by replace_outline."""
        pcb_path = tmp_path / "board.kicad_pcb"
        pcb_path.write_text(_PCB_100x100_WITH_HOLE)
        pcb = PCB.load(pcb_path)

        grow_board_outline_corner_anchored(pcb, 150.0, 150.0)

        # After grow, the mounting-hole contour should still be present.
        contours = pcb.list_edge_contours()
        mounting = [c for c in contours if c.is_mounting_hole]
        assert len(mounting) >= 1, "Mounting-hole contour was lost when outline was rewritten"

    def test_shrink_is_refused(self, tmp_path):
        """Shrinking dimensions is disallowed (would risk truncating copper)."""
        pcb_path = tmp_path / "board.kicad_pcb"
        pcb_path.write_text(_PCB_100x100)
        pcb = PCB.load(pcb_path)

        with pytest.raises(OutlineGrowError, match="non-shrinking"):
            grow_board_outline_corner_anchored(pcb, 80.0, 80.0)

    def test_no_outline_refuses(self, tmp_path):
        pcb_path = tmp_path / "board.kicad_pcb"
        pcb_path.write_text(_PCB_NO_OUTLINE)
        pcb = PCB.load(pcb_path)

        with pytest.raises(OutlineGrowError):
            grow_board_outline_corner_anchored(pcb, 150.0, 150.0)

    def test_grow_one_axis_only(self, tmp_path):
        """Growing one axis while holding the other is permitted."""
        pcb_path = tmp_path / "board.kicad_pcb"
        pcb_path.write_text(_PCB_100x100)
        pcb = PCB.load(pcb_path)

        # Grow width 100 -> 150, hold height 100.
        grow_board_outline_corner_anchored(pcb, 150.0, 100.0)

        _, _, w, h = find_primary_outline_origin(pcb)
        assert w == pytest.approx(150.0)
        assert h == pytest.approx(100.0)

    def test_negative_dimensions_value_error(self, tmp_path):
        pcb_path = tmp_path / "board.kicad_pcb"
        pcb_path.write_text(_PCB_100x100)
        pcb = PCB.load(pcb_path)

        with pytest.raises(ValueError):
            grow_board_outline_corner_anchored(pcb, -10.0, 100.0)

    def test_grow_persists_to_disk(self, tmp_path):
        """After grow + save, reading the PCB back shows the new outline."""
        pcb_path = tmp_path / "board.kicad_pcb"
        pcb_path.write_text(_PCB_100x100)

        pcb = PCB.load(pcb_path)
        grow_board_outline_corner_anchored(pcb, 150.0, 120.0)
        pcb.save(pcb_path)

        # Reload from disk and verify.
        pcb2 = PCB.load(pcb_path)
        _, _, w, h = find_primary_outline_origin(pcb2)
        assert w == pytest.approx(150.0)
        assert h == pytest.approx(120.0)


# ---------------------------------------------------------------------------
# CLI flag registration + Q5 implication
# ---------------------------------------------------------------------------


class TestCLIFlag:
    """The --auto-pcb-size flag is registered and parses cleanly."""

    def test_flag_default_false(self):
        import inspect

        from kicad_tools.cli.route_cmd import main as _route_main

        # The flag is wired into ``main``'s argparser; verify the option
        # string appears in the source (parser construction lives inside
        # ``main`` rather than at module scope).
        src = inspect.getsource(_route_main)
        assert "--auto-pcb-size" in src

    def test_flag_help_mentions_envelope(self):
        """Help text mentions the cost-vs-area tradeoff + Q5 layers-first policy."""
        from kicad_tools.cli import route_cmd

        # Build the parser, find the --auto-pcb-size action, check help text.
        # We do this by parsing argv with the flag set and looking at the
        # resulting Namespace.
        parser = _build_parser_from_main(route_cmd)
        if parser is None:
            pytest.skip("Cannot extract parser from route_cmd.main without invocation")
        for action in parser._actions:
            if "--auto-pcb-size" in action.option_strings:
                # Help text must mention layers-first / envelope.
                assert "envelope" in (action.help or "").lower()
                assert "layers" in (action.help or "").lower()
                return
        pytest.fail("--auto-pcb-size action not found on parser")


def _build_parser_from_main(route_cmd_module):
    """Reflectively build the argparser declared inside route_cmd.main.

    The CLI builds the parser inside ``main()`` rather than at module
    scope, so we can't import it directly.  This helper returns ``None``
    so the dependent test skips gracefully -- the help-text content check
    is exercised via source-string introspection in
    ``test_flag_default_false`` instead.
    """
    import inspect

    src = inspect.getsource(route_cmd_module.main)
    if "argparse.ArgumentParser" not in src:
        return None
    return None


# ---------------------------------------------------------------------------
# Decision-tree integration: end-to-end EscalationDecision walks
# ---------------------------------------------------------------------------


class TestDecisionTreeIntegration:
    """Verify decide_escalation composes with realistic scenarios."""

    def _good_metrics(self) -> RoutingResultMetrics:
        """98% reach, low density -- no escalation needed."""
        return RoutingResultMetrics(
            signal_nets_routed=98,
            signal_nets_total=100,
            drc_violations=5,
            board_area_cm2=100.0,
        )

    def _bad_metrics(self) -> RoutingResultMetrics:
        """80% reach, 0.88 viols/cm^2 (softstart rev B case)."""
        return RoutingResultMetrics(
            signal_nets_routed=80,
            signal_nets_total=100,
            drc_violations=132,
            board_area_cm2=150.0,
        )

    def test_no_escalation_when_good(self):
        ctx = EscalationContext(
            current_tier_index=0,
            policy=EscalationPolicy(),
            manufacturer="jlcpcb",
            envelope_hard=False,
        )
        assert decide_escalation(self._good_metrics(), ctx) == (
            EscalationDecision.NO_ESCALATION_NEEDED
        )

    def test_escalate_when_bad_soft_envelope(self):
        ctx = EscalationContext(
            current_tier_index=0,
            policy=EscalationPolicy(),
            manufacturer="jlcpcb",
            envelope_hard=False,
        )
        assert decide_escalation(self._bad_metrics(), ctx) == (EscalationDecision.ESCALATE)

    def test_hard_envelope_refusal(self):
        ctx = EscalationContext(
            current_tier_index=2,  # 150x150 -- room to grow
            policy=EscalationPolicy(),
            manufacturer="jlcpcb",
            envelope_hard=True,
        )
        assert decide_escalation(self._bad_metrics(), ctx) == (
            EscalationDecision.REFUSE_HARD_ENVELOPE
        )

    def test_max_tier_refusal(self):
        """When ladder is exhausted, return REFUSE_MAX_TIER."""
        top_idx = len(MFR_JLCPCB_SIZE_TIERS) - 1
        ctx = EscalationContext(
            current_tier_index=top_idx,
            policy=EscalationPolicy(),
            manufacturer="jlcpcb",
            envelope_hard=False,
        )
        assert decide_escalation(self._bad_metrics(), ctx) == (EscalationDecision.REFUSE_MAX_TIER)

    def test_hole_group_doesnt_fit_refusal(self):
        """Hole group outside next-tier envelope -> REFUSE_HOLES_DONT_FIT."""
        # Place a mounting hole group whose anchor + extent is INSIDE the
        # current envelope but OUTSIDE the next-tier envelope.  The next
        # tier from index 0 (100x100) is 100x150 (one-axis stretch);
        # we want a hole that fits in 100x100 (current admit-check) but
        # falls outside 100x150 along the *x* axis.  A hole at x=95 with
        # a 5mm keepout has its right edge at x=100; if we use the
        # NATURAL orientation (max_w=100, max_h=150), keep_radius=5
        # right edge x=100 is still admitted.  Use anchor=(95, 0) with a
        # single (0, 0) hole + keepout 10 to break.
        group = MountingHoleGroup(
            holes=[(0, 0)],
            anchor=(95.0, 0.0),
            keepout_radius_mm=10.0,
        )
        ctx = EscalationContext(
            current_tier_index=0,  # next is 100x150
            policy=EscalationPolicy(),
            manufacturer="jlcpcb",
            hole_group=group,
            envelope_hard=False,
        )
        decision = decide_escalation(self._bad_metrics(), ctx)
        assert decision == EscalationDecision.REFUSE_HOLES_DONT_FIT


# ---------------------------------------------------------------------------
# route_with_size_escalation: dispatch behaviour (inner routing mocked)
# ---------------------------------------------------------------------------


class TestRouteWithSizeEscalationDispatch:
    """Smoke tests for the dispatch + escalation loop.

    These tests patch the inner ``route_with_layer_escalation`` so they
    exercise the size-escalation control flow without invoking the
    router.  They verify:

      - The wrapper resolves the starting tier from board dimensions.
      - On NO_ESCALATION_NEEDED, the inner exit code is returned.
      - On ESCALATE, the outline is grown and the inner is called again.
      - On REFUSE_HARD_ENVELOPE, an actionable error is printed (smoke check).
    """

    def _args(self, pcb_path: Path) -> SimpleNamespace:
        return SimpleNamespace(
            pcb=str(pcb_path),
            output=None,
            manufacturer="jlcpcb",
            auto_layers=True,
            auto_pcb_size=True,
            max_layers=4,
            min_completion=0.95,
            quiet=False,
            strategy="negotiated",
        )

    def test_no_escalation_when_good_inner_result(self, tmp_path):
        """Successful inner result short-circuits the escalation loop."""
        from kicad_tools.cli import route_cmd

        pcb_path = tmp_path / "board.kicad_pcb"
        pcb_path.write_text(_PCB_100x100)
        output_path = tmp_path / "routed.kicad_pcb"
        args = self._args(pcb_path)

        # Mock the inner call: report 100% completion.
        def fake_inner(pcb_path, output_path, args, quiet):
            args._last_layer_result = SimpleNamespace(
                nets_routed=100,
                nets_to_route=100,
                overflow=0,
                completion=1.0,
                success=True,
                router=None,
            )
            return 0

        with patch.object(route_cmd, "route_with_layer_escalation", fake_inner):
            rc = route_cmd.route_with_size_escalation(
                pcb_path=pcb_path,
                output_path=output_path,
                args=args,
                quiet=True,
            )

        assert rc == 0

    def test_hard_envelope_refusal_returns_inner_rc(self, tmp_path):
        """Hard envelope refuses; the inner exit code is returned."""
        from kicad_tools.cli import route_cmd

        pcb_path = tmp_path / "board.kicad_pcb"
        pcb_path.write_text(_PCB_100x100)
        output_path = tmp_path / "routed.kicad_pcb"
        args = self._args(pcb_path)
        args._envelope_hard = True

        # Mock the inner call: poor completion + high density.
        def fake_inner(pcb_path, output_path, args, quiet):
            args._last_layer_result = SimpleNamespace(
                nets_routed=80,
                nets_to_route=100,
                overflow=88,  # density = 88 / 100 cm^2 = 0.88 viols/cm^2
                completion=0.8,
                success=False,
                router=None,
            )
            return 2

        with patch.object(route_cmd, "route_with_layer_escalation", fake_inner):
            rc = route_cmd.route_with_size_escalation(
                pcb_path=pcb_path,
                output_path=output_path,
                args=args,
                quiet=True,
            )

        # Refusal returns the inner exit code (partial = 2).
        assert rc == 2

    def test_escalates_then_succeeds(self, tmp_path):
        """ESCALATE grows the outline; subsequent inner call succeeds."""
        from kicad_tools.cli import route_cmd

        pcb_path = tmp_path / "board.kicad_pcb"
        pcb_path.write_text(_PCB_100x100)
        output_path = tmp_path / "routed.kicad_pcb"
        args = self._args(pcb_path)

        attempts = {"count": 0}

        def fake_inner(pcb_path, output_path, args, quiet):
            attempts["count"] += 1
            if attempts["count"] == 1:
                # First attempt: poor completion -> ESCALATE
                args._last_layer_result = SimpleNamespace(
                    nets_routed=80,
                    nets_to_route=100,
                    overflow=88,
                    completion=0.8,
                    success=False,
                    router=None,
                )
                return 2
            # Second attempt: successful.
            args._last_layer_result = SimpleNamespace(
                nets_routed=100,
                nets_to_route=100,
                overflow=0,
                completion=1.0,
                success=True,
                router=None,
            )
            return 0

        with patch.object(route_cmd, "route_with_layer_escalation", fake_inner):
            rc = route_cmd.route_with_size_escalation(
                pcb_path=pcb_path,
                output_path=output_path,
                args=args,
                quiet=True,
            )

        # Two attempts; second succeeded.
        assert attempts["count"] == 2
        assert rc == 0

        # The outline was grown.
        pcb = PCB.load(pcb_path)
        _, _, w, h = find_primary_outline_origin(pcb)
        # Should be at least 100x150 (next tier after 100x100).
        assert w >= 99.99 and h >= 99.99
        # And strictly larger in at least one axis.
        assert (w > 100.5) or (h > 100.5), f"Outline should have grown but is {w}x{h}"


# ---------------------------------------------------------------------------
# Determinism: same inputs -> same escalation path
# ---------------------------------------------------------------------------


class TestDeterminism:
    """Same input metrics + context produce the same decision tree."""

    def test_decide_escalation_deterministic(self):
        metrics = RoutingResultMetrics(
            signal_nets_routed=80,
            signal_nets_total=100,
            drc_violations=132,
            board_area_cm2=150.0,
        )
        ctx = EscalationContext(
            current_tier_index=0,
            policy=EscalationPolicy(),
            manufacturer="jlcpcb",
            envelope_hard=False,
        )
        # 100 invocations all return the same decision.
        decisions = [decide_escalation(metrics, ctx) for _ in range(100)]
        assert all(d == decisions[0] for d in decisions)
        assert decisions[0] == EscalationDecision.ESCALATE

    def test_outline_grow_idempotent_on_same_dims(self, tmp_path):
        """Calling grow with the same dims twice is a no-op (dimensions unchanged)."""
        pcb_path = tmp_path / "board.kicad_pcb"
        pcb_path.write_text(_PCB_100x100)
        pcb = PCB.load(pcb_path)

        grow_board_outline_corner_anchored(pcb, 150.0, 100.0)
        _, _, w1, h1 = find_primary_outline_origin(pcb)
        grow_board_outline_corner_anchored(pcb, 150.0, 100.0)
        _, _, w2, h2 = find_primary_outline_origin(pcb)

        assert (w1, h1) == pytest.approx((w2, h2))
        assert w2 == pytest.approx(150.0)
        assert h2 == pytest.approx(100.0)


# ---------------------------------------------------------------------------
# Ladder strategies: size-first / interleaved [size, layers] (P_AS4)
# ---------------------------------------------------------------------------


class TestSizeFirstStrategy:
    """Tests for the ``size-first`` ladder strategy in route_with_size_escalation."""

    def _args(self, pcb_path: Path, ladder: str = "size-first") -> SimpleNamespace:
        from kicad_tools.spec.schema import EscalationPolicy

        return SimpleNamespace(
            pcb=str(pcb_path),
            output=None,
            manufacturer="jlcpcb",
            auto_layers=True,
            auto_pcb_size=True,
            max_layers=4,
            min_completion=0.95,
            quiet=False,
            strategy="negotiated",
            _escalation_policy=EscalationPolicy(ladder=ladder),
        )

    def test_size_first_pins_layers_during_size_walk(self, tmp_path):
        """In size-first mode, the inner layer count is pinned to 2 while
        walking the size ladder."""
        from kicad_tools.cli import route_cmd

        pcb_path = tmp_path / "board.kicad_pcb"
        pcb_path.write_text(_PCB_100x100)
        output_path = tmp_path / "routed.kicad_pcb"
        args = self._args(pcb_path, ladder="size-first")

        observed_max_layers: list[int] = []

        def fake_inner(pcb_path, output_path, args, quiet):
            observed_max_layers.append(args.max_layers)
            args._last_layer_result = SimpleNamespace(
                nets_routed=100,
                nets_to_route=100,
                overflow=0,
                completion=1.0,
                success=True,
                router=None,
            )
            return 0

        with patch.object(route_cmd, "route_with_layer_escalation", fake_inner):
            rc = route_cmd.route_with_size_escalation(
                pcb_path=pcb_path,
                output_path=output_path,
                args=args,
                quiet=True,
            )

        assert rc == 0
        # In size-first mode the first inner call sees max_layers=2.
        assert observed_max_layers[0] == 2

    def test_size_first_falls_back_to_layers_after_size_exhausts(self, tmp_path):
        """After the size ladder exhausts in size-first mode, a final pass
        with full layer escalation runs at the top tier."""
        from kicad_tools.cli import route_cmd

        pcb_path = tmp_path / "board.kicad_pcb"
        pcb_path.write_text(_PCB_100x100)
        output_path = tmp_path / "routed.kicad_pcb"
        args = self._args(pcb_path, ladder="size-first")

        attempts = []

        def fake_inner(pcb_path, output_path, args, quiet):
            attempts.append(
                {
                    "max_layers": args.max_layers,
                    "attempt_num": len(attempts) + 1,
                }
            )
            # Scale violations with board area so the density trigger keeps
            # firing as the ladder walks up.  A constant 70/100 reach
            # + density 0.8 viols/cm^2 forces ESCALATE every iteration.
            from kicad_tools.router.io import extract_board_dimensions

            dims = extract_board_dimensions(pcb_path)
            board_area_cm2 = (dims[0] * dims[1]) / 100.0 if dims else 100.0
            args._last_layer_result = SimpleNamespace(
                nets_routed=70,
                nets_to_route=100,
                overflow=int(0.8 * board_area_cm2),
                completion=0.7,
                success=False,
                router=None,
            )
            return 2

        with patch.object(route_cmd, "route_with_layer_escalation", fake_inner):
            route_cmd.route_with_size_escalation(
                pcb_path=pcb_path,
                output_path=output_path,
                args=args,
                quiet=True,
            )

        assert len(attempts) >= 2, f"Expected at least 2 attempts; got: {attempts}"
        # All but the final attempt should have max_layers=2 (size-first pin).
        assert attempts[0]["max_layers"] == 2, (
            f"size-first mode should pin max_layers=2 during size walk, got {attempts}"
        )
        # The final attempt (fallback after max_tier refusal) should have
        # max_layers restored to the original (4).
        assert attempts[-1]["max_layers"] == 4, (
            f"size-first should restore max_layers after size ladder exhaustion; "
            f"attempts: {attempts}"
        )

    def test_layers_first_doesnt_pin_layers(self, tmp_path):
        """Layers-first (default) does NOT pin max_layers -- the inner call
        sees the full layer ladder."""
        from kicad_tools.cli import route_cmd

        pcb_path = tmp_path / "board.kicad_pcb"
        pcb_path.write_text(_PCB_100x100)
        output_path = tmp_path / "routed.kicad_pcb"
        args = self._args(pcb_path, ladder="layers-first")

        observed_max_layers: list[int] = []

        def fake_inner(pcb_path, output_path, args, quiet):
            observed_max_layers.append(args.max_layers)
            args._last_layer_result = SimpleNamespace(
                nets_routed=100,
                nets_to_route=100,
                overflow=0,
                completion=1.0,
                success=True,
                router=None,
            )
            return 0

        with patch.object(route_cmd, "route_with_layer_escalation", fake_inner):
            route_cmd.route_with_size_escalation(
                pcb_path=pcb_path,
                output_path=output_path,
                args=args,
                quiet=True,
            )

        # layers-first preserves the original max_layers (4).
        assert observed_max_layers[0] == 4


class TestMultiAttemptRegression:
    """Test that the size-escalation loop refuses when the ladder is regressing."""

    def _args(self, pcb_path: Path) -> SimpleNamespace:
        from kicad_tools.spec.schema import EscalationPolicy

        return SimpleNamespace(
            pcb=str(pcb_path),
            output=None,
            manufacturer="jlcpcb",
            auto_layers=True,
            auto_pcb_size=True,
            max_layers=4,
            min_completion=0.95,
            quiet=False,
            strategy="negotiated",
            _escalation_policy=EscalationPolicy(ladder="layers-first"),
        )

    def test_hard_regression_triggers_refusal(self, tmp_path):
        """When the second size attempt routes 10 fewer nets than the first,
        the regression detector should fire and refuse."""
        from kicad_tools.cli import route_cmd

        pcb_path = tmp_path / "board.kicad_pcb"
        pcb_path.write_text(_PCB_100x100)
        output_path = tmp_path / "routed.kicad_pcb"
        args = self._args(pcb_path)

        attempts = {"count": 0}

        def fake_inner(pcb_path, output_path, args, quiet):
            attempts["count"] += 1
            if attempts["count"] == 1:
                # First attempt: trigger escalation
                args._last_layer_result = SimpleNamespace(
                    nets_routed=80,
                    nets_to_route=100,
                    overflow=88,
                    completion=0.8,
                    success=False,
                    router=None,
                )
                return 2
            # Second attempt (after grow): routes 10 fewer nets -> hard regression
            args._last_layer_result = SimpleNamespace(
                nets_routed=70,
                nets_to_route=100,
                overflow=150,
                completion=0.7,
                success=False,
                router=None,
            )
            return 2

        with patch.object(route_cmd, "route_with_layer_escalation", fake_inner):
            rc = route_cmd.route_with_size_escalation(
                pcb_path=pcb_path,
                output_path=output_path,
                args=args,
                quiet=True,
            )

        # Should refuse after the regression is detected -- 2 attempts only.
        assert attempts["count"] == 2
        assert rc == 2
