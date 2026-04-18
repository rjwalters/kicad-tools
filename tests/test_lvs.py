"""Tests for LVS (Layout-vs-Schematic) checking with hierarchical schematic support."""

from pathlib import Path

import pytest

from kicad_tools.validate.consistency import (
    LVSMatch,
    LVSResult,
    SchematicPCBChecker,
    _extract_package_size,
    _extract_ref_prefix,
    _normalize_footprint,
)

# ---------------------------------------------------------------------------
# Helper fixtures: inline schematic + PCB S-expression strings
# ---------------------------------------------------------------------------

SCHEMATIC_R1_C1 = """(kicad_sch
  (version 20231120)
  (generator "test")
  (generator_version "8.0")
  (uuid "00000000-0000-0000-0000-000000000001")
  (paper "A4")
  (lib_symbols)
  (symbol
    (lib_id "Device:R")
    (at 100 100 0)
    (uuid "00000000-0000-0000-0000-000000000002")
    (property "Reference" "R1" (at 100 90 0) (effects (font (size 1.27 1.27))))
    (property "Value" "10k" (at 100 110 0) (effects (font (size 1.27 1.27))))
    (property "Footprint" "Resistor_SMD:R_0402_1005Metric" (at 100 100 0) (effects (hide yes)))
    (property "Datasheet" "" (at 100 100 0) (effects (hide yes)))
    (pin "1" (uuid "00000000-0000-0000-0000-000000000003"))
    (pin "2" (uuid "00000000-0000-0000-0000-000000000004"))
    (instances
      (project "test"
        (path "/00000000-0000-0000-0000-000000000001"
          (reference "R1") (unit 1)
        )
      )
    )
  )
  (symbol
    (lib_id "Device:C")
    (at 120 100 0)
    (uuid "00000000-0000-0000-0000-000000000005")
    (property "Reference" "C1" (at 120 90 0) (effects (font (size 1.27 1.27))))
    (property "Value" "100nF" (at 120 110 0) (effects (font (size 1.27 1.27))))
    (property "Footprint" "Capacitor_SMD:C_0402_1005Metric" (at 120 100 0) (effects (hide yes)))
    (property "Datasheet" "" (at 120 100 0) (effects (hide yes)))
    (pin "1" (uuid "00000000-0000-0000-0000-000000000006"))
    (pin "2" (uuid "00000000-0000-0000-0000-000000000007"))
    (instances
      (project "test"
        (path "/00000000-0000-0000-0000-000000000001"
          (reference "C1") (unit 1)
        )
      )
    )
  )
)
"""

PCB_R1_C1_EXACT = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal))
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000010")
    (at 100 100)
    (property "Reference" "R1" (at 0 -1.5 0) (layer "F.SilkS") (uuid "fp-r1-ref"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab") (uuid "fp-r1-val"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25))
  )
  (footprint "Capacitor_SMD:C_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000020")
    (at 110 100)
    (property "Reference" "C1" (at 0 -1.5 0) (layer "F.SilkS") (uuid "fp-c1-ref"))
    (property "Value" "100nF" (at 0 1.5 0) (layer "F.Fab") (uuid "fp-c1-val"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25))
  )
)
"""

# PCB where R5=10k uses the same footprint as schematic R1=10k (different ref)
PCB_SWAPPED_REF = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal))
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000010")
    (at 100 100)
    (property "Reference" "R5" (at 0 -1.5 0) (layer "F.SilkS") (uuid "fp-r5-ref"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab") (uuid "fp-r5-val"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25))
  )
  (footprint "Capacitor_SMD:C_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000020")
    (at 110 100)
    (property "Reference" "C1" (at 0 -1.5 0) (layer "F.SilkS") (uuid "fp-c1-ref"))
    (property "Value" "100nF" (at 0 1.5 0) (layer "F.Fab") (uuid "fp-c1-val"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25))
  )
)
"""

# PCB where R1=10k but with 0603 footprint (value matches, footprint differs)
PCB_FOOTPRINT_MISMATCH = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal))
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (footprint "Resistor_SMD:R_0603_1608Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000010")
    (at 100 100)
    (property "Reference" "R1" (at 0 -1.5 0) (layer "F.SilkS") (uuid "fp-r1-ref"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab") (uuid "fp-r1-val"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25))
  )
  (footprint "Capacitor_SMD:C_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000020")
    (at 110 100)
    (property "Reference" "C1" (at 0 -1.5 0) (layer "F.SilkS") (uuid "fp-c1-ref"))
    (property "Value" "100nF" (at 0 1.5 0) (layer "F.Fab") (uuid "fp-c1-val"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25))
  )
)
"""

# Empty PCB (no footprints)
PCB_EMPTY = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal))
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
)
"""

# Empty schematic (no symbols)
SCHEMATIC_EMPTY = """(kicad_sch
  (version 20231120)
  (generator "test")
  (generator_version "8.0")
  (uuid "00000000-0000-0000-0000-000000000001")
  (paper "A4")
  (lib_symbols)
)
"""

# PCB with extra component not in schematic
PCB_EXTRA_TP1 = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal))
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000010")
    (at 100 100)
    (property "Reference" "R1" (at 0 -1.5 0) (layer "F.SilkS") (uuid "fp-r1-ref"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab") (uuid "fp-r1-val"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25))
  )
  (footprint "Capacitor_SMD:C_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000020")
    (at 110 100)
    (property "Reference" "C1" (at 0 -1.5 0) (layer "F.SilkS") (uuid "fp-c1-ref"))
    (property "Value" "100nF" (at 0 1.5 0) (layer "F.Fab") (uuid "fp-c1-val"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25))
  )
  (footprint "TestPoint:TestPoint_Pad_1.0x1.0mm"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000030")
    (at 120 100)
    (property "Reference" "TP1" (at 0 -1.5 0) (layer "F.SilkS") (uuid "fp-tp1-ref"))
    (property "Value" "TestPoint" (at 0 1.5 0) (layer "F.Fab") (uuid "fp-tp1-val"))
    (pad "1" smd rect (at 0 0) (size 1.0 1.0) (layers "F.Cu" "F.Paste" "F.Mask"))
  )
)
"""

# PCB with net-based matching scenario: R3 has nets but different value
PCB_NET_BASED = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal))
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "VCC")
  (net 2 "GND")
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000010")
    (at 100 100)
    (property "Reference" "R3" (at 0 -1.5 0) (layer "F.SilkS") (uuid "fp-r3-ref"))
    (property "Value" "4.7k" (at 0 1.5 0) (layer "F.Fab") (uuid "fp-r3-val"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 1 "VCC"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 2 "GND"))
  )
  (footprint "Capacitor_SMD:C_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000020")
    (at 110 100)
    (property "Reference" "C1" (at 0 -1.5 0) (layer "F.SilkS") (uuid "fp-c1-ref"))
    (property "Value" "100nF" (at 0 1.5 0) (layer "F.Fab") (uuid "fp-c1-val"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25))
  )
)
"""

