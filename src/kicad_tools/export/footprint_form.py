"""Converge BOM ``Footprint`` and CPL ``Package`` library-prefix forms (issue #8569).

The BOM reads ``BOMItem.footprint`` (schematic ``lib:name``) while the CPL
reads the PCB footprint name, so the two files can carry the same footprint
in different forms (``Lib:C_0402`` vs ``C_0402``).  The committed bundles and
rotation tests rely on the qualified ``lib:name`` form, so when exactly one
side is bare and the bare names agree, the qualified form wins on both sides.
A genuine mismatch (different bare names) is never rewritten.
"""

from __future__ import annotations

from collections.abc import Iterable


def _bare(name: str) -> str:
    return name.split(":", 1)[1] if ":" in name else name


def reconcile_footprint(bom_fp: str, pcb_fp: str) -> str | None:
    """Return the qualified form to use on both sides, or ``None`` for no change.

    Only acts when exactly one side is qualified and the bare names match.
    """
    bom_fp = (bom_fp or "").strip()
    pcb_fp = (pcb_fp or "").strip()
    if not bom_fp or not pcb_fp or bom_fp == pcb_fp:
        return None
    if (":" in bom_fp) == (":" in pcb_fp):
        return None
    if _bare(bom_fp) != _bare(pcb_fp):
        return None
    return bom_fp if ":" in bom_fp else pcb_fp


def reconcile_bom_items(items: Iterable, pcb_names: dict[str, str]) -> dict[str, str]:
    """Qualify bare BOM footprints in place; return CPL overrides by reference.

    Args:
        items: BOM items (``reference`` / ``footprint`` attributes).
        pcb_names: PCB footprint name per reference.

    Returns:
        ``{reference: qualified}`` for refs whose *PCB* (CPL) side is bare and
        must be qualified to match the BOM.
    """
    cpl_overrides: dict[str, str] = {}
    for item in items:
        pcb_fp = pcb_names.get(item.reference, "")
        qualified = reconcile_footprint(item.footprint, pcb_fp)
        if qualified is None:
            continue
        if qualified != item.footprint:
            item.footprint = qualified
        if qualified != pcb_fp:
            cpl_overrides[item.reference] = qualified
    return cpl_overrides
