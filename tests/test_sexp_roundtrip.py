"""
Integration tests for S-expression round-trip with KiCad.

These tests verify that files saved by the SExp serializer can be loaded by KiCad CLI.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

from kicad_tools.sexp import parse_file
from kicad_tools.sexp.parser import parse_string

# Check if kicad-cli is available
KICAD_CLI = shutil.which("kicad-cli")


# Synthetic minimal footprint carrying an embedded keepout rule area, matching
# the shape KiCad emits for footprints like RF_Module:ESP32-C3-WROOM-02. Used
# instead of a stock-library fixture so the test has no dependency on a specific
# KiCad library install being present (issue #4185).
SYNTHETIC_KEEPOUT_FOOTPRINT = """(footprint "TEST:Keepout"
	(layer "F.Cu")
	(uuid "00000000-0000-0000-0000-000000000abc")
	(at 5 5)
	(zone
		(net 0)
		(net_name "")
		(layers "F&B.Cu")
		(hatch full 0.508)
		(keepout
			(tracks not_allowed)
			(vias not_allowed)
			(pads not_allowed)
			(copperpour not_allowed)
			(footprints not_allowed)
		)
		(polygon
			(pts
				(xy 0 0) (xy 1 0) (xy 1 1) (xy 0 1)
			)
		)
	)
)
"""


@pytest.fixture
def demo_pcb_path():
    """Path to a demo PCB file."""
    path = (
        Path(__file__).parent.parent / "boards" / "02-charlieplex-led" / "charlieplex_3x3.kicad_pcb"
    )
    if not path.exists():
        pytest.skip(f"Demo PCB file not found: {path}")
    return path


@pytest.fixture
def demo_routed_pcb_path():
    """Path to a demo routed PCB file."""
    path = (
        Path(__file__).parent.parent
        / "boards"
        / "02-charlieplex-led"
        / "charlieplex_3x3_routed.kicad_pcb"
    )
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

    def test_keepout_footprint_roundtrips_bare(self):
        """A footprint keepout rule area must round-trip with bare enum tokens.

        KiCad emits `(tracks not_allowed)` as bare symbols; the quoted form
        `(tracks "not_allowed")` is a hard parse error that makes the board
        unloadable. Before issue #4185 the serializer quoted these tokens
        because they were absent from the unquoted-keyword allowlist. This test
        uses a synthetic footprint fixture (no stock-library dependency).
        """
        output = parse_string(SYNTHETIC_KEEPOUT_FOOTPRINT).to_string()
        assert '"not_allowed"' not in output, (
            f"keepout enum tokens must round-trip bare, got: {output}"
        )
        for field in ("tracks", "vias", "pads", "copperpour", "footprints"):
            assert f"({field} not_allowed)" in output, (
                f"expected bare ({field} not_allowed) in: {output}"
            )

    def test_bare_backslash_token_roundtrips_quoted_and_escaped(self):
        """A bare token containing a raw backslash round-trips byte-exact.

        Parsing `(field abc\\def)` yields a bare atom whose value contains a
        literal backslash. Before issue #4213 the serializer re-emitted it bare
        and unescaped; with backslash added to _must_quote() it is now forced
        into the quoting branch, whose unconditional escaping doubles the
        backslash. The emitted form (`(field "abc\\\\def")`) must re-parse to
        the identical value, and a second serialize/parse cycle must be a fixed
        point (byte-exact round-trip).
        """
        # Source text: (field abc\def) — a single, unescaped, bare backslash.
        doc = parse_string("(field abc\\def)")
        assert doc.get_string(0) == "abc\\def"

        # Serialized output forces quoting and doubles the backslash.
        output = doc.to_string()
        assert '(field "abc\\\\def")' in output, (
            f"bare backslash token must serialize quoted+escaped, got: {output}"
        )

        # Re-parsing the emitted form yields the exact original value.
        reparsed = parse_string(output)
        assert reparsed.get_string(0) == "abc\\def"

        # A second serialize cycle is a fixed point (stable byte-exact).
        assert reparsed.to_string() == output

    @pytest.mark.skipif(KICAD_CLI is None, reason="kicad-cli not installed")
    def test_keepout_board_loads_in_kicad(self, tmp_path):
        """A board carrying a keepout footprint must load after round-trip.

        Injects a synthetic footprint with an embedded keepout rule area into a
        real demo board, serializes it, and asserts kicad-cli can load it. Before
        issue #4185 the keepout tokens were quoted, causing "Failed to load
        board" (verified: the quoted form is a hard pcbnew parse error).
        """
        demo = (
            Path(__file__).parent.parent
            / "boards"
            / "00-simple-led"
            / "output"
            / "simple_led.kicad_pcb"
        )
        if not demo.exists():
            pytest.skip(f"Demo board not found: {demo}")

        doc = parse_file(demo)
        # The synthetic footprint already carries a uuid so KiCad accepts it.
        doc.children.append(parse_string(SYNTHETIC_KEEPOUT_FOOTPRINT))

        output = doc.to_string()
        assert '"not_allowed"' not in output, "keepout tokens must be bare before load"

        output_path = tmp_path / "keepout_board.kicad_pcb"
        output_path.write_text(output)

        result = subprocess.run(
            [KICAD_CLI, "pcb", "drc", str(output_path), "-o", str(tmp_path / "drc.json")],
            capture_output=True,
            text=True,
        )
        assert "Failed to load board" not in result.stderr, (
            f"KiCad failed to load board with keepout footprint.\n"
            f"stdout: {result.stdout}\n"
            f"stderr: {result.stderr}"
        )

    def test_serialized_output_format(self, demo_pcb_path):
        """Verify that serialized output uses correct KiCad formatting."""
        doc = parse_file(demo_pcb_path)
        output = doc.to_string()

        # Check indentation uses tabs, matching KiCad native format
        assert "\t" in output, "Output should use tab indentation"

        # Check that known keywords are not quoted
        # These keywords appear in the demo file
        assert "signal)" in output or "signal\n" in output, "signal keyword should not be quoted"
        assert "user)" in output or "user " in output, "user keyword should not be quoted"
        assert "no)" in output or "no\n" in output, "no keyword should not be quoted"
        assert "none)" in output or "none\n" in output, "none keyword should not be quoted"
        assert "default)" in output or "default\n" in output, "default keyword should not be quoted"

        # Check that layer names are quoted
        assert '"F.Cu"' in output, "Layer names should be quoted"
        assert '"B.Cu"' in output, "Layer names should be quoted"

    def test_serialized_output_reparseable(self, demo_pcb_path):
        """Verify that serialized output can be parsed again."""
        doc = parse_file(demo_pcb_path)
        output = doc.to_string()

        # Should be able to parse the output
        reparsed = parse_file.__wrapped__(output) if hasattr(parse_file, "__wrapped__") else None
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
