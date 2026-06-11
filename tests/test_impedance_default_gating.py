"""Tests for the controlled-impedance opt-in gating of ``ImpedanceRule``.

Issue #2696: On 2-layer hobbyist boards with no explicit stackup, the
default ``.*CLK.*`` -> 50Ω auto-applied spec produces spurious DRC
errors because 50Ω is infeasible on a 1.6mm core (~2.8mm trace width
required). The rule now suppresses its auto-applied defaults when the
board has neither an explicit ``(setup (stackup ...))`` block nor 4+
copper layers.

These tests cover:

- 2-layer default stackup + SWCLK trace -> zero violations under defaults
- 4-layer default stackup + SWCLK trace -> defaults still apply
- 2-layer with explicit stackup data -> defaults still apply
- User-supplied specs are always evaluated regardless of stackup
- The ``Stackup.has_explicit_data`` flag round-trips through ``from_pcb``
"""

from __future__ import annotations

from pathlib import Path

import pytest

# Skip the entire module if yaml (and therefore the validate package)
# isn't available — mirrors test_physics_integration.py's gating.
pytest.importorskip("yaml")


# 2-layer SWCLK PCB fixture (mimics board 04's relevant geometry).
TWO_LAYER_SWCLK_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (37 "F.SilkS" user "F.Silkscreen")
    (44 "Edge.Cuts" user)
    (49 "F.Fab" user)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "SWCLK")
  (gr_rect (start 100 100) (end 150 150)
    (stroke (width 0.1) (type default))
    (fill none)
    (layer "Edge.Cuts")
  )
  (segment (start 110 120) (end 140 120) (width 0.2) (layer "F.Cu") (net 1)
    (uuid "00000000-0000-0000-0000-000000000020"))
  (segment (start 110 121) (end 140 121) (width 0.2) (layer "B.Cu") (net 1)
    (uuid "00000000-0000-0000-0000-000000000021"))
)
"""


@pytest.fixture
def two_layer_swclk_pcb(tmp_path: Path) -> Path:
    pcb_file = tmp_path / "two_layer_swclk.kicad_pcb"
    pcb_file.write_text(TWO_LAYER_SWCLK_PCB)
    return pcb_file


class TestImpedanceDefaultGating:
    """Tests for the 2-layer / non-controlled-impedance opt-out (#2696)."""

    def test_two_layer_default_swclk_no_violations(self, two_layer_swclk_pcb: Path):
        """2-layer board with SWCLK + no explicit stackup -> zero violations.

        Before #2696, this fixture fired 2 ImpedanceRule errors (one per
        layer) because the default ``.*CLK.*`` -> 50Ω spec auto-applied
        and 0.2 mm traces on default 2-layer FR4 give ~133Ω / ~64Ω
        (well outside the 10% tolerance).

        With the gating fix, the rule sees a generic 2-layer stackup
        (no ``(setup (stackup ...))`` block, 2 copper layers) and skips
        its default name-pattern matching entirely.
        """
        from kicad_tools.manufacturers import get_profile
        from kicad_tools.schema.pcb import PCB
        from kicad_tools.validate.rules.impedance import ImpedanceRule

        pcb = PCB.load(str(two_layer_swclk_pcb))
        design_rules = get_profile("jlcpcb").get_design_rules(2, 1.0)

        rule = ImpedanceRule()
        results = rule.check(pcb, design_rules)

        # Defaults gated off — no violations expected.
        assert len(results.errors) == 0, (
            f"Expected 0 errors on 2-layer SWCLK board with default specs, "
            f"got {len(results.errors)}: {[e.message for e in results.errors]}"
        )
        # Also: no warnings from the rule itself (only the
        # "physics module unavailable" path emits a warning, and that
        # path is not exercised here).
        non_physics_warnings = [
            w for w in results.warnings if "physics module not available" not in w.message
        ]
        assert non_physics_warnings == [], (
            f"Expected no non-physics warnings, got: {[w.message for w in non_physics_warnings]}"
        )

    def test_user_specs_evaluated_on_two_layer(self, two_layer_swclk_pcb: Path):
        """Explicit user-supplied specs are evaluated regardless of stackup.

        If the caller passes a spec list explicitly, they have opted in
        to controlled impedance checking and the rule must evaluate
        their specs — the gating only suppresses auto-applied defaults.
        """
        from kicad_tools.manufacturers import get_profile
        from kicad_tools.schema.pcb import PCB
        from kicad_tools.validate.rules.impedance import ImpedanceRule, NetImpedanceSpec

        pcb = PCB.load(str(two_layer_swclk_pcb))
        design_rules = get_profile("jlcpcb").get_design_rules(2, 1.0)

        # User explicitly requests a 50Ω target on CLK nets.  This should
        # fire (just like the old default behavior) because the caller
        # opted in by passing specs explicitly.
        user_specs = [NetImpedanceSpec(r".*CLK.*", target_z0=50.0)]
        rule = ImpedanceRule(specs=user_specs)
        results = rule.check(pcb, design_rules)

        # Expect violations: 0.2mm on 2-layer FR4 != 50Ω.
        assert len(results.errors) + len(results.warnings) > 0, (
            "Expected violations when user explicitly passes 50Ω spec on 2-layer board"
        )

    def test_board_has_controlled_impedance_logic(self):
        """Direct unit test of the gating helper.

        Covers the three branches: explicit stackup flag, 4+ layer
        threshold, and the 2-layer default fallback.
        """
        from kicad_tools.physics import Stackup
        from kicad_tools.validate.rules.impedance import ImpedanceRule

        # 1) 2-layer default stackup, no explicit data -> opt out.
        rule = ImpedanceRule()
        rule._stackup = Stackup.default_2layer()
        assert rule._board_has_controlled_impedance() is False

        # 2) 4-layer JLCPCB stackup, no explicit data -> opt in (>=4 copper).
        rule2 = ImpedanceRule()
        rule2._stackup = Stackup.jlcpcb_4layer()
        assert rule2._board_has_controlled_impedance() is True

        # 3) 2-layer with explicit data flag set -> opt in.
        rule3 = ImpedanceRule()
        rule3._stackup = Stackup.default_2layer()
        rule3._stackup.has_explicit_data = True
        assert rule3._board_has_controlled_impedance() is True

        # 4) No stackup at all -> preserve original behavior (opt in).
        rule4 = ImpedanceRule()
        rule4._stackup = None
        assert rule4._board_has_controlled_impedance() is True

    def test_using_default_specs_flag(self):
        """``_using_default_specs`` tracks whether caller supplied specs."""
        from kicad_tools.validate.rules.impedance import ImpedanceRule, NetImpedanceSpec

        # Default: no specs -> using defaults.
        rule_default = ImpedanceRule()
        assert rule_default._using_default_specs is True

        # User supplies specs -> not using defaults.
        rule_user = ImpedanceRule(specs=[NetImpedanceSpec(r".*", target_z0=50.0)])
        assert rule_user._using_default_specs is False

        # Empty list is still user-supplied (caller signaled "no specs").
        rule_empty = ImpedanceRule(specs=[])
        assert rule_empty._using_default_specs is False


class TestDiffPairSuffixPatterns:
    """Issue #2970: default regex specs must cover the ``+/-`` diff-pair
    suffix convention (board 06's PCIE/MIPI nets), not just ``_P/_N``.

    Acceptance criteria:

    - Each board-06 ``+/-`` net resolves to ``target_zdiff=100.0`` via
      the new ``.*[+\\-]$`` spec.
    - ``MIPI_CLK+`` is NOT mis-classified by the single-ended ``.*CLK``
      pattern — the trailing ``+`` must route to the diff-pair spec.
    - The single-ended ``.*CLK`` pattern is anchored (``$``) so it does
      not eat ``MIPI_CLK+`` even before list-order tie-breaking.
    """

    def test_pcie_diff_pair_nets_resolve_to_100ohm_diff(self):
        from kicad_tools.validate.rules.impedance import ImpedanceRule

        rule = ImpedanceRule()
        for net in ("PCIE_TX+", "PCIE_TX-", "PCIE_RX+", "PCIE_RX-"):
            spec = rule._find_matching_spec(net)
            assert spec is not None, f"No spec matched {net} (regression: pre-#2970 gap)"
            assert spec.target_zdiff == 100.0, (
                f"{net} matched {spec.net_pattern!r} but target_zdiff="
                f"{spec.target_zdiff}, expected 100.0"
            )
            assert spec.target_z0 is None, (
                f"{net} unexpectedly carries target_z0={spec.target_z0} (should be diff-pair only)"
            )

    def test_mipi_diff_pair_nets_resolve_to_100ohm_diff(self):
        from kicad_tools.validate.rules.impedance import ImpedanceRule

        rule = ImpedanceRule()
        for net in ("MIPI_CLK+", "MIPI_CLK-", "MIPI_D0+", "MIPI_D0-"):
            spec = rule._find_matching_spec(net)
            assert spec is not None, f"No spec matched {net} (regression: pre-#2970 gap)"
            assert spec.target_zdiff == 100.0, (
                f"{net} matched {spec.net_pattern!r} but target_zdiff="
                f"{spec.target_zdiff}, expected 100.0 (NOT 50Ω single-ended)"
            )
            assert spec.target_z0 is None, (
                f"{net} got single-ended target_z0={spec.target_z0} -- the "
                f".*CLK pattern leaked. Issue #2970 fix regressed."
            )

    def test_mipi_clk_plus_does_not_match_clk_single_ended(self):
        """The single-ended ``.*CLK$`` anchor must not eat ``MIPI_CLK+``.

        Even if list order changed, the anchor itself guarantees correct
        classification — this is the belt to the suspenders of ordering.
        """
        import re

        from kicad_tools.validate.rules.impedance import ImpedanceRule

        clk_specs = [s for s in ImpedanceRule._get_default_specs() if "CLK" in s.net_pattern]
        assert clk_specs, "Expected at least one CLK-targeted default spec"
        for spec in clk_specs:
            if spec.target_z0 is None:
                # Skip diff-pair specs that happen to match CLK by accident
                continue
            assert not re.match(spec.net_pattern, "MIPI_CLK+", re.IGNORECASE), (
                f"Single-ended spec {spec.net_pattern!r} unexpectedly "
                f"matches 'MIPI_CLK+' -- this re-introduces Issue #2970."
            )

    def test_pn_suffix_pattern_still_works(self):
        """Regression guard: the pre-existing ``.*_[PN]$`` pattern must
        keep matching ``CLK_OUT_P`` / ``LVDS_TX_N`` etc."""
        from kicad_tools.validate.rules.impedance import ImpedanceRule

        rule = ImpedanceRule()
        for net in ("FOO_P", "BAR_N", "DATA_BUS_P"):
            spec = rule._find_matching_spec(net)
            assert spec is not None
            assert spec.target_zdiff == 100.0

    def test_swclk_still_resolves_to_50ohm_single_ended(self):
        """Regression guard for PR #2966: bare ``SWCLK`` (no suffix)
        must still resolve to the 50Ω single-ended default."""
        from kicad_tools.validate.rules.impedance import ImpedanceRule

        rule = ImpedanceRule()
        spec = rule._find_matching_spec("SWCLK")
        assert spec is not None
        assert spec.target_z0 == 50.0
        assert spec.target_zdiff is None

    def test_plain_nets_still_unmatched(self):
        """Negative gate: nets without recognized prefixes / suffixes
        must NOT match any default spec.  Specifically, generic power /
        ground / data line names must stay unmatched so the validator
        does not impose impedance targets on signals that have none."""
        from kicad_tools.validate.rules.impedance import ImpedanceRule

        rule = ImpedanceRule()
        for net in ("VCC", "GND", "DATA_LINE_5V", "PLAIN_NET"):
            spec = rule._find_matching_spec(net)
            assert spec is None, (
                f"Net {net!r} unexpectedly matched spec "
                f"{spec.net_pattern!r} -- the new diff-pair patterns "
                f"are too greedy."
            )

    def test_usb_diff_pair_still_resolves_via_usb_pattern(self):
        """``USB2_D+`` / ``USB2_D-`` must keep matching the dedicated
        ``USB.*D[PM\\+\\-]?`` 90Ω spec (which lives BEFORE the generic
        ``.*[+\\-]$`` 100Ω spec in list order)."""
        from kicad_tools.validate.rules.impedance import ImpedanceRule

        rule = ImpedanceRule()
        for net in ("USB2_D+", "USB2_D-", "USB_DP", "USB_DM"):
            spec = rule._find_matching_spec(net)
            assert spec is not None
            assert spec.target_zdiff == 90.0, (
                f"{net} matched {spec.net_pattern!r} -> "
                f"target_zdiff={spec.target_zdiff}, expected 90.0 "
                f"(USB-specific pattern must precede generic +/- pattern)"
            )


class TestStackupExplicitDataFlag:
    """Tests for ``Stackup.has_explicit_data`` round-tripping."""

    def test_default_2layer_has_no_explicit_data(self):
        """Default presets are NOT marked as explicit."""
        from kicad_tools.physics import Stackup

        stackup = Stackup.default_2layer()
        assert stackup.has_explicit_data is False

    def test_jlcpcb_4layer_has_no_explicit_data(self):
        """Manufacturer presets are NOT marked as explicit either.

        ``has_explicit_data`` specifically signals "this came from a real
        PCB file's stackup block". A preset constructed in code is not
        explicit board data — but it doesn't matter for the gating since
        4-layer boards opt in via the copper-count branch anyway.
        """
        from kicad_tools.physics import Stackup

        stackup = Stackup.jlcpcb_4layer()
        assert stackup.has_explicit_data is False

    def test_from_pcb_no_stackup_data_is_not_explicit(self, tmp_path: Path):
        """A PCB with no ``(setup (stackup ...))`` -> ``has_explicit_data=False``."""
        from kicad_tools.physics import Stackup
        from kicad_tools.schema.pcb import PCB

        pcb_file = tmp_path / "no_stackup.kicad_pcb"
        pcb_file.write_text(TWO_LAYER_SWCLK_PCB)

        pcb = PCB.load(str(pcb_file))
        stackup = Stackup.from_pcb(pcb)
        assert stackup.has_explicit_data is False
        assert stackup.num_copper_layers == 2


# Minimal 4-layer fixture mimicking board 06's opt-out shape:
# diff-pair nets (`PCIE_TX+/-`, `MIPI_CLK+/-`) at the literal 0.2mm
# trace width on a 4L board with no explicit stackup.  Board 06 opts
# out of impedance-driven sizing via APPLY_IMPEDANCE_DRIVEN_SIZING=False
# in its generate_design.py, so its routed traces don't compute to
# 100Ω differential.  Pre-fix, the standalone CLI (``kct check`` with
# no ``detected_pairs`` context) would silently fall through the
# coupled-lines path and evaluate every diff-pair trace against a
# bogus ``target_z = spec.target_z0 or 50.0`` (i.e. 50Ω single-ended),
# producing spurious "target is 50.0Ω" errors on PCIE_TX+/-, MIPI_*,
# etc.  The fix in ImpedanceRule.check() skips diff-only specs when
# no diff-pair detection context is supplied.
FOUR_LAYER_DIFFPAIR_OPT_OUT_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (1 "In1.Cu" signal)
    (2 "In2.Cu" signal)
    (31 "B.Cu" signal)
    (37 "F.SilkS" user "F.Silkscreen")
    (44 "Edge.Cuts" user)
    (49 "F.Fab" user)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "PCIE_TX+")
  (net 2 "PCIE_TX-")
  (net 3 "MIPI_CLK+")
  (net 4 "MIPI_CLK-")
  (net 5 "USB3_RX1+")
  (net 6 "USB3_RX1-")
  (gr_rect (start 100 100) (end 150 150)
    (stroke (width 0.1) (type default))
    (fill none)
    (layer "Edge.Cuts")
  )
  (segment (start 110 120) (end 140 120) (width 0.2) (layer "F.Cu") (net 1)
    (uuid "00000000-0000-0000-0000-000000000020"))
  (segment (start 110 121) (end 140 121) (width 0.2) (layer "F.Cu") (net 2)
    (uuid "00000000-0000-0000-0000-000000000021"))
  (segment (start 110 122) (end 140 122) (width 0.2) (layer "F.Cu") (net 3)
    (uuid "00000000-0000-0000-0000-000000000022"))
  (segment (start 110 123) (end 140 123) (width 0.2) (layer "F.Cu") (net 4)
    (uuid "00000000-0000-0000-0000-000000000023"))
  (segment (start 110 124) (end 140 124) (width 0.2) (layer "F.Cu") (net 5)
    (uuid "00000000-0000-0000-0000-000000000024"))
  (segment (start 110 125) (end 140 125) (width 0.2) (layer "F.Cu") (net 6)
    (uuid "00000000-0000-0000-0000-000000000025"))
)
"""


@pytest.fixture
def four_layer_diffpair_opt_out_pcb(tmp_path: Path) -> Path:
    pcb_file = tmp_path / "four_layer_diffpair.kicad_pcb"
    pcb_file.write_text(FOUR_LAYER_DIFFPAIR_OPT_OUT_PCB)
    return pcb_file


class TestDiffOnlySpecsSuppressedWithoutDetection:
    """PR #2973 follow-up: diff-only validator specs must not fire on
    standalone ``kct check`` invocations that don't supply diff-pair
    detection context.

    Background: ``ImpedanceRule._check_trace_impedance`` falls back to
    the single-ended microstrip / stripline model when a trace's net is
    NOT in ``self._partner_map`` (i.e. the rule was constructed without
    ``detected_pairs``).  The single-ended fallback evaluates the trace
    against ``target_z = spec.target_z0 or 50.0`` -- for a diff-only
    spec (``target_z0=None, target_zdiff=100``), this collapses to 50.0Ω
    and produces "target is 50.0Ω" errors that have nothing to do with
    the spec's actual differential target.

    Board 06 (4L diff-pair test, ``APPLY_IMPEDANCE_DRIVEN_SIZING=False``)
    exercises this on the routed-DRC CI gate: its PCIE/MIPI/USB3 nets
    at the literal 0.2mm width produced 82+ spurious errors after PR
    #2973's regex extension brought them under the diff-only patterns.

    The fix in ``ImpedanceRule.check()`` skips diff-only specs for nets
    that aren't in ``self._partner_map``.  Diff-pair impedance is still
    validated when the router supplies ``detected_pairs`` (via the
    autorouter integration); only the bogus single-ended fallback is
    suppressed.
    """

    def test_diff_only_specs_suppressed_on_4l_without_detected_pairs(
        self, four_layer_diffpair_opt_out_pcb: Path
    ):
        """Mirror of board 06's CI scenario: PCIE_TX+/-, MIPI_CLK+/-,
        and USB3_RX1+/- at the literal 0.2mm width on a 4L board with
        no detected_pairs context produces zero impedance violations.

        Before the fix, all 6 nets fired against the 50Ω single-ended
        fallback (deviation ~36%), producing 6 errors.
        """
        from kicad_tools.manufacturers import get_profile
        from kicad_tools.schema.pcb import PCB
        from kicad_tools.validate.rules.impedance import ImpedanceRule

        pcb = PCB.load(str(four_layer_diffpair_opt_out_pcb))
        design_rules = get_profile("jlcpcb").get_design_rules(4, 1.0)

        rule = ImpedanceRule()  # no detected_pairs
        results = rule.check(pcb, design_rules)

        impedance_errors = [e for e in results.errors if e.rule_id == "impedance"]
        assert len(impedance_errors) == 0, (
            f"Expected 0 impedance errors on 4L diff-pair board without "
            f"detected_pairs context, got {len(impedance_errors)}: "
            f"{[e.message for e in impedance_errors]}.  PR #2973 follow-up "
            f"regressed: diff-only specs are leaking into the single-ended "
            f"50Ω fallback path."
        )

    def test_single_ended_specs_still_fire_without_detected_pairs(self):
        """Regression guard: ``.*CLK$`` / ``.*MCLK$`` / ``.*ETH.*``
        (the single-ended ``target_z0`` specs) must still fire on a
        ``kct check`` invocation without ``detected_pairs``.  This
        preserves the controlled-impedance use case (e.g. an SWCLK net
        flagged at DRC time on a 4L board) and keeps the validator from
        becoming a silent no-op.
        """
        from kicad_tools.physics import Stackup
        from kicad_tools.validate.rules.impedance import ImpedanceRule

        rule = ImpedanceRule()
        rule._stackup = Stackup.jlcpcb_4layer()
        # Verify the single-ended specs are NOT suppressed by the new
        # gate.  We probe ``_find_matching_spec`` directly (the check
        # method's per-trace logic is exercised by other tests).
        clk_spec = rule._find_matching_spec("SWCLK")
        assert clk_spec is not None
        assert clk_spec.target_z0 == 50.0
        assert clk_spec.target_zdiff is None
        # The new gate only triggers when target_zdiff is set AND
        # target_z0 is None.  Single-ended specs (target_z0 set) bypass
        # the gate -- they always evaluate.
        assert not (clk_spec.target_z0 is None and clk_spec.target_zdiff is not None), (
            "Single-ended SWCLK spec must not be classified as diff-only"
        )

    def test_diff_specs_still_fire_with_detected_pairs(self):
        """Regression guard: when the autorouter supplies ``detected_pairs``,
        the diff-only specs SHOULD still fire -- this is the Phase 3K
        coupled-lines integration path.  The gate only suppresses diff
        specs in the no-context standalone CLI path.
        """
        from unittest.mock import MagicMock

        from kicad_tools.validate.rules.impedance import ImpedanceRule

        # Build a fake DifferentialPair so the rule populates _partner_map.
        # The rule's __init__ reads .positive.net_name + .negative.net_name.
        fake_pair = MagicMock()
        fake_pair.positive.net_name = "PCIE_TX+"
        fake_pair.negative.net_name = "PCIE_TX-"

        rule = ImpedanceRule(detected_pairs=[fake_pair])

        # The partner_map MUST be populated -- the gate keys off this
        # to decide whether to suppress.
        assert "PCIE_TX+" in rule._partner_map
        assert "PCIE_TX-" in rule._partner_map
        assert rule._partner_map["PCIE_TX+"] == "PCIE_TX-"


# Chorus-like 4-layer audio board (Issue #3157).  Slow I2S / DAC clock
# nets (``DAC_CLK``, ``BCLK``, ``MCLK``, ``I2S_LRCLK``) plus plain audio /
# I2C nets (``AUDIO_L``, ``AUDIO_R``, ``SDA``, ``SCL``) routed at the
# literal 0.200 mm width on a 4-layer board WITH an explicit
# ``(setup (stackup ...))`` block (JLCPCB-style).
#
# The real chorus-test board produced 32 false-positive impedance errors
# here: the ``*CLK`` / ``*MCLK`` heuristic specs assumed "ends in CLK ==
# high-speed 50Ω" and fired on these slow audio clocks (0.200 mm gives
# ~63.5Ω, target 50.0Ω, ~27% deviation, requires 0.336 mm).  The plain
# audio / I2C nets never matched any spec.  With the Issue #3157 fix the
# single-ended heuristics are suppressed by default, so this board
# produces zero single-ended impedance errors unless a net is explicitly
# declared controlled-impedance.
CHORUS_LIKE_4L_CLOCK_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (1 "In1.Cu" power)
    (2 "In2.Cu" power)
    (31 "B.Cu" signal)
    (37 "F.SilkS" user "F.Silkscreen")
    (44 "Edge.Cuts" user)
    (49 "F.Fab" user)
  )
  (setup
    (stackup
      (layer "F.SilkS" (type "Top Silk Screen"))
      (layer "F.Paste" (type "Top Solder Paste"))
      (layer "F.Mask" (type "Top Solder Mask") (thickness 0.01))
      (layer "F.Cu" (type "copper") (thickness 0.035))
      (layer "dielectric 1" (type "prepreg") (thickness 0.2104) (material "FR4") (epsilon_r 4.05) (loss_tangent 0.02))
      (layer "In1.Cu" (type "copper") (thickness 0.0152))
      (layer "dielectric 2" (type "core") (thickness 1.065) (material "FR4") (epsilon_r 4.5) (loss_tangent 0.02))
      (layer "In2.Cu" (type "copper") (thickness 0.0152))
      (layer "dielectric 3" (type "prepreg") (thickness 0.2104) (material "FR4") (epsilon_r 4.05) (loss_tangent 0.02))
      (layer "B.Cu" (type "copper") (thickness 0.035))
      (layer "B.Mask" (type "Bottom Solder Mask") (thickness 0.01))
      (layer "B.SilkS" (type "Bottom Silk Screen"))
      (copper_finish "ENIG")
      (dielectric_constraints no)
    )
    (pad_to_mask_clearance 0)
  )
  (net 0 "")
  (net 1 "DAC_CLK")
  (net 2 "BCLK")
  (net 3 "MCLK")
  (net 4 "I2S_LRCLK")
  (net 5 "AUDIO_L")
  (net 6 "AUDIO_R")
  (net 7 "SDA")
  (net 8 "SCL")
  (gr_rect (start 100 100) (end 150 150)
    (stroke (width 0.1) (type default))
    (fill none)
    (layer "Edge.Cuts")
  )
  (segment (start 110 120) (end 140 120) (width 0.2) (layer "F.Cu") (net 1)
    (uuid "00000000-0000-0000-0000-000000000031"))
  (segment (start 110 121) (end 140 121) (width 0.2) (layer "F.Cu") (net 2)
    (uuid "00000000-0000-0000-0000-000000000032"))
  (segment (start 110 122) (end 140 122) (width 0.2) (layer "F.Cu") (net 3)
    (uuid "00000000-0000-0000-0000-000000000033"))
  (segment (start 110 123) (end 140 123) (width 0.2) (layer "F.Cu") (net 4)
    (uuid "00000000-0000-0000-0000-000000000034"))
  (segment (start 110 124) (end 140 124) (width 0.2) (layer "F.Cu") (net 5)
    (uuid "00000000-0000-0000-0000-000000000035"))
  (segment (start 110 125) (end 140 125) (width 0.2) (layer "F.Cu") (net 6)
    (uuid "00000000-0000-0000-0000-000000000036"))
  (segment (start 110 126) (end 140 126) (width 0.2) (layer "F.Cu") (net 7)
    (uuid "00000000-0000-0000-0000-000000000037"))
  (segment (start 110 127) (end 140 127) (width 0.2) (layer "F.Cu") (net 8)
    (uuid "00000000-0000-0000-0000-000000000038"))
)
"""