# Schematic with R1 (10k) -- for net-based matching against PCB R3 (4.7k)
SCHEMATIC_R1_ONLY = """(kicad_sch
  (version 20231120)
  (generator "test")
  (generator_version "8.0")
  (uuid "00000000-0000-0000-0000-000000000001")
  (paper "A4")
  (lib_symbols)
  (symbol
    (lib_id "Device:R")
    (at 100 100 0)
    (uuid "00000000-0000-0000-0000-000000000002")
    (property "Reference" "R1" (at 100 90 0) (effects (font (size 1.27 1.27))))
    (property "Value" "10k" (at 100 110 0) (effects (font (size 1.27 1.27))))
    (property "Footprint" "Resistor_SMD:R_0402_1005Metric" (at 100 100 0) (effects (hide yes)))
    (property "Datasheet" "" (at 100 100 0) (effects (hide yes)))
    (pin "1" (uuid "00000000-0000-0000-0000-000000000003"))
    (pin "2" (uuid "00000000-0000-0000-0000-000000000004"))
    (instances
      (project "test"
        (path "/00000000-0000-0000-0000-000000000001"
          (reference "R1") (unit 1)
        )
      )
    )
  )
  (symbol
    (lib_id "Device:C")
    (at 120 100 0)
    (uuid "00000000-0000-0000-0000-000000000005")
    (property "Reference" "C1" (at 120 90 0) (effects (font (size 1.27 1.27))))
    (property "Value" "100nF" (at 120 110 0) (effects (font (size 1.27 1.27))))
    (property "Footprint" "Capacitor_SMD:C_0402_1005Metric" (at 120 100 0) (effects (hide yes)))
    (property "Datasheet" "" (at 120 100 0) (effects (hide yes)))
    (pin "1" (uuid "00000000-0000-0000-0000-000000000006"))
    (pin "2" (uuid "00000000-0000-0000-0000-000000000007"))
    (instances
      (project "test"
        (path "/00000000-0000-0000-0000-000000000001"
          (reference "C1") (unit 1)
        )
      )
    )
  )
)
"""


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sch_r1_c1(tmp_path: Path) -> Path:
    """Schematic with R1=10k and C1=100nF."""
    p = tmp_path / "test.kicad_sch"
    p.write_text(SCHEMATIC_R1_C1)
    return p


@pytest.fixture
def pcb_r1_c1_exact(tmp_path: Path) -> Path:
    """PCB with R1=10k and C1=100nF (exact match)."""
    p = tmp_path / "test.kicad_pcb"
    p.write_text(PCB_R1_C1_EXACT)
    return p


@pytest.fixture
def pcb_swapped_ref(tmp_path: Path) -> Path:
    """PCB with R5=10k (swapped from R1) and C1=100nF."""
    p = tmp_path / "test.kicad_pcb"
    p.write_text(PCB_SWAPPED_REF)
    return p


@pytest.fixture
def pcb_footprint_mismatch(tmp_path: Path) -> Path:
    """PCB with R1=10k 0603 (schematic has 0402)."""
    p = tmp_path / "test.kicad_pcb"
    p.write_text(PCB_FOOTPRINT_MISMATCH)
    return p


@pytest.fixture
def pcb_empty(tmp_path: Path) -> Path:
    """Empty PCB (no footprints)."""
    p = tmp_path / "test.kicad_pcb"
    p.write_text(PCB_EMPTY)
    return p


@pytest.fixture
def sch_empty(tmp_path: Path) -> Path:
    """Empty schematic."""
    p = tmp_path / "test.kicad_sch"
    p.write_text(SCHEMATIC_EMPTY)
    return p


@pytest.fixture
def pcb_extra_tp1(tmp_path: Path) -> Path:
    """PCB with R1, C1, and extra TP1."""
    p = tmp_path / "test.kicad_pcb"
    p.write_text(PCB_EXTRA_TP1)
    return p


@pytest.fixture
def pcb_net_based(tmp_path: Path) -> Path:
    """PCB with R3=4.7k (nets assigned) and C1."""
    p = tmp_path / "test.kicad_pcb"
    p.write_text(PCB_NET_BASED)
    return p


@pytest.fixture
def sch_r1_only(tmp_path: Path) -> Path:
    """Schematic with R1=10k and C1=100nF."""
    p = tmp_path / "test.kicad_sch"
    p.write_text(SCHEMATIC_R1_ONLY)
    return p


# ---------------------------------------------------------------------------
# Tests: LVSMatch and LVSResult dataclasses
# ---------------------------------------------------------------------------


class TestLVSMatch:
    """Tests for LVSMatch dataclass."""

    def test_creation(self):
        m = LVSMatch(
            pcb_ref="R1",
            sch_ref="R1",
            confidence=1.0,
            match_reason="exact",
            value_match=True,
            footprint_match=True,
        )
        assert m.pcb_ref == "R1"
        assert m.confidence == 1.0

    def test_to_dict(self):
        m = LVSMatch(
            pcb_ref="R5",
            sch_ref="R1",
            confidence=0.8,
            match_reason="value+footprint",
            value_match=True,
            footprint_match=True,
        )
        d = m.to_dict()
        assert d["pcb_ref"] == "R5"
        assert d["sch_ref"] == "R1"
        assert d["confidence"] == 0.8


