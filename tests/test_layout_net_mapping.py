"""Tests for layout net mapping functionality."""

from __future__ import annotations

from kicad_tools.layout import NetMapper, NetMapping, RemapResult, remap_traces
from kicad_tools.layout.types import MatchReason, OrphanedSegment, SegmentRemap
from kicad_tools.operations.netlist import Netlist, NetlistNet, NetNode
from kicad_tools.sexp import parse_sexp


class TestNetMapping:
    """Tests for NetMapping dataclass."""

    def test_exact_match(self):
        """Test exact match properties."""
        mapping = NetMapping(
            old_name="GND",
            new_name="GND",
            confidence=1.0,
            match_reason=MatchReason.EXACT,
        )
        assert mapping.is_exact
        assert not mapping.is_removed
        assert not mapping.is_renamed

    def test_removed_net(self):
        """Test removed net properties."""
        mapping = NetMapping(
            old_name="unused_net",
            new_name=None,
            confidence=0.0,
            match_reason=MatchReason.REMOVED,
        )
        assert not mapping.is_exact
        assert mapping.is_removed
        assert not mapping.is_renamed

    def test_renamed_net(self):
        """Test renamed net via connectivity."""
        mapping = NetMapping(
            old_name="Net-U1-Pad5",
            new_name="MCU_TX",
            confidence=0.85,
            match_reason=MatchReason.CONNECTIVITY,
            shared_pins=2,
        )
        assert not mapping.is_exact
        assert not mapping.is_removed
        assert mapping.is_renamed

    def test_string_match_reason_conversion(self):
        """Test that string match_reason is converted to enum."""
        mapping = NetMapping(
            old_name="test",
            new_name="test",
            confidence=1.0,
            match_reason="exact",
        )
        assert mapping.match_reason == MatchReason.EXACT


class TestRemapResult:
    """Tests for RemapResult dataclass."""

    def test_empty_result(self):
        """Test empty result properties."""
        result = RemapResult()
        assert result.remapped_count == 0
        assert result.orphaned_count == 0
        assert result.renamed_nets == []
        assert result.removed_nets == []

    def test_summary(self):
        """Test summary generation."""
        mappings = [
            NetMapping("GND", "GND", 1.0, MatchReason.EXACT),
            NetMapping("VCC", "VCC_3V3", 0.8, MatchReason.CONNECTIVITY),
            NetMapping("old_net", None, 0.0, MatchReason.REMOVED),
        ]
        result = RemapResult(
            remapped_segments=[
                SegmentRemap("uuid1", "VCC", "VCC_3V3", 1, 2),
            ],
            orphaned_segments=[
                OrphanedSegment("uuid2", "old_net", 3, "Net removed"),
            ],
            net_mappings=mappings,
            new_nets=["NEW_NET"],
        )

        summary = result.summary()
        assert summary["remapped_segments"] == 1
        assert summary["orphaned_segments"] == 1
        assert summary["total_mappings"] == 3
        assert summary["exact_matches"] == 1
        assert summary["renamed_nets"] == 1
        assert summary["removed_nets"] == 1
        assert summary["new_nets"] == 1


