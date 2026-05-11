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
