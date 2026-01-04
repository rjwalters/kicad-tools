"""PCB analysis tools.

This module provides analysis tools for PCB designs:
- Routing congestion analysis
- Density calculations
- Problem area identification
- Trace length analysis for timing-critical nets
- Signal integrity analysis (crosstalk and impedance discontinuities)
"""

from .congestion import CongestionAnalyzer, CongestionReport, Severity
from .signal_integrity import (
    CrosstalkRisk,
    ImpedanceDiscontinuity,
    RiskLevel,
    SignalIntegrityAnalyzer,
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
    "RiskLevel",
    "Severity",
    "SignalIntegrityAnalyzer",
    "TraceLengthAnalyzer",
    "TraceLengthReport",
]
