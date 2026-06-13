"""Tests for the sch preflight pre-layout validation command."""

import json
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Fixtures: schematic snippets used by the preflight checks
# ---------------------------------------------------------------------------

# A schematic with a symbol whose footprint is missing the library prefix.
_SCH_BAD_FOOTPRINT_NO_PREFIX = """(kicad_sch
  (version 20231120)
  (generator "test")
  (uuid "00000000-0000-0000-0000-000000000001")
  (paper "A4")
  (lib_symbols)
  (symbol
    (lib_id "Device:R")
    (at 100 100 0)
    (uuid "00000000-0000-0000-0000-000000000002")
    (property "Reference" "R1" (at 100 90 0) (effects (font (size 1.27 1.27))))
    (property "Value" "10k" (at 100 110 0) (effects (font (size 1.27 1.27))))
    (property "Footprint" "R_0402_1005Metric" (at 100 100 0) (effects (hide yes)))
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
)
"""

# A schematic with an empty library prefix (":" with no library name).
_SCH_EMPTY_LIB_PREFIX = """(kicad_sch
  (version 20231120)
  (generator "test")
  (uuid "00000000-0000-0000-0000-000000000001")
  (paper "A4")
  (lib_symbols)
  (symbol
    (lib_id "Device:R")
    (at 100 100 0)
    (uuid "00000000-0000-0000-0000-000000000002")
    (property "Reference" "R1" (at 100 90 0) (effects (font (size 1.27 1.27))))
    (property "Value" "10k" (at 100 110 0) (effects (font (size 1.27 1.27))))
    (property "Footprint" ":R_0402" (at 100 100 0) (effects (hide yes)))
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
)
"""

# A schematic with a generic value ("R") that should be flagged.
_SCH_GENERIC_VALUE = """(kicad_sch
  (version 20231120)
  (generator "test")
  (uuid "00000000-0000-0000-0000-000000000001")
  (paper "A4")
  (lib_symbols)
  (symbol
    (lib_id "Device:R")
    (at 100 100 0)
    (uuid "00000000-0000-0000-0000-000000000002")
    (property "Reference" "R1" (at 100 90 0) (effects (font (size 1.27 1.27))))
    (property "Value" "R" (at 100 110 0) (effects (font (size 1.27 1.27))))
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
)
"""

# A schematic with power symbols but no PWR_FLAG.
_SCH_NO_PWR_FLAG = """(kicad_sch
  (version 20231120)
  (generator "test")
  (uuid "00000000-0000-0000-0000-000000000001")
  (paper "A4")
  (lib_symbols)
  (symbol
    (lib_id "power:VCC")
    (at 100 50 0)
    (uuid "00000000-0000-0000-0000-000000000010")
    (property "Reference" "#PWR01" (at 100 45 0) (effects (font (size 1.27 1.27))))
    (property "Value" "VCC" (at 100 55 0) (effects (font (size 1.27 1.27))))
    (property "Footprint" "" (at 100 50 0) (effects (hide yes)))
    (property "Datasheet" "" (at 100 50 0) (effects (hide yes)))
    (pin "1" (uuid "00000000-0000-0000-0000-000000000011"))
    (instances
      (project "test"
        (path "/00000000-0000-0000-0000-000000000001"
          (reference "#PWR01")
          (unit 1)
        )
      )
    )
  )
  (symbol
    (lib_id "power:GND")
    (at 100 150 0)
    (uuid "00000000-0000-0000-0000-000000000020")
    (property "Reference" "#PWR02" (at 100 155 0) (effects (font (size 1.27 1.27))))
    (property "Value" "GND" (at 100 145 0) (effects (font (size 1.27 1.27))))
    (property "Footprint" "" (at 100 150 0) (effects (hide yes)))
    (property "Datasheet" "" (at 100 150 0) (effects (hide yes)))
    (pin "1" (uuid "00000000-0000-0000-0000-000000000021"))
    (instances
      (project "test"
        (path "/00000000-0000-0000-0000-000000000001"
          (reference "#PWR02")
          (unit 1)
        )
      )
    )
  )
)
"""

