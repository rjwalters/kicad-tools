#!/usr/bin/env python3
"""
Repair missing project instances in KiCad hierarchical schematics.

Finds all symbols that lack ``(instances ...)`` blocks for the current
project, assigns non-conflicting reference designators, and writes the
instances blocks into the sub-sheet files.

Usage:
    # Preview changes
    kicad-tools sch repair-instances board.kicad_sch --dry-run

    # Fix all missing instances
    kicad-tools sch repair-instances board.kicad_sch

    # Fix with backup
    kicad-tools sch repair-instances board.kicad_sch --backup
"""

import re
import sys
from pathlib import Path

from kicad_tools.cli.modify_schematic import create_backup
from kicad_tools.cli.sch_re_annotate import (
    _apply_uuid_reference_rename,
    _detect_indent,
    _detect_project_info,
    _format_reference,
    _parse_reference,
)
from kicad_tools.cli.sch_set_footprint import _collect_schematic_files

# Whitespace token for regex: matches one or more tabs or spaces
_WS = r"[ \t]+"

# Mapping from bare symbol-name references (used as lib_id-derived
# reference designators) to the proper KiCad annotation prefix.
# For example, a PWR_FLAG symbol gets reference "#PWR_FLAG" which
# should be re-annotated as "#FLG01", "#FLG02", etc.
_BARE_REF_PREFIX_MAP: dict[str, str] = {
    "#PWR_FLAG": "#FLG",
}


