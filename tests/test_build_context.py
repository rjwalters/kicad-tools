"""
Tests for YAMLPattern._build_context() PCB parsing.

Verifies that _build_context() uses PCB.load() to extract real
component_positions, component_values, component_footprints, and
net_lengths from PCB files.
"""

from pathlib import Path

import pytest

from kicad_tools.patterns.checks import CheckContext
from kicad_tools.patterns.loader import PatternLoader, YAMLPattern


# Minimal PCB with two footprints and a segment for testing
PCB_TWO_COMPONENTS = """\
(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
    (36 "B.SilkS" user "B.Silkscreen")
    (37 "F.SilkS" user "F.Silkscreen")
    (48 "B.Fab" user)
    (49 "F.Fab" user)
  )
  (setup
    (pad_to_mask_clearance 0)
  )
  (net 0 "")
  (net 1 "VCC")
  (net 2 "GND")
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000001")
    (at 10 20)
    (property "Reference" "R1"
      (at 0 -1.5 0) (layer "F.SilkS")
      (uuid "00000000-0000-0000-0000-000000000002"))
    (property "Value" "10k"
      (at 0 1.5 0) (layer "F.Fab")
      (uuid "00000000-0000-0000-0000-000000000003"))
    (property "Footprint" "Resistor_SMD:R_0402_1005Metric"
      (at 0 0 0) (layer "F.Fab") (hide yes)
      (uuid "00000000-0000-0000-0000-000000000004"))
    (pad "1" smd roundrect (at -0.5 0) (size 0.5 0.6)
      (layers "F.Cu") (roundrect_rratio 0.25) (net 1 "VCC"))
    (pad "2" smd roundrect (at 0.5 0) (size 0.5 0.6)
      (layers "F.Cu") (roundrect_rratio 0.25) (net 2 "GND"))
  )
  (footprint "Capacitor_SMD:C_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000010")
    (at 20 20)
    (property "Reference" "C1"
      (at 0 -1.5 0) (layer "F.SilkS")
      (uuid "00000000-0000-0000-0000-000000000011"))
    (property "Value" "100nF"
      (at 0 1.5 0) (layer "F.Fab")
      (uuid "00000000-0000-0000-0000-000000000012"))
    (property "Footprint" "Capacitor_SMD:C_0402_1005Metric"
      (at 0 0 0) (layer "F.Fab") (hide yes)
      (uuid "00000000-0000-0000-0000-000000000013"))
    (pad "1" smd roundrect (at -0.5 0) (size 0.5 0.6)
      (layers "F.Cu") (roundrect_rratio 0.25) (net 1 "VCC"))
    (pad "2" smd roundrect (at 0.5 0) (size 0.5 0.6)
      (layers "F.Cu") (roundrect_rratio 0.25) (net 2 "GND"))
  )
  (segment (start 10 20) (end 20 20) (width 0.2) (layer "F.Cu") (net 1)
    (uuid "00000000-0000-0000-0000-000000000020"))
  (segment (start 10 20) (end 10 25) (width 0.2) (layer "F.Cu") (net 2)
    (uuid "00000000-0000-0000-0000-000000000021"))
)
"""

# PCB with footprints but no routed segments
PCB_NO_TRACES = """\
(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
    (36 "B.SilkS" user "B.Silkscreen")
    (37 "F.SilkS" user "F.Silkscreen")
    (48 "B.Fab" user)
    (49 "F.Fab" user)
  )
  (setup
    (pad_to_mask_clearance 0)
  )
  (net 0 "")
  (net 1 "VCC")
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000030")
    (at 50 60)
    (property "Reference" "R2"
      (at 0 -1.5 0) (layer "F.SilkS")
      (uuid "00000000-0000-0000-0000-000000000031"))
    (property "Value" ""
      (at 0 1.5 0) (layer "F.Fab")
      (uuid "00000000-0000-0000-0000-000000000032"))
    (property "Footprint" "Resistor_SMD:R_0402_1005Metric"
      (at 0 0 0) (layer "F.Fab") (hide yes)
      (uuid "00000000-0000-0000-0000-000000000033"))
  )
)
"""

SIMPLE_PATTERN_YAML = """\
name: test_distance
description: Pattern with distance check
components:
  - role: resistor
    reference_prefix: R
  - role: capacitor
    reference_prefix: C
validation:
  - check: component_distance
    params:
      from_component: R1
      to_component: C1
      max_mm: 5.0
"""