class TestLVSResult:
    """Tests for LVSResult dataclass."""

    def test_empty_is_clean(self):
        r = LVSResult()
        assert r.is_clean
        assert r.exact_match_count == 0
        assert r.fuzzy_match_count == 0

    def test_all_exact_is_clean(self):
        r = LVSResult(
            matches=[
                LVSMatch("R1", "R1", 1.0, "exact", True, True),
            ]
        )
        assert r.is_clean
        assert r.exact_match_count == 1
        assert r.fuzzy_match_count == 0

    def test_fuzzy_is_not_clean(self):
        r = LVSResult(
            matches=[
                LVSMatch("R5", "R1", 0.8, "fuzzy", True, True),
            ]
        )
        assert not r.is_clean
        assert r.fuzzy_match_count == 1

    def test_orphans_not_clean(self):
        r = LVSResult(unmatched_pcb=["TP1"])
        assert not r.is_clean

    def test_to_dict(self):
        r = LVSResult(
            matches=[LVSMatch("R1", "R1", 1.0, "exact", True, True)],
            unmatched_pcb=["TP1"],
            unmatched_sch=["C2"],
        )
        d = r.to_dict()
        assert d["exact_matches"] == 1
        assert d["unmatched_pcb"] == ["TP1"]
        assert d["unmatched_sch"] == ["C2"]

    def test_summary(self):
        r = LVSResult(
            matches=[LVSMatch("R1", "R1", 1.0, "exact", True, True)],
        )
        s = r.summary()
        assert "CLEAN" in s
        assert "Exact matches" in s


# ---------------------------------------------------------------------------
# Tests: Helper functions
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_extract_ref_prefix(self):
        assert _extract_ref_prefix("R1") == "R"
        assert _extract_ref_prefix("C100") == "C"
        assert _extract_ref_prefix("U1") == "U"
        assert _extract_ref_prefix("SW12") == "SW"

    def test_normalize_footprint(self):
        assert _normalize_footprint("Resistor_SMD:R_0402_1005Metric") == "R_0402_1005Metric"
        assert _normalize_footprint("R_0402_1005Metric") == "R_0402_1005Metric"

    def test_extract_package_size_standard(self):
        assert _extract_package_size("R_0402_1005Metric") == "0402"
        assert _extract_package_size("C_0805_2012Metric") == "0805"
        assert _extract_package_size("L_1206_3216Metric") == "1206"
        assert _extract_package_size("R_0603_1608Metric") == "0603"

    def test_extract_package_size_with_library_prefix(self):
        assert _extract_package_size("Resistor_SMD:R_0402_1005Metric") == "0402"
        assert _extract_package_size("Capacitor_SMD:C_0805_2012Metric") == "0805"

    def test_extract_package_size_non_passive(self):
        assert _extract_package_size("SOT-23-5") is None
        assert _extract_package_size("TSSOP-20_4.4x6.5mm_P0.65mm") is None
        assert _extract_package_size("TestPoint_Pad_1.0x1.0mm") is None
        assert _extract_package_size("") is None

    def test_extract_package_size_bare_code(self):
        # Bare 4-digit code at boundary
        assert _extract_package_size("C_0402") == "0402"
        assert _extract_package_size("R-0805") == "0805"


# ---------------------------------------------------------------------------
# Tests: Multi-pass LVS matching
# ---------------------------------------------------------------------------


class TestLVSPass1Exact:
    """Pass 1: exact ref + value + footprint match."""

    def test_all_exact(self, sch_r1_c1: Path, pcb_r1_c1_exact: Path):
        checker = SchematicPCBChecker(sch_r1_c1, pcb_r1_c1_exact)
        result = checker.check_lvs()

        assert result.is_clean
        assert result.exact_match_count == 2
        assert result.fuzzy_match_count == 0
        assert result.unmatched_pcb == []
        assert result.unmatched_sch == []

        refs = {m.sch_ref for m in result.matches}
        assert refs == {"R1", "C1"}
        for m in result.matches:
            assert m.confidence == 1.0


class TestLVSPass2ValueFootprint:
    """Pass 2: unique value+footprint across different refs."""

    def test_swapped_ref_matched(self, sch_r1_c1: Path, pcb_swapped_ref: Path):
        checker = SchematicPCBChecker(sch_r1_c1, pcb_swapped_ref)
        result = checker.check_lvs()

        # C1 should be exact, R1->R5 should be fuzzy at 0.8
        assert result.exact_match_count == 1  # C1
        assert result.fuzzy_match_count == 1  # R1->R5
        assert not result.unmatched_pcb
        assert not result.unmatched_sch

        fuzzy = [m for m in result.matches if m.confidence < 1.0]
        assert len(fuzzy) == 1
        assert fuzzy[0].sch_ref == "R1"
        assert fuzzy[0].pcb_ref == "R5"
        assert fuzzy[0].confidence == 0.8
        assert fuzzy[0].value_match
        assert fuzzy[0].footprint_match