# A schematic with power symbols AND a PWR_FLAG -- should pass.
_SCH_WITH_PWR_FLAG = """(kicad_sch
  (version 20231120)
  (generator "test")
  (uuid "00000000-0000-0000-0000-000000000001")
  (paper "A4")
  (lib_symbols)
  (symbol
    (lib_id "power:VCC")
    (at 100 50 0)
    (uuid "00000000-0000-0000-0000-000000000010")
    (property "Reference" "#PWR01" (at 100 45 0) (effects (font (size 1.27 1.27))))
    (property "Value" "VCC" (at 100 55 0) (effects (font (size 1.27 1.27))))
    (property "Footprint" "" (at 100 50 0) (effects (hide yes)))
    (property "Datasheet" "" (at 100 50 0) (effects (hide yes)))
    (pin "1" (uuid "00000000-0000-0000-0000-000000000011"))
    (instances
      (project "test"
        (path "/00000000-0000-0000-0000-000000000001"
          (reference "#PWR01")
          (unit 1)
        )
      )
    )
  )
  (symbol
    (lib_id "power:PWR_FLAG")
    (at 110 50 0)
    (uuid "00000000-0000-0000-0000-000000000030")
    (property "Reference" "#FLG01" (at 110 45 0) (effects (font (size 1.27 1.27))))
    (property "Value" "PWR_FLAG" (at 110 55 0) (effects (font (size 1.27 1.27))))
    (property "Footprint" "" (at 110 50 0) (effects (hide yes)))
    (property "Datasheet" "" (at 110 50 0) (effects (hide yes)))
    (pin "1" (uuid "00000000-0000-0000-0000-000000000031"))
    (instances
      (project "test"
        (path "/00000000-0000-0000-0000-000000000001"
          (reference "#FLG01")
          (unit 1)
        )
      )
    )
  )
)
"""

# A schematic with a single-occurrence label (single-pin net).
_SCH_SINGLE_PIN_NET = """(kicad_sch
  (version 20231120)
  (generator "test")
  (uuid "00000000-0000-0000-0000-000000000001")
  (paper "A4")
  (lib_symbols)
  (label "LONELY_NET"
    (at 90 100 0)
    (effects (font (size 1.27 1.27)))
    (uuid "00000000-0000-0000-0000-000000000006")
  )
)
"""

# A schematic with a derived symbol using (extends ...).
# The base symbol "Regulator_Linear:AP2204K-1.5" has 5 pins.
# The derived symbol "Regulator_Linear:AP2112K-3.3" uses extends and has 0
# direct pins -- its pin count should be inherited from the base.
_SCH_EXTENDS_SYMBOL = """(kicad_sch
  (version 20231120)
  (generator "test")
  (uuid "00000000-0000-0000-0000-000000000001")
  (paper "A4")
  (lib_symbols
    (symbol "Regulator_Linear:AP2204K-1.5"
      (property "Reference" "U" (at 0 0 0) (effects (font (size 1.27 1.27))))
      (property "Value" "AP2204K-1.5" (at 0 0 0) (effects (font (size 1.27 1.27))))
      (property "Footprint" "Package_TO_SOT_SMD:SOT-23-5" (at 0 0 0) (effects (hide yes)))
      (property "Datasheet" "" (at 0 0 0) (effects (hide yes)))
      (symbol "AP2204K-1.5_1_1"
        (pin input line (at -10 2.54 0) (length 2.54)
          (name "VIN" (effects (font (size 1.27 1.27))))
          (number "1" (effects (font (size 1.27 1.27))))
        )
        (pin power_in line (at 0 -7.62 90) (length 2.54)
          (name "GND" (effects (font (size 1.27 1.27))))
          (number "2" (effects (font (size 1.27 1.27))))
        )
        (pin input line (at -10 -2.54 0) (length 2.54)
          (name "EN" (effects (font (size 1.27 1.27))))
          (number "3" (effects (font (size 1.27 1.27))))
        )
        (pin passive line (at 10 -2.54 180) (length 2.54)
          (name "NC" (effects (font (size 1.27 1.27))))
          (number "4" (effects (font (size 1.27 1.27))))
        )
        (pin power_out line (at 10 2.54 180) (length 2.54)
          (name "VOUT" (effects (font (size 1.27 1.27))))
          (number "5" (effects (font (size 1.27 1.27))))
        )
      )
    )
    (symbol "Regulator_Linear:AP2112K-3.3"
      (extends "Regulator_Linear:AP2204K-1.5")
      (property "Reference" "U" (at 0 0 0) (effects (font (size 1.27 1.27))))
      (property "Value" "AP2112K-3.3" (at 0 0 0) (effects (font (size 1.27 1.27))))
      (property "Footprint" "Package_TO_SOT_SMD:SOT-23-5" (at 0 0 0) (effects (hide yes)))
      (property "Datasheet" "" (at 0 0 0) (effects (hide yes)))
    )
  )
  (symbol
    (lib_id "Regulator_Linear:AP2112K-3.3")
    (at 100 100 0)
    (uuid "00000000-0000-0000-0000-000000000002")
    (property "Reference" "U1" (at 100 90 0) (effects (font (size 1.27 1.27))))
    (property "Value" "AP2112K-3.3" (at 100 110 0) (effects (font (size 1.27 1.27))))
    (property "Footprint" "Package_TO_SOT_SMD:SOT-23-5" (at 100 100 0) (effects (hide yes)))
    (property "Datasheet" "" (at 100 100 0) (effects (hide yes)))
    (pin "1" (uuid "00000000-0000-0000-0000-000000000101"))
    (pin "2" (uuid "00000000-0000-0000-0000-000000000102"))
    (pin "3" (uuid "00000000-0000-0000-0000-000000000103"))
    (pin "4" (uuid "00000000-0000-0000-0000-000000000104"))
    (pin "5" (uuid "00000000-0000-0000-0000-000000000105"))
    (instances
      (project "test"
        (path "/00000000-0000-0000-0000-000000000001"
          (reference "U1")
          (unit 1)
        )
      )
    )
  )
)
"""

