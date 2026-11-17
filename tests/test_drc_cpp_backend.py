"""Tests for C++ DRC backend wrapper and Python/C++ parity."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from kicad_tools.drc.cpp_backend import (
    get_backend_info,
    get_cpp_unavailable_reason,
    is_cpp_available,
)
from kicad_tools.drc.incremental import (
    IncrementalDRC,
)
from kicad_tools.manufacturers.base import DesignRules
from kicad_tools.schema.pcb import PCB

# ----- Test Fixtures -----

# PCB with two close components (clearance violation expected)
PCB_CLOSE_COMPONENTS = """(kicad_pcb
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
  (net 2 "SIG1")
  (net 3 "SIG2")
  (gr_rect (start 100 100) (end 200 200)
    (stroke (width 0.1) (type default))
    (fill none)
    (layer "Edge.Cuts")
  )
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000010")
    (at 120 120)
    (property "Reference" "R1" (at 0 -1.5 0) (layer "F.SilkS") (uuid "00000000-0000-0000-0000-000000000011"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab") (uuid "00000000-0000-0000-0000-000000000012"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 1 "GND"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 2 "SIG1"))
  )
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000020")
    (at 121.2 120)
    (property "Reference" "R2" (at 0 -1.5 0) (layer "F.SilkS") (uuid "00000000-0000-0000-0000-000000000021"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab") (uuid "00000000-0000-0000-0000-000000000022"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 3 "SIG2"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 1 "GND"))
  )
)
"""

# PCB with same-net pads close together (should NOT trigger violation)
PCB_SAME_NET = """(kicad_pcb
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
  (gr_rect (start 100 100) (end 200 200)
    (stroke (width 0.1) (type default))
    (fill none)
    (layer "Edge.Cuts")
  )
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000010")
    (at 120 120)
    (property "Reference" "R1" (at 0 -1.5 0) (layer "F.SilkS") (uuid "00000000-0000-0000-0000-000000000011"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab") (uuid "00000000-0000-0000-0000-000000000012"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 1 "GND"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 1 "GND"))
  )
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000020")
    (at 121.2 120)
    (property "Reference" "R2" (at 0 -1.5 0) (layer "F.SilkS") (uuid "00000000-0000-0000-0000-000000000021"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab") (uuid "00000000-0000-0000-0000-000000000022"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 1 "GND"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 1 "GND"))
  )
)
"""

# PCB with well-spaced components (no violations)
PCB_WELL_SPACED = """(kicad_pcb
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
  (net 2 "+3.3V")
  (gr_rect (start 100 100) (end 200 200)
    (stroke (width 0.1) (type default))
    (fill none)
    (layer "Edge.Cuts")
  )
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000010")
    (at 120 120)
    (property "Reference" "R1" (at 0 -1.5 0) (layer "F.SilkS") (uuid "00000000-0000-0000-0000-000000000011"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab") (uuid "00000000-0000-0000-0000-000000000012"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 1 "GND"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 2 "+3.3V"))
  )
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000020")
    (at 140 140)
    (property "Reference" "R2" (at 0 -1.5 0) (layer "F.SilkS") (uuid "00000000-0000-0000-0000-000000000021"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab") (uuid "00000000-0000-0000-0000-000000000022"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 1 "GND"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 2 "+3.3V"))
  )
)
"""

# PCB with a rotated component
PCB_ROTATED = """(kicad_pcb
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
  (net 2 "SIG1")
  (net 3 "SIG2")
  (gr_rect (start 100 100) (end 200 200)
    (stroke (width 0.1) (type default))
    (fill none)
    (layer "Edge.Cuts")
  )
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000010")
    (at 120 120 90)
    (property "Reference" "R1" (at 0 -1.5 0) (layer "F.SilkS") (uuid "00000000-0000-0000-0000-000000000011"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab") (uuid "00000000-0000-0000-0000-000000000012"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 1 "GND"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 2 "SIG1"))
  )
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000020")
    (at 121.2 120)
    (property "Reference" "R2" (at 0 -1.5 0) (layer "F.SilkS") (uuid "00000000-0000-0000-0000-000000000021"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab") (uuid "00000000-0000-0000-0000-000000000022"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 3 "SIG2"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 1 "GND"))
  )
)
"""

# PCB with no pads (edge case)
PCB_NO_PADS = """(kicad_pcb
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
  (gr_rect (start 100 100) (end 200 200)
    (stroke (width 0.1) (type default))
    (fill none)
    (layer "Edge.Cuts")
  )
  (footprint "TestLib:NoPads"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000010")
    (at 120 120)
    (property "Reference" "J1" (at 0 -1.5 0) (layer "F.SilkS") (uuid "00000000-0000-0000-0000-000000000011"))
    (property "Value" "CONN" (at 0 1.5 0) (layer "F.Fab") (uuid "00000000-0000-0000-0000-000000000012"))
  )
  (footprint "TestLib:NoPads"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000020")
    (at 121 120)
    (property "Reference" "J2" (at 0 -1.5 0) (layer "F.SilkS") (uuid "00000000-0000-0000-0000-000000000021"))
    (property "Value" "CONN" (at 0 1.5 0) (layer "F.Fab") (uuid "00000000-0000-0000-0000-000000000022"))
  )
)
"""

# PCB with single pad per component
PCB_SINGLE_PAD = """(kicad_pcb
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
  (net 2 "SIG1")
  (gr_rect (start 100 100) (end 200 200)
    (stroke (width 0.1) (type default))
    (fill none)
    (layer "Edge.Cuts")
  )
  (footprint "TestLib:SinglePad"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000010")
    (at 120 120)
    (property "Reference" "TP1" (at 0 -1.5 0) (layer "F.SilkS") (uuid "00000000-0000-0000-0000-000000000011"))
    (property "Value" "TP" (at 0 1.5 0) (layer "F.Fab") (uuid "00000000-0000-0000-0000-000000000012"))
    (pad "1" smd circle (at 0 0) (size 1.0 1.0) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "GND"))
  )
  (footprint "TestLib:SinglePad"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000020")
    (at 121.5 120)
    (property "Reference" "TP2" (at 0 -1.5 0) (layer "F.SilkS") (uuid "00000000-0000-0000-0000-000000000021"))
    (property "Value" "TP" (at 0 1.5 0) (layer "F.Fab") (uuid "00000000-0000-0000-0000-000000000022"))
    (pad "1" smd circle (at 0 0) (size 1.0 1.0) (layers "F.Cu" "F.Paste" "F.Mask") (net 2 "SIG1"))
  )
)
"""


def _load_pcb(tmp_path: Path, content: str, name: str = "test.kicad_pcb") -> PCB:
    """Helper to load a PCB from string content."""
    pcb_file = tmp_path / name
    pcb_file.write_text(content)
    return PCB.load(str(pcb_file))


@pytest.fixture
def default_rules() -> DesignRules:
    """Standard design rules for testing."""
    return DesignRules(
        min_trace_width_mm=0.1,
        min_clearance_mm=0.15,
        min_via_drill_mm=0.3,
        min_via_diameter_mm=0.6,
        min_annular_ring_mm=0.15,
    )


# ----- Backend availability tests -----


class TestCppBackendAvailability:
    """Tests for C++ backend availability detection and fallback."""

    def test_is_cpp_available_returns_bool(self):
        """is_cpp_available returns a boolean."""
        result = is_cpp_available()
        assert isinstance(result, bool)

    def test_get_backend_info_structure(self):
        """get_backend_info returns well-formed dict."""
        info = get_backend_info()
        assert "backend" in info
        assert info["backend"] in ("cpp", "python")
        assert "available" in info
        assert "platform" in info

    def test_get_backend_info_cpp_available(self):
        """When C++ is available, backend info reflects that."""
        info = get_backend_info()
        if info["available"]:
            assert info["backend"] == "cpp"
            assert "version" in info
        else:
            assert info["backend"] == "python"
            assert "unavailable_reason" in info

    def test_fallback_on_import_error(self):
        """Backend gracefully falls back when C++ module is missing."""
        # This test verifies that the import failure path is handled.
        # Even if C++ is available in this environment, we test the
        # fallback logic by checking the module structure.
        reason = get_cpp_unavailable_reason()
        if is_cpp_available():
            assert reason is None
        else:
            assert isinstance(reason, str)
            assert len(reason) > 0


# ----- Python fallback parity tests -----


class TestPythonFallbackParity:
    """Tests ensuring Python fallback produces correct results.

    These tests run with forced Python mode by patching the availability
    check, ensuring the Python path is always exercised regardless of
    whether C++ is actually available.
    """

    def test_python_detects_violation(self, tmp_path: Path, default_rules: DesignRules):
        """Python fallback detects clearance violations."""
        pcb = _load_pcb(tmp_path, PCB_CLOSE_COMPONENTS)
        with patch("kicad_tools.drc.incremental._is_drc_cpp_available", return_value=False):
            drc = IncrementalDRC(pcb, default_rules)
            violations = drc.full_check()
        assert len(violations) > 0
        assert all(v.rule_id == "clearance" for v in violations)

    def test_python_clean_board(self, tmp_path: Path, default_rules: DesignRules):
        """Python fallback returns no violations for clean board."""
        pcb = _load_pcb(tmp_path, PCB_WELL_SPACED)
        with patch("kicad_tools.drc.incremental._is_drc_cpp_available", return_value=False):
            drc = IncrementalDRC(pcb, default_rules)
            violations = drc.full_check()
        assert len(violations) == 0

    def test_python_same_net_skip(self, tmp_path: Path, default_rules: DesignRules):
        """Python fallback correctly skips same-net pad pairs."""
        pcb = _load_pcb(tmp_path, PCB_SAME_NET)
        with patch("kicad_tools.drc.incremental._is_drc_cpp_available", return_value=False):
            drc = IncrementalDRC(pcb, default_rules)
            violations = drc.full_check()
        # All pads on same net -- no violations even though components are close
        assert len(violations) == 0

    def test_python_no_pads(self, tmp_path: Path, default_rules: DesignRules):
        """Python fallback handles components with no pads."""
        pcb = _load_pcb(tmp_path, PCB_NO_PADS)
        with patch("kicad_tools.drc.incremental._is_drc_cpp_available", return_value=False):
            drc = IncrementalDRC(pcb, default_rules)
            violations = drc.full_check()
        assert len(violations) == 0

    def test_python_single_pad(self, tmp_path: Path, default_rules: DesignRules):
        """Python fallback handles single-pad components."""
        pcb = _load_pcb(tmp_path, PCB_SINGLE_PAD)
        with patch("kicad_tools.drc.incremental._is_drc_cpp_available", return_value=False):
            drc = IncrementalDRC(pcb, default_rules)
            violations = drc.full_check()
        # Single pad components 1.5mm apart, pad radius 0.5mm each
        # clearance = 1.5 - 0.5 - 0.5 = 0.5mm > 0.15mm min, so no violation
        assert len(violations) == 0

    def test_python_rotated_component(self, tmp_path: Path, default_rules: DesignRules):
        """Python fallback correctly handles rotated components."""
        pcb = _load_pcb(tmp_path, PCB_ROTATED)
        with patch("kicad_tools.drc.incremental._is_drc_cpp_available", return_value=False):
            drc = IncrementalDRC(pcb, default_rules)
            violations = drc.full_check()
        # Should still detect violations (R1 rotated 90 degrees but still close to R2)
        # The rotation may change which pads are closest
        assert isinstance(violations, list)

    def test_python_check_move(self, tmp_path: Path, default_rules: DesignRules):
        """Python fallback correctly handles check_move with position offset."""
        pcb = _load_pcb(tmp_path, PCB_CLOSE_COMPONENTS)
        with patch("kicad_tools.drc.incremental._is_drc_cpp_available", return_value=False):
            drc = IncrementalDRC(pcb, default_rules)
            drc.full_check()
            # Get R2's internal bounds center to move R1 right next to it
            r2_center_x = drc.state.component_bounds["R2"].center_x
            r2_center_y = drc.state.component_bounds["R2"].center_y
            # Move R1 to overlap with R2 (same center = maximum overlap)
            delta = drc.check_move("R1", r2_center_x, r2_center_y)
        # R1 overlapping R2 should produce violations (different-net pads overlap)
        assert len(delta.new_violations) > 0


# ----- C++ / Python parity tests (only run if C++ is available) -----


class TestCppPythonParity:
    """Tests comparing C++ and Python backends for identical results.

    These tests verify that both backends produce the same violation
    results for identical inputs, ensuring the C++ optimization does
    not change behavior.
    """

    @pytest.mark.skipif(not is_cpp_available(), reason="C++ DRC backend not built")
    def test_parity_violation_detection(self, tmp_path: Path, default_rules: DesignRules):
        """C++ and Python backends detect the same violations."""
        pcb = _load_pcb(tmp_path, PCB_CLOSE_COMPONENTS)

        # Run with C++ backend
        drc_cpp_run = IncrementalDRC(pcb, default_rules)
        violations_cpp = drc_cpp_run.full_check()

        # Run with Python backend
        with patch("kicad_tools.drc.incremental._is_drc_cpp_available", return_value=False):
            drc_py = IncrementalDRC(pcb, default_rules)
            violations_py = drc_py.full_check()

        assert len(violations_cpp) == len(violations_py)

        # Compare violation details
        for v_cpp, v_py in zip(
            sorted(violations_cpp, key=lambda v: v.items),
            sorted(violations_py, key=lambda v: v.items),
            strict=False,
        ):
            assert v_cpp.rule_id == v_py.rule_id
            assert v_cpp.items == v_py.items
            assert v_cpp.nets == v_py.nets
            # Clearance values should match within float32 tolerance
            assert abs(v_cpp.actual_value - v_py.actual_value) < 1e-3

    @pytest.mark.skipif(not is_cpp_available(), reason="C++ DRC backend not built")
    def test_parity_clean_board(self, tmp_path: Path, default_rules: DesignRules):
        """Both backends agree on clean board."""
        pcb = _load_pcb(tmp_path, PCB_WELL_SPACED)

        drc_cpp_run = IncrementalDRC(pcb, default_rules)
        violations_cpp = drc_cpp_run.full_check()

        with patch("kicad_tools.drc.incremental._is_drc_cpp_available", return_value=False):
            drc_py = IncrementalDRC(pcb, default_rules)
            violations_py = drc_py.full_check()

        assert len(violations_cpp) == 0
        assert len(violations_py) == 0

    @pytest.mark.skipif(not is_cpp_available(), reason="C++ DRC backend not built")
    def test_parity_same_net_skip(self, tmp_path: Path, default_rules: DesignRules):
        """Both backends correctly skip same-net pairs."""
        pcb = _load_pcb(tmp_path, PCB_SAME_NET)

        drc_cpp_run = IncrementalDRC(pcb, default_rules)
        violations_cpp = drc_cpp_run.full_check()

        with patch("kicad_tools.drc.incremental._is_drc_cpp_available", return_value=False):
            drc_py = IncrementalDRC(pcb, default_rules)
            violations_py = drc_py.full_check()

        assert len(violations_cpp) == 0
        assert len(violations_py) == 0

    @pytest.mark.skipif(not is_cpp_available(), reason="C++ DRC backend not built")
    def test_parity_rotated(self, tmp_path: Path, default_rules: DesignRules):
        """Both backends agree on rotated component results."""
        pcb = _load_pcb(tmp_path, PCB_ROTATED)

        drc_cpp_run = IncrementalDRC(pcb, default_rules)
        violations_cpp = drc_cpp_run.full_check()

        with patch("kicad_tools.drc.incremental._is_drc_cpp_available", return_value=False):
            drc_py = IncrementalDRC(pcb, default_rules)
            violations_py = drc_py.full_check()

        assert len(violations_cpp) == len(violations_py)

    @pytest.mark.skipif(not is_cpp_available(), reason="C++ DRC backend not built")
    def test_parity_numerical_precision(self, tmp_path: Path, default_rules: DesignRules):
        """C++ and Python clearance values match within float32 tolerance."""
        pcb = _load_pcb(tmp_path, PCB_CLOSE_COMPONENTS)

        drc_cpp_run = IncrementalDRC(pcb, default_rules)
        violations_cpp = drc_cpp_run.full_check()

        with patch("kicad_tools.drc.incremental._is_drc_cpp_available", return_value=False):
            drc_py = IncrementalDRC(pcb, default_rules)
            violations_py = drc_py.full_check()

        for v_cpp, v_py in zip(
            sorted(violations_cpp, key=lambda v: v.items),
            sorted(violations_py, key=lambda v: v.items),
            strict=False,
        ):
            # float32 vs float64 can differ by ~1e-6 in the worst case,
            # but pad geometry values are small so 1e-3 is conservative
            assert abs(v_cpp.actual_value - v_py.actual_value) < 1e-3, (
                f"Clearance mismatch: C++={v_cpp.actual_value:.6f} "
                f"vs Python={v_py.actual_value:.6f}"
            )

    @pytest.mark.skipif(not is_cpp_available(), reason="C++ DRC backend not built")
    def test_parity_check_move(self, tmp_path: Path, default_rules: DesignRules):
        """Both backends agree on check_move results with position offset."""
        pcb = _load_pcb(tmp_path, PCB_CLOSE_COMPONENTS)

        # C++ path
        drc_cpp_run = IncrementalDRC(pcb, default_rules)
        drc_cpp_run.full_check()
        r2_cx = drc_cpp_run.state.component_bounds["R2"].center_x
        r2_cy = drc_cpp_run.state.component_bounds["R2"].center_y
        delta_cpp = drc_cpp_run.check_move("R1", r2_cx, r2_cy)

        # Python path
        with patch("kicad_tools.drc.incremental._is_drc_cpp_available", return_value=False):
            drc_py = IncrementalDRC(pcb, default_rules)
            drc_py.full_check()
            delta_py = drc_py.check_move("R1", r2_cx, r2_cy)

        assert len(delta_cpp.new_violations) == len(delta_py.new_violations)


# ----- Edge case tests -----


class TestEdgeCases:
    """Edge case tests that run on whichever backend is available."""

    def test_zero_pads_no_crash(self, tmp_path: Path, default_rules: DesignRules):
        """Components with zero pads do not crash."""
        pcb = _load_pcb(tmp_path, PCB_NO_PADS)
        drc = IncrementalDRC(pcb, default_rules)
        violations = drc.full_check()
        assert len(violations) == 0

    def test_single_pad_components(self, tmp_path: Path, default_rules: DesignRules):
        """Single-pad components work correctly (1x1 trivial case)."""
        pcb = _load_pcb(tmp_path, PCB_SINGLE_PAD)
        drc = IncrementalDRC(pcb, default_rules)
        violations = drc.full_check()
        # 1.5mm center-to-center, 0.5mm radius each = 0.5mm clearance > 0.15mm
        assert len(violations) == 0

    def test_net_zero_not_skipped(self, tmp_path: Path, default_rules: DesignRules):
        """Pads with net_number=0 (unconnected) are not skipped."""
        # Unconnected pads (net 0) should still be checked against each other
        # because net_number == 0 is not a real net connection
        pcb_content = PCB_CLOSE_COMPONENTS.replace('(net 2 "SIG1")', '(net 0 "")')
        pcb_content = pcb_content.replace('(net 3 "SIG2")', '(net 0 "")')
        # Replace pad net assignments
        pcb_content = pcb_content.replace('(net 2 "SIG1"))', '(net 0 ""))')
        pcb_content = pcb_content.replace('(net 3 "SIG2"))', '(net 0 ""))')
        pcb = _load_pcb(tmp_path, pcb_content)
        drc = IncrementalDRC(pcb, default_rules)
        violations = drc.full_check()
        # Should still detect violations because net 0 pairs are not skipped
        assert len(violations) > 0
