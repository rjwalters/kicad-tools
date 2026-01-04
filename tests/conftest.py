"""Pytest fixtures for kicad-tools tests."""

from pathlib import Path

import pytest


@pytest.fixture
def fixtures_dir() -> Path:
    """Return the path to the test fixtures directory."""
    return Path(__file__).parent / "fixtures"


@pytest.fixture
def simple_rc_schematic(fixtures_dir: Path) -> Path:
    """Return the path to the simple RC circuit schematic."""
    return fixtures_dir / "simple_rc.kicad_sch"


# Minimal KiCad schematic for testing
MINIMAL_SCHEMATIC = """(kicad_sch
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
          (reference "R1")
          (unit 1)
        )
      )
    )
  )
  (wire
    (pts (xy 90 100) (xy 100 100))
    (stroke (width 0) (type default))
    (uuid "00000000-0000-0000-0000-000000000005")
  )
  (label "NET1"
    (at 90 100 0)
    (effects (font (size 1.27 1.27)))
    (uuid "00000000-0000-0000-0000-000000000006")
  )
)
"""

# Minimal KiCad PCB for testing
MINIMAL_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general
    (thickness 1.6)
    (legacy_teardrops no)
  )
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (32 "B.Adhes" user "B.Adhesive")
    (33 "F.Adhes" user "F.Adhesive")
    (34 "B.Paste" user)
    (35 "F.Paste" user)
    (36 "B.SilkS" user "B.Silkscreen")
    (37 "F.SilkS" user "F.Silkscreen")
    (38 "B.Mask" user)
    (39 "F.Mask" user)
    (40 "Dwgs.User" user "User.Drawings")
    (41 "Cmts.User" user "User.Comments")
    (42 "Eco1.User" user "User.Eco1")
    (43 "Eco2.User" user "User.Eco2")
    (44 "Edge.Cuts" user)
    (45 "Margin" user)
    (46 "B.CrtYd" user "B.Courtyard")
    (47 "F.CrtYd" user "F.Courtyard")
    (48 "B.Fab" user)
    (49 "F.Fab" user)
    (50 "User.1" user)
    (51 "User.2" user)
    (52 "User.3" user)
    (53 "User.4" user)
    (54 "User.5" user)
    (55 "User.6" user)
    (56 "User.7" user)
    (57 "User.8" user)
    (58 "User.9" user)
  )
  (setup
    (stackup
      (layer "F.SilkS" (type "Top Silk Screen"))
      (layer "F.Paste" (type "Top Solder Paste"))
      (layer "F.Mask" (type "Top Solder Mask") (thickness 0.01))
      (layer "F.Cu" (type "copper") (thickness 0.035))
      (layer "dielectric 1" (type "core") (thickness 1.51) (material "FR4") (epsilon_r 4.5) (loss_tangent 0.02))
      (layer "B.Cu" (type "copper") (thickness 0.035))
      (layer "B.Mask" (type "Bottom Solder Mask") (thickness 0.01))
      (layer "B.Paste" (type "Bottom Solder Paste"))
      (layer "B.SilkS" (type "Bottom Silk Screen"))
      (copper_finish "None")
      (dielectric_constraints no)
    )
    (pad_to_mask_clearance 0)
  )
  (net 0 "")
  (net 1 "GND")
  (net 2 "+3.3V")
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000010")
    (at 100 100)
    (property "Reference" "R1" (at 0 -1.5 0) (layer "F.SilkS") (uuid "00000000-0000-0000-0000-000000000011"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab") (uuid "00000000-0000-0000-0000-000000000012"))
    (property "Footprint" "Resistor_SMD:R_0402_1005Metric" (at 0 0 0) (layer "F.Fab") (hide yes) (uuid "00000000-0000-0000-0000-000000000013"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 1 "GND"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 2 "+3.3V"))
  )
  (segment (start 100 100) (end 110 100) (width 0.2) (layer "F.Cu") (net 1) (uuid "00000000-0000-0000-0000-000000000020"))
)
"""


@pytest.fixture
def minimal_schematic(tmp_path: Path) -> Path:
    """Create a minimal schematic file for testing."""
    sch_file = tmp_path / "test.kicad_sch"
    sch_file.write_text(MINIMAL_SCHEMATIC)
    return sch_file


@pytest.fixture
def minimal_pcb(tmp_path: Path) -> Path:
    """Create a minimal PCB file for testing."""
    pcb_file = tmp_path / "test.kicad_pcb"
    pcb_file.write_text(MINIMAL_PCB)
    return pcb_file


# PCB that passes all DRC checks - for testing kct check command
# This fixture is specifically designed to have no DRC violations:
# - Board outline with adequate edge clearance
# - Traces with proper clearance from pads
# - Silkscreen text with adequate height (1.0mm >= JLCPCB 0.8mm minimum)
DRC_CLEAN_PCB = """(kicad_pcb
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
  (net 1 "GND")
  (net 2 "+3.3V")
  (gr_rect (start 100 100) (end 150 150)
    (stroke (width 0.1) (type default))
    (fill none)
    (layer "Edge.Cuts")
  )
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000010")
    (at 125 125)
    (property "Reference" "R1" (at 0 -1.5 0) (layer "F.SilkS")
      (effects (font (size 1.0 1.0) (thickness 0.15)))
      (uuid "00000000-0000-0000-0000-000000000011"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab") (uuid "00000000-0000-0000-0000-000000000012"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64)
      (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 1 "GND"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64)
      (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 2 "+3.3V"))
  )
  (segment (start 122 125) (end 115 125) (width 0.25) (layer "F.Cu") (net 1)
    (uuid "00000000-0000-0000-0000-000000000020"))
)
"""


@pytest.fixture
def drc_clean_pcb(tmp_path: Path) -> Path:
    """Create a PCB file that passes all DRC checks."""
    pcb_file = tmp_path / "drc_clean.kicad_pcb"
    pcb_file.write_text(DRC_CLEAN_PCB)
    return pcb_file


# PCB with edge cuts and multiple components for routing tests
ROUTING_TEST_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general
    (thickness 1.6)
    (legacy_teardrops no)
  )
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (setup
    (pad_to_mask_clearance 0)
  )
  (net 0 "")
  (net 1 "NET1")
  (net 2 "GND")
  (net 3 "+3.3V")
  (gr_rect (start 100 100) (end 150 140)
    (stroke (width 0.1) (type default))
    (fill none)
    (layer "Edge.Cuts")
  )
  (footprint "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000100")
    (at 115 115)
    (fp_text reference "U1" (at 0 -3.5) (layer "F.SilkS"))
    (pad "1" smd rect (at -2.7 -1.905) (size 1.5 0.6) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "NET1"))
    (pad "2" smd rect (at -2.7 -0.635) (size 1.5 0.6) (layers "F.Cu" "F.Paste" "F.Mask") (net 2 "GND"))
    (pad "3" smd rect (at -2.7 0.635) (size 1.5 0.6) (layers "F.Cu" "F.Paste" "F.Mask") (net 2 "GND"))
    (pad "4" smd rect (at -2.7 1.905) (size 1.5 0.6) (layers "F.Cu" "F.Paste" "F.Mask") (net 3 "+3.3V"))
    (pad "5" smd rect (at 2.7 1.905) (size 1.5 0.6) (layers "F.Cu" "F.Paste" "F.Mask") (net 3 "+3.3V"))
    (pad "6" smd rect (at 2.7 0.635) (size 1.5 0.6) (layers "F.Cu" "F.Paste" "F.Mask") (net 0 ""))
    (pad "7" smd rect (at 2.7 -0.635) (size 1.5 0.6) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "NET1"))
    (pad "8" smd rect (at 2.7 -1.905) (size 1.5 0.6) (layers "F.Cu" "F.Paste" "F.Mask") (net 3 "+3.3V"))
  )
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000200")
    (at 135 115)
    (fp_text reference "R1" (at 0 -1.5) (layer "F.SilkS"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 1 "NET1"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 2 "GND"))
  )
  (footprint "Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000300")
    (at 125 130)
    (fp_text reference "J1" (at 0 -2.5) (layer "F.SilkS"))
    (pad "1" thru_hole circle (at 0 0) (size 1.7 1.7) (drill 1.0) (layers "*.Cu" "*.Mask") (net 3 "+3.3V"))
    (pad "2" thru_hole oval (at 0 2.54) (size 1.7 1.7) (drill 1.0) (layers "*.Cu" "*.Mask") (net 2 "GND"))
  )
)
"""


