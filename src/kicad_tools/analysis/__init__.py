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
from .net_status import (
    NetStatus,
    NetStatusAnalyzer,
    NetStatusResult,
    PadInfo,
)
from .signal_integrity import (
    CrosstalkRisk,
    ImpedanceDiscontinuity,
    RiskLevel,
    SignalIntegrityAnalyzer,
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
    "AnalogComponent",
    "Bottleneck",
    "ComplexityAnalyzer",
    "ComplexityRating",
    "CongestionAnalyzer",
    "CongestionReport",
    "CrosstalkRisk",
    "DifferentialPairReport",
    "ImpedanceDiscontinuity",
    "LayerPrediction",
    "NetStatus",
    "NetStatusAnalyzer",
    "NetStatusResult",
    "PadInfo",
    "PowerEstimator",
    "RiskLevel",
    "RoutingComplexity",
    "Severity",
    "SignalIntegrityAnalyzer",
    "ThermalAnalyzer",
    "ThermalHotspot",
    "ThermalSeverity",
    "ThermalSource",
    "TraceLengthAnalyzer",
    "TraceLengthReport",
    "detect_analog_components",
]