# A schematic with a multi-level extends chain: C extends B extends A.
_SCH_MULTI_LEVEL_EXTENDS = """(kicad_sch
  (version 20231120)
  (generator "test")
  (uuid "00000000-0000-0000-0000-000000000001")
  (paper "A4")
  (lib_symbols
    (symbol "Device:BaseWidget"
      (property "Reference" "U" (at 0 0 0) (effects (font (size 1.27 1.27))))
      (property "Value" "BaseWidget" (at 0 0 0) (effects (font (size 1.27 1.27))))
      (property "Footprint" "Package_SO:SOIC-8" (at 0 0 0) (effects (hide yes)))
      (property "Datasheet" "" (at 0 0 0) (effects (hide yes)))
      (symbol "BaseWidget_1_1"
        (pin input line (at -10 0 0) (length 2.54)
          (name "A" (effects (font (size 1.27 1.27))))
          (number "1" (effects (font (size 1.27 1.27))))
        )
        (pin output line (at 10 0 180) (length 2.54)
          (name "B" (effects (font (size 1.27 1.27))))
          (number "2" (effects (font (size 1.27 1.27))))
        )
        (pin power_in line (at 0 -5 90) (length 2.54)
          (name "GND" (effects (font (size 1.27 1.27))))
          (number "3" (effects (font (size 1.27 1.27))))
        )
      )
    )
    (symbol "Device:MidWidget"
      (extends "Device:BaseWidget")
      (property "Reference" "U" (at 0 0 0) (effects (font (size 1.27 1.27))))
      (property "Value" "MidWidget" (at 0 0 0) (effects (font (size 1.27 1.27))))
      (property "Footprint" "Package_SO:SOIC-8" (at 0 0 0) (effects (hide yes)))
      (property "Datasheet" "" (at 0 0 0) (effects (hide yes)))
    )
    (symbol "Device:LeafWidget"
      (extends "Device:MidWidget")
      (property "Reference" "U" (at 0 0 0) (effects (font (size 1.27 1.27))))
      (property "Value" "LeafWidget" (at 0 0 0) (effects (font (size 1.27 1.27))))
      (property "Footprint" "Package_SO:SOIC-8" (at 0 0 0) (effects (hide yes)))
      (property "Datasheet" "" (at 0 0 0) (effects (hide yes)))
    )
  )
  (symbol
    (lib_id "Device:LeafWidget")
    (at 100 100 0)
    (uuid "00000000-0000-0000-0000-000000000002")
    (property "Reference" "U1" (at 100 90 0) (effects (font (size 1.27 1.27))))
    (property "Value" "LeafWidget" (at 100 110 0) (effects (font (size 1.27 1.27))))
    (property "Footprint" "Package_SO:SOIC-8" (at 100 100 0) (effects (hide yes)))
    (property "Datasheet" "" (at 100 100 0) (effects (hide yes)))
    (pin "1" (uuid "00000000-0000-0000-0000-000000000101"))
    (pin "2" (uuid "00000000-0000-0000-0000-000000000102"))
    (pin "3" (uuid "00000000-0000-0000-0000-000000000103"))
    (instances
      (project "test"
        (path "/00000000-0000-0000-0000-000000000001"
          (reference "U1")
          (unit 1)
        )
      )
    )
  )
)
"""

