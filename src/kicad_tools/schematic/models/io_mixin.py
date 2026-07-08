"""
Schematic I/O Mixin

Provides file loading and saving capabilities for Schematic class.
"""

from __future__ import annotations

import copy
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

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
    from kicad_tools.erc import ERCReport

    from .schematic import Schematic


class SchematicIOMixin:
    """Mixin providing I/O operations for Schematic class."""

    if TYPE_CHECKING:
        # Attributes provided by the concrete ``Schematic`` class (via
        # ``SchematicElementsMixin`` and ``Schematic.__init__``).  Declared
        # here so mypy can see them when this mixin references them.
        _PWR_SYNTH_LIB_PREFIX: str
        _synthesized_pwr_defs: dict[str, SExp]

    @classmethod
    def load(cls, path: str | Path) -> Schematic:
        """Load a schematic from a .kicad_sch file.

        This enables round-trip editing: load -> modify -> save.

        Args:
            path: Path to the .kicad_sch file

        Returns:
            A Schematic instance populated with all elements from the file

        Example:
            sch = Schematic.load("power.kicad_sch")
            sch.add_symbol("Device:R", 100, 100, "R5", "10k")
            sch.write("power.kicad_sch")
        """
        from kicad_tools.sexp import parse_file

        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"Schematic file not found: {path}")

        doc = parse_file(path)
        sch = cls._from_sexp(doc)
        # Track the source path so operations that need to walk
        # sub-sheets (e.g., extract_netlist(hierarchical=True), run_erc)
        # can resolve relative sheet references.
        sch._saved_path = path
        return sch

    @classmethod
    def _from_sexp(cls, doc: SExp) -> Schematic:
        """Create a Schematic from a parsed S-expression tree.

        This is the internal method that does the actual parsing.
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
        sheet_uuid = str(uuid_node_elem.get_first_atom()) if uuid_node_elem else str(uuid.uuid4())

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
                    sch.power_symbols.append(pwr)
                else:
                    sym = SymbolInstance.from_sexp(
                        child, symbol_defs=sch._symbol_defs, lib_symbols=embedded_lib_symbols
                    )
                    sch.symbols.append(sym)
                    # Cache the symbol def
                    sch._symbol_defs[sym.symbol_def.lib_id] = sym.symbol_def

        # Parse wires
        for child in doc.children:
            if child.name == "wire":
                sch.wires.append(Wire.from_sexp(child))

        # Parse junctions
        for child in doc.children:
            if child.name == "junction":
                sch.junctions.append(Junction.from_sexp(child))

        # Parse no-connects
        for child in doc.children:
            if child.name == "no_connect":
                sch.no_connects.append(NoConnect.from_sexp(child))

        # Parse labels
        for child in doc.children:
            if child.name == "label":
                sch.labels.append(Label.from_sexp(child))

        # Parse hierarchical labels
        for child in doc.children:
            if child.name == "hierarchical_label":
                sch.hier_labels.append(HierarchicalLabel.from_sexp(child))

        # Parse global labels
        for child in doc.children:
            if child.name == "global_label":
                sch.global_labels.append(GlobalLabel.from_sexp(child))

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

    def _build_text_note_node(self, text: str, x: float, y: float) -> SExp:
        """Build a text note as SExp node."""
        return text_node(text, x, y, str(uuid.uuid4()))

    def to_sexp_node(self) -> SExp:
        """Build complete schematic as SExp tree."""
        # generator_version is a strict-typed string field in KiCad; emit the
        # value as a quoted atom so kicad-cli accepts the file even though
        # "9.0" textually parses as a number.
        root = SExp.list(
            "kicad_sch",
            SExp.list("version", 20231120),
            SExp.list("generator", "eeschema"),
            SExp.list("generator_version", SExp.quoted_atom("9.0")),
            uuid_node(self.sheet_uuid),
            SExp.list("paper", self.paper),
        )

        # Title block
        root.append(
            title_block(
                title=self.title,
                date=self.date,
                revision=self.revision,
                company=self.company,
                comment1=self.comment1,
                comment2=self.comment2,
            )
        )

        # Library symbols
        root.append(self._build_lib_symbols_node())

        # Symbol instances
        for sym in self.symbols:
            root.append(sym.to_sexp_node(self.project_name, self.sheet_path))

        # Power symbols
        for pwr in self.power_symbols:
            root.append(pwr.to_sexp_node(self.project_name, self.sheet_path))

        # Wires
        for wire in self.wires:
            root.append(wire.to_sexp_node())

        # Junctions
        for junc in self.junctions:
            root.append(junc.to_sexp_node())

        # No-connects
        for nc in self.no_connects:
            root.append(nc.to_sexp_node())

        # Labels
        for label in self.labels:
            root.append(label.to_sexp_node())

        # Hierarchical labels
        for hl in self.hier_labels:
            root.append(hl.to_sexp_node())

        # Global labels
        for gl in self.global_labels:
            root.append(gl.to_sexp_node())

        # Text notes
        for text, x, y in self.text_notes:
            root.append(self._build_text_note_node(text, x, y))

        # Sheet instances
        root.append(sheet_instances(self.sheet_path, self.page))

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
            SExp.list("version", 20231120),
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
