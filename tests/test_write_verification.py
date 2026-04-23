"""Tests for post-write verification of zones and stitch persistence.

Covers the fix for issue #1944: zones batch and stitch commands silently
fail to persist changes.
"""

import pytest
from pathlib import Path

from kicad_tools.core.sexp_file import (
    load_pcb,
    save_pcb,
    verify_pcb_write,
    WriteVerificationError,
)
from kicad_tools.sexp import SExp, parse_string
from kicad_tools.sexp.builders import via_node, zone_node, segment_node


# ---------------------------------------------------------------------------
# Minimal PCB fixture text
# ---------------------------------------------------------------------------

MINIMAL_PCB = """\
(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")
  (net 1 "GND")
  (net 2 "+3.3V")
)"""


def _write_minimal_pcb(tmp_path: Path) -> Path:
    """Write a minimal PCB fixture and return its path."""
    pcb = tmp_path / "test.kicad_pcb"
    pcb.write_text(MINIMAL_PCB, encoding="utf-8")
    return pcb


# ---------------------------------------------------------------------------
# SExp round-trip tests
# ---------------------------------------------------------------------------


class TestSExpRoundTrip:
    """Verify that append + to_string correctly persists new children."""

    def test_append_zone_round_trip(self):
        """Parse a PCB, append a zone, serialize, re-parse, verify zone exists."""
        doc = parse_string(MINIMAL_PCB)
        zone = zone_node(
            net=1,
            net_name="GND",
            layer="B.Cu",
            points=[(0, 0), (100, 0), (100, 100), (0, 100)],
            uuid_str="test-uuid-zone",
        )
        doc.append(zone)
        text = doc.to_string()

        reparsed = parse_string(text)
        zones = reparsed.find_all("zone")
        assert len(zones) == 1
        # Verify net_name is correct
        net_name_node = zones[0].get("net_name")
        assert net_name_node is not None
        assert net_name_node.get_first_atom() == "GND"

    def test_append_via_round_trip(self):
        """Parse a PCB, append a via, serialize, re-parse, verify via exists."""
        doc = parse_string(MINIMAL_PCB)
        via = via_node(
            x=50,
            y=50,
            size=0.45,
            drill=0.2,
            layers=("F.Cu", "B.Cu"),
            net=1,
            uuid_str="test-uuid-via",
        )
        doc.append(via)
        text = doc.to_string()

        reparsed = parse_string(text)
        vias = reparsed.find_all("via")
        assert len(vias) == 1

    def test_append_segment_round_trip(self):
        """Parse a PCB, append a segment, serialize, re-parse, verify segment exists."""
        doc = parse_string(MINIMAL_PCB)
        seg = segment_node(
            start_x=10,
            start_y=20,
            end_x=30,
            end_y=40,
            width=0.2,
            layer="F.Cu",
            net=1,
            uuid_str="test-uuid-seg",
        )
        doc.append(seg)
        text = doc.to_string()

        reparsed = parse_string(text)
        segments = reparsed.find_all("segment")
        assert len(segments) == 1

    def test_append_multiple_elements(self):
        """Append zones, vias, and segments together; all survive round-trip."""
        doc = parse_string(MINIMAL_PCB)

        for i in range(3):
            doc.append(
                zone_node(
                    net=1,
                    net_name="GND",
                    layer="B.Cu",
                    points=[(0, 0), (100, 0), (100, 100), (0, 100)],
                    uuid_str=f"zone-{i}",
                )
            )

        for i in range(5):
            doc.append(
                via_node(
                    x=10 + i * 10,
                    y=50,
                    size=0.45,
                    drill=0.2,
                    layers=("F.Cu", "B.Cu"),
                    net=1,
                    uuid_str=f"via-{i}",
                )
            )

        text = doc.to_string()
        reparsed = parse_string(text)

        assert len(reparsed.find_all("zone")) == 3
        assert len(reparsed.find_all("via")) == 5


# ---------------------------------------------------------------------------
# verify_pcb_write tests
# ---------------------------------------------------------------------------


