"""Tests for wire_dangling / endpoint_off_grid sheet re-attribution.

Verifies that ``reattribute_wire_dangling_violations`` correctly maps
root-sheet-attributed violations to child sheets based on wire endpoint
coordinates, and that violation descriptions are enriched with position
information.
"""

from __future__ import annotations

import json
import os
import tempfile as _tempfile
from unittest.mock import MagicMock, patch

from kicad_tools.cli.sch_validate import ValidationIssue, run_erc
from kicad_tools.erc.cross_sheet import (
    _enrich_description_with_pos,
    reattribute_wire_dangling_violations,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_erc_json(sheets: list[dict]) -> str:
    """Build a minimal KiCad ERC JSON report with multiple sheets."""
    return json.dumps(
        {
            "source": "test.kicad_sch",
            "kicad_version": "8.0.0",
            "coordinate_units": "mm",
            "sheets": sheets,
        }
    )


def _make_sheet(path: str, violations: list[dict]) -> dict:
    return {
        "path": path,
        "uuid_path": "00000000-0000-0000-0000-000000000000",
        "violations": violations,
    }


def _wire_dangling(x: float, y: float, desc: str = "Wire not connected at both ends") -> dict:
    return {
        "type": "wire_dangling",
        "severity": "warning",
        "description": desc,
        "pos": {"x": x, "y": y},
        "items": [],
    }


def _endpoint_off_grid(x: float, y: float) -> dict:
    return {
        "type": "endpoint_off_grid",
        "severity": "warning",
        "description": "Wire endpoint off grid",
        "pos": {"x": x, "y": y},
        "items": [],
    }


# ---------------------------------------------------------------------------
# Unit tests: reattribute_wire_dangling_violations
# ---------------------------------------------------------------------------


class TestReattributeWireDangling:
    """Test the re-attribution logic directly."""

    def test_root_violation_reattributed_to_child(self):
        """A wire_dangling at (100.0, 50.0) on '/' should map to '/DAC'."""
        violations = [_wire_dangling(100.0, 50.0)]
        violations[0]["_sheet_path"] = "/"

        # Mock the hierarchy: one child sheet /DAC with a wire at (100.0, 50.0)
        fake_wire = MagicMock()
        fake_wire.start = (100.0, 50.0)
        fake_wire.end = (100.0, 80.0)

        fake_sch = MagicMock()
        fake_sch.wires = [fake_wire]

        fake_child = MagicMock()
        fake_child.is_root = False
        fake_child.path = "/tmp/dac.kicad_sch"
        fake_child.get_path_string.return_value = "/DAC"

        fake_root = MagicMock()
        fake_root.is_root = True
        fake_root.all_nodes.return_value = [fake_root, fake_child]

        with (
            patch(
                "kicad_tools.schema.hierarchy.build_hierarchy",
                return_value=fake_root,
            ),
            patch(
                "kicad_tools.schema.Schematic.load",
                return_value=fake_sch,
            ),
        ):
            result = reattribute_wire_dangling_violations(violations, "test.kicad_sch")

        assert result[0]["_sheet_path"] == "/DAC"

    def test_root_violation_stays_when_no_child_match(self):
        """A wire_dangling at (999, 999) with no matching child wire stays on '/'."""
        violations = [_wire_dangling(999.0, 999.0)]
        violations[0]["_sheet_path"] = "/"

        fake_wire = MagicMock()
        fake_wire.start = (100.0, 50.0)
        fake_wire.end = (100.0, 80.0)

        fake_sch = MagicMock()
        fake_sch.wires = [fake_wire]

        fake_child = MagicMock()
        fake_child.is_root = False
        fake_child.path = "/tmp/dac.kicad_sch"
        fake_child.get_path_string.return_value = "/DAC"

        fake_root = MagicMock()
        fake_root.is_root = True
        fake_root.all_nodes.return_value = [fake_root, fake_child]

        with (
            patch(
                "kicad_tools.schema.hierarchy.build_hierarchy",
                return_value=fake_root,
            ),
            patch(
                "kicad_tools.schema.Schematic.load",
                return_value=fake_sch,
            ),
        ):
            result = reattribute_wire_dangling_violations(violations, "test.kicad_sch")

        assert result[0]["_sheet_path"] == "/"

    def test_child_violation_not_altered(self):
        """A wire_dangling already attributed to a child sheet should not change."""
        violations = [_wire_dangling(100.0, 50.0)]
        violations[0]["_sheet_path"] = "/MCU"

        # No hierarchy traversal should occur since nothing is on "/"
        result = reattribute_wire_dangling_violations(violations, "test.kicad_sch")
        assert result[0]["_sheet_path"] == "/MCU"

    def test_endpoint_off_grid_also_reattributed(self):
        """endpoint_off_grid violations should also be re-attributed."""
        violations = [_endpoint_off_grid(50.0, 25.0)]
        violations[0]["_sheet_path"] = "/"

        fake_wire = MagicMock()
        fake_wire.start = (50.0, 25.0)
        fake_wire.end = (50.0, 55.0)

        fake_sch = MagicMock()
        fake_sch.wires = [fake_wire]

        fake_child = MagicMock()
        fake_child.is_root = False
        fake_child.path = "/tmp/power.kicad_sch"
        fake_child.get_path_string.return_value = "/Power"

        fake_root = MagicMock()
        fake_root.is_root = True
        fake_root.all_nodes.return_value = [fake_root, fake_child]

        with (
            patch(
                "kicad_tools.schema.hierarchy.build_hierarchy",
                return_value=fake_root,
            ),
            patch(
                "kicad_tools.schema.Schematic.load",
                return_value=fake_sch,
            ),
        ):
            result = reattribute_wire_dangling_violations(violations, "test.kicad_sch")

        assert result[0]["_sheet_path"] == "/Power"

    def test_other_violation_types_pass_through(self):
        """Non-wire-dangling violations should pass through unchanged."""
        violations = [
            {
                "type": "pin_not_connected",
                "severity": "error",
                "description": "Pin not connected",
                "pos": {"x": 100, "y": 50},
                "_sheet_path": "/",
                "items": [],
            }
        ]
        result = reattribute_wire_dangling_violations(violations, "test.kicad_sch")
        assert result[0]["_sheet_path"] == "/"
        assert result[0]["description"] == "Pin not connected"

    def test_no_target_violations_skips_hierarchy(self):
        """When no wire_dangling/endpoint_off_grid exist, hierarchy is not built."""
        violations = [
            {
                "type": "pin_not_connected",
                "severity": "error",
                "description": "Pin not connected",
                "_sheet_path": "/",
                "items": [],
            }
        ]
        with patch(
            "kicad_tools.schema.hierarchy.build_hierarchy"
        ) as mock_build:
            reattribute_wire_dangling_violations(violations, "test.kicad_sch")
            mock_build.assert_not_called()


# ---------------------------------------------------------------------------
# Unit tests: description enrichment
# ---------------------------------------------------------------------------


class TestDescriptionEnrichment:
    """Verify that wire_dangling descriptions include position coordinates."""

    def test_coordinates_appended(self):
        v = _wire_dangling(100.0, 50.0)
        _enrich_description_with_pos(v)
        assert "at (100.0, 50.0)" in v["description"]

    def test_no_duplicate_coordinates(self):
        v = _wire_dangling(100.0, 50.0, "Wire not connected at both ends at (100.0, 50.0)")
        _enrich_description_with_pos(v)
        assert v["description"].count("at (100.0, 50.0)") == 1

    def test_missing_pos_no_enrichment(self):
        v = {"type": "wire_dangling", "description": "Wire not connected"}
        _enrich_description_with_pos(v)
        assert v["description"] == "Wire not connected"

    def test_partial_pos_no_enrichment(self):
        v = {
            "type": "wire_dangling",
            "description": "Wire not connected",
            "pos": {"x": 10},
        }
        _enrich_description_with_pos(v)
        assert "at (" not in v["description"]


# ---------------------------------------------------------------------------
# Integration test: run_erc with wire_dangling re-attribution
# ---------------------------------------------------------------------------


class TestRunERCWireDanglingIntegration:
    """Verify that run_erc invokes re-attribution and the final
    ValidationIssue carries the correct sheet location."""

    def _run_erc_with_sheets(self, sheets: list[dict]) -> list[ValidationIssue]:
        """Run ``run_erc`` with mocked subprocess and custom sheet data."""
        erc_json = _make_erc_json(sheets)

        tmp = _tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w")
        tmp.write(erc_json)
        tmp.close()

        class _FakeTmp:
            name = tmp.name

            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

        with (
            patch(
                "kicad_tools.cli.sch_validate.find_kicad_cli",
                return_value="/usr/bin/kicad-cli",
            ),
            patch("kicad_tools.cli.sch_validate.subprocess.run") as mock_run,
            patch(
                "kicad_tools.cli.sch_validate.filter_cross_sheet_global_labels",
                side_effect=lambda v, p: v,
            ),
            patch(
                "kicad_tools.cli.sch_validate.filter_cross_sheet_power_violations",
                side_effect=lambda v, p: v,
            ),
            patch(
                "kicad_tools.cli.sch_validate.reattribute_wire_dangling_violations",
                side_effect=lambda v, p: v,
            ),
            patch("tempfile.NamedTemporaryFile", return_value=_FakeTmp()),
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            issues = run_erc("test.kicad_sch")

        if os.path.exists(tmp.name):
            os.unlink(tmp.name)
        return issues

    def test_wire_dangling_location_from_sheet_path(self):
        """Verify the location field uses _sheet_path for wire_dangling issues."""
        sheets = [
            _make_sheet("/", [_wire_dangling(100.0, 50.0)]),
        ]
        issues = self._run_erc_with_sheets(sheets)
        assert len(issues) == 1
        # With the mock passthrough, the sheet_path stays as "/"
        assert issues[0].location == "/"
        assert issues[0].category == "erc"

    def test_reattribute_called_in_pipeline(self):
        """Verify that reattribute_wire_dangling_violations is called."""
        erc_json = _make_erc_json(
            [_make_sheet("/", [_wire_dangling(100.0, 50.0)])]
        )

        tmp = _tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w")
        tmp.write(erc_json)
        tmp.close()

        class _FakeTmp:
            name = tmp.name

            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

        with (
            patch(
                "kicad_tools.cli.sch_validate.find_kicad_cli",
                return_value="/usr/bin/kicad-cli",
            ),
            patch("kicad_tools.cli.sch_validate.subprocess.run") as mock_run,
            patch(
                "kicad_tools.cli.sch_validate.filter_cross_sheet_global_labels",
                side_effect=lambda v, p: v,
            ),
            patch(
                "kicad_tools.cli.sch_validate.filter_cross_sheet_power_violations",
                side_effect=lambda v, p: v,
            ),
            patch(
                "kicad_tools.cli.sch_validate.reattribute_wire_dangling_violations",
                side_effect=lambda v, p: v,
            ) as mock_reattr,
            patch("tempfile.NamedTemporaryFile", return_value=_FakeTmp()),
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            run_erc("test.kicad_sch")

        mock_reattr.assert_called_once()
        # First arg is the violations list, second is the schematic path
        args = mock_reattr.call_args[0]
        assert args[1] == "test.kicad_sch"

        if os.path.exists(tmp.name):
            os.unlink(tmp.name)

    def test_multiple_sheets_wire_dangling_preserved(self):
        """Multiple sheets with wire_dangling keep their own sheet paths."""
        sheets = [
            _make_sheet("/", [_wire_dangling(10.0, 20.0)]),
            _make_sheet("/DAC", [_wire_dangling(30.0, 40.0)]),
        ]
        issues = self._run_erc_with_sheets(sheets)
        assert len(issues) == 2
        locs = {i.location for i in issues}
        assert "/" in locs
        assert "/DAC" in locs


class TestCoordinateSnap:
    """Verify floating-point coordinate matching with tolerance."""

    def test_slight_offset_still_matches(self):
        """A violation at (100.004, 50.003) should match a wire at (100.0, 50.0)."""
        violations = [_wire_dangling(100.004, 50.003)]
        violations[0]["_sheet_path"] = "/"

        fake_wire = MagicMock()
        fake_wire.start = (100.0, 50.0)
        fake_wire.end = (100.0, 80.0)

        fake_sch = MagicMock()
        fake_sch.wires = [fake_wire]

        fake_child = MagicMock()
        fake_child.is_root = False
        fake_child.path = "/tmp/dac.kicad_sch"
        fake_child.get_path_string.return_value = "/DAC"

        fake_root = MagicMock()
        fake_root.is_root = True
        fake_root.all_nodes.return_value = [fake_root, fake_child]

        with (
            patch(
                "kicad_tools.schema.hierarchy.build_hierarchy",
                return_value=fake_root,
            ),
            patch(
                "kicad_tools.schema.Schematic.load",
                return_value=fake_sch,
            ),
        ):
            result = reattribute_wire_dangling_violations(violations, "test.kicad_sch")

        assert result[0]["_sheet_path"] == "/DAC"
