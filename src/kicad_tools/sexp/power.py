"""Shared power-symbol predicate helpers (#7615, #7637).

KiCad's source of truth for "is this a power symbol" is the ``(power)``
flag on the library symbol, read from the schematic's embedded
``lib_symbols``. Every predicate in kicad-tools that needs the answer
(BOM, missing-footprint gate, parser routing, ``fix-annotation``) goes
through this module so they cannot drift apart.

The helpers take plain S-expression nodes (or a lookup callable) rather
than ``Schematic`` objects so that both ``kicad_tools.schema`` and
``kicad_tools.schematic`` can import them without a cycle.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

from .parser import SExp

#: Library prefixes that always denote power symbols, regardless of whether
#: the embedded ``lib_symbols`` entry is available (stock KiCad ``power:`` and
#: kicad-tools' synthesized ``kicad_tools_pwr:``, see #3291).
POWER_LIB_PREFIXES = ("power:", "kicad_tools_pwr:")

LibSymbolLookup = Callable[[str], "SExp | None"]


def direct_child(node: SExp, name: str) -> SExp | None:
    """Return *node*'s first direct (non-descendant) child called *name*."""
    return next((c for c in node.children if not c.is_atom and c.name == name), None)


def has_power_flag(lib_sym: SExp) -> bool:
    """True when *lib_sym* has a direct ``(power ...)`` child."""
    return direct_child(lib_sym, "power") is not None


def is_power_lib_symbol(lookup: LibSymbolLookup, key: str, *, max_depth: int = 10) -> bool:
    """True when the embedded ``lib_symbols`` entry *key* is a power symbol.

    A definition counts when it carries a direct ``(power)`` child (bare,
    ``local`` or ``global``) or ``(extends "Base")`` a definition that
    does, resolved via *lookup* (``key -> lib_symbols entry | None``).
    Returns ``False`` when *key* has no embedded definition; callers fall
    back to the ``#`` reference convention for that case.
    """
    name = key
    definition = lookup(name)
    for _ in range(max_depth):
        if definition is None:
            return False
        if has_power_flag(definition):
            return True
        ext = direct_child(definition, "extends")
        base = ext.get_string(0) if ext is not None else None
        if not base:
            return False
        # ``extends`` names the base without a library prefix; the base is
        # embedded under the derived symbol's library (or, rarely, bare).
        lib_prefix = name.split(":", 1)[0] + ":" if ":" in name else ""
        name = f"{lib_prefix}{base}"
        definition = lookup(name)
        if definition is None and lib_prefix:
            name = base
            definition = lookup(name)
    return False


def lib_symbol_key(placed: SExp | None, lib_id: str) -> str:
    """Key of a placed symbol's embedded library definition.

    KiCad keys ``lib_symbols`` by the placed symbol's ``(lib_name ...)``
    when present (a locally modified copy), otherwise by its ``lib_id``.
    """
    if placed is not None:
        lib_name = direct_child(placed, "lib_name")
        if lib_name is not None and (value := lib_name.get_string(0)):
            return value
    return lib_id


def lookup_from_nodes(lib_symbols: Iterable[SExp]) -> LibSymbolLookup:
    """Build a *lookup* over an iterable of ``lib_symbols`` ``(symbol ...)`` nodes."""
    by_name = {s.get_string(0): s for s in lib_symbols}
    return by_name.get


def is_power_symbol(
    lib_id: str,
    reference: str = "",
    *,
    lookup: LibSymbolLookup | None = None,
    placed: SExp | None = None,
    flagged: bool | None = None,
) -> bool:
    """Single power-symbol predicate shared by every call site.

    A symbol is a power symbol when, in order:

    1. *flagged* (a precomputed library ``(power)`` result) is true, or
       *lookup* resolves its embedded definition to a power symbol;
    2. its ``lib_id`` is in the stock ``power:`` / ``kicad_tools_pwr:``
       libraries (the documented fallback when no embedded definition is
       available, e.g. before a symbol has been placed);
    3. its reference starts with ``#`` (KiCad's virtual-designator
       convention: ``#PWR``, ``#FLG``, ...).
    """
    if flagged is None and lookup is not None:
        flagged = is_power_lib_symbol(lookup, lib_symbol_key(placed, lib_id))
    if flagged:
        return True
    if lib_id.startswith(POWER_LIB_PREFIXES):
        return True
    return reference.startswith("#")