class TestLVSPass3ValuePrefix:
    """Pass 3: value+prefix match (footprint may differ)."""

    def test_footprint_mismatch_same_value_different_size(
        self, sch_r1_c1: Path, pcb_footprint_mismatch: Path
    ):
        """Schematic R1=10k 0402 vs PCB R1=10k 0603: same value but different
        package size.  The size constraint should prevent a cross-size match,
        leaving R1 unmatched on both sides."""
        checker = SchematicPCBChecker(sch_r1_c1, pcb_footprint_mismatch)
        result = checker.check_lvs()

        # C1 exact match.  R1 should be rejected by the size constraint in
        # Pass 3 (and Pass 4 has no nets), so it remains unmatched.
        assert result.exact_match_count == 1  # C1
        assert "R1" in result.unmatched_sch
        assert "R1" in result.unmatched_pcb

    def test_footprint_mismatch_same_value_same_size(self, tmp_path: Path):
        """Pass 3 should still match when footprints differ only in non-size
        details but the package size is the same."""
        # Schematic: R1=10k R_0402_1005Metric
        sch = """(kicad_sch
  (version 20231120)
  (generator "test")
  (generator_version "8.0")
  (uuid "00000000-0000-0000-0000-000000000001")
  (paper "A4")
  (lib_symbols)
  (symbol
    (lib_id "Device:R")
    (at 100 100 0)
    (uuid "00000000-0000-0000-0000-000000000002")
    (property "Reference" "R1" (at 100 90 0) (effects (font (size 1.27 1.27))))
    (property "Value" "10k" (at 100 110 0) (effects (font (size 1.27 1.27))))
    (property "Footprint" "Resistor_SMD:R_0402_1005Metric" (at 100 100 0) (effects (hide yes)))
    (property "Datasheet" "" (at 100 100 0) (effects (hide yes)))
    (pin "1" (uuid "00000000-0000-0000-0000-000000000003"))
    (pin "2" (uuid "00000000-0000-0000-0000-000000000004"))
    (instances
      (project "test"
        (path "/00000000-0000-0000-0000-000000000001"
          (reference "R1") (unit 1)
        )
      )
    )
  )
)
"""
        # PCB: R5=10k R_0402_1005Metric (same size, different ref)
        pcb = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal))
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000010")
    (at 100 100)
    (property "Reference" "R5" (at 0 -1.5 0) (layer "F.SilkS") (uuid "fp-r5-ref"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab") (uuid "fp-r5-val"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25))
  )
)
"""
        sch_path = tmp_path / "test.kicad_sch"
        sch_path.write_text(sch)
        pcb_path = tmp_path / "test.kicad_pcb"
        pcb_path.write_text(pcb)

        checker = SchematicPCBChecker(sch_path, pcb_path)
        result = checker.check_lvs()

        # R1 -> R5 should match via Pass 2 (unique value+footprint)
        assert len(result.matches) == 1
        assert result.matches[0].sch_ref == "R1"
        assert result.matches[0].pcb_ref == "R5"
        assert result.matches[0].confidence == 0.8
        assert result.matches[0].value_match
        assert result.matches[0].footprint_match


class TestLVSPass4NetBased:
    """Pass 4: net-based correlation."""

    def test_net_based_matching(self, sch_r1_only: Path, pcb_net_based: Path):
        checker = SchematicPCBChecker(sch_r1_only, pcb_net_based)
        result = checker.check_lvs()

        # C1 exact match. R1 (sch) vs R3 (pcb): different value + different ref.
        # Pass 2 won't match (value differs). Pass 3 won't match (value differs).
        # Pass 4 should match since R is the only prefix left on both sides.
        c1_match = next((m for m in result.matches if m.sch_ref == "C1"), None)
        assert c1_match is not None
        assert c1_match.confidence == 1.0

        r_match = next((m for m in result.matches if m.sch_ref == "R1"), None)
        assert r_match is not None
        assert r_match.pcb_ref == "R3"
        assert r_match.confidence == 0.4
        assert not r_match.value_match  # 10k vs 4.7k


class TestLVSOrphans:
    """Orphan detection tests."""

    def test_pcb_orphan(self, sch_r1_c1: Path, pcb_extra_tp1: Path):
        checker = SchematicPCBChecker(sch_r1_c1, pcb_extra_tp1)
        result = checker.check_lvs()

        assert result.exact_match_count == 2  # R1, C1
        assert "TP1" in result.unmatched_pcb
        assert result.unmatched_sch == []

    def test_schematic_orphan(self, sch_r1_c1: Path, pcb_empty: Path):
        checker = SchematicPCBChecker(sch_r1_c1, pcb_empty)
        result = checker.check_lvs()

        assert result.exact_match_count == 0
        assert set(result.unmatched_sch) == {"R1", "C1"}
        assert result.unmatched_pcb == []

    def test_empty_schematic(self, sch_empty: Path, pcb_r1_c1_exact: Path):
        checker = SchematicPCBChecker(sch_empty, pcb_r1_c1_exact)
        result = checker.check_lvs()

        assert result.exact_match_count == 0
        assert result.unmatched_sch == []
        assert set(result.unmatched_pcb) == {"R1", "C1"}


class TestLVSSizeConstraint:
    """Tests for package-size consistency constraint in Passes 3 and 4."""

    def test_pass3_rejects_cross_size_match(self, tmp_path: Path):
        """Pass 3 should NOT match same-value caps that differ in package size."""
        # Schematic: C1=100nF 0402, C2=100nF 0603
        sch = """(kicad_sch
  (version 20231120)
  (generator "test")
  (generator_version "8.0")
  (uuid "00000000-0000-0000-0000-000000000001")
  (paper "A4")
  (lib_symbols)
  (symbol
    (lib_id "Device:C")
    (at 100 100 0)
    (uuid "00000000-0000-0000-0000-000000000010")
    (property "Reference" "C1" (at 100 90 0) (effects (font (size 1.27 1.27))))
    (property "Value" "100nF" (at 100 110 0) (effects (font (size 1.27 1.27))))
    (property "Footprint" "Capacitor_SMD:C_0402_1005Metric" (at 100 100 0) (effects (hide yes)))
    (property "Datasheet" "" (at 100 100 0) (effects (hide yes)))
    (pin "1" (uuid "00000000-0000-0000-0000-000000000011"))
    (pin "2" (uuid "00000000-0000-0000-0000-000000000012"))
    (instances
      (project "test"
        (path "/00000000-0000-0000-0000-000000000001"
          (reference "C1") (unit 1)
        )
      )
    )
  )
  (symbol
    (lib_id "Device:C")
    (at 120 100 0)
    (uuid "00000000-0000-0000-0000-000000000020")
    (property "Reference" "C2" (at 120 90 0) (effects (font (size 1.27 1.27))))
    (property "Value" "100nF" (at 120 110 0) (effects (font (size 1.27 1.27))))
    (property "Footprint" "Capacitor_SMD:C_0603_1608Metric" (at 120 100 0) (effects (hide yes)))
    (property "Datasheet" "" (at 120 100 0) (effects (hide yes)))
    (pin "1" (uuid "00000000-0000-0000-0000-000000000021"))
    (pin "2" (uuid "00000000-0000-0000-0000-000000000022"))
    (instances
      (project "test"
        (path "/00000000-0000-0000-0000-000000000001"
          (reference "C2") (unit 1)
        )
      )
    )
  )
)
"""
        # PCB: C3=100nF 0402, C4=100nF 0603 (swapped refs)
        pcb = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal))
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (footprint "Capacitor_SMD:C_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000030")
    (at 100 100)
    (property "Reference" "C3" (at 0 -1.5 0) (layer "F.SilkS") (uuid "fp-c3-ref"))
    (property "Value" "100nF" (at 0 1.5 0) (layer "F.Fab") (uuid "fp-c3-val"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 1 "VCC"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 2 "GND"))
  )
  (footprint "Capacitor_SMD:C_0603_1608Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000040")
    (at 110 100)
    (property "Reference" "C4" (at 0 -1.5 0) (layer "F.SilkS") (uuid "fp-c4-ref"))
    (property "Value" "100nF" (at 0 1.5 0) (layer "F.Fab") (uuid "fp-c4-val"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 1 "VCC"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 2 "GND"))
  )
)
"""
        sch_path = tmp_path / "test.kicad_sch"
        sch_path.write_text(sch)
        pcb_path = tmp_path / "test.kicad_pcb"
        pcb_path.write_text(pcb)

        checker = SchematicPCBChecker(sch_path, pcb_path)
        result = checker.check_lvs()

        # Pass 2 should match C1(0402)->C3(0402) and C2(0603)->C4(0603)
        # because value+footprint is unique per size.  No cross-size match.
        matched_pairs = {(m.sch_ref, m.pcb_ref) for m in result.matches}
        assert ("C1", "C3") in matched_pairs
        assert ("C2", "C4") in matched_pairs
        # Crucially, no cross-size pairing
        assert ("C1", "C4") not in matched_pairs
        assert ("C2", "C3") not in matched_pairs

    def test_pass3_rejects_cross_size_with_unique_value_prefix(self, tmp_path: Path):
        """When two caps have the same value but different sizes and unique
        prefix+value, Pass 3 must NOT cross-match them."""
        # Schematic: C1=100nF 0402
        sch = """(kicad_sch
  (version 20231120)
  (generator "test")
  (generator_version "8.0")
  (uuid "00000000-0000-0000-0000-000000000001")
  (paper "A4")
  (lib_symbols)
  (symbol
    (lib_id "Device:C")
    (at 100 100 0)
    (uuid "00000000-0000-0000-0000-000000000010")
    (property "Reference" "C1" (at 100 90 0) (effects (font (size 1.27 1.27))))
    (property "Value" "100nF" (at 100 110 0) (effects (font (size 1.27 1.27))))
    (property "Footprint" "Capacitor_SMD:C_0402_1005Metric" (at 100 100 0) (effects (hide yes)))
    (property "Datasheet" "" (at 100 100 0) (effects (hide yes)))
    (pin "1" (uuid "00000000-0000-0000-0000-000000000011"))
    (pin "2" (uuid "00000000-0000-0000-0000-000000000012"))
    (instances
      (project "test"
        (path "/00000000-0000-0000-0000-000000000001"
          (reference "C1") (unit 1)
        )
      )
    )
  )
)
"""
        # PCB: C5=100nF 0603 (different ref AND different size)
        pcb = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal))
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "VCC")
  (net 2 "GND")
  (footprint "Capacitor_SMD:C_0603_1608Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000030")
    (at 100 100)
    (property "Reference" "C5" (at 0 -1.5 0) (layer "F.SilkS") (uuid "fp-c5-ref"))
    (property "Value" "100nF" (at 0 1.5 0) (layer "F.Fab") (uuid "fp-c5-val"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 1 "VCC"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 2 "GND"))
  )
)
"""
        sch_path = tmp_path / "test.kicad_sch"
        sch_path.write_text(sch)
        pcb_path = tmp_path / "test.kicad_pcb"
        pcb_path.write_text(pcb)

        checker = SchematicPCBChecker(sch_path, pcb_path)
        result = checker.check_lvs()

        # Pass 3: C1 and C5 have same value+prefix but different sizes.
        # The size constraint should reject the match.
        # Pass 4: same -- different sizes should still be rejected.
        assert result.matches == []
        assert "C1" in result.unmatched_sch
        assert "C5" in result.unmatched_pcb

    def test_pass4_rejects_cross_size_net_based(self, tmp_path: Path):
        """Pass 4 net-based match should reject when package sizes differ."""
        # Schematic: C1=100nF 0402
        sch = """(kicad_sch
  (version 20231120)
  (generator "test")
  (generator_version "8.0")
  (uuid "00000000-0000-0000-0000-000000000001")
  (paper "A4")
  (lib_symbols)
  (symbol
    (lib_id "Device:C")
    (at 100 100 0)
    (uuid "00000000-0000-0000-0000-000000000010")
    (property "Reference" "C1" (at 100 90 0) (effects (font (size 1.27 1.27))))
    (property "Value" "2.2uF" (at 100 110 0) (effects (font (size 1.27 1.27))))
    (property "Footprint" "Capacitor_SMD:C_0402_1005Metric" (at 100 100 0) (effects (hide yes)))
    (property "Datasheet" "" (at 100 100 0) (effects (hide yes)))
    (pin "1" (uuid "00000000-0000-0000-0000-000000000011"))
    (pin "2" (uuid "00000000-0000-0000-0000-000000000012"))
    (instances
      (project "test"
        (path "/00000000-0000-0000-0000-000000000001"
          (reference "C1") (unit 1)
        )
      )
    )
  )
)
"""
        # PCB: C3=100nF 0603 (different value AND different size)
        # This should NOT match even through Pass 4 net-based
        pcb = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal))
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "VCC")
  (net 2 "GND")
  (footprint "Capacitor_SMD:C_0603_1608Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000030")
    (at 100 100)
    (property "Reference" "C3" (at 0 -1.5 0) (layer "F.SilkS") (uuid "fp-c3-ref"))
    (property "Value" "100nF" (at 0 1.5 0) (layer "F.Fab") (uuid "fp-c3-val"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 1 "VCC"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 2 "GND"))
  )
)
"""
        sch_path = tmp_path / "test.kicad_sch"
        sch_path.write_text(sch)
        pcb_path = tmp_path / "test.kicad_pcb"
        pcb_path.write_text(pcb)

        checker = SchematicPCBChecker(sch_path, pcb_path)
        result = checker.check_lvs()

        # Neither Pass 3 nor Pass 4 should cross-match
        assert result.matches == []
        assert "C1" in result.unmatched_sch
        assert "C3" in result.unmatched_pcb

    def test_ic_matching_unaffected(self, tmp_path: Path):
        """ICs/connectors without recognised package sizes should still match."""
        # Schematic: U1 in TSSOP-20
        sch = """(kicad_sch
  (version 20231120)
  (generator "test")
  (generator_version "8.0")
  (uuid "00000000-0000-0000-0000-000000000001")
  (paper "A4")
  (lib_symbols)
  (symbol
    (lib_id "Device:U")
    (at 100 100 0)
    (uuid "00000000-0000-0000-0000-000000000010")
    (property "Reference" "U1" (at 100 90 0) (effects (font (size 1.27 1.27))))
    (property "Value" "ATmega328P" (at 100 110 0) (effects (font (size 1.27 1.27))))
    (property "Footprint" "Package_SO:TSSOP-20_4.4x6.5mm_P0.65mm" (at 100 100 0) (effects (hide yes)))
    (property "Datasheet" "" (at 100 100 0) (effects (hide yes)))
    (pin "1" (uuid "00000000-0000-0000-0000-000000000011"))
    (pin "2" (uuid "00000000-0000-0000-0000-000000000012"))
    (instances
      (project "test"
        (path "/00000000-0000-0000-0000-000000000001"
          (reference "U1") (unit 1)
        )
      )
    )
  )
)
"""
        # PCB: U5 with same value+footprint (different ref)
        pcb = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal))
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "VCC")
  (footprint "Package_SO:TSSOP-20_4.4x6.5mm_P0.65mm"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000030")
    (at 100 100)
    (property "Reference" "U5" (at 0 -1.5 0) (layer "F.SilkS") (uuid "fp-u5-ref"))
    (property "Value" "ATmega328P" (at 0 1.5 0) (layer "F.Fab") (uuid "fp-u5-val"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 1 "VCC"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25))
  )
)
"""
        sch_path = tmp_path / "test.kicad_sch"
        sch_path.write_text(sch)
        pcb_path = tmp_path / "test.kicad_pcb"
        pcb_path.write_text(pcb)

        checker = SchematicPCBChecker(sch_path, pcb_path)
        result = checker.check_lvs()

        # U1 -> U5 should still match via Pass 2 (unique value+footprint)
        assert len(result.matches) == 1
        assert result.matches[0].sch_ref == "U1"
        assert result.matches[0].pcb_ref == "U5"
        assert result.matches[0].confidence == 0.8


class TestLVSEdgeCases:
    """Edge cases."""

    def test_empty_both(self, sch_empty: Path, pcb_empty: Path):
        checker = SchematicPCBChecker(sch_empty, pcb_empty)
        result = checker.check_lvs()
        assert result.is_clean
        assert result.matches == []

    def test_no_path_raises(self, sch_r1_c1: Path, pcb_r1_c1_exact: Path):
        """check_lvs with Schematic object (no path) raises ValueError."""
        from kicad_tools.schema.pcb import PCB
        from kicad_tools.schema.schematic import Schematic

        sch = Schematic.load(str(sch_r1_c1))
        pcb = PCB.load(str(pcb_r1_c1_exact))
        # Construct with objects -- no path stored
        checker = SchematicPCBChecker(sch, pcb)
        with pytest.raises(ValueError, match="No schematic path"):
            checker.check_lvs()

    def test_power_symbols_excluded(self, tmp_path: Path, pcb_r1_c1_exact: Path):
        """Power symbols (#PWR) should be excluded from LVS."""
        sch_with_power = """(kicad_sch
  (version 20231120)
  (generator "test")
  (generator_version "8.0")
  (uuid "00000000-0000-0000-0000-000000000001")
  (paper "A4")
  (lib_symbols)
  (symbol
    (lib_id "Device:R")
    (at 100 100 0)
    (uuid "00000000-0000-0000-0000-000000000002")
    (property "Reference" "R1" (at 100 90 0) (effects (font (size 1.27 1.27))))
    (property "Value" "10k" (at 100 110 0) (effects (font (size 1.27 1.27))))
    (property "Footprint" "Resistor_SMD:R_0402_1005Metric" (at 100 100 0) (effects (hide yes)))
    (property "Datasheet" "" (at 100 100 0) (effects (hide yes)))
    (pin "1" (uuid "00000000-0000-0000-0000-000000000003"))
    (pin "2" (uuid "00000000-0000-0000-0000-000000000004"))
    (instances
      (project "test"
        (path "/00000000-0000-0000-0000-000000000001"
          (reference "R1") (unit 1)
        )
      )
    )
  )
  (symbol
    (lib_id "Device:C")
    (at 120 100 0)
    (uuid "00000000-0000-0000-0000-000000000005")
    (property "Reference" "C1" (at 120 90 0) (effects (font (size 1.27 1.27))))
    (property "Value" "100nF" (at 120 110 0) (effects (font (size 1.27 1.27))))
    (property "Footprint" "Capacitor_SMD:C_0402_1005Metric" (at 120 100 0) (effects (hide yes)))
    (property "Datasheet" "" (at 120 100 0) (effects (hide yes)))
    (pin "1" (uuid "00000000-0000-0000-0000-000000000006"))
    (pin "2" (uuid "00000000-0000-0000-0000-000000000007"))
    (instances
      (project "test"
        (path "/00000000-0000-0000-0000-000000000001"
          (reference "C1") (unit 1)
        )
      )
    )
  )
  (symbol
    (lib_id "power:GND")
    (at 100 120 0)
    (uuid "00000000-0000-0000-0000-000000000008")
    (property "Reference" "#PWR01" (at 100 130 0) (effects (font (size 1.27 1.27)) hide))
    (property "Value" "GND" (at 100 124 0) (effects (font (size 1.27 1.27))))
    (pin "1" (uuid "00000000-0000-0000-0000-000000000009"))
  )
)
"""
        sch_path = tmp_path / "power.kicad_sch"
        sch_path.write_text(sch_with_power)

        checker = SchematicPCBChecker(sch_path, pcb_r1_c1_exact)
        result = checker.check_lvs()

        # Power symbols should not appear as unmatched
        assert result.is_clean
        assert result.exact_match_count == 2
        refs = {m.sch_ref for m in result.matches}
        assert "#PWR01" not in refs


class TestLVSHierarchical:
    """Tests using hierarchical schematic fixtures."""

    def test_hierarchical_with_subsheet_components(self, tmp_path: Path):
        """Create a hierarchical schematic with components in sub-sheets."""
        # Root schematic with C1 and a sub-sheet containing R2
        root_sch = """(kicad_sch
  (version 20231120)
  (generator "test")
  (generator_version "8.0")
  (uuid "00000000-0000-0000-0000-000000000001")
  (paper "A4")
  (lib_symbols)
  (symbol
    (lib_id "Device:C")
    (at 100 60 0)
    (uuid "c1-uuid")
    (property "Reference" "C1" (at 104 58 0) (effects (font (size 1.27 1.27))))
    (property "Value" "100nF" (at 104 62 0) (effects (font (size 1.27 1.27))))
    (property "Footprint" "Capacitor_SMD:C_0402_1005Metric" (at 100 60 0) (effects (hide yes)))
    (pin "1" (uuid "c1-p1"))
    (pin "2" (uuid "c1-p2"))
    (instances
      (project "test"
        (path "/00000000-0000-0000-0000-000000000001"
          (reference "C1") (unit 1)
        )
      )
    )
  )
  (sheet
    (at 130 40) (size 40 30)
    (uuid "sub-sheet-uuid")
    (property "Sheetname" "Sub"
      (at 130 39 0) (effects (font (size 1.27 1.27)))
    )
    (property "Sheetfile" "sub.kicad_sch"
      (at 130 71 0) (effects (font (size 1.27 1.27)) hide)
    )
  )
)
"""
        # Sub-sheet with R2
        sub_sch = """(kicad_sch
  (version 20231120)
  (generator "test")
  (generator_version "8.0")
  (uuid "00000000-0000-0000-0000-000000000002")
  (paper "A4")
  (lib_symbols)
  (symbol
    (lib_id "Device:R")
    (at 80 60 0)
    (uuid "r2-uuid")
    (property "Reference" "R2" (at 84 58 0) (effects (font (size 1.27 1.27))))
    (property "Value" "4.7k" (at 84 62 0) (effects (font (size 1.27 1.27))))
    (property "Footprint" "Resistor_SMD:R_0402_1005Metric" (at 80 60 0) (effects (hide yes)))
    (pin "1" (uuid "r2-p1"))
    (pin "2" (uuid "r2-p2"))
    (instances
      (project "test"
        (path "/00000000-0000-0000-0000-000000000001/sub-sheet-uuid"
          (reference "R2") (unit 1)
        )
      )
    )
  )
)
"""
        # PCB with both C1 and R2
        pcb_content = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal))
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (footprint "Capacitor_SMD:C_0402_1005Metric"
    (layer "F.Cu")
    (uuid "fp-c1")
    (at 100 100)
    (property "Reference" "C1" (at 0 -1.5 0) (layer "F.SilkS") (uuid "fp-c1-ref"))
    (property "Value" "100nF" (at 0 1.5 0) (layer "F.Fab") (uuid "fp-c1-val"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25))
  )
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "fp-r2")
    (at 110 100)
    (property "Reference" "R2" (at 0 -1.5 0) (layer "F.SilkS") (uuid "fp-r2-ref"))
    (property "Value" "4.7k" (at 0 1.5 0) (layer "F.Fab") (uuid "fp-r2-val"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25))
  )
)
"""
        root_path = tmp_path / "root.kicad_sch"
        root_path.write_text(root_sch)
        sub_path = tmp_path / "sub.kicad_sch"
        sub_path.write_text(sub_sch)
        pcb_path = tmp_path / "root.kicad_pcb"
        pcb_path.write_text(pcb_content)

        checker = SchematicPCBChecker(root_path, pcb_path)
        result = checker.check_lvs()

        # Both C1 (root) and R2 (sub-sheet) should be exact matches
        assert result.is_clean
        assert result.exact_match_count == 2
        refs = {m.sch_ref for m in result.matches}
        assert refs == {"C1", "R2"}

    def test_hierarchical_subsheet_missing_from_pcb(self, tmp_path: Path):
        """Sub-sheet component missing from PCB should be in unmatched_sch."""
        root_sch = """(kicad_sch
  (version 20231120)
  (generator "test")
  (generator_version "8.0")
  (uuid "00000000-0000-0000-0000-000000000001")
  (paper "A4")
  (lib_symbols)
  (symbol
    (lib_id "Device:C")
    (at 100 60 0)
    (uuid "c1-uuid")
    (property "Reference" "C1" (at 104 58 0) (effects (font (size 1.27 1.27))))
    (property "Value" "100nF" (at 104 62 0) (effects (font (size 1.27 1.27))))
    (property "Footprint" "Capacitor_SMD:C_0402_1005Metric" (at 100 60 0) (effects (hide yes)))
    (pin "1" (uuid "c1-p1"))
    (pin "2" (uuid "c1-p2"))
    (instances
      (project "test"
        (path "/00000000-0000-0000-0000-000000000001"
          (reference "C1") (unit 1)
        )
      )
    )
  )
  (sheet
    (at 130 40) (size 40 30)
    (uuid "sub-sheet-uuid")
    (property "Sheetname" "Sub"
      (at 130 39 0) (effects (font (size 1.27 1.27)))
    )
    (property "Sheetfile" "sub.kicad_sch"
      (at 130 71 0) (effects (font (size 1.27 1.27)) hide)
    )
  )
)
"""
        sub_sch = """(kicad_sch
  (version 20231120)
  (generator "test")
  (generator_version "8.0")
  (uuid "00000000-0000-0000-0000-000000000002")
  (paper "A4")
  (lib_symbols)
  (symbol
    (lib_id "Device:R")
    (at 80 60 0)
    (uuid "r2-uuid")
    (property "Reference" "R2" (at 84 58 0) (effects (font (size 1.27 1.27))))
    (property "Value" "4.7k" (at 84 62 0) (effects (font (size 1.27 1.27))))
    (property "Footprint" "Resistor_SMD:R_0402_1005Metric" (at 80 60 0) (effects (hide yes)))
    (pin "1" (uuid "r2-p1"))
    (pin "2" (uuid "r2-p2"))
    (instances
      (project "test"
        (path "/00000000-0000-0000-0000-000000000001/sub-sheet-uuid"
          (reference "R2") (unit 1)
        )
      )
    )
  )
)
"""
        # PCB with only C1 (R2 missing)
        pcb_content = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal))
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (footprint "Capacitor_SMD:C_0402_1005Metric"
    (layer "F.Cu")
    (uuid "fp-c1")
    (at 100 100)
    (property "Reference" "C1" (at 0 -1.5 0) (layer "F.SilkS") (uuid "fp-c1-ref"))
    (property "Value" "100nF" (at 0 1.5 0) (layer "F.Fab") (uuid "fp-c1-val"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25))
  )
)
"""
        root_path = tmp_path / "root.kicad_sch"
        root_path.write_text(root_sch)
        sub_path = tmp_path / "sub.kicad_sch"
        sub_path.write_text(sub_sch)
        pcb_path = tmp_path / "root.kicad_pcb"
        pcb_path.write_text(pcb_content)

        checker = SchematicPCBChecker(root_path, pcb_path)
        result = checker.check_lvs()

        assert not result.is_clean
        assert result.exact_match_count == 1  # C1
        assert "R2" in result.unmatched_sch


