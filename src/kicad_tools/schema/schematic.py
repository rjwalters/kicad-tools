"""
Schematic document model.

Provides a high-level interface to KiCad schematic files.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Dict, Iterator, List, Optional, Tuple

from ..core.sexp import SExp
from ..core.sexp_file import load_schematic, save_schematic
from .label import GlobalLabel, HierarchicalLabel, Label
from .symbol import SymbolInstance
from .wire import Junction, Wire

if TYPE_CHECKING:
    from ..query.symbols import SymbolList


@dataclass
class TitleBlock:
    """Schematic title block information."""

    title: str = ""
    date: str = ""
    rev: str = ""
    company: str = ""
    comments: Dict[int, str] = field(default_factory=dict)

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
    position: Tuple[float, float] = (0, 0)
    size: Tuple[float, float] = (50, 25)

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

    def __init__(self, sexp: SExp, path: Optional[Path] = None):
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
        self._symbols: Optional[List[SymbolInstance]] = None
        self._wires: Optional[List[Wire]] = None
        self._junctions: Optional[List[Junction]] = None
        self._labels: Optional[List[Label]] = None
        self._hierarchical_labels: Optional[List[HierarchicalLabel]] = None
        self._sheets: Optional[List[SheetInstance]] = None

    @classmethod
    def load(cls, path: str | Path) -> Schematic:
        """Load a schematic from file."""
        path = Path(path)
        sexp = load_schematic(path)
        return cls(sexp, path)

    def save(self, path: Optional[str | Path] = None) -> None:
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
    def path(self) -> Optional[Path]:
        """Path to the source file, if loaded from file."""
        return self._path

    @property
    def version(self) -> Optional[int]:
        """Get the schematic version number."""
        if v := self._sexp.find("version"):
            return v.get_int(0)
        return None

    @property
    def generator(self) -> Optional[str]:
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
    def paper(self) -> Optional[str]:
        """Get the paper size."""
        if p := self._sexp.find("paper"):
            return p.get_string(0)
        return None

    @property
    def uuid(self) -> Optional[str]:
        """Get the schematic UUID."""
        if u := self._sexp.find("uuid"):
            return u.get_string(0)
        return None

    # Symbol operations

    @property
    def symbols(self) -> "SymbolList":
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

            items = [SymbolInstance.from_sexp(s) for s in self._sexp.find_all("symbol")]
            self._symbols = SymbolList(items)
        return self._symbols

    def get_symbol(self, reference: str) -> Optional[SymbolInstance]:
        """Get a symbol by its reference designator (e.g., 'R1', 'U1')."""
        for sym in self.symbols:
            if sym.reference == reference:
                return sym
        return None

    def find_symbols_by_lib(self, lib_id: str) -> List[SymbolInstance]:
        """Find all symbols using a specific library symbol."""
        return [s for s in self.symbols if s.lib_id == lib_id]

    def iter_symbols(self) -> Iterator[SymbolInstance]:
        """Iterate over all symbol instances."""
        return iter(self.symbols)

    # Wire operations

    @property
    def wires(self) -> List[Wire]:
        """Get all wires in the schematic."""
        if self._wires is None:
            self._wires = [Wire.from_sexp(w) for w in self._sexp.find_all("wire")]
        return self._wires

    @property
    def junctions(self) -> List[Junction]:
        """Get all junctions in the schematic."""
        if self._junctions is None:
            self._junctions = [Junction.from_sexp(j) for j in self._sexp.find_all("junction")]
        return self._junctions

    # Label operations

    @property
    def labels(self) -> List[Label]:
        """Get all local labels."""
        if self._labels is None:
            self._labels = [Label.from_sexp(lbl) for lbl in self._sexp.find_all("label")]
        return self._labels

    @property
    def hierarchical_labels(self) -> List[HierarchicalLabel]:
        """Get all hierarchical labels."""
        if self._hierarchical_labels is None:
            self._hierarchical_labels = [
                HierarchicalLabel.from_sexp(lbl)
                for lbl in self._sexp.find_all("hierarchical_label")
            ]
        return self._hierarchical_labels

    @property
    def global_labels(self) -> List[GlobalLabel]:
        """Get all global labels."""
        return [GlobalLabel.from_sexp(lbl) for lbl in self._sexp.find_all("global_label")]

    # Sheet operations

    @property
    def sheets(self) -> List[SheetInstance]:
        """Get all hierarchical sheet references."""
        if self._sheets is None:
            self._sheets = [SheetInstance.from_sexp(s) for s in self._sexp.find_all("sheet")]
        return self._sheets

    def is_hierarchical(self) -> bool:
        """Check if this is a hierarchical schematic (has sub-sheets)."""
        return len(self.sheets) > 0

    # Library symbols

    @property
    def lib_symbols(self) -> Optional[SExp]:
        """Get the embedded library symbols section."""
        return self._sexp.find("lib_symbols")

    def get_lib_symbol(self, lib_id: str) -> Optional[SExp]:
        """Get an embedded library symbol definition by lib_id."""
        if lib_syms := self.lib_symbols:
            for sym in lib_syms.find_all("symbol"):
                sym_name = sym.get_string(0)
                if sym_name == lib_id:
                    return sym
        return None

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
