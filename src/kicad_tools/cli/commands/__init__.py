"""Command handlers for kicad-tools CLI.

This package contains command handler modules organized by domain:
- schematic: sch subcommand handlers
- pcb: pcb subcommand handlers
- library: lib subcommand handlers
- routing: route, zones, optimize-traces handlers
- validation: validate, check, constraints handlers
- footprint: footprint generation handlers
- parts: LCSC parts lookup handlers
- datasheet: datasheet command handlers
- reasoning: reason command handler
- config: config and interactive command handlers
- manufacturer: mfr subcommand handlers
- analyze: PCB analysis tools (congestion, etc.)
"""

from .analyze import run_analyze_command
from .config import run_config_command, run_interactive_command
from .datasheet import run_datasheet_command
from .estimate import run_estimate_command
from .footprint import run_footprint_command
from .impedance import run_impedance_command
from .library import run_lib_command
from .manufacturer import run_mfr_command
from .parts import run_parts_command
from .pcb import run_pcb_command
from .placement import run_placement_command
from .project import run_clean_command
from .reasoning import run_reason_command
from .routing import run_optimize_command, run_route_command, run_zones_command
from .schematic import run_sch_command
from .suggest import run_suggest_command
from .validation import (
    run_audit_command,
    run_check_command,
    run_constraints_command,
    run_fix_footprints_command,
    run_validate_command,
    run_validate_footprints_command,
)

__all__ = [
    # Schematic
    "run_sch_command",
    # PCB
    "run_pcb_command",
    # Library
    "run_lib_command",
    # Routing
    "run_route_command",
    "run_zones_command",
    "run_optimize_command",
    # Validation
    "run_check_command",
    "run_validate_command",
    "run_validate_footprints_command",
    "run_fix_footprints_command",
    "run_constraints_command",
    # Footprint
    "run_footprint_command",
    # Parts
    "run_parts_command",
    # Datasheet
    "run_datasheet_command",
    # Reasoning
    "run_reason_command",
    # Placement
    "run_placement_command",
    # Config
    "run_config_command",
    "run_interactive_command",
    # Manufacturer
    "run_mfr_command",
    # Analysis
    "run_analyze_command",
    # Estimate
    "run_estimate_command",
    # Audit
    "run_audit_command",
    # Suggest
    "run_suggest_command",
    # Project
    "run_clean_command",
    # Impedance
    "run_impedance_command",
]
