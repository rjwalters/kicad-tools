"""
PCB Chain-of-Thought Reasoning - LLM-Driven Layout via Structured Reasoning

This module provides a framework for PCB layout using chain-of-thought reasoning
where an LLM makes strategic decisions and tools handle geometric execution.

The core insight: Traditional autorouters are semantically blind - they connect
pads without understanding design intent. An LLM can reason about WHY decisions
matter, not just execute WHAT needs connecting.

Architecture:
    ┌─────────────────────────────────────────────────────────────────┐
    │ LLM (Strategy)                                                  │
    │ "Route MCLK around analog section via northern path"           │
    └─────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
    ┌─────────────────────────────────────────────────────────────────┐
    │ Command Interpreter                                             │
    │ Translates intent → geometric operations                        │
    └─────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
    ┌─────────────────────────────────────────────────────────────────┐
    │ PCB Tools (Execution)                                           │
    │ Pathfinding, DRC checking, trace placement                      │
    └─────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
    ┌─────────────────────────────────────────────────────────────────┐
    │ Feedback (Diagnosis)                                            │
    │ "Path blocked by U2, alternatives: north +4mm, south crosses X" │
    └─────────────────────────────────────────────────────────────────┘

Usage:
    from kicad_tools.reasoning import PCBReasoningAgent, PCBState

    # Load board state
    agent = PCBReasoningAgent.from_pcb("board.kicad_pcb")

    # Run reasoning loop
    while not agent.is_complete():
        # Get current state for LLM
        state = agent.get_state()

        # LLM generates next action (external call)
        action = llm_decide(state.to_prompt())

        # Execute and get feedback
        result = agent.execute(action)

        if not result.success:
            # Diagnosis helps LLM understand what went wrong
            diagnosis = agent.diagnose(result)

    # Save result
    agent.save("board-routed.kicad_pcb")
"""

from .state import (
    PCBState,
    ComponentState,
    NetState,
    PadState,
    TraceState,
    ViaState,
    ZoneState,
    ViolationState,
)
from .vocabulary import (
    SpatialRegion,
    SpatialRelation,
    NetType,
    ComponentGroup,
    RoutingPriority,
)
from .commands import (
    Command,
    PlaceComponentCommand,
    RouteNetCommand,
    DeleteTraceCommand,
    AddViaCommand,
    DefineZoneCommand,
    CommandResult,
)
from .interpreter import CommandInterpreter
from .diagnosis import DiagnosisEngine, RoutingDiagnosis
from .agent import PCBReasoningAgent

__all__ = [
    # State
    "PCBState",
    "ComponentState",
    "NetState",
    "PadState",
    "TraceState",
    "ViaState",
    "ZoneState",
    "ViolationState",
    # Vocabulary
    "SpatialRegion",
    "SpatialRelation",
    "NetType",
    "ComponentGroup",
    "RoutingPriority",
    # Commands
    "Command",
    "PlaceComponentCommand",
    "RouteNetCommand",
    "DeleteTraceCommand",
    "AddViaCommand",
    "DefineZoneCommand",
    "CommandResult",
    # Interpreter
    "CommandInterpreter",
    # Diagnosis
    "DiagnosisEngine",
    "RoutingDiagnosis",
    # Agent
    "PCBReasoningAgent",
]