class TestVerifyPcbWrite:
    """Tests for the verify_pcb_write helper."""

    def test_passes_when_structures_present(self, tmp_path):
        """Verification passes when expected structures exist."""
        pcb = _write_minimal_pcb(tmp_path)
        sexp = load_pcb(pcb)

        sexp.append(
            zone_node(
                net=1,
                net_name="GND",
                layer="B.Cu",
                points=[(0, 0), (100, 0), (100, 100), (0, 100)],
                uuid_str="zone-1",
            )
        )
        sexp.append(
            via_node(
                x=50,
                y=50,
                size=0.45,
                drill=0.2,
                layers=("F.Cu", "B.Cu"),
                net=1,
                uuid_str="via-1",
            )
        )
        save_pcb(sexp, pcb)

        # Should not raise
        verify_pcb_write(pcb, expected_zones=1, expected_vias=1)

    def test_fails_when_zones_missing(self, tmp_path):
        """Verification raises error when expected zones are missing."""
        pcb = _write_minimal_pcb(tmp_path)

        with pytest.raises(WriteVerificationError, match="zone"):
            verify_pcb_write(pcb, expected_zones=3)

    def test_fails_when_vias_missing(self, tmp_path):
        """Verification raises error when expected vias are missing."""
        pcb = _write_minimal_pcb(tmp_path)

        with pytest.raises(WriteVerificationError, match="via"):
            verify_pcb_write(pcb, expected_vias=10)

    def test_fails_when_segments_missing(self, tmp_path):
        """Verification raises error when expected segments are missing."""
        pcb = _write_minimal_pcb(tmp_path)

        with pytest.raises(WriteVerificationError, match="segment"):
            verify_pcb_write(pcb, expected_segments=5)

    def test_passes_with_zero_expectations(self, tmp_path):
        """Verification passes when no structures expected (all defaults)."""
        pcb = _write_minimal_pcb(tmp_path)
        # Should not raise
        verify_pcb_write(pcb)

    def test_multiple_failures_reported(self, tmp_path):
        """All missing structure types reported in error message."""
        pcb = _write_minimal_pcb(tmp_path)

        with pytest.raises(WriteVerificationError) as exc_info:
            verify_pcb_write(pcb, expected_zones=1, expected_vias=1, expected_segments=1)

        msg = str(exc_info.value)
        assert "zone" in msg
        assert "via" in msg
        assert "segment" in msg


# ---------------------------------------------------------------------------
# ZoneGenerator persistence tests
# ---------------------------------------------------------------------------


class TestZoneGeneratorPersistence:
    """Test that ZoneGenerator.save() persists zones to disk."""

    def test_save_persists_zones(self, tmp_path):
        """Zones added via ZoneGenerator appear in the written file."""
        from kicad_tools.zones import ZoneGenerator

        pcb = _write_minimal_pcb(tmp_path)
        out = tmp_path / "output.kicad_pcb"

        gen = ZoneGenerator.from_pcb(str(pcb))
        gen.add_zone(net="GND", layer="B.Cu", priority=1)
        gen.save(out)

        # Re-read and verify
        sexp = load_pcb(out)
        zones = sexp.find_all("zone")
        assert len(zones) == 1
        assert zones[0].get("net_name").get_first_atom() == "GND"

    def test_save_multiple_zones(self, tmp_path):
        """Multiple zones all persist correctly."""
        from kicad_tools.zones import ZoneGenerator

        pcb = _write_minimal_pcb(tmp_path)
        out = tmp_path / "output.kicad_pcb"

        gen = ZoneGenerator.from_pcb(str(pcb))
        gen.add_zone(net="GND", layer="B.Cu", priority=1)
        gen.add_zone(net="+3.3V", layer="F.Cu", priority=0)
        gen.save(out)

        sexp = load_pcb(out)
        zones = sexp.find_all("zone")
        assert len(zones) == 2

    def test_save_idempotent(self, tmp_path):
        """Calling save() twice does not duplicate zones."""
        from kicad_tools.zones import ZoneGenerator

        pcb = _write_minimal_pcb(tmp_path)
        out = tmp_path / "output.kicad_pcb"

        gen = ZoneGenerator.from_pcb(str(pcb))
        gen.add_zone(net="GND", layer="B.Cu")
        gen.save(out)
        # Second save should not duplicate
        gen.save(out)

        sexp = load_pcb(out)
        zones = sexp.find_all("zone")
        assert len(zones) == 1

    def test_apply_then_save_no_duplicate(self, tmp_path):
        """Calling apply() then save() does not duplicate zones."""
        from kicad_tools.zones import ZoneGenerator

        pcb = _write_minimal_pcb(tmp_path)
        out = tmp_path / "output.kicad_pcb"

        gen = ZoneGenerator.from_pcb(str(pcb))
        gen.add_zone(net="GND", layer="B.Cu")
        gen.apply()
        gen.save(out)

        sexp = load_pcb(out)
        zones = sexp.find_all("zone")
        assert len(zones) == 1

    def test_save_in_place(self, tmp_path):
        """Saving in-place (output == input) works correctly."""
        from kicad_tools.zones import ZoneGenerator

        pcb = _write_minimal_pcb(tmp_path)

        gen = ZoneGenerator.from_pcb(str(pcb))
        gen.add_zone(net="GND", layer="B.Cu")
        gen.save(pcb)

        sexp = load_pcb(pcb)
        zones = sexp.find_all("zone")
        assert len(zones) == 1