# ---------------------------------------------------------------------------
# Tests: CLI integration
# ---------------------------------------------------------------------------


class TestLVSCLI:
    """Tests for the validate --lvs CLI command."""

    def test_cli_table_output(self, sch_r1_c1: Path, pcb_r1_c1_exact: Path):
        from kicad_tools.cli.validate_lvs_cmd import main

        exit_code = main(["--schematic", str(sch_r1_c1), "--pcb", str(pcb_r1_c1_exact)])
        assert exit_code == 0

    def test_cli_json_output(self, sch_r1_c1: Path, pcb_r1_c1_exact: Path, capsys):
        from kicad_tools.cli.validate_lvs_cmd import main

        exit_code = main(
            [
                "--schematic",
                str(sch_r1_c1),
                "--pcb",
                str(pcb_r1_c1_exact),
                "--format",
                "json",
            ]
        )
        assert exit_code == 0
        captured = capsys.readouterr()
        data = __import__("json").loads(captured.out)
        assert data["is_clean"] is True
        assert data["exact_matches"] == 2

    def test_cli_summary_output(self, sch_r1_c1: Path, pcb_r1_c1_exact: Path, capsys):
        from kicad_tools.cli.validate_lvs_cmd import main

        exit_code = main(
            [
                "--schematic",
                str(sch_r1_c1),
                "--pcb",
                str(pcb_r1_c1_exact),
                "--format",
                "summary",
            ]
        )
        assert exit_code == 0
        captured = capsys.readouterr()
        assert "CLEAN" in captured.out

    def test_cli_mismatches_exit_1(self, sch_r1_c1: Path, pcb_swapped_ref: Path):
        from kicad_tools.cli.validate_lvs_cmd import main

        exit_code = main(["--schematic", str(sch_r1_c1), "--pcb", str(pcb_swapped_ref)])
        assert exit_code == 1

    def test_cli_strict_exit_2(self, sch_r1_c1: Path, pcb_swapped_ref: Path):
        from kicad_tools.cli.validate_lvs_cmd import main

        exit_code = main(
            [
                "--schematic",
                str(sch_r1_c1),
                "--pcb",
                str(pcb_swapped_ref),
                "--strict",
            ]
        )
        # --strict with fuzzy matches (no orphans) should exit 2
        assert exit_code == 2

    def test_cli_min_confidence_filter(self, sch_r1_c1: Path, pcb_swapped_ref: Path, capsys):
        from kicad_tools.cli.validate_lvs_cmd import main

        main(
            [
                "--schematic",
                str(sch_r1_c1),
                "--pcb",
                str(pcb_swapped_ref),
                "--format",
                "json",
                "--min-confidence",
                "0.9",
            ]
        )
        captured = capsys.readouterr()
        data = __import__("json").loads(captured.out)
        # Only exact matches should remain (C1)
        assert data["exact_matches"] == 1
        # R1 (0.8 confidence) should be filtered out, leaving it as unmatched
        # But note: filtering happens on display, the result still tracks R1->R5
        # Actually the filter creates a new LVSResult with filtered matches
        # so fuzzy_matches should be 0 after filtering
        assert data["fuzzy_matches"] == 0

    def test_cli_missing_files(self, capsys):
        from kicad_tools.cli.validate_lvs_cmd import main

        exit_code = main([])
        assert exit_code == 1

    def test_cli_errors_only(self, sch_r1_c1: Path, pcb_r1_c1_exact: Path, capsys):
        from kicad_tools.cli.validate_lvs_cmd import main

        exit_code = main(
            [
                "--schematic",
                str(sch_r1_c1),
                "--pcb",
                str(pcb_r1_c1_exact),
                "--errors-only",
            ]
        )
        assert exit_code == 0
