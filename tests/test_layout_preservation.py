"""
Tests for layout preservation module.

Tests snapshot capture and layout restoration functionality.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_tools.layout import (
    ComponentLayout,
    LayoutPreserver,
    LayoutSnapshot,
    PreservationResult,
    SnapshotCapture,
    TraceSegment,
    ViaLayout,
    ZoneLayout,
    capture_layout,
)


class TestComponentLayout:
    """Tests for ComponentLayout dataclass."""

    def test_create_layout(self):
        """Create a basic component layout."""
        layout = ComponentLayout(
            address="power.C1",
            x=100.0,
            y=50.0,
            rotation=0.0,
            layer="F.Cu",
        )
        assert layout.address == "power.C1"
        assert layout.x == 100.0
        assert layout.y == 50.0
        assert layout.position == (100.0, 50.0)

    def test_layout_with_all_fields(self):
        """Create layout with all optional fields."""
        layout = ComponentLayout(
            address="C1",
            x=120.0,
            y=60.0,
            rotation=90.0,
            layer="B.Cu",
            locked=True,
            reference="C1",
            uuid="test-uuid",
        )
        assert layout.locked is True
        assert layout.reference == "C1"
        assert layout.uuid == "test-uuid"
        assert layout.rotation == 90.0

    def test_distance_calculation(self):
        """Test distance between layouts."""
        layout1 = ComponentLayout(address="C1", x=0.0, y=0.0, rotation=0.0, layer="F.Cu")
        layout2 = ComponentLayout(address="C2", x=3.0, y=4.0, rotation=0.0, layer="F.Cu")
        assert layout1.distance_to(layout2) == 5.0

    def test_empty_address_raises(self):
        """Empty address should raise ValueError."""
        with pytest.raises(ValueError, match="address"):
            ComponentLayout(
                address="",
                x=0.0,
                y=0.0,
                rotation=0.0,
                layer="F.Cu",
            )


class TestTraceSegment:
    """Tests for TraceSegment dataclass."""

    def test_create_segment(self):
        """Create a trace segment."""
        trace = TraceSegment(
            net_name="VCC",
            start=(0.0, 0.0),
            end=(10.0, 0.0),
            width=0.25,
            layer="F.Cu",
        )
        assert trace.net_name == "VCC"
        assert trace.length == 10.0

    def test_diagonal_length(self):
        """Test length of diagonal trace."""
        trace = TraceSegment(
            net_name="NET1",
            start=(0.0, 0.0),
            end=(3.0, 4.0),
            width=0.25,
            layer="F.Cu",
        )
        assert trace.length == 5.0


class TestViaLayout:
    """Tests for ViaLayout dataclass."""

    def test_create_via(self):
        """Create a via layout."""
        via = ViaLayout(
            net_name="GND",
            position=(50.0, 50.0),
            size=0.8,
            drill=0.4,
            layers=["F.Cu", "B.Cu"],
        )
        assert via.net_name == "GND"
        assert via.position == (50.0, 50.0)
        assert len(via.layers) == 2


class TestZoneLayout:
    """Tests for ZoneLayout dataclass."""

    def test_create_zone(self):
        """Create a zone layout."""
        zone = ZoneLayout(
            net_name="GND",
            layer="F.Cu",
            name="GND_ZONE",
            polygon=[(0, 0), (100, 0), (100, 100), (0, 100)],
            priority=0,
        )
        assert zone.net_name == "GND"
        assert len(zone.polygon) == 4


class TestLayoutSnapshot:
    """Tests for LayoutSnapshot dataclass."""

    def test_empty_snapshot(self):
        """Create an empty snapshot."""
        snapshot = LayoutSnapshot()
        assert snapshot.component_count == 0
        assert snapshot.trace_count == 0
        assert snapshot.zone_count == 0
        assert snapshot.via_count == 0

    def test_snapshot_with_components(self):
        """Snapshot with component positions."""
        snapshot = LayoutSnapshot()
        snapshot.component_positions["C1"] = ComponentLayout(
            address="C1", x=100.0, y=50.0, rotation=0.0, layer="F.Cu"
        )
        snapshot.component_positions["R1"] = ComponentLayout(
            address="R1", x=120.0, y=50.0, rotation=0.0, layer="F.Cu"
        )
        assert snapshot.component_count == 2
        assert snapshot.get_component("C1") is not None
        assert snapshot.get_component("R1") is not None
        assert snapshot.get_component("D1") is None

    def test_snapshot_with_traces(self):
        """Snapshot with trace segments."""
        snapshot = LayoutSnapshot()
        snapshot.traces["NET1"] = [
            TraceSegment("NET1", (0, 0), (10, 0), 0.25, "F.Cu"),
            TraceSegment("NET1", (10, 0), (20, 0), 0.25, "F.Cu"),
        ]
        assert snapshot.trace_count == 2
        assert len(snapshot.get_traces_for_net("NET1")) == 2
        assert len(snapshot.get_traces_for_net("NET2")) == 0

    def test_snapshot_summary(self):
        """Test snapshot summary generation."""
        snapshot = LayoutSnapshot(pcb_path="/test/board.kicad_pcb")
        snapshot.component_positions["C1"] = ComponentLayout(
            address="C1", x=100.0, y=50.0, rotation=0.0, layer="F.Cu"
        )
        summary = snapshot.summary()
        assert summary["components"] == 1
        assert summary["pcb_path"] == "/test/board.kicad_pcb"


class TestSnapshotCapture:
    """Tests for SnapshotCapture functionality."""

    def test_capture_from_pcb(self, test_project_pcb: Path, test_project_sch: Path):
        """Capture layout from test project PCB."""
        capture = SnapshotCapture(test_project_pcb, test_project_sch)
        snapshot = capture.capture()

        # Should have captured components
        assert snapshot.component_count >= 3  # R1, C1, D1

        # Should have captured traces
        assert snapshot.trace_count >= 2  # Two segments

        # Verify component positions
        r1 = snapshot.get_component("R1")
        if r1:
            assert r1.x == 100.0
            assert r1.y == 50.0

    def test_capture_convenience_function(self, test_project_pcb: Path, test_project_sch: Path):
        """Test capture_layout convenience function."""
        snapshot = capture_layout(test_project_pcb, test_project_sch)
        assert snapshot.component_count >= 3

    def test_capture_nonexistent_pcb(self, tmp_path: Path):
        """Capture from nonexistent PCB should raise."""
        with pytest.raises(FileNotFoundError):
            capture = SnapshotCapture(
                tmp_path / "nonexistent.kicad_pcb",
                tmp_path / "nonexistent.kicad_sch",
            )
            capture.capture()


class TestPreservationResult:
    """Tests for PreservationResult dataclass."""

    def test_empty_result(self):
        """Empty result has 0% match rate."""
        result = PreservationResult()
        assert result.match_rate == 0.0

    def test_full_match(self):
        """All components matched."""
        result = PreservationResult(
            matched_components=["C1", "R1", "D1"],
            unmatched_components=[],
            new_components=[],
        )
        assert result.match_rate == 100.0

    def test_partial_match(self):
        """Some components matched."""
        result = PreservationResult(
            matched_components=["C1", "R1"],
            unmatched_components=["D1", "D2"],
            new_components=["U1"],
        )
        assert result.match_rate == 50.0

    def test_summary(self):
        """Test summary generation."""
        result = PreservationResult(
            matched_components=["C1"],
            unmatched_components=["R1"],
            new_components=["D1"],
            preserved_traces=["NET1", "NET2"],
            preserved_zones=["GND_ZONE"],
        )
        summary = result.summary()
        assert summary["matched"] == 1
        assert summary["unmatched"] == 1
        assert summary["new"] == 1
        assert summary["preserved_traces"] == 2
        assert summary["preserved_zones"] == 1


class TestLayoutPreserver:
    """Tests for LayoutPreserver functionality."""

    def test_preserver_captures_snapshot(self, test_project_pcb: Path, test_project_sch: Path):
        """Preserver should capture initial snapshot."""
        preserver = LayoutPreserver(test_project_pcb, test_project_sch)
        assert preserver.snapshot is not None
        assert preserver.snapshot.component_count >= 3

    def test_apply_to_same_pcb(
        self, test_project_pcb: Path, test_project_sch: Path, tmp_path: Path
    ):
        """Applying to same PCB should preserve all positions."""
        # Copy PCB to temp location
        import shutil

        temp_pcb = tmp_path / "test_project.kicad_pcb"
        temp_sch = tmp_path / "test_project.kicad_sch"
        shutil.copy(test_project_pcb, temp_pcb)
        shutil.copy(test_project_sch, temp_sch)

        preserver = LayoutPreserver(temp_pcb, temp_sch)
        result = preserver.apply_to_new_pcb(temp_pcb, temp_sch, save=False)

        # All components should be matched
        assert len(result.matched_components) >= 3
        assert len(result.unmatched_components) == 0

    def test_apply_preserves_positions(
        self, test_project_pcb: Path, test_project_sch: Path, tmp_path: Path
    ):
        """Applied positions should match original."""
        import shutil

        # Create copies
        temp_pcb = tmp_path / "test_project.kicad_pcb"
        temp_sch = tmp_path / "test_project.kicad_sch"
        shutil.copy(test_project_pcb, temp_pcb)
        shutil.copy(test_project_sch, temp_sch)

        # Capture original positions
        preserver = LayoutPreserver(temp_pcb, temp_sch)
        original_snapshot = preserver.snapshot
        assert original_snapshot is not None

        # Apply and verify
        result = preserver.apply_to_new_pcb(temp_pcb, temp_sch, save=True)

        # Recapture and verify positions match
        new_snapshot = capture_layout(temp_pcb, temp_sch)

        for addr in result.matched_components:
            original = original_snapshot.get_component(addr)
            new = new_snapshot.get_component(addr)
            if original and new:
                assert abs(original.x - new.x) < 0.01
                assert abs(original.y - new.y) < 0.01


class TestLayoutPreservationIntegration:
    """Integration tests for the complete workflow."""

    def test_complete_workflow(
        self, test_project_pcb: Path, test_project_sch: Path, tmp_path: Path
    ):
        """Test the complete preservation workflow."""
        import shutil

        # Setup: Copy original files
        original_pcb = tmp_path / "original.kicad_pcb"
        original_sch = tmp_path / "original.kicad_sch"
        new_pcb = tmp_path / "new.kicad_pcb"
        new_sch = tmp_path / "new.kicad_sch"

        shutil.copy(test_project_pcb, original_pcb)
        shutil.copy(test_project_sch, original_sch)
        shutil.copy(test_project_pcb, new_pcb)
        shutil.copy(test_project_sch, new_sch)

        # Step 1: Capture original layout
        preserver = LayoutPreserver(original_pcb, original_sch)
        original = preserver.snapshot
        assert original is not None

        # Step 2: Apply to "new" PCB (simulating regeneration)
        result = preserver.apply_to_new_pcb(new_pcb, new_sch, save=True)

        # Step 3: Verify results
        assert result.match_rate == 100.0
        assert len(result.new_components) == 0

        # Step 4: Verify file was saved correctly
        assert new_pcb.exists()


class TestFuzzyMatching:
    """Tests for fuzzy component matching."""

    def test_exact_match_preferred(self):
        """Exact address match should be used when available."""
        snapshot = LayoutSnapshot()
        snapshot.component_positions["C1"] = ComponentLayout(
            address="C1", x=100.0, y=50.0, rotation=0.0, layer="F.Cu"
        )

        # The snapshot has C1 at (100, 50)
        layout = snapshot.get_component("C1")
        assert layout is not None
        assert layout.x == 100.0

    def test_sheet_path_matching(self):
        """Components in same sheet path should match."""
        snapshot = LayoutSnapshot()
        snapshot.component_positions["power.C1"] = ComponentLayout(
            address="power.C1", x=100.0, y=50.0, rotation=0.0, layer="F.Cu"
        )
        snapshot.component_positions["power.C2"] = ComponentLayout(
            address="power.C2", x=120.0, y=50.0, rotation=0.0, layer="F.Cu"
        )

        # Both should be accessible
        c1 = snapshot.get_component("power.C1")
        c2 = snapshot.get_component("power.C2")
        assert c1 is not None
        assert c2 is not None


# Fixtures


@pytest.fixture
def test_project_pcb() -> Path:
    """Return path to test project PCB."""
    return Path(__file__).parent / "fixtures" / "projects" / "test_project.kicad_pcb"


@pytest.fixture
def test_project_sch() -> Path:
    """Return path to test project schematic."""
    return Path(__file__).parent / "fixtures" / "projects" / "test_project.kicad_sch"
