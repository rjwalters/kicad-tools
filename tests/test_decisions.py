"""Tests for design decision tracking functionality."""

import tempfile
from pathlib import Path

from kicad_tools.explain.decisions import (
    Alternative,
    Decision,
    DecisionStore,
    PlacementRationale,
    RoutingRationale,
    get_decisions_path,
)


class TestAlternative:
    """Tests for Alternative model."""

    def test_create_alternative(self):
        """Test creating an Alternative."""
        alt = Alternative(
            description="Place at (20, 30)",
            rejected_because="Too close to connector",
            metrics={"distance": 5.2},
        )
        assert alt.description == "Place at (20, 30)"
        assert alt.rejected_because == "Too close to connector"
        assert alt.metrics["distance"] == 5.2

    def test_alternative_to_dict(self):
        """Test Alternative serialization."""
        alt = Alternative(
            description="Option A",
            rejected_because="Reason B",
        )
        d = alt.to_dict()
        assert d["description"] == "Option A"
        assert d["rejected_because"] == "Reason B"
        assert d["metrics"] == {}

    def test_alternative_from_dict(self):
        """Test Alternative deserialization."""
        data = {
            "description": "Option C",
            "rejected_because": "Reason D",
            "metrics": {"score": 0.5},
        }
        alt = Alternative.from_dict(data)
        assert alt.description == "Option C"
        assert alt.rejected_because == "Reason D"
        assert alt.metrics["score"] == 0.5


class TestDecision:
    """Tests for Decision model."""

    def test_create_decision(self):
        """Test creating a Decision using factory method."""
        decision = Decision.create(
            action="place",
            components=["U1"],
            position=(50.0, 30.0),
            rationale="Placed MCU near board center",
            decided_by="optimizer",
        )
        assert decision.id.startswith("dec_")
        assert decision.action == "place"
        assert decision.components == ["U1"]
        assert decision.position == (50.0, 30.0)
        assert decision.rationale == "Placed MCU near board center"
        assert decision.decided_by == "optimizer"

    def test_decision_with_alternatives(self):
        """Test Decision with alternatives considered."""
        alt1 = Alternative("Position A", "Too close to edge")
        alt2 = Alternative("Position B", "USB traces too long")

        decision = Decision.create(
            action="place",
            components=["U1"],
            position=(50.0, 30.0),
            rationale="Optimal position for routing",
            alternatives_considered=[alt1, alt2],
        )
        assert len(decision.alternatives) == 2
        assert decision.alternatives[0].description == "Position A"

    def test_decision_to_dict(self):
        """Test Decision serialization."""
        decision = Decision.create(
            action="route",
            nets=["USB_D+"],
            rationale="Shortest path",
            metrics={"length": 25.5},
        )
        d = decision.to_dict()
        assert d["action"] == "route"
        assert d["nets"] == ["USB_D+"]
        assert d["rationale"] == "Shortest path"
        assert d["metrics"]["length"] == 25.5

    def test_decision_from_dict(self):
        """Test Decision deserialization."""
        data = {
            "id": "dec_12345678",
            "timestamp": "2024-01-15T10:30:00+00:00",
            "action": "move",
            "components": ["R1"],
            "nets": [],
            "position": [45.0, 20.0],
            "rationale": "Moved for better clearance",
            "decided_by": "agent",
            "alternatives": [],
            "constraints_satisfied": ["min_clearance"],
            "constraints_violated": [],
            "parent_decision": None,
            "metrics": {},
        }
        decision = Decision.from_dict(data)
        assert decision.id == "dec_12345678"
        assert decision.action == "move"
        assert decision.components == ["R1"]
        assert decision.position == (45.0, 20.0)
        assert "min_clearance" in decision.constraints_satisfied