@pytest.fixture
def chorus_like_4l_clock_pcb(tmp_path: Path) -> Path:
    pcb_file = tmp_path / "chorus_like_4l_clock.kicad_pcb"
    pcb_file.write_text(CHORUS_LIKE_4L_CLOCK_PCB)
    return pcb_file


class TestSingleEndedHeuristicSuppression:
    """Issue #3157: single-ended impedance is declarative / opt-in.

    The built-in single-ended name-pattern heuristics (``.*CLK$`` /
    ``.*MCLK$`` / ``.*ETH.*`` -> 50Ω) must NOT auto-fire as DRC errors,
    because they assume "ends in CLK == high-speed 50Ω" and produced 32
    false-positive impedance errors on chorus-test (a low-speed 4-layer
    audio board with slow I2S / DAC clock nets).
    """

    def test_explicit_stackup_4l_fixture_has_expected_shape(self, chorus_like_4l_clock_pcb: Path):
        """Sanity: the fixture really IS a 4-layer board with explicit
        stackup data (so it opts into controlled impedance via BOTH the
        ``has_explicit_data`` and the ``>=4 copper`` branches -- the exact
        chorus-test situation that made the heuristics fire pre-fix).
        """
        from kicad_tools.physics import Stackup
        from kicad_tools.schema.pcb import PCB

        pcb = PCB.load(str(chorus_like_4l_clock_pcb))
        stackup = Stackup.from_pcb(pcb)
        assert stackup.has_explicit_data is True
        assert stackup.num_copper_layers == 4

    def test_clock_nets_produce_zero_impedance_errors_by_default(
        self, chorus_like_4l_clock_pcb: Path
    ):
        """AC #1: a 4-layer board with clock-named nets and NO explicit
        impedance declaration produces 0 impedance errors by default.

        This is the chorus-test false-positive scenario.  Pre-#3157 the
        ``*CLK`` / ``*MCLK`` heuristic specs fired on DAC_CLK / BCLK /
        MCLK / I2S_LRCLK (each ~27% off 50Ω at 0.200 mm).  With the fix
        the single-ended heuristics are suppressed by default.
        """
        from kicad_tools.manufacturers import get_profile
        from kicad_tools.schema.pcb import PCB
        from kicad_tools.validate.rules.impedance import ImpedanceRule

        pcb = PCB.load(str(chorus_like_4l_clock_pcb))
        design_rules = get_profile("jlcpcb").get_design_rules(4, 1.0)

        rule = ImpedanceRule()  # default specs, no net_class_map, no specs=
        results = rule.check(pcb, design_rules)

        impedance_violations = [
            v for v in (results.errors + results.warnings) if v.rule_id == "impedance"
        ]
        # The "physics module unavailable" warning is not an impedance
        # mismatch -- filter it out for an exact count.
        impedance_mismatches = [
            v for v in impedance_violations if "physics module not available" not in v.message
        ]
        assert impedance_mismatches == [], (
            f"Expected 0 single-ended impedance errors on a 4L audio board "
            f"with clock-named nets and no declared impedance, got "
            f"{len(impedance_mismatches)}: {[v.message for v in impedance_mismatches]}.  "
            f"Issue #3157: the *CLK / *MCLK heuristic specs are firing on "
            f"slow audio clocks again."
        )

    def test_checker_pipeline_zero_impedance_errors_without_net_class_map(
        self, chorus_like_4l_clock_pcb: Path
    ):
        """AC #1 at the ``DRCChecker`` level: ``check_impedance()`` with no
        ``net_class_map`` produces 0 impedance errors on the chorus-like
        board.  This is the exact path ``kct check`` (no ``--net-class-map``)
        exercises.
        """
        from kicad_tools.schema.pcb import PCB
        from kicad_tools.validate import DRCChecker

        pcb = PCB.load(str(chorus_like_4l_clock_pcb))
        checker = DRCChecker(pcb, manufacturer="jlcpcb", layers=4)
        results = checker.check_impedance()

        impedance_mismatches = [
            v
            for v in (results.errors + results.warnings)
            if v.rule_id == "impedance" and "physics module not available" not in v.message
        ]
        assert impedance_mismatches == [], (
            f"Expected 0 impedance errors from DRCChecker.check_impedance() "
            f"without a net_class_map, got {len(impedance_mismatches)}: "
            f"{[v.message for v in impedance_mismatches]}"
        )

    def test_default_specs_still_contain_se_patterns(self):
        """Regression guard: the SE heuristic patterns must stay IN the
        default spec list (so ``_find_matching_spec`` and the router's
        impedance-driven-sizing bridge can still resolve a 50Ω target).

        Issue #3157 suppresses their *evaluation* in ``check()``, it does
        NOT remove them from the default list -- the #2964 router bridge
        depends on ``_get_default_specs()`` returning the CLK spec.
        """
        from kicad_tools.validate.rules.impedance import ImpedanceRule

        rule = ImpedanceRule()
        clk_spec = rule._find_matching_spec("SWCLK")
        assert clk_spec is not None
        assert clk_spec.target_z0 == 50.0
        # And it IS classified as a heuristic spec (so check() suppresses it).
        assert ImpedanceRule._is_single_ended_heuristic_spec(clk_spec) is True

    def test_plain_audio_and_i2c_nets_never_matched(self, chorus_like_4l_clock_pcb: Path):
        """The plain audio / I2C nets (AUDIO_L/R, SDA, SCL) match no spec
        at all -- they never produced errors even pre-fix.  This documents
        the curator's correction to the issue body ("not ALL nets")."""
        from kicad_tools.validate.rules.impedance import ImpedanceRule

        rule = ImpedanceRule()
        for net in ("AUDIO_L", "AUDIO_R", "SDA", "SCL"):
            assert rule._find_matching_spec(net) is None, (
                f"Net {net!r} unexpectedly matched a default spec"
            )


