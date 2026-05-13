"""Tests for ``kct stitch`` manufacturer-aware via dimension resolution.

Covers issue #2848: ``kct stitch`` was completely manufacturer-unaware and
silently injected jlcpcb-tier1 *4-layer* minima (0.45/0.20 mm) into
*2-layer* PCBs, producing 14 DRC violations on board 04
(`dimension_via_drill` / `dimension_via_diameter` / `dimension_annular_ring`).

The fix adds a ``--mfr`` / ``--manufacturer`` flag that, when set, resolves
via dimensions from the manufacturer YAML profile keyed on the board's
actual copper layer count.  When ``--mfr`` is *not* set the previous
defaults are preserved for backwards compatibility.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_tools.cli.stitch_cmd import (
    _count_copper_layers,
    _resolve_mfr_via_dimensions,
    main,
)

# Minimal 2-layer PCB with a GND zone and one SMD pad on GND.  Chosen so a
# single stitching via is produced on the F.Cu surface, with the via
# dimensions driven entirely by the CLI / mfr resolution path.
TWO_LAYER_GND_ZONE_PCB = """(kicad_pcb
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
  (net 1 "GND")
  (footprint "Capacitor_SMD:C_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000100")
    (at 110 110)
    (property "Reference" "C1" (at 0 -1.5 0) (layer "F.SilkS") (uuid "ref-1"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 1 "GND"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 0 ""))
  )
  (zone (net 1) (net_name "GND") (layer "B.Cu") (uuid "zone-1") (hatch edge 0.5)
    (connect_pads (clearance 0.2))
    (min_thickness 0.2) (filled_areas_thickness no)
    (fill yes (thermal_gap 0.2) (thermal_bridge_width 0.3))
    (polygon (pts (xy 100 100) (xy 140 100) (xy 140 140) (xy 100 140)))
  )
)
"""


# 4-layer PCB with a GND zone — identical pad/zone geometry, but the
# stackup is declared with two inner layers so layer-count detection
# returns 4.
FOUR_LAYER_GND_ZONE_PCB = """(kicad_pcb
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
    (44 "Edge.Cuts" user)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "GND")
  (footprint "Capacitor_SMD:C_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000100")
    (at 110 110)
    (property "Reference" "C1" (at 0 -1.5 0) (layer "F.SilkS") (uuid "ref-1"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 1 "GND"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 0 ""))
  )
  (zone (net 1) (net_name "GND") (layer "In1.Cu") (uuid "zone-1") (hatch edge 0.5)
    (connect_pads (clearance 0.2))
    (min_thickness 0.2) (filled_areas_thickness no)
    (fill yes (thermal_gap 0.2) (thermal_bridge_width 0.3))
    (polygon (pts (xy 100 100) (xy 140 100) (xy 140 140) (xy 100 140)))
  )
)
"""


@pytest.fixture
def two_layer_pcb(tmp_path: Path) -> Path:
    """Create a 2-layer test PCB with a GND zone."""
    pcb_file = tmp_path / "two_layer.kicad_pcb"
    pcb_file.write_text(TWO_LAYER_GND_ZONE_PCB)
    return pcb_file


@pytest.fixture
def four_layer_pcb(tmp_path: Path) -> Path:
    """Create a 4-layer test PCB with a GND zone."""
    pcb_file = tmp_path / "four_layer.kicad_pcb"
    pcb_file.write_text(FOUR_LAYER_GND_ZONE_PCB)
    return pcb_file


class TestCountCopperLayers:
    """Sanity tests for the copper-layer detection helper."""

    def test_two_layer_board(self, two_layer_pcb: Path) -> None:
        assert _count_copper_layers(two_layer_pcb) == 2

    def test_four_layer_board(self, four_layer_pcb: Path) -> None:
        assert _count_copper_layers(four_layer_pcb) == 4

    def test_missing_file_falls_back_to_two(self, tmp_path: Path) -> None:
        # _count_copper_layers swallows exceptions and returns the
        # 2-layer default; the caller surfaces missing-file errors via
        # its own path existence check.
        assert _count_copper_layers(tmp_path / "nope.kicad_pcb") == 2


class TestResolveMfrViaDimensions:
    """Direct unit tests of the YAML-driven via dimension resolver."""

    def test_jlcpcb_tier1_two_layer(self) -> None:
        """jlcpcb-tier1 / 2-layer should yield 0.6mm/0.3mm (2-layer minima)."""
        diameter, drill = _resolve_mfr_via_dimensions("jlcpcb-tier1", 2)
        assert diameter == pytest.approx(0.6)
        assert drill == pytest.approx(0.3)

    def test_jlcpcb_tier1_four_layer(self) -> None:
        """jlcpcb-tier1 / 4-layer should yield 0.45mm/0.2mm (4-layer minima)."""
        diameter, drill = _resolve_mfr_via_dimensions("jlcpcb-tier1", 4)
        assert diameter == pytest.approx(0.45)
        assert drill == pytest.approx(0.2)

    def test_jlcpcb_two_layer(self) -> None:
        """Base jlcpcb profile / 2-layer should yield 0.6mm/0.3mm."""
        diameter, drill = _resolve_mfr_via_dimensions("jlcpcb", 2)
        assert diameter == pytest.approx(0.6)
        assert drill == pytest.approx(0.3)

    def test_underscore_alias_resolves(self) -> None:
        """jlcpcb_tier1 (alias spelling) must resolve via the dash-form YAML."""
        diameter, drill = _resolve_mfr_via_dimensions("jlcpcb_tier1", 2)
        assert diameter == pytest.approx(0.6)
        assert drill == pytest.approx(0.3)

    def test_unknown_mfr_raises(self) -> None:
        """An unknown manufacturer surfaces FileNotFoundError."""
        with pytest.raises(FileNotFoundError):
            _resolve_mfr_via_dimensions("does-not-exist", 2)

    def test_annular_ring_constraint_dominates(self) -> None:
        """If drill + 2*annular > min_diameter, the diameter is enlarged.

        For jlcpcb-tier1 / 2-layer: drill=0.3, annular=0.15 -> floor=0.6 == min_diameter.
        This test pins the contract that the resolver always satisfies
        BOTH the diameter minimum and the annular ring minimum
        simultaneously (matches fix_vias_cmd's resolution semantics).
        """
        diameter, drill = _resolve_mfr_via_dimensions("jlcpcb-tier1", 2)
        from kicad_tools.manufacturers.base import load_design_rules_from_yaml

        rules = load_design_rules_from_yaml("jlcpcb_tier1")["2layer_1oz"]
        assert diameter >= rules.min_via_diameter_mm
        assert diameter >= drill + 2 * rules.min_annular_ring_mm


class TestStitchMainMfrFlag:
    """End-to-end tests through ``main()`` with the new ``--mfr`` flag."""

    def test_mfr_jlcpcb_tier1_two_layer_emits_2layer_minima(
        self, two_layer_pcb: Path, tmp_path: Path
    ) -> None:
        """Stitching a 2-layer board with --mfr jlcpcb-tier1 must emit 0.6/0.3 vias."""
        output_file = tmp_path / "out.kicad_pcb"
        exit_code = main(
            [
                str(two_layer_pcb),
                "--net",
                "GND",
                "--output",
                str(output_file),
                "--mfr",
                "jlcpcb-tier1",
            ]
        )
        assert exit_code == 0

        text = output_file.read_text()
        # Acceptance from issue #2848:
        #   * via diameter >= 0.6 mm (2-layer jlcpcb-tier1 minimum)
        #   * via drill    >= 0.3 mm (2-layer jlcpcb-tier1 minimum)
        # The 4-layer minima (0.45 / 0.2) must NOT appear on this board.
        assert "(size 0.6)" in text or "(size 0.600)" in text, (
            "Expected 2-layer jlcpcb-tier1 via diameter (0.6mm) in output"
        )
        assert "(drill 0.3)" in text or "(drill 0.300)" in text, (
            "Expected 2-layer jlcpcb-tier1 via drill (0.3mm) in output"
        )
        # Guard against silent regression to the old hardcoded 4-layer
        # defaults.
        assert "(size 0.45)" not in text, (
            "Stitch should not emit jlcpcb-tier1 4-layer via diameter (0.45mm) on a 2-layer board"
        )
        assert "(drill 0.2)" not in text, (
            "Stitch should not emit jlcpcb-tier1 4-layer via drill (0.2mm) on a 2-layer board"
        )

    def test_mfr_jlcpcb_tier1_four_layer_emits_4layer_minima(
        self, four_layer_pcb: Path, tmp_path: Path
    ) -> None:
        """Stitching a 4-layer board with --mfr jlcpcb-tier1 must emit 0.45/0.2 vias."""
        output_file = tmp_path / "out.kicad_pcb"
        exit_code = main(
            [
                str(four_layer_pcb),
                "--net",
                "GND",
                "--output",
                str(output_file),
                "--mfr",
                "jlcpcb-tier1",
            ]
        )
        assert exit_code == 0

        text = output_file.read_text()
        # 4-layer jlcpcb-tier1 minima: 0.45 mm diameter / 0.20 mm drill.
        assert "(size 0.45)" in text or "(size 0.450)" in text, (
            "Expected 4-layer jlcpcb-tier1 via diameter (0.45mm) in output"
        )
        assert "(drill 0.2)" in text or "(drill 0.200)" in text, (
            "Expected 4-layer jlcpcb-tier1 via drill (0.2mm) in output"
        )

    def test_no_mfr_preserves_existing_defaults(
        self, two_layer_pcb: Path, tmp_path: Path
    ) -> None:
        """Without --mfr, the historical CLI defaults (0.45/0.2) are preserved.

        This pins the contract from Curator guidance: "Default behavior
        unchanged when --mfr not passed."  Raising the unqualified
        defaults to 2-layer-safe values is intentionally deferred to a
        separate follow-up (per Curator's narrowed scope).
        """
        output_file = tmp_path / "out.kicad_pcb"
        exit_code = main(
            [
                str(two_layer_pcb),
                "--net",
                "GND",
                "--output",
                str(output_file),
            ]
        )
        assert exit_code == 0

        text = output_file.read_text()
        # Existing defaults: 0.45 / 0.20.
        assert "(size 0.45)" in text or "(size 0.450)" in text, (
            "Default (no --mfr) stitch must preserve historical via diameter (0.45mm)"
        )
        assert "(drill 0.2)" in text or "(drill 0.200)" in text, (
            "Default (no --mfr) stitch must preserve historical via drill (0.2mm)"
        )

    def test_unknown_mfr_returns_error(
        self, two_layer_pcb: Path, tmp_path: Path, capsys
    ) -> None:
        """Unknown manufacturer surfaces a clear error and exits non-zero."""
        output_file = tmp_path / "out.kicad_pcb"
        exit_code = main(
            [
                str(two_layer_pcb),
                "--net",
                "GND",
                "--output",
                str(output_file),
                "--mfr",
                "does-not-exist",
            ]
        )
        assert exit_code == 1
        captured = capsys.readouterr()
        assert "does-not-exist" in captured.err

    def test_mfr_short_alias_route_cmd_compat(
        self, two_layer_pcb: Path, tmp_path: Path
    ) -> None:
        """The --manufacturer alias matches route_cmd's spelling and works identically."""
        output_file = tmp_path / "out.kicad_pcb"
        exit_code = main(
            [
                str(two_layer_pcb),
                "--net",
                "GND",
                "--output",
                str(output_file),
                "--manufacturer",
                "jlcpcb-tier1",
            ]
        )
        assert exit_code == 0
        text = output_file.read_text()
        assert "(size 0.6)" in text or "(size 0.600)" in text
        assert "(drill 0.3)" in text or "(drill 0.300)" in text
