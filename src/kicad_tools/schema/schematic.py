"""
Schematic document model.

Provides a high-level interface to KiCad schematic files.
"""

from __future__ import annotations

import uuid as uuid_mod
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from kicad_tools.sexp import SExp

from ..core.sexp_file import load_schematic, save_schematic
from .label import GlobalLabel, HierarchicalLabel, Label
from .library import LibrarySymbol
from .symbol import SymbolInstance, SymbolPin, SymbolProperty
from .wire import Junction, Wire

if TYPE_CHECKING:
    from ..erc import ERCReport
    from ..query.symbols import SymbolList


@dataclass
class TitleBlock:
    """Schematic title block information."""

    title: str = ""
    date: str = ""
    rev: str = ""
    company: str = ""
    comments: dict[int, str] = field(default_factory=dict)

    @classmethod
    def from_sexp(cls, sexp: SExp) -> TitleBlock:
        """Parse from S-expression."""
        tb = cls()
        if title := sexp.find("title"):
            tb.title = title.get_string(0) or ""
        if date := sexp.find("date"):
            tb.date = date.get_string(0) or ""
        if rev := sexp.find("rev"):
            tb.rev = rev.get_string(0) or ""
        if company := sexp.find("company"):
            tb.company = company.get_string(0) or ""

        # Parse comments - they're stored as (comment 1 "text")
        for child in sexp.iter_children():
            if child.tag == "comment":
                num = child.get_int(0)
                text = child.get_string(1)
                if num is not None and text is not None:
                    tb.comments[num] = text

        return tb


@dataclass
class SheetInstance:
    """Reference to a hierarchical sheet."""

    name: str
    filename: str
    uuid: str
    position: tuple[float, float] = (0, 0)
    size: tuple[float, float] = (50, 25)

    @classmethod
    def from_sexp(cls, sexp: SExp) -> SheetInstance:
        """Parse from S-expression."""
        name = ""
        filename = ""
        uuid = ""
        pos = (0.0, 0.0)
        size = (50.0, 25.0)

        if sexp.find("property"):
            for prop in sexp.find_all("property"):
                prop_name = prop.get_string(0)
                if prop_name == "Sheetname":
                    name = prop.get_string(1) or ""
                elif prop_name == "Sheetfile":
                    filename = prop.get_string(1) or ""

        if uuid_node := sexp.find("uuid"):
            uuid = uuid_node.get_string(0) or ""

        if at := sexp.find("at"):
            pos = (at.get_float(0) or 0, at.get_float(1) or 0)

        if sz := sexp.find("size"):
            size = (sz.get_float(0) or 50, sz.get_float(1) or 25)

        return cls(name=name, filename=filename, uuid=uuid, position=pos, size=size)


