"""Committed net-class-map sidecar resolution for report generation.

Part B of #4008. ``kct export``'s report.md DRC section runs
``ManufacturingAudit`` with ``net_class_map=None``, so the three
sidecar-gated rule families (``diffpair_length_skew``,
``diffpair_routing_continuity``, ``match_group_length_skew``) silently
no-op — report.md printed "Errors 0 / PASS" on boards with real blocking
diff-pair / match-group errors.

This module provides the **tier-1** resolution the report layer needs:
look for a committed ``net_class_map.json`` sidecar next to the routed PCB
and return its path so it can be forwarded to ``ManufacturingAudit`` (which
already loads a JSON sidecar via its ``net_class_map_path`` argument).

Why tier-1 only (per #4031 curation): report.md generation runs after the
board's own ``generate_design.py`` pipeline in the normal ``kct export``
flow, so the committed sidecar is the case that matters for report accuracy.
The tier-2 in-process derivation in
``scripts/ci/net_class_map_resolver.py`` exists specifically for the CI
dual-gate-counter scenario (#4008 / PR #4029), not for ``kct export``.

Crucially, this deliberately does NOT import from ``scripts/ci/`` —
that path has no ``__init__.py`` and is excluded from the installed package
(``pip install kicad-tools``), so importing it from ``src/kicad_tools/``
would break wheel installs.

Issue #4634: the *filename* probe is no longer reimplemented here. It was
a fourth independent copy that fell out of sync when #4601/PR #4629 taught
``kct check`` about the stem-keyed ``<pcb_stem>.net_class_map.json``
convention, so in a directory holding both forms this surface loaded a
different file (and therefore a different rule set) than ``kct check``.
The names and their in-directory precedence now come from the stdlib-only
leaf module :mod:`kicad_tools.sidecars`, which both this module and the
``scripts/ci`` gates import. The **directory** scope stays tier-1-only
(the PCB's own directory) — that part is intentionally unchanged.
"""

from __future__ import annotations

from pathlib import Path

from kicad_tools.sidecars import (
    NET_CLASS_MAP_SIDECAR_BASENAME,
    first_existing_net_class_map_sidecar,
)

# Canonical *bare* name of the committed sidecar a board's generate_design.py
# may emit next to its routed PCB (Phase 3M pattern; boards 03/06/07 emit one).
# Re-exported from kicad_tools.sidecars, which is now the single source of
# truth shared with scripts/ci/net_class_map_resolver.SIDECAR_FILENAME.
SIDECAR_FILENAME = NET_CLASS_MAP_SIDECAR_BASENAME


def resolve_committed_net_class_map(pcb_path: str | Path) -> Path | None:
    """Return the committed net-class-map sidecar for a routed PCB.

    Looks in ``<pcb_dir>`` only — the same tier-1 directory scope as
    ``scripts.ci.net_class_map_resolver.resolve_net_class_map_sidecar``.
    Within that directory the stem-keyed ``<pcb_stem>.net_class_map.json``
    wins over the bare ``net_class_map.json`` (Issue #4634; see
    :func:`kicad_tools.sidecars.net_class_map_sidecar_names`).

    Args:
        pcb_path: Path to a routed ``*.kicad_pcb`` file.

    Returns:
        The sidecar ``Path`` if a committed sidecar exists next to the PCB,
        otherwise ``None`` (the report then keeps its graceful no-op behavior
        for the three sidecar-gated DRC rule families).
    """
    # The directory is resolved (unchanged tier-1 behaviour); the *stem* is
    # taken from the path as given, so a symlinked PCB looks for the sidecar
    # named after the name the caller used -- the same stem semantics
    # ``kct check``'s probe uses.
    pcb = Path(pcb_path)
    return first_existing_net_class_map_sidecar([pcb.resolve().parent], pcb.stem)
