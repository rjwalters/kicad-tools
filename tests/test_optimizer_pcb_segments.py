"""Tests for parse_segments and replace_segments in optimizer/pcb.py.

Validates that the balanced-parentheses walker and field-extraction
regexes handle:
  - Standard KiCad segment format (no uuid)
  - Segments with trailing (uuid "...") after (net N)
  - Segments with (uuid "...") *before* (net N) (field reorder)
  - Segments with (locked yes) interspersed
  - Mixed format boards (some segments with uuid, some without)
  - Multiline vs single-line segment formatting
  - replace_segments correctly removes uuid-bearing segments
  - replace_segments correctly removes segments with reordered fields
"""

import pytest

from kicad_tools.router.layers import Layer
from kicad_tools.router.optimizer.pcb import (
    _extract_balanced_blocks,
    parse_segments,
    replace_segments,
)
from kicad_tools.router.primitives import Segment

# ---------------------------------------------------------------------------
# Fixture PCB snippets
# ---------------------------------------------------------------------------

# Standard format: no uuid field
_PCB_STANDARD = """\
(kicad_pcb
  (net 0 "")
  (net 1 "VCC")
  (net 2 "GND")
  (segment (start 100 100) (end 110 100) (width 0.25) (layer "F.Cu") (net 1))
  (segment (start 110 100) (end 120 100) (width 0.25) (layer "F.Cu") (net 1))
  (segment (start 200 200) (end 210 200) (width 0.25) (layer "B.Cu") (net 2))
)
"""

# KiCad-cli DRC output format: uuid AFTER net
_PCB_UUID_AFTER_NET = """\
(kicad_pcb
  (net 0 "")
  (net 1 "VCC")
  (net 2 "GND")
  (segment (start 100 100) (end 110 100) (width 0.25) (layer "F.Cu") (net 1) (uuid "seg-a1"))
  (segment (start 110 100) (end 120 100) (width 0.25) (layer "F.Cu") (net 1) (uuid "seg-a2"))
  (segment (start 200 200) (end 210 200) (width 0.25) (layer "B.Cu") (net 2) (uuid "seg-b1"))
)
"""

# Reordered fields: uuid BEFORE net
_PCB_UUID_BEFORE_NET = """\
(kicad_pcb
  (net 0 "")
  (net 1 "VCC")
  (net 2 "GND")
  (segment (start 100 100) (end 110 100) (width 0.25) (layer "F.Cu") (uuid "seg-a1") (net 1))
  (segment (start 110 100) (end 120 100) (width 0.25) (layer "F.Cu") (uuid "seg-a2") (net 1))
  (segment (start 200 200) (end 210 200) (width 0.25) (layer "B.Cu") (uuid "seg-b1") (net 2))
)
"""

# Segments with (locked yes) interspersed between fields
_PCB_LOCKED_SEGMENTS = """\
(kicad_pcb
  (net 0 "")
  (net 1 "VCC")
  (segment (start 100 100) (end 110 100) (width 0.25) (layer "F.Cu") (locked yes) (net 1) (uuid "seg-lock"))
)
"""

# Mixed format: some segments have uuid, some don't
_PCB_MIXED_FORMAT = """\
(kicad_pcb
  (net 0 "")
  (net 1 "VCC")
  (net 2 "GND")
  (segment (start 100 100) (end 110 100) (width 0.25) (layer "F.Cu") (net 1))
  (segment (start 110 100) (end 120 100) (width 0.25) (layer "F.Cu") (net 1) (uuid "seg-a2"))
  (segment (start 200 200) (end 210 200) (width 0.25) (layer "B.Cu") (uuid "seg-b1") (net 2))
)
"""