class TestNetMapper:
    """Tests for NetMapper class."""

    def _create_netlist(self, nets: list[tuple[str, list[tuple[str, str]]]]) -> Netlist:
        """
        Helper to create a netlist.

        Args:
            nets: List of (net_name, [(ref, pin), ...]) tuples.

        Returns:
            Netlist object.
        """
        netlist = Netlist()
        for i, (name, pins) in enumerate(nets):
            net = NetlistNet(code=i, name=name)
            for ref, pin in pins:
                net.nodes.append(NetNode(reference=ref, pin=pin))
            netlist.nets.append(net)
        return netlist

    def test_exact_match(self):
        """Test detection of exact net name matches."""
        old = self._create_netlist(
            [
                ("GND", [("R1", "1"), ("C1", "2")]),
                ("VCC", [("U1", "1")]),
            ]
        )
        new = self._create_netlist(
            [
                ("GND", [("R1", "1"), ("C1", "2")]),
                ("VCC", [("U1", "1")]),
            ]
        )

        mapper = NetMapper(old, new)
        mappings = mapper.compute_mappings()

        assert len(mappings) == 2
        assert all(m.is_exact for m in mappings)
        assert all(m.confidence == 1.0 for m in mappings)

    def test_renamed_net_detection(self):
        """Test detection of renamed nets via connectivity."""
        old = self._create_netlist(
            [
                ("Net-U1-Pad5", [("U1", "5"), ("R1", "1")]),
            ]
        )
        new = self._create_netlist(
            [
                ("MCU_TX", [("U1", "5"), ("R1", "1")]),
            ]
        )

        mapper = NetMapper(old, new)
        mappings = mapper.compute_mappings()

        assert len(mappings) == 1
        m = mappings[0]
        assert m.old_name == "Net-U1-Pad5"
        assert m.new_name == "MCU_TX"
        assert m.is_renamed
        assert m.confidence == 1.0  # Perfect pin overlap
        assert m.shared_pins == 2

    def test_removed_net_detection(self):
        """Test detection of removed nets."""
        old = self._create_netlist(
            [
                ("GND", [("R1", "1")]),
                ("unused_net", [("R2", "1")]),
            ]
        )
        new = self._create_netlist(
            [
                ("GND", [("R1", "1")]),
            ]
        )

        mapper = NetMapper(old, new)
        mappings = mapper.compute_mappings()

        assert len(mappings) == 2
        gnd_mapping = next(m for m in mappings if m.old_name == "GND")
        unused_mapping = next(m for m in mappings if m.old_name == "unused_net")

        assert gnd_mapping.is_exact
        assert unused_mapping.is_removed
        assert unused_mapping.new_name is None

    def test_new_net_detection(self):
        """Test detection of new nets."""
        old = self._create_netlist(
            [
                ("GND", [("R1", "1")]),
            ]
        )
        new = self._create_netlist(
            [
                ("GND", [("R1", "1")]),
                ("VCC", [("U1", "1")]),
            ]
        )

        mapper = NetMapper(old, new)
        new_nets = mapper.get_new_nets()

        assert "VCC" in new_nets
        assert "GND" not in new_nets

    def test_partial_connectivity_match(self):
        """Test connectivity matching with partial overlap."""
        # Old net has 3 pins, new net has 2 of those pins + 1 new
        old = self._create_netlist(
            [
                ("NET1", [("U1", "1"), ("R1", "1"), ("R2", "1")]),
            ]
        )
        new = self._create_netlist(
            [
                ("NET1_RENAMED", [("U1", "1"), ("R1", "1"), ("C1", "1")]),
            ]
        )

        mapper = NetMapper(old, new, min_confidence=0.4)
        mappings = mapper.compute_mappings()

        assert len(mappings) == 1
        m = mappings[0]
        assert m.new_name == "NET1_RENAMED"
        assert m.shared_pins == 2
        # Jaccard: 2 / 4 = 0.5
        assert m.confidence == 0.5

    def test_split_net_handling(self):
        """Test handling of net split (one old -> multiple new)."""
        # Original net with 4 pins
        old = self._create_netlist(
            [
                ("BIG_NET", [("U1", "1"), ("U1", "2"), ("R1", "1"), ("R2", "1")]),
            ]
        )
        # Split into two nets (first two pins and last two pins)
        new = self._create_netlist(
            [
                ("NET_A", [("U1", "1"), ("U1", "2")]),
                ("NET_B", [("R1", "1"), ("R2", "1")]),
            ]
        )

        mapper = NetMapper(old, new)
        mappings = mapper.compute_mappings()

        # Should map to one of them (highest overlap or first found)
        assert len(mappings) == 1
        m = mappings[0]
        assert m.old_name == "BIG_NET"
        assert m.new_name in ["NET_A", "NET_B"]
        assert m.shared_pins == 2

    def test_merged_net_handling(self):
        """Test handling of merged nets (multiple old -> one new)."""
        old = self._create_netlist(
            [
                ("NET_A", [("U1", "1")]),
                ("NET_B", [("U1", "2")]),
            ]
        )
        new = self._create_netlist(
            [
                ("MERGED_NET", [("U1", "1"), ("U1", "2")]),
            ]
        )

        mapper = NetMapper(old, new)
        mappings = mapper.compute_mappings()

        assert len(mappings) == 2
        # First one should get the match, second should be removed (already used)
        first = next(m for m in mappings if m.new_name == "MERGED_NET")
        second = next(m for m in mappings if m.new_name != "MERGED_NET")

        assert first.confidence > 0
        assert second.is_removed  # Couldn't find unused match

    def test_low_confidence_threshold(self):
        """Test that low confidence matches are rejected."""
        old = self._create_netlist(
            [
                ("NET1", [("U1", "1"), ("U1", "2"), ("U1", "3"), ("U1", "4")]),
            ]
        )
        new = self._create_netlist(
            [
                ("NET2", [("U1", "1")]),  # Only 1 of 4 pins match
            ]
        )

        # Default threshold is 0.5, but Jaccard would be 1/4 = 0.25
        mapper = NetMapper(old, new, min_confidence=0.5)
        mappings = mapper.compute_mappings()

        assert len(mappings) == 1
        assert mappings[0].is_removed  # Below threshold

    def test_ambiguous_match(self):
        """Test detection of ambiguous matches."""
        old = self._create_netlist(
            [
                ("NET1", [("U1", "1"), ("U1", "2")]),
            ]
        )
        new = self._create_netlist(
            [
                ("NET_A", [("U1", "1")]),
                ("NET_B", [("U1", "2")]),
            ]
        )

        mapper = NetMapper(old, new, min_confidence=0.3)
        mappings = mapper.compute_mappings()

        assert len(mappings) == 1
        m = mappings[0]
        # Both have same overlap (1 pin), should be marked ambiguous
        # But will still pick one
        assert m.new_name is not None

    def test_get_removed_nets(self):
        """Test get_removed_nets helper."""
        old = self._create_netlist(
            [
                ("GND", [("R1", "1")]),
                ("REMOVED1", [("R2", "1")]),
                ("RENAMED", [("U1", "1")]),
            ]
        )
        new = self._create_netlist(
            [
                ("GND", [("R1", "1")]),
                ("NEW_NAME", [("U1", "1")]),  # Same pins as RENAMED
            ]
        )

        mapper = NetMapper(old, new)
        removed = mapper.get_removed_nets()

        assert "REMOVED1" in removed
        assert "GND" not in removed
        assert "RENAMED" not in removed  # Has connectivity match