@pytest.fixture
def routing_test_pcb(tmp_path: Path) -> Path:
    """Create a PCB file with components for routing tests."""
    pcb_file = tmp_path / "routing_test.kicad_pcb"
    pcb_file.write_text(ROUTING_TEST_PCB)
    return pcb_file


# PCB with zones for testing zone parsing
ZONE_TEST_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general
    (thickness 1.6)
    (legacy_teardrops no)
  )
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (setup
    (pad_to_mask_clearance 0)
  )
  (net 0 "")
  (net 1 "GND")
  (net 2 "+3.3V")
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000010")
    (at 110 110)
    (property "Reference" "R1" (at 0 -1.5 0) (layer "F.SilkS") (uuid "ref-uuid"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "GND"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (net 2 "+3.3V"))
  )
  (zone
    (net 1)
    (net_name "GND")
    (layer "F.Cu")
    (uuid "zone-uuid-1")
    (name "GND_Zone")
    (hatch edge 0.5)
    (priority 1)
    (connect_pads (clearance 0.25))
    (min_thickness 0.15)
    (filled_areas_thickness no)
    (fill yes (thermal_gap 0.4) (thermal_bridge_width 0.35))
    (polygon
      (pts
        (xy 100 100)
        (xy 130 100)
        (xy 130 120)
        (xy 100 120)
      )
    )
    (filled_polygon
      (layer "F.Cu")
      (pts
        (xy 100.1 100.1)
        (xy 129.9 100.1)
        (xy 129.9 119.9)
        (xy 100.1 119.9)
      )
    )
  )
  (zone
    (net 2)
    (net_name "+3.3V")
    (layer "B.Cu")
    (uuid "zone-uuid-2")
    (hatch edge 0.5)
    (priority 0)
    (connect_pads yes (clearance 0.2))
    (min_thickness 0.2)
    (fill no (thermal_gap 0.3) (thermal_bridge_width 0.3))
    (polygon
      (pts
        (xy 100 100)
        (xy 120 100)
        (xy 120 115)
        (xy 100 115)
      )
    )
  )
  (zone
    (net 1)
    (net_name "GND")
    (layer "B.Cu")
    (uuid "zone-uuid-3")
    (hatch edge 0.5)
    (connect_pads no)
    (min_thickness 0.2)
    (fill yes)
    (polygon
      (pts
        (xy 140 100)
        (xy 160 100)
        (xy 160 120)
        (xy 155 125)
        (xy 145 125)
        (xy 140 120)
      )
    )
  )
)
"""


@pytest.fixture
def zone_test_pcb(tmp_path: Path) -> Path:
    """Create a PCB file with zones for testing zone parsing."""
    pcb_file = tmp_path / "zone_test.kicad_pcb"
    pcb_file.write_text(ZONE_TEST_PCB)
    return pcb_file


# Minimal KiCad footprint (KiCad 6+ format)
MINIMAL_FOOTPRINT = """(footprint "R_0402_1005Metric"
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (layer "F.Cu")
  (descr "Resistor SMD 0402 (1005 Metric)")
  (tags "resistor 0402")
  (property "Reference" "REF**" (at 0 -1.1 0) (layer "F.SilkS") (uuid "ref-uuid"))
  (property "Value" "R_0402_1005Metric" (at 0 1.1 0) (layer "F.Fab") (uuid "val-uuid"))
  (fp_line (start -0.153641 -0.38) (end 0.153641 -0.38) (stroke (width 0.12) (type solid)) (layer "F.SilkS"))
  (fp_line (start -0.153641 0.38) (end 0.153641 0.38) (stroke (width 0.12) (type solid)) (layer "F.SilkS"))
  (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25))
  (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25))
  (model "${KICAD8_3DMODEL_DIR}/Resistor_SMD.3dshapes/R_0402_1005Metric.wrl"
    (offset (xyz 0 0 0))
    (scale (xyz 1 1 1))
    (rotate (xyz 0 0 0))
  )
)
"""

# Minimal KiCad footprint (KiCad 5 format with "module" tag)
MINIMAL_FOOTPRINT_KICAD5 = """(module "R_0402_1005Metric"
  (layer "F.Cu")
  (descr "Resistor SMD 0402 (1005 Metric)")
  (tags "resistor 0402")
  (fp_text reference "REF**" (at 0 -1.1) (layer "F.SilkS"))
  (fp_text value "R_0402_1005Metric" (at 0 1.1) (layer "F.Fab"))
  (pad 1 smd roundrect (at -0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25))
  (pad 2 smd roundrect (at 0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25))
)
"""

# Minimal KiCad design rules file
MINIMAL_DESIGN_RULES = """(version 1)
(rule "Trace Width"
  (constraint track_width (min 0.127mm)))