# Multiline format (KiCad 10 style)
_PCB_MULTILINE = """\
(kicad_pcb
  (net 0 "")
  (net 1 "VCC")
  (segment
    (start 100 100)
    (end 110 100)
    (width 0.25)
    (layer "F.Cu")
    (net 1)
    (uuid "seg-multi-1")
  )
  (segment
    (start 110 100)
    (end 120 100)
    (width 0.25)
    (layer "F.Cu")
    (net 1)
    (uuid "seg-multi-2")
  )
)
"""

# Multiline format with reordered fields (uuid before net)
_PCB_MULTILINE_REORDERED = """\
(kicad_pcb
  (net 0 "")
  (net 1 "VCC")
  (segment
    (start 100 100)
    (end 110 100)
    (width 0.25)
    (layer "F.Cu")
    (uuid "seg-re-1")
    (net 1)
  )
)
"""


# ---------------------------------------------------------------------------
# Tests for _extract_balanced_blocks
# ---------------------------------------------------------------------------


class TestExtractBalancedBlocks:
    """Tests for the balanced-parentheses block extractor."""

    def test_extracts_simple_blocks(self):
        text = '(segment (start 1 2) (end 3 4) (width 0.25) (layer "F.Cu") (net 1))'
        blocks = _extract_balanced_blocks(text, "segment")
        assert len(blocks) == 1
        assert blocks[0][2] == text

    def test_extracts_blocks_with_uuid(self):
        text = '(segment (start 1 2) (end 3 4) (width 0.25) (layer "F.Cu") (net 1) (uuid "abc"))'
        blocks = _extract_balanced_blocks(text, "segment")
        assert len(blocks) == 1
        assert "(uuid" in blocks[0][2]

    def test_multiple_blocks(self):
        blocks = _extract_balanced_blocks(_PCB_UUID_AFTER_NET, "segment")
        assert len(blocks) == 3

    def test_does_not_match_partial_keyword(self):
        """'segment_extra' should not match keyword 'segment'."""
        text = '(segment_extra (start 1 2)) (segment (start 3 4) (end 5 6) (width 0.25) (layer "F.Cu") (net 1))'
        blocks = _extract_balanced_blocks(text, "segment")
        # Only the second block should match
        assert len(blocks) == 1
        assert "(start 3 4)" in blocks[0][2]

    def test_multiline_blocks(self):
        blocks = _extract_balanced_blocks(_PCB_MULTILINE, "segment")
        assert len(blocks) == 2

    def test_returns_correct_positions(self):
        text = "prefix (segment (net 1)) suffix"
        blocks = _extract_balanced_blocks(text, "segment")
        assert len(blocks) == 1
        start, end, block = blocks[0]
        assert text[start:end] == "(segment (net 1))"


# ---------------------------------------------------------------------------
# Tests for parse_segments
# ---------------------------------------------------------------------------


