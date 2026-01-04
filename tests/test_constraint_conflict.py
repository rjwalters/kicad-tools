"""Tests for constraint conflict detection."""

from kicad_tools.constraints.conflict import (
    ConflictResolution,
    ConflictType,
    ConstraintConflict,
    ConstraintConflictDetector,
)
from kicad_tools.constraints.locks import RegionConstraint
from kicad_tools.optim.constraints import GroupingConstraint, SpatialConstraint
from kicad_tools.optim.keepout import KeepoutType, KeepoutZone


class TestConflictResolution:
    """Tests for ConflictResolution dataclass."""

    def test_create_resolution(self):
        """Test creating a conflict resolution."""
        resolution = ConflictResolution(
            action="shrink_keepout",
            description="Reduce the size of the keepout zone",
            trade_off="Less protected area",
            priority=1,
        )
        assert resolution.action == "shrink_keepout"
        assert resolution.priority == 1

    def test_to_dict(self):
        """Test serialization to dictionary."""
        resolution = ConflictResolution(
            action="merge_keepouts",
            description="Combine zones",
            trade_off="Larger restricted area",
            priority=0,
        )
        d = resolution.to_dict()
        assert d["action"] == "merge_keepouts"
        assert d["description"] == "Combine zones"
        assert d["trade_off"] == "Larger restricted area"
        assert d["priority"] == 0


class TestConstraintConflict:
    """Tests for ConstraintConflict dataclass."""

    def test_create_conflict(self):
        """Test creating a constraint conflict."""
        conflict = ConstraintConflict(
            constraint1_type="keepout",
            constraint1_name="usb_zone",
            constraint2_type="keepout",
            constraint2_name="antenna_zone",
            conflict_type=ConflictType.OVERLAP,
            description="Zones overlap at corner",
            location=(10.0, 20.0),
        )
        assert conflict.constraint1_type == "keepout"
        assert conflict.conflict_type == ConflictType.OVERLAP
        assert conflict.location == (10.0, 20.0)

    def test_to_dict(self):
        """Test serialization to dictionary."""
        conflict = ConstraintConflict(
            constraint1_type="grouping",
            constraint1_name="decoupling_caps",
            constraint2_type="region",
            constraint2_name="analog_domain",
            conflict_type=ConflictType.CONTRADICTION,
            description="Group member in disallowed region",
            location=(5.0, 5.0),
            priority_winner="region",
            resolutions=[
                ConflictResolution(
                    action="move_group",
                    description="Move group to allowed area",
                    trade_off="Longer traces",
                )
            ],
        )
        d = conflict.to_dict()
        assert d["constraint1"]["type"] == "grouping"
        assert d["constraint1"]["name"] == "decoupling_caps"
        assert d["conflict_type"] == "contradiction"
        assert d["priority_winner"] == "region"
        assert len(d["resolutions"]) == 1


