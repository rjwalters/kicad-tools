"""Shared utility helpers for report generation.

Provides schematic file discovery logic used by both the data collector
and the report CLI command.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def find_schematic(pcb_path: Path) -> Path | None:
    """Locate the root .kicad_sch file for a given .kicad_pcb.

    Resolution order:

    1. **Direct stem match** -- ``<pcb_stem>.kicad_sch`` in the same directory.
    2. **Project file lookup** -- read ``*.kicad_pro`` to derive the project
       stem, then check ``<project_stem>.kicad_sch``.
    3. **Single-glob fallback** -- if exactly one ``*.kicad_sch`` exists in
       the same directory, return it.  When multiple are found the result is
       ambiguous and ``None`` is returned with a warning.
    4. **None** -- all candidates exhausted; the caller should warn and
       suggest ``--sch``.

    Args:
        pcb_path: Path to the ``.kicad_pcb`` file.

    Returns:
        Resolved :class:`~pathlib.Path` to the schematic, or ``None`` when
        no unambiguous candidate is found.
    """
    directory = pcb_path.parent

    # Step 1: direct stem match
    candidate = pcb_path.with_suffix(".kicad_sch")
    if candidate.exists():
        return candidate

    # Step 1.5: strip known suffixes (_routed, _fixed, etc.) from the PCB stem
    _STRIP_SUFFIXES = ("_routed", "_fixed", "_v2", "_final")
    stem = pcb_path.stem
    for suffix in _STRIP_SUFFIXES:
        if stem.endswith(suffix):
            stripped = directory / (stem[: -len(suffix)] + ".kicad_sch")
            if stripped.exists():
                return stripped
            break  # only strip one suffix

    # Step 2: project file lookup
    pro_files = list(directory.glob("*.kicad_pro"))
    for pro in pro_files:
        # Try to read meta.filename from the project JSON to derive the stem.
        # Fall back to the project file's own stem if the JSON is unreadable.
        project_stem = _project_stem(pro)
        pro_candidate = directory / (project_stem + ".kicad_sch")
        if pro_candidate.exists():
            return pro_candidate

    # Step 3: single-file glob
    sch_files = list(directory.glob("*.kicad_sch"))
    if len(sch_files) == 1:
        return sch_files[0]
    if len(sch_files) > 1:
        logger.warning(
            "Multiple .kicad_sch files found in %s; "
            "cannot determine which is the root schematic. "
            "Use --sch to specify explicitly.",
            directory,
        )
        return None

    # Step 4: no candidates at all
    return None


def _project_stem(pro_path: Path) -> str:
    """Derive the project stem from a ``.kicad_pro`` file.

    Attempts to read the ``meta.filename`` key from the JSON content.
    Falls back to the file's own stem on any error.
    """
    try:
        with open(pro_path, encoding="utf-8") as f:
            data = json.load(f)
        filename = data.get("meta", {}).get("filename", "")
        if filename:
            return Path(filename).stem
    except Exception:
        pass
    return pro_path.stem