class TestParseSegments:
    """Tests for parse_segments with various field orderings and extra fields."""

    def test_standard_format(self):
        """Segments without uuid are parsed correctly."""
        result = parse_segments(_PCB_STANDARD)
        assert "VCC" in result
        assert "GND" in result
        assert len(result["VCC"]) == 2
        assert len(result["GND"]) == 1

    def test_uuid_after_net(self):
        """Segments with (uuid ...) after (net N) are parsed correctly."""
        result = parse_segments(_PCB_UUID_AFTER_NET)
        assert "VCC" in result
        assert len(result["VCC"]) == 2
        seg = result["VCC"][0]
        assert seg.x1 == pytest.approx(100.0)
        assert seg.y1 == pytest.approx(100.0)
        assert seg.x2 == pytest.approx(110.0)
        assert seg.y2 == pytest.approx(100.0)
        assert seg.width == pytest.approx(0.25)
        assert seg.net == 1

    def test_uuid_before_net(self):
        """Segments with (uuid ...) before (net N) are parsed correctly."""
        result = parse_segments(_PCB_UUID_BEFORE_NET)
        assert "VCC" in result
        assert len(result["VCC"]) == 2
        assert "GND" in result
        assert len(result["GND"]) == 1
        # Verify field values are correct despite reordering
        seg = result["VCC"][0]
        assert seg.x1 == pytest.approx(100.0)
        assert seg.net == 1
        assert seg.layer == Layer.F_CU

    def test_locked_segments(self):
        """Segments with (locked yes) interspersed are parsed correctly."""
        result = parse_segments(_PCB_LOCKED_SEGMENTS)
        assert "VCC" in result
        assert len(result["VCC"]) == 1
        seg = result["VCC"][0]
        assert seg.x1 == pytest.approx(100.0)
        assert seg.net == 1

    def test_mixed_format(self):
        """Board with mixed uuid/no-uuid segments parses all correctly."""
        result = parse_segments(_PCB_MIXED_FORMAT)
        assert "VCC" in result
        assert len(result["VCC"]) == 2
        assert "GND" in result
        assert len(result["GND"]) == 1

    def test_multiline_format(self):
        """Multiline (KiCad 10 style) segments are parsed correctly."""
        result = parse_segments(_PCB_MULTILINE)
        assert "VCC" in result
        assert len(result["VCC"]) == 2

    def test_multiline_reordered(self):
        """Multiline segments with uuid before net are parsed correctly."""
        result = parse_segments(_PCB_MULTILINE_REORDERED)
        assert "VCC" in result
        assert len(result["VCC"]) == 1
        seg = result["VCC"][0]
        assert seg.net == 1

    def test_layer_mapping(self):
        """Layer names are mapped to Layer enum correctly."""
        result = parse_segments(_PCB_UUID_AFTER_NET)
        vcc_seg = result["VCC"][0]
        gnd_seg = result["GND"][0]
        assert vcc_seg.layer == Layer.F_CU
        assert gnd_seg.layer == Layer.B_CU

    def test_empty_pcb(self):
        """PCB with no segments returns empty dict."""
        pcb = '(kicad_pcb (net 0 "") (net 1 "A"))'
        result = parse_segments(pcb)
        assert result == {}

    def test_segment_count_matches_across_formats(self):
        """All fixture formats that represent the same board yield the same count."""
        standard = parse_segments(_PCB_STANDARD)
        uuid_after = parse_segments(_PCB_UUID_AFTER_NET)
        uuid_before = parse_segments(_PCB_UUID_BEFORE_NET)
        mixed = parse_segments(_PCB_MIXED_FORMAT)

        for fmt_result in [standard, uuid_after, uuid_before, mixed]:
            assert len(fmt_result["VCC"]) == 2
            assert len(fmt_result["GND"]) == 1


# ---------------------------------------------------------------------------
# Tests for replace_segments
# ---------------------------------------------------------------------------


def _make_segments(net: int, net_name: str, count: int) -> list[Segment]:
    """Helper to build a list of Segment objects for testing."""
    segs = []
    for i in range(count):
        segs.append(
            Segment(
                x1=100.0 + i * 10,
                y1=100.0,
                x2=110.0 + i * 10,
                y2=100.0,
                width=0.25,
                layer=Layer.F_CU,
                net=net,
                net_name=net_name,
            )
        )
    return segs