class Schematic:
    """
    High-level interface to a KiCad schematic.

    Provides methods for querying and modifying schematic contents.
    """

    def __init__(self, sexp: SExp, path: Path | None = None):
        """
        Initialize from parsed S-expression.

        Args:
            sexp: Parsed schematic S-expression
            path: Optional path to the source file
        """
        if sexp.tag != "kicad_sch":
            raise ValueError(f"Not a schematic: {sexp.tag}")

        self._sexp = sexp
        self._path = path
        self._symbols: list[SymbolInstance] | None = None
        self._wires: list[Wire] | None = None
        self._junctions: list[Junction] | None = None
        self._labels: list[Label] | None = None
        self._hierarchical_labels: list[HierarchicalLabel] | None = None
        self._sheets: list[SheetInstance] | None = None

    @classmethod
    def load(cls, path: str | Path) -> Schematic:
        """Load a schematic from file."""
        path = Path(path)
        sexp = load_schematic(path)
        return cls(sexp, path)

    def save(self, path: str | Path | None = None) -> None:
        """Save the schematic to file."""
        save_path = Path(path) if path else self._path
        if not save_path:
            raise ValueError("No path specified and no original path available")
        save_schematic(self._sexp, save_path)

    @property
    def sexp(self) -> SExp:
        """Access the underlying S-expression tree."""
        return self._sexp

    @property
    def path(self) -> Path | None:
        """Path to the source file, if loaded from file."""
        return self._path

    @property
    def version(self) -> int | None:
        """Get the schematic version number."""
        if v := self._sexp.find("version"):
            return v.get_int(0)
        return None

    @property
    def generator(self) -> str | None:
        """Get the generator program name."""
        if g := self._sexp.find("generator"):
            return g.get_string(0)
        return None

    @property
    def title_block(self) -> TitleBlock:
        """Get the title block information."""
        if tb := self._sexp.find("title_block"):
            return TitleBlock.from_sexp(tb)
        return TitleBlock()

    @property
    def paper(self) -> str | None:
        """Get the paper size."""
        if p := self._sexp.find("paper"):
            return p.get_string(0)
        return None

    @property
    def uuid(self) -> str | None:
        """Get the schematic UUID."""
        if u := self._sexp.find("uuid"):
            return u.get_string(0)
        return None

    # Symbol operations

    @property
    def symbols(self) -> SymbolList:
        """Get all symbol instances in the schematic.

        Returns a SymbolList which extends list with query methods:
            sch.symbols.by_reference("U1")
            sch.symbols.filter(value="100nF")
            sch.symbols.query().capacitors().all()

        Backward compatible - all list operations still work.
        """
        if self._symbols is None:
            # Import here to avoid circular import
            from ..query.symbols import SymbolList

            # Use find_children to only get direct symbol children, not lib_symbol definitions
            items = [SymbolInstance.from_sexp(s) for s in self._sexp.find_children("symbol")]
            self._symbols = SymbolList(items)
        return self._symbols

    def get_symbol(self, reference: str) -> SymbolInstance | None:
        """Get a symbol by its reference designator (e.g., 'R1', 'U1')."""
        for sym in self.symbols:
            if sym.reference == reference:
                return sym
        return None

    def find_symbols_by_lib(self, lib_id: str) -> list[SymbolInstance]:
        """Find all symbols using a specific library symbol."""
        return [s for s in self.symbols if s.lib_id == lib_id]

    def iter_symbols(self) -> Iterator[SymbolInstance]:
        """Iterate over all symbol instances."""
        return iter(self.symbols)

    # Wire operations

    @property
    def wires(self) -> list[Wire]:
        """Get all wires in the schematic."""
        if self._wires is None:
            self._wires = [Wire.from_sexp(w) for w in self._sexp.find_all("wire")]
        return self._wires

    @property
    def junctions(self) -> list[Junction]:
        """Get all junctions in the schematic."""
        if self._junctions is None:
            self._junctions = [Junction.from_sexp(j) for j in self._sexp.find_all("junction")]
        return self._junctions

    # Label operations

    @property
    def labels(self) -> list[Label]:
        """Get all local labels."""
        if self._labels is None:
            self._labels = [Label.from_sexp(lbl) for lbl in self._sexp.find_all("label")]
        return self._labels

    @property
    def hierarchical_labels(self) -> list[HierarchicalLabel]:
        """Get all hierarchical labels."""
        if self._hierarchical_labels is None:
            self._hierarchical_labels = [
                HierarchicalLabel.from_sexp(lbl)
                for lbl in self._sexp.find_all("hierarchical_label")
            ]
        return self._hierarchical_labels

    @property
    def global_labels(self) -> list[GlobalLabel]:
        """Get all global labels."""
        return [GlobalLabel.from_sexp(lbl) for lbl in self._sexp.find_all("global_label")]

    # Sheet operations

    @property
    def sheets(self) -> list[SheetInstance]:
        """Get all hierarchical sheet references."""
        if self._sheets is None:
            self._sheets = [SheetInstance.from_sexp(s) for s in self._sexp.find_all("sheet")]
        return self._sheets

    def is_hierarchical(self) -> bool:
        """Check if this is a hierarchical schematic (has sub-sheets)."""
        return len(self.sheets) > 0

    # Library symbols

    @property
    def lib_symbols(self) -> SExp | None:
        """Get the embedded library symbols section."""
        return self._sexp.find("lib_symbols")

    def get_lib_symbol(self, lib_id: str) -> SExp | None:
        """Get an embedded library symbol definition by lib_id."""
        if lib_syms := self.lib_symbols:
            for sym in lib_syms.find_all("symbol"):
                sym_name = sym.get_string(0)
                if sym_name == lib_id:
                    return sym
        return None

    # Editing API

    def _find_insertion_index(self) -> int:
        """Find the S-expression index where new elements should be inserted.

        Elements are inserted before ``sheet_instances`` and
        ``symbol_instances`` sections at the end of the file.  If neither
        exists, elements are appended at the very end.
        """
        sentinel_tags = {"sheet_instances", "symbol_instances"}
        for i, child in enumerate(self._sexp.children):
            if child.name in sentinel_tags:
                return i
        return len(self._sexp.children)

    def add_symbol(
        self,
        lib_id: str,
        reference: str,
        value: str,
        footprint: str,
        position: tuple[float, float],
        rotation: float = 0,
        mirror: str = "",
        unit: int = 1,
        pin_numbers: list[str] | None = None,
        datasheet: str = "",
    ) -> SymbolInstance:
        """Add a new component symbol to the schematic.

        The symbol's library definition must already exist in the
        schematic's ``lib_symbols`` section.  Use
        :meth:`embed_lib_symbol` to add it first if needed.

        Args:
            lib_id: Library identifier (e.g. ``"Device:R"``).
            reference: Reference designator (e.g. ``"R1"``).
            value: Component value (e.g. ``"10k"``).
            footprint: Footprint name (e.g. ``"Resistor_SMD:R_0402_1005Metric"``).
            position: ``(x, y)`` placement in schematic coordinates.
            rotation: Rotation in degrees (default 0).
            mirror: Mirror mode (``""``, ``"x"``, or ``"y"``).
            unit: Unit number for multi-unit symbols (default 1).
            pin_numbers: Optional list of pin numbers.  When *None*, pins
                are auto-detected from the embedded ``lib_symbols`` entry.
            datasheet: Optional datasheet URL.

        Returns:
            The created :class:`SymbolInstance`.

        Raises:
            ValueError: If ``lib_id`` is not found in ``lib_symbols`` and
                *pin_numbers* was not supplied.
        """
        sym_uuid = str(uuid_mod.uuid4())

        # Build properties
        props: dict[str, SymbolProperty] = {
            "Reference": SymbolProperty(
                name="Reference",
                value=reference,
                position=position,
                rotation=0,
                visible=True,
            ),
            "Value": SymbolProperty(
                name="Value",
                value=value,
                position=(position[0], position[1] + 2.54),
                rotation=0,
                visible=True,
            ),
            "Footprint": SymbolProperty(
                name="Footprint",
                value=footprint,
                position=position,
                rotation=0,
                visible=False,
            ),
            "Datasheet": SymbolProperty(
                name="Datasheet",
                value=datasheet,
                position=position,
                rotation=0,
                visible=False,
            ),
        }

        # Determine pin numbers from lib_symbols when not explicitly given
        if pin_numbers is None:
            lib_sym_sexp = self.get_lib_symbol(lib_id)
            if lib_sym_sexp is not None:
                lib_sym = LibrarySymbol.from_sexp(lib_sym_sexp)
                pin_numbers = [p.number for p in lib_sym.pins]
            else:
                raise ValueError(
                    f"lib_id '{lib_id}' not found in schematic lib_symbols. "
                    "Use embed_lib_symbol() first, or supply pin_numbers explicitly."
                )

        pins = [SymbolPin(number=num, uuid=str(uuid_mod.uuid4())) for num in pin_numbers]

        instance = SymbolInstance(
            lib_id=lib_id,
            uuid=sym_uuid,
            position=position,
            rotation=rotation,
            mirror=mirror,
            unit=unit,
            in_bom=True,
            on_board=True,
            dnp=False,
            properties=props,
            pins=pins,
        )

        # Insert into S-expression tree before sentinel sections
        idx = self._find_insertion_index()
        self._sexp.insert(idx, instance.to_sexp())
        self.invalidate_cache()
        return instance

    def add_power(
        self,
        name: str,
        position: tuple[float, float],
        rotation: float = 0,
    ) -> SymbolInstance:
        """Add a power symbol (e.g. GND, +3V3).

        Power symbols have ``in_bom=False`` and ``on_board=False``.
        The library entry must already exist in ``lib_symbols``.

        Args:
            name: Power symbol name (e.g. ``"GND"``, ``"+3V3"``).
            position: ``(x, y)`` placement.
            rotation: Rotation in degrees (default 0).

        Returns:
            The created :class:`SymbolInstance`.
        """
        lib_id = f"power:{name}"
        sym_uuid = str(uuid_mod.uuid4())

        # Detect pins from lib_symbols
        pin_numbers: list[str] = []
        lib_sym_sexp = self.get_lib_symbol(lib_id)
        if lib_sym_sexp is not None:
            lib_sym = LibrarySymbol.from_sexp(lib_sym_sexp)
            pin_numbers = [p.number for p in lib_sym.pins]
        else:
            # Power symbols typically have a single pin "1"
            pin_numbers = ["1"]

        props: dict[str, SymbolProperty] = {
            "Reference": SymbolProperty(
                name="Reference",
                value=f"#{name}",
                position=position,
                rotation=0,
                visible=False,
            ),
            "Value": SymbolProperty(
                name="Value",
                value=name,
                position=(position[0], position[1] + 2.54),
                rotation=0,
                visible=True,
            ),
            "Footprint": SymbolProperty(
                name="Footprint",
                value="",
                position=position,
                rotation=0,
                visible=False,
            ),
            "Datasheet": SymbolProperty(
                name="Datasheet",
                value="",
                position=position,
                rotation=0,
                visible=False,
            ),
        }

        pins = [SymbolPin(number=num, uuid=str(uuid_mod.uuid4())) for num in pin_numbers]

        instance = SymbolInstance(
            lib_id=lib_id,
            uuid=sym_uuid,
            position=position,
            rotation=rotation,
            mirror="",
            unit=1,
            in_bom=False,
            on_board=False,
            dnp=False,
            properties=props,
            pins=pins,
        )

        idx = self._find_insertion_index()
        self._sexp.insert(idx, instance.to_sexp())
        self.invalidate_cache()
        return instance

    def add_wire(
        self,
        start: tuple[float, float],
        end: tuple[float, float],
    ) -> Wire:
        """Add a wire segment to the schematic.

        Args:
            start: ``(x, y)`` start point.
            end: ``(x, y)`` end point.

        Returns:
            The created :class:`Wire`.
        """
        wire = Wire(
            start=start,
            end=end,
            uuid=str(uuid_mod.uuid4()),
            stroke_width=0,
            stroke_type="default",
        )
        idx = self._find_insertion_index()
        self._sexp.insert(idx, wire.to_sexp())
        self.invalidate_cache()
        return wire

    def embed_lib_symbol(self, lib_sym: LibrarySymbol) -> None:
        """Insert a library symbol definition into ``lib_symbols``.

        If a definition with the same name already exists, this is a
        no-op.

        Args:
            lib_sym: The :class:`LibrarySymbol` to embed.
        """
        lib_syms = self.lib_symbols
        if lib_syms is None:
            # Create the lib_symbols section
            lib_syms = SExp.list("lib_symbols")
            # Insert it early (after uuid/paper, before symbols)
            # Find a good insertion point
            insert_idx = 0
            for i, child in enumerate(self._sexp.children):
                if child.name in (
                    "uuid",
                    "paper",
                    "title_block",
                    "generator",
                    "generator_version",
                    "version",
                ):
                    insert_idx = i + 1
            self._sexp.insert(insert_idx, lib_syms)

        # Check if already present
        for sym in lib_syms.find_all("symbol"):
            sym_name = sym.get_string(0)
            if sym_name == lib_sym.name:
                return  # Already embedded

        lib_syms.append(lib_sym.to_sexp_node())

    @staticmethod
    def snap_to_grid(
        value: float,
        grid: float = 1.27,
    ) -> float:
        """Round a coordinate value to the nearest grid point.

        Args:
            value: Coordinate value in mm.
            grid: Grid spacing in mm (default 1.27 = 50mil).

        Returns:
            The snapped value.
        """
        return round(value / grid) * grid

    def snap_all_to_grid(self, grid: float = 1.27) -> None:
        """Snap all symbol and wire positions to the nearest grid point.

        This modifies both the Python dataclass fields *and* the
        underlying S-expression tree, then invalidates the cache.

        Args:
            grid: Grid spacing in mm (default 1.27 = 50mil).
        """
        snap = self.snap_to_grid

        # Snap symbols
        for sym_sexp in self._sexp.find_children("symbol"):
            if at := sym_sexp.find("at"):
                x = at.get_float(0) or 0
                y = at.get_float(1) or 0
                at.set_value(0, snap(x, grid))
                at.set_value(1, snap(y, grid))

        # Snap wires
        for wire_sexp in self._sexp.find_all("wire"):
            if pts := wire_sexp.find("pts"):
                for xy in pts.find_all("xy"):
                    x = xy.get_float(0) or 0
                    y = xy.get_float(1) or 0
                    xy.set_value(0, snap(x, grid))
                    xy.set_value(1, snap(y, grid))

        # Snap junctions
        for junc_sexp in self._sexp.find_all("junction"):
            if at := junc_sexp.find("at"):
                x = at.get_float(0) or 0
                y = at.get_float(1) or 0
                at.set_value(0, snap(x, grid))
                at.set_value(1, snap(y, grid))

        self.invalidate_cache()

    # Modification helpers

    def invalidate_cache(self) -> None:
        """Clear cached data after modifications."""
        self._symbols = None
        self._wires = None
        self._junctions = None
        self._labels = None
        self._hierarchical_labels = None
        self._sheets = None

    def __repr__(self) -> str:
        path_str = str(self._path) if self._path else "unsaved"
        return f"Schematic({path_str}, symbols={len(self.symbols)}, wires={len(self.wires)})"

    # ERC operations

    def run_erc(self, output_path: str | Path | None = None) -> ERCReport:
        """Run KiCad ERC on this schematic.

        Invokes kicad-cli to run electrical rules check and returns
        the parsed report with violations, errors, and warnings.

        Args:
            output_path: Optional path for ERC report file.
                        If None, uses a temporary file that is cleaned up.

        Returns:
            Parsed ERCReport with violations, errors, warnings.

        Raises:
            KiCadCLIError: If kicad-cli is not found or fails.
            ValueError: If schematic has no path (not saved to disk).

        Example::

            sch = Schematic.load("design.kicad_sch")
            report = sch.run_erc()
            if report.error_count > 0:
                for error in report.errors:
                    print(f"ERC Error: {error.type} at {error.location_str}")
        """
        from ..cli.runner import run_erc as cli_run_erc
        from ..erc import ERCReport
        from ..exceptions import KiCadCLIError

        # Schematic must be saved to disk for kicad-cli
        if self._path is None:
            raise ValueError(
                "Schematic must be saved to disk before running ERC. "
                "Use save() to write the schematic first."
            )

        if not self._path.exists():
            raise ValueError(f"Schematic file not found: {self._path}")

        # Convert output_path to Path if provided
        output = Path(output_path) if output_path else None

        # Run ERC via kicad-cli
        result = cli_run_erc(self._path, output_path=output)

        if not result.success:
            raise KiCadCLIError(
                f"ERC failed: {result.stderr}",
                context={
                    "schematic": str(self._path),
                    "return_code": result.return_code,
                },
                suggestions=[
                    "Ensure KiCad 8+ is installed",
                    "On macOS: brew install --cask kicad",
                    "On Linux: Check your package manager for kicad",
                ],
            )

        # Parse the report
        try:
            report = ERCReport.load(result.output_path)
        finally:
            # Clean up temp file if we created one
            if output_path is None and result.output_path:
                result.output_path.unlink(missing_ok=True)

        return report
