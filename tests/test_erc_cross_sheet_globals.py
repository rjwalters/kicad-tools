"""Tests for cross-sheet global label false-positive filtering."""

from pathlib import Path

from kicad_tools.erc.cross_sheet import (
    _extract_label_name,
    build_global_label_inventory,
    build_sheet_label_presence,
    filter_cross_sheet_global_labels,
)

# ---------------------------------------------------------------------------
# Schematic templates with global labels
# ---------------------------------------------------------------------------

_ROOT_WITH_GLOBALS_TEMPLATE = """\
(kicad_sch
  (version 20231120)
  (generator "test")
  (generator_version "8.0")
  (uuid "root-uuid-001")
  (paper "A4")
  (lib_symbols)
  {global_labels}
  {sheets}
)
"""

_SUBSHEET_WITH_GLOBALS_TEMPLATE = """\
(kicad_sch
  (version 20231120)
  (generator "test")
  (generator_version "8.0")
  (uuid "{uuid}")
  (paper "A4")
  (lib_symbols)
  {global_labels}
)
"""

_GLOBAL_LABEL_TEMPLATE = """\
  (global_label "{text}"
    (shape input)
    (at 100 100 0)
    (fields_autoplaced yes)
    (effects (font (size 1.27 1.27)) (justify left))
    (uuid "{uuid}")
  )
"""

_SHEET_TEMPLATE = """\
  (sheet
    (at 130 40) (size 40 30)
    (uuid "{uuid}")
    (property "Sheetname" "{name}"
      (at 130 39 0)
      (effects (font (size 1.27 1.27)))
    )
    (property "Sheetfile" "{filename}"
      (at 130 71 0)
      (effects (font (size 1.27 1.27)))
    )
  )
"""


def _make_global_label(text: str, uuid: str = "gl-001") -> str:
    return _GLOBAL_LABEL_TEMPLATE.format(text=text, uuid=uuid)


_LOCAL_LABEL_TEMPLATE = """\
  (label "{text}"
    (at 100 100 0)
    (fields_autoplaced yes)
    (effects (font (size 1.27 1.27)) (justify left))
    (uuid "{uuid}")
  )
"""


def _make_local_label(text: str, uuid: str = "ll-001") -> str:
    return _LOCAL_LABEL_TEMPLATE.format(text=text, uuid=uuid)


def _make_sheet(name: str, filename: str, uuid: str = "sheet-001") -> str:
    return _SHEET_TEMPLATE.format(name=name, filename=filename, uuid=uuid)


# ---------------------------------------------------------------------------
# Tests: _extract_label_name
# ---------------------------------------------------------------------------


class TestExtractLabelName:
    """Tests for parsing label names from violation descriptions."""

    def test_single_global_label_description(self):
        desc = "Label 'AUDIO_L' appears only once in the design"
        assert _extract_label_name(desc) == "AUDIO_L"

    def test_global_label_description(self):
        desc = "Global label 'SPI_MOSI' is not connected anywhere else in the schematic"
        assert _extract_label_name(desc) == "SPI_MOSI"

    def test_double_quoted_label(self):
        desc = 'Label "I2C_SDA" is isolated'
        assert _extract_label_name(desc) == "I2C_SDA"

    def test_no_label_name(self):
        desc = "Pin connected to only other pins or labels on the sheet"
        assert _extract_label_name(desc) is None

    def test_label_with_special_chars(self):
        desc = "Label 'NET_3V3' appears only once in the design"
        assert _extract_label_name(desc) == "NET_3V3"

    def test_kicad10_label_in_items_single_quoted(self):
        """KiCad 10+ puts the label name in items, not the description."""
        desc = "Global label only appears once in the schematic"
        items = [{"description": "Global Label 'AUDIO_L'", "pos": {"x": 0, "y": 0}}]
        assert _extract_label_name(desc, items) == "AUDIO_L"

    def test_kicad10_label_in_items_isolated_pin(self):
        """KiCad 10+ isolated_pin_label also uses items for the label name."""
        desc = "Label connected to only one pin"
        items = [{"description": "Global Label 'SYNC_R'", "pos": {"x": 0, "y": 0}}]
        assert _extract_label_name(desc, items) == "SYNC_R"

    def test_kicad10_no_items_returns_none(self):
        """Generic description with no items should return None."""
        desc = "Label connected to only one pin"
        assert _extract_label_name(desc) is None
        assert _extract_label_name(desc, []) is None

    def test_kicad10_items_without_label_returns_none(self):
        """Items that don't contain a label name should return None."""
        desc = "Label connected to only one pin"
        items = [{"description": "Pin 1 of R1", "pos": {"x": 0, "y": 0}}]
        assert _extract_label_name(desc, items) is None