def _make_pattern() -> YAMLPattern:
    """Create a simple YAMLPattern for testing."""
    loader = PatternLoader()
    pattern, _ = loader.load_string(SIMPLE_PATTERN_YAML)
    return pattern


class TestBuildContextPopulated:
    """Tests for _build_context with a real PCB file."""

    def test_component_positions_populated(self, tmp_path: Path) -> None:
        """component_positions contains (x, y) tuples keyed by reference."""
        pcb_file = tmp_path / "test.kicad_pcb"
        pcb_file.write_text(PCB_TWO_COMPONENTS)

        pattern = _make_pattern()
        ctx = pattern._build_context(pcb_file)

        assert "R1" in ctx.component_positions
        assert "C1" in ctx.component_positions
        assert ctx.component_positions["R1"] == pytest.approx((10.0, 20.0))
        assert ctx.component_positions["C1"] == pytest.approx((20.0, 20.0))

    def test_component_values_populated(self, tmp_path: Path) -> None:
        """component_values contains value strings keyed by reference."""
        pcb_file = tmp_path / "test.kicad_pcb"
        pcb_file.write_text(PCB_TWO_COMPONENTS)

        pattern = _make_pattern()
        ctx = pattern._build_context(pcb_file)

        assert ctx.component_values["R1"] == "10k"
        assert ctx.component_values["C1"] == "100nF"

    def test_component_footprints_populated(self, tmp_path: Path) -> None:
        """component_footprints contains footprint library names keyed by reference."""
        pcb_file = tmp_path / "test.kicad_pcb"
        pcb_file.write_text(PCB_TWO_COMPONENTS)

        pattern = _make_pattern()
        ctx = pattern._build_context(pcb_file)

        assert "R_0402_1005Metric" in ctx.component_footprints["R1"]
        assert "C_0402_1005Metric" in ctx.component_footprints["C1"]

    def test_net_lengths_populated(self, tmp_path: Path) -> None:
        """net_lengths contains trace lengths in mm for routed nets."""
        pcb_file = tmp_path / "test.kicad_pcb"
        pcb_file.write_text(PCB_TWO_COMPONENTS)

        pattern = _make_pattern()
        ctx = pattern._build_context(pcb_file)

        # VCC net: segment from (10,20) to (20,20) = 10mm
        assert "VCC" in ctx.net_lengths
        assert ctx.net_lengths["VCC"] == pytest.approx(10.0)
        # GND net: segment from (10,20) to (10,25) = 5mm
        assert "GND" in ctx.net_lengths
        assert ctx.net_lengths["GND"] == pytest.approx(5.0)

    def test_pcb_path_set(self, tmp_path: Path) -> None:
        """pcb_path is set in the returned context."""
        pcb_file = tmp_path / "test.kicad_pcb"
        pcb_file.write_text(PCB_TWO_COMPONENTS)

        pattern = _make_pattern()
        ctx = pattern._build_context(pcb_file)

        assert ctx.pcb_path == pcb_file

    def test_context_is_check_context(self, tmp_path: Path) -> None:
        """Returned object is a CheckContext instance."""
        pcb_file = tmp_path / "test.kicad_pcb"
        pcb_file.write_text(PCB_TWO_COMPONENTS)

        pattern = _make_pattern()
        ctx = pattern._build_context(pcb_file)

        assert isinstance(ctx, CheckContext)


class TestBuildContextMissingFile:
    """Tests for _build_context when PCB file does not exist."""

    def test_returns_empty_context(self) -> None:
        """Returns empty CheckContext when PCB file is missing."""
        pattern = _make_pattern()
        with pytest.warns(UserWarning, match="PCB file not found"):
            ctx = pattern._build_context(Path("nonexistent.kicad_pcb"))

        assert ctx.component_positions == {}
        assert ctx.component_values == {}
        assert ctx.component_footprints == {}
        assert ctx.net_lengths == {}

    def test_pcb_path_still_set(self) -> None:
        """pcb_path is set even when file is missing."""
        pattern = _make_pattern()
        with pytest.warns(UserWarning, match="PCB file not found"):
            ctx = pattern._build_context("missing.kicad_pcb")

        assert ctx.pcb_path == Path("missing.kicad_pcb")


