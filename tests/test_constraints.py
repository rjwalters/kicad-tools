"""
Unit tests for the constraint locking system.

Tests cover:
- LockType enumeration
- ComponentLock creation and methods
- NetRouteLock creation
- RegionConstraint creation and validation
- RelativeConstraint creation and validation
- ConstraintManifest serialization (JSON and YAML)
- ConstraintManager API and validation
"""

import tempfile
from datetime import datetime
from pathlib import Path

import pytest

from kicad_tools.constraints import (
    ComponentLock,
    ConstraintManager,
    ConstraintManifest,
    ConstraintViolation,
    LockType,
    NetRouteLock,
    RegionConstraint,
    RelativeConstraint,
)


class TestLockType:
    """Tests for LockType enumeration."""

    def test_lock_type_values(self):
        """Test LockType enum values."""
        assert LockType.POSITION.value == "position"
        assert LockType.ROTATION.value == "rotation"
        assert LockType.FULL.value == "full"

    def test_lock_type_from_string(self):
        """Test creating LockType from string value."""
        assert LockType("position") == LockType.POSITION
        assert LockType("rotation") == LockType.ROTATION
        assert LockType("full") == LockType.FULL


class TestComponentLock:
    """Tests for ComponentLock dataclass."""

    def test_component_lock_creation(self):
        """Test creating a ComponentLock."""
        lock = ComponentLock(
            ref="FB1",
            lock_type=LockType.POSITION,
            reason="Domain boundary",
            locked_by="domain_analyzer",
        )
        assert lock.ref == "FB1"
        assert lock.lock_type == LockType.POSITION
        assert lock.reason == "Domain boundary"
        assert lock.locked_by == "domain_analyzer"
        assert isinstance(lock.timestamp, datetime)

    def test_component_lock_with_position(self):
        """Test ComponentLock with stored position."""
        lock = ComponentLock(
            ref="U1",
            lock_type=LockType.FULL,
            reason="Critical placement",
            locked_by="placer",
            position=(100.0, 50.0),
            rotation=90.0,
        )
        assert lock.position == (100.0, 50.0)
        assert lock.rotation == 90.0

    def test_locks_position(self):
        """Test locks_position method."""
        assert ComponentLock(
            ref="A", lock_type=LockType.POSITION, reason="", locked_by=""
        ).locks_position()
        assert ComponentLock(
            ref="A", lock_type=LockType.FULL, reason="", locked_by=""
        ).locks_position()
        assert not ComponentLock(
            ref="A", lock_type=LockType.ROTATION, reason="", locked_by=""
        ).locks_position()

    def test_locks_rotation(self):
        """Test locks_rotation method."""
        assert ComponentLock(
            ref="A", lock_type=LockType.ROTATION, reason="", locked_by=""
        ).locks_rotation()
        assert ComponentLock(
            ref="A", lock_type=LockType.FULL, reason="", locked_by=""
        ).locks_rotation()
        assert not ComponentLock(
            ref="A", lock_type=LockType.POSITION, reason="", locked_by=""
        ).locks_rotation()


class TestNetRouteLock:
    """Tests for NetRouteLock dataclass."""

    def test_net_route_lock_creation(self):
        """Test creating a NetRouteLock."""
        lock = NetRouteLock(
            net_name="MCLK_MCU",
            reason="Timing-critical",
            locked_by="clock_router",
        )
        assert lock.net_name == "MCLK_MCU"
        assert lock.reason == "Timing-critical"
        assert lock.trace_geometry == []
        assert lock.via_positions == []

    def test_net_route_lock_with_geometry(self):
        """Test NetRouteLock with trace geometry."""
        lock = NetRouteLock(
            net_name="CLK",
            reason="Length matched",
            locked_by="router",
            trace_geometry=[
                (10.0, 20.0, 30.0, 20.0, "F.Cu", 0.25),
                (30.0, 20.0, 30.0, 40.0, "F.Cu", 0.25),
            ],
            via_positions=[(30.0, 40.0, ("F.Cu", "B.Cu"))],
        )
        assert len(lock.trace_geometry) == 2
        assert len(lock.via_positions) == 1