class TestConstraintConflictDetector:
    """Tests for ConstraintConflictDetector class."""

    def test_no_conflicts_empty_inputs(self):
        """Test with no constraints returns no conflicts."""
        detector = ConstraintConflictDetector()
        conflicts = detector.detect()
        assert conflicts == []

    def test_keepout_vs_keepout_overlap(self):
        """Test detection of overlapping keepout zones."""
        # Create two overlapping keepout zones
        zone1 = KeepoutZone(
            name="zone1",
            zone_type=KeepoutType.MECHANICAL,
            polygon=[(0, 0), (10, 0), (10, 10), (0, 10)],
        )
        zone2 = KeepoutZone(
            name="zone2",
            zone_type=KeepoutType.THERMAL,
            polygon=[(5, 5), (15, 5), (15, 15), (5, 15)],
        )

        detector = ConstraintConflictDetector()
        conflicts = detector.detect(keepout_zones=[zone1, zone2])

        assert len(conflicts) == 1
        assert conflicts[0].conflict_type == ConflictType.OVERLAP
        assert "zone1" in conflicts[0].constraint1_name or "zone1" in conflicts[0].constraint2_name
        assert "zone2" in conflicts[0].constraint1_name or "zone2" in conflicts[0].constraint2_name
        assert len(conflicts[0].resolutions) > 0

    def test_keepout_vs_keepout_no_overlap(self):
        """Test non-overlapping keepout zones produce no conflict."""
        zone1 = KeepoutZone(
            name="zone1",
            zone_type=KeepoutType.MECHANICAL,
            polygon=[(0, 0), (10, 0), (10, 10), (0, 10)],
        )
        zone2 = KeepoutZone(
            name="zone2",
            zone_type=KeepoutType.THERMAL,
            polygon=[(20, 20), (30, 20), (30, 30), (20, 30)],
        )

        detector = ConstraintConflictDetector()
        conflicts = detector.detect(keepout_zones=[zone1, zone2])

        assert len(conflicts) == 0

    def test_keepout_vs_region_overlap(self):
        """Test detection of keepout vs region overlap."""
        keepout = KeepoutZone(
            name="mounting_hole",
            zone_type=KeepoutType.MECHANICAL,
            polygon=[(5, 5), (15, 5), (15, 15), (5, 15)],
        )
        region = RegionConstraint(
            name="analog_domain",
            bounds={"x_min": 0, "x_max": 20, "y_min": 0, "y_max": 20},
            reason="Analog signal isolation",
        )

        detector = ConstraintConflictDetector()
        conflicts = detector.detect(
            keepout_zones=[keepout],
            region_constraints=[region],
        )

        assert len(conflicts) == 1
        assert conflicts[0].conflict_type == ConflictType.OVERLAP

    def test_grouping_vs_grouping_shared_member(self):
        """Test detection of conflicting grouping constraints with shared members."""
        # Create two groups with a shared member but different anchors
        group1 = GroupingConstraint(
            name="power_section",
            members=["U1", "C1", "C2"],
            constraints=[SpatialConstraint.max_distance(anchor="U1", radius_mm=5.0)],
        )
        group2 = GroupingConstraint(
            name="analog_section",
            members=["U2", "C1", "C3"],  # C1 is shared
            constraints=[SpatialConstraint.max_distance(anchor="U2", radius_mm=5.0)],
        )

        # Without PCB context, we can't determine if anchors are too far apart
        detector = ConstraintConflictDetector()
        conflicts = detector.detect(grouping_constraints=[group1, group2])

        # Should not have conflicts without PCB to check distances
        assert len(conflicts) == 0

    def test_resolution_suggestions(self):
        """Test that resolutions are generated for conflicts."""
        zone1 = KeepoutZone(
            name="zone1",
            zone_type=KeepoutType.MECHANICAL,
            polygon=[(0, 0), (10, 0), (10, 10), (0, 10)],
        )
        zone2 = KeepoutZone(
            name="zone2",
            zone_type=KeepoutType.THERMAL,
            polygon=[(5, 5), (15, 5), (15, 15), (5, 15)],
        )

        detector = ConstraintConflictDetector()
        conflicts = detector.detect(keepout_zones=[zone1, zone2])

        assert len(conflicts) == 1
        resolutions = conflicts[0].resolutions
        assert len(resolutions) >= 2  # Should have multiple resolution options

        # Check resolution actions
        actions = [r.action for r in resolutions]
        assert "shrink_keepout" in actions
        assert any("merge" in a or "remove" in a for a in actions)

    def test_conflict_location(self):
        """Test that conflict location is captured."""
        zone1 = KeepoutZone(
            name="zone1",
            zone_type=KeepoutType.MECHANICAL,
            polygon=[(0, 0), (10, 0), (10, 10), (0, 10)],
        )
        zone2 = KeepoutZone(
            name="zone2",
            zone_type=KeepoutType.THERMAL,
            polygon=[(5, 5), (15, 5), (15, 15), (5, 15)],
        )

        detector = ConstraintConflictDetector()
        conflicts = detector.detect(keepout_zones=[zone1, zone2])

        assert len(conflicts) == 1
        assert conflicts[0].location is not None
        # Location should be in the overlap region
        x, y = conflicts[0].location
        assert 5 <= x <= 10
        assert 5 <= y <= 10


class TestConflictType:
    """Tests for ConflictType enum."""

    def test_conflict_types(self):
        """Test all conflict types are available."""
        assert ConflictType.OVERLAP.value == "overlap"
        assert ConflictType.CONTRADICTION.value == "contradiction"
        assert ConflictType.IMPOSSIBLE.value == "impossible"