# ---------------------------------------------------------------------------
# Tests: build_global_label_inventory
# ---------------------------------------------------------------------------


class TestBuildGlobalLabelInventory:
    """Tests for building cross-sheet global label inventory."""

    def test_label_on_multiple_sheets(self, tmp_path: Path):
        """AUDIO_L on root and sub-sheet should map to both paths."""
        sub_file = "sub.kicad_sch"

        root_labels = _make_global_label("AUDIO_L", uuid="gl-root")
        root_sheets = _make_sheet("Sub", sub_file, uuid="sheet-sub")
        root_content = _ROOT_WITH_GLOBALS_TEMPLATE.format(
            global_labels=root_labels, sheets=root_sheets
        )

        sub_labels = _make_global_label("AUDIO_L", uuid="gl-sub")
        sub_content = _SUBSHEET_WITH_GLOBALS_TEMPLATE.format(
            uuid="sub-uuid-001", global_labels=sub_labels
        )

        (tmp_path / "root.kicad_sch").write_text(root_content)
        (tmp_path / sub_file).write_text(sub_content)

        inventory = build_global_label_inventory(str(tmp_path / "root.kicad_sch"))

        assert "AUDIO_L" in inventory
        assert len(inventory["AUDIO_L"]) == 2

    def test_label_on_single_sheet(self, tmp_path: Path):
        """Label only on root should map to one path."""
        root_labels = _make_global_label("LONELY_NET", uuid="gl-lonely")
        root_content = _ROOT_WITH_GLOBALS_TEMPLATE.format(global_labels=root_labels, sheets="")

        (tmp_path / "root.kicad_sch").write_text(root_content)

        inventory = build_global_label_inventory(str(tmp_path / "root.kicad_sch"))

        assert "LONELY_NET" in inventory
        assert len(inventory["LONELY_NET"]) == 1

    def test_multiple_labels_across_three_sheets(self, tmp_path: Path):
        """Multiple labels across three sheets should all be tracked."""
        sub_a = "sub_a.kicad_sch"
        sub_b = "sub_b.kicad_sch"

        root_labels = _make_global_label("AUDIO_L", uuid="gl-root-audio")
        root_sheets = _make_sheet("SubA", sub_a, uuid="sheet-a") + _make_sheet(
            "SubB", sub_b, uuid="sheet-b"
        )
        root_content = _ROOT_WITH_GLOBALS_TEMPLATE.format(
            global_labels=root_labels, sheets=root_sheets
        )

        sub_a_labels = _make_global_label("AUDIO_L", uuid="gl-a-audio") + _make_global_label(
            "SPI_CLK", uuid="gl-a-spi"
        )
        sub_a_content = _SUBSHEET_WITH_GLOBALS_TEMPLATE.format(
            uuid="sub-a-uuid", global_labels=sub_a_labels
        )

        sub_b_labels = _make_global_label("AUDIO_L", uuid="gl-b-audio")
        sub_b_content = _SUBSHEET_WITH_GLOBALS_TEMPLATE.format(
            uuid="sub-b-uuid", global_labels=sub_b_labels
        )

        (tmp_path / "root.kicad_sch").write_text(root_content)
        (tmp_path / sub_a).write_text(sub_a_content)
        (tmp_path / sub_b).write_text(sub_b_content)

        inventory = build_global_label_inventory(str(tmp_path / "root.kicad_sch"))

        assert len(inventory["AUDIO_L"]) == 3
        # SPI_CLK only appears on sub_a
        assert len(inventory["SPI_CLK"]) == 1

    def test_no_global_labels(self, tmp_path: Path):
        """Schematic with no global labels returns empty inventory."""
        root_content = _ROOT_WITH_GLOBALS_TEMPLATE.format(global_labels="", sheets="")
        (tmp_path / "root.kicad_sch").write_text(root_content)

        inventory = build_global_label_inventory(str(tmp_path / "root.kicad_sch"))
        assert inventory == {}


