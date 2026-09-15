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

Issue #5240: every handler used to be imported eagerly here, so importing
this package -- which a single ``kct <anything>`` invocation always does,
directly or via :mod:`kicad_tools.cli` -- paid the full transitive import
cost of *all* ~30 command domains (including the router's heaviest module,
``routing.py``/``route_cmd.py``) even for a cheap, unrelated command like
``kct zones fill`` or a bare ``--help``.  Measured locally: importing just
``run_route_command`` this way cost ~1.7s of CPU time (~3s wall) beyond a
bare interpreter start, and every CLI subcommand paid it regardless of
which domain it actually needed.  Board 06 / diff-pair regression's
post-route pipeline alone spawns 2-7 ``kicad_tools.cli`` subprocesses per
run (first zone fill + stitch + up to ``MAX_POUR_REPAIR_ROUNDS`` re-fills),
each previously re-paying this cost for code it never touches.

This module now resolves each handler **lazily** on first attribute access
(:pep:`562`), so a caller that only needs ``run_zones_command`` never
imports ``routing``'s sibling domains (placement, reasoning, datasheet,
...). Behavior is unchanged: every name below remains importable exactly
as before (``from kicad_tools.cli.commands import run_route_command``,
``kicad_tools.cli.commands.run_route_command``, tab-completion via
``__dir__``, etc.) -- only the *timing* of the underlying submodule import
moves from "this package's import time" to "this name's first access".
"""

from __future__ import annotations

import importlib
from typing import Any

# Maps each public handler name to the submodule (relative to this package)
# that defines it.  Kept as plain strings (not real imports) so listing a
# name here costs nothing until it is actually requested.
_HANDLER_MODULES: dict[str, str] = {
    "run_analyze_command": ".analyze",
    "run_bench_command": ".bench",
    "run_benchmark_command": ".benchmark",
    "run_board_metrics_command": ".board_metrics",
    "run_build_command": ".build",
    "run_config_command": ".config",
    "run_interactive_command": ".config",
    "run_create_pcb_command": ".create_pcb",
    "run_creepage_command": ".creepage",
    "run_creepage_export_rules_command": ".creepage_export_rules",
    "run_datasheet_command": ".datasheet",
    "run_decisions_command": ".decisions",
    "run_doctor_command": ".doctor",
    "run_estimate_command": ".estimate",
    "run_fleet_command": ".fleet",
    "run_footprint_command": ".footprint",
    "run_impedance_command": ".impedance",
    "run_ipc_command": ".ipc",
    "run_lib_command": ".library",
    "run_mfr_command": ".manufacturer",
    "run_mcp_command": ".mcp",
    "run_build_native_command": ".native",
    "run_optimize_placement_command": ".optimize_placement",
    "run_panel_command": ".panel",
    "run_parts_command": ".parts",
    "run_pcb_command": ".pcb",
    "run_pipeline_command": ".pipeline",
    "run_placement_command": ".placement",
    "run_clean_command": ".project",
    "run_init_command": ".project",
    "run_readiness_command": ".readiness",
    "run_reason_command": ".reasoning",
    "run_optimize_command": ".routing",
    "run_route_auto_command": ".routing",
    "run_route_command": ".routing",
    "run_zones_command": ".routing",
    "run_run_command": ".run",
    "run_sch_command": ".schematic",
    "run_spec_command": ".spec",
    "run_suggest_command": ".suggest",
    "run_sync_command": ".sync",
    "run_audit_command": ".validation",
    "run_check_command": ".validation",
    "run_constraints_command": ".validation",
    "run_fix_drc_command": ".validation",
    "run_fix_erc_command": ".validation",
    "run_fix_footprints_command": ".validation",
    "run_fix_silkscreen_command": ".validation",
    "run_fix_vias_command": ".validation",
    "run_place_silk_refs_command": ".validation",
    "run_repair_clearance_command": ".validation",
    "run_validate_command": ".validation",
    "run_validate_footprints_command": ".validation",
}

__all__ = sorted(_HANDLER_MODULES)


def __getattr__(name: str) -> Any:
    """Resolve a handler on first access (:pep:`562`).

    Imports only the ONE submodule that defines ``name``, caches the
    result as a normal module attribute (so subsequent access skips this
    hook entirely -- standard lazy-attribute pattern), and raises
    ``AttributeError`` for anything else so failures look identical to the
    pre-#5240 eager-import behaviour.
    """
    try:
        submodule_name = _HANDLER_MODULES[name]
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
    submodule = importlib.import_module(submodule_name, __name__)
    value = getattr(submodule, name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_HANDLER_MODULES))
