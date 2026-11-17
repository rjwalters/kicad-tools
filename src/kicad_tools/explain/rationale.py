"""Rationale query API for design decisions.

This module provides high-level functions for querying and explaining
design decisions recorded in a PCB project.

Example:
    >>> from kicad_tools.explain.rationale import explain_placement, explain_route
    >>> from kicad_tools.schema.pcb import PCB
    >>>
    >>> pcb = PCB.load("board.kicad_pcb")
    >>>
    >>> # Why is this component here?
    >>> rationale = explain_placement("U1", pcb)
    >>> print(rationale.rationale)
    Placed MCU near board center for balanced routing
    >>>
    >>> # Why was this route chosen?
    >>> rationale = explain_route("USB_D+", pcb)
    >>> print(rationale.rationale)
    Shortest path avoiding high-frequency signals
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from .decisions import (
    DecisionStore,
    PlacementRationale,
    RoutingRationale,
    get_decisions_path,
)

if TYPE_CHECKING:
    from kicad_tools.schema.pcb import PCB


def record_decision(
    pcb: PCB,
    action: str,
    components: list[str] | None = None,
    nets: list[str] | None = None,
    position: tuple[float, float] | None = None,
    rationale: str = "",
    alternatives_considered: list[dict[str, str]] | None = None,
    constraints_satisfied: list[str] | None = None,
    constraints_violated: list[str] | None = None,
    decided_by: str = "agent",
    parent_decision: str | None = None,
    metrics: dict[str, float] | None = None,
) -> str:
    """Record a design decision for a PCB.

    This is the main API for recording decisions. The decision is saved
    to a JSON file alongside the PCB file.

    Args:
        pcb: The PCB object (used to determine file path)
        action: Action type ("place", "route", "move", "reroute", "delete")
        components: List of component references affected
        nets: List of net names affected
        position: Position tuple (x, y) if applicable
        rationale: Human-readable explanation of why
        alternatives_considered: List of alternatives with rejection reasons
            Each dict should have "description" and "rejected_because" keys
        constraints_satisfied: List of constraints this decision satisfies
        constraints_violated: List of constraints this decision violates
        decided_by: Who/what made the decision ("agent", "human", "optimizer", "autorouter")
        parent_decision: ID of parent decision for decision chains
        metrics: Quantitative metrics (e.g., {"trace_length": 52.3})

    Returns:
        The decision ID

    Example:
        >>> decision_id = record_decision(
        ...     pcb=pcb,
        ...     action="place",
        ...     components=["U1"],
        ...     position=(50, 30),
        ...     rationale="Placed MCU near board center for balanced routing",
        ...     alternatives_considered=[
        ...         {"description": "Position (20, 30)", "rejected_because": "Too close to connector"},
        ...         {"description": "Position (80, 30)", "rejected_because": "USB traces too long"},
        ...     ],
        ...     constraints_satisfied=["usb_trace_length < 50mm"],
        ... )
    """
    from .decisions import Alternative, Decision

    # Get or create decision store
    pcb_path = Path(pcb.path) if hasattr(pcb, "path") and pcb.path else None
    if pcb_path:
        decisions_path = get_decisions_path(pcb_path)
        store = DecisionStore.load(decisions_path)
    else:
        # No path yet, create in-memory store attached to PCB
        if not hasattr(pcb, "_decision_store"):
            pcb._decision_store = DecisionStore()
        store = pcb._decision_store

    # Convert alternatives
    alternatives = []
    if alternatives_considered:
        for alt_dict in alternatives_considered:
            alternatives.append(
                Alternative(
                    description=alt_dict.get("description", ""),
                    rejected_because=alt_dict.get("rejected_because", ""),
                    metrics=alt_dict.get("metrics", {}),
                )
            )

    # Create and record decision
    decision = Decision.create(
        action=action,
        components=components,
        nets=nets,
        position=position,
        rationale=rationale,
        decided_by=decided_by,
        alternatives_considered=alternatives,
        constraints_satisfied=constraints_satisfied,
        constraints_violated=constraints_violated,
        parent_decision=parent_decision,
        metrics=metrics,
    )

    decision_id = store.record(decision)

    # Save to file if we have a path
    if pcb_path:
        store.save(decisions_path)

    return decision_id


def get_decisions(
    pcb: PCB,
    component: str | None = None,
    net: str | None = None,
    action: str | None = None,
) -> list:
    """Query decisions for a PCB.

    Args:
        pcb: The PCB object
        component: Filter by component reference
        net: Filter by net name
        action: Filter by action type

    Returns:
        List of Decision objects matching the criteria

    Example:
        >>> # Get all decisions for U1
        >>> decisions = get_decisions(pcb, component="U1")
        >>>
        >>> # Get all routing decisions for USB_D+
        >>> decisions = get_decisions(pcb, action="route", net="USB_D+")
    """
    pcb_path = Path(pcb.path) if hasattr(pcb, "path") and pcb.path else None

    if pcb_path:
        decisions_path = get_decisions_path(pcb_path)
        store = DecisionStore.load(decisions_path)
    elif hasattr(pcb, "_decision_store"):
        store = pcb._decision_store
    else:
        return []

    return store.query(component=component, net=net, action=action)


def explain_placement(component: str, pcb: PCB) -> PlacementRationale | None:
    """Explain why a component is placed at its current position.

    Queries the decision history to find placement decisions for the
    specified component and returns the rationale.

    Args:
        component: Component reference (e.g., "U1")
        pcb: The PCB object

    Returns:
        PlacementRationale with explanation, or None if no decision found

    Example:
        >>> rationale = explain_placement("U1", pcb)
        >>> if rationale:
        ...     print(f"Component: {rationale.component}")
        ...     print(f"Position: {rationale.position}")
        ...     print(f"Rationale: {rationale.rationale}")
        ...     print(f"Decided by: {rationale.decided_by}")
    """
    # Get placement decisions for this component
    decisions = get_decisions(pcb, component=component, action="place")

    # Also check move decisions
    move_decisions = get_decisions(pcb, component=component, action="move")
    decisions.extend(move_decisions)

    # Sort by timestamp (newest first) and get the most recent
    decisions.sort(key=lambda d: d.timestamp, reverse=True)

    if not decisions:
        # No decision recorded, try to get position from PCB
        for fp in getattr(pcb, "footprints", []):
            if fp.reference == component:
                return PlacementRationale(
                    component=component,
                    position=(fp.position[0], fp.position[1]),
                    rationale="No decision recorded for this placement",
                    decided_by="unknown",
                    timestamp="",
                )
        return None

    decision = decisions[0]
    position = decision.position

    # If no position in decision, get from PCB
    if position is None:
        for fp in getattr(pcb, "footprints", []):
            if fp.reference == component:
                position = (fp.position[0], fp.position[1])
                break

    if position is None:
        position = (0.0, 0.0)

    return PlacementRationale(
        component=component,
        position=position,
        rationale=decision.rationale,
        decided_by=decision.decided_by,
        timestamp=decision.timestamp,
        alternatives=decision.alternatives,
        constraints=decision.constraints_satisfied,
        decision_id=decision.id,
    )


def explain_route(net: str, pcb: PCB) -> RoutingRationale | None:
    """Explain why a net was routed the way it was.

    Queries the decision history to find routing decisions for the
    specified net and returns the rationale.

    Args:
        net: Net name (e.g., "USB_D+")
        pcb: The PCB object

    Returns:
        RoutingRationale with explanation, or None if no decision found

    Example:
        >>> rationale = explain_route("USB_D+", pcb)
        >>> if rationale:
        ...     print(f"Net: {rationale.net}")
        ...     print(f"Rationale: {rationale.rationale}")
        ...     print(f"Metrics: {rationale.metrics}")
    """
    # Get routing decisions for this net
    decisions = get_decisions(pcb, net=net, action="route")

    # Also check reroute decisions
    reroute_decisions = get_decisions(pcb, net=net, action="reroute")
    decisions.extend(reroute_decisions)

    # Sort by timestamp (newest first) and get the most recent
    decisions.sort(key=lambda d: d.timestamp, reverse=True)

    if not decisions:
        return RoutingRationale(
            net=net,
            rationale="No decision recorded for this route",
            decided_by="unknown",
        )

    decision = decisions[0]

    return RoutingRationale(
        net=net,
        rationale=decision.rationale,
        decided_by=decision.decided_by,
        timestamp=decision.timestamp,
        alternatives=decision.alternatives,
        constraints=decision.constraints_satisfied,
        metrics=decision.metrics,
        decision_id=decision.id,
    )


def get_decision_store(pcb: PCB) -> DecisionStore:
    """Get the decision store for a PCB.

    Args:
        pcb: The PCB object

    Returns:
        The DecisionStore for this PCB
    """
    pcb_path = Path(pcb.path) if hasattr(pcb, "path") and pcb.path else None

    if pcb_path:
        decisions_path = get_decisions_path(pcb_path)
        return DecisionStore.load(decisions_path)
    elif hasattr(pcb, "_decision_store"):
        return pcb._decision_store
    else:
        store = DecisionStore()
        pcb._decision_store = store
        return store


def save_decisions(pcb: PCB) -> bool:
    """Save the decision store for a PCB to disk.

    Args:
        pcb: The PCB object

    Returns:
        True if saved successfully, False if no path available
    """
    pcb_path = Path(pcb.path) if hasattr(pcb, "path") and pcb.path else None

    if not pcb_path:
        return False

    store = get_decision_store(pcb)
    decisions_path = get_decisions_path(pcb_path)
    store.save(decisions_path)
    return True