# An empty schematic (no components) -- should pass cleanly.
_SCH_EMPTY = """(kicad_sch
  (version 20231120)
  (generator "test")
  (uuid "00000000-0000-0000-0000-000000000001")
  (paper "A4")
  (lib_symbols)
)
"""


# ---------------------------------------------------------------------------
# Helper to write a schematic file from a string
# ---------------------------------------------------------------------------


@pytest.fixture
def _write_sch(tmp_path: Path):
    """Return a helper that writes schematic content to a temp file and returns its path."""

    def _inner(content: str, name: str = "test.kicad_sch") -> Path:
        p = tmp_path / name
        p.write_text(content)
        return p

    return _inner


# ---------------------------------------------------------------------------
# Individual check tests
# ---------------------------------------------------------------------------


class TestCheckFootprintLibraryResolution:
    """Tests for check_footprint_library_resolution."""

    def test_missing_library_prefix(self, _write_sch):
        from kicad_tools.cli.sch_preflight import check_footprint_library_resolution

        sch = _write_sch(_SCH_BAD_FOOTPRINT_NO_PREFIX)
        issues = check_footprint_library_resolution(str(sch))
        assert len(issues) >= 1
        assert any("missing library prefix" in i.message for i in issues)
        assert all(i.severity == "warning" for i in issues)

    def test_empty_library_prefix(self, _write_sch):
        from kicad_tools.cli.sch_preflight import check_footprint_library_resolution

        sch = _write_sch(_SCH_EMPTY_LIB_PREFIX)
        issues = check_footprint_library_resolution(str(sch))
        assert len(issues) >= 1
        assert any("empty library or footprint name" in i.message for i in issues)
        assert any(i.severity == "error" for i in issues)

    def test_valid_footprint_passes(self, minimal_schematic: Path):
        from kicad_tools.cli.sch_preflight import check_footprint_library_resolution

        issues = check_footprint_library_resolution(str(minimal_schematic))
        fp_issues = [i for i in issues if i.category == "footprint_resolution"]
        assert len(fp_issues) == 0

    def test_on_disk_check_flags_missing_kicad_mod(self, tmp_path: Path):
        """Project fp-lib-table + missing .kicad_mod => 'file not found' error."""
        from kicad_tools.cli.sch_preflight import check_footprint_library_resolution

        # Synthesize a project with a CustomLib in fp-lib-table but no
        # actual .kicad_mod file -- the on-disk check must flag this.
        (tmp_path / "proj.kicad_pro").write_text("{}", encoding="utf-8")
        (tmp_path / "fp-lib-table").write_text(
            '(fp_lib_table (lib (name "CustomLib") (type "KiCad")'
            ' (uri "${KIPRJMOD}/CustomLib.pretty") (options "") (descr "")))',
            encoding="utf-8",
        )
        (tmp_path / "CustomLib.pretty").mkdir()
        # Note: NO MissingPart.kicad_mod -- this is the failure we want to flag.

        sch = tmp_path / "proj.kicad_sch"
        sch.write_text(
            '(kicad_sch (version 20231120) (generator "test")'
            ' (uuid "00000000-0000-0000-0000-000000000001") (paper "A4")'
            " (lib_symbols)"
            ' (symbol (lib_id "Device:R") (at 0 0 0)'
            '   (uuid "00000000-0000-0000-0000-000000000002")'
            '   (property "Reference" "R1" (at 0 0 0))'
            '   (property "Value" "10k" (at 0 0 0))'
            '   (property "Footprint" "CustomLib:MissingPart" (at 0 0 0))'
            '   (property "Datasheet" "" (at 0 0 0))'
            '   (pin "1" (uuid "00000000-0000-0000-0000-000000000003"))'
            '   (pin "2" (uuid "00000000-0000-0000-0000-000000000004"))))',
            encoding="utf-8",
        )

        issues = check_footprint_library_resolution(str(sch))
        msgs = [i.message for i in issues if i.category == "footprint_resolution"]
        assert any("file not found" in m for m in msgs), msgs
        # The mod-file failure is an error, not just a warning.
        assert any(i.severity == "error" and "file not found" in i.message for i in issues)

    def test_on_disk_check_flags_unknown_nickname(self, tmp_path: Path):
        """Reference to a library nickname absent from both tables => error."""
        from kicad_tools.cli.sch_preflight import check_footprint_library_resolution

        # Project table exists but with a different nickname.  Symbol
        # footprint references an unknown nickname.
        (tmp_path / "proj.kicad_pro").write_text("{}", encoding="utf-8")
        (tmp_path / "fp-lib-table").write_text(
            '(fp_lib_table (lib (name "OtherLib") (type "KiCad")'
            ' (uri "${KIPRJMOD}/OtherLib.pretty") (options "") (descr "")))',
            encoding="utf-8",
        )
        (tmp_path / "OtherLib.pretty").mkdir()

        sch = tmp_path / "proj.kicad_sch"
        sch.write_text(
            '(kicad_sch (version 20231120) (generator "test")'
            ' (uuid "00000000-0000-0000-0000-000000000001") (paper "A4")'
            " (lib_symbols)"
            ' (symbol (lib_id "Device:R") (at 0 0 0)'
            '   (uuid "00000000-0000-0000-0000-000000000002")'
            '   (property "Reference" "R1" (at 0 0 0))'
            '   (property "Value" "10k" (at 0 0 0))'
            '   (property "Footprint" "TotallyUnknownLib:Foo" (at 0 0 0))'
            '   (property "Datasheet" "" (at 0 0 0))'
            '   (pin "1" (uuid "00000000-0000-0000-0000-000000000003"))'
            '   (pin "2" (uuid "00000000-0000-0000-0000-000000000004"))))',
            encoding="utf-8",
        )

        issues = check_footprint_library_resolution(str(sch))
        msgs = [i.message for i in issues if i.category == "footprint_resolution"]
        assert any("not found in" in m and "TotallyUnknownLib" in m for m in msgs), msgs

    def test_on_disk_check_passes_for_valid_project_lib(self, tmp_path: Path):
        """Synthesized project fp-lib-table + matching .kicad_mod => no errors."""
        from kicad_tools.cli.sch_preflight import check_footprint_library_resolution

        (tmp_path / "proj.kicad_pro").write_text("{}", encoding="utf-8")
        (tmp_path / "fp-lib-table").write_text(
            '(fp_lib_table (lib (name "CustomLib") (type "KiCad")'
            ' (uri "${KIPRJMOD}/CustomLib.pretty") (options "") (descr "")))',
            encoding="utf-8",
        )
        lib_dir = tmp_path / "CustomLib.pretty"
        lib_dir.mkdir()
        (lib_dir / "MyPart.kicad_mod").write_text(
            '(footprint "MyPart" (version 20240108) (generator "test") (layer "F.Cu"))',
            encoding="utf-8",
        )

        sch = tmp_path / "proj.kicad_sch"
        sch.write_text(
            '(kicad_sch (version 20231120) (generator "test")'
            ' (uuid "00000000-0000-0000-0000-000000000001") (paper "A4")'
            " (lib_symbols)"
            ' (symbol (lib_id "Device:R") (at 0 0 0)'
            '   (uuid "00000000-0000-0000-0000-000000000002")'
            '   (property "Reference" "R1" (at 0 0 0))'
            '   (property "Value" "10k" (at 0 0 0))'
            '   (property "Footprint" "CustomLib:MyPart" (at 0 0 0))'
            '   (property "Datasheet" "" (at 0 0 0))'
            '   (pin "1" (uuid "00000000-0000-0000-0000-000000000003"))'
            '   (pin "2" (uuid "00000000-0000-0000-0000-000000000004"))))',
            encoding="utf-8",
        )

        issues = check_footprint_library_resolution(str(sch))
        fp_issues = [i for i in issues if i.category == "footprint_resolution"]
        assert fp_issues == [], [i.message for i in fp_issues]


