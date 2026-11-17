"""Design decision tracking and persistence.

This module provides the Decision model and DecisionStore for tracking
why design decisions were made during PCB layout.

Example:
    >>> from kicad_tools.explain.decisions import Decision, DecisionStore
    >>>
    >>> # Create a decision store
    >>> store = DecisionStore()
    >>>
    >>> # Record a placement decision
    >>> decision_id = store.record(Decision.create(
    ...     action="place",
    ...     components=["U1"],
    ...     position=(50, 30),
    ...     rationale="Placed MCU near board center for balanced routing",
    ...     alternatives_considered=[
    ...         Alternative("Position (20, 30)", "Too close to connector"),
    ...         Alternative("Position (80, 30)", "USB traces would be too long"),
    ...     ],
    ...     constraints_satisfied=["usb_trace_length < 50mm"],
    ... ))
    >>>
    >>> # Query decisions
    >>> decisions = store.query(component="U1")
    >>> decisions = store.query(action="route", net="USB_D+")
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    pass


def _now_iso() -> str:
    """Get current time as ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


def _generate_id(prefix: str = "dec") -> str:
    """Generate a unique ID with prefix."""
    uid = str(uuid.uuid4())[:8]
    return f"{prefix}_{uid}"


@dataclass
class Alternative:
    """An alternative that was considered but rejected.

    Attributes:
        description: What the alternative was
        rejected_because: Why it was rejected
        metrics: Optional metrics for comparison (e.g., {"trace_length": 52.3})
    """

    description: str
    rejected_because: str
    metrics: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "description": self.description,
            "rejected_because": self.rejected_because,
            "metrics": self.metrics,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Alternative:
        """Create from dictionary."""
        return cls(
            description=data["description"],
            rejected_because=data["rejected_because"],
            metrics=data.get("metrics", {}),
        )


@dataclass
class Decision:
    """A recorded design decision.

    Tracks the rationale behind placement, routing, and other design choices
    to enable later querying and explanation.

    Attributes:
        id: Unique identifier for this decision
        timestamp: When the decision was made (ISO 8601)
        action: Type of action (place, route, move, reroute, delete)
        components: List of component references affected
        nets: List of net names affected
        position: Position tuple (x, y) if applicable
        rationale: Human-readable explanation of why
        decided_by: Who/what made the decision (agent, human, optimizer)
        alternatives: Alternatives that were considered
        constraints_satisfied: Constraints this decision satisfies
        constraints_violated: Constraints this decision violates
        parent_decision: ID of parent decision for decision chains
        metrics: Quantitative metrics (e.g., trace_length, clearance)
    """

    id: str
    timestamp: str
    action: Literal["place", "route", "move", "reroute", "delete"]
    components: list[str] = field(default_factory=list)
    nets: list[str] = field(default_factory=list)
    position: tuple[float, float] | None = None
    rationale: str = ""
    decided_by: Literal["agent", "human", "optimizer", "autorouter"] = "agent"
    alternatives: list[Alternative] = field(default_factory=list)
    constraints_satisfied: list[str] = field(default_factory=list)
    constraints_violated: list[str] = field(default_factory=list)
    parent_decision: str | None = None
    metrics: dict[str, float] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        action: Literal["place", "route", "move", "reroute", "delete"],
        components: list[str] | None = None,
        nets: list[str] | None = None,
        position: tuple[float, float] | None = None,
        rationale: str = "",
        decided_by: Literal["agent", "human", "optimizer", "autorouter"] = "agent",
        alternatives_considered: list[Alternative] | None = None,
        constraints_satisfied: list[str] | None = None,
        constraints_violated: list[str] | None = None,
        parent_decision: str | None = None,
        metrics: dict[str, float] | None = None,
    ) -> Decision:
        """Factory method to create a new Decision with generated ID and timestamp."""
        return cls(
            id=_generate_id(),
            timestamp=_now_iso(),
            action=action,
            components=components or [],
            nets=nets or [],
            position=position,
            rationale=rationale,
            decided_by=decided_by,
            alternatives=alternatives_considered or [],
            constraints_satisfied=constraints_satisfied or [],
            constraints_violated=constraints_violated or [],
            parent_decision=parent_decision,
            metrics=metrics or {},
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "id": self.id,
            "timestamp": self.timestamp,
            "action": self.action,
            "components": self.components,
            "nets": self.nets,
            "position": list(self.position) if self.position else None,
            "rationale": self.rationale,
            "decided_by": self.decided_by,
            "alternatives": [alt.to_dict() for alt in self.alternatives],
            "constraints_satisfied": self.constraints_satisfied,
            "constraints_violated": self.constraints_violated,
            "parent_decision": self.parent_decision,
            "metrics": self.metrics,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Decision:
        """Create from dictionary."""
        position = data.get("position")
        return cls(
            id=data["id"],
            timestamp=data["timestamp"],
            action=data["action"],
            components=data.get("components", []),
            nets=data.get("nets", []),
            position=tuple(position) if position else None,
            rationale=data.get("rationale", ""),
            decided_by=data.get("decided_by", "agent"),
            alternatives=[Alternative.from_dict(alt) for alt in data.get("alternatives", [])],
            constraints_satisfied=data.get("constraints_satisfied", []),
            constraints_violated=data.get("constraints_violated", []),
            parent_decision=data.get("parent_decision"),
            metrics=data.get("metrics", {}),
        )


@dataclass
class PlacementRationale:
    """Rationale for a component placement.

    Returned by explain_placement() to provide a complete explanation
    of why a component is placed where it is.

    Attributes:
        component: Component reference
        position: Current position (x, y)
        rationale: Human-readable explanation
        decided_by: Who/what made the decision
        timestamp: When the decision was made
        alternatives: Alternatives that were considered
        constraints: Constraints this placement satisfies
        decision_id: ID of the original decision
    """

    component: str
    position: tuple[float, float]
    rationale: str
    decided_by: str
    timestamp: str
    alternatives: list[Alternative] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    decision_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "component": self.component,
            "position": list(self.position),
            "rationale": self.rationale,
            "decided_by": self.decided_by,
            "timestamp": self.timestamp,
            "alternatives": [alt.to_dict() for alt in self.alternatives],
            "constraints": self.constraints,
            "decision_id": self.decision_id,
        }