class TestSingleEndedImpedanceOptIn:
    """Issue #3157 AC #2: a net explicitly declared single-ended impedance
    (via ``net_class_map`` ``target_single_impedance``) STILL gets checked.
    """

    def test_declared_50ohm_via_net_class_map_still_fires(self, chorus_like_4l_clock_pcb: Path):
        """A net class declaring ``target_single_impedance=50`` makes the
        rule evaluate that net's traces -- and the 0.200 mm width (~63.5Ω
        on this 4L stackup) fires an impedance error.

        This proves the opt-in path works: declared impedance is checked,
        undeclared (heuristic-only) impedance is not.
        """
        from kicad_tools.router.rules import NetClassRouting
        from kicad_tools.schema.pcb import PCB
        from kicad_tools.validate import DRCChecker

        pcb = PCB.load(str(chorus_like_4l_clock_pcb))

        # Declare DAC_CLK as a 50Ω single-ended controlled-impedance net.
        controlled = NetClassRouting(name="ControlledClock", target_single_impedance=50.0)
        net_class_map = {"DAC_CLK": controlled}

        checker = DRCChecker(pcb, manufacturer="jlcpcb", layers=4, net_class_map=net_class_map)
        results = checker.check_impedance()

        impedance_violations = [
            v
            for v in (results.errors + results.warnings)
            if v.rule_id == "impedance" and "physics module not available" not in v.message
        ]
        # DAC_CLK is now checked and fails (0.200 mm != 50Ω on this stackup).
        dac_clk_violations = [v for v in impedance_violations if "DAC_CLK" in (v.items or ())]
        assert dac_clk_violations, (
            "Expected an impedance violation on DAC_CLK after declaring it "
            "50Ω single-ended via net_class_map.target_single_impedance, "
            "but none fired.  AC #2 regressed -- declared impedance must "
            "still be checked."
        )

        # The OTHER clock nets (BCLK, MCLK, I2S_LRCLK) were NOT declared,
        # so they must stay silent -- only the declared net is checked.
        for undeclared in ("BCLK", "MCLK", "I2S_LRCLK"):
            assert not [v for v in impedance_violations if undeclared in (v.items or ())], (
                f"Net {undeclared!r} was not declared controlled-impedance "
                f"but produced an impedance violation -- the heuristic "
                f"suppression leaked."
            )

    def test_declared_50ohm_at_correct_width_passes(self, tmp_path: Path):
        """Belt-and-suspenders: a net declared 50Ω whose width is correct
        for the stackup produces NO violation -- the opt-in path is a real
        impedance check, not an unconditional failure.

        The 50Ω-correct width on F.Cu is computed from the physics module
        so the test stays stackup-agnostic, then a fresh PCB is written
        with DAC_CLK widened to that value.
        """
        from kicad_tools.physics import Stackup
        from kicad_tools.router.rules import NetClassRouting
        from kicad_tools.schema.pcb import PCB
        from kicad_tools.validate import DRCChecker
        from kicad_tools.validate.rules.impedance import ImpedanceRule

        # Compute the 50Ω-correct F.Cu width for this stackup.
        base_pcb = tmp_path / "base.kicad_pcb"
        base_pcb.write_text(CHORUS_LIKE_4L_CLOCK_PCB)
        rule = ImpedanceRule(stackup=Stackup.from_pcb(PCB.load(str(base_pcb))))
        rule._init_physics(PCB.load(str(base_pcb)))
        correct_width = rule.get_required_width(50.0, "F.Cu")
        assert correct_width is not None and correct_width > 0

        # Write a variant where DAC_CLK's segment is widened to that value.
        widened = CHORUS_LIKE_4L_CLOCK_PCB.replace(
            '(segment (start 110 120) (end 140 120) (width 0.2) (layer "F.Cu") (net 1)',
            f'(segment (start 110 120) (end 140 120) (width {correct_width:.4f}) (layer "F.Cu") (net 1)',
        )
        variant = tmp_path / "variant.kicad_pcb"
        variant.write_text(widened)

        controlled = NetClassRouting(name="ControlledClock", target_single_impedance=50.0)
        checker = DRCChecker(
            PCB.load(str(variant)),
            manufacturer="jlcpcb",
            layers=4,
            net_class_map={"DAC_CLK": controlled},
        )
        results = checker.check_impedance()

        dac_clk_violations = [
            v
            for v in (results.errors + results.warnings)
            if v.rule_id == "impedance" and "DAC_CLK" in (v.items or ())
        ]
        assert dac_clk_violations == [], (
            f"DAC_CLK declared 50Ω at the impedance-correct width "
            f"({correct_width:.4f}mm) should pass, but fired: "
            f"{[v.message for v in dac_clk_violations]}"
        )


