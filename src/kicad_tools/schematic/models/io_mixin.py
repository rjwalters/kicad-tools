"""
Schematic I/O Mixin

Provides file loading and saving capabilities for Schematic class.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import TYPE_CHECKING

from kicad_tools.core.symbol_transform import normalize_mirror, symbol_to_sheet_offset
from kicad_tools.core.version import (
    KICAD_GENERATOR_VERSION,
    KICAD_SCH_FORMAT_VERSION,
    KICAD_SYM_FORMAT_VERSION,
)
from kicad_tools.sexp import SExp
from kicad_tools.sexp.builders import (
    sheet_instances,
    text_node,
    title_block,
    uuid_node,
)

from ..logging import _log_info, _log_warning
from .elements import GlobalLabel, HierarchicalLabel, Junction, Label, NoConnect, PowerSymbol, Wire
from .symbol import SymbolInstance

if TYPE_CHECKING:
    from kicad_tools.core.schematic_uuids import UuidMinter
    from kicad_tools.erc import ERCReport

    from .schematic import Schematic


# --- Source-preserving round trip (issue #6051) ------------------------------
#
# The model keeps only the fields it edits, so regenerating a loaded file
# from the model rewrites everything the model does not track: label and
# text justification, text font size, symbol/power-symbol field positions,
# ``Description`` properties, wire stroke types, junction diameters,
# ``polyline``/``sheet``/``bus`` items, ``embedded_fonts``, the format
# ``version`` ...  Instead, every element parsed from a file remembers its
# source node plus a fingerprint of what the model would have generated for
# it at load time.  On save, an element whose regenerated form still matches
# that fingerprint has not been touched by the caller and is re-emitted
# verbatim; anything edited is patched in place (only the atoms the edit
# changed, issue #6057); anything the model does not parse at all passes
# through unchanged.

#: Top-level nodes that belong to the file header.  New header nodes (a
#: ``title_block`` set on a file that had none) are inserted before the
#: first node *not* in this set.
_HEADER_NODES = frozenset(
    {"version", "generator", "generator_version", "uuid", "paper", "title_block"}
)

#: Top-level nodes that close the file.  Elements added to a loaded
#: schematic are inserted before the first of these.
_TRAILER_NODES = frozenset({"sheet_instances", "symbol_instances", "embedded_fonts"})


def _attach_source(elem, node: SExp):
    """Record the node *elem* was parsed from (fingerprinted later)."""
    elem._source_node = node
    return elem


def _source_format_version(doc: SExp) -> int:
    """Return the file's ``(version N)``, or 0 when absent/unparseable."""
    version_node = doc.get("version")
    if version_node is None:
        return 0
    try:
        return int(str(version_node.get_first_atom()))
    except ValueError:
        return 0


def _fingerprint(node: SExp | None, skip: frozenset[str] = frozenset()) -> str:
    """Canonical text of a generated node, ignoring random pin UUIDs.

    Symbol builders mint a fresh UUID for every pin on each call, so two
    generations of an untouched symbol differ only there.  Element UUIDs
    themselves are kept: re-assigning one is an edit.  Child nodes named in
    *skip* are left out entirely.
    """
    if node is None:
        return ""
    parts: list[str] = []
    stack: list[tuple[SExp, str | None] | str] = [(node, None)]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            parts.append(item)
            continue
        n, parent = item
        if n.name is None:
            parts.append(repr(n.value))
            continue
        if n.name == "uuid" and parent == "pin":
            parts.append("(uuid)")
            continue
        if parent is not None and n.name in skip:
            continue
        parts.append("(" + n.name)
        stack.append(")")
        for child in reversed(n.children):
            stack.append((child, n.name))
    return " ".join(parts)


# --- Patching an edited element's source node (issue #6057) -----------------
#
# An edited element is not rebuilt from builder defaults.  Instead the edit is
# replayed onto its source node as a three-way merge: ``old`` is what the
# model generated at load time, ``new`` is what it generates now, and only the
# atoms/children that differ between the two are changed in ``src``.  Every
# attribute the model does not track (label justify, font sizes, field
# positions and effects, ``Description`` properties, pin UUIDs, the instances
# ``project`` name, wire stroke type ...) therefore survives the edit.

#: Repeated children told apart by their first atom (a property's name, a
#: pin's number, a lib symbol's id, a title-block comment's index) rather than
#: by their order of appearance.
_KEYED_BY_FIRST_ATOM = frozenset({"property", "pin", "symbol", "comment"})

#: Nodes whose first two atoms are an X/Y coordinate.  When the source value
#: differs from the model's (a field placed away from the builder default),
#: a moved element translates it by the same delta instead of snapping it to
#: the builder default.
_COORD_NODES = frozenset({"at", "xy", "start", "end", "center", "mid"})

#: The model rounds loaded coordinates to 0.01 mm; a source coordinate within
#: this distance of the model's is the same coordinate.
_COORD_TOL = 0.006


def _child_keys(node: SExp) -> list[tuple[str, str | None, int] | None]:
    """A stable key per child of *node*; ``None`` for atoms."""
    seen: dict[tuple[str, str | None], int] = {}
    keys: list[tuple[str, str | None, int] | None] = []
    for child in node.children:
        if child.name is None:
            keys.append(None)
            continue
        disc = None
        if child.name in _KEYED_BY_FIRST_ATOM:
            first = child.get_first_atom()
            disc = None if first is None else repr(first)
        base = (child.name, disc)
        n = seen.get(base, 0)
        seen[base] = n + 1
        keys.append((child.name, disc, n))
    return keys


def _keyed(node: SExp) -> dict[tuple[str, str | None, int], SExp]:
    return {key: child for key, child in zip(_child_keys(node), node.children, strict=True) if key}


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _atom_eq(a: SExp, b: SExp) -> bool:
    if _is_number(a.value) and _is_number(b.value):
        return float(a.value) == float(b.value)  # type: ignore[arg-type]
    return repr(a.value) == repr(b.value)


