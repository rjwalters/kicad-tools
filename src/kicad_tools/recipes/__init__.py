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

The package holds the two halves of the recipe contract:

* :mod:`kicad_tools.recipes.precondition` -- the **pre**-condition
  (:func:`~kicad_tools.recipes.precondition.require_spec`, issue #4539):
  before a recipe mutates anything, assert the board carries captured
  intent (a non-empty, parseable ``project.kct``).  Advisory and fail-soft
  by default; opt into a hard semantic-validation gate with
  ``KCT_REQUIRE_SPEC=1``.
* :mod:`kicad_tools.recipes.gate` -- the **post**-condition
  (:func:`~kicad_tools.recipes.gate.evaluate_pipeline_gate`, issue #3912):
  after route / DRC / LVS have run, turn their verdicts into ONE
  :class:`~kicad_tools.recipes.gate.PipelineGateResult` that drives both
  the printed ``SUMMARY`` and the process exit code.
"""

from __future__ import annotations

from kicad_tools.recipes.gate import (
    DEFAULT_ADVISORY_DRC_TYPES,
    PipelineGateResult,
    evaluate_pipeline_gate,
)
from kicad_tools.recipes.precondition import (
    SPEC_FILENAME,
    STRICT_ENV_VAR,
    SpecPreconditionResult,
    discover_spec,
    require_spec,
)

__all__ = [
    "DEFAULT_ADVISORY_DRC_TYPES",
    "SPEC_FILENAME",
    "STRICT_ENV_VAR",
    "PipelineGateResult",
    "SpecPreconditionResult",
    "discover_spec",
    "evaluate_pipeline_gate",
    "require_spec",
]