class TestDeriveSingleEndedImpedanceSpecs:
    """Unit tests for the producer helper that maps a net-class map's
    ``target_single_impedance`` to explicit ``NetImpedanceSpec`` entries.
    """

    def test_none_map_returns_empty(self):
        from kicad_tools.validate.impedance_specs import (
            derive_single_ended_impedance_specs,
        )

        assert derive_single_ended_impedance_specs(None) == []
        assert derive_single_ended_impedance_specs({}) == []

    def test_class_without_target_yields_no_spec(self):
        from kicad_tools.router.rules import NetClassRouting
        from kicad_tools.validate.impedance_specs import (
            derive_single_ended_impedance_specs,
        )

        ncm = {"FOO": NetClassRouting(name="Plain")}  # target_single_impedance=None
        assert derive_single_ended_impedance_specs(ncm) == []

    def test_declared_target_yields_anchored_spec(self):
        from kicad_tools.router.rules import NetClassRouting
        from kicad_tools.validate.impedance_specs import (
            derive_single_ended_impedance_specs,
        )

        ncm = {
            "MY_CLK": NetClassRouting(
                name="C", target_single_impedance=50.0, impedance_tolerance_percent=12.0
            ),
            "OTHER": NetClassRouting(name="Plain"),
        }
        specs = derive_single_ended_impedance_specs(ncm)
        assert len(specs) == 1
        spec = specs[0]
        assert spec.target_z0 == 50.0
        assert spec.target_zdiff is None
        assert spec.tolerance_percent == 12.0
        # Anchored to the exact net name -- must match MY_CLK and nothing else.
        assert spec.matches("MY_CLK")
        assert not spec.matches("NOT_MY_CLK")
        assert not spec.matches("MY_CLK_2")

    def test_special_regex_chars_in_net_name_are_escaped(self):
        """Net names with ``+`` (diff-pair convention) must be matched
        literally, not interpreted as a regex quantifier."""
        from kicad_tools.router.rules import NetClassRouting
        from kicad_tools.validate.impedance_specs import (
            derive_single_ended_impedance_specs,
        )

        ncm = {"CLK+": NetClassRouting(name="C", target_single_impedance=50.0)}
        specs = derive_single_ended_impedance_specs(ncm)
        assert len(specs) == 1
        assert specs[0].matches("CLK+")
        assert not specs[0].matches("CLKK")  # '+' is literal, not quantifier