def _coord_atom(value: float) -> SExp:
    rounded = round(value, 4)
    return SExp.atom(int(rounded) if rounded == int(rounded) else rounded)


def _patch_atoms(name: str, src: list[SExp], old: list[SExp], new: list[SExp]) -> list[SExp]:
    """Replay the model's ``old`` -> ``new`` atom change onto ``src``."""
    if len(old) == len(new) and all(_atom_eq(o, n) for o, n in zip(old, new, strict=True)):
        return src
    if not len(src) == len(old) == len(new):
        return new
    out: list[SExp] = []
    for i, (s, o, n) in enumerate(zip(src, old, new, strict=True)):
        if _atom_eq(o, n):
            out.append(s)  # not edited: keep the source token
        elif _atom_eq(s, o):
            out.append(n)
        elif _is_number(s.value) and _is_number(o.value) and _is_number(n.value):
            sv, ov, nv = float(s.value), float(o.value), float(n.value)  # type: ignore[arg-type]
            if abs(sv - ov) <= _COORD_TOL or name not in _COORD_NODES or i >= 2:
                out.append(n)
            else:
                out.append(_coord_atom(sv + (nv - ov)))
        else:
            out.append(n)
    return out


def _at_atoms(node: SExp) -> list[float] | None:
    """The numeric atoms of *node*'s ``(at x y [angle])`` child, if any."""
    at_node = node.get("at")
    if at_node is None:
        return None
    vals = [a.value for a in at_node.children if a.name is None]
    if len(vals) < 2 or not all(_is_number(v) for v in vals):
        return None
    return [float(v) for v in vals]  # type: ignore[arg-type]


def _quarter_angle(at: list[float]) -> float | None:
    """The ``(at x y angle)`` angle if it is a right angle, else ``None``."""
    angle = (at[2] if len(at) > 2 else 0.0) % 360
    return angle if abs(angle - round(angle / 90.0) * 90.0) <= 1e-6 else None


def _node_mirror(node: SExp) -> str:
    mirror_node = node.get("mirror")
    return normalize_mirror(mirror_node.get_first_atom()) if mirror_node else ""


def _rotate_fields_with_symbol(node: SExp, old: SExp, new: SExp) -> None:
    """Carry *node*'s fields through the edit's change of symbol transform.

    KiCad moves a symbol's fields with the symbol when it is rotated or
    mirrored, and turns their text between horizontal and vertical.  The
    model only tracks the symbol's own angle and mirror, so the field
    positions the three-way merge produced (after any move delta) are
    re-transformed here, in place on the freshly built *node* (issue #6085).

    A field offset is held fixed in library space: the sheet offset is mapped
    back through the *old* ``(rotation, mirror)`` with ``core.symbol_transform``
    and forward through the *new* one.  That makes the direction right for
    mirrored symbols too -- rotate-then-mirror means a file angle of +D turns a
    mirrored symbol clockwise on screen -- and handles an edit that changes the
    mirror and the angle together by applying the new mirror state.  If either
    angle is not a right angle the fields are left as the merge produced them.
    The text angle toggles only with the file-angle change; a mirror flip
    alone does not turn text.
    """
    old_at, new_at, out_at = _at_atoms(old), _at_atoms(new), _at_atoms(node)
    if not old_at or not new_at or not out_at:
        return
    old_rot, new_rot = _quarter_angle(old_at), _quarter_angle(new_at)
    if old_rot is None or new_rot is None:
        return  # only the right-angle rotations KiCad's symbols use
    old_mirror, new_mirror = _node_mirror(old), _node_mirror(new)
    delta = (new_rot - old_rot) % 360
    if delta == 0 and old_mirror == new_mirror:
        return
    # Orthogonal old transform: its inverse is the transpose of its columns.
    c1 = symbol_to_sheet_offset(1, 0, old_rot, old_mirror)
    c2 = symbol_to_sheet_offset(0, 1, old_rot, old_mirror)
    ox, oy = out_at[0], out_at[1]
    for i, child in enumerate(node.children):
        if child.name != "property":
            continue
        pos = _at_atoms(child)
        if pos is None:
            continue
        dx, dy = pos[0] - ox, pos[1] - oy
        lx = dx * c1[0] + dy * c1[1]
        ly = dx * c2[0] + dy * c2[1]
        ndx, ndy = symbol_to_sheet_offset(lx, ly, new_rot, new_mirror)
        angle = ((pos[2] if len(pos) > 2 else 0.0) + delta) % 180
        rotated = [
            ox + ndx,
            oy + ndy,
            int(angle) if angle == int(angle) else angle,
        ]
        new_prop = SExp.list(child.name)
        new_prop._inline = child._inline
        for sub in child.children:
            if sub.name == "at":
                at_new = SExp.list("at")
                at_new._inline = sub._inline
                at_new.children = [
                    _coord_atom(rotated[0]),
                    _coord_atom(rotated[1]),
                    SExp.atom(rotated[2]),
                ]
                new_prop.children.append(at_new)
            else:
                new_prop.children.append(sub)
        node.children[i] = new_prop


