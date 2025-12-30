"""
Integration tests for S-expression round-trip with KiCad.

These tests verify that files saved by the SExp serializer can be loaded by KiCad CLI.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

from kicad_tools.sexp import parse_file

# Check if kicad-cli is available
KICAD_CLI = shutil.which("kicad-cli")


@pytest.fixture
def demo_pcb_path():
    """Path to a demo PCB file."""
    path = Path(__file__).parent.parent / "demo" / "charlieplex_led_grid" / "charlieplex_3x3.kicad_pcb"
    if not path.exists():
        pytest.skip(f"Demo PCB file not found: {path}")
    return path


@pytest.fixture
def demo_routed_pcb_path():
    """Path to a demo routed PCB file."""
    path = Path(__file__).parent.parent / "demo" / "charlieplex_led_grid" / "charlieplex_3x3_routed.kicad_pcb"
    if not path.exists():
        pytest.skip(f"Demo PCB file not found: {path}")
    return path


class TestSExpRoundTripWithKiCad:
    """Tests that verify round-trip compatibility with KiCad CLI."""

    @pytest.mark.skipif(KICAD_CLI is None, reason="kicad-cli not installed")
    def test_roundtrip_demo_pcb_loads_in_kicad(self, demo_pcb_path, tmp_path):
        """Verify that serialized PCB can be loaded by kicad-cli."""
        # Parse the original file
        doc = parse_file(demo_pcb_path)

        # Serialize to temp file
        output_path = tmp_path / "roundtrip_test.kicad_pcb"
        output_path.write_text(doc.to_string())

        # Try to load with kicad-cli (DRC command will fail if file can't be loaded)
        result = subprocess.run(
            [KICAD_CLI, "pcb", "drc", str(output_path), "-o", str(tmp_path / "drc.json")],
            capture_output=True,
            text=True,
        )

        # kicad-cli returns 0 on success or if violations found, non-zero only on load failure
        assert "Failed to load board" not in result.stderr, (
            f"KiCad failed to load serialized file.\n"
            f"stdout: {result.stdout}\n"
            f"stderr: {result.stderr}"
        )

    @pytest.mark.skipif(KICAD_CLI is None, reason="kicad-cli not installed")
    def test_roundtrip_routed_pcb_loads_in_kicad(self, demo_routed_pcb_path, tmp_path):
        """Verify that serialized routed PCB can be loaded by kicad-cli."""
        # Parse the original file
        doc = parse_file(demo_routed_pcb_path)

        # Serialize to temp file
        output_path = tmp_path / "roundtrip_routed_test.kicad_pcb"
        output_path.write_text(doc.to_string())

        # Try to load with kicad-cli
        result = subprocess.run(
            [KICAD_CLI, "pcb", "drc", str(output_path), "-o", str(tmp_path / "drc.json")],
            capture_output=True,
            text=True,
        )

        assert "Failed to load board" not in result.stderr, (
            f"KiCad failed to load serialized file.\n"
            f"stdout: {result.stdout}\n"
            f"stderr: {result.stderr}"
        )

    def test_serialized_output_format(self, demo_pcb_path):
        """Verify that serialized output uses correct KiCad formatting."""
        doc = parse_file(demo_pcb_path)
        output = doc.to_string()

        # Check indentation uses spaces, not tabs
        assert '\t' not in output, "Output should use spaces, not tabs"

        # Check that known keywords are not quoted
        # These keywords appear in the demo file
        assert " signal)" in output or " signal\n" in output, "signal keyword should not be quoted"
        assert " user)" in output or " user " in output, "user keyword should not be quoted"
        assert " no)" in output or " no\n" in output, "no keyword should not be quoted"
        assert " none)" in output or " none\n" in output, "none keyword should not be quoted"
        assert " default)" in output or " default\n" in output, "default keyword should not be quoted"

        # Check that layer names are quoted
        assert '"F.Cu"' in output, "Layer names should be quoted"
        assert '"B.Cu"' in output, "Layer names should be quoted"

    def test_serialized_output_reparseable(self, demo_pcb_path):
        """Verify that serialized output can be parsed again."""
        doc = parse_file(demo_pcb_path)
        output = doc.to_string()

        # Should be able to parse the output
        reparsed = parse_file.__wrapped__(output) if hasattr(parse_file, '__wrapped__') else None
        if reparsed is None:
            # parse_file expects a path, use parse_string equivalent
            from kicad_tools.sexp.parser import parse_string
            reparsed = parse_string(output)

        assert reparsed.name == "kicad_pcb"
        assert reparsed["version"] is not None
        assert reparsed["generator"] is not None

    def test_footprint_keywords_unquoted(self, demo_pcb_path):
        """Verify that footprint keywords are not quoted."""
        doc = parse_file(demo_pcb_path)
        output = doc.to_string()

        # Check pad type keywords
        if "thru_hole" in output:
            assert '"thru_hole"' not in output, "thru_hole should not be quoted"
        if "smd" in output:
            assert '"smd"' not in output, "smd should not be quoted"

        # Check pad shape keywords
        if "rect " in output or "rect)" in output:
            assert '"rect"' not in output, "rect should not be quoted"
        if "oval " in output or "oval)" in output:
            assert '"oval"' not in output, "oval should not be quoted"
        if "roundrect" in output:
            assert '"roundrect"' not in output, "roundrect should not be quoted"

        # Check fp_text types
        if "reference" in output:
            assert '"reference"' not in output, "reference should not be quoted"
        if " value " in output or "(fp_text value" in output:
            # Note: 'value' as keyword, not as a component value like "100R"
            pass  # This is trickier to test, skip for now
