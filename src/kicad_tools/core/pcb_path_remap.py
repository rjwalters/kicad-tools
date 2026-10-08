"""Re-point a board's footprint ``(path ...)`` links at a regenerated schematic.

Issue #6076 made generated schematics use deterministic UUIDs, so the first
regeneration of each board changes every symbol and sheet UUID once.  A
footprint's ``(path "/<sheet-uuid>/.../<symbol-uuid>")`` is how KiCad ties
it to its schematic symbol, so a board routed against the old schematic
would come out unlinked.  Re-routing to fix that is expensive and
non-deterministic; this module instead rewrites only the ``path`` atoms.

How a path is mapped
--------------------
KiCad's board-side path is the symbol's schematic instance path with the
root sheet UUID dropped, plus the symbol UUID: a root-sheet symbol's
footprint path is ``/<symbol-uuid>``, one on a sub-sheet is
``/<sheet-uuid>/<symbol-uuid>``.  (The root-inclusive spelling
``/<root>/<symbol>`` is accepted too and is mapped to the new
root-inclusive spelling.)  Both schematics are read, including sub-sheets,
and every old path is resolved to the ``(reference, unit)`` of the symbol
instance it names; the new path is the unique new-schematic instance with
that ``(reference, unit)``.  The footprint's own ``Reference`` must agree.

The rewrite refuses -- raising :class:`PathRemapError` and writing nothing --
if any footprint's path is absent from the old schematic, names a reference
the new schematic lacks or has twice, disagrees with the footprint's own
reference, or is shared with another footprint.  Footprints with no
``path`` at all are refused too unless ``allow_unlinked=True`` (they are
then left alone).  Only the quoted ``path`` values change: every other byte
of the board file is preserved.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from kicad_tools.sexp import SExp, parse_file, parse_string

__all__ = [
    "PathRemapError",
    "PathRemapResult",
    "remap_pcb_file_paths",
    "remap_pcb_paths",
    "schematic_instance_paths",
]


class PathRemapError(ValueError):
    """A footprint's path could not be mapped unambiguously; nothing was written."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__(
            "refusing to remap footprint paths:\n  " + "\n  ".join(problems)
            if problems
            else "refusing to remap footprint paths"
        )


@dataclass
class PathRemapResult:
    """What :func:`remap_pcb_paths` did."""

    text: str
    #: ``old path -> new path`` for every footprint path rewritten.
    mapping: dict[str, str] = field(default_factory=dict)
    #: References of footprints left alone because they had no ``path``.
    unlinked: list[str] = field(default_factory=list)

    @property
    def changed(self) -> int:
        return sum(1 for old, new in self.mapping.items() if old != new)


def _first_atom(node: SExp | None) -> str | None:
    if node is None:
        return None
    value = node.get_first_atom()
    return None if value is None else str(value)


def _symbol_reference(sym: SExp) -> str | None:
    for prop in sym.find_children("property"):
        if prop.get_string(0) == "Reference":
            return prop.get_string(1)
    return None


def _sheet_file(sheet: SExp) -> str | None:
    for prop in sheet.find_children("property"):
        if prop.get_string(0) in ("Sheetfile", "Sheet file"):
            return prop.get_string(1)
    return None


def schematic_instance_paths(sch_path: str | Path) -> tuple[str, dict[str, tuple[str, int]]]:
    """``(root UUID, {board path: (reference, unit)})`` for a schematic tree.

    Walks ``sch_path`` and every sub-sheet file it references (relative to
    its directory).  Each placed symbol contributes one entry per
    ``(instances (project (path ...)))`` record -- so a sheet instantiated
    twice yields both -- keyed by the board-side path (root UUID dropped) and
    by the root-inclusive spelling.  A key seen with two different
    ``(reference, unit)`` values maps to ``("", -1)``, i.e. ambiguous.
    """
    root_path = Path(sch_path)
    root = parse_file(root_path)
    root_uuid = (_first_atom(root.find_child("uuid")) or "").lower()
    out: dict[str, tuple[str, int]] = {}
    seen_files: set[Path] = set()

    def record(key: str, value: tuple[str, int]) -> None:
        prior = out.get(key)
        out[key] = value if prior is None or prior == value else ("", -1)

    def walk(path: Path, doc: SExp) -> None:
        resolved = path.resolve()
        if resolved in seen_files:
            return
        seen_files.add(resolved)
        for sym in doc.find_children("symbol"):
            if sym.find_child("lib_id") is None:
                continue
            sym_uuid = (_first_atom(sym.find_child("uuid")) or "").lower()
            if not sym_uuid:
                continue
            unit_node = sym.find_child("unit")
            default_unit = int(_first_atom(unit_node) or 1)
            default_ref = _symbol_reference(sym) or ""
            instances = sym.find_child("instances")
            records: list[tuple[str, str, int]] = []
            if instances is not None:
                for project in instances.find_children("project"):
                    for inst in project.find_children("path"):
                        inst_path = (inst.get_string(0) or "").lower()
                        ref = _first_atom(inst.find_child("reference")) or default_ref
                        unit = int(_first_atom(inst.find_child("unit")) or default_unit)
                        records.append((inst_path, ref, unit))
            if not records:
                records.append((f"/{root_uuid}", default_ref, default_unit))
            for inst_path, ref, unit in records:
                full = f"{inst_path.rstrip('/')}/{sym_uuid}"
                record(full, (ref, unit))
                prefix = f"/{root_uuid}"
                if root_uuid and (inst_path == prefix or inst_path.startswith(prefix + "/")):
                    record(full[len(prefix) :], (ref, unit))
        for sheet in doc.find_children("sheet"):
            name = _sheet_file(sheet)
            if not name:
                continue
            child = path.parent / name
            if child.is_file():
                walk(child, parse_file(child))

    walk(root_path, root)
    return root_uuid, out


