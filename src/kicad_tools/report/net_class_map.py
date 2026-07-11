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
would break wheel installs. The tier-1 logic here is intentionally a small,
self-contained reimplementation of that resolver's first tier.
"""

from __future__ import annotations

from pathlib import Path

# Canonical name of the committed sidecar a board's generate_design.py may
# emit next to its routed PCB (Phase 3M pattern; boards 03/06/07 emit one).
# Kept identical to scripts/ci/net_class_map_resolver.SIDECAR_FILENAME.
SIDECAR_FILENAME = "net_class_map.json"


def resolve_committed_net_class_map(pcb_path: str | Path) -> Path | None:
    """Return the committed ``net_class_map.json`` sidecar for a routed PCB.

    Looks for ``<pcb_dir>/net_class_map.json`` adjacent to ``pcb_path`` —
    the same tier-1 lookup as
    ``scripts.ci.net_class_map_resolver.resolve_net_class_map_sidecar``.

    Args:
        pcb_path: Path to a routed ``*.kicad_pcb`` file.

    Returns:
        The sidecar ``Path`` if a committed sidecar exists next to the PCB,
        otherwise ``None`` (the report then keeps its graceful no-op behavior
        for the three sidecar-gated DRC rule families).
    """
    committed = Path(pcb_path).resolve().parent / SIDECAR_FILENAME
    if committed.is_file():
        return committed
    return None
