"""PCB analysis tools.

This module provides analysis tools for PCB designs:
- Routing congestion analysis
- Density calculations
- Problem area identification
- Trace length analysis for timing-critical nets
"""

from .congestion import CongestionAnalyzer, CongestionReport, Severity
from .trace_length import (
    DifferentialPairReport,
    TraceLengthAnalyzer,
    TraceLengthReport,
)

__all__ = [
    "CongestionAnalyzer",
    "CongestionReport",
    "DifferentialPairReport",
    "Severity",
    "TraceLengthAnalyzer",
    "TraceLengthReport",
]
