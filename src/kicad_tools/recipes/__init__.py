"""Shared building blocks for the ``boards/*/*.py`` demo/manufacturing recipes.

Each board recipe (``boards/NN-name/generate_design.py`` or ``design.py``)
historically hand-rolled its own success gate: an ad-hoc ``main()`` that
computed a process exit code from a partial AND of route / DRC / LVS
booleans, and a separately-printed ``SUMMARY`` block.  Because the exit
expression and the SUMMARY were written independently per board they
drifted -- and both drifted away from ground truth (issue #3912: board-06
dropped the DRC leg from its exit code, board-05 dropped route-completion
and gated on a stale-zone-fill ``kct check`` that missed real copper
shorts).

This package holds the shared pieces those recipes should call so a single
verdict drives BOTH the SUMMARY and the exit code, and the authoritative
``kicad-cli pcb drc --refill-zones`` engine is used for the DRC leg.

See :mod:`kicad_tools.recipes.gate` for the shared pipeline success gate.
"""

from __future__ import annotations

from kicad_tools.recipes.gate import (
    DEFAULT_ADVISORY_DRC_TYPES,
    PipelineGateResult,
    evaluate_pipeline_gate,
)

__all__ = [
    "DEFAULT_ADVISORY_DRC_TYPES",
    "PipelineGateResult",
    "evaluate_pipeline_gate",
]