class TestReplaceSegments:
    """Tests for replace_segments with various segment formats."""

    def test_removes_standard_segments(self):
        """Standard segments (no uuid) are removed and replaced."""
        original = parse_segments(_PCB_STANDARD)
        optimized = {"VCC": _make_segments(1, "VCC", 1)}  # 2 -> 1 segment
        result = replace_segments(_PCB_STANDARD, original, optimized)

        # Old VCC segments should be gone
        # New optimized segment should be present
        result_parsed = parse_segments(result)
        assert "VCC" in result_parsed
        # GND segment should remain (not in optimized)
        assert "GND" in result_parsed or "(net 2)" in result

    def test_removes_uuid_bearing_segments(self):
        """Segments with (uuid ...) after (net N) are correctly removed."""
        original = parse_segments(_PCB_UUID_AFTER_NET)
        optimized = {"VCC": _make_segments(1, "VCC", 1)}
        result = replace_segments(_PCB_UUID_AFTER_NET, original, optimized)

        # The original uuid-bearing segments for VCC should be gone
        assert '(uuid "seg-a1")' not in result
        assert '(uuid "seg-a2")' not in result
        # GND uuid segment should remain
        assert '(uuid "seg-b1")' in result

    def test_removes_segments_with_uuid_before_net(self):
        """Segments with (uuid ...) before (net N) are correctly removed."""
        original = parse_segments(_PCB_UUID_BEFORE_NET)
        optimized = {"VCC": _make_segments(1, "VCC", 1)}
        result = replace_segments(_PCB_UUID_BEFORE_NET, original, optimized)

        # The original reordered segments for VCC should be gone
        assert '(uuid "seg-a1")' not in result
        assert '(uuid "seg-a2")' not in result
        # GND segment should remain
        assert '(uuid "seg-b1")' in result

    def test_removes_locked_segments(self):
        """Segments with (locked yes) are correctly identified and removed."""
        original = parse_segments(_PCB_LOCKED_SEGMENTS)
        optimized = {"VCC": _make_segments(1, "VCC", 1)}
        result = replace_segments(_PCB_LOCKED_SEGMENTS, original, optimized)

        assert '(uuid "seg-lock")' not in result
        assert "(locked yes)" not in result

    def test_removes_mixed_format_segments(self):
        """Mixed uuid/no-uuid segments for the same net are all removed."""
        original = parse_segments(_PCB_MIXED_FORMAT)
        optimized = {"VCC": _make_segments(1, "VCC", 1)}
        result = replace_segments(_PCB_MIXED_FORMAT, original, optimized)

        # Both VCC segments (one with uuid, one without) should be gone
        assert '(uuid "seg-a2")' not in result
        # GND segment should remain
        assert '(uuid "seg-b1")' in result

    def test_removes_multiline_segments(self):
        """Multiline segment blocks are correctly removed."""
        original = parse_segments(_PCB_MULTILINE)
        optimized = {"VCC": _make_segments(1, "VCC", 1)}
        result = replace_segments(_PCB_MULTILINE, original, optimized)

        assert '(uuid "seg-multi-1")' not in result
        assert '(uuid "seg-multi-2")' not in result

    def test_inserts_new_segments(self):
        """Optimized segments are inserted into the PCB text."""
        original = parse_segments(_PCB_STANDARD)
        new_seg = _make_segments(1, "VCC", 1)
        optimized = {"VCC": new_seg}
        result = replace_segments(_PCB_STANDARD, original, optimized)

        # The new segment should be present
        assert "(segment" in result
        # It should have the new coordinates
        assert "100.0000" in result
        assert "110.0000" in result

    def test_no_duplicate_segments_after_replace(self):
        """Replace should not leave duplicate segment blocks."""
        original = parse_segments(_PCB_UUID_AFTER_NET)
        # Replace VCC with same count but different coords
        new_segs = [
            Segment(
                x1=150.0,
                y1=150.0,
                x2=160.0,
                y2=150.0,
                width=0.25,
                layer=Layer.F_CU,
                net=1,
                net_name="VCC",
            )
        ]
        optimized = {"VCC": new_segs}
        result = replace_segments(_PCB_UUID_AFTER_NET, original, optimized)

        # Count segments for net 1 in result
        result_segs = parse_segments(result)
        assert len(result_segs.get("VCC", [])) == 1
        # Verify it's the new segment, not the old one
        seg = result_segs["VCC"][0]
        assert seg.x1 == pytest.approx(150.0)

    def test_preserves_non_segment_content(self):
        """Non-segment content (net declarations, etc.) is preserved."""
        original = parse_segments(_PCB_UUID_AFTER_NET)
        optimized = {"VCC": _make_segments(1, "VCC", 1)}
        result = replace_segments(_PCB_UUID_AFTER_NET, original, optimized)

        assert '(net 0 "")' in result
        assert '(net 1 "VCC")' in result
        assert '(net 2 "GND")' in result

    def test_empty_optimized_removes_all_for_net(self):
        """When optimized has empty list, all segments for that net are removed."""
        original = parse_segments(_PCB_UUID_AFTER_NET)
        optimized = {"VCC": []}
        result = replace_segments(_PCB_UUID_AFTER_NET, original, optimized)

        # VCC segments should be removed
        assert '(uuid "seg-a1")' not in result
        assert '(uuid "seg-a2")' not in result
        # GND should remain
        assert '(uuid "seg-b1")' in result

    def test_replace_does_not_affect_other_nets(self):
        """Replacing segments for one net does not affect another net."""
        original = parse_segments(_PCB_UUID_AFTER_NET)
        optimized = {"VCC": _make_segments(1, "VCC", 1)}
        result = replace_segments(_PCB_UUID_AFTER_NET, original, optimized)

        result_segs = parse_segments(result)
        # GND should still have its original segment
        assert "GND" in result_segs
        assert len(result_segs["GND"]) == 1
        gnd_seg = result_segs["GND"][0]
        assert gnd_seg.x1 == pytest.approx(200.0)
        assert gnd_seg.y1 == pytest.approx(200.0)