class TestCheckGenericValues:
    """Tests for check_generic_values."""

    def test_generic_value_flagged(self, _write_sch):
        from kicad_tools.cli.sch_preflight import check_generic_values

        sch = _write_sch(_SCH_GENERIC_VALUE)
        issues = check_generic_values(str(sch))
        assert len(issues) >= 1
        assert any("generic value" in i.message.lower() for i in issues)
        assert all(i.severity == "warning" for i in issues)

    def test_real_value_passes(self, minimal_schematic: Path):
        from kicad_tools.cli.sch_preflight import check_generic_values

        # minimal_schematic has value "10k" which is not generic
        issues = check_generic_values(str(minimal_schematic))
        assert len(issues) == 0


class TestCheckPowerFlags:
    """Tests for check_power_flags."""

    def test_no_pwr_flag_warns(self, _write_sch):
        from kicad_tools.cli.sch_preflight import check_power_flags

        sch = _write_sch(_SCH_NO_PWR_FLAG)
        issues = check_power_flags(str(sch))
        assert len(issues) == 1
        assert "PWR_FLAG" in issues[0].message
        assert issues[0].severity == "warning"

    def test_with_pwr_flag_passes(self, _write_sch):
        from kicad_tools.cli.sch_preflight import check_power_flags

        sch = _write_sch(_SCH_WITH_PWR_FLAG)
        issues = check_power_flags(str(sch))
        pf_issues = [i for i in issues if i.category == "power_flag"]
        assert len(pf_issues) == 0

    def test_no_power_symbols_passes(self, _write_sch):
        from kicad_tools.cli.sch_preflight import check_power_flags

        sch = _write_sch(_SCH_EMPTY)
        issues = check_power_flags(str(sch))
        assert len(issues) == 0


