"""Unit tests for the ampacity DRC rule (Issue #4217, Part 3 of #4215).

Exercises :class:`kicad_tools.validate.rules.ampacity.AmpacityRule` and the
:func:`kicad_tools.validate.ampacity_specs.derive_ampacity_specs` producer
helper against synthetic in-memory PCBs — no golden-board dependency.

The golden IPC-2221 widths (15 A / 2 oz / 10 C) come from
``tests/test_ampacity.py``:

* external (k=0.048) ~= 6.29 mm
* internal (k=0.024) ~= 16.37 mm

so a 15 A net class on 2 oz copper routed at 3 mm on ``F.Cu`` must be
flagged (3 < 6.29) and the same net routed at >= 6.3 mm must pass.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from kicad_tools.manufacturers import DesignRules
from kicad_tools.physics.ampacity import width_for_current
from kicad_tools.schema.pcb import PCB
from kicad_tools.validate.ampacity_specs import derive_ampacity_specs
from kicad_tools.validate.rules.ampacity import AmpacityRule

# --- Synthetic fixtures -------------------------------------------------


def _pcb_with_segment(width_mm: float, layer: str, net: str = "VBUS") -> str:
    """Build a minimal .kicad_pcb string with one routed segment."""
    layers_block = (
        '    (0 "F.Cu" signal)\n'
        '    (1 "In1.Cu" signal)\n'
        '    (2 "In2.Cu" signal)\n'
        '    (31 "B.Cu" signal)\n'
        '    (44 "Edge.Cuts" user)\n'
    )
    return f"""(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
{layers_block}  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "{net}")
  (gr_rect (start 100 100) (end 150 150)
    (stroke (width 0.1) (type default))
    (fill none)
    (layer "Edge.Cuts")
  )
  (segment (start 110 120) (end 140 120) (width {width_mm}) (layer "{layer}") (net 1)
    (uuid "00000000-0000-0000-0000-000000000030"))
)
"""


def _write_pcb(tmp_path: Path, text: str) -> PCB:
    pcb_file = tmp_path / "ampacity.kicad_pcb"
    pcb_file.write_text(text)
    return PCB.load(str(pcb_file))


def _design_rules_2oz() -> DesignRules:
    """DesignRules with 2 oz outer + inner copper (matches the golden)."""
    return DesignRules(
        min_trace_width_mm=0.127,
        min_clearance_mm=0.127,
        min_via_drill_mm=0.2,
        min_via_diameter_mm=0.45,
        min_annular_ring_mm=0.13,
        outer_copper_oz=2.0,
        inner_copper_oz=2.0,
    )


# --- derive_ampacity_specs ---------------------------------------------


@dataclass
class _FakeNetClass:
    """Minimal stand-in for NetClassRouting with just target_ampacity."""

    target_ampacity: float | None = None


class TestDeriveAmpacitySpecs:
    def test_none_map_returns_empty(self) -> None:
        assert derive_ampacity_specs(None) == {}

    def test_empty_map_returns_empty(self) -> None:
        assert derive_ampacity_specs({}) == {}

    def test_extracts_only_nets_with_target(self) -> None:
        net_map = {
            "VBUS": _FakeNetClass(target_ampacity=15.0),
            "SIGNAL": _FakeNetClass(target_ampacity=None),
        }
        assert derive_ampacity_specs(net_map) == {"VBUS": 15.0}


# --- AmpacityRule -------------------------------------------------------


class TestAmpacityRule:
    def test_under_width_external_segment_flagged(self, tmp_path: Path) -> None:
        """15 A / 2 oz / F.Cu routed at 3 mm -> 1 error, required ~6.29 mm."""
        pcb = _write_pcb(tmp_path, _pcb_with_segment(3.0, "F.Cu"))
        rule = AmpacityRule(specs={"VBUS": 15.0})
        results = rule.check(pcb, _design_rules_2oz())

        assert len(results.errors) == 1
        v = results.errors[0]
        assert v.rule_id == "ampacity"
        assert v.severity == "error"
        assert v.layer == "F.Cu"
        assert v.items == ("VBUS",)
        assert v.actual_value == pytest.approx(3.0)
        assert v.required_value == pytest.approx(6.29, abs=0.05)

    def test_compliant_external_segment_passes(self, tmp_path: Path) -> None:
        """Same net routed at 6.3 mm (>= required) -> no findings."""
        pcb = _write_pcb(tmp_path, _pcb_with_segment(6.3, "F.Cu"))
        rule = AmpacityRule(specs={"VBUS": 15.0})
        results = rule.check(pcb, _design_rules_2oz())
        assert results.errors == []

    def test_no_target_not_checked(self, tmp_path: Path) -> None:
        """A net with no ampacity target is never checked, regardless of width."""
        pcb = _write_pcb(tmp_path, _pcb_with_segment(0.2, "F.Cu"))
        rule = AmpacityRule(specs={})  # no target for VBUS
        results = rule.check(pcb, _design_rules_2oz())
        assert results.errors == []

    def test_boundary_width_passes(self, tmp_path: Path) -> None:
        """Width exactly at the required floor passes (>=, not >)."""
        required = width_for_current(15.0, copper_weight_oz=2.0, layer="external")
        pcb = _write_pcb(tmp_path, _pcb_with_segment(required, "F.Cu"))
        rule = AmpacityRule(specs={"VBUS": 15.0})
        results = rule.check(pcb, _design_rules_2oz())
        assert results.errors == []

    def test_internal_layer_uses_inner_copper_weight(self, tmp_path: Path) -> None:
        """An internal-layer segment uses the wider internal (k=0.024) floor.

        A 3 mm internal segment is flagged, and its required width matches
        the internal golden (~16.37 mm) — measurably larger than the
        external requirement (~6.29 mm) at identical current / copper
        weight.
        """
        pcb = _write_pcb(tmp_path, _pcb_with_segment(3.0, "In1.Cu"))
        rule = AmpacityRule(specs={"VBUS": 15.0})
        results = rule.check(pcb, _design_rules_2oz())

        assert len(results.errors) == 1
        v = results.errors[0]
        assert v.layer == "In1.Cu"
        assert v.required_value == pytest.approx(16.37, abs=0.05)
        # Internal requirement is strictly wider than external at same inputs.
        external_req = width_for_current(15.0, copper_weight_oz=2.0, layer="external")
        assert v.required_value > external_req

    def test_multi_segment_only_underwidth_flagged(self, tmp_path: Path) -> None:
        """Two segments on the same net, one wide one narrow -> exactly 1 error."""
        text = """(kicad_pcb
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
  (net 1 "VBUS")
  (gr_rect (start 100 100) (end 200 200)
    (stroke (width 0.1) (type default))
    (fill none)
    (layer "Edge.Cuts")
  )
  (segment (start 110 120) (end 140 120) (width 7.0) (layer "F.Cu") (net 1)
    (uuid "00000000-0000-0000-0000-000000000031"))
  (segment (start 110 130) (end 140 130) (width 3.0) (layer "F.Cu") (net 1)
    (uuid "00000000-0000-0000-0000-000000000032"))
)
"""
        pcb = _write_pcb(tmp_path, text)
        rule = AmpacityRule(specs={"VBUS": 15.0})
        results = rule.check(pcb, _design_rules_2oz())

        assert len(results.errors) == 1
        # The flagged segment is the 3.0 mm one.  The rule reports a
        # board-relative midpoint (board_origin at the edge-cut corner
        # 100,100 is subtracted by PCB.load), so its y is 130 - 100 = 30.
        v = results.errors[0]
        assert v.actual_value == pytest.approx(3.0)
        assert v.location is not None
        assert v.location[1] == pytest.approx(30.0, abs=1e-6)

    def test_empty_specs_no_op(self, tmp_path: Path) -> None:
        pcb = _write_pcb(tmp_path, _pcb_with_segment(0.1, "F.Cu"))
        rule = AmpacityRule()  # specs=None -> {}
        results = rule.check(pcb, _design_rules_2oz())
        assert results.errors == []


class TestCheckDruAgreement:
    """The check's required width must equal the DRU generator's width.

    Both call ``width_for_current`` with the identical copper-weight /
    layer split, so for a given (net, copper-weight, layer) the check's
    ``required_value`` and the DRU generator's emitted min-width agree.
    """

    def test_check_required_width_matches_dru_external(self, tmp_path: Path) -> None:
        from kicad_tools.manufacturers.dru_generator import generate_dru
        from kicad_tools.router.rules import NetClassRouting

        design_rules = _design_rules_2oz()
        nc = NetClassRouting(name="POWER", target_ampacity=15.0)

        # DRU-emitted external width for this class.
        dru_text = generate_dru(design_rules, net_classes=[nc])
        lines = dru_text.splitlines()
        # Find the external rule block, then the following track_width
        # constraint line: (constraint track_width (min X.XXXXmm)).
        idx = next(
            i for i, line in enumerate(lines) if "Ampacity Min Width (POWER, external)" in line
        )
        constraint_line = next(line for line in lines[idx:] if "track_width" in line)
        dru_width = float(constraint_line.split("min ")[1].split("mm")[0])

        # The check's derived required width for the same inputs.
        rule = AmpacityRule(specs={"POWER": 15.0})
        pcb = _write_pcb(tmp_path, _pcb_with_segment(0.5, "F.Cu", net="POWER"))
        results = rule.check(pcb, design_rules)
        assert len(results.errors) == 1
        check_width = results.errors[0].required_value

        assert check_width == pytest.approx(dru_width, abs=1e-4)
