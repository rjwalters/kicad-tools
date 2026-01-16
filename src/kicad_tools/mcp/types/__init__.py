"""Type definitions for MCP tools.

Provides dataclasses for tool inputs and outputs, organized by domain.

This module re-exports all types for backwards compatibility with code
that imports from `kicad_tools.mcp.types`. For new code, consider
importing from specific submodules:

    from kicad_tools.mcp.types.board import BoardAnalysis
    from kicad_tools.mcp.types.drc import DRCResult
    from kicad_tools.mcp.types.session import SessionInfo
"""

from __future__ import annotations

# Assembly Export Types
from .assembly import (
    AssemblyExportResult,
    BOMExportResult,
    BOMGenerationResult,
    BOMItemResult,
    CostEstimate,
    PnPExportResult,
)

# Board Analysis Types
from .board import (
    BoardAnalysis,
    BoardDimensions,
    ComponentSummary,
    LayerInfo,
    NetFanout,
    NetSummary,
    RoutingStatus,
    ZoneInfo,
)

# Clearance Measurement Types
from .clearance import (
    ClearanceMeasurement,
    ClearanceResult,
)

# DRC Violation Types
from .drc import (
    AffectedItem,
    DRCResult,
    DRCViolation,
    ViolationLocation,
)

# DRC Delta Types (for continuous validation)
from .drc_delta import (
    DRCDeltaInfo,
    DRCSummary,
    DRCViolationDetail,
)

# Gerber Export Types
from .gerber import (
    LAYER_FILE_TYPES,
    GerberExportResult,
    GerberFile,
    get_file_type,
)

# Intent Declaration Types
from .intent import (
    ClearIntentResult,
    ConstraintInfo,
    DeclareInterfaceResult,
    DeclarePowerRailResult,
    IntentInfo,
    IntentStatus,
    IntentViolation,
    ListIntentsResult,
)

# Placement Analysis Types
from .placement import (
    PlacementAnalysis,
    PlacementCluster,
    PlacementIssue,
    PlacementScores,
    RoutingEstimate,
)

# Routing Types
from .routing import (
    NetRoutingStatus,
    RouteNetResult,
    UnroutedNetsResult,
)

# Session Management Types
from .session import (
    ApplyMoveResult,
    CommitResult,
    ComponentPosition,
    QueryMoveResult,
    RollbackResult,
    RoutingImpactInfo,
    SessionInfo,
    SessionStatusResult,
    StartSessionResult,
    UndoResult,
    ViolationInfo,
)

# Predictive Warning Types
from .warnings import (
    PredictiveWarningInfo,
)

__all__ = [
    # Board Analysis Types
    "BoardDimensions",
    "LayerInfo",
    "ComponentSummary",
    "NetFanout",
    "NetSummary",
    "ZoneInfo",
    "RoutingStatus",
    "BoardAnalysis",
    # Gerber Export Types
    "GerberFile",
    "GerberExportResult",
    "LAYER_FILE_TYPES",
    "get_file_type",
    # DRC Violation Types
    "ViolationLocation",
    "AffectedItem",
    "DRCViolation",
    "DRCResult",
    # DRC Delta Types
    "DRCViolationDetail",
    "DRCDeltaInfo",
    "DRCSummary",
    # Session Management Types
    "SessionInfo",
    "ComponentPosition",
    "RoutingImpactInfo",
    "ViolationInfo",
    "StartSessionResult",
    "QueryMoveResult",
    "ApplyMoveResult",
    "CommitResult",
    "RollbackResult",
    "UndoResult",
    "SessionStatusResult",
    # Assembly Export Types
    "BOMExportResult",
    "BOMItemResult",
    "BOMGenerationResult",
    "PnPExportResult",
    "CostEstimate",
    "AssemblyExportResult",
    # Placement Analysis Types
    "PlacementScores",
    "PlacementIssue",
    "PlacementCluster",
    "RoutingEstimate",
    "PlacementAnalysis",
    # Clearance Measurement Types
    "ClearanceMeasurement",
    "ClearanceResult",
    # Routing Types
    "NetRoutingStatus",
    "UnroutedNetsResult",
    "RouteNetResult",
    # Intent Declaration Types
    "ConstraintInfo",
    "DeclareInterfaceResult",
    "DeclarePowerRailResult",
    "IntentInfo",
    "ListIntentsResult",
    "ClearIntentResult",
    "IntentViolation",
    "IntentStatus",
    # Predictive Warning Types
    "PredictiveWarningInfo",
]
