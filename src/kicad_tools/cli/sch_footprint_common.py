#!/usr/bin/env python3
"""Shared helpers for footprint-aware schematic commands.

The two callers today are:

* ``sch_assign_footprints.run_assign_footprints`` — bulk auto-assignment.
* ``sch_validate.check_missing_footprints`` — preflight enumeration.

Both need to walk the schematic hierarchy and yield the same set of
"missing-footprint" symbols. Centralising the predicate here keeps the
two paths in lock-step: if one filter (power, DNP) drifts, the other
silently drifts with it, which previously masked real gaps.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from kicad_tools.schema import Schematic
from kicad_tools.schema.hierarchy import build_hierarchy


def is_missing_footprint(sym: Any) -> bool:
    """Return ``True`` when a symbol has no concrete footprint assigned.

    KiCad emits ``""`` for fresh placements and ``"~"`` as the legacy
    placeholder; both are treated as "missing". A real ``Library:Name``
    string is considered assigned even if the library does not exist on
    disk — that is a different problem (handled by ``set-footprint`` /
    DRC), not "missing".
    """
    fp = (sym.footprint or "").strip()
    return fp == "" or fp == "~"


def should_skip_symbol(
    sym: Any,
    *,
    include_power: bool = False,
    include_dnp: bool = False,
) -> bool:
    """Mirror ``check_missing_footprints``'s skip rules.

    By default power symbols and ``dnp`` (Do-Not-Populate) symbols are
    skipped — they correctly have no footprint. Callers that need to
    surface every reference (e.g. a "force-everything" audit pass) can
    opt in via the keyword arguments.

    "Power symbol" matches the same convention as
    :attr:`kicad_tools.schema.bom.BOMItem.is_power_symbol`: KiCad's stock
    ``power:`` library, kicad-tools' synthesized ``kicad_tools_pwr:``
    library (used by ``Schematic.add_pwr_symbol`` for non-stock rails
    like ``VIN``/``VMOTOR`` — see #3291), and any symbol whose reference
    carries the virtual ``#PWR`` designator. These are footprint-less by
    design and must never trip the missing-footprint gate (#3866).
    """
    if not include_power and _is_power_symbol(sym):
        return True
    if not include_dnp and getattr(sym, "dnp", False):
        return True
    return False


# Power/virtual library prefixes treated as footprint-less by design.
# Kept in lock-step with ``BOMItem.is_power_symbol`` (schema/bom.py).
_POWER_LIB_PREFIXES = ("power:", "kicad_tools_pwr:")


def _is_power_symbol(sym: Any) -> bool:
    """Return ``True`` for power/virtual symbols (no footprint by design).

    Mirrors :attr:`kicad_tools.schema.bom.BOMItem.is_power_symbol`: any
    stock ``power:`` or synthesized ``kicad_tools_pwr:`` library symbol,
    plus the legacy ``#PWR`` reference prefix as a fallback for symbols
    whose ``lib_id`` was stripped or rewritten on load.
    """
    lib_id = getattr(sym, "lib_id", "") or ""
    if lib_id.startswith(_POWER_LIB_PREFIXES):
        return True
    ref = getattr(sym, "reference", "") or ""
    return ref.startswith("#PWR")


def iter_missing_footprint_symbols(
    schematic_path: Path | str,
    *,
    include_power: bool = False,
    include_dnp: bool = False,
    include_assigned: bool = False,
    on_sheet_error: Callable[[Any, Exception], None] | None = None,
) -> Iterator[tuple[Any, Any, Schematic]]:
    """Yield ``(node, sym, sch)`` for every symbol needing a footprint.

    Walks the full hierarchy via :func:`build_hierarchy`, loads each
    sheet's schematic, and applies the same skip rules as
    ``check_missing_footprints`` (power, dnp). Sheets that fail to load
    are skipped; callers that need to surface those (the preflight path
    emits ``info`` severity issues) can pass ``on_sheet_error``.

    Args:
        schematic_path: Path to the root ``.kicad_sch`` file.
        include_power: Yield power symbols (default skip).
        include_dnp: Yield DNP symbols (default skip).
        include_assigned: Yield every symbol regardless of footprint
            state. The default (``False``) restricts the iteration to
            symbols whose footprint is empty or ``~``. ``--force`` in
            the assign-footprints CLI flips this to ``True``.
        on_sheet_error: Optional callback invoked as
            ``on_sheet_error(node, exc)`` for every sheet that fails to
            load, before that sheet is skipped. Lets callers surface
            per-sheet load failures without re-walking the hierarchy.

    Yields:
        Triples of ``(node, sym, sch)`` where ``node`` is the
        :class:`HierarchyNode`, ``sym`` is the parsed symbol instance,
        and ``sch`` is the loaded :class:`Schematic` (caches one
        load per sheet so callers can reuse it for
        ``_resolve_target_pin_count`` / ``_get_fp_filters``).
    """
    root = build_hierarchy(str(schematic_path))
    for node in root.all_nodes():
        try:
            sch = Schematic.load(node.path)
        except Exception as exc:
            if on_sheet_error is not None:
                on_sheet_error(node, exc)
            continue
        for sym in sch.symbols:
            if should_skip_symbol(
                sym,
                include_power=include_power,
                include_dnp=include_dnp,
            ):
                continue
            if not include_assigned and not is_missing_footprint(sym):
                continue
            yield node, sym, sch