class TestCheckPinPadCountExtends:
    """Tests for check_pin_pad_count with derived (extends) symbols."""

    def test_derived_symbol_inherits_pin_count(self, _write_sch):
        """Derived symbol using (extends ...) should inherit base pin count."""
        from kicad_tools.cli.sch_preflight import check_pin_pad_count

        sch = _write_sch(_SCH_EXTENDS_SYMBOL)
        issues = check_pin_pad_count(str(sch))
        mismatch_issues = [i for i in issues if i.category == "pin_pad_mismatch"]
        assert len(mismatch_issues) == 0, (
            f"Expected no pin_pad_mismatch for derived symbol, got: "
            f"{[i.message for i in mismatch_issues]}"
        )

    def test_multi_level_extends_chain(self, _write_sch):
        """Multi-level extends chain (C extends B extends A) resolves transitively."""
        from kicad_tools.cli.sch_preflight import check_pin_pad_count

        sch = _write_sch(_SCH_MULTI_LEVEL_EXTENDS)
        issues = check_pin_pad_count(str(sch))
        mismatch_issues = [i for i in issues if i.category == "pin_pad_mismatch"]
        assert len(mismatch_issues) == 0, (
            f"Expected no pin_pad_mismatch for multi-level extends, got: "
            f"{[i.message for i in mismatch_issues]}"
        )


class TestCheckSinglePinNets:
    """Tests for check_single_pin_nets."""

    def test_single_pin_net_detected(self, _write_sch):
        from kicad_tools.cli.sch_preflight import check_single_pin_nets

        sch = _write_sch(_SCH_SINGLE_PIN_NET)
        issues = check_single_pin_nets(str(sch))
        assert len(issues) >= 1
        assert any("LONELY_NET" in i.message for i in issues)
        assert all(i.severity == "warning" for i in issues)

    def test_empty_schematic_passes(self, _write_sch):
        from kicad_tools.cli.sch_preflight import check_single_pin_nets

        sch = _write_sch(_SCH_EMPTY)
        issues = check_single_pin_nets(str(sch))
        assert len(issues) == 0