class TestBuildContextInvalidFile:
    """Tests for _build_context when PCB file is invalid."""

    def test_returns_empty_context_on_parse_error(self, tmp_path: Path) -> None:
        """Returns empty CheckContext when PCB file cannot be parsed."""
        bad_file = tmp_path / "bad.kicad_pcb"
        bad_file.write_text("this is not a valid kicad pcb file")

        pattern = _make_pattern()
        with pytest.warns(UserWarning, match="Failed to parse"):
            ctx = pattern._build_context(bad_file)

        assert ctx.component_positions == {}
        assert ctx.component_values == {}
        assert ctx.component_footprints == {}
        assert ctx.net_lengths == {}


class TestBuildContextEdgeCases:
    """Edge case tests for _build_context."""

    def test_no_traces(self, tmp_path: Path) -> None:
        """net_lengths is empty when PCB has footprints but no routed traces."""
        pcb_file = tmp_path / "test.kicad_pcb"
        pcb_file.write_text(PCB_NO_TRACES)

        pattern = _make_pattern()
        ctx = pattern._build_context(pcb_file)

        assert ctx.net_lengths == {}
        # Footprint should still be found
        assert "R2" in ctx.component_positions
        assert ctx.component_positions["R2"] == pytest.approx((50.0, 60.0))

    def test_empty_value(self, tmp_path: Path) -> None:
        """Footprints with empty value still appear in component_values."""
        pcb_file = tmp_path / "test.kicad_pcb"
        pcb_file.write_text(PCB_NO_TRACES)

        pattern = _make_pattern()
        ctx = pattern._build_context(pcb_file)

        assert "R2" in ctx.component_values
        assert ctx.component_values["R2"] == ""

    def test_string_path(self, tmp_path: Path) -> None:
        """_build_context accepts string paths."""
        pcb_file = tmp_path / "test.kicad_pcb"
        pcb_file.write_text(PCB_TWO_COMPONENTS)

        pattern = _make_pattern()
        ctx = pattern._build_context(str(pcb_file))

        assert "R1" in ctx.component_positions


class TestEndToEndValidation:
    """End-to-end tests: validate() produces real violations from PCB data."""

    def test_violation_when_components_too_far(self, tmp_path: Path) -> None:
        """validate() returns violation when components exceed max distance."""
        pcb_file = tmp_path / "test.kicad_pcb"
        pcb_file.write_text(PCB_TWO_COMPONENTS)

        # R1 is at (10,20), C1 is at (20,20) = 10mm apart
        # Pattern check has max_mm=5, so this should violate
        pattern = _make_pattern()
        violations = pattern.validate(pcb_file)

        distance_violations = [v for v in violations if "too far" in v.message]
        assert len(distance_violations) == 1
        assert "10.00mm" in distance_violations[0].message
        assert "5.00mm" in distance_violations[0].message

    def test_no_violation_when_within_distance(self, tmp_path: Path) -> None:
        """validate() returns no distance violation when within limit."""
        # Create a PCB where R1 and C1 are 3mm apart (within 5mm max)
        close_pcb = PCB_TWO_COMPONENTS.replace("(at 20 20)", "(at 13 20)")
        pcb_file = tmp_path / "test.kicad_pcb"
        pcb_file.write_text(close_pcb)

        pattern = _make_pattern()
        violations = pattern.validate(pcb_file)

        distance_violations = [v for v in violations if "too far" in v.message]
        assert len(distance_violations) == 0

    def test_validate_with_missing_file_returns_empty(self) -> None:
        """validate() with missing PCB returns empty violations gracefully."""
        pattern = _make_pattern()
        with pytest.warns(UserWarning, match="PCB file not found"):
            violations = pattern.validate("nonexistent.kicad_pcb")

        # With empty context, no component positions found, so distance
        # checks will report "not found" violations (not crashes)
        assert isinstance(violations, list)


class TestBackwardCompatibility:
    """Verify existing test patterns still work with the updated code."""

    def test_dummy_path_returns_empty_context(self) -> None:
        """Dummy paths (as used by existing tests) return empty context."""
        pattern = _make_pattern()
        with pytest.warns(UserWarning, match="PCB file not found"):
            ctx = pattern._build_context("dummy.kicad_pcb")

        assert ctx.component_positions == {}

    def test_existing_validate_with_dummy_path(self) -> None:
        """validate() with dummy path does not crash."""
        loader = PatternLoader()
        pattern, _ = loader.load_string("""
name: basic
components:
  - role: r
""")
        with pytest.warns(UserWarning, match="PCB file not found"):
            violations = pattern.validate("dummy.kicad_pcb")
        assert isinstance(violations, list)