(rule "Clearance"
  (constraint clearance (min 0.127mm)))
(rule "Via Drill"
  (constraint hole_size (min 0.3mm)))
(rule "Via Diameter"
  (constraint via_diameter (min 0.6mm)))
(rule "Annular Ring"
  (constraint annular_width (min 0.15mm)))
(rule "Copper to Edge"
  (constraint edge_clearance (min 0.3mm)))
"""


@pytest.fixture
def minimal_footprint(tmp_path: Path) -> Path:
    """Create a minimal footprint file for testing (KiCad 6+ format)."""
    mod_file = tmp_path / "R_0402_1005Metric.kicad_mod"
    mod_file.write_text(MINIMAL_FOOTPRINT)
    return mod_file


@pytest.fixture
def minimal_footprint_kicad5(tmp_path: Path) -> Path:
    """Create a minimal footprint file for testing (KiCad 5 format)."""
    mod_file = tmp_path / "R_0402_1005Metric_v5.kicad_mod"
    mod_file.write_text(MINIMAL_FOOTPRINT_KICAD5)
    return mod_file


@pytest.fixture
def footprint_library_dir(tmp_path: Path) -> Path:
    """Create a .pretty directory with multiple footprints for testing."""
    pretty_dir = tmp_path / "TestLib.pretty"
    pretty_dir.mkdir()

    # Add a couple of footprints
    (pretty_dir / "R_0402_1005Metric.kicad_mod").write_text(MINIMAL_FOOTPRINT)
    (pretty_dir / "C_0402_1005Metric.kicad_mod").write_text(
        MINIMAL_FOOTPRINT.replace("R_0402", "C_0402").replace("Resistor", "Capacitor")
    )

    return pretty_dir


@pytest.fixture
def minimal_design_rules(tmp_path: Path) -> Path:
    """Create a minimal design rules file for testing."""
    dru_file = tmp_path / "test.kicad_dru"
    dru_file.write_text(MINIMAL_DESIGN_RULES)
    return dru_file


# Minimal KiCad symbol library for testing
MINIMAL_SYMBOL_LIBRARY = """(kicad_symbol_lib
  (version "20231120")
  (generator "test")
  (symbol "Device:R"
    (property "Reference" "R" (at 0 0 0) (effects (font (size 1.27 1.27))))
    (property "Value" "R" (at 0 2.54 0) (effects (font (size 1.27 1.27))))
    (property "Footprint" "" (at 0 0 0) (effects (hide yes)))
    (property "Datasheet" "" (at 0 0 0) (effects (hide yes)))
    (symbol "Device:R_0_1"
      (pin passive line (at -2.54 0 0) (length 2.54) (name "1") (number "1"))
      (pin passive line (at 2.54 0 180) (length 2.54) (name "2") (number "2"))
    )
  )
  (symbol "Device:C"
    (property "Reference" "C" (at 0 0 0) (effects (font (size 1.27 1.27))))
    (property "Value" "C" (at 0 2.54 0) (effects (font (size 1.27 1.27))))
    (symbol "Device:C_0_1"
      (pin passive line (at -2.54 0 0) (length 2.54) (name "1") (number "1"))
      (pin passive line (at 2.54 0 180) (length 2.54) (name "2") (number "2"))
    )
  )
)
"""


@pytest.fixture
def minimal_symbol_library(tmp_path: Path) -> Path:
    """Create a minimal symbol library file for testing."""
    lib_file = tmp_path / "test.kicad_sym"
    lib_file.write_text(MINIMAL_SYMBOL_LIBRARY)
    return lib_file


@pytest.fixture
def hierarchical_schematic(fixtures_dir: Path) -> Path:
    """Return the path to the hierarchical test schematic."""
    return fixtures_dir / "projects" / "hierarchical_main.kicad_sch"