class TestRegionConstraint:
    """Tests for RegionConstraint dataclass."""

    def test_region_constraint_creation(self):
        """Test creating a RegionConstraint."""
        region = RegionConstraint(
            name="analog_domain",
            bounds={"x_min": 100, "x_max": 150, "y_min": 50, "y_max": 100},
            reason="Analog signal isolation",
            allowed_nets=["GNDA", "+3.3VA"],
        )
        assert region.name == "analog_domain"
        assert region.bounds["x_min"] == 100

    def test_contains_point(self):
        """Test contains_point method."""
        region = RegionConstraint(
            name="test",
            bounds={"x_min": 0, "x_max": 100, "y_min": 0, "y_max": 100},
            reason="Test region",
        )
        assert region.contains_point(50, 50)
        assert region.contains_point(0, 0)
        assert region.contains_point(100, 100)
        assert not region.contains_point(-1, 50)
        assert not region.contains_point(50, 101)

    def test_is_net_allowed(self):
        """Test is_net_allowed method."""
        region = RegionConstraint(
            name="test",
            bounds={},
            reason="",
            allowed_nets=["NET1", "NET2"],
            disallowed_nets=["BAD_NET"],
        )
        assert region.is_net_allowed("NET1")
        assert region.is_net_allowed("NET2")
        assert not region.is_net_allowed("NET3")
        assert not region.is_net_allowed("BAD_NET")

    def test_is_component_allowed(self):
        """Test is_component_allowed method."""
        region = RegionConstraint(
            name="test",
            bounds={},
            reason="",
            allowed_components=["U1", "U2"],
            disallowed_components=["R1"],
        )
        assert region.is_component_allowed("U1")
        assert not region.is_component_allowed("U3")
        assert not region.is_component_allowed("R1")


class TestRelativeConstraint:
    """Tests for RelativeConstraint dataclass."""

    def test_relative_constraint_creation(self):
        """Test creating a RelativeConstraint."""
        constraint = RelativeConstraint(
            ref1="C1",
            relation="near",
            ref2="U1",
            max_distance=3.0,
            reason="Decoupling capacitor",
        )
        assert constraint.ref1 == "C1"
        assert constraint.relation == "near"
        assert constraint.ref2 == "U1"
        assert constraint.max_distance == 3.0

    def test_check_satisfied_near(self):
        """Test check_satisfied for 'near' relation."""
        constraint = RelativeConstraint(
            ref1="C1",
            relation="near",
            ref2="U1",
            max_distance=5.0,
        )

        # Within distance
        satisfied, msg = constraint.check_satisfied((0, 0), (3, 4))  # distance = 5
        assert satisfied
        assert msg == ""

        # Exactly at distance
        satisfied, msg = constraint.check_satisfied((0, 0), (5, 0))
        assert satisfied

        # Beyond distance
        satisfied, msg = constraint.check_satisfied((0, 0), (10, 0))
        assert not satisfied
        assert "must be within 5.0mm" in msg


