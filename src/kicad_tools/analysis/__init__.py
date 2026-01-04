"""PCB analysis tools.

This module provides analysis tools for PCB designs:
- Routing congestion analysis
- Density calculations
- Problem area identification
- Trace length analysis for timing-critical nets
- Signal integrity analysis (crosstalk and impedance discontinuities)
- Thermal analysis and hotspot detection
"""

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
    "CongestionAnalyzer",
    "CongestionReport",
    "CrosstalkRisk",
    "DifferentialPairReport",
    "ImpedanceDiscontinuity",
    "PowerEstimator",
    "RiskLevel",
    "Severity",
    "SignalIntegrityAnalyzer",
    "ThermalAnalyzer",
    "ThermalHotspot",
    "ThermalSeverity",
    "ThermalSource",
    "TraceLengthAnalyzer",
    "TraceLengthReport",
    "NetStatusAnalyzer",
    "NetStatusResult",
    "NetStatus",
    "PadInfo",
]
