"""Schematic discovery helper for PCB-only command entry points.

Commands like ``kct route`` and ``kct check`` receive only a ``.kicad_pcb``
path, but schematic/PCB drift detection (the :class:`Reconciler`) needs both
files.  This module factors out the single schematic-resolution heuristic so
``route``, ``check``, and ``audit`` all agree on where the schematic lives.

Resolution order (matches the historical ``Auditor._resolve_schematic_path``,
extended by issue #4350 to tolerate versioned board basenames):
    1. ``project.kct`` ``artifacts.schematic`` (looked up next to the PCB, then
       one directory up), if it points at an existing file.
    2. Sibling ``<pcb-basename>.kicad_sch`` (stage-stripped stem first, then the
       raw stem).
    3. Project-pairing fallback: a sibling ``<pro-stem>.kicad_sch`` that is
       paired with a ``<pro-stem>.kicad_pro`` project file.  A KiCad
       ``.kicad_pro`` does not store the root-schematic path as an explicit
       field -- the root schematic is the by-convention ``<project-stem>.kicad_sch``
       paired with ``<project-stem>.kicad_pro`` -- so pairing on the project
       stem is how "the schematic referenced by the board's project" is
       realized in practice.  Child/sub-sheets have ``.kicad_sch`` files but no
       matching ``.kicad_pro``, so this naturally excludes them.  Only fires
       when exactly one distinct paired root schematic exists.
    4. Sole-schematic fallback: if no paired root schematic is found and there
       is exactly one ``*.kicad_sch`` in the directory, use it (flat
       single-file projects).

A PCB basename may carry pipeline-stage suffixes (``_routed``, ``_optimized``,
``_stitched``, ``_phase1``) that the schematic never has, so those are stripped
before the sibling lookup.  Version suffixes (``_v24``, ``_v23_mfg``, ...) are
*not* stripped -- arbitrary version schemes are too brittle to enumerate -- so
versioned board artifacts are resolved via the scheme-agnostic ``.kicad_pro``
pairing (step 3) instead.

**Ambiguity guard:** when steps 3-4 find more than one candidate root schematic
and none matches the board stem, this returns ``None`` rather than guessing.
Comparing copper against the *wrong* schematic is worse than skipping; callers
should require an explicit ``--schematic`` in that case.

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


def _paired_root_schematics(project_dir: Path) -> list[Path]:
    """Return sibling ``.kicad_sch`` files paired with a matching ``.kicad_pro``.

    A KiCad root schematic is paired with a project file of the same stem
    (``foo.kicad_pro`` + ``foo.kicad_sch``).  Child/sub-sheets have a
    ``.kicad_sch`` but no matching ``.kicad_pro``, so pairing on the stem
    isolates the root schematic(s).  Returns a de-duplicated, sorted list.
    """
    candidates: set[Path] = set()
    for pro in project_dir.glob("*.kicad_pro"):
        sch = pro.with_suffix(".kicad_sch")
        if sch.exists():
            candidates.add(sch)
    return sorted(candidates)


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

    # 3. Project-pairing fallback (issue #4350).  Versioned board basenames
    #    (``board_v24.kicad_pcb``) never match the unversioned root schematic
    #    by stem, so look for a ``.kicad_sch`` paired with a sibling
    #    ``.kicad_pro`` of the same stem -- the by-convention root schematic.
    #    Only resolve when exactly one distinct candidate exists; more than one
    #    (and none matched the board stem in step 2) is genuinely ambiguous, so
    #    return ``None`` rather than comparing copper against the wrong design.
    paired = _paired_root_schematics(project_dir)
    if len(paired) == 1:
        logger.debug("Using project-paired root schematic: %s", paired[0])
        return paired[0]
    if len(paired) > 1:
        logger.debug(
            "Ambiguous schematic discovery: %d paired root schematics, none "
            "matching board stem %r; returning None (pass --schematic)",
            len(paired),
            pcb_path.stem,
        )
        return None

    # 4. Sole-schematic fallback.  A flat single-file project has one
    #    ``.kicad_sch`` and (perhaps) no paired ``.kicad_pro`` at all; if there
    #    is exactly one schematic in the directory, use it.  Guard on count == 1
    #    -- hierarchical designs have several sibling child sheets and must not
    #    be resolved by guessing.
    all_sch = sorted(project_dir.glob("*.kicad_sch"))
    if len(all_sch) == 1:
        logger.debug("Using sole schematic in directory: %s", all_sch[0])
        return all_sch[0]

    return None
