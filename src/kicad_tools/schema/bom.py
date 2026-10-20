"""
Bill of Materials (BOM) generation.

Extracts component information from schematics for manufacturing.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path

from kicad_tools.sexp import SExp

from .schematic import Schematic
from .symbol import SymbolInstance


@dataclass
class BOMItem:
    """A single component in the BOM."""

    reference: str
    value: str
    footprint: str
    lib_id: str
    datasheet: str = ""
    description: str = ""
    manufacturer: str = ""
    mpn: str = ""  # Manufacturer Part Number
    lcsc: str = ""  # LCSC Part Number
    dnp: bool = False
    in_bom: bool = True
    on_board: bool = True
    # True when the symbol's library definition carries KiCad's ``(power)``
    # flag (bare, ``local`` or ``global``), read from the schematic's
    # embedded ``lib_symbols`` (#7615).
    is_power: bool = False

    # Additional properties from schematic
    properties: dict[str, str] = field(default_factory=dict)

    @property
    def is_power_symbol(self) -> bool:
        """Check if this is a power symbol (not a real component).

        KiCad's source of truth is the ``(power)`` flag on the library
        symbol, so any symbol whose embedded ``lib_symbols`` entry carries
        it counts, whatever its library is called -- project-library power
        symbols and PWR_FLAGs included (#7615). Also covers KiCad's stock
        ``power:`` library and kicad-tools' synthesized
        ``kicad_tools_pwr:`` library (used by ``Schematic.add_pwr_symbol``
        for non-stock rails like ``VIN``, ``VMOTOR``, etc. — see #3291).
        As a fallback for files whose ``lib_symbols`` entry is missing, a
        reference starting with ``#`` (KiCad's virtual-designator
        convention: ``#PWR``, ``#FLG``, ...) also marks it virtual.
        """
        if self.is_power:
            return True
        if self.lib_id.startswith(("power:", "kicad_tools_pwr:")):
            return True
        return bool(self.reference) and self.reference.startswith("#")

    @property
    def is_virtual(self) -> bool:
        """Check if this is a virtual component (not placed on PCB)."""
        return not self.in_bom or self.is_power_symbol

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "reference": self.reference,
            "value": self.value,
            "footprint": self.footprint,
            "description": self.description,
            "manufacturer": self.manufacturer,
            "mpn": self.mpn,
            "lcsc": self.lcsc,
            "dnp": self.dnp,
        }


@dataclass
class BOMGroup:
    """A group of identical components."""

    value: str
    footprint: str
    items: list[BOMItem] = field(default_factory=list)

    @property
    def quantity(self) -> int:
        return len(self.items)

    @property
    def references(self) -> str:
        """Comma-separated list of references."""
        refs = sorted(
            self.items,
            key=lambda x: (
                x.reference[0] if x.reference else "",
                int("".join(filter(str.isdigit, x.reference)) or "0"),
            ),
        )
        return ", ".join(item.reference for item in refs)

    @property
    def lcsc(self) -> str:
        """Get LCSC part number (from first item with one)."""
        for item in self.items:
            if item.lcsc:
                return item.lcsc
        return ""

    @property
    def mpn(self) -> str:
        """Get manufacturer part number (from first item with one)."""
        for item in self.items:
            if item.mpn:
                return item.mpn
        return ""

    @property
    def description(self) -> str:
        """Get description (from first item with one)."""
        for item in self.items:
            if item.description:
                return item.description
        return ""

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "value": self.value,
            "footprint": self.footprint,
            "qty": self.quantity,
            "refs": self.references,
            "description": self.description,
            "mpn": self.mpn,
            "lcsc": self.lcsc,
            "items": [item.to_dict() for item in self.items],
        }


@dataclass
class BOM:
    """Complete Bill of Materials."""

    items: list[BOMItem] = field(default_factory=list)
    source: str = ""  # Source schematic path

    @property
    def total_components(self) -> int:
        return len([i for i in self.items if not i.is_virtual and not i.dnp])

    @property
    def unique_parts(self) -> int:
        return len(self.grouped())

    @property
    def dnp_count(self) -> int:
        return len([i for i in self.items if i.dnp])

    def grouped(self, by: str = "value+footprint") -> list[BOMGroup]:
        """
        Group items by specified criteria.

        Args:
            by: Grouping mode - "value+footprint", "value", "footprint", "mpn"

        Returns:
            List of BOMGroup
        """
        groups: dict[str, BOMGroup] = {}

        for item in self.items:
            # Skip virtual components
            if item.is_virtual:
                continue

            # Create group key
            if by == "value+footprint":
                key = f"{item.value}|{item.footprint}"
            elif by == "value":
                key = item.value
            elif by == "footprint":
                key = item.footprint
            elif by == "mpn":
                key = item.mpn or f"{item.value}|{item.footprint}"
            else:
                key = f"{item.value}|{item.footprint}"

            if key not in groups:
                groups[key] = BOMGroup(
                    value=item.value,
                    footprint=item.footprint,
                )
            groups[key].items.append(item)

        # Sort groups by reference prefix then number
        return sorted(
            groups.values(),
            key=lambda g: (
                g.items[0].reference[0] if g.items and g.items[0].reference else "",
                g.value,
            ),
        )

    def filter(
        self,
        include_dnp: bool = False,
        reference_pattern: str | None = None,
    ) -> BOM:
        """
        Return a filtered BOM.

        Args:
            include_dnp: Include DNP components
            reference_pattern: Filter by reference pattern (e.g., "R*", "U*")

        Returns:
            New filtered BOM
        """
        import fnmatch

        filtered = []
        for item in self.items:
            # Skip DNP unless requested
            if item.dnp and not include_dnp:
                continue

            # Skip virtual
            if item.is_virtual:
                continue

            # Apply reference filter
            if reference_pattern and not fnmatch.fnmatch(item.reference, reference_pattern):
                continue

            filtered.append(item)

        return BOM(items=filtered, source=self.source)


def _has_power_flag(lib_sym: SExp) -> bool:
    """True when *lib_sym* has a direct ``(power ...)`` child."""
    return any(not child.is_atom and child.name == "power" for child in lib_sym.children)


def _direct_child(node: SExp, name: str) -> SExp | None:
    """Return *node*'s first direct (non-descendant) child called *name*."""
    return next((c for c in node.children if not c.is_atom and c.name == name), None)


def _is_power_lib_symbol(schematic: Schematic, key: str, *, max_depth: int = 10) -> bool:
    """True when the embedded ``lib_symbols`` entry *key* is a power symbol.

    A definition counts when it carries a direct ``(power)`` child (bare,
    ``local`` or ``global``) or ``(extends "Base")`` a definition that
    does, resolved within the same embedded ``lib_symbols`` (#7615).
    Returns ``False`` when *key* has no embedded definition; callers fall
    back to the ``#`` reference convention for that case.
    """
    name = key
    definition = schematic.get_lib_symbol(name)
    for _ in range(max_depth):
        if definition is None:
            return False
        if _direct_child(definition, "power") is not None:
            return True
        ext = _direct_child(definition, "extends")
        base = ext.get_string(0) if ext is not None else None
        if not base:
            return False
        # ``extends`` names the base without a library prefix; the base is
        # embedded under the derived symbol's library (or, rarely, bare).
        lib_prefix = name.split(":", 1)[0] + ":" if ":" in name else ""
        name = f"{lib_prefix}{base}"
        definition = schematic.get_lib_symbol(name)
        if definition is None and lib_prefix:
            name = base
            definition = schematic.get_lib_symbol(name)
    return False


def _lib_symbol_key(sym: SymbolInstance) -> str:
    """Key of *sym*'s embedded library definition.

    KiCad keys ``lib_symbols`` by the placed symbol's ``(lib_name ...)``
    when present (a locally modified copy), otherwise by its ``lib_id``.
    """
    sexp = sym._sexp
    if sexp is not None:
        lib_name = _direct_child(sexp, "lib_name")
        if lib_name is not None and (value := lib_name.get_string(0)):
            return value
    return sym.lib_id


def _bom_item_from_symbol(
    sym,
    reference: str,
    schematic: Schematic,
    power_by_key: dict[str, bool],
) -> BOMItem | None:
    """Convert one placed symbol to a :class:`BOMItem` (``None`` for power symbols).

    ``reference`` is the designator of this *placement*, which differs from
    ``sym.reference`` when the symbol's sheet file is placed more than once.
    ``schematic`` is the sheet the symbol lives on: its embedded ``lib_symbols``
    decide ``is_power`` (#7615). ``power_by_key`` caches one walk per library
    key and must not be shared across schematics.
    """
    # Skip power symbols
    if sym.lib_id.startswith("power:"):
        return None

    key = _lib_symbol_key(sym)
    if key not in power_by_key:
        power_by_key[key] = _is_power_lib_symbol(schematic, key)

    item = BOMItem(
        reference=reference,
        value=sym.value,
        footprint=sym.footprint,
        lib_id=sym.lib_id,
        datasheet=sym.datasheet,
        dnp=sym.dnp,
        in_bom=sym.in_bom,
        on_board=sym.on_board,
        is_power=power_by_key[key],
    )

    # Extract additional properties
    for name, prop in sym.properties.items():
        value = prop.value

        # Map common property names
        if name.lower() in ("description", "desc"):
            item.description = value
        elif name.lower() in ("manufacturer", "mfr", "mfg"):
            item.manufacturer = value
        elif name.lower() in ("mpn", "mfr_pn", "manufacturer_pn", "pn"):
            item.mpn = value
        elif name.lower() in ("lcsc", "lcsc_pn", "lcsc part", "jlc", "jlcpcb"):
            item.lcsc = value
        else:
            item.properties[name] = value

    return item


def extract_bom_from_schematic(schematic: Schematic) -> list[BOMItem]:
    """Extract BOM items from a single schematic."""
    items = []
    # One ``get_lib_symbol`` walk per distinct library key, not per placement.
    power_by_key: dict[str, bool] = {}

    for sym in schematic.symbols:
        item = _bom_item_from_symbol(sym, sym.reference, schematic, power_by_key)
        if item is not None:
            items.append(item)

    return items


def extract_bom_hierarchical(root_schematic: str) -> BOM:
    """
    Extract BOM from a hierarchical schematic.

    Walks every sheet *placement* (a sheet file placed twice contributes its
    components twice) and uses each placement's own reference designator from
    the symbol's ``(instances ...)`` block, as KiCad does (issue #7616).
    A multi-unit symbol yields one item per placement, not one per unit.

    Args:
        root_schematic: Path to the root .kicad_sch file

    Returns:
        Complete BOM with all components

    Raises:
        FileNotFoundError: If the root schematic does not exist.
        Exception: If the root schematic cannot be parsed. Failing
            sub-sheets are skipped.
    """
    # Imported lazily: lvs imports the schema package.
    from ..lvs.board_lvs import _instance_reference_map, _walk_hierarchy_schematics

    # The root sheet must load: a missing or unparseable root is an error,
    # not an empty BOM. Only failing sub-sheets are skipped (with their
    # subtree) by the walker.
    Schematic.load(root_schematic)

    visits = list(_walk_hierarchy_schematics(Path(root_schematic), skip_unloadable=True))
    placements: dict[Path, int] = {}
    for visit in visits:
        key = visit.source.resolve()
        placements[key] = placements.get(key, 0) + 1

    all_items: list[BOMItem] = []
    for visit in visits:
        # The walker yields its own Schematic model; BOM needs this module's.
        sch = Schematic.load(str(visit.source))
        # Only a re-placed sheet file needs per-placement renaming.
        renames = _instance_reference_map(visit) if placements[visit.source.resolve()] > 1 else {}
        power_by_key: dict[str, bool] = {}
        first_unit: dict[str, int] = {}
        for sym in sch.symbols:
            ref = renames.get(sym.reference, sym.reference)
            # Collapse only a further *unit* of an annotated multi-unit part.
            # Unannotated parts all share ``R?`` and each is its own line.
            if ref and not ref.endswith("?"):
                if ref in first_unit and sym.unit != first_unit[ref]:
                    continue
                first_unit.setdefault(ref, sym.unit)
            item = _bom_item_from_symbol(sym, ref, sch, power_by_key)
            if item is not None:
                all_items.append(item)

    return BOM(items=all_items, source=root_schematic)


def extract_bom(schematic_path: str, hierarchical: bool = True) -> BOM:
    """
    Extract BOM from a schematic.

    Args:
        schematic_path: Path to .kicad_sch file
        hierarchical: If True, include all sub-sheets

    Returns:
        BOM object
    """
    if hierarchical:
        return extract_bom_hierarchical(schematic_path)
    else:
        sch = Schematic.load(schematic_path)
        items = extract_bom_from_schematic(sch)
        return BOM(items=items, source=schematic_path)


def extract_bom_from_pcb(pcb_path: str) -> BOM:
    """
    Extract BOM from PCB footprints instead of schematic symbols.

    This is useful when the schematic and PCB have divergent reference
    designators (e.g., partial builds, re-annotations, external designs).

    Footprint properties such as LCSC, MPN, Manufacturer, and Description
    are extracted when present. Missing fields generate warnings but do
    not cause failures.

    Args:
        pcb_path: Path to .kicad_pcb file

    Returns:
        BOM object built from PCB footprints
    """
    import logging

    from .pcb import PCB

    logger = logging.getLogger(__name__)

    pcb = PCB.load(pcb_path)
    items: list[BOMItem] = []

    for fp in pcb.footprints:
        # Skip footprints excluded from BOM
        if fp.exclude_from_bom:
            continue

        # Warn about missing fields
        if not fp.value:
            logger.warning(
                "PCB footprint %s has empty value field; BOM entry may be incomplete",
                fp.reference,
            )
        if not fp.name:
            logger.warning(
                "PCB footprint %s has empty footprint name; BOM entry may be incomplete",
                fp.reference,
            )

        # Map footprint properties to BOM fields
        props = fp.properties
        description = ""
        manufacturer = ""
        mpn = ""
        lcsc = ""
        extra_props: dict[str, str] = {}

        for name, value in props.items():
            name_lower = name.lower()
            if name_lower in ("description", "desc"):
                description = value
            elif name_lower in ("manufacturer", "mfr", "mfg"):
                manufacturer = value
            elif name_lower in ("mpn", "mfr_pn", "manufacturer_pn", "pn"):
                mpn = value
            elif name_lower in ("lcsc", "lcsc_pn", "lcsc part", "jlc", "jlcpcb"):
                lcsc = value
            else:
                extra_props[name] = value

        item = BOMItem(
            reference=fp.reference,
            value=fp.value,
            footprint=fp.name,
            lib_id="",  # No lib_id available from PCB
            description=description,
            manufacturer=manufacturer,
            mpn=mpn,
            lcsc=lcsc,
            dnp=fp.dnp,
            in_bom=True,
            properties=extra_props,
        )
        items.append(item)

    return BOM(items=items, source=pcb_path)


def backfill_footprints_from_pcb(items: list[BOMItem], pcb_path: str | Path) -> int:
    """Fill blank ``BOMItem.footprint`` fields from the PCB's footprint refs.

    This is a strict fallback: only items whose ``footprint`` is empty or
    whitespace are touched.  Items with any non-empty footprint string
    (typically populated by the schematic-side ``Footprint`` property) are
    preserved verbatim, so schematic-driven boards see no change.

    Use case: schematics produced by spec-overlay / programmatic-generation
    workflows often leave the instance-level ``Footprint`` property empty
    on each symbol, even though the PCB has the correct footprint assigned.
    The CPL writer reads ``fp.name`` directly off the PCB and is therefore
    immune; the BOM writer reads ``BOMItem.footprint`` and ends up emitting
    blank ``Footprint`` columns.  Calling this helper right after
    :func:`extract_bom` repairs the BOM stream before grouping and CSV
    output.

    Args:
        items: BOM items to back-fill in place.
        pcb_path: Path to the ``.kicad_pcb`` file to source footprint refs
            from.

    Returns:
        Number of items whose ``footprint`` field was populated.  Useful
        for logging and test assertions; never raises on a no-op.
    """
    from .pcb import PCB

    pcb = PCB.load(str(pcb_path))
    ref_to_fp_name: dict[str, str] = {fp.reference: fp.name for fp in pcb.footprints}

    filled = 0
    for item in items:
        if (item.footprint or "").strip():
            # Preserve any existing schematic-side metadata.
            continue
        fp_name = ref_to_fp_name.get(item.reference)
        if fp_name:
            item.footprint = fp_name
            filled += 1
    return filled


def load_bom_csv(path: str | Path) -> BOM:
    """Load a BOM from a CSV file.

    Recognises the common column spellings (Reference/Designator, Value/Comment,
    Footprint/Package, LCSC/JLC Part, MPN/Part Number).  Rows without a reference
    are skipped.  Raises ``OSError`` / ``UnicodeDecodeError`` / ``csv.Error`` for
    unreadable input; an input with no usable rows yields an empty BOM.
    """
    items: list[BOMItem] = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            # Try to find common column names
            reference = (
                row.get("Reference")
                or row.get("reference")
                or row.get("Designator")
                or row.get("designator")
                or ""
            )
            value = (
                row.get("Value")
                or row.get("value")
                or row.get("Comment")
                or row.get("comment")
                or ""
            )
            footprint = (
                row.get("Footprint")
                or row.get("footprint")
                or row.get("Package")
                or row.get("package")
                or ""
            )
            lcsc = (
                row.get("LCSC")
                or row.get("lcsc")
                or row.get("LCSC Part")
                or row.get("JLC Part")
                or ""
            )
            mpn = (
                row.get("MPN")
                or row.get("mpn")
                or row.get("Manufacturer Part")
                or row.get("Part Number")
                or ""
            )

            if reference:
                items.append(
                    BOMItem(
                        reference=reference,
                        value=value,
                        footprint=footprint,
                        lib_id="",
                        lcsc=lcsc,
                        mpn=mpn,
                    )
                )

    return BOM(items=items, source=str(path))