@dataclass
class RoutingRationale:
    """Rationale for a routing decision.

    Returned by explain_route() to provide a complete explanation
    of why a route was chosen.

    Attributes:
        net: Net name
        source: Source pad (component, pin)
        target: Target pad (component, pin)
        rationale: Human-readable explanation
        decided_by: Who/what made the decision
        timestamp: When the decision was made
        alternatives: Alternatives that were considered
        constraints: Constraints this route satisfies
        metrics: Route metrics (length, vias, etc.)
        decision_id: ID of the original decision
    """

    net: str
    source: tuple[str, str] | None = None
    target: tuple[str, str] | None = None
    rationale: str = ""
    decided_by: str = "autorouter"
    timestamp: str = ""
    alternatives: list[Alternative] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    metrics: dict[str, float] = field(default_factory=dict)
    decision_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "net": self.net,
            "source": list(self.source) if self.source else None,
            "target": list(self.target) if self.target else None,
            "rationale": self.rationale,
            "decided_by": self.decided_by,
            "timestamp": self.timestamp,
            "alternatives": [alt.to_dict() for alt in self.alternatives],
            "constraints": self.constraints,
            "metrics": self.metrics,
            "decision_id": self.decision_id,
        }


class DecisionStore:
    """Persistent storage for design decisions.

    Manages a collection of decisions with query and persistence capabilities.
    Decisions are stored in a JSON file alongside the PCB file.

    Example:
        >>> store = DecisionStore()
        >>> decision_id = store.record(Decision.create(
        ...     action="place",
        ...     components=["U1"],
        ...     rationale="MCU placement"
        ... ))
        >>> store.save(Path("board.decisions.json"))
        >>>
        >>> # Later, load and query
        >>> store = DecisionStore.load(Path("board.decisions.json"))
        >>> decisions = store.query(component="U1")
    """

    def __init__(self) -> None:
        """Initialize an empty decision store."""
        self._decisions: list[Decision] = []
        self._index_by_component: dict[str, list[str]] = {}
        self._index_by_net: dict[str, list[str]] = {}
        self._index_by_action: dict[str, list[str]] = {}
        self._index_by_id: dict[str, Decision] = {}

    def record(self, decision: Decision) -> str:
        """Record a decision.

        Args:
            decision: The decision to record

        Returns:
            The decision ID
        """
        self._decisions.append(decision)
        self._index_by_id[decision.id] = decision

        # Index by component
        for comp in decision.components:
            if comp not in self._index_by_component:
                self._index_by_component[comp] = []
            self._index_by_component[comp].append(decision.id)

        # Index by net
        for net in decision.nets:
            if net not in self._index_by_net:
                self._index_by_net[net] = []
            self._index_by_net[net].append(decision.id)

        # Index by action
        if decision.action not in self._index_by_action:
            self._index_by_action[decision.action] = []
        self._index_by_action[decision.action].append(decision.id)

        return decision.id

    def query(
        self,
        component: str | None = None,
        net: str | None = None,
        action: str | None = None,
        since: datetime | None = None,
    ) -> list[Decision]:
        """Query decisions by criteria.

        Args:
            component: Filter by component reference
            net: Filter by net name
            action: Filter by action type
            since: Filter by timestamp (after this time)

        Returns:
            List of matching decisions, sorted by timestamp (newest first)
        """
        # Start with all decisions
        candidates = {d.id for d in self._decisions}

        # Filter by component
        if component is not None:
            component_decisions = set(self._index_by_component.get(component, []))
            candidates &= component_decisions

        # Filter by net
        if net is not None:
            net_decisions = set(self._index_by_net.get(net, []))
            candidates &= net_decisions

        # Filter by action
        if action is not None:
            action_decisions = set(self._index_by_action.get(action, []))
            candidates &= action_decisions

        # Get matching decisions
        results = [self._index_by_id[dec_id] for dec_id in candidates]

        # Filter by timestamp
        if since is not None:
            since_iso = since.isoformat()
            results = [d for d in results if d.timestamp >= since_iso]

        # Sort by timestamp (newest first)
        results.sort(key=lambda d: d.timestamp, reverse=True)

        return results

    def get(self, decision_id: str) -> Decision | None:
        """Get a decision by ID.

        Args:
            decision_id: The decision ID

        Returns:
            The decision, or None if not found
        """
        return self._index_by_id.get(decision_id)

    def get_chain(self, decision_id: str) -> list[Decision]:
        """Get the full decision chain (parent -> child).

        Follows parent_decision links to build the complete chain
        of decisions that led to this one.

        Args:
            decision_id: Starting decision ID

        Returns:
            List of decisions from root to the given decision
        """
        chain: list[Decision] = []
        current = self.get(decision_id)

        while current is not None:
            chain.insert(0, current)
            if current.parent_decision:
                current = self.get(current.parent_decision)
            else:
                break

        return chain

    def get_children(self, decision_id: str) -> list[Decision]:
        """Get all decisions that have this one as a parent.

        Args:
            decision_id: Parent decision ID

        Returns:
            List of child decisions
        """
        return [d for d in self._decisions if d.parent_decision == decision_id]

    def all(self) -> list[Decision]:
        """Get all decisions.

        Returns:
            All decisions, sorted by timestamp (newest first)
        """
        return sorted(self._decisions, key=lambda d: d.timestamp, reverse=True)

    def save(self, path: Path) -> None:
        """Save decisions to a JSON file.

        Args:
            path: Path to the JSON file
        """
        data = {
            "version": "1.0",
            "decisions": [d.to_dict() for d in self._decisions],
        }
        with open(path, "w") as f:
            json.dump(data, f, indent=2)

    @classmethod
    def load(cls, path: Path) -> DecisionStore:
        """Load decisions from a JSON file.

        Args:
            path: Path to the JSON file

        Returns:
            A DecisionStore with the loaded decisions
        """
        store = cls()

        if not path.exists():
            return store

        with open(path) as f:
            data = json.load(f)

        for dec_data in data.get("decisions", []):
            decision = Decision.from_dict(dec_data)
            store.record(decision)

        return store

    def clear(self) -> None:
        """Clear all decisions."""
        self._decisions.clear()
        self._index_by_component.clear()
        self._index_by_net.clear()
        self._index_by_action.clear()
        self._index_by_id.clear()

    def __len__(self) -> int:
        """Return the number of decisions."""
        return len(self._decisions)

    def __iter__(self):
        """Iterate over decisions."""
        return iter(self._decisions)


def get_decisions_path(pcb_path: Path) -> Path:
    """Get the decisions file path for a PCB file.

    Args:
        pcb_path: Path to the PCB file

    Returns:
        Path to the decisions JSON file
    """
    return pcb_path.with_suffix(".decisions.json")
