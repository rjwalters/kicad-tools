"""Tests for interactive HTML report generation.

Covers PCB data extraction, interactive HTML rendering, and
template placeholder injection.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

from kicad_tools.report.pcb_data import (
    extract_pcb_data,
    natsort_refs,
)

# ---------------------------------------------------------------------------
# natsort_refs
# ---------------------------------------------------------------------------


class TestNatsortRefs:
    def test_simple_sort(self) -> None:
        refs = ["R10", "R2", "R1", "R20"]
        assert natsort_refs(refs) == ["R1", "R2", "R10", "R20"]

    def test_mixed_prefixes(self) -> None:
        refs = ["C1", "R2", "C10", "R1"]
        result = natsort_refs(refs)
        assert result == ["C1", "C10", "R1", "R2"]

    def test_empty(self) -> None:
        assert natsort_refs([]) == []

    def test_single(self) -> None:
        assert natsort_refs(["U1"]) == ["U1"]


# ---------------------------------------------------------------------------
# extract_pcb_data
# ---------------------------------------------------------------------------


def _make_mock_pcb(
    outline: list[tuple[float, float]] | None = None,
    footprints: list | None = None,
    segments: list | None = None,
    vias: list | None = None,
    copper_layers: list | None = None,
) -> MagicMock:
    """Build a mock PCB object with controllable geometry."""
    pcb = MagicMock()

    # Board outline
    if outline is not None:
        pcb.get_board_outline.return_value = outline
    else:
        pcb.get_board_outline.return_value = [
            (0, 0),
            (50, 0),
            (50, 30),
            (0, 30),
        ]

    # Copper layers
    if copper_layers is None:
        layer_f = MagicMock()
        layer_f.name = "F.Cu"
        layer_b = MagicMock()
        layer_b.name = "B.Cu"
        copper_layers = [layer_f, layer_b]
    pcb.copper_layers = copper_layers

    # Footprints
    if footprints is None:
        fp = MagicMock()
        fp.reference = "R1"
        fp.value = "10k"
        fp.position = (25.0, 15.0)
        fp.rotation = 0.0
        fp.layer = "F.Cu"
        fp.attr = "smd"
        pad = MagicMock()
        pad.number = "1"
        pad.type = "smd"
        pad.shape = "roundrect"
        pad.position = (24.0, 15.0)
        pad.size = (1.0, 0.8)
        pad.layers = ["F.Cu"]
        pad.net_name = "GND"
        fp.pads = [pad]
        footprints = [fp]
    pcb.footprints = footprints

    # Segments
    if segments is None:
        seg = MagicMock()
        seg.start = (10.0, 15.0)
        seg.end = (25.0, 15.0)
        seg.width = 0.25
        seg.layer = "F.Cu"
        seg.net_number = 1
        seg.net_name = "GND"
        segments = [seg]
    pcb.segments = segments

    # Vias
    if vias is None:
        via = MagicMock()
        via.position = (20.0, 15.0)
        via.size = 0.6
        via.drill = 0.3
        via.layers = ["F.Cu", "B.Cu"]
        via.net_number = 1
        via.net_name = "GND"
        vias = [via]
    pcb.vias = vias

    return pcb


class TestExtractPcbData:
    def test_basic_structure(self) -> None:
        pcb = _make_mock_pcb()
        data = extract_pcb_data(pcb)

        assert "board_outline" in data
        assert "bounds" in data
        assert "footprints" in data
        assert "segments" in data
        assert "vias" in data
        assert "layers" in data

    def test_outline_extracted(self) -> None:
        pcb = _make_mock_pcb(outline=[(0, 0), (100, 0), (100, 50), (0, 50)])
        data = extract_pcb_data(pcb)
        assert len(data["board_outline"]) == 4
        assert data["board_outline"][0] == [0.0, 0.0]

    def test_bounds_from_outline(self) -> None:
        pcb = _make_mock_pcb(outline=[(10, 20), (60, 20), (60, 50), (10, 50)])
        data = extract_pcb_data(pcb)
        assert data["bounds"]["min_x"] == 10.0
        assert data["bounds"]["min_y"] == 20.0
        assert data["bounds"]["max_x"] == 60.0
        assert data["bounds"]["max_y"] == 50.0

    def test_footprint_data(self) -> None:
        pcb = _make_mock_pcb()
        data = extract_pcb_data(pcb)
        fps = data["footprints"]
        assert len(fps) == 1
        assert fps[0]["reference"] == "R1"
        assert fps[0]["value"] == "10k"
        assert len(fps[0]["pads"]) == 1
        assert fps[0]["pads"][0]["net_name"] == "GND"

    def test_segments_data(self) -> None:
        pcb = _make_mock_pcb()
        data = extract_pcb_data(pcb)
        segs = data["segments"]
        assert len(segs) == 1
        assert segs[0]["layer"] == "F.Cu"
        assert segs[0]["width"] == 0.25

    def test_vias_data(self) -> None:
        pcb = _make_mock_pcb()
        data = extract_pcb_data(pcb)
        vias = data["vias"]
        assert len(vias) == 1
        assert vias[0]["size"] == 0.6
        assert vias[0]["drill"] == 0.3

    def test_layers_list(self) -> None:
        pcb = _make_mock_pcb()
        data = extract_pcb_data(pcb)
        assert data["layers"] == ["F.Cu", "B.Cu"]

    def test_empty_board(self) -> None:
        pcb = _make_mock_pcb(
            outline=[],
            footprints=[],
            segments=[],
            vias=[],
        )
        # get_board_outline returns empty list
        pcb.get_board_outline.return_value = []
        data = extract_pcb_data(pcb)
        assert data["footprints"] == []
        assert data["segments"] == []
        assert data["vias"] == []
        # bounds should have a fallback
        assert "min_x" in data["bounds"]

    def test_json_serializable(self) -> None:
        pcb = _make_mock_pcb()
        data = extract_pcb_data(pcb)
        # Should not raise
        json_str = json.dumps(data)
        assert len(json_str) > 0


# ---------------------------------------------------------------------------
# Interactive HTML rendering
# ---------------------------------------------------------------------------


class TestRenderInteractiveHtml:
    def test_single_file_output(self) -> None:
        """Output should be a complete HTML file with no external references."""
        from kicad_tools.report.interactive import _build_html

        pcb_data = {
            "board_outline": [[0, 0], [50, 0], [50, 30], [0, 30]],
            "bounds": {"min_x": 0, "min_y": 0, "max_x": 50, "max_y": 30},
            "footprints": [],
            "segments": [],
            "vias": [],
            "layers": ["F.Cu", "B.Cu"],
        }
        drc_data = {
            "violations": [],
            "error_count": 0,
            "warning_count": 0,
        }

        html = _build_html("Test Report", pcb_data, drc_data, "test", "2026-01-01")

        # Single-file: must contain DOCTYPE, inline style and script
        assert "<!DOCTYPE html>" in html
        assert "<style>" in html
        assert "<script>" in html
        # No external resource links
        assert 'href="http' not in html
        assert 'src="http' not in html
        assert "<link " not in html

    def test_drc_violations_in_output(self) -> None:
        """Violations should appear in both the JS data and be addressable."""
        from kicad_tools.report.interactive import _build_html

        pcb_data = {
            "board_outline": [],
            "bounds": {"min_x": 0, "min_y": 0, "max_x": 100, "max_y": 100},
            "footprints": [],
            "segments": [],
            "vias": [],
            "layers": [],
        }
        violations = [
            {
                "type": "clearance",
                "type_str": "Clearance",
                "severity": "error",
                "message": "Pad too close to track",
                "locations": [{"x_mm": 25.0, "y_mm": 15.0, "layer": "F.Cu"}],
                "items": [],
                "nets": [],
            }
        ]
        drc_data = {
            "violations": violations,
            "error_count": 1,
            "warning_count": 0,
        }

        html = _build_html("Test", pcb_data, drc_data, "test", "2026-01-01")

        # Violation data should be embedded in the HTML
        assert "Pad too close to track" in html
        assert "25.0" in html
        assert "15.0" in html

    def test_zero_violations(self) -> None:
        """Board with zero violations should still produce valid HTML."""
        from kicad_tools.report.interactive import _build_html

        pcb_data = {
            "board_outline": [[0, 0], [50, 0], [50, 30], [0, 30]],
            "bounds": {"min_x": 0, "min_y": 0, "max_x": 50, "max_y": 30},
            "footprints": [],
            "segments": [],
            "vias": [],
            "layers": ["F.Cu"],
        }
        drc_data = {
            "violations": [],
            "error_count": 0,
            "warning_count": 0,
        }

        html = _build_html("No Violations", pcb_data, drc_data, "test", "2026-01-01")
        assert "<!DOCTYPE html>" in html
        # The JS handles empty violations gracefully
        assert '"violations":[]' in html

    def test_violations_without_location(self) -> None:
        """Violations without location data should degrade gracefully."""
        from kicad_tools.report.interactive import _build_html

        pcb_data = {
            "board_outline": [],
            "bounds": {"min_x": 0, "min_y": 0, "max_x": 50, "max_y": 30},
            "footprints": [],
            "segments": [],
            "vias": [],
            "layers": [],
        }
        violations = [
            {
                "type": "unknown",
                "type_str": "UnknownRule",
                "severity": "warning",
                "message": "Some warning without location",
                "locations": [],
                "items": [],
                "nets": [],
            }
        ]
        drc_data = {
            "violations": violations,
            "error_count": 0,
            "warning_count": 1,
        }

        html = _build_html("Test", pcb_data, drc_data, "test", "2026-01-01")
        assert "Some warning without location" in html

    def test_html_escaping(self) -> None:
        """Title with special characters should be escaped."""
        from kicad_tools.report.interactive import _build_html

        pcb_data = {
            "board_outline": [],
            "bounds": {"min_x": 0, "min_y": 0, "max_x": 50, "max_y": 30},
            "footprints": [],
            "segments": [],
            "vias": [],
            "layers": [],
        }
        drc_data = {"violations": [], "error_count": 0, "warning_count": 0}

        html = _build_html(
            "Board <script>alert(1)</script>",
            pcb_data,
            drc_data,
            "test",
            "2026-01-01",
        )
        # Title should be escaped
        assert "&lt;script&gt;" in html
        assert "<script>alert(1)</script>" not in html.split("<style>")[0]


# ---------------------------------------------------------------------------
# Template placeholder injection
# ---------------------------------------------------------------------------


class TestTemplateInjection:
    def test_placeholders_replaced(self) -> None:
        """All template placeholders should be replaced in the output."""
        from kicad_tools.report.interactive import _build_html

        pcb_data = {
            "board_outline": [],
            "bounds": {"min_x": 0, "min_y": 0, "max_x": 50, "max_y": 30},
            "footprints": [],
            "segments": [],
            "vias": [],
            "layers": [],
        }
        drc_data = {"violations": [], "error_count": 0, "warning_count": 0}

        html = _build_html("Test", pcb_data, drc_data, "proj", "2026-01-01")

        # No unreplaced placeholders
        assert "{{ title }}" not in html
        assert "{{ css }}" not in html
        assert "{{ js }}" not in html
        assert "{{ pcb_data_script }}" not in html
        assert "{{ drc_data_script }}" not in html
        assert "{{ report_meta_script }}" not in html

    def test_valid_json_in_scripts(self) -> None:
        """JSON data injected into scripts should be valid."""
        from kicad_tools.report.interactive import _build_html

        pcb_data = {
            "board_outline": [[0, 0], [10, 10]],
            "bounds": {"min_x": 0, "min_y": 0, "max_x": 10, "max_y": 10},
            "footprints": [{"reference": "U1", "position": [5, 5]}],
            "segments": [],
            "vias": [],
            "layers": ["F.Cu"],
        }
        drc_data = {"violations": [], "error_count": 0, "warning_count": 0}

        html = _build_html("Test", pcb_data, drc_data, "proj", "2026-01-01")

        # Extract the PCB_DATA JSON from the HTML
        import re

        match = re.search(r"window\.PCB_DATA\s*=\s*({.*?});", html, re.DOTALL)
        assert match is not None
        parsed = json.loads(match.group(1))
        assert parsed["layers"] == ["F.Cu"]
        assert len(parsed["board_outline"]) == 2