# Neck-down taper exemption fixture (Issue #3413).  2-layer board with an
# explicit-spec target so the rule evaluates regardless of stackup gating.
# Net 1 carries one LONG (30 mm) mis-sized segment and two SHORT (< 1 mm)
# mis-sized "neck-down taper" segments like the router emits at fine-pitch
# pads (board 06 MIPI_RST at J4/U4: 0.32-0.78 mm at 0.10-0.19 mm width).
NECK_DOWN_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "MIPI_RST")
  (gr_rect (start 100 100) (end 150 150)
    (stroke (width 0.1) (type default))
    (fill none)
    (layer "Edge.Cuts")
  )
  (segment (start 110 120) (end 140 120) (width 0.2) (layer "F.Cu") (net 1)
    (uuid "00000000-0000-0000-0000-000000000030"))
  (segment (start 110 120) (end 110 119.3) (width 0.1) (layer "F.Cu") (net 1)
    (uuid "00000000-0000-0000-0000-000000000031"))
  (segment (start 140 120) (end 140 120.5) (width 0.12) (layer "F.Cu") (net 1)
    (uuid "00000000-0000-0000-0000-000000000032"))
)
"""


class TestNeckDownTaperExemption:
    """Issue #3413: sub-1mm neck-down taper segments are impedance-exempt.

    The router deliberately tapers controlled-impedance traces below
    their resolved width for the last <= 1 mm approaching fine-pitch
    pads (Issue #3313 neck-down mechanic).  Those taper segments are
    electrically insignificant (far below lambda/10 for 50-100 ohm
    digital targets) and must not fire the impedance rule; the net's
    long trunk segments still carry the check.
    """

    @pytest.fixture
    def neck_down_pcb(self, tmp_path: Path) -> Path:
        pcb_file = tmp_path / "neck_down.kicad_pcb"
        pcb_file.write_text(NECK_DOWN_PCB)
        return pcb_file

    def _violations(self, pcb_path: Path):
        from kicad_tools.manufacturers import get_profile
        from kicad_tools.schema.pcb import PCB
        from kicad_tools.validate.rules.impedance import ImpedanceRule, NetImpedanceSpec

        pcb = PCB.load(str(pcb_path))
        design_rules = get_profile("jlcpcb").get_design_rules(2, 1.0)
        specs = [NetImpedanceSpec(r"MIPI_RST", target_z0=50.0)]
        rule = ImpedanceRule(specs=specs)
        results = rule.check(pcb, design_rules)
        return list(results.errors) + list(results.warnings)

    def test_short_tapers_exempt_long_trunk_still_fires(self, neck_down_pcb: Path):
        """Only the 30 mm trunk fires; the 0.7/0.5 mm tapers are skipped.

        Pre-#3413 behaviour would have produced 3 violations (one per
        segment) -- the two extra ones are exactly the false-positive
        shape PR #3500 measured on board 06's MIPI_RST.
        """
        violations = self._violations(neck_down_pcb)
        assert len(violations) == 1, (
            f"Expected exactly 1 violation (the 30 mm trunk), got "
            f"{len(violations)}: {[v.message for v in violations]}"
        )
        # The surviving violation must be the 0.2 mm trunk, not a taper.
        assert "0.200mm" in violations[0].message

    def test_segment_endpoints_are_collected(self, neck_down_pcb: Path):
        """Regression: ``_collect_traces`` must record real endpoints.

        The historical implementation read ``segment.x1`` (nonexistent on
        ``schema.pcb.Segment``, which has ``start``/``end`` tuples) so all
        endpoints collected as (0, 0) -- which would make the neck-down
        exemption skip EVERY segment.
        """
        from kicad_tools.schema.pcb import PCB
        from kicad_tools.validate.rules.impedance import ImpedanceRule

        pcb = PCB.load(str(neck_down_pcb))
        rule = ImpedanceRule()
        traces = rule._collect_traces(pcb)
        lengths = sorted(
            ImpedanceRule._segment_length_mm(t) for t in traces["MIPI_RST"]
        )
        assert lengths == pytest.approx([0.5, 0.7, 30.0])
