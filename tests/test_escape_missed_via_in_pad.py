"""Tests for the missed_via_in_pad_rescues counter on EscapeRouter (Issue #2881).

The counter is the canonical signal that the ``--auto-mfr-tier`` escalation
loop reads to decide whether escalating to a via-in-pad-capable
manufacturer would unblock blocked pins.

Coverage:
- Counter initializes to zero.
- Counter does NOT increment when the manufacturer supports via-in-pad
  (the in-pad rescue path is taken instead).
- Counter accepts ``missed_via_in_pad_components`` set tracking.
"""


class TestMissedViaInPadCounterInit:
    """Basic instantiation tests -- no PCB required."""

    def test_counter_initializes_to_zero(self):
        """A fresh EscapeRouter has zero missed rescues."""
        from kicad_tools.router.escape import EscapeRouter
        from kicad_tools.router.grid import DesignRules, RoutingGrid

        rules = DesignRules(grid_resolution=0.1, trace_width=0.2, trace_clearance=0.15)
        grid = RoutingGrid(width=10.0, height=10.0, rules=rules)
        router = EscapeRouter(grid, rules)

        assert router.missed_via_in_pad_rescues == 0
        assert router.missed_via_in_pad_components == set()

    def test_via_in_pad_unavailable_on_base_jlcpcb(self):
        """Base jlcpcb correctly resolves via_in_pad_supported=False."""
        from kicad_tools.router.escape import EscapeRouter
        from kicad_tools.router.grid import DesignRules, RoutingGrid

        rules = DesignRules(grid_resolution=0.1, trace_width=0.2, trace_clearance=0.15)
        grid = RoutingGrid(width=10.0, height=10.0, rules=rules)
        router = EscapeRouter(grid, rules, manufacturer="jlcpcb")

        assert router.via_in_pad_supported is False
        # Counter should still be zero (no routing attempted yet).
        assert router.missed_via_in_pad_rescues == 0

    def test_via_in_pad_available_on_tier1(self):
        """jlcpcb-tier1 resolves via_in_pad_supported=True on a 4+ layer board.

        Issue #5201: ``jlcpcb-tier1``'s via-in-pad-specific POFV process
        requires >= 4 copper layers, so eligibility now also depends on
        the board's actual layer count -- a 4-layer grid is required here
        (see ``test_via_in_pad_unavailable_on_tier1_at_2_layers`` for the
        2-layer, ineligible case).
        """
        from kicad_tools.router.escape import EscapeRouter
        from kicad_tools.router.grid import DesignRules, RoutingGrid
        from kicad_tools.router.layers import LayerStack

        rules = DesignRules(grid_resolution=0.1, trace_width=0.2, trace_clearance=0.15)
        grid = RoutingGrid(
            width=10.0,
            height=10.0,
            rules=rules,
            layer_stack=LayerStack.four_layer_sig_sig_gnd_pwr(),
        )
        router = EscapeRouter(grid, rules, manufacturer="jlcpcb-tier1")

        assert router.via_in_pad_supported is True

    def test_via_in_pad_unavailable_on_tier1_at_2_layers(self):
        """jlcpcb-tier1 resolves via_in_pad_supported=False on a 2-layer board.

        Issue #5201 (board02 regression): every ``jlcpcb-tier1`` layer
        configuration sets the bare ``via_in_pad_supported`` capability
        flag, but the tier's via-in-pad-specific POFV process itself
        requires >= 4 copper layers -- a 2-layer board has no eligible
        process, so the escape router must fall back to the non-in-pad
        strategy instead of placing an unmanufacturable via.
        """
        from kicad_tools.router.escape import EscapeRouter
        from kicad_tools.router.grid import DesignRules, RoutingGrid

        rules = DesignRules(grid_resolution=0.1, trace_width=0.2, trace_clearance=0.15)
        # RoutingGrid defaults to a 2-layer stack when none is supplied.
        grid = RoutingGrid(width=10.0, height=10.0, rules=rules)
        router = EscapeRouter(grid, rules, manufacturer="jlcpcb-tier1")

        assert router.via_in_pad_supported is False

    def test_via_in_pad_available_on_pcbway(self):
        """pcbway resolves via_in_pad_supported=True (already at tier 1)."""
        from kicad_tools.router.escape import EscapeRouter
        from kicad_tools.router.grid import DesignRules, RoutingGrid

        rules = DesignRules(grid_resolution=0.1, trace_width=0.2, trace_clearance=0.15)
        grid = RoutingGrid(width=10.0, height=10.0, rules=rules)
        router = EscapeRouter(grid, rules, manufacturer="pcbway")

        assert router.via_in_pad_supported is True

    def test_unknown_manufacturer_does_not_crash(self):
        """An unrecognized manufacturer name silently degrades."""
        from kicad_tools.router.escape import EscapeRouter
        from kicad_tools.router.grid import DesignRules, RoutingGrid

        rules = DesignRules(grid_resolution=0.1, trace_width=0.2, trace_clearance=0.15)
        grid = RoutingGrid(width=10.0, height=10.0, rules=rules)
        # Should not raise:
        router = EscapeRouter(grid, rules, manufacturer="not-a-real-mfr")
        assert router.via_in_pad_supported is False
