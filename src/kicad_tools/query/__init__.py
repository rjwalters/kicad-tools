"""Fluent query interface for KiCad elements.

Provides Django-ORM-style query interface for filtering collections
of symbols, footprints, and other KiCad elements.

Example:
    from kicad_tools import Schematic

    sch = Schematic.load("project.kicad_sch")

    # Direct shortcuts
    u1 = sch.symbols.by_reference("U1")
    caps = sch.symbols.filter(value="100nF")

    # Query object for complex filtering
    power_ics = sch.symbols.query() \\
        .filter(reference__startswith="U") \\
        .exclude(lib_id__startswith="power:") \\
        .order_by("reference") \\
        .all()

    # Filter operations support Django-style lookups:
    # - exact match: value="100nF"
    # - contains: value__contains="100"
    # - startswith: reference__startswith="C"
    # - endswith: value__endswith="nF"
    # - in: value__in=["100nF", "10nF"]
    # - regex: value__regex=r"\\d+nF"
    # - gt/lt/gte/lte: rotation__gt=0
    # - icontains/iexact: case-insensitive variants
"""

from .base import BaseQuery
from .footprints import FootprintList, FootprintQuery
from .symbols import SymbolList, SymbolQuery

__all__ = [
    "BaseQuery",
    "SymbolQuery",
    "SymbolList",
    "FootprintQuery",
    "FootprintList",
]
