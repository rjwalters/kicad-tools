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


def unresolved_schematic_candidates(pcb_path: str | Path) -> list[Path]:
    """The candidate root schematics that made resolution ambiguous (Issue #7147).

    Meant for the case where :func:`resolve_schematic_for_pcb` returned
    ``None``: more than one entry means several candidates were found and the
    resolver refused to guess; zero or one means nothing usable was found.
    Paired roots (step 3) take precedence over the bare sibling list (step 4),
    mirroring the resolver's own order.
    """
    project_dir = Path(pcb_path).parent
    paired = _paired_root_schematics(project_dir)
    if paired:
        return paired
    return sorted(project_dir.glob("*.kicad_sch"))


def resolve_target_fab_for_pcb(pcb_path: str | Path) -> str | None:
    """Resolve the target fabricator profile declared for a PCB (issue #3920).

    A routed ``.kicad_pcb`` carries no embedded fab-tier hint, so bare
    ``kct check`` cannot tell which manufacturer profile a board targets and
    defaults to the base ``jlcpcb`` tier -- reporting a false ``FAILED`` on
    boards that route legal, tier-gated geometry (e.g. via-in-pad, which is
    legal at ``jlcpcb-tier1``).  This helper lets ``kct check`` consult the
    board's ``project.kct`` for its declared ``target_fab`` as one input to the
    effective ``--mfr`` resolution, mirroring how ``resolve_schematic_for_pcb``
    already opens the same file for ``artifacts.schematic``.

    Looks for ``project.kct`` next to the PCB, then one directory up (the same
    search order as :func:`resolve_schematic_for_pcb`), and reads
    ``requirements.manufacturing.target_fab``.

    Args:
        pcb_path: Path to a ``.kicad_pcb`` file.

    Returns:
        The declared ``target_fab`` profile id (e.g. ``"jlcpcb-tier1"``) if a
        ``project.kct`` declares one, else ``None``.
    """
    pcb_path = Path(pcb_path)
    project_dir = pcb_path.parent

    kct_path = project_dir / "project.kct"
    if not kct_path.exists():
        kct_path = project_dir.parent / "project.kct"

    if not kct_path.exists():
        return None

    try:
        from kicad_tools.spec import load_spec

        spec = load_spec(kct_path)
        if (
            spec.requirements
            and spec.requirements.manufacturing
            and spec.requirements.manufacturing.target_fab
        ):
            target_fab = spec.requirements.manufacturing.target_fab
            logger.debug("Using target_fab from project.kct: %s", target_fab)
            return target_fab
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("Failed to read target_fab from project.kct: %s", e)

    return None


def _project_kct_for_pcb(pcb_path: Path) -> Path | None:
    """``project.kct`` next to the PCB, else one directory up (same search order)."""
    kct_path = pcb_path.parent / "project.kct"
    if not kct_path.exists():
        kct_path = pcb_path.parent.parent / "project.kct"
    return kct_path if kct_path.exists() else None


def resolve_declared_currents_for_pcb(pcb_path: str | Path) -> list[str]:
    """Labels of the current requirements a PCB's ``project.kct`` declares (issue #7206).

    kct never infers *which net* carries a spec current (``target_ampacity``
    stays declarative); this only answers "does the spec claim a current at
    all?", so an absent ``target_ampacity`` can be reported ``unknown`` rather
    than a vacuous ``skipped``.  A value counts as declared when it is
    non-null; it need not parse (``"4.6mA per LED at 5V"``).

    Sources: ``intent.interfaces[].current_max``,
    ``requirements.electrical.input.current``,
    ``requirements.electrical.outputs[].current_max`` and any other
    ``requirements.electrical`` key containing ``current`` (``motor_current``).

    Returns ``[]`` when no ``project.kct`` is found or it is unreadable.
    """
    kct_path = _project_kct_for_pcb(Path(pcb_path))
    if kct_path is None:
        return []
    try:
        import yaml  # type: ignore[import-untyped]

        data = yaml.safe_load(kct_path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001 - unreadable spec keeps today's behaviour
        logger.debug("Failed to read declared currents from project.kct: %s", e)
        return []
    if not isinstance(data, dict):
        return []

    found: list[str] = []

    def _dict(value: object) -> dict:
        return value if isinstance(value, dict) else {}

    def _list(value: object) -> list:
        return value if isinstance(value, list) else []

    for i, iface in enumerate(_list(_dict(data.get("intent")).get("interfaces"))):
        if isinstance(iface, dict) and iface.get("current_max") is not None:
            found.append(f"interfaces[{iface.get('name', i)}].current_max")

    electrical = _dict(_dict(data.get("requirements")).get("electrical"))
    if _dict(electrical.get("input")).get("current") is not None:
        found.append("input.current")
    for i, out in enumerate(_list(electrical.get("outputs"))):
        if isinstance(out, dict) and out.get("current_max") is not None:
            found.append(f"outputs[{out.get('rail', i)}].current_max")
    for key, value in electrical.items():
        if key not in ("input", "outputs") and "current" in str(key) and value is not None:
            found.append(str(key))
    return found
