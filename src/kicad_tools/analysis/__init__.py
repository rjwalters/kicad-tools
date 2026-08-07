"""PCB analysis tools.

This module provides analysis tools for PCB designs:
- Pre-routing complexity estimation and layer prediction
- Routing congestion analysis
- Density calculations
- Problem area identification
- Trace length analysis for timing-critical nets
- Signal integrity analysis (crosstalk and impedance discontinuities)
- Thermal analysis and hotspot detection
- Analog component detection for layout-sensitive parts
"""

from .analog_detect import (
    AnalogComponent,
    detect_analog_components,
)
from .complexity import (
    Bottleneck,
    ComplexityAnalyzer,
    ComplexityRating,
    LayerPrediction,
    RoutingComplexity,
)
from .congestion import CongestionAnalyzer, CongestionReport, Severity
from .current_sense import (
    CurrentSenseAnalyzer,
    CurrentSenseResult,
)
from .electrical_rating import (
    ElectricalRatingAnalyzer,
    ElectricalRatingResult,
    infer_rail_voltage,
)
from .net_status import (
    NetStatus,
    NetStatusAnalyzer,
    NetStatusResult,
    PadInfo,
    build_zone_net_map,
)
from .routing_quality import (
    FRAGMENT_LENGTH_MM,
    RULE_FRAGMENT_FRACTION,
    RULE_STAIRCASE_FRACTION,
    STAIRCASE_STEP_MM,
    RoutingQualityMetrics,
    ThresholdBreach,
    compute_routing_quality,
    evaluate_routing_quality_thresholds,
    routing_quality_gate_dict,
)
from .signal_integrity import (
    CrosstalkRisk,
    ImpedanceDiscontinuity,
    RiskLevel,
    SignalIntegrityAnalyzer,
    TraceCrosstalkRisk,
    TraceIntegrityAnalyzer,
)
from .thermal import (
    PowerEstimator,
    ThermalAnalyzer,
    ThermalHotspot,
    ThermalSeverity,
    ThermalSource,
)
from .trace_length import (
    DifferentialPairReport,
    TraceLengthAnalyzer,
    TraceLengthReport,
)

__all__ = [
    "FRAGMENT_LENGTH_MM",
    "RULE_FRAGMENT_FRACTION",
    "RULE_STAIRCASE_FRACTION",
    "STAIRCASE_STEP_MM",
    "AnalogComponent",
    "Bottleneck",
    "ComplexityAnalyzer",
    "ComplexityRating",
    "CongestionAnalyzer",
    "CongestionReport",
    "CrosstalkRisk",
    "CurrentSenseAnalyzer",
    "CurrentSenseResult",
    "DifferentialPairReport",
    "ElectricalRatingAnalyzer",
    "ElectricalRatingResult",
    "ImpedanceDiscontinuity",
    "LayerPrediction",
    "NetStatus",
    "NetStatusAnalyzer",
    "NetStatusResult",
    "PadInfo",
    "PowerEstimator",
    "RiskLevel",
    "RoutingComplexity",
    "RoutingQualityMetrics",
    "Severity",
    "SignalIntegrityAnalyzer",
    "ThresholdBreach",
    "TraceCrosstalkRisk",
    "TraceIntegrityAnalyzer",
    "ThermalAnalyzer",
    "ThermalHotspot",
    "ThermalSeverity",
    "ThermalSource",
    "TraceLengthAnalyzer",
    "TraceLengthReport",
    "build_zone_net_map",
    "compute_routing_quality",
    "detect_analog_components",
    "evaluate_routing_quality_thresholds",
    "infer_rail_voltage",
    "routing_quality_gate_dict",
]
