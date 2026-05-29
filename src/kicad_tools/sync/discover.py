"""Schematic discovery helper for PCB-only command entry points.

Commands like ``kct route`` and ``kct check`` receive only a ``.kicad_pcb``
path, but schematic/PCB drift detection (the :class:`Reconciler`) needs both
files.  This module factors out the single schematic-resolution heuristic so
``route``, ``check``, and ``audit`` all agree on where the schematic lives.

Resolution order (matches the historical ``Auditor._resolve_schematic_path``):
    1. ``project.kct`` ``artifacts.schematic`` (looked up next to the PCB, then
       one directory up), if it points at an existing file.
    2. Sibling ``<pcb-basename>.kicad_sch``.

A PCB basename may carry pipeline-stage suffixes (``_routed``, ``_optimized``,
``_stitched``, ``_phase1``) that the schematic never has, so those are stripped
before the sibling lookup.

Returns ``None`` when no schematic can be found -- callers should treat that as
"skip silently" rather than an error, since many PCB-only workflows are
legitimate.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Pipeline-stage suffixes appended to a PCB basename by routing/optimization
# stages.  The schematic keeps the bare project basename, so we strip these
# before looking for a sibling ``.kicad_sch``.
_PCB_STAGE_SUFFIXES = ("_routed", "_optimized", "_stitched", "_phase1")


def _strip_stage_suffix(stem: str) -> str:
    """Strip a single known pipeline-stage suffix from a PCB file stem."""
    for suffix in _PCB_STAGE_SUFFIXES:
        if stem.endswith(suffix):
            return stem[: -len(suffix)]
    return stem


def resolve_schematic_for_pcb(pcb_path: str | Path) -> Path | None:
    """Resolve the schematic file associated with a PCB.

    Args:
        pcb_path: Path to a ``.kicad_pcb`` file.

    Returns:
        Path to the resolved ``.kicad_sch`` file if one exists, else ``None``.
    """
    pcb_path = Path(pcb_path)
    project_dir = pcb_path.parent

    # 1. Try project.kct artifacts.schematic (next to the PCB, then one up).
    kct_path = project_dir / "project.kct"
    if not kct_path.exists():
        kct_path = project_dir.parent / "project.kct"

    if kct_path.exists():
        try:
            from kicad_tools.spec import load_spec

            spec = load_spec(kct_path)
            if spec.project and spec.project.artifacts and spec.project.artifacts.schematic:
                sch_path = kct_path.parent / spec.project.artifacts.schematic
                if sch_path.exists():
                    logger.debug("Using schematic from project.kct: %s", sch_path)
                    return sch_path
        except Exception as e:  # pragma: no cover - defensive
            logger.debug("Failed to load project.kct: %s", e)

    # 2. Sibling <basename>.kicad_sch, trying the stage-stripped stem first
    #    (e.g. board_routed.kicad_pcb -> board.kicad_sch), then the raw stem.
    stripped_stem = _strip_stage_suffix(pcb_path.stem)
    candidates = [project_dir / f"{stripped_stem}.kicad_sch"]
    if pcb_path.stem != stripped_stem:
        candidates.append(pcb_path.with_suffix(".kicad_sch"))

    for candidate in candidates:
        if candidate.exists():
            logger.debug("Using sibling schematic: %s", candidate)
            return candidate

    return None