# ---------------------------------------------------------------------------
# Tests: filter_cross_sheet_global_labels
# ---------------------------------------------------------------------------


class TestFilterCrossSheetGlobalLabels:
    """Tests for filtering false-positive global label violations."""

    def _make_violation(self, vtype: str, label_name: str) -> dict:
        """Helper to create a mock violation dict."""
        return {
            "type": vtype,
            "severity": "warning",
            "description": f"Label '{label_name}' appears only once in the design",
        }

    def test_suppresses_multi_sheet_single_global_label(self, tmp_path: Path):
        """single_global_label for a label on 2 sheets should be removed."""
        sub_file = "sub.kicad_sch"

        root_labels = _make_global_label("AUDIO_L", uuid="gl-root")
        root_sheets = _make_sheet("Sub", sub_file, uuid="sheet-sub")
        root_content = _ROOT_WITH_GLOBALS_TEMPLATE.format(
            global_labels=root_labels, sheets=root_sheets
        )

        sub_labels = _make_global_label("AUDIO_L", uuid="gl-sub")
        sub_content = _SUBSHEET_WITH_GLOBALS_TEMPLATE.format(
            uuid="sub-uuid", global_labels=sub_labels
        )

        (tmp_path / "root.kicad_sch").write_text(root_content)
        (tmp_path / sub_file).write_text(sub_content)

        violations = [self._make_violation("single_global_label", "AUDIO_L")]
        result = filter_cross_sheet_global_labels(violations, str(tmp_path / "root.kicad_sch"))

        assert len(result) == 0

    def test_preserves_genuine_single_global_label(self, tmp_path: Path):
        """single_global_label for a label on 1 sheet should be kept."""
        root_labels = _make_global_label("LONELY_NET", uuid="gl-lonely")
        root_content = _ROOT_WITH_GLOBALS_TEMPLATE.format(global_labels=root_labels, sheets="")
        (tmp_path / "root.kicad_sch").write_text(root_content)

        violations = [self._make_violation("single_global_label", "LONELY_NET")]
        result = filter_cross_sheet_global_labels(violations, str(tmp_path / "root.kicad_sch"))

        assert len(result) == 1

    def test_suppresses_multi_sheet_isolated_pin_label(self, tmp_path: Path):
        """isolated_pin_label for a global label on 2 sheets should be removed."""
        sub_file = "sub.kicad_sch"

        root_labels = _make_global_label("SPI_MOSI", uuid="gl-root")
        root_sheets = _make_sheet("Sub", sub_file, uuid="sheet-sub")
        root_content = _ROOT_WITH_GLOBALS_TEMPLATE.format(
            global_labels=root_labels, sheets=root_sheets
        )

        sub_labels = _make_global_label("SPI_MOSI", uuid="gl-sub")
        sub_content = _SUBSHEET_WITH_GLOBALS_TEMPLATE.format(
            uuid="sub-uuid", global_labels=sub_labels
        )

        (tmp_path / "root.kicad_sch").write_text(root_content)
        (tmp_path / sub_file).write_text(sub_content)

        violations = [self._make_violation("isolated_pin_label", "SPI_MOSI")]
        result = filter_cross_sheet_global_labels(violations, str(tmp_path / "root.kicad_sch"))

        assert len(result) == 0

    def test_other_violation_types_pass_through(self, tmp_path: Path):
        """Violations of other types should not be affected."""
        root_content = _ROOT_WITH_GLOBALS_TEMPLATE.format(global_labels="", sheets="")
        (tmp_path / "root.kicad_sch").write_text(root_content)

        violations = [
            {
                "type": "pin_not_connected",
                "severity": "error",
                "description": "Pin 1 of R1 is not connected",
            },
            {
                "type": "duplicate_reference",
                "severity": "error",
                "description": "Duplicate reference R12",
            },
        ]
        result = filter_cross_sheet_global_labels(violations, str(tmp_path / "root.kicad_sch"))

        assert len(result) == 2

    def test_no_target_violations_skips_hierarchy(self, tmp_path: Path):
        """When no single_global_label or isolated_pin_label violations exist,
        the hierarchy traversal should be skipped entirely."""
        # Don't even create schematic files -- if hierarchy is traversed,
        # the function will fail because the files don't exist.
        violations = [
            {
                "type": "pin_not_connected",
                "severity": "error",
                "description": "Pin 1 of R1 is not connected",
            },
        ]
        result = filter_cross_sheet_global_labels(
            violations, str(tmp_path / "nonexistent.kicad_sch")
        )

        assert len(result) == 1

    def test_mixed_violations_selective_filtering(self, tmp_path: Path):
        """Mix of genuine and false-positive violations: only false positives
        should be removed."""
        sub_file = "sub.kicad_sch"

        root_labels = _make_global_label("MULTI_SHEET", uuid="gl-root-multi") + _make_global_label(
            "SINGLE_ONLY", uuid="gl-root-single"
        )
        root_sheets = _make_sheet("Sub", sub_file, uuid="sheet-sub")
        root_content = _ROOT_WITH_GLOBALS_TEMPLATE.format(
            global_labels=root_labels, sheets=root_sheets
        )

        sub_labels = _make_global_label("MULTI_SHEET", uuid="gl-sub-multi")
        sub_content = _SUBSHEET_WITH_GLOBALS_TEMPLATE.format(
            uuid="sub-uuid", global_labels=sub_labels
        )

        (tmp_path / "root.kicad_sch").write_text(root_content)
        (tmp_path / sub_file).write_text(sub_content)

        violations = [
            self._make_violation("single_global_label", "MULTI_SHEET"),  # false positive
            self._make_violation("single_global_label", "SINGLE_ONLY"),  # genuine
            {
                "type": "pin_not_connected",
                "severity": "error",
                "description": "Pin 1 of R1 is not connected",
            },
        ]
        result = filter_cross_sheet_global_labels(violations, str(tmp_path / "root.kicad_sch"))

        assert len(result) == 2
        types = [v["type"] for v in result]
        assert "pin_not_connected" in types
        assert "single_global_label" in types
        # The remaining single_global_label should be for SINGLE_ONLY
        sgl = [v for v in result if v["type"] == "single_global_label"][0]
        assert "SINGLE_ONLY" in sgl["description"]

    def test_unparseable_description_kept(self, tmp_path: Path):
        """If the label name cannot be parsed from the description, keep the
        violation to be safe."""
        sub_file = "sub.kicad_sch"

        root_labels = _make_global_label("AUDIO_L", uuid="gl-root")
        root_sheets = _make_sheet("Sub", sub_file, uuid="sheet-sub")
        root_content = _ROOT_WITH_GLOBALS_TEMPLATE.format(
            global_labels=root_labels, sheets=root_sheets
        )

        sub_labels = _make_global_label("AUDIO_L", uuid="gl-sub")
        sub_content = _SUBSHEET_WITH_GLOBALS_TEMPLATE.format(
            uuid="sub-uuid", global_labels=sub_labels
        )

        (tmp_path / "root.kicad_sch").write_text(root_content)
        (tmp_path / sub_file).write_text(sub_content)

        violations = [
            {
                "type": "single_global_label",
                "severity": "warning",
                "description": "Some unusual description with no label name",
            },
        ]
        result = filter_cross_sheet_global_labels(violations, str(tmp_path / "root.kicad_sch"))

        # Cannot parse label name, so violation is kept
        assert len(result) == 1

    def test_label_on_child_sheets_only(self, tmp_path: Path):
        """Global label appearing only on child sheets (not root) should
        still be recognized as multi-sheet."""
        sub_a = "sub_a.kicad_sch"
        sub_b = "sub_b.kicad_sch"

        root_sheets = _make_sheet("SubA", sub_a, uuid="sheet-a") + _make_sheet(
            "SubB", sub_b, uuid="sheet-b"
        )
        root_content = _ROOT_WITH_GLOBALS_TEMPLATE.format(global_labels="", sheets=root_sheets)

        sub_a_labels = _make_global_label("CHILD_NET", uuid="gl-a")
        sub_a_content = _SUBSHEET_WITH_GLOBALS_TEMPLATE.format(
            uuid="sub-a-uuid", global_labels=sub_a_labels
        )

        sub_b_labels = _make_global_label("CHILD_NET", uuid="gl-b")
        sub_b_content = _SUBSHEET_WITH_GLOBALS_TEMPLATE.format(
            uuid="sub-b-uuid", global_labels=sub_b_labels
        )

        (tmp_path / "root.kicad_sch").write_text(root_content)
        (tmp_path / sub_a).write_text(sub_a_content)
        (tmp_path / sub_b).write_text(sub_b_content)

        violations = [self._make_violation("single_global_label", "CHILD_NET")]
        result = filter_cross_sheet_global_labels(violations, str(tmp_path / "root.kicad_sch"))

        assert len(result) == 0

    def test_empty_violations_list(self, tmp_path: Path):
        """Empty violations list should return empty list."""
        result = filter_cross_sheet_global_labels([], str(tmp_path / "nonexistent.kicad_sch"))
        assert result == []

    def _make_kicad10_violation(self, vtype: str, label_name: str) -> dict:
        """Helper to create a KiCad 10 style violation dict.

        KiCad 10 uses a generic top-level description and puts the
        specific label name inside the ``items`` array.
        """
        if vtype == "single_global_label":
            desc = "Global label only appears once in the schematic"
        else:
            desc = "Label connected to only one pin"
        return {
            "type": vtype,
            "severity": "warning",
            "description": desc,
            "items": [
                {
                    "description": f"Global Label '{label_name}'",
                    "pos": {"x": 0, "y": 0},
                    "uuid": "test-uuid",
                }
            ],
        }

    def test_kicad10_single_global_label_filtered(self, tmp_path: Path):
        """KiCad 10 format: single_global_label with label in items
        should be filtered when the label appears on multiple sheets."""
        sub_file = "sub.kicad_sch"

        root_labels = _make_global_label("AUDIO_L", uuid="gl-root")
        root_sheets = _make_sheet("Sub", sub_file, uuid="sheet-sub")
        root_content = _ROOT_WITH_GLOBALS_TEMPLATE.format(
            global_labels=root_labels, sheets=root_sheets
        )

        sub_labels = _make_global_label("AUDIO_L", uuid="gl-sub")
        sub_content = _SUBSHEET_WITH_GLOBALS_TEMPLATE.format(
            uuid="sub-uuid", global_labels=sub_labels
        )

        (tmp_path / "root.kicad_sch").write_text(root_content)
        (tmp_path / sub_file).write_text(sub_content)

        violations = [
            self._make_kicad10_violation("single_global_label", "AUDIO_L"),
        ]
        result = filter_cross_sheet_global_labels(violations, str(tmp_path / "root.kicad_sch"))

        assert len(result) == 0

    def test_kicad10_isolated_pin_label_filtered(self, tmp_path: Path):
        """KiCad 10 format: isolated_pin_label with label in items
        should be filtered when the label appears on multiple sheets."""
        sub_file = "sub.kicad_sch"

        root_labels = _make_global_label("SYNC_R", uuid="gl-root")
        root_sheets = _make_sheet("Sub", sub_file, uuid="sheet-sub")
        root_content = _ROOT_WITH_GLOBALS_TEMPLATE.format(
            global_labels=root_labels, sheets=root_sheets
        )

        sub_labels = _make_global_label("SYNC_R", uuid="gl-sub")
        sub_content = _SUBSHEET_WITH_GLOBALS_TEMPLATE.format(
            uuid="sub-uuid", global_labels=sub_labels
        )

        (tmp_path / "root.kicad_sch").write_text(root_content)
        (tmp_path / sub_file).write_text(sub_content)

        violations = [
            self._make_kicad10_violation("isolated_pin_label", "SYNC_R"),
        ]
        result = filter_cross_sheet_global_labels(violations, str(tmp_path / "root.kicad_sch"))

        assert len(result) == 0

    def test_kicad10_mixed_format_chorus_scenario(self, tmp_path: Path):
        """Reproduce the chorus-test-revA scenario: 8 violations (4
        isolated_pin_label on root + 4 single_global_label on /Sync/)
        should all be suppressed when labels exist on multiple sheets."""
        sync_file = "sync.kicad_sch"
        dac_file = "dac.kicad_sch"

        labels = ["AUDIO_L", "AUDIO_R", "SYNC_L", "SYNC_R"]

        root_labels = "\n".join(_make_global_label(name, uuid=f"gl-root-{name}") for name in labels)
        root_sheets = _make_sheet("Sync", sync_file, uuid="sheet-sync") + _make_sheet(
            "DAC", dac_file, uuid="sheet-dac"
        )
        root_content = _ROOT_WITH_GLOBALS_TEMPLATE.format(
            global_labels=root_labels, sheets=root_sheets
        )

        sync_labels = "\n".join(_make_global_label(name, uuid=f"gl-sync-{name}") for name in labels)
        sync_content = _SUBSHEET_WITH_GLOBALS_TEMPLATE.format(
            uuid="sync-uuid", global_labels=sync_labels
        )

        dac_labels = "\n".join(_make_global_label(name, uuid=f"gl-dac-{name}") for name in labels)
        dac_content = _SUBSHEET_WITH_GLOBALS_TEMPLATE.format(
            uuid="dac-uuid", global_labels=dac_labels
        )

        (tmp_path / "root.kicad_sch").write_text(root_content)
        (tmp_path / sync_file).write_text(sync_content)
        (tmp_path / dac_file).write_text(dac_content)

        # Build violations matching KiCad 10 output format
        violations = []
        for name in labels:
            violations.append(self._make_kicad10_violation("isolated_pin_label", name))
            violations.append(self._make_kicad10_violation("single_global_label", name))

        result = filter_cross_sheet_global_labels(violations, str(tmp_path / "root.kicad_sch"))

        assert len(result) == 0

    def test_kicad10_genuine_single_label_preserved(self, tmp_path: Path):
        """KiCad 10 format: a label that truly appears on only one
        sheet should still be reported."""
        root_labels = _make_global_label("LONELY_NET", uuid="gl-lonely")
        root_content = _ROOT_WITH_GLOBALS_TEMPLATE.format(global_labels=root_labels, sheets="")
        (tmp_path / "root.kicad_sch").write_text(root_content)

        violations = [
            self._make_kicad10_violation("single_global_label", "LONELY_NET"),
        ]
        result = filter_cross_sheet_global_labels(violations, str(tmp_path / "root.kicad_sch"))

        assert len(result) == 1

    def test_suppresses_unparseable_violation_on_label_free_sheet(self, tmp_path: Path):
        """isolated_pin_label on a sheet with no labels should be suppressed."""
        sub_file = "sub.kicad_sch"

        # Root has only sheet references, no labels at all
        root_sheets = _make_sheet("Sub", sub_file, uuid="sheet-sub")
        root_content = _ROOT_WITH_GLOBALS_TEMPLATE.format(global_labels="", sheets=root_sheets)

        sub_labels = _make_global_label("SPI_MOSI", uuid="gl-sub")
        sub_content = _SUBSHEET_WITH_GLOBALS_TEMPLATE.format(
            uuid="sub-uuid", global_labels=sub_labels
        )

        (tmp_path / "root.kicad_sch").write_text(root_content)
        (tmp_path / sub_file).write_text(sub_content)

        violations = [
            {
                "type": "isolated_pin_label",
                "severity": "warning",
                "description": "Pin connected to only other pins or labels on the sheet",
                "_sheet_path": "/",
            },
        ]
        result = filter_cross_sheet_global_labels(violations, str(tmp_path / "root.kicad_sch"))

        # Root sheet has no labels -- violation is a phantom detection
        assert len(result) == 0

    def test_keeps_unparseable_violation_on_sheet_with_labels(self, tmp_path: Path):
        """isolated_pin_label on a sheet that has labels should be kept."""
        root_labels = _make_local_label("LOCAL_NET", uuid="ll-root")
        root_content = _ROOT_WITH_GLOBALS_TEMPLATE.format(global_labels=root_labels, sheets="")

        (tmp_path / "root.kicad_sch").write_text(root_content)

        violations = [
            {
                "type": "isolated_pin_label",
                "severity": "warning",
                "description": "Pin connected to only other pins or labels on the sheet",
                "_sheet_path": "/",
            },
        ]
        result = filter_cross_sheet_global_labels(violations, str(tmp_path / "root.kicad_sch"))

        # Root sheet has a local label -- violation is kept
        assert len(result) == 1

    def test_keeps_unparseable_violation_without_sheet_path(self, tmp_path: Path):
        """isolated_pin_label without _sheet_path should be kept (safe default)."""
        root_content = _ROOT_WITH_GLOBALS_TEMPLATE.format(global_labels="", sheets="")
        (tmp_path / "root.kicad_sch").write_text(root_content)

        violations = [
            {
                "type": "isolated_pin_label",
                "severity": "warning",
                "description": "Pin connected to only other pins or labels on the sheet",
                # No _sheet_path key
            },
        ]
        result = filter_cross_sheet_global_labels(violations, str(tmp_path / "root.kicad_sch"))

        # No sheet path info -- keep to be safe
        assert len(result) == 1

    def test_suppresses_multiple_phantom_violations_on_root(self, tmp_path: Path):
        """Multiple isolated_pin_label on label-free root should all be suppressed."""
        sub_a = "sub_a.kicad_sch"
        sub_b = "sub_b.kicad_sch"

        root_sheets = _make_sheet("SubA", sub_a, uuid="sheet-a") + _make_sheet(
            "SubB", sub_b, uuid="sheet-b"
        )
        root_content = _ROOT_WITH_GLOBALS_TEMPLATE.format(global_labels="", sheets=root_sheets)

        sub_a_labels = _make_global_label("NET_A", uuid="gl-a")
        sub_a_content = _SUBSHEET_WITH_GLOBALS_TEMPLATE.format(
            uuid="sub-a-uuid", global_labels=sub_a_labels
        )

        sub_b_labels = _make_global_label("NET_B", uuid="gl-b")
        sub_b_content = _SUBSHEET_WITH_GLOBALS_TEMPLATE.format(
            uuid="sub-b-uuid", global_labels=sub_b_labels
        )

        (tmp_path / "root.kicad_sch").write_text(root_content)
        (tmp_path / sub_a).write_text(sub_a_content)
        (tmp_path / sub_b).write_text(sub_b_content)

        # 4 phantom violations on root sheet (like the issue describes)
        violations = [
            {
                "type": "isolated_pin_label",
                "severity": "warning",
                "description": "Label connected to only one pin",
                "_sheet_path": "/",
            }
            for _ in range(4)
        ]
        result = filter_cross_sheet_global_labels(violations, str(tmp_path / "root.kicad_sch"))

        assert len(result) == 0