_FOOTPRINT_PATH_RE = re.compile(r'(\(path\s+")([^"]*)(")')


def _footprint_reference(fp: SExp) -> str | None:
    for prop in fp.find_children("property"):
        if prop.get_string(0) == "Reference":
            return prop.get_string(1)
    for text in fp.find_children("fp_text"):
        if text.get_string(0) == "reference":
            return text.get_string(1)
    return None


def remap_pcb_paths(
    pcb_text: str,
    old_sch: str | Path,
    new_sch: str | Path,
    *,
    allow_unlinked: bool = False,
) -> PathRemapResult:
    """Rewrite ``pcb_text``'s footprint paths from ``old_sch``'s UUIDs to ``new_sch``'s.

    Returns the new text and the mapping; raises :class:`PathRemapError`
    (with every problem found) instead of producing a partial rewrite.
    """
    old_root, old_paths = schematic_instance_paths(old_sch)
    new_root, new_paths = schematic_instance_paths(new_sch)
    old_has_root = {k for k in old_paths if old_root and k.startswith(f"/{old_root}/")}

    # (reference, unit, root-inclusive?) -> new paths
    by_ref: dict[tuple[str, int, bool], set[str]] = {}
    for path, (ref, unit) in new_paths.items():
        inclusive = bool(new_root) and path.startswith(f"/{new_root}/")
        by_ref.setdefault((ref, unit, inclusive), set()).add(path)

    board = parse_string(pcb_text)
    problems: list[str] = []
    mapping: dict[str, str] = {}
    unlinked: list[str] = []
    path_owner: dict[str, str] = {}
    footprint_paths = 0

    for fp in board.find_children("footprint"):
        fp_ref = _footprint_reference(fp) or "?"
        path_node = fp.find_child("path")
        if path_node is None:
            if allow_unlinked:
                unlinked.append(fp_ref)
            else:
                problems.append(f"{fp_ref}: footprint has no (path ...) to remap")
            continue
        footprint_paths += 1
        old = (path_node.get_string(0) or "").lower()
        if old in path_owner:
            problems.append(f"{fp_ref}: path {old} is shared with {path_owner[old]}")
            continue
        path_owner[old] = fp_ref
        target = old_paths.get(old)
        if target is None:
            problems.append(f"{fp_ref}: path {old} matches no symbol in the old schematic")
            continue
        ref, unit = target
        if unit < 0:
            problems.append(f"{fp_ref}: path {old} is ambiguous in the old schematic")
            continue
        if ref != fp_ref:
            problems.append(f"{fp_ref}: path {old} belongs to {ref} in the old schematic")
            continue
        candidates = by_ref.get((ref, unit, old in old_has_root), set())
        if len(candidates) != 1:
            what = "no" if not candidates else f"{len(candidates)}"
            problems.append(
                f"{fp_ref}: {what} symbol instances of {ref} unit {unit} in new schematic"
            )
            continue
        new = next(iter(candidates))
        if new_paths[new][1] < 0:
            problems.append(f"{fp_ref}: new path {new} is ambiguous")
            continue
        mapping[old] = new

    if problems:
        raise PathRemapError(problems)

    # Textual rewrite of exactly the footprint path atoms, so nothing else
    # in the file changes.  Every ``(path "...")`` in a board is a footprint
    # link; refuse if the count disagrees with what the tree walk saw.
    matches = list(_FOOTPRINT_PATH_RE.finditer(pcb_text))
    if len(matches) != footprint_paths:
        raise PathRemapError(
            [
                f"found {len(matches)} (path ...) atoms but {footprint_paths} footprint "
                "paths; refusing to guess which to rewrite"
            ]
        )

    def substitute(m: re.Match[str]) -> str:
        value = m.group(2)
        new = mapping[value.lower()]
        return f"{m.group(1)}{new}{m.group(3)}"

    text = _FOOTPRINT_PATH_RE.sub(substitute, pcb_text)
    return PathRemapResult(text=text, mapping=mapping, unlinked=unlinked)


def remap_pcb_file_paths(
    pcb_path: str | Path,
    old_sch: str | Path,
    new_sch: str | Path,
    *,
    output: str | Path | None = None,
    allow_unlinked: bool = False,
) -> PathRemapResult:
    """File wrapper for :func:`remap_pcb_paths`; writes ``output`` (default: in place)."""
    src = Path(pcb_path)
    result = remap_pcb_paths(
        src.read_text(encoding="utf-8"), old_sch, new_sch, allow_unlinked=allow_unlinked
    )
    dest = Path(output) if output is not None else src
    if dest != src or result.changed:
        dest.write_text(result.text, encoding="utf-8")
    return result