class TestRemapTraces:
    """Tests for remap_traces function."""

    # Minimal PCB with segments for testing
    PCB_WITH_SEGMENTS = """(kicad_pcb
      (version 20240108)
      (generator "test")
      (net 0 "")
      (net 1 "OLD_NET")
      (net 2 "GND")
      (segment (start 100 100) (end 110 100) (width 0.2) (layer "F.Cu") (net 1) (uuid "seg-1"))
      (segment (start 110 100) (end 120 100) (width 0.2) (layer "F.Cu") (net 1) (uuid "seg-2"))
      (segment (start 100 110) (end 110 110) (width 0.2) (layer "F.Cu") (net 2) (uuid "seg-3"))
      (via (at 115 100) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1) (uuid "via-1"))
    )"""

    def test_remap_renamed_net(self):
        """Test remapping segments when net is renamed."""
        pcb_doc = parse_sexp(self.PCB_WITH_SEGMENTS)

        mappings = [
            NetMapping("OLD_NET", "NEW_NET", 0.9, MatchReason.CONNECTIVITY),
            NetMapping("GND", "GND", 1.0, MatchReason.EXACT),
        ]

        # Provide the new net ID lookup
        net_id_lookup = {"NEW_NET": 1, "GND": 2}

        result = remap_traces(pcb_doc, mappings, net_id_lookup)

        # OLD_NET segments should be remapped
        assert result.remapped_count == 3  # 2 segments + 1 via
        assert result.orphaned_count == 0

        # Verify the segment UUIDs
        remapped_uuids = {s.segment_uuid for s in result.remapped_segments}
        assert "seg-1" in remapped_uuids
        assert "seg-2" in remapped_uuids
        assert "via-1" in remapped_uuids

    def test_orphan_removed_net_segments(self):
        """Test that segments on removed nets become orphaned."""
        pcb_doc = parse_sexp(self.PCB_WITH_SEGMENTS)

        mappings = [
            NetMapping("OLD_NET", None, 0.0, MatchReason.REMOVED),
            NetMapping("GND", "GND", 1.0, MatchReason.EXACT),
        ]

        result = remap_traces(pcb_doc, mappings)

        # OLD_NET segments should be orphaned
        assert result.orphaned_count == 3  # 2 segments + 1 via
        assert result.remapped_count == 0

        orphaned_uuids = {s.segment_uuid for s in result.orphaned_segments}
        assert "seg-1" in orphaned_uuids
        assert "seg-2" in orphaned_uuids
        assert "via-1" in orphaned_uuids

    def test_exact_match_no_change(self):
        """Test that exact matches don't produce remap entries."""
        pcb_doc = parse_sexp(self.PCB_WITH_SEGMENTS)

        mappings = [
            NetMapping("OLD_NET", "OLD_NET", 1.0, MatchReason.EXACT),
            NetMapping("GND", "GND", 1.0, MatchReason.EXACT),
        ]

        result = remap_traces(pcb_doc, mappings)

        # No remapping needed for exact matches
        assert result.remapped_count == 0
        assert result.orphaned_count == 0

    def test_new_nets_detected(self):
        """Test detection of new nets in result."""
        pcb_doc = parse_sexp(self.PCB_WITH_SEGMENTS)

        mappings = [
            NetMapping("OLD_NET", "OLD_NET", 1.0, MatchReason.EXACT),
        ]

        # PCB has GND which isn't in mappings - it's a "new" net
        result = remap_traces(pcb_doc, mappings)

        assert "GND" in result.new_nets

    def test_missing_new_net_id(self):
        """Test handling when new net ID not found in PCB."""
        pcb_doc = parse_sexp(self.PCB_WITH_SEGMENTS)

        mappings = [
            NetMapping("OLD_NET", "NONEXISTENT_NET", 0.9, MatchReason.CONNECTIVITY),
        ]

        # Empty lookup - new net doesn't exist
        net_id_lookup: dict[str, int] = {}

        result = remap_traces(pcb_doc, mappings, net_id_lookup)

        # Should orphan segments because target net not found
        assert result.orphaned_count == 3
        for orphan in result.orphaned_segments:
            assert "not found" in orphan.reason