# ---------------------------------------------------------------------------
# Tests: build_sheet_label_presence
# ---------------------------------------------------------------------------


class TestBuildSheetLabelPresence:
    """Tests for building sheet label presence set."""

    def test_root_with_global_labels(self, tmp_path: Path):
        """Root sheet with global labels should be in the presence set."""
        root_labels = _make_global_label("AUDIO_L", uuid="gl-root")
        root_content = _ROOT_WITH_GLOBALS_TEMPLATE.format(global_labels=root_labels, sheets="")
        (tmp_path / "root.kicad_sch").write_text(root_content)

        presence = build_sheet_label_presence(str(tmp_path / "root.kicad_sch"))

        assert "/" in presence

    def test_root_without_labels(self, tmp_path: Path):
        """Root sheet with no labels should not be in the presence set."""
        root_content = _ROOT_WITH_GLOBALS_TEMPLATE.format(global_labels="", sheets="")
        (tmp_path / "root.kicad_sch").write_text(root_content)

        presence = build_sheet_label_presence(str(tmp_path / "root.kicad_sch"))

        assert "/" not in presence

    def test_root_with_local_labels(self, tmp_path: Path):
        """Root sheet with local labels should be in the presence set."""
        root_labels = _make_local_label("LOCAL_NET", uuid="ll-root")
        root_content = _ROOT_WITH_GLOBALS_TEMPLATE.format(global_labels=root_labels, sheets="")
        (tmp_path / "root.kicad_sch").write_text(root_content)

        presence = build_sheet_label_presence(str(tmp_path / "root.kicad_sch"))

        assert "/" in presence

    def test_mixed_sheets(self, tmp_path: Path):
        """Root without labels, sub-sheet with labels: only sub-sheet in set."""
        sub_file = "sub.kicad_sch"

        root_sheets = _make_sheet("Sub", sub_file, uuid="sheet-sub")
        root_content = _ROOT_WITH_GLOBALS_TEMPLATE.format(global_labels="", sheets=root_sheets)

        sub_labels = _make_global_label("NET_A", uuid="gl-sub")
        sub_content = _SUBSHEET_WITH_GLOBALS_TEMPLATE.format(
            uuid="sub-uuid", global_labels=sub_labels
        )

        (tmp_path / "root.kicad_sch").write_text(root_content)
        (tmp_path / sub_file).write_text(sub_content)

        presence = build_sheet_label_presence(str(tmp_path / "root.kicad_sch"))

        assert "/" not in presence
        assert "/Sub" in presence
