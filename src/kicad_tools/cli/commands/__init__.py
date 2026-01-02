"""Command handlers for kicad-tools CLI.

This package contains command handler modules organized by domain:
- schematic: sch subcommand handlers
- pcb: pcb subcommand handlers
- library: lib subcommand handlers
- routing: route, zones, optimize-traces handlers
- validation: validate, check handlers
- footprint: footprint generation handlers
- parts: LCSC parts lookup handlers
- datasheet: datasheet command handlers
- reasoning: reason command handler
- config: config and interactive command handlers
- manufacturer: mfr subcommand handlers
"""

from .config import run_config_command, run_interactive_command
from .datasheet import run_datasheet_command
from .footprint import run_footprint_command
from .library import run_lib_command
from .manufacturer import run_mfr_command
from .parts import run_parts_command
from .pcb import run_pcb_command
from .placement import run_placement_command
from .reasoning import run_reason_command
from .routing import run_optimize_command, run_route_command, run_zones_command
from .schematic import run_sch_command
from .validation import (
    run_check_command,
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
]
