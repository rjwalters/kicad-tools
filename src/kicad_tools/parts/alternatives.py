"""Offline Basic/Preferred JLCPCB alternative suggestions.

Pure ranking helpers used by ``kct parts alternatives``. They operate on
:class:`~kicad_tools.parts.models.Part` objects so they can be unit-tested
without sqlite; the catalog query itself lives in
:func:`suggest_alternatives`.

Ranking rule: candidates must be in stock and not the source part; they are
ordered Basic, then Preferred, then Extended, then lowest unit price, then
highest stock. Only candidates with a strictly better library tier than the
source, or the same tier at a lower unit price, are suggested.
"""

from __future__ import annotations

import re

from .jlcparts_catalog import JlcpartsCatalog
from .models import Part

_TIER_BASIC = 0
_TIER_PREFERRED = 1
_TIER_EXTENDED = 2

_TIER_NAMES = {_TIER_BASIC: "basic", _TIER_PREFERRED: "preferred", _TIER_EXTENDED: "extended"}

# Candidate pool size pulled from the catalog before ranking in Python.
_POOL_LIMIT = 500


def library_tier(part: Part) -> int:
    """Return 0 for Basic, 1 for Preferred, 2 for Extended."""
    if part.is_basic:
        return _TIER_BASIC
    if part.is_preferred:
        return _TIER_PREFERRED
    return _TIER_EXTENDED


def library_tier_name(part: Part) -> str:
    """Return ``"basic"``, ``"preferred"`` or ``"extended"``."""
    return _TIER_NAMES[library_tier(part)]


def _price_key(part: Part) -> float:
    price = part.best_price
    return price if price is not None else float("inf")


def rank_alternatives(source: Part, candidates: list[Part], limit: int = 5) -> list[Part]:
    """Filter and rank *candidates* as substitutes for *source*.

    Excludes the source itself and out-of-stock parts, keeps only candidates
    with a strictly better library tier or the same tier at a lower unit
    price, and orders them Basic > Preferred > Extended, then cheapest, then
    highest stock.
    """
    src_tier = library_tier(source)
    src_price = _price_key(source)
    kept = []
    for cand in candidates:
        if cand.lcsc_part.upper() == source.lcsc_part.upper() or cand.stock <= 0:
            continue
        tier = library_tier(cand)
        if tier < src_tier or (tier == src_tier and _price_key(cand) < src_price):
            kept.append(cand)
    kept.sort(key=lambda p: (library_tier(p), _price_key(p), -p.stock, p.lcsc_part))
    return kept[: max(limit, 0)]


def _value_term(source: Part) -> str:
    """Pick the search term identifying the part's value/family.

    The jlcparts schema has no value column, so use ``Part.value`` when set,
    else the first description token containing a digit (e.g. ``100nF``,
    ``10kΩ``), else the first description token.
    """
    if source.value:
        return source.value
    tokens = source.description.split()
    for token in tokens:
        if re.search(r"\d", token):
            return token
    return tokens[0] if tokens else source.mfr_part


def suggest_alternatives(catalog: JlcpartsCatalog, source: Part, limit: int = 5) -> list[Part]:
    """Query *catalog* for in-stock substitutes for *source* and rank them."""
    term = _value_term(source)
    if not term:
        return []
    candidates = catalog.search(
        term, package=source.package or None, min_stock=1, limit=_POOL_LIMIT
    )
    return rank_alternatives(source, candidates, limit)