class TestIntegration:
    """Integration tests for net mapping workflow."""

    def test_full_workflow(self):
        """Test complete net mapping workflow."""
        # Create old netlist
        old_netlist = Netlist()
        old_netlist.nets = [
            NetlistNet(
                code=1,
                name="GND",
                nodes=[
                    NetNode(reference="R1", pin="1"),
                    NetNode(reference="C1", pin="2"),
                ],
            ),
            NetlistNet(
                code=2,
                name="Net-U1-Pad5",
                nodes=[
                    NetNode(reference="U1", pin="5"),
                    NetNode(reference="R2", pin="1"),
                ],
            ),
            NetlistNet(
                code=3,
                name="UNUSED",
                nodes=[
                    NetNode(reference="R3", pin="1"),
                ],
            ),
        ]

        # Create new netlist with changes
        new_netlist = Netlist()
        new_netlist.nets = [
            NetlistNet(
                code=1,
                name="GND",
                nodes=[
                    NetNode(reference="R1", pin="1"),
                    NetNode(reference="C1", pin="2"),
                ],
            ),
            NetlistNet(
                code=2,
                name="MCU_TX",
                nodes=[  # Renamed
                    NetNode(reference="U1", pin="5"),
                    NetNode(reference="R2", pin="1"),
                ],
            ),
            NetlistNet(
                code=4,
                name="NEW_SIGNAL",
                nodes=[  # New net
                    NetNode(reference="U2", pin="1"),
                ],
            ),
        ]

        # Compute mappings
        mapper = NetMapper(old_netlist, new_netlist)
        mappings = mapper.compute_mappings()

        # Verify mappings
        gnd = next(m for m in mappings if m.old_name == "GND")
        assert gnd.is_exact

        renamed = next(m for m in mappings if m.old_name == "Net-U1-Pad5")
        assert renamed.new_name == "MCU_TX"
        assert renamed.is_renamed

        unused = next(m for m in mappings if m.old_name == "UNUSED")
        assert unused.is_removed

        # Check new nets
        new_nets = mapper.get_new_nets()
        assert "NEW_SIGNAL" in new_nets

        # Create PCB and remap
        pcb_content = """(kicad_pcb
          (version 20240108)
          (net 0 "")
          (net 1 "GND")
          (net 2 "Net-U1-Pad5")
          (net 3 "UNUSED")
          (segment (start 100 100) (end 110 100) (width 0.2) (layer "F.Cu") (net 2) (uuid "seg-tx"))
          (segment (start 100 110) (end 110 110) (width 0.2) (layer "F.Cu") (net 3) (uuid "seg-unused"))
        )"""

        pcb_doc = parse_sexp(pcb_content)
        net_id_lookup = {"GND": 1, "MCU_TX": 2, "NEW_SIGNAL": 4}

        result = remap_traces(pcb_doc, mappings, net_id_lookup)

        # TX segment should be remapped
        assert result.remapped_count == 1
        assert result.remapped_segments[0].old_net_name == "Net-U1-Pad5"
        assert result.remapped_segments[0].new_net_name == "MCU_TX"

        # UNUSED segment should be orphaned
        assert result.orphaned_count == 1
        assert result.orphaned_segments[0].net_name == "UNUSED"
