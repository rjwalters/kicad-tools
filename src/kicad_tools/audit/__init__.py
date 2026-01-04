"""
Manufacturing readiness audit for KiCad PCB designs.

Comprehensive pre-manufacturing check that validates:
- ERC (Electrical Rules Check) on schematic
- DRC (Design Rules Check) on PCB
- Net connectivity (all nets routed)
- Manufacturer compatibility (design rules meet fab specs)
- Layer utilization statistics
- Cost estimation

Usage:
    from kicad_tools.audit import ManufacturingAudit, AuditResult

    audit = ManufacturingAudit("project.kicad_pro", manufacturer="jlcpcb")
    result = audit.run()
    print(result.verdict)  # "READY" or "NOT READY"
"""

from .auditor import (
    AuditResult,
    AuditVerdict,
    ManufacturingAudit,
)

__all__ = [
    "ManufacturingAudit",
    "AuditResult",
    "AuditVerdict",
]
