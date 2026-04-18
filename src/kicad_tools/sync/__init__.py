"""Schematic-to-PCB synchronization and reconciliation.

This module bridges the gap between read-only consistency checking
(validate/consistency.py) and PCB mutation (cli/pcb_modify.py) by
analyzing mismatches and applying fixes.

Key classes:
    SyncAnalysis - Result of analyzing schematic/PCB mismatches
    SyncMatch - A proposed mapping between schematic and PCB components
    Reconciler - Orchestrates analysis and application of fixes
"""

from .reconciler import (
    Reconciler,
    SyncAnalysis,
    SyncMatch,
)

__all__ = [
    "Reconciler",
    "SyncAnalysis",
    "SyncMatch",
]