class TestConstraintManifest:
    """Tests for ConstraintManifest serialization."""

    def test_empty_manifest(self):
        """Test empty manifest."""
        manifest = ConstraintManifest()
        assert manifest.is_empty()
        assert manifest.to_dict() == {}

    def test_manifest_with_component_lock(self):
        """Test manifest with component locks."""
        manifest = ConstraintManifest()
        manifest.component_locks["FB1"] = ComponentLock(
            ref="FB1",
            lock_type=LockType.POSITION,
            reason="Domain boundary",
            locked_by="analyzer",
            position=(100.0, 50.0),
        )

        data = manifest.to_dict()
        assert "components" in data
        assert "FB1" in data["components"]
        assert data["components"]["FB1"]["lock"] == "position"

    def test_manifest_json_roundtrip(self):
        """Test JSON serialization roundtrip."""
        manifest = ConstraintManifest()
        manifest.component_locks["U1"] = ComponentLock(
            ref="U1",
            lock_type=LockType.FULL,
            reason="Test",
            locked_by="test",
            position=(10.0, 20.0),
            rotation=90.0,
        )
        manifest.relative_constraints.append(
            RelativeConstraint(
                ref1="C1",
                relation="near",
                ref2="U1",
                max_distance=3.0,
                reason="Decoupling",
            )
        )

        json_str = manifest.to_json()
        loaded = ConstraintManifest.from_json(json_str)

        assert "U1" in loaded.component_locks
        assert loaded.component_locks["U1"].lock_type == LockType.FULL
        assert len(loaded.relative_constraints) == 1
        assert loaded.relative_constraints[0].max_distance == 3.0

    def test_manifest_file_save_load(self):
        """Test saving and loading manifest from file."""
        manifest = ConstraintManifest()
        manifest.component_locks["R1"] = ComponentLock(
            ref="R1",
            lock_type=LockType.ROTATION,
            reason="Thermal",
            locked_by="thermal_analyzer",
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "test.json"
            manifest.save(path)

            loaded = ConstraintManifest.load(path)
            assert "R1" in loaded.component_locks
            assert loaded.component_locks["R1"].lock_type == LockType.ROTATION


class TestConstraintManager:
    """Tests for ConstraintManager API."""

    def test_empty_manager(self):
        """Test empty constraint manager."""
        cm = ConstraintManager()
        assert cm.is_empty()
        assert cm.locked_components == []
        assert cm.locked_net_routes == []

    def test_lock_component(self):
        """Test locking a component."""
        cm = ConstraintManager()
        lock = cm.lock_component(
            "FB1",
            LockType.POSITION,
            "Domain boundary",
            "analyzer",
            position=(100.0, 50.0),
        )

        assert cm.is_component_locked("FB1")
        assert not cm.is_component_locked("FB2")
        assert lock.ref == "FB1"
        assert lock.position == (100.0, 50.0)

    def test_unlock_component(self):
        """Test unlocking a component."""
        cm = ConstraintManager()
        cm.lock_component("U1", LockType.FULL, "Test", "test")

        assert cm.is_component_locked("U1")
        assert cm.unlock_component("U1")
        assert not cm.is_component_locked("U1")
        assert not cm.unlock_component("U1")  # Already unlocked

    def test_lock_net_route(self):
        """Test locking a net route."""
        cm = ConstraintManager()
        cm.lock_net_route("MCLK", "Timing", "router")

        assert cm.is_net_route_locked("MCLK")
        assert not cm.is_net_route_locked("OTHER")
        assert cm.get_net_route_lock("MCLK") is not None

    def test_define_region(self):
        """Test defining a region constraint."""
        cm = ConstraintManager()
        region = cm.define_region(
            "analog",
            bounds={"x_min": 0, "x_max": 50, "y_min": 0, "y_max": 50},
            reason="Analog isolation",
            allowed_nets=["GNDA"],
        )

        assert "analog" in cm.regions
        assert region.name == "analog"

    def test_add_relative_constraint(self):
        """Test adding relative constraints."""
        cm = ConstraintManager()
        cm.add_relative_constraint("C1", "near", "U1", max_distance=3.0, reason="Decoupling")

        assert len(cm.relative_constraints) == 1
        assert cm.relative_constraints[0].ref1 == "C1"

    def test_remove_relative_constraints(self):
        """Test removing relative constraints."""
        cm = ConstraintManager()
        cm.add_relative_constraint("C1", "near", "U1", max_distance=3.0)
        cm.add_relative_constraint("C2", "near", "U1", max_distance=3.0)
        cm.add_relative_constraint("C3", "near", "U2", max_distance=3.0)

        removed = cm.remove_relative_constraints("U1")
        assert removed == 2
        assert len(cm.relative_constraints) == 1

    def test_summary(self):
        """Test summary method."""
        cm = ConstraintManager()
        cm.lock_component("U1", LockType.FULL, "Test", "test")
        cm.lock_component("U2", LockType.POSITION, "Test", "test")
        cm.lock_net_route("NET1", "Test", "test")
        cm.define_region("R1", {}, "Test")
        cm.add_relative_constraint("C1", "near", "U1", max_distance=3.0)

        summary = cm.summary()
        assert summary["component_locks"] == 2
        assert summary["net_route_locks"] == 1
        assert summary["region_constraints"] == 1
        assert summary["relative_constraints"] == 1

    def test_save_load_roundtrip(self):
        """Test save/load roundtrip."""
        cm = ConstraintManager()
        cm.lock_component("FB1", LockType.POSITION, "Domain", "analyzer")
        cm.lock_net_route("CLK", "Timing", "router")

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "constraints.json"
            cm.save(path)

            loaded = ConstraintManager.load(path)
            assert loaded.is_component_locked("FB1")
            assert loaded.is_net_route_locked("CLK")

    def test_clear(self):
        """Test clearing all constraints."""
        cm = ConstraintManager()
        cm.lock_component("U1", LockType.FULL, "Test", "test")
        cm.lock_net_route("NET1", "Test", "test")

        assert not cm.is_empty()
        cm.clear()
        assert cm.is_empty()


class TestConstraintViolation:
    """Tests for ConstraintViolation."""

    def test_violation_creation(self):
        """Test creating a violation."""
        violation = ConstraintViolation(
            constraint_type="component",
            constraint_name="FB1",
            message="Position changed",
            location=(100.0, 50.0),
            severity="error",
        )
        assert violation.constraint_type == "component"
        assert violation.location == (100.0, 50.0)
        assert violation.severity == "error"

    def test_violation_repr(self):
        """Test violation string representation."""
        violation = ConstraintViolation(
            constraint_type="component",
            constraint_name="U1",
            message="Rotation changed",
            location=(10.0, 20.0),
        )
        repr_str = repr(violation)
        assert "component" in repr_str
        assert "Rotation changed" in repr_str
        assert "10.0" in repr_str


# Optional YAML tests (only run if pyyaml is installed)
import importlib.util

HAS_YAML = importlib.util.find_spec("yaml") is not None


@pytest.mark.skipif(not HAS_YAML, reason="pyyaml not installed")
class TestYAMLSerialization:
    """Tests for YAML serialization (requires pyyaml)."""

    def test_manifest_yaml_roundtrip(self):
        """Test YAML serialization roundtrip."""
        manifest = ConstraintManifest()
        manifest.component_locks["FB1"] = ComponentLock(
            ref="FB1",
            lock_type=LockType.POSITION,
            reason="Domain boundary",
            locked_by="analyzer",
        )
        manifest.region_constraints["analog"] = RegionConstraint(
            name="analog",
            bounds={"x_min": 0, "x_max": 50, "y_min": 0, "y_max": 50},
            reason="Analog isolation",
            allowed_nets=["GNDA", "AUDIO_L"],
        )

        yaml_str = manifest.to_yaml()
        loaded = ConstraintManifest.from_yaml(yaml_str)

        assert "FB1" in loaded.component_locks
        assert "analog" in loaded.region_constraints
        assert loaded.region_constraints["analog"].allowed_nets == ["GNDA", "AUDIO_L"]

    def test_yaml_file_save_load(self):
        """Test saving and loading YAML manifest from file."""
        manifest = ConstraintManifest()
        manifest.component_locks["U1"] = ComponentLock(
            ref="U1",
            lock_type=LockType.FULL,
            reason="Test",
            locked_by="test",
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "test.yaml"
            manifest.save(path)

            loaded = ConstraintManifest.load(path)
            assert "U1" in loaded.component_locks
