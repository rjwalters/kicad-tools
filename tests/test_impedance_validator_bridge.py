"""Tests for the validator-regex -> router NetClass-target bridge (Issue #2964).

The validator's :class:`ImpedanceRule._get_default_specs` auto-applies
50Ω to any net matching ``.*CLK.*`` (and several other patterns).  The
router's :func:`_resolve_impedance_for_net_classes` only engages when a
:class:`NetClassRouting` has an explicit ``target_*_impedance``.  Before
the fix, these two subsystems were not connected: validator defaults
stayed invisible to the router, so nets like ``SWCLK`` routed at the
literal 0.2 mm width and later failed ImpedanceRule at DRC time.

These tests assert the bridge fires (synthesizes a NetClass target on
the router's net_class_map) and that the resolver then rewrites the
trace_width to the impedance-driven value.  Test must FAIL on main and
pass on the PR (per AC2 of #2964).
"""

from __future__ import annotations

import logging

import pytest

from kicad_tools.physics import Stackup
from kicad_tools.router.core import Autorouter
from kicad_tools.router.layers import LayerStack
from kicad_tools.router.rules import (
    NET_CLASS_DEBUG,
    DEFAULT_NET_CLASS_MAP,
    DesignRules,
    NetClassRouting,
)


# ---------------------------------------------------------------------------
# Primary bridge gate (must fail on main, pass on PR)
# ---------------------------------------------------------------------------


class TestValidatorRegexBridge:
    """The synthesizer reads validator regex defaults and writes
    ``target_single_impedance`` / ``target_diff_impedance`` onto the
    matched nets' :class:`NetClassRouting` before the resolver runs.
    """

    def _make_autorouter_4l_with_swclk(self) -> Autorouter:
        """Build a 4-layer Autorouter with SWCLK registered as a net.

        SWCLK is matched by the validator's ``.*CLK.*`` -> 50Ω default.
        Before the fix, the router has no notion of this target and
        keeps the NET_CLASS_DEBUG literal width (0.2 mm).  After the
        fix, the synthesizer writes ``target_single_impedance=50`` onto
        a cloned class and the resolver rewrites trace_width to the
        physics-driven value (~0.375 mm on JLCPCB 4L F.Cu).
        """
        rules = DesignRules(manufacturer="jlcpcb")
        stackup = Stackup.jlcpcb_4layer()
        layer_stack = LayerStack.four_layer_sig_gnd_pwr_sig()

        # Use DEFAULT_NET_CLASS_MAP-style mapping: SWCLK -> NET_CLASS_DEBUG.
        # This matches what real boards (board 04) end up with -- they
        # do not declare an explicit impedance target.
        net_class_map = {"SWCLK": NET_CLASS_DEBUG}

        ar = Autorouter(
            width=20.0,
            height=20.0,
            rules=rules,
            stackup=stackup,
            layer_stack=layer_stack,
            net_class_map=net_class_map,
        )
        ar.nets[1] = [("U1", "37")]
        ar.net_names[1] = "SWCLK"
        return ar

    def test_swclk_on_4l_gets_50ohm_target_synthesized(self):
        """SWCLK on a 4L stackup with no explicit target gets
        ``target_single_impedance=50`` synthesized from the validator
        regex default ``.*CLK.*``.

        This is the bridge gate -- the assertion that fails on main
        because the bridge does not exist.
        """
        ar = self._make_autorouter_4l_with_swclk()

        # Sanity: before _prepare_routing, no target is set.
        assert ar.net_class_map["SWCLK"].target_single_impedance is None
        assert ar.net_class_map["SWCLK"].target_diff_impedance is None

        ar._prepare_routing()

        # After: the synthesizer wrote target_single_impedance=50 onto
        # a cloned NetClassRouting for SWCLK.  Without the bridge this
        # field would still be None (the failure mode the issue describes).
        resolved_nc = ar.net_class_map["SWCLK"]
        assert resolved_nc.target_single_impedance == 50.0, (
            "Bridge FAILED: SWCLK did not receive the validator's "
            "regex default target_single_impedance=50.0.  See Issue #2964."
        )

    def test_swclk_on_4l_gets_impedance_driven_width(self):
        """After the bridge fires, the resolver rewrites SWCLK's
        trace_width from the 0.2 mm literal to the impedance-driven
        ~0.375 mm value.  This is the load-bearing user-visible effect.
        """
        ar = self._make_autorouter_4l_with_swclk()

        # Literal at start.
        assert ar.net_class_map["SWCLK"].trace_width == NET_CLASS_DEBUG.trace_width
        assert NET_CLASS_DEBUG.trace_width == 0.2

        ar._prepare_routing()

        resolved_width = ar.net_class_map["SWCLK"].trace_width
        assert resolved_width > 0.25, (
            f"Bridge FAILED: SWCLK trace_width={resolved_width:.3f}mm is "
            f"still near the 0.2mm literal.  The resolver should have "
            f"rewritten it to the 50Ω impedance-driven width "
            f"(~0.375mm on JLCPCB 4L F.Cu).  See Issue #2964."
        )

    def test_swclk_synthesis_logs_at_info_level(self, caplog):
        """AC3: the bridge logs an INFO-level message so users can see
        the synthesized target."""
        ar = self._make_autorouter_4l_with_swclk()

        with caplog.at_level(logging.INFO, logger="kicad_tools.router.core"):
            ar._prepare_routing()

        log_text = "\n".join(rec.message for rec in caplog.records)
        assert "SWCLK" in log_text, (
            f"Expected INFO log mentioning SWCLK after synthesis, got:\n{log_text}"
        )
        assert "50" in log_text, (
            f"Expected INFO log mentioning 50Ω target, got:\n{log_text}"
        )