def _patch_node(src: SExp, old: SExp, new: SExp, parent: str | None = None) -> SExp:
    """Return *src* with the model's ``old`` -> ``new`` change applied.

    *src* is never mutated: changed nodes are rebuilt, unchanged subtrees are
    shared with the source tree.
    """
    if _fingerprint(old) == _fingerprint(new):
        return src
    if src.name is None or src.name != old.name or old.name != new.name:
        return new

    def atoms(node: SExp) -> list[SExp]:
        return [c for c in node.children if c.name is None]

    patched_atoms = _patch_atoms(src.name, atoms(src), atoms(old), atoms(new))
    old_map, new_map = _keyed(old), _keyed(new)

    out: list[tuple[tuple[str, str | None, int] | None, SExp]] = []
    atom_iter = iter(patched_atoms)
    atoms_placed = False
    for key, child in zip(_child_keys(src), src.children, strict=True):
        if key is None:
            if not atoms_placed and len(patched_atoms) != len(atoms(src)):
                out.extend((None, a) for a in patched_atoms)
                atoms_placed = True
            elif not atoms_placed:
                out.append((None, next(atom_iter)))
            continue
        if src.name == "pin" and key[0] == "uuid":
            out.append((key, child))  # pin UUIDs are regenerated randomly
        elif key in old_map and key in new_map:
            out.append((key, _patch_node(child, old_map[key], new_map[key], src.name)))
        elif key in old_map:
            continue  # removed by the edit
        elif key in new_map:
            out.append((key, new_map[key]))
        else:
            out.append((key, child))  # not modelled: keep
    if not atoms_placed and not any(k is None for k, _ in out) and patched_atoms:
        out[0:0] = [(None, a) for a in patched_atoms]

    # Children the edit added that the source lacks go after their nearest
    # preceding sibling in the generated order.
    present = {k for k, _ in out if k is not None}
    prev: tuple[str, str | None, int] | None = None
    for key in (k for k in _child_keys(new) if k is not None):
        if key not in present:
            # A child the source omits stays omitted unless the edit changed
            # its content -- not merely its builder-derived position.
            o = old_map.get(key)
            if o is None or _fingerprint(o, _COORD_NODES) != _fingerprint(
                new_map[key], _COORD_NODES
            ):
                at = next(
                    (i + 1 for i, (k, _) in enumerate(out) if prev is not None and k == prev),
                    sum(1 for k, _ in out if k is None),
                )
                out.insert(at, (key, new_map[key]))
                present.add(key)
            else:
                continue  # the source deliberately lacks it; unchanged
        prev = key

    node = SExp.list(src.name)
    node._inline = src._inline
    node.children = [child for _, child in out]
    if src.name == "symbol":
        _rotate_fields_with_symbol(node, old, new)
    return node