# ---------------------------------------------------------------------------
# Integration: run_preflight
# ---------------------------------------------------------------------------


class TestRunPreflight:
    """Tests for the run_preflight aggregator."""

    def test_all_checks_run(self, minimal_schematic: Path):
        from kicad_tools.cli.sch_preflight import run_preflight

        result = run_preflight(str(minimal_schematic))
        # Should run all 8 check categories
        assert "footprints" in result.checks_run
        assert "values" in result.checks_run
        assert "hierarchy" in result.checks_run
        assert "footprint_resolution" in result.checks_run
        assert "pin_pad_count" in result.checks_run
        assert "single_pin_nets" in result.checks_run
        assert "generic_values" in result.checks_run
        assert "power_flags" in result.checks_run

    def test_clean_schematic_passes(self, minimal_schematic: Path):
        from kicad_tools.cli.sch_preflight import run_preflight

        result = run_preflight(str(minimal_schematic))
        # The minimal schematic has valid footprint, real value, no power issues
        assert result.passed is True

    def test_empty_schematic_passes(self, _write_sch):
        from kicad_tools.cli.sch_preflight import run_preflight

        sch = _write_sch(_SCH_EMPTY)
        result = run_preflight(str(sch))
        assert result.passed is True
        assert result.error_count == 0


# ---------------------------------------------------------------------------
# CLI entry point: main()
# ---------------------------------------------------------------------------


class TestPreflightCLI:
    """Tests for the CLI entry point (main)."""

    def test_file_not_found_exits_1(self, capsys):
        from kicad_tools.cli.sch_preflight import main

        with pytest.raises(SystemExit) as exc_info:
            main(["nonexistent.kicad_sch"])
        assert exc_info.value.code == 1

    def test_text_output(self, minimal_schematic: Path, capsys):
        from kicad_tools.cli.sch_preflight import main

        # Should not raise (clean schematic)
        main([str(minimal_schematic)])
        captured = capsys.readouterr()
        assert "Validation:" in captured.out
        assert "Checks run:" in captured.out

    def test_json_output(self, minimal_schematic: Path, capsys):
        from kicad_tools.cli.sch_preflight import main

        main([str(minimal_schematic), "--format", "json"])
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert "schematic" in data
        assert "passed" in data
        assert "checks_run" in data
        assert "issues" in data
        assert isinstance(data["checks_run"], list)
        assert len(data["checks_run"]) == 8

    def test_strict_flag_exits_on_warnings(self, _write_sch, capsys):
        from kicad_tools.cli.sch_preflight import main

        # This schematic triggers a generic-value warning
        sch = _write_sch(_SCH_GENERIC_VALUE)
        with pytest.raises(SystemExit) as exc_info:
            main([str(sch), "--strict"])
        assert exc_info.value.code == 1

    def test_no_strict_passes_with_warnings(self, _write_sch, capsys):
        from kicad_tools.cli.sch_preflight import main

        sch = _write_sch(_SCH_GENERIC_VALUE)
        # Without --strict, warnings do not cause exit(1)
        main([str(sch)])
        captured = capsys.readouterr()
        assert (
            "warning" in captured.out.lower() or "Warning" in captured.out or len(captured.out) > 0
        )

    def test_quiet_json_omits_warnings(self, _write_sch, capsys):
        from kicad_tools.cli.sch_preflight import main

        sch = _write_sch(_SCH_GENERIC_VALUE)
        main([str(sch), "--format", "json", "--quiet"])
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        # With --quiet, only errors should appear in the issues list
        for issue in data["issues"]:
            assert issue["severity"] == "error"

    def test_error_causes_exit_1(self, _write_sch, capsys):
        from kicad_tools.cli.sch_preflight import main

        # Empty library prefix produces an error
        sch = _write_sch(_SCH_EMPTY_LIB_PREFIX)
        with pytest.raises(SystemExit) as exc_info:
            main([str(sch)])
        assert exc_info.value.code == 1