# ---------------------------------------------------------------------------
# Gate negation: bridge must be a no-op when it should not fire.
# ---------------------------------------------------------------------------


class TestValidatorRegexBridgeGating:
    """The bridge mirrors :meth:`ImpedanceRule._board_has_controlled_impedance`
    and only fires when the board opts into controlled-impedance routing.
    AC5: boards without ``*CLK*`` nets, with explicit NetClass, or 2L
    must see no behavior change.
    """

    def test_no_clk_net_no_synthesis(self):
        """A board without any CLK / MCLK / ETH / etc. nets is a no-op:
        the synthesizer touches nothing."""
        rules = DesignRules(manufacturer="jlcpcb")
        stackup = Stackup.jlcpcb_4layer()
        layer_stack = LayerStack.four_layer_sig_gnd_pwr_sig()

        plain_class = NetClassRouting(
            name="Plain",
            trace_width=0.2,
            clearance=0.15,
        )
        ar = Autorouter(
            width=20.0,
            height=20.0,
            rules=rules,
            stackup=stackup,
            layer_stack=layer_stack,
            net_class_map={"DATA_LINE_5V": plain_class},
        )
        ar.nets[1] = [("R1", "1")]
        ar.net_names[1] = "DATA_LINE_5V"

        ar._prepare_routing()

        # No target should be synthesized -- the net name does not
        # match any validator default regex.
        assert ar.net_class_map["DATA_LINE_5V"].target_single_impedance is None
        assert ar.net_class_map["DATA_LINE_5V"].target_diff_impedance is None
        # Width unchanged.
        assert ar.net_class_map["DATA_LINE_5V"].trace_width == 0.2

    def test_2l_stackup_no_synthesis(self):
        """On 2L without explicit stackup data, the bridge must NOT
        fire (AC4: graceful 2L non-regression).  This mirrors the
        validator's :meth:`ImpedanceRule._board_has_controlled_impedance`
        suppression."""
        rules = DesignRules(manufacturer="jlcpcb")
        stackup = Stackup.default_2layer()  # no explicit data, 2 copper layers
        layer_stack = LayerStack.two_layer()

        ar = Autorouter(
            width=20.0,
            height=20.0,
            rules=rules,
            stackup=stackup,
            layer_stack=layer_stack,
            net_class_map={"SWCLK": NET_CLASS_DEBUG},
        )
        ar.nets[1] = [("U1", "37")]
        ar.net_names[1] = "SWCLK"

        ar._prepare_routing()

        # The 2L generic stackup does NOT opt into controlled impedance,
        # so the bridge must not synthesize.  SWCLK keeps its literal.
        assert ar.net_class_map["SWCLK"].target_single_impedance is None
        assert ar.net_class_map["SWCLK"].trace_width == NET_CLASS_DEBUG.trace_width

    def test_explicit_target_not_clobbered(self):
        """When a NetClass already declares ``target_single_impedance``,
        the bridge must NOT overwrite it -- the explicit declaration
        wins (AC5: bridge is no-op when explicit NetClass exists)."""
        rules = DesignRules(manufacturer="jlcpcb")
        stackup = Stackup.jlcpcb_4layer()
        layer_stack = LayerStack.four_layer_sig_gnd_pwr_sig()

        # Explicit declaration: user wants 75Ω, not the regex default of 50Ω.
        explicit_class = NetClassRouting(
            name="Custom75",
            trace_width=0.2,
            clearance=0.15,
            target_single_impedance=75.0,
        )
        ar = Autorouter(
            width=20.0,
            height=20.0,
            rules=rules,
            stackup=stackup,
            layer_stack=layer_stack,
            net_class_map={"MY_CLK": explicit_class},
        )
        ar.nets[1] = [("R1", "1")]
        ar.net_names[1] = "MY_CLK"

        ar._prepare_routing()

        # The explicit 75Ω target survives -- the bridge did not
        # overwrite it with the regex default of 50Ω.
        assert ar.net_class_map["MY_CLK"].target_single_impedance == 75.0


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


class TestValidatorRegexBridgeIdempotent:
    """Running the synthesizer twice produces the same result."""

    def test_synthesis_is_idempotent(self):
        """Multi-pass routing strategies may call ``_prepare_routing``
        repeatedly.  Second call must produce the same widths as the
        first."""
        rules = DesignRules(manufacturer="jlcpcb")
        stackup = Stackup.jlcpcb_4layer()
        layer_stack = LayerStack.four_layer_sig_gnd_pwr_sig()

        ar = Autorouter(
            width=20.0,
            height=20.0,
            rules=rules,
            stackup=stackup,
            layer_stack=layer_stack,
            net_class_map={"SWCLK": NET_CLASS_DEBUG},
        )
        ar.nets[1] = [("U1", "37")]
        ar.net_names[1] = "SWCLK"

        ar._prepare_routing()
        width_after_first = ar.net_class_map["SWCLK"].trace_width
        target_after_first = ar.net_class_map["SWCLK"].target_single_impedance

        ar._prepare_routing()
        width_after_second = ar.net_class_map["SWCLK"].trace_width
        target_after_second = ar.net_class_map["SWCLK"].target_single_impedance

        assert width_after_first == width_after_second
        assert target_after_first == target_after_second == 50.0