# ---------------------------------------------------------------------------
# Regression test: the original bug scenario
# ---------------------------------------------------------------------------


class TestZoneFillDRCFallbackRegression:
    """Regression tests for the exact scenario described in issue #1284.

    After kicad-cli DRC re-serialises a PCB, segments gain (uuid "...")
    fields. The old regex-based replace_segments failed to remove these,
    causing duplicate segments or "0 -> 0" counts.
    """

    def test_full_round_trip_with_uuid_segments(self):
        """Parse, replace, re-parse: segment counts are correct after uuid addition."""
        # Simulate a board that went through kicad-cli DRC
        original = parse_segments(_PCB_UUID_AFTER_NET)
        assert len(original["VCC"]) == 2
        assert len(original["GND"]) == 1

        # "Optimize" VCC from 2 segments to 1
        optimized_vcc = _make_segments(1, "VCC", 1)
        optimized = {"VCC": optimized_vcc, "GND": original["GND"]}

        result = replace_segments(_PCB_UUID_AFTER_NET, original, optimized)
        result_parsed = parse_segments(result)

        assert len(result_parsed["VCC"]) == 1, "VCC should have exactly 1 optimized segment"
        assert len(result_parsed["GND"]) == 1, "GND should be unchanged"

    def test_full_round_trip_with_reordered_fields(self):
        """Parse, replace, re-parse: works when uuid precedes net."""
        original = parse_segments(_PCB_UUID_BEFORE_NET)
        assert len(original["VCC"]) == 2

        optimized_vcc = _make_segments(1, "VCC", 1)
        optimized = {"VCC": optimized_vcc}

        result = replace_segments(_PCB_UUID_BEFORE_NET, original, optimized)
        result_parsed = parse_segments(result)

        assert len(result_parsed["VCC"]) == 1

    def test_nonzero_segment_count_after_zone_fill_format(self):
        """Ensure parse_segments returns non-zero counts for zone-filled boards.

        This is the direct symptom: the old regex returned 0 matches for
        boards that went through kicad-cli DRC zone fill.
        """
        for pcb_text in [
            _PCB_UUID_AFTER_NET,
            _PCB_UUID_BEFORE_NET,
            _PCB_LOCKED_SEGMENTS,
            _PCB_MIXED_FORMAT,
            _PCB_MULTILINE,
            _PCB_MULTILINE_REORDERED,
        ]:
            result = parse_segments(pcb_text)
            total = sum(len(segs) for segs in result.values())
            assert total > 0, f"parse_segments returned 0 segments for:\n{pcb_text[:200]}"