def _find_sub_block_span(block: str, start: int) -> int:
    """Walk forward from ``start`` (an opening ``(``) and return the position
    just past the matching closing ``)``.

    Skips over quoted strings so quoted parens don't confuse the depth count.
    Returns ``len(block)`` if no balanced close is found.
    """
    depth = 0
    i = start
    while i < len(block):
        ch = block[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        elif ch == '"':
            i += 1
            while i < len(block) and block[i] != '"':
                if block[i] == "\\":
                    i += 1
                i += 1
        i += 1
    return len(block)


def _scan_symbol_children(
    block: str,
) -> tuple[tuple[int, int] | None, list[dict]]:
    """Scan the direct children of a ``(symbol ...)`` block.

    *block* is the full ``(symbol ...)`` source text starting at the opening
    ``(``.  Returns a tuple ``(instances_span, project_children)`` where:

    - ``instances_span`` is ``(start, end)`` (relative to *block*) of the
      direct-child ``(instances ...)`` form, or ``None`` if absent.
    - ``project_children`` is a list of dicts describing each direct-child
      ``(project "..." ...)`` form (the malformed case — these are siblings
      of ``(instances)`` rather than children of it).  Each dict has keys
      ``name`` (project name string), ``span`` (``(start, end)`` relative
      to *block*), and ``reference`` (the first ``(reference "...")`` value
      found inside the project block, or empty string).

    Direct children of ``(symbol)`` open at depth-1 within the block (since
    the ``(symbol`` opening sets depth to 1).
    """
    if not block.startswith("(symbol"):
        return None, []

    instances_span: tuple[int, int] | None = None
    project_children: list[dict] = []

    # We've consumed the opening "(" of (symbol, so start at depth=1.
    # A direct child opens when depth goes 1 -> 2, i.e. when we see "(" at
    # current depth == 1.
    depth = 1
    i = len("(")  # advance past the leading "("
    while i < len(block):
        ch = block[i]
        if ch == '"':
            # skip over quoted strings
            i += 1
            while i < len(block) and block[i] != '"':
                if block[i] == "\\":
                    i += 1
                i += 1
            i += 1
            continue
        if ch == "(":
            if depth == 1:
                # Direct child: peek at the form head
                child_start = i
                child_end = _find_sub_block_span(block, child_start)
                child_text = block[child_start:child_end]
                # Identify form by leading token (e.g. "(instances",
                # "(project \"name\"")
                m_inst = re.match(r"\(instances\b", child_text)
                m_proj = re.match(r'\(project\s+"([^"]+)"', child_text)
                if m_inst:
                    instances_span = (child_start, child_end)
                elif m_proj:
                    proj_name = m_proj.group(1)
                    ref_m = re.search(r'\(reference "([^"]+)"', child_text)
                    project_children.append(
                        {
                            "name": proj_name,
                            "span": (child_start, child_end),
                            "reference": ref_m.group(1) if ref_m else "",
                        }
                    )
                # Jump past this child entirely
                i = child_end
                continue
            depth += 1
            i += 1
            continue
        if ch == ")":
            depth -= 1
            if depth == 0:
                break
            i += 1
            continue
        i += 1

    return instances_span, project_children


def _extract_symbols_with_instance_info(text: str, project_name: str) -> list[dict]:
    """Extract symbols and whether they have instances for the given project.

    Uses a bracket-depth approach to find complete symbol blocks, which is
    more robust than the regex-only approach used in ``sch_re_annotate``.

    Returns list of dicts with keys: reference, prefix, number, unit_suffix,
    lib_id, uuid, has_project_instance, has_wrong_project,
    has_loose_project_blocks, loose_project_blocks, symbol_abs_start.

    ``has_loose_project_blocks`` is True when one or more ``(project "..." ...)``
    forms appear at symbol-child indent (i.e. as siblings of, not children of,
    ``(instances)``).  This is the malformed shape that ``kicad-cli`` silently
    drops from the netlist.

    ``loose_project_blocks`` carries the per-form details (name, absolute
    text span, and the reference value it holds) so the repair phase can
    splice them out without re-parsing.

    ``symbol_abs_start`` is the absolute offset (in *text*) of the ``(symbol``
    opening paren for this symbol — the loose-project spans are relative to
    that offset.
    """
    symbols = []
    ref_pattern = re.compile(r'\(property "Reference" "([^"]+)"')
    value_pattern = re.compile(r'\(property "Value" "([^"]*)"')
    in_bom_pattern = re.compile(r"\(in_bom\s+(yes|no)\)")
    on_board_pattern = re.compile(r"\(on_board\s+(yes|no)\)")

    # Find symbol block starts: lines matching <ws>(symbol\n<ws>(lib_id "...")
    start_pattern = re.compile(
        r"(?:^|\n)(" + _WS + r")\(symbol\n" + _WS + r'\(lib_id "([^"]+)"\)',
        re.DOTALL,
    )

    for start_match in start_pattern.finditer(text):
        indent = start_match.group(1)
        lib_id = start_match.group(2)
        block_start = start_match.start()

        # Find the end of this symbol block by tracking parenthesis depth
        # Start from the opening '(' of '(symbol'
        paren_start = text.index("(symbol", block_start)
        block_end = _find_sub_block_span(text, paren_start)

        block = text[paren_start:block_end]

        ref_match = ref_pattern.search(block)
        if not ref_match:
            continue

        ref = ref_match.group(1)

        # Extract Value property + in_bom/on_board for downstream consumers
        # (e.g. validate's "graphical-only symbol" skip filter).  These are
        # optional — sub-sheet symbol blocks always carry them in KiCad 8+,
        # but minimal hand-built fixtures may omit them.  When absent, the
        # defaults (value="", in_bom=True, on_board=True) match KiCad's
        # behaviour: a symbol with no explicit (in_bom no) is in the BOM.
        value_match = value_pattern.search(block)
        sym_value = value_match.group(1) if value_match else ""
        in_bom_match = in_bom_pattern.search(block)
        sym_in_bom = (in_bom_match.group(1) == "yes") if in_bom_match else True
        on_board_match = on_board_pattern.search(block)
        sym_on_board = (on_board_match.group(1) == "yes") if on_board_match else True

        # Structural scan: locate the direct-child (instances ...) form and
        # any direct-child (project ...) forms (the malformed shape).
        instances_span, loose_projects = _scan_symbol_children(block)

        has_instances_block = instances_span is not None
        if has_instances_block:
            inst_start, inst_end = instances_span
            instances_text = block[inst_start:inst_end]
            has_project = f'(project "{project_name}"' in instances_text
        else:
            has_project = False

        has_loose_project_blocks = bool(loose_projects)

        # Skip power symbols only when they have no instances block at all
        # (legacy behavior) AND no loose project blocks to repair.  Power
        # symbols with an instances block referencing the *wrong* project
        # or with stray loose project blocks still need repair.
        if lib_id.startswith("power:"):
            if (not has_instances_block and not has_loose_project_blocks) or (
                has_project and not has_loose_project_blocks
            ):
                continue

        # Find the symbol-level uuid (the last uuid in the block, or the
        # one that's a direct child of the symbol -- not inside a pin)
        # Strategy: find all uuids, the symbol-level one is typically the
        # one right before (instances) or at the end of the block.
        # We search for (uuid "...") that is NOT inside a (pin ...) block.
        # Simple approach: find the uuid that appears after all pin blocks.
        all_uuids = list(re.finditer(r'\(uuid "([^"]+)"\)', block))
        # The symbol-level uuid is typically the last one before instances,
        # or the last one in the block if no instances.
        # Pin uuids are nested inside (pin "X" (uuid "...")) blocks.
        # The symbol uuid is at indent level 2 (direct child of symbol).
        sym_uuid = ""
        for uuid_m in reversed(all_uuids):
            # Check if this uuid is inside a (pin ...) block
            preceding = block[: uuid_m.start()]
            # Count unmatched (pin openings
            pin_opens = len(re.findall(r"\(pin\b", preceding))
            pin_closes = preceding.count(")")  # rough estimate
            # Better approach: check if (pin appears after the last )
            # before our uuid position
            last_paren_close = preceding.rfind(")")
            last_pin_open = preceding.rfind("(pin ")
            if last_pin_open > last_paren_close:
                # uuid is inside a pin block
                continue
            sym_uuid = uuid_m.group(1)
            break

        if not sym_uuid and all_uuids:
            sym_uuid = all_uuids[-1].group(1)

        prefix, number, unit_suffix = _parse_reference(ref)

        # has_wrong_project means: instances block exists, names the wrong
        # project, AND there are no loose project blocks to take precedence.
        # Loose-project repair is handled separately.
        has_wrong_project = has_instances_block and not has_project and not has_loose_project_blocks

        symbols.append(
            {
                "reference": ref,
                "prefix": prefix,
                "number": number,
                "unit_suffix": unit_suffix,
                "lib_id": lib_id,
                "uuid": sym_uuid,
                "value": sym_value,
                "in_bom": sym_in_bom,
                "on_board": sym_on_board,
                "has_project_instance": has_project,
                "has_wrong_project": has_wrong_project,
                "has_loose_project_blocks": has_loose_project_blocks,
                "loose_project_blocks": loose_projects,
                "symbol_abs_start": paren_start,
            }
        )

    return symbols


def _find_symbol_block(text: str, sym_uuid: str) -> tuple[int, int] | None:
    """Find the start and end positions of the symbol block containing *sym_uuid*.

    Uses bracket-depth parsing for robustness.
    Returns (start, end) positions or None if not found.
    """
    uuid_str = f'(uuid "{sym_uuid}")'
    uuid_pos = text.find(uuid_str)
    if uuid_pos == -1:
        return None

    # Walk backward to find the start of this symbol block
    start_pattern = re.compile(_WS + r"\(symbol\n")
    block_start = -1
    for m in start_pattern.finditer(text, 0, uuid_pos):
        block_start = m.start()
    if block_start == -1:
        return None

    # Find the opening paren of (symbol
    paren_start = text.index("(symbol", block_start)

    # Walk forward tracking depth to find closing paren
    depth = 0
    i = paren_start
    while i < len(text):
        ch = text[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return paren_start, i + 1
        elif ch == '"':
            i += 1
            while i < len(text) and text[i] != '"':
                if text[i] == "\\":
                    i += 1
                i += 1
        i += 1

    return None


def _insert_project_instance(
    text: str,
    sym_uuid: str,
    project_name: str,
    instance_path: str,
    new_ref: str,
    unit: int = 1,
    *,
    replace_wrong_project: bool = False,
    repair_loose_blocks: bool = False,
) -> str:
    """Insert or replace a project instance entry in a symbol's block.

    If the symbol has an ``(instances)`` block and *replace_wrong_project*
    is ``True``, any existing ``(project "...")`` entry whose name does
    **not** match *project_name* will have its name replaced in-place so
    that stale project references are corrected without leaving duplicates.

    If the symbol has an ``(instances)`` block but *replace_wrong_project*
    is ``False``, a new ``(project ...)`` entry is appended.

    If the symbol has no ``(instances)`` block, one is created before the
    symbol's closing parenthesis.

    If *repair_loose_blocks* is ``True``, any ``(project ...)`` forms at
    symbol-child indent (siblings of ``(instances)`` rather than children
    of it) are removed before the canonical project entry is inserted
    inside ``(instances)``.  This handles the malformed shape that
    ``kicad-cli`` silently drops from the netlist.

    Uses bracket-depth parsing for robustness with varying file layouts.
    """
    bounds = _find_symbol_block(text, sym_uuid)
    if bounds is None:
        return text

    start, end = bounds
    block = text[start:end]

    ind = _detect_indent(text)
    i2 = ind * 2
    i3 = ind * 3
    i4 = ind * 4
    i5 = ind * 5

    project_entry = (
        f'{i3}(project "{project_name}"\n'
        f'{i4}(path "{instance_path}"\n'
        f'{i5}(reference "{new_ref}")\n'
        f"{i5}(unit {unit})\n"
        f"{i4})\n"
        f"{i3})\n"
    )

    # Repair loose project blocks first (if requested).  This rewrites the
    # symbol block in place to remove any direct-child (project ...) forms,
    # so subsequent logic sees a clean structure.  We also normalise any
    # collapsed/empty (instances) form (e.g. ``(instances)`` on a single
    # line) into a multi-line shape so the downstream insertion logic — which
    # assumes the closing ``)`` of (instances) sits on its own line — can
    # operate uniformly.
    if repair_loose_blocks:
        instances_span_pre, loose_projects = _scan_symbol_children(block)
        if loose_projects:
            # Delete loose blocks from last to first so earlier spans stay
            # valid.  Each span is relative to *block*.  We also strip the
            # surrounding indentation and trailing newline so we don't leave
            # blank lines behind.
            new_block = block
            for proj in sorted(loose_projects, key=lambda p: p["span"][0], reverse=True):
                p_start, p_end = proj["span"]
                # Extend deletion to the start of the line containing
                # p_start (consumes the leading indent) and to the end of
                # the line containing p_end (consumes the trailing newline).
                line_start = new_block.rfind("\n", 0, p_start) + 1
                line_end = new_block.find("\n", p_end)
                if line_end == -1:
                    line_end = len(new_block)
                else:
                    line_end += 1  # include the newline
                new_block = new_block[:line_start] + new_block[line_end:]
            # Splice the cleaned block back into the full text.
            text = text[:start] + new_block + text[end:]
            # Re-derive bounds and refresh block view.
            bounds = _find_symbol_block(text, sym_uuid)
            if bounds is None:
                return text
            start, end = bounds
            block = text[start:end]

        # Normalise a collapsed/empty (instances) form.  The downstream
        # insertion code expects (instances)'s closing ``)`` to be on its
        # own line; rewrite ``(instances)`` to a multi-line form so the
        # generic insertion path works.
        post_instances_span, _ = _scan_symbol_children(block)
        if post_instances_span is not None:
            inst_start, inst_end = post_instances_span
            inst_text = block[inst_start:inst_end]
            if "\n" not in inst_text:
                # Collapsed form like ``(instances)`` — expand it.
                replacement = f"(instances\n{i2})"
                new_block = block[:inst_start] + replacement + block[inst_end:]
                text = text[:start] + new_block + text[end:]
                # Re-derive bounds and refresh block view.
                bounds = _find_symbol_block(text, sym_uuid)
                if bounds is None:
                    return text
                start, end = bounds
                block = text[start:end]

    # Check if block already has (instances
    instances_pos = block.find("(instances")
    if instances_pos != -1:
        # Check if this project already exists *inside* (instances).
        # Re-scan structurally so a stray loose (project) (which we should
        # have removed above, but defensively check) doesn't fool us.
        instances_span, _ = _scan_symbol_children(block)
        already_has_project = False
        if instances_span is not None:
            inst_start, inst_end = instances_span
            already_has_project = f'(project "{project_name}"' in block[inst_start:inst_end]
        if already_has_project:
            return text

        # If the instances block has a wrong project name, replace it
        if replace_wrong_project:
            wrong_project_match = re.search(r'\(project "([^"]+)"', block)
            if wrong_project_match:
                wrong_name = wrong_project_match.group(1)
                # Replace the wrong project name with the correct one
                # in the full text at the correct absolute position
                abs_pos = start + wrong_project_match.start()
                old_str = f'(project "{wrong_name}"'
                new_str = f'(project "{project_name}"'
                text = text[:abs_pos] + new_str + text[abs_pos + len(old_str) :]
                # Also update the reference and path within this project block
                # Re-find the symbol block since positions shifted
                bounds2 = _find_symbol_block(text, sym_uuid)
                if bounds2 is not None:
                    s2, e2 = bounds2
                    block2 = text[s2:e2]
                    # Replace the path
                    new_block = re.sub(
                        r'(\(path ")[^"]+"',
                        rf'\g<1>{instance_path}"',
                        block2,
                        count=1,
                    )
                    # Replace the reference
                    new_block = re.sub(
                        r'(\(reference ")[^"]+"',
                        rf'\g<1>{new_ref}"',
                        new_block,
                        count=1,
                    )
                    text = text[:s2] + new_block + text[e2:]
                return text

        # Find the closing ) of the (instances block
        # Walk from instances_pos tracking depth
        depth = 0
        i = instances_pos
        while i < len(block):
            ch = block[i]
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    # Insert the new project entry before this closing )
                    insert_pos = start + i
                    # Find the start of the line containing this )
                    line_start = text.rfind("\n", 0, insert_pos) + 1
                    return text[:line_start] + project_entry + text[line_start:]
            elif ch == '"':
                i += 1
                while i < len(block) and block[i] != '"':
                    if block[i] == "\\":
                        i += 1
                    i += 1
            i += 1

        return text  # Couldn't find instances closing

    # No instances block - insert one before the symbol's closing )
    # The closing ) of the symbol block is at position `end - 1`
    # We want to insert before the line containing that closing paren
    closing_paren_pos = end - 1
    line_start = text.rfind("\n", 0, closing_paren_pos) + 1

    instances_block = f"{i2}(instances\n{project_entry}{i2})\n"

    return text[:line_start] + instances_block + text[line_start:]


def _collect_existing_refs(
    all_file_symbols: dict[Path, list[dict]],
) -> dict[str, set[int]]:
    """Collect all existing reference numbers per prefix across all files.

    Only counts symbols that already have project instances (i.e. already
    annotated in the project).
    """
    existing: dict[str, set[int]] = {}
    for symbols in all_file_symbols.values():
        for sym in symbols:
            if sym["has_project_instance"] and sym["number"] is not None:
                existing.setdefault(sym["prefix"], set()).add(sym["number"])
    return existing


def run_repair_instances(
    schematic_path: Path,
    dry_run: bool = False,
    backup: bool = True,
    format: str = "text",
) -> int:
    """Run the repair-instances operation.

    Args:
        schematic_path: Path to root .kicad_sch file.
        dry_run: If True, show what would change without modifying files.
        backup: If True, create backups before modifying.
        format: Output format ("text" or "json").

    Returns:
        0 on success, 1 on error.
    """
    if not schematic_path.exists():
        print(f"Error: File not found: {schematic_path}", file=sys.stderr)
        return 1

    # Collect all schematic files in hierarchy order
    all_files = _collect_schematic_files(schematic_path)

    if not all_files:
        print("Error: No schematic files found", file=sys.stderr)
        return 1

    # Detect project info
    project_name, root_uuid, file_instance_paths = _detect_project_info(schematic_path, all_files)

    if not root_uuid:
        print("Error: Could not detect project UUID from root schematic", file=sys.stderr)
        return 1

    # Phase 1: Collect all symbols across hierarchy with instance info
    file_symbols: dict[Path, list[dict]] = {}
    file_texts: dict[Path, str] = {}

    for sch_file in all_files:
        try:
            text = sch_file.read_text(encoding="utf-8")
        except OSError as e:
            print(f"Error reading {sch_file}: {e}", file=sys.stderr)
            return 1
        file_texts[sch_file] = text
        file_symbols[sch_file] = _extract_symbols_with_instance_info(text, project_name)

    # Phase 2: Find symbols needing repair — either missing project
    # instances or carrying loose ``(project ...)`` blocks at symbol-child
    # indent (the malformed shape from issue #2624).  A symbol with a
    # well-formed instance AND a stray loose sibling still needs the
    # sibling cleaned up.
    missing: list[dict] = []  # [{file, sym}, ...]
    for sch_file in all_files:
        for sym in file_symbols[sch_file]:
            if not sym["has_project_instance"] or sym.get("has_loose_project_blocks"):
                missing.append({"file": sch_file, "sym": sym})

    if not missing:
        if format == "text":
            print("All symbols have project instances. No repairs needed.")
        elif format == "json":
            import json

            print(json.dumps({"repairs": [], "total": 0, "dry_run": dry_run}))
        return 0

    # Phase 3: Assign reference designators for unannotated symbols
    # Collect existing refs to avoid conflicts
    existing_refs = _collect_existing_refs(file_symbols)

    # Also collect refs from the missing symbols that are already annotated
    # (have a number but just lack the instances block)
    for entry in missing:
        sym = entry["sym"]
        if sym["number"] is not None:
            existing_refs.setdefault(sym["prefix"], set()).add(sym["number"])

    # Track counters for assigning new refs
    counters: dict[str, int] = {}

    def _next_available(prefix: str) -> int:
        n = counters.get(prefix, 1)
        reserved = existing_refs.get(prefix, set())
        while n in reserved:
            n += 1
        counters[prefix] = n + 1
        # Also reserve so subsequent calls don't reuse
        reserved.add(n)
        return n

    repairs: list[dict] = []
    for entry in missing:
        sym = entry["sym"]
        sch_file = entry["file"]

        # Determine repair_type.  Loose-project repair takes precedence:
        # the loose blocks already carry a valid annotated reference that
        # must be preserved (no rename), so we extract it directly rather
        # than assigning a new number.
        if sym.get("has_loose_project_blocks"):
            repair_type = "loose_project_blocks"
        elif sym.get("has_wrong_project"):
            repair_type = "wrong_project"
        else:
            repair_type = "missing_instances"

        if repair_type == "loose_project_blocks":
            # Prefer the loose block whose name matches the current project;
            # fall back to any loose block that carries a non-empty reference.
            preferred_ref = ""
            for proj in sym.get("loose_project_blocks") or []:
                if proj["name"] == project_name and proj["reference"]:
                    preferred_ref = proj["reference"]
                    break
            if not preferred_ref:
                for proj in sym.get("loose_project_blocks") or []:
                    if proj["reference"]:
                        preferred_ref = proj["reference"]
                        break
            new_ref = preferred_ref or sym["reference"]
            needs_rename = False
        elif sym["number"] is not None:
            # Already annotated, just needs instances block
            new_ref = sym["reference"]
            needs_rename = False
        else:
            # Unannotated (R?, C?, etc.) - assign next available.
            # Map bare symbol-name prefixes (e.g. #PWR_FLAG -> #FLG)
            # to proper KiCad annotation prefixes.
            prefix = _BARE_REF_PREFIX_MAP.get(sym["prefix"], sym["prefix"])
            new_number = _next_available(prefix)
            new_ref = _format_reference(prefix, new_number, sym["unit_suffix"])
            needs_rename = True

        instance_path = file_instance_paths.get(sch_file, "")

        repairs.append(
            {
                "file": sch_file,
                "uuid": sym["uuid"],
                "lib_id": sym["lib_id"],
                "old_ref": sym["reference"],
                "new_ref": new_ref,
                "instance_path": instance_path,
                "needs_rename": needs_rename,
                "repair_type": repair_type,
            }
        )

    # Phase 4: Output
    if format == "json":
        import json

        output = {
            "repairs": [
                {
                    "file": str(r["file"]),
                    "uuid": r["uuid"],
                    "lib_id": r["lib_id"],
                    "old_ref": r["old_ref"],
                    "new_ref": r["new_ref"],
                    "instance_path": r["instance_path"],
                    "repair_type": r["repair_type"],
                }
                for r in repairs
            ],
            "total": len(repairs),
            "dry_run": dry_run,
        }
        print(json.dumps(output, indent=2))
    else:
        n_wrong = sum(1 for r in repairs if r["repair_type"] == "wrong_project")
        n_loose = sum(1 for r in repairs if r["repair_type"] == "loose_project_blocks")
        n_missing = len(repairs) - n_wrong - n_loose
        parts = []
        if n_missing:
            parts.append(f"{n_missing} missing instances")
        if n_wrong:
            parts.append(f"{n_wrong} wrong project")
        if n_loose:
            parts.append(f"{n_loose} loose project blocks")
        print(f"Found {len(repairs)} symbol(s) needing repair ({', '.join(parts)}):")
        print()
        # Group by file for display
        by_file: dict[Path, list[dict]] = {}
        for r in repairs:
            by_file.setdefault(r["file"], []).append(r)

        for sch_file, file_repairs in by_file.items():
            print(f"  {sch_file.name}:")
            for r in file_repairs:
                if r["repair_type"] == "wrong_project":
                    tag = "[wrong project]"
                elif r["repair_type"] == "loose_project_blocks":
                    tag = "[loose project blocks]"
                else:
                    tag = "[add instances block]"
                if r["needs_rename"]:
                    print(f"    {r['old_ref']} -> {r['new_ref']}  ({r['lib_id']}) {tag}")
                else:
                    print(f"    {r['new_ref']}  ({r['lib_id']}) {tag}")
        print()

    if dry_run:
        if format == "text":
            print("Dry run: no changes made.")
        return 0

    # Phase 5: Apply changes
    modified_files: dict[Path, str] = {}

    for r in repairs:
        sch_file = r["file"]
        text = modified_files.get(sch_file, file_texts[sch_file])

        # If unannotated, rename the reference first
        if r["needs_rename"]:
            text = _apply_uuid_reference_rename(text, r["uuid"], r["new_ref"])

        # Add or replace the project instance block
        if r["instance_path"]:
            text = _insert_project_instance(
                text,
                r["uuid"],
                project_name,
                r["instance_path"],
                r["new_ref"],
                replace_wrong_project=r["repair_type"] == "wrong_project",
                repair_loose_blocks=r["repair_type"] == "loose_project_blocks",
            )

        modified_files[sch_file] = text

    # Phase 6: Write modified files
    if modified_files:
        for sch_file, new_text in modified_files.items():
            if backup:
                bak = create_backup(sch_file)
                if format == "text":
                    print(f"  Backup: {bak.name}")
            try:
                sch_file.write_text(new_text, encoding="utf-8")
            except OSError as e:
                print(f"Error writing {sch_file}: {e}", file=sys.stderr)
                return 1

    if format == "text":
        print(f"Repaired {len(repairs)} instance(s) across {len(modified_files)} file(s).")

    return 0


def main(argv: list[str] | None = None):
    """CLI entry point."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Repair missing project instances in KiCad schematics",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("schematic", type=Path, help="Path to .kicad_sch file")
    parser.add_argument(
        "--dry-run",
        "-n",
        action="store_true",
        help="Preview changes without modifying files",
    )
    parser.add_argument(
        "--backup",
        action="store_true",
        help="Create backup before modifying",
    )
    parser.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="Output format (default: text)",
    )

    args = parser.parse_args(argv)

    return run_repair_instances(
        schematic_path=args.schematic,
        dry_run=args.dry_run,
        backup=args.backup,
        format=args.format,
    )


if __name__ == "__main__":
    sys.exit(main())