# ---------------------------------------------------------------------------
# Stitch persistence tests
# ---------------------------------------------------------------------------


STITCH_PCB = """\
(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")
  (net 1 "GND")
  (zone
    (net 1)
    (net_name "GND")
    (layer "B.Cu")
    (uuid "existing-zone")
    (hatch edge 0.5)
    (connect_pads (clearance 0.2))
    (min_thickness 0.2)
    (filled_areas_thickness no)
    (fill yes (thermal_gap 0.3) (thermal_bridge_width 0.3))
    (polygon
      (pts
        (xy 0 0)
        (xy 200 0)
        (xy 200 200)
        (xy 0 200)
      )
    )
  )
  (footprint "R_0402"
    (layer "F.Cu")
    (at 100 100)
    (pad "1" smd rect
      (at -0.5 0)
      (size 0.6 0.5)
      (layers "F.Cu" "F.Paste" "F.Mask")
      (net 1 "GND")
    )
  )
)"""


class TestStitchPersistence:
    """Test that stitch commands persist vias to disk."""

    def test_blanket_stitch_persists_vias(self, tmp_path):
        """Blanket stitching vias appear in the written file."""
        from kicad_tools.cli.stitch_cmd import run_blanket_stitch

        pcb = tmp_path / "stitch_test.kicad_pcb"
        pcb.write_text(STITCH_PCB, encoding="utf-8")

        result = run_blanket_stitch(
            pcb_path=pcb,
            net_names=["GND"],
            via_size=0.45,
            drill=0.2,
            clearance=0.2,
            spacing=5.0,
        )

        if result.vias_added:
            # Re-read and verify vias are present
            sexp = load_pcb(pcb)
            vias = sexp.find_all("via")
            assert len(vias) >= len(result.vias_added)

    def test_pad_stitch_persists_vias_and_traces(self, tmp_path):
        """Pad-based stitching vias and traces appear in the written file."""
        from kicad_tools.cli.stitch_cmd import run_stitch

        pcb = tmp_path / "stitch_test.kicad_pcb"
        pcb.write_text(STITCH_PCB, encoding="utf-8")

        result = run_stitch(
            pcb_path=pcb,
            net_names=["GND"],
            via_size=0.45,
            drill=0.2,
            clearance=0.2,
            offset=0.5,
        )

        if result.vias_added:
            sexp = load_pcb(pcb)
            vias = sexp.find_all("via")
            assert len(vias) >= len(result.vias_added)

        if result.traces_added:
            sexp = load_pcb(pcb)
            segments = sexp.find_all("segment")
            assert len(segments) >= len(result.traces_added)


# ---------------------------------------------------------------------------
# Sequential zones-then-stitch test
# ---------------------------------------------------------------------------


class TestZonesThenStitch:
    """Test running zones batch then stitch sequentially on same file."""

    def test_zones_then_stitch_both_persist(self, tmp_path):
        """Both zones and vias present after sequential operations."""
        from kicad_tools.cli.stitch_cmd import run_blanket_stitch
        from kicad_tools.zones import ZoneGenerator

        pcb = _write_minimal_pcb(tmp_path)

        # Step 1: Add zones
        gen = ZoneGenerator.from_pcb(str(pcb))
        gen.add_zone(net="GND", layer="B.Cu", priority=1)
        gen.save(pcb)

        # Verify zones exist after step 1
        sexp = load_pcb(pcb)
        assert len(sexp.find_all("zone")) == 1

        # Step 2: Run blanket stitch on the same file
        result = run_blanket_stitch(
            pcb_path=pcb,
            net_names=["GND"],
            via_size=0.45,
            drill=0.2,
            spacing=5.0,
        )

        # Verify both zones AND vias still present
        sexp = load_pcb(pcb)
        assert len(sexp.find_all("zone")) >= 1, "Zones lost after stitch"
        if result.vias_added:
            assert len(sexp.find_all("via")) >= 1, "Vias not persisted"