class SchematicIOMixin:
    """Mixin providing I/O operations for Schematic class."""

    if TYPE_CHECKING:
        # Attributes provided by the concrete ``Schematic`` class (via
        # ``SchematicElementsMixin`` and ``Schematic.__init__``).  Declared
        # here so mypy can see them when this mixin references them.
        _PWR_SYNTH_LIB_PREFIX: str
        _synthesized_pwr_defs: dict[str, SExp]
        text_notes: list[tuple[str, float, float]]
        _source_doc: SExp | None
        _source_consumed: list[SExp]
        _source_slots: dict[str, tuple[SExp | None, str, SExp]]
        _text_note_sources: dict[tuple[str, float, float], list[SExp]]
        _uuid_minter: UuidMinter

        def _assign_provisional_uuids(self) -> int: ...
        def _text_note_uuid(self, text: str, x: float, y: float, seen: dict) -> str: ...

        # ``_from_sexp`` constructs the concrete ``Schematic`` via ``cls(...)``.
        # Declaring the constructor signature here lets mypy validate those
        # keyword arguments against the real ``Schematic.__init__`` shape
        # instead of the empty ``object`` init (resolves the spurious
        # ``call-arg`` errors on the ``cls(...)`` call site).
        def __init__(
            self,
            title: str = "",
            date: str = "2025-01",
            revision: str = "A",
            company: str = "",
            comment1: str = "",
            comment2: str = "",
            paper: str = "A4",
            project_name: str = "project",
            sheet_uuid: str | None = None,
            parent_uuid: str | None = None,
            page: str = "1",
            grid: float = ...,
            snap_mode: object = ...,
            local_symbol_libs: list[Path] | None = None,
        ) -> None: ...

    @classmethod
    def load(
        cls,
        path: str | Path,
        local_symbol_libs: list[Path] | None = None,
    ) -> Schematic:
        """Load a schematic from a .kicad_sch file.

        This enables round-trip editing: load -> modify -> save.

        Args:
            path: Path to the .kicad_sch file
            local_symbol_libs: Optional list of project-local ``.kicad_sym``
                files to register on the loaded schematic (mirrors the
                ``Schematic(...)`` constructor argument).  These are consulted
                by :meth:`resolve_lib_path` during subsequent ``add_symbol()``
                calls so a reloaded schematic can resolve ``LIBNAME:SYMNAME``
                ids against project-local libraries, matching the constructor
                path.  Default ``None`` preserves prior behavior (stock libs
                only) for all existing callers.

        Returns:
            A Schematic instance populated with all elements from the file

        Example:
            sch = Schematic.load("power.kicad_sch")
            sch.add_symbol("Device:R", 100, 100, "R5", "10k")
            sch.write("power.kicad_sch")

            # Reload with a project-local symbol library registered:
            sch = Schematic.load(
                "power.kicad_sch",
                local_symbol_libs=[Path("libs/custom.kicad_sym")],
            )
        """
        from kicad_tools.sexp import parse_file

        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"Schematic file not found: {path}")

        doc = parse_file(path)
        sch = cls._from_sexp(doc, local_symbol_libs=local_symbol_libs)
        # Track the source path so operations that need to walk
        # sub-sheets (e.g., extract_netlist(hierarchical=True), run_erc)
        # can resolve relative sheet references.
        sch._saved_path = path
        return sch

    @classmethod
    def _from_sexp(
        cls,
        doc: SExp,
        local_symbol_libs: list[Path] | None = None,
    ) -> Schematic:
        """Create a Schematic from a parsed S-expression tree.

        This is the internal method that does the actual parsing.

        Args:
            doc: Parsed S-expression tree for the schematic.
            local_symbol_libs: Optional project-local ``.kicad_sym`` files to
                register on the constructed schematic (threaded through from
                :meth:`load`).  Default ``None`` preserves prior behavior.
        """
        from .schematic import SnapMode

        # Extract title block info
        title = ""
        date = ""
        revision = ""
        company = ""
        comment1 = ""
        comment2 = ""

        tb = doc.get("title_block")
        if tb:
            title_node = tb.get("title")
            if title_node:
                title = str(title_node.get_first_atom() or "")
            date_node = tb.get("date")
            if date_node:
                date = str(date_node.get_first_atom() or "")
            rev_node = tb.get("rev")
            if rev_node:
                revision = str(rev_node.get_first_atom() or "")
            company_node = tb.get("company")
            if company_node:
                company = str(company_node.get_first_atom() or "")
            # Comments are numbered
            for comment_node in tb.find_all("comment"):
                atoms = comment_node.get_atoms()
                if len(atoms) >= 2:
                    num = int(atoms[0])
                    text = str(atoms[1])
                    if num == 1:
                        comment1 = text
                    elif num == 2:
                        comment2 = text

        # Get paper size
        paper_node = doc.get("paper")
        paper = str(paper_node.get_first_atom()) if paper_node else "A4"

        # Get UUID
        uuid_node_elem = doc.get("uuid")
        # A file without a root UUID gets the deterministic default (#6076).
        sheet_uuid = str(uuid_node_elem.get_first_atom()) if uuid_node_elem else None

        # Parse lib_symbols to get embedded symbol definitions
        embedded_lib_symbols: dict[str, SExp] = {}
        lib_symbols_node = doc.get("lib_symbols")
        if lib_symbols_node:
            for sym_node in lib_symbols_node.children:
                if sym_node.name == "symbol":
                    sym_name = str(sym_node.get_first_atom())
                    embedded_lib_symbols[sym_name] = sym_node

        # Create schematic instance with minimal init
        # We disable snapping for loaded schematics to preserve coordinates
        sch = cls(
            title=title,
            date=date,
            revision=revision,
            company=company,
            comment1=comment1,
            comment2=comment2,
            paper=paper,
            sheet_uuid=sheet_uuid,
            snap_mode=SnapMode.OFF,  # Preserve original coordinates
            local_symbol_libs=local_symbol_libs,
        )

        # Store embedded lib_symbols for round-trip
        sch._embedded_lib_symbols = embedded_lib_symbols

        # Parse sheet_instances to get project name and parent info
        sheet_instances_node = doc.get("sheet_instances")
        if sheet_instances_node:
            project_node = sheet_instances_node.get("project")
            if project_node:
                sch.project_name = str(project_node.get_first_atom() or "project")
                path_node = project_node.get("path")
                if path_node:
                    page_node = path_node.get("page")
                    if page_node:
                        sch.page = str(page_node.get_first_atom() or "1")

        # Parse placed symbols (those with lib_id)
        for child in doc.children:
            if child.name == "symbol" and child.get("lib_id"):
                if PowerSymbol.is_power_symbol(child):
                    pwr = PowerSymbol.from_sexp(child)
                    _attach_source(pwr, child)
                    sch.power_symbols.append(pwr)
                else:
                    sym = SymbolInstance.from_sexp(
                        child, symbol_defs=sch._symbol_defs, lib_symbols=embedded_lib_symbols
                    )
                    _attach_source(sym, child)
                    sch.symbols.append(sym)
                    # Cache the symbol def
                    sch._symbol_defs[sym.symbol_def.lib_id] = sym.symbol_def

        # Parse wires
        for child in doc.children:
            if child.name == "wire":
                sch.wires.append(_attach_source(Wire.from_sexp(child), child))

        # Parse junctions
        for child in doc.children:
            if child.name == "junction":
                sch.junctions.append(_attach_source(Junction.from_sexp(child), child))

        # Parse no-connects
        for child in doc.children:
            if child.name == "no_connect":
                sch.no_connects.append(_attach_source(NoConnect.from_sexp(child), child))

        # Parse labels
        for child in doc.children:
            if child.name == "label":
                sch.labels.append(_attach_source(Label.from_sexp(child), child))

        # Parse hierarchical labels
        for child in doc.children:
            if child.name == "hierarchical_label":
                sch.hier_labels.append(_attach_source(HierarchicalLabel.from_sexp(child), child))

        # Parse global labels
        for child in doc.children:
            if child.name == "global_label":
                sch.global_labels.append(_attach_source(GlobalLabel.from_sexp(child), child))

        # Parse text notes
        for child in doc.children:
            if child.name == "text":
                text = str(child.get_first_atom() or "")
                at_node = child.get("at")
                if at_node:
                    atoms = at_node.get_atoms()
                    x = round(float(atoms[0]), 2)
                    y = round(float(atoms[1]), 2)
                    sch.text_notes.append((text, x, y))
                    sch._text_note_sources.setdefault((text, x, y), []).append(child)

        # Keep rule (Issue #6076): every UUID in the file is reserved, so an
        # element added later can never be minted a UUID the file already
        # uses, and an element the file gave no UUID gets a deterministic one
        # now -- before the snapshot, so its fingerprint already carries it.
        sch._uuid_minter.reserve(
            str(node.get_first_atom())
            for node in doc.iter_all()
            if node.name in ("uuid", "tstamp") and node.get_first_atom() is not None
        )
        sch._assign_provisional_uuids()

        # Update power counter based on existing power symbols
        max_pwr = 0
        for pwr in sch.power_symbols:
            # Extract number from #PWR01, #PWR02, etc.
            if pwr.reference.startswith("#PWR"):
                try:
                    num = int(pwr.reference[4:])
                    max_pwr = max(max_pwr, num)
                except ValueError:
                    pass
        sch._pwr_counter = max_pwr + 1

        # Remember the source tree so ``to_sexp_node`` can re-emit every
        # element the caller did not touch verbatim (issue #6051) and patch
        # edited ones in place (issue #6057).  Older-format files are still
        # regenerated wholesale at kct's version: their verbatim nodes would
        # skip KiCad's legacy-format conversions once restamped, and keeping
        # the old version would misdescribe kct-generated nodes (#6057 left
        # this unchanged deliberately).
        if _source_format_version(doc) >= KICAD_SCH_FORMAT_VERSION:
            sch._snapshot_source(doc)
        else:
            sch._text_note_sources = {}

        _log_info(
            f"Loaded schematic: {len(sch.symbols)} symbols, "
            f"{len(sch.power_symbols)} power symbols, "
            f"{len(sch.wires)} wires"
        )

        return sch

    def _build_lib_symbols_node(self) -> SExp:
        """Build lib_symbols section as SExp node."""
        lib_symbols = SExp.list("lib_symbols")

        added_lib_ids = set()

        # First, add any embedded lib_symbols from loaded schematics
        for sym_name, sym_node in self._embedded_lib_symbols.items():
            lib_symbols.append(sym_node)
            added_lib_ids.add(sym_name)

        # Then add any new symbol defs that weren't embedded
        for sym_def in self._symbol_defs.values():
            if sym_def.lib_id not in added_lib_ids:
                for sym_node in sym_def.to_sexp_nodes():
                    lib_symbols.append(sym_node)
                    added_lib_ids.add(sym_def.lib_id)

        return lib_symbols

    def _build_text_note_node(self, text: str, x: float, y: float, seen: dict) -> SExp:
        """Build a text note as SExp node (deterministic UUID, Issue #6076)."""
        return text_node(text, x, y, self._text_note_uuid(text, x, y, seen))

    def _header_slot_nodes(self) -> dict[str, SExp]:
        """Model-generated header/trailer nodes, keyed by node name."""
        return {
            "uuid": uuid_node(self.sheet_uuid),
            "paper": SExp.list("paper", self.paper),
            "title_block": title_block(
                title=self.title,
                date=self.date,
                revision=self.revision,
                company=self.company,
                comment1=self.comment1,
                comment2=self.comment2,
            ),
            "lib_symbols": self._build_lib_symbols_node(),
            "sheet_instances": sheet_instances(self.sheet_path, self.page),
        }

    def _element_nodes(self) -> list[tuple[object, SExp]]:
        """``(element, generated node)`` for every placed element, in write order."""
        out: list[tuple[object, SExp]] = []
        for sym in self.symbols:
            out.append((sym, sym.to_sexp_node(self.project_name, self.sheet_path)))
        for pwr in self.power_symbols:
            out.append((pwr, pwr.to_sexp_node(self.project_name, self.sheet_path)))
        for group in (
            self.wires,
            self.junctions,
            self.no_connects,
            self.labels,
            self.hier_labels,
            self.global_labels,
        ):
            for elem in group:
                out.append((elem, elem.to_sexp_node()))
        return out

    def _snapshot_source(self, doc: SExp) -> None:
        """Fingerprint every loaded element so untouched ones round-trip verbatim.

        Called once at the end of :meth:`_from_sexp`, after all model state
        (``project_name``, ``page``, symbol defs) is populated, so the
        fingerprints describe exactly what an unmodified save would generate.
        """
        self._source_doc = doc
        consumed: list[SExp] = []
        for elem, node in self._element_nodes():
            src = getattr(elem, "_source_node", None)
            if src is not None:
                elem._source_fp = _fingerprint(node)  # type: ignore[attr-defined]
                elem._source_gen = node  # type: ignore[attr-defined]
                consumed.append(src)
        for nodes in self._text_note_sources.values():
            consumed.extend(nodes)
        for name, node in self._header_slot_nodes().items():
            src = doc.get(name)
            self._source_slots[name] = (src, _fingerprint(node), node)
            if src is not None:
                consumed.append(src)
        self._source_consumed = consumed

    def to_sexp_node(self) -> SExp:
        """Build complete schematic as SExp tree.

        A schematic built from scratch is generated entirely from the model.
        A loaded one (KiCad format ``version`` >= ``KICAD_SCH_FORMAT_VERSION``)
        is written source-preserving: see :meth:`_to_sexp_node_preserving`.
        """
        # Issue #6076: settle every provisional UUID (write-back) first.
        self._assign_provisional_uuids()
        if self._source_doc is not None:
            return self._to_sexp_node_preserving(self._source_doc)

        # generator_version is a strict-typed string field in KiCad; emit the
        # value as a quoted atom so kicad-cli accepts the file even though the
        # version string textually parses as a number.
        slots = self._header_slot_nodes()
        root = SExp.list(
            "kicad_sch",
            SExp.list("version", KICAD_SCH_FORMAT_VERSION),
            SExp.list("generator", "eeschema"),
            SExp.list("generator_version", SExp.quoted_atom(KICAD_GENERATOR_VERSION)),
            slots["uuid"],
            slots["paper"],
        )
        root.append(slots["title_block"])
        root.append(slots["lib_symbols"])

        # Symbols, power symbols, wires, junctions, no-connects, labels,
        # hierarchical labels, global labels
        for _elem, node in self._element_nodes():
            root.append(node)

        # Text notes
        seen_notes: dict = {}
        for text, x, y in self.text_notes:
            root.append(self._build_text_note_node(text, x, y, seen_notes))

        root.append(slots["sheet_instances"])

        return root

    def _to_sexp_node_preserving(self, doc: SExp) -> SExp:
        """Write a loaded schematic, changing only what the caller edited (issue #6051).

        * Elements whose generated form still matches their load-time
          fingerprint are emitted as the original source node, in their
          original position.
        * Edited elements keep their source node with only the edited atoms
          changed (issue #6057, see :func:`_patch_node`), in place.
        * Elements removed from the model are dropped.
        * Elements added to the model are appended before the trailer
          (``sheet_instances`` / ``embedded_fonts``).
        * Top-level nodes the model does not parse (``polyline``, ``sheet``,
          ``bus``, ``rectangle``, ``image``, ``embedded_fonts`` ...) and the
          ``version`` / ``generator`` / ``generator_version`` header pass
          through unchanged.  The format ``version`` is deliberately kept:
          the file is only preserving-loaded when its version is at least
          ``KICAD_SCH_FORMAT_VERSION``, so every regenerated node is valid
          under it, while relabelling verbatim newer-format content with an
          older version would misdescribe it.
        """
        index = {id(child): i for i, child in enumerate(doc.children)}
        replaced: dict[int, SExp] = {}
        new_header: list[SExp] = []
        new_body: list[SExp] = []
        new_trailer: list[SExp] = []

        def place(src: SExp | None, fp: str | None, gen: SExp | None, node: SExp) -> bool:
            if src is None or id(src) not in index or id(src) in replaced:
                return False
            if _fingerprint(node) == fp:
                replaced[id(src)] = src
            elif gen is None:
                replaced[id(src)] = node
            else:
                # Edited: replay the edit onto the source node (issue #6057).
                replaced[id(src)] = _patch_node(src, gen, node)
            return True

        for name, node in self._header_slot_nodes().items():
            src, fp, gen = self._source_slots.get(name, (None, None, None))
            if src is not None:
                place(src, fp, gen, node)
            elif _fingerprint(node) != fp:
                # Absent from the source and set since load: add it.
                (new_trailer if name in _TRAILER_NODES else new_header).append(node)

        for elem, node in self._element_nodes():
            src = getattr(elem, "_source_node", None)
            fp = getattr(elem, "_source_fp", None)
            if not place(src, fp, getattr(elem, "_source_gen", None), node):
                new_body.append(node)

        # Text notes are plain ``(text, x, y)`` tuples, so an edited note is a
        # removed tuple plus an added one.  Notes matching a source exactly
        # are kept; a leftover added note is then paired with a leftover
        # source note of the same text (moved) or position (retyped) and
        # patched onto it, keeping its font, justify and UUID (issue #6057).
        live = [
            (key, src)
            for key, nodes in self._text_note_sources.items()
            for src in nodes
            if id(src) in index
        ]
        pending: dict[tuple[str, float, float], list[SExp]] = {}
        for key, src in live:
            pending.setdefault(key, []).append(src)
        unmatched: list[tuple[str, float, float]] = []
        for note in self.text_notes:
            candidates = pending.get(note)
            src = candidates.pop(0) if candidates else None
            if src is None or id(src) in replaced:
                unmatched.append(note)
            else:
                replaced[id(src)] = src
        leftover = [(key, src) for key, src in live if id(src) not in replaced]
        seen_notes: dict = {}
        for text, x, y in unmatched:
            # A position match wins over a text match: a note retyped in place
            # keeps its own font and UUID even when another deleted note
            # happens to carry the new text (issue #6085).
            pair = next((p for p in leftover if p[0][1:] == (x, y)), None) or next(
                (p for p in leftover if p[0][0] == text), None
            )
            if pair is None:
                new_body.append(self._build_text_note_node(text, x, y, seen_notes))
                continue
            leftover.remove(pair)
            (old_text, old_x, old_y), src = pair
            uuid_child = src.get("uuid")
            note_uuid = (
                str(uuid_child.get_first_atom())
                if uuid_child
                else self._text_note_uuid(text, x, y, seen_notes)
            )
            replaced[id(src)] = _patch_node(
                src,
                text_node(old_text, old_x, old_y, note_uuid),
                text_node(text, x, y, note_uuid),
            )

        # Held as node references (not ``id()`` ints) so deepcopy/pickle remap
        # them together with ``_source_doc`` (issue #6071).
        consumed_ids = {id(n) for n in self._source_consumed}
        children: list[SExp] = []
        for child in doc.children:
            if id(child) in replaced:
                children.append(replaced[id(child)])
            elif id(child) not in consumed_ids:
                children.append(child)  # not modelled: pass through
            # else: removed from the model since load

        def insert_before(nodes: list[SExp], stop: frozenset[str], invert: bool) -> None:
            if not nodes:
                return
            for i, child in enumerate(children):
                if (child.name in stop) != invert:
                    children[i:i] = nodes
                    return
            children.extend(nodes)

        # version must stay first; new header nodes go after the header.
        insert_before(new_header, _HEADER_NODES, invert=True)
        insert_before(new_body, _TRAILER_NODES, invert=False)
        insert_before(new_trailer, frozenset({"embedded_fonts"}), invert=False)

        root = SExp.list("kicad_sch")
        for child in children:
            root.append(child)
        return root

    def to_sexp(self) -> str:
        """Generate complete schematic S-expression string."""
        return self.to_sexp_node().to_string()

    def content_bounds(self) -> tuple[float, float, float, float] | None:
        """Compute the absolute extent of all placed content in mm.

        Covers symbol bounding boxes, power symbols, wires, junctions,
        no-connects, all label kinds, and text notes.  Returns
        ``(min_x, min_y, max_x, max_y)``, or ``None`` when the schematic
        has no placed content.
        """
        xs: list[float] = []
        ys: list[float] = []

        for sym in self.symbols:
            try:
                bx1, by1, bx2, by2 = sym.bounding_box(padding=0.0)
                xs.extend((bx1, bx2))
                ys.extend((by1, by2))
            except Exception:
                # Fall back to the placement origin when pin geometry is
                # unavailable (e.g. partially-constructed symbol defs).
                xs.append(sym.x)
                ys.append(sym.y)

        for pwr in self.power_symbols:
            xs.append(pwr.x)
            ys.append(pwr.y)

        for wire in self.wires:
            xs.extend((wire.x1, wire.x2))
            ys.extend((wire.y1, wire.y2))

        for junc in self.junctions:
            xs.append(junc.x)
            ys.append(junc.y)

        for nc in self.no_connects:
            xs.append(nc.x)
            ys.append(nc.y)

        for label in (*self.labels, *self.hier_labels, *self.global_labels):
            xs.append(label.x)
            ys.append(label.y)

        for _text, tx, ty in self.text_notes:
            xs.append(tx)
            ys.append(ty)

        if not xs:
            return None
        return (min(xs), min(ys), max(xs), max(ys))

    def auto_size_paper(self, margin: float | None = None) -> str:
        """Escalate ``self.paper`` until the content extent fits the sheet.

        Walks the standard ladder (A4 -> A3 -> A2 -> A1 -> A0) starting at
        the currently declared size — the sheet is never shrunk.  KiCad
        clips content placed beyond the declared paper bounds in every
        faithful render, so a generator that placed content past the sheet
        edge previously produced schematics whose renders were silently
        truncated (issue #3530).

        Args:
            margin: Clearance in mm kept between the content extent and
                the sheet edge (default:
                :data:`~kicad_tools.schematic.models.paper.DEFAULT_PAPER_MARGIN_MM`).

        Returns:
            The (possibly updated) paper name.
        """
        from .paper import (
            DEFAULT_PAPER_MARGIN_MM,
            paper_dimensions,
            select_paper_for_extent,
        )

        if margin is None:
            margin = DEFAULT_PAPER_MARGIN_MM

        bounds = self.content_bounds()
        if bounds is None:
            return self.paper

        min_x, min_y, max_x, max_y = bounds

        if min_x < 0 or min_y < 0:
            _log_warning(
                f"Schematic content extends to negative coordinates "
                f"(min x={min_x:.1f}, min y={min_y:.1f} mm); KiCad clips "
                f"content above/left of the sheet origin regardless of "
                f"paper size. Shift the affected elements into positive "
                f"coordinate space."
            )

        declared = paper_dimensions(self.paper)
        if declared is None:
            # Custom/unmodeled paper string ("User ...", ANSI sizes):
            # don't second-guess an explicit declaration we can't parse.
            return self.paper

        decl_w, decl_h = declared
        if max_x + margin <= decl_w and max_y + margin <= decl_h:
            return self.paper  # content already fits the declared sheet

        base = self.paper.split()[0]
        chosen = select_paper_for_extent(max_x, max_y, margin=margin, minimum=base)
        if chosen is None:
            _log_warning(
                f"Schematic content extent ({max_x:.0f} x {max_y:.0f} mm) "
                f"exceeds even A0 (1189 x 841 mm); declaring A0 but KiCad "
                f"will clip. Split the design into hierarchical sheets."
            )
            chosen = "A0"

        if chosen != self.paper:
            _log_warning(
                f"Schematic content extent ({max_x:.0f} x {max_y:.0f} mm) "
                f"overflows the declared {self.paper} sheet "
                f"({decl_w:.0f} x {decl_h:.0f} mm); auto-sizing paper to "
                f"{chosen} so renders are not clipped (issue #3530)."
            )
            self.paper = chosen
        return self.paper

    def write(self, path: str | Path, auto_size_paper: bool = True):
        """Write schematic to file.

        Args:
            path: Destination ``.kicad_sch`` path.
            auto_size_paper: When ``True`` (default), escalate the declared
                paper size along the A4->A0 ladder if the placed content
                overflows the current sheet (issue #3530).  The sheet is
                never shrunk.  Pass ``False`` to write the declared paper
                verbatim (a warning is still logged on overflow).
        """
        path = Path(path)
        if auto_size_paper:
            self.auto_size_paper()
        else:
            self._warn_if_content_overflows()
        content = self.to_sexp()
        path.write_text(content)
        # Store the path for later use (e.g., run_erc)
        self._saved_path = path
        # Emit the companion sym-lib-table + .kicad_sym so kicad-cli's ERC
        # nickname-presence check resolves ``kicad_tools_pwr`` (issue #3943).
        self._write_sym_lib_table(path)
        _log_info(
            f"Wrote schematic to {path} ({len(self.symbols)} symbols, {len(self.wires)} wires)"
        )

    # KiCad's sym-lib-table version tag (KiCad 7+ uses version 7).
    _SYM_LIB_TABLE_VERSION = 7

    def _write_sym_lib_table(self, sch_path: Path) -> None:
        """Emit sidecar files that satisfy ERC's library-nickname check.

        The generator embeds synthesized ``kicad_tools_pwr:{net}`` power
        symbols directly in the schematic's ``lib_symbols`` block, which is
        authoritative for *loading* the file. However, ``kicad-cli sch erc``
        (and the KiCad GUI's ERC runner) separately validate that every
        library *nickname* referenced by a placed symbol resolves through
        the project's ``sym-lib-table``. Generated schematics never write
        that table, so ERC logs a spurious "does not include the symbol
        library 'kicad_tools_pwr'" warning even though the definitions are
        embedded (issue #3943).

        This writes two sidecars next to the schematic:

        * ``kicad_tools_pwr.kicad_sym`` — a real symbol library holding the
          synthesized definitions (names stripped of the nickname prefix),
          so KiCad can locate the nickname's backing file.
        * ``sym-lib-table`` — registers the ``kicad_tools_pwr`` nickname,
          pointing at the ``.kicad_sym`` above via ``${KIPRJMOD}``.

        Both are no-ops when the schematic uses no synthesized power symbols
        (``add_pwr_symbol``), so plain schematics gain no spurious sidecar.

        A pre-existing ``sym-lib-table`` (e.g. a user's own project table)
        is **merged**, never clobbered: the ``kicad_tools_pwr`` entry is
        appended only when absent. If the existing table cannot be parsed,
        it is left untouched and a warning is logged.

        Note: KiCad only consults the local ``sym-lib-table`` when a
        companion ``.kicad_pro`` project file exists in the same directory
        (which board generators emit). When no project file is present the
        sidecars are harmless — they simply aren't read.
        """
        if not self._synthesized_pwr_defs:
            return

        directory = sch_path.parent

        try:
            self._write_synth_pwr_symbol_lib(directory / "kicad_tools_pwr.kicad_sym")
            self._merge_sym_lib_table_entry(directory / "sym-lib-table")
        except OSError as exc:
            # Read-only directory or similar — degrade to a warning rather
            # than failing the whole write() after the .kicad_sch landed.
            _log_warning(
                f"Could not write sym-lib-table sidecar for synthesized power "
                f"symbols next to {sch_path.name}: {exc}. ERC may warn about a "
                f"missing 'kicad_tools_pwr' library until the sidecar exists."
            )

    def _write_synth_pwr_symbol_lib(self, path: Path) -> None:
        """Write the ``kicad_tools_pwr.kicad_sym`` backing library.

        Each synthesized def is emitted with its outer symbol name reduced
        from ``kicad_tools_pwr:{net}`` to just ``{net}`` — inside a
        ``.kicad_sym`` file the nickname is supplied by the table entry, so
        the symbol name must be bare (matching how stock KiCad libraries
        store their symbols).
        """
        lib = SExp.list(
            "kicad_symbol_lib",
            SExp.list("version", KICAD_SYM_FORMAT_VERSION),
            SExp.list("generator", "kicad-tools"),
        )
        prefix = f"{self._PWR_SYNTH_LIB_PREFIX}:"
        for net_name, sym_node in self._synthesized_pwr_defs.items():
            bare = copy.deepcopy(sym_node)
            # The def's first atom is the prefixed lib_id
            # (``kicad_tools_pwr:{net}``); strip the nickname prefix.
            first = bare.get_first_atom()
            if isinstance(first, str) and first.startswith(prefix):
                bare.set_atom(0, net_name)
            lib.append(bare)
        path.write_text(lib.to_string() + "\n")

    def _merge_sym_lib_table_entry(self, path: Path) -> None:
        """Ensure ``sym-lib-table`` registers the ``kicad_tools_pwr`` nickname.

        Creates the table if absent; otherwise appends the entry only when
        the nickname is not already present, preserving any user entries.
        """
        from kicad_tools.sexp import parse_string

        nickname = self._PWR_SYNTH_LIB_PREFIX
        entry = SExp.list(
            "lib",
            SExp.list("name", SExp.quoted_atom(nickname)),
            SExp.list("type", SExp.quoted_atom("KiCad")),
            SExp.list(
                "uri",
                SExp.quoted_atom("${KIPRJMOD}/kicad_tools_pwr.kicad_sym"),
            ),
            SExp.list("options", SExp.quoted_atom("")),
            SExp.list(
                "descr",
                SExp.quoted_atom("kicad-tools synthesized power symbols"),
            ),
        )

        if path.exists():
            try:
                table = parse_string(path.read_text())
            except Exception as exc:
                _log_warning(
                    f"Existing sym-lib-table at {path} could not be parsed "
                    f"({exc}); leaving it untouched. ERC may warn about the "
                    f"'kicad_tools_pwr' library until the entry is added."
                )
                return
            if table.name != "sym_lib_table":
                _log_warning(
                    f"Existing sym-lib-table at {path} is not a sym_lib_table "
                    f"node; leaving it untouched."
                )
                return
            # Skip if the nickname is already registered.
            for lib_node in table.find_all("lib"):
                name_node = lib_node.get("name")
                if name_node and str(name_node.get_first_atom() or "") == nickname:
                    return
            table.append(entry)
            path.write_text(table.to_string() + "\n")
            return

        table = SExp.list(
            "sym_lib_table",
            SExp.list("version", self._SYM_LIB_TABLE_VERSION),
            entry,
        )
        path.write_text(table.to_string() + "\n")

    def _warn_if_content_overflows(self) -> None:
        """Log a warning when content exceeds the declared sheet bounds."""
        from .paper import paper_dimensions

        bounds = self.content_bounds()
        declared = paper_dimensions(self.paper)
        if bounds is None or declared is None:
            return
        _min_x, _min_y, max_x, max_y = bounds
        decl_w, decl_h = declared
        if max_x > decl_w or max_y > decl_h:
            _log_warning(
                f"Schematic content extent ({max_x:.0f} x {max_y:.0f} mm) "
                f"overflows the declared {self.paper} sheet "
                f"({decl_w:.0f} x {decl_h:.0f} mm); renders will be "
                f"clipped (auto-sizing disabled)."
            )

    def run_erc(self, output_path: str | Path | None = None) -> ERCReport:
        """Run KiCad ERC on this schematic.

        Invokes kicad-cli to run electrical rules check and returns
        the parsed report with violations, errors, and warnings.

        The schematic must be saved to disk first via write().

        Args:
            output_path: Optional path for ERC report file.
                        If None, uses a temporary file that is cleaned up.

        Returns:
            Parsed ERCReport with violations, errors, warnings.

        Raises:
            KiCadCLIError: If kicad-cli is not found or fails.
            ValueError: If schematic has not been saved to disk.

        Example::

            sch = Schematic("My Design")
            sch.add_symbol("Device:R", 100, 50, "R1", "10k")
            sch.write("design.kicad_sch")

            report = sch.run_erc()
            if report.error_count > 0:
                for error in report.errors:
                    print(f"ERC Error: {error.type} at {error.location_str}")
        """
        from kicad_tools.cli.runner import run_erc as cli_run_erc
        from kicad_tools.erc import ERCReport
        from kicad_tools.exceptions import KiCadCLIError

        # Get the saved path
        saved_path = getattr(self, "_saved_path", None)
        if saved_path is None:
            raise ValueError(
                "Schematic must be saved to disk before running ERC. "
                "Use write() to save the schematic first."
            )

        if not saved_path.exists():
            raise ValueError(f"Schematic file not found: {saved_path}")

        # Convert output_path to Path if provided
        output = Path(output_path) if output_path else None

        # Run ERC via kicad-cli
        result = cli_run_erc(saved_path, output_path=output)

        if not result.success:
            raise KiCadCLIError(
                f"ERC failed: {result.stderr}",
                context={
                    "schematic": str(saved_path),
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
