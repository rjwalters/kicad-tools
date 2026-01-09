"""
Tests for layout incremental update module.

Tests change detection between old layout snapshot and new design,
and incremental update application preserving unchanged layout.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kicad_tools.layout import (
    AddressRegistry,
    ChangeDetector,
    ChangeType,
    ComponentState,
    IncrementalSnapshot,
    IncrementalUpdater,
    LayoutChange,
    SnapshotBuilder,
    UpdateResult,
    detect_layout_changes,
)


class TestComponentState:
    """Tests for ComponentState dataclass."""

    def test_create_component_state(self):
        """Basic component state creation."""
        state = ComponentState(
            reference="U1",
            address="power.U1",
            position=(50.0, 30.0),
            rotation=90.0,
            layer="F.Cu",
            footprint="Package_SO:SOIC-8",
            uuid="test-uuid-123",
        )
        assert state.reference == "U1"
        assert state.address == "power.U1"
        assert state.position == (50.0, 30.0)
        assert state.rotation == 90.0
        assert state.layer == "F.Cu"

    def test_position_tuple(self):
        """Position tuple includes rotation."""
        state = ComponentState(
            reference="C1",
            address="C1",
            position=(100.0, 200.0),
            rotation=45.0,
            layer="F.Cu",
        )
        assert state.position_tuple == (100.0, 200.0, 45.0)


class TestLayoutChange:
    """Tests for LayoutChange dataclass."""

    def test_create_added_change(self):
        """Change for added component."""
        change = LayoutChange(
            change_type=ChangeType.ADDED,
            component_address="power.C5",
        )
        assert change.is_added
        assert not change.is_removed
        assert not change.is_modified
        assert not change.is_unchanged

    def test_create_removed_change(self):
        """Change for removed component."""
        old_state = ComponentState(
            reference="R1",
            address="R1",
            position=(10.0, 20.0),
            rotation=0.0,
            layer="F.Cu",
        )
        change = LayoutChange(
            change_type=ChangeType.REMOVED,
            component_address="R1",
            old_state=old_state,
            affected_nets=["VCC", "GND"],
        )
        assert change.is_removed
        assert change.old_state is not None
        assert change.new_state is None
        assert "VCC" in change.affected_nets

    def test_create_modified_change(self):
        """Change for modified component."""
        old_state = ComponentState(
            reference="U1",
            address="U1",
            position=(50.0, 50.0),
            rotation=0.0,
            layer="F.Cu",
        )
        change = LayoutChange(
            change_type=ChangeType.MODIFIED,
            component_address="U1",
            old_state=old_state,
            affected_nets=["SDA", "SCL"],
        )
        assert change.is_modified
        assert len(change.affected_nets) == 2

    def test_create_unchanged_change(self):
        """Change for unchanged component."""
        state = ComponentState(
            reference="C1",
            address="C1",
            position=(25.0, 35.0),
            rotation=90.0,
            layer="F.Cu",
        )
        change = LayoutChange(
            change_type=ChangeType.UNCHANGED,
            component_address="C1",
            old_state=state,
            new_state=state,
        )
        assert change.is_unchanged
        assert change.old_state == change.new_state

    def test_string_change_type_conversion(self):
        """String change type should convert to enum."""
        change = LayoutChange(
            change_type="added",
            component_address="C1",
        )
        assert change.change_type == ChangeType.ADDED


class TestIncrementalSnapshot:
    """Tests for IncrementalSnapshot dataclass."""

    def test_create_empty_snapshot(self):
        """Empty snapshot has no components."""
        snapshot = IncrementalSnapshot()
        assert snapshot.component_count == 0
        assert len(snapshot.addresses()) == 0

    def test_create_snapshot_with_components(self):
        """Snapshot with multiple components."""
        states = {
            "C1": ComponentState(
                reference="C1",
                address="C1",
                position=(10.0, 20.0),
                rotation=0.0,
                layer="F.Cu",
            ),
            "R1": ComponentState(
                reference="R1",
                address="R1",
                position=(30.0, 40.0),
                rotation=90.0,
                layer="F.Cu",
            ),
        }
        nets = {
            "C1": ["VCC", "GND"],
            "R1": ["VCC", "OUT"],
        }
        snapshot = IncrementalSnapshot(
            component_states=states,
            net_connections=nets,
        )
        assert snapshot.component_count == 2
        assert "C1" in snapshot.addresses()
        assert "R1" in snapshot.addresses()

    def test_get_state(self):
        """Get component state by address."""
        state = ComponentState(
            reference="U1",
            address="power.U1",
            position=(50.0, 60.0),
            rotation=180.0,
            layer="F.Cu",
        )
        snapshot = IncrementalSnapshot(
            component_states={"power.U1": state},
        )
        retrieved = snapshot.get_state("power.U1")
        assert retrieved is not None
        assert retrieved.reference == "U1"
        assert retrieved.position == (50.0, 60.0)

        # Non-existent address
        assert snapshot.get_state("nonexistent") is None

    def test_get_nets(self):
        """Get nets connected to a component."""
        snapshot = IncrementalSnapshot(
            component_states={
                "C1": ComponentState(
                    reference="C1",
                    address="C1",
                    position=(0, 0),
                    rotation=0,
                    layer="F.Cu",
                ),
            },
            net_connections={"C1": ["VCC", "GND"]},
        )
        nets = snapshot.get_nets("C1")
        assert "VCC" in nets
        assert "GND" in nets

        # Non-existent address returns empty list
        assert snapshot.get_nets("nonexistent") == []

    def test_timestamp_auto_set(self):
        """Timestamp should be auto-set if not provided."""
        snapshot = IncrementalSnapshot()
        assert snapshot.created_at != ""
        assert "T" in snapshot.created_at  # ISO format has T separator

    def test_to_dict_and_from_dict(self):
        """Serialization round-trip."""
        state = ComponentState(
            reference="U1",
            address="power.U1",
            position=(50.0, 60.0),
            rotation=90.0,
            layer="B.Cu",
            footprint="Package_SO:SOIC-8",
            uuid="test-uuid",
        )
        original = IncrementalSnapshot(
            component_states={"power.U1": state},
            net_connections={"power.U1": ["VCC", "GND"]},
            created_at="2024-01-01T00:00:00+00:00",
        )

        # Serialize
        data = original.to_dict()
        assert isinstance(data, dict)
        assert "component_states" in data
        assert "net_connections" in data
        assert "created_at" in data

        # Deserialize
        restored = IncrementalSnapshot.from_dict(data)
        assert restored.component_count == 1
        assert restored.created_at == original.created_at

        restored_state = restored.get_state("power.U1")
        assert restored_state is not None
        assert restored_state.reference == "U1"
        assert restored_state.position == (50.0, 60.0)
        assert restored_state.rotation == 90.0
        assert restored_state.layer == "B.Cu"

    def test_json_serializable(self):
        """Snapshot should be JSON serializable."""
        state = ComponentState(
            reference="C1",
            address="C1",
            position=(10.0, 20.0),
            rotation=0.0,
            layer="F.Cu",
        )
        snapshot = IncrementalSnapshot(
            component_states={"C1": state},
            net_connections={"C1": ["VCC"]},
        )

        # Should not raise
        json_str = json.dumps(snapshot.to_dict())
        assert isinstance(json_str, str)

        # Should round-trip
        restored = IncrementalSnapshot.from_dict(json.loads(json_str))
        assert restored.component_count == 1


class TestUpdateResult:
    """Tests for UpdateResult dataclass."""

    def test_empty_result(self):
        """Empty result has no changes."""
        result = UpdateResult()
        assert result.total_changes == 0
        assert result.preserved_components == 0
        assert not result.has_errors

    def test_result_with_changes(self):
        """Result with various changes."""
        result = UpdateResult(
            added_components=["C5", "C6"],
            removed_components=["R10"],
            updated_components=["U1"],
            preserved_components=15,
            affected_nets=["VCC", "GND", "SDA"],
        )
        assert result.total_changes == 4  # 2 + 1 + 1
        assert result.preserved_components == 15
        assert len(result.affected_nets) == 3

    def test_result_with_errors(self):
        """Result with errors."""
        result = UpdateResult(
            errors=["Failed to place C1", "Missing footprint for U2"],
        )
        assert result.has_errors
        assert len(result.errors) == 2

    def test_summary(self):
        """Summary dictionary."""
        result = UpdateResult(
            added_components=["C1", "C2"],
            removed_components=["R1"],
            updated_components=["U1"],
            preserved_components=10,
            affected_nets=["NET1", "NET2"],
            errors=["Error 1"],
        )
        summary = result.summary()
        assert summary["added"] == 2
        assert summary["removed"] == 1
        assert summary["updated"] == 1
        assert summary["preserved"] == 10
        assert summary["total_changes"] == 4
        assert summary["affected_nets"] == 2
        assert summary["errors"] == 1


class TestChangeDetector:
    """Tests for ChangeDetector class."""

    def test_detect_removed_components(self, tmp_path: Path):
        """Detect components that were removed."""
        # Old snapshot has C1, C2
        old_snapshot = IncrementalSnapshot(
            component_states={
                "C1": ComponentState(
                    reference="C1",
                    address="C1",
                    position=(10.0, 20.0),
                    rotation=0.0,
                    layer="F.Cu",
                ),
                "C2": ComponentState(
                    reference="C2",
                    address="C2",
                    position=(30.0, 40.0),
                    rotation=0.0,
                    layer="F.Cu",
                ),
            },
            net_connections={"C1": ["VCC", "GND"], "C2": ["VCC", "OUT"]},
        )

        # New schematic only has C1 (C2 was removed)
        sch = tmp_path / "test.kicad_sch"
        sch.write_text("""(kicad_sch
    (version 20231120)
    (generator "test")
    (generator_version "8.0")
    (uuid "root-uuid")
    (paper "A4")
    (lib_symbols
        (symbol "Device:C"
            (symbol "C_1_1"
                (pin passive line (at 0 3.81 270) (length 2.794) (name "~") (number "1"))
            )
        )
    )
    (symbol
        (lib_id "Device:C")
        (at 50 50 0)
        (unit 1)
        (uuid "c1-uuid")
        (property "Reference" "C1" (at 54 48 0))
        (property "Value" "100nF" (at 54 52 0))
    )
)
""")

        registry = AddressRegistry(sch)
        detector = ChangeDetector(old_snapshot, registry)
        changes = detector.detect_changes()

        # Should detect C2 as removed
        removed = [c for c in changes if c.is_removed]
        assert len(removed) == 1
        assert removed[0].component_address == "C2"
        assert "VCC" in removed[0].affected_nets

    def test_detect_added_components(self, tmp_path: Path):
        """Detect components that were added."""
        # Old snapshot has C1 only
        old_snapshot = IncrementalSnapshot(
            component_states={
                "C1": ComponentState(
                    reference="C1",
                    address="C1",
                    position=(10.0, 20.0),
                    rotation=0.0,
                    layer="F.Cu",
                ),
            },
        )

        # New schematic has C1 and C2
        sch = tmp_path / "test.kicad_sch"
        sch.write_text("""(kicad_sch
    (version 20231120)
    (generator "test")
    (generator_version "8.0")
    (uuid "root-uuid")
    (paper "A4")
    (lib_symbols
        (symbol "Device:C"
            (symbol "C_1_1"
                (pin passive line (at 0 3.81 270) (length 2.794) (name "~") (number "1"))
            )
        )
    )
    (symbol
        (lib_id "Device:C")
        (at 50 50 0)
        (unit 1)
        (uuid "c1-uuid")
        (property "Reference" "C1" (at 54 48 0))
        (property "Value" "100nF" (at 54 52 0))
    )
    (symbol
        (lib_id "Device:C")
        (at 70 50 0)
        (unit 1)
        (uuid "c2-uuid")
        (property "Reference" "C2" (at 74 48 0))
        (property "Value" "10uF" (at 74 52 0))
    )
)
""")

        registry = AddressRegistry(sch)
        detector = ChangeDetector(old_snapshot, registry)
        changes = detector.detect_changes()

        # Should detect C2 as added
        added = [c for c in changes if c.is_added]
        assert len(added) == 1
        assert added[0].component_address == "C2"

    def test_detect_unchanged_components(self, tmp_path: Path):
        """Components present in both should be marked unchanged."""
        # Old snapshot has C1
        old_snapshot = IncrementalSnapshot(
            component_states={
                "C1": ComponentState(
                    reference="C1",
                    address="C1",
                    position=(10.0, 20.0),
                    rotation=0.0,
                    layer="F.Cu",
                ),
            },
        )

        # New schematic also has C1
        sch = tmp_path / "test.kicad_sch"
        sch.write_text("""(kicad_sch
    (version 20231120)
    (generator "test")
    (generator_version "8.0")
    (uuid "root-uuid")
    (paper "A4")
    (lib_symbols
        (symbol "Device:C"
            (symbol "C_1_1"
                (pin passive line (at 0 3.81 270) (length 2.794) (name "~") (number "1"))
            )
        )
    )
    (symbol
        (lib_id "Device:C")
        (at 50 50 0)
        (unit 1)
        (uuid "c1-uuid")
        (property "Reference" "C1" (at 54 48 0))
        (property "Value" "100nF" (at 54 52 0))
    )
)
""")

        registry = AddressRegistry(sch)
        detector = ChangeDetector(old_snapshot, registry)
        changes = detector.detect_changes()

        # C1 should be unchanged
        unchanged = [c for c in changes if c.is_unchanged]
        assert len(unchanged) == 1
        assert unchanged[0].component_address == "C1"
        assert unchanged[0].old_state is not None
        assert unchanged[0].old_state.position == (10.0, 20.0)

    def test_get_summary(self, tmp_path: Path):
        """Summary counts changes by type."""
        old_snapshot = IncrementalSnapshot(
            component_states={
                "C1": ComponentState(
                    reference="C1",
                    address="C1",
                    position=(10.0, 20.0),
                    rotation=0.0,
                    layer="F.Cu",
                ),
                "C2": ComponentState(
                    reference="C2",
                    address="C2",
                    position=(30.0, 40.0),
                    rotation=0.0,
                    layer="F.Cu",
                ),
            },
        )

        # New schematic has C1 (unchanged) and C3 (added), C2 is removed
        sch = tmp_path / "test.kicad_sch"
        sch.write_text("""(kicad_sch
    (version 20231120)
    (generator "test")
    (generator_version "8.0")
    (uuid "root-uuid")
    (paper "A4")
    (lib_symbols
        (symbol "Device:C"
            (symbol "C_1_1"
                (pin passive line (at 0 3.81 270) (length 2.794) (name "~") (number "1"))
            )
        )
    )
    (symbol
        (lib_id "Device:C")
        (at 50 50 0)
        (unit 1)
        (uuid "c1-uuid")
        (property "Reference" "C1" (at 54 48 0))
        (property "Value" "100nF" (at 54 52 0))
    )
    (symbol
        (lib_id "Device:C")
        (at 90 50 0)
        (unit 1)
        (uuid "c3-uuid")
        (property "Reference" "C3" (at 94 48 0))
        (property "Value" "1uF" (at 94 52 0))
    )
)
""")

        registry = AddressRegistry(sch)
        detector = ChangeDetector(old_snapshot, registry)
        summary = detector.get_summary()

        assert summary["added"] == 1  # C3
        assert summary["removed"] == 1  # C2
        assert summary["unchanged"] == 1  # C1
        assert summary["total"] == 3


class TestIncrementalUpdater:
    """Tests for IncrementalUpdater class."""

    def test_apply_with_no_changes(self):
        """Apply with all unchanged components."""
        state = ComponentState(
            reference="C1",
            address="C1",
            position=(10.0, 20.0),
            rotation=0.0,
            layer="F.Cu",
        )
        changes = [
            LayoutChange(
                change_type=ChangeType.UNCHANGED,
                component_address="C1",
                old_state=state,
                new_state=state,
            ),
        ]

        updater = IncrementalUpdater()
        result = updater.apply(None, changes)  # type: ignore - PCB not needed for basic test

        assert result.preserved_components == 1
        assert len(result.added_components) == 0
        assert len(result.removed_components) == 0
        assert len(result.updated_components) == 0

    def test_apply_with_added_components(self):
        """Apply with added components."""
        changes = [
            LayoutChange(
                change_type=ChangeType.ADDED,
                component_address="C5",
            ),
            LayoutChange(
                change_type=ChangeType.ADDED,
                component_address="C6",
            ),
        ]

        updater = IncrementalUpdater()
        result = updater.apply(None, changes)  # type: ignore

        assert len(result.added_components) == 2
        assert "C5" in result.added_components
        assert "C6" in result.added_components
        assert result.preserved_components == 0

    def test_apply_with_removed_components(self):
        """Apply with removed components."""
        old_state = ComponentState(
            reference="R1",
            address="R1",
            position=(10.0, 20.0),
            rotation=0.0,
            layer="F.Cu",
        )
        changes = [
            LayoutChange(
                change_type=ChangeType.REMOVED,
                component_address="R1",
                old_state=old_state,
                affected_nets=["VCC", "OUT"],
            ),
        ]

        updater = IncrementalUpdater()
        result = updater.apply(None, changes)  # type: ignore

        assert len(result.removed_components) == 1
        assert "R1" in result.removed_components
        assert "VCC" in result.affected_nets
        assert "OUT" in result.affected_nets

    def test_apply_with_modified_preservable(self):
        """Modified component with preservable position."""
        old_state = ComponentState(
            reference="U1",
            address="U1",
            position=(50.0, 60.0),
            rotation=90.0,
            layer="F.Cu",
        )
        changes = [
            LayoutChange(
                change_type=ChangeType.MODIFIED,
                component_address="U1",
                old_state=old_state,
                affected_nets=["SDA", "SCL"],
            ),
        ]

        updater = IncrementalUpdater()
        result = updater.apply(None, changes)  # type: ignore

        # Modified with preservable position should be preserved
        assert result.preserved_components == 1
        assert len(result.updated_components) == 0

    def test_apply_mixed_changes(self):
        """Apply with mix of all change types."""
        changes = [
            LayoutChange(
                change_type=ChangeType.UNCHANGED,
                component_address="C1",
                old_state=ComponentState(
                    reference="C1",
                    address="C1",
                    position=(10.0, 20.0),
                    rotation=0.0,
                    layer="F.Cu",
                ),
            ),
            LayoutChange(
                change_type=ChangeType.ADDED,
                component_address="C5",
            ),
            LayoutChange(
                change_type=ChangeType.REMOVED,
                component_address="R1",
                old_state=ComponentState(
                    reference="R1",
                    address="R1",
                    position=(30.0, 40.0),
                    rotation=0.0,
                    layer="F.Cu",
                ),
                affected_nets=["NET1"],
            ),
            LayoutChange(
                change_type=ChangeType.MODIFIED,
                component_address="U1",
                old_state=ComponentState(
                    reference="U1",
                    address="U1",
                    position=(50.0, 60.0),
                    rotation=0.0,
                    layer="F.Cu",
                ),
                affected_nets=["NET2"],
            ),
        ]

        updater = IncrementalUpdater()
        result = updater.apply(None, changes)  # type: ignore

        assert result.preserved_components == 2  # C1 (unchanged) + U1 (modified but preserved)
        assert len(result.added_components) == 1  # C5
        assert len(result.removed_components) == 1  # R1
        assert "NET1" in result.affected_nets


class TestConvenienceFunctions:
    """Tests for module-level convenience functions."""

    def test_detect_layout_changes(self, tmp_path: Path):
        """detect_layout_changes convenience function."""
        old_snapshot = IncrementalSnapshot(
            component_states={
                "C1": ComponentState(
                    reference="C1",
                    address="C1",
                    position=(10.0, 20.0),
                    rotation=0.0,
                    layer="F.Cu",
                ),
            },
        )

        sch = tmp_path / "test.kicad_sch"
        sch.write_text("""(kicad_sch
    (version 20231120)
    (generator "test")
    (generator_version "8.0")
    (uuid "root-uuid")
    (paper "A4")
    (lib_symbols
        (symbol "Device:C"
            (symbol "C_1_1"
                (pin passive line (at 0 3.81 270) (length 2.794) (name "~") (number "1"))
            )
        )
    )
    (symbol
        (lib_id "Device:C")
        (at 50 50 0)
        (unit 1)
        (uuid "c1-uuid")
        (property "Reference" "C1" (at 54 48 0))
        (property "Value" "100nF" (at 54 52 0))
    )
)
""")

        registry = AddressRegistry(sch)
        changes = detect_layout_changes(old_snapshot, registry)

        assert len(changes) == 1
        assert changes[0].component_address == "C1"
        assert changes[0].is_unchanged


class TestSnapshotBuilder:
    """Tests for SnapshotBuilder class."""

    def test_build_from_pcb(self, simple_pcb: Path):
        """Build snapshot from PCB file."""
        from kicad_tools.schema.pcb import PCB

        pcb = PCB.load(str(simple_pcb))
        builder = SnapshotBuilder()
        snapshot = builder.build(pcb)

        # Should have captured footprints
        assert snapshot.component_count > 0

        # Check first component has valid state
        for addr in snapshot.addresses():
            state = snapshot.get_state(addr)
            assert state is not None
            assert state.reference != ""
            break


# Fixtures


@pytest.fixture
def simple_pcb(tmp_path: Path) -> Path:
    """Create a simple PCB file for testing."""
    pcb_path = tmp_path / "test.kicad_pcb"
    pcb_path.write_text("""(kicad_pcb
    (version 20231120)
    (generator "test")
    (generator_version "8.0")
    (general
        (thickness 1.6)
        (legacy_teardrops no)
    )
    (paper "A4")
    (layers
        (0 "F.Cu" signal)
        (31 "B.Cu" signal)
    )
    (net 0 "")
    (net 1 "VCC")
    (net 2 "GND")
    (footprint "Capacitor_SMD:C_0402_1005Metric"
        (layer "F.Cu")
        (uuid "fp-c1-uuid")
        (at 100 50 0)
        (property "Reference" "C1" (at 0 -2 0) (layer "F.SilkS"))
        (property "Value" "100nF" (at 0 2 0) (layer "F.Fab"))
        (pad "1" smd roundrect (at -0.5 0) (size 0.5 0.6) (layers "F.Cu" "F.Paste" "F.Mask")
            (net 1 "VCC") (uuid "pad-c1-1"))
        (pad "2" smd roundrect (at 0.5 0) (size 0.5 0.6) (layers "F.Cu" "F.Paste" "F.Mask")
            (net 2 "GND") (uuid "pad-c1-2"))
    )
    (footprint "Resistor_SMD:R_0402_1005Metric"
        (layer "F.Cu")
        (uuid "fp-r1-uuid")
        (at 110 50 90)
        (property "Reference" "R1" (at 0 -2 90) (layer "F.SilkS"))
        (property "Value" "10k" (at 0 2 90) (layer "F.Fab"))
        (pad "1" smd roundrect (at 0 -0.5) (size 0.5 0.6) (layers "F.Cu" "F.Paste" "F.Mask")
            (net 1 "VCC") (uuid "pad-r1-1"))
        (pad "2" smd roundrect (at 0 0.5) (size 0.5 0.6) (layers "F.Cu" "F.Paste" "F.Mask")
            (net 2 "GND") (uuid "pad-r1-2"))
    )
)
""")
    return pcb_path