class TestDecisionStore:
    """Tests for DecisionStore."""

    def test_record_and_query(self):
        """Test recording and querying decisions."""
        store = DecisionStore()

        d1 = Decision.create(
            action="place",
            components=["U1"],
            rationale="MCU placement",
        )
        d2 = Decision.create(
            action="route",
            nets=["USB_D+"],
            rationale="USB routing",
        )
        d3 = Decision.create(
            action="place",
            components=["C1"],
            rationale="Bypass cap placement",
        )

        store.record(d1)
        store.record(d2)
        store.record(d3)

        assert len(store) == 3

        # Query by component
        results = store.query(component="U1")
        assert len(results) == 1
        assert results[0].components == ["U1"]

        # Query by action
        results = store.query(action="place")
        assert len(results) == 2

        # Query by net
        results = store.query(net="USB_D+")
        assert len(results) == 1

    def test_get_by_id(self):
        """Test getting a decision by ID."""
        store = DecisionStore()
        decision = Decision.create(action="place", components=["U1"])
        store.record(decision)

        retrieved = store.get(decision.id)
        assert retrieved is not None
        assert retrieved.id == decision.id

        # Non-existent ID
        assert store.get("nonexistent") is None

    def test_save_and_load(self):
        """Test saving and loading decisions."""
        store = DecisionStore()
        d1 = Decision.create(
            action="place",
            components=["U1"],
            position=(50.0, 30.0),
            rationale="Test placement",
        )
        d2 = Decision.create(
            action="route",
            nets=["GND"],
            rationale="Ground routing",
        )
        store.record(d1)
        store.record(d2)

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "decisions.json"
            store.save(path)

            assert path.exists()

            # Load into new store
            loaded_store = DecisionStore.load(path)
            assert len(loaded_store) == 2

            # Check data integrity
            loaded_d1 = loaded_store.get(d1.id)
            assert loaded_d1 is not None
            assert loaded_d1.components == ["U1"]
            assert loaded_d1.position == (50.0, 30.0)

    def test_decision_chain(self):
        """Test decision chain tracking."""
        store = DecisionStore()

        # Create parent decision
        parent = Decision.create(
            action="place",
            components=["U1"],
            rationale="Initial placement",
        )
        store.record(parent)

        # Create child decision
        child = Decision.create(
            action="move",
            components=["U1"],
            rationale="Adjusted placement",
            parent_decision=parent.id,
        )
        store.record(child)

        # Get chain
        chain = store.get_chain(child.id)
        assert len(chain) == 2
        assert chain[0].id == parent.id
        assert chain[1].id == child.id

        # Get children
        children = store.get_children(parent.id)
        assert len(children) == 1
        assert children[0].id == child.id

    def test_clear(self):
        """Test clearing the store."""
        store = DecisionStore()
        store.record(Decision.create(action="place", components=["U1"]))
        store.record(Decision.create(action="route", nets=["VCC"]))
        assert len(store) == 2

        store.clear()
        assert len(store) == 0

    def test_load_nonexistent_file(self):
        """Test loading from a non-existent file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "nonexistent.json"
            store = DecisionStore.load(path)
            assert len(store) == 0


class TestPlacementRationale:
    """Tests for PlacementRationale model."""

    def test_create_placement_rationale(self):
        """Test creating a PlacementRationale."""
        rationale = PlacementRationale(
            component="U1",
            position=(50.0, 30.0),
            rationale="Placed near center for balanced routing",
            decided_by="optimizer",
            timestamp="2024-01-15T10:30:00+00:00",
        )
        assert rationale.component == "U1"
        assert rationale.position == (50.0, 30.0)
        assert rationale.rationale == "Placed near center for balanced routing"

    def test_placement_rationale_to_dict(self):
        """Test PlacementRationale serialization."""
        rationale = PlacementRationale(
            component="R1",
            position=(20.0, 15.0),
            rationale="Near power IC",
            decided_by="agent",
            timestamp="2024-01-15T10:30:00+00:00",
        )
        d = rationale.to_dict()
        assert d["component"] == "R1"
        assert d["position"] == [20.0, 15.0]
        assert d["rationale"] == "Near power IC"


class TestRoutingRationale:
    """Tests for RoutingRationale model."""

    def test_create_routing_rationale(self):
        """Test creating a RoutingRationale."""
        rationale = RoutingRationale(
            net="USB_D+",
            rationale="Shortest path avoiding high-frequency signals",
            decided_by="autorouter",
            metrics={"length": 25.5, "vias": 2},
        )
        assert rationale.net == "USB_D+"
        assert rationale.rationale == "Shortest path avoiding high-frequency signals"
        assert rationale.metrics["length"] == 25.5

    def test_routing_rationale_to_dict(self):
        """Test RoutingRationale serialization."""
        rationale = RoutingRationale(
            net="GND",
            rationale="Ground plane connection",
            decided_by="autorouter",
        )
        d = rationale.to_dict()
        assert d["net"] == "GND"
        assert d["rationale"] == "Ground plane connection"


class TestGetDecisionsPath:
    """Tests for get_decisions_path helper."""

    def test_decisions_path(self):
        """Test getting the decisions path for a PCB file."""
        pcb_path = Path("/project/board.kicad_pcb")
        decisions_path = get_decisions_path(pcb_path)
        assert decisions_path == Path("/project/board.decisions.json")

    def test_decisions_path_nested(self):
        """Test getting decisions path for nested PCB file."""
        pcb_path = Path("/project/output/final_board.kicad_pcb")
        decisions_path = get_decisions_path(pcb_path)
        assert decisions_path == Path("/project/output/final_board.decisions.json")


class TestDecisionStoreIndexing:
    """Tests for DecisionStore indexing and filtering."""

    def test_multiple_component_decision(self):
        """Test decision affecting multiple components."""
        store = DecisionStore()
        decision = Decision.create(
            action="place",
            components=["U1", "C1", "C2"],
            rationale="MCU with bypass caps",
        )
        store.record(decision)

        # Should be found when querying any of the components
        assert len(store.query(component="U1")) == 1
        assert len(store.query(component="C1")) == 1
        assert len(store.query(component="C2")) == 1
        assert len(store.query(component="R1")) == 0

    def test_multiple_net_decision(self):
        """Test decision affecting multiple nets."""
        store = DecisionStore()
        decision = Decision.create(
            action="route",
            nets=["USB_D+", "USB_D-"],
            rationale="Differential pair routing",
        )
        store.record(decision)

        assert len(store.query(net="USB_D+")) == 1
        assert len(store.query(net="USB_D-")) == 1
        assert len(store.query(net="GND")) == 0

    def test_combined_filters(self):
        """Test combining multiple filters."""
        store = DecisionStore()

        d1 = Decision.create(action="place", components=["U1"])
        d2 = Decision.create(action="move", components=["U1"])
        d3 = Decision.create(action="place", components=["U2"])

        store.record(d1)
        store.record(d2)
        store.record(d3)

        # Component + action
        results = store.query(component="U1", action="place")
        assert len(results) == 1
        assert results[0].id == d1.id

        # Different combination
        results = store.query(component="U1", action="move")
        assert len(results) == 1
        assert results[0].id == d2.id

    def test_all_decisions_sorted(self):
        """Test that all() returns decisions sorted by timestamp."""
        store = DecisionStore()

        # Record decisions (they'll have sequential timestamps)
        d1 = Decision.create(action="place", components=["U1"])
        d2 = Decision.create(action="place", components=["U2"])
        d3 = Decision.create(action="place", components=["U3"])

        store.record(d1)
        store.record(d2)
        store.record(d3)

        all_decisions = store.all()
        assert len(all_decisions) == 3
        # Should be newest first
        assert all_decisions[0].id == d3.id
        assert all_decisions[2].id == d1.id
