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


# ---------------------------------------------------------------------------
# Issue #2967 -- board 06 regression: respect implicit "don't apply
# impedance sizing" when the board script declares explicit
# ``target_*_impedance`` but does NOT pass a stackup to ``Autorouter``.
# Mirrors board 06's ``APPLY_IMPEDANCE_DRIVEN_SIZING = False`` opt-out:
# the resolver must stay dormant on the production CLI path so the
# ``intra_pair_clearance`` literals are not overwritten with physically
# unrouteable ~8 mm gaps.
# ---------------------------------------------------------------------------


class TestBoard06DormancyOptOut:
    """Issue #2967: when a board declares explicit ``target_*_impedance``
    on its net classes but does NOT pass a stackup to ``Autorouter``,
    the resolver must stay dormant -- matching pre-#2964 production CLI
    behavior.  This is how board 06 opts out of impedance-driven sizing
    on its dense diff-pair fabric (its ``intra_pair_clearance`` literals
    of 0.075-0.10 mm would otherwise be overwritten with ~8 mm values
    that block the entire BGA/QFN/FFC pad-pitch corridors)."""

    def test_explicit_diff_targets_without_stackup_leaves_resolver_dormant(self):
        """Board 06's scenario: MIPI/USB/PCIE classes carry
        ``target_diff_impedance`` but the production CLI route passes
        no stackup to ``Autorouter`` -- the resolver must stay dormant
        and ``intra_pair_clearance`` literals must survive untouched."""
        rules = DesignRules(manufacturer="jlcpcb")
        layer_stack = LayerStack.four_layer_sig_gnd_pwr_sig()

        # Mirror board 06's MIPI net class (target_diff_impedance=100,
        # intra_pair_clearance=0.10 mm).  No stackup is supplied --
        # this matches ``load_pcb_for_routing`` (Issue #2966 root cause
        # was that this path silently woke the resolver on this exact
        # net-class shape, producing ~8 mm gaps on MIPI/USB/PCIE).
        mipi_class = NetClassRouting(
            name="MIPI",
            trace_width=0.15,
            clearance=0.15,
            intra_pair_clearance=0.10,
            target_diff_impedance=100.0,
        )
        ar = Autorouter(
            width=20.0,
            height=20.0,
            rules=rules,
            stackup=None,  # explicit: no stackup, just like load_pcb_for_routing
            layer_stack=layer_stack,
            net_class_map={"MIPI_CLK+": mipi_class, "MIPI_CLK-": mipi_class},
        )
        # No CLK-matching net WITHOUT a target -- both already have
        # target_diff_impedance set, so synthesis must not fire.
        ar.nets[1] = [("U1", "1")]
        ar.net_names[1] = "MIPI_CLK+"
        ar.nets[2] = [("U1", "2")]
        ar.net_names[2] = "MIPI_CLK-"

        # Pre-gate: ``_has_synthesis_candidates`` must return False
        # because every CLK net already has an explicit target.
        assert ar._has_synthesis_candidates() is False, (
            "Issue #2967 regression: _has_synthesis_candidates() must "
            "return False when every matching net already carries an "
            "explicit target_*_impedance.  Returning True would wake "
            "the auto-derive path and overwrite intra_pair_clearance "
            "with ~8 mm gaps, blocking board 06's diff-pair fabric."
        )

        ar._prepare_routing()

        # Resolver must have stayed dormant: stackup is still None,
        # intra_pair_clearance literal survives, target_diff_impedance
        # survives (board 06 still declares it for AC#6 assertions).
        assert ar._stackup is None, (
            "Issue #2967 regression: auto-stackup fired on a board "
            "that has explicit targets but no synthesis candidates.  "
            "Board 06 routes 0/21 nets when this happens."
        )
        resolved = ar.net_class_map["MIPI_CLK+"]
        assert resolved.intra_pair_clearance == 0.10, (
            f"Issue #2967 regression: MIPI intra_pair_clearance was "
            f"overwritten to {resolved.intra_pair_clearance}mm (board "
            f"06's literal is 0.10mm).  The resolver fired on an "
            f"explicit target despite the board author's implicit "
            f"opt-out (no stackup passed)."
        )
        assert resolved.target_diff_impedance == 100.0

    def test_swclk_synthesis_candidate_still_wakes_auto_derive(self):
        """The opt-out must NOT regress the #2964 SWCLK fix: when a
        regex-matching net (e.g. SWCLK) has no explicit target,
        ``_has_synthesis_candidates`` returns True and the auto-derive
        fires -- preserving board 04's impedance-driven SWCLK width."""
        rules = DesignRules(manufacturer="jlcpcb")
        layer_stack = LayerStack.four_layer_sig_gnd_pwr_sig()

        ar = Autorouter(
            width=20.0,
            height=20.0,
            rules=rules,
            stackup=None,
            layer_stack=layer_stack,
            net_class_map={"SWCLK": NET_CLASS_DEBUG},
        )
        ar.nets[1] = [("U1", "37")]
        ar.net_names[1] = "SWCLK"

        # SWCLK has no target on NET_CLASS_DEBUG, and matches
        # ``.*CLK.*`` -- synthesis should fire.
        assert ar._has_synthesis_candidates() is True

        ar._prepare_routing()

        # The auto-derive fired (stackup now set), synthesis ran,
        # and the resolver produced a 50Ω-driven width.
        assert ar._stackup is not None
        assert ar.net_class_map["SWCLK"].target_single_impedance == 50.0
        assert ar.net_class_map["SWCLK"].trace_width > 0.25

    def test_auto_derive_uses_jlcpcb_4l_not_generic_4l(self):
        """PR #2966 Judge Q2: the router's auto-derived 4L stackup must
        match what the validator uses by default (JLCPCB 4L, er=4.05,
        0.2104 mm prepreg) so router and validator agree on impedance
        widths.  Before the fix the router used
        ``_create_generic_stackup`` (er=4.5, 0.20 mm prepreg) which
        produced a different SWCLK width (0.325 mm vs the JLCPCB 0.375
        mm) and put router/validator out of lockstep."""
        rules = DesignRules(manufacturer="jlcpcb")
        layer_stack = LayerStack.four_layer_sig_gnd_pwr_sig()

        ar = Autorouter(
            width=20.0,
            height=20.0,
            rules=rules,
            stackup=None,
            layer_stack=layer_stack,
            net_class_map={"SWCLK": NET_CLASS_DEBUG},
        )
        ar.nets[1] = [("U1", "37")]
        ar.net_names[1] = "SWCLK"

        ar._prepare_routing()

        # Verify the resolver produced the JLCPCB 4L width (0.375 mm)
        # rather than the generic 4L width (0.325 mm).  Both are within
        # the 10% DRC tolerance for 50Ω SWCLK on JLCPCB, but only the
        # JLCPCB value keeps router and validator in lockstep.
        resolved_width = ar.net_class_map["SWCLK"].trace_width
        assert resolved_width == pytest.approx(0.375, abs=0.02), (
            f"PR #2966 Judge Q2 regression: SWCLK width={resolved_width:.3f}mm "
            f"-- expected ~0.375mm (JLCPCB 4L er=4.05).  The router's "
            f"auto-derived stackup must match Stackup._create_default_stackup "
            f"so the router and validator compute the same impedance widths."
        )

        # Verify the auto-derived stackup carries the JLCPCB epsilon_r
        # for the F.Cu reference dielectric (the first prepreg).
        # JLCPCB 4L has prepreg er=4.05.  Generic 4L has er=4.5.
        from kicad_tools.physics.stackup import LayerType

        first_dielectric = next(
            layer
            for layer in ar._stackup.layers
            if layer.layer_type == LayerType.DIELECTRIC and layer.epsilon_r
        )
        assert first_dielectric.epsilon_r == pytest.approx(4.05, abs=0.01), (
            f"Auto-derived stackup's first dielectric has er="
            f"{first_dielectric.epsilon_r:.2f}; expected 4.05 (JLCPCB).  "
            f"Validator/router lockstep drift will resurface."
        )
