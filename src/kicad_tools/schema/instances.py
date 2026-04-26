"""Shared helpers for deriving project instance metadata.

The ``(instances ...)`` block inside a KiCad symbol S-expression ties a
placed symbol to a project and a hierarchy path.  These two functions --
:func:`find_project_name` and :func:`build_instance_path` -- are used by
every ``sch add-*`` command that places symbols.
"""

from __future__ import annotations

from pathlib import Path


def find_project_name(schematic_path: Path) -> str:
    """Derive the project name from the nearest ``.kicad_pro`` file.

    Walks up from *schematic_path* looking for a ``.kicad_pro`` file.
    Falls back to the schematic stem if none is found.
    """
    directory = schematic_path.resolve().parent
    for parent in [directory, *directory.parents]:
        pro_files = list(parent.glob("*.kicad_pro"))
        if pro_files:
            return pro_files[0].stem
    return schematic_path.stem


def build_instance_path(schematic_path: Path, sch_uuid: str) -> str:
    """Build the hierarchical instance path for a symbol.

    For a root schematic, returns ``/<sch_uuid>``.
    For a sub-sheet, walks up the hierarchy from the project root to build
    ``/<root_uuid>/<sheet_uuid>/...``.
    """
    from kicad_tools.schema.hierarchy import build_hierarchy

    resolved = schematic_path.resolve()
    directory = resolved.parent

    # Find the project root schematic (same stem as .kicad_pro, or look
    # for the .kicad_pro file and derive the root schematic from it).
    root_sch_path: Path | None = None
    for parent in [directory, *directory.parents]:
        pro_files = list(parent.glob("*.kicad_pro"))
        if pro_files:
            candidate = pro_files[0].with_suffix(".kicad_sch")
            if candidate.exists():
                root_sch_path = candidate
            break

    # If no .kicad_pro found, assume the schematic *is* the root
    if root_sch_path is None or root_sch_path.resolve() == resolved:
        return f"/{sch_uuid}"

    # Build hierarchy from root and find the node matching our schematic
    try:
        root_node = build_hierarchy(str(root_sch_path))
    except Exception:
        # If hierarchy building fails, fall back to simple root path
        return f"/{sch_uuid}"

    # Walk the hierarchy to find the node whose path matches our file
    for node in root_node.all_nodes():
        if Path(node.path).resolve() == resolved:
            # Build the UUID path from root to this node
            parts: list[str] = []
            current: object = node
            while current is not None:
                parts.append(current.uuid)  # type: ignore[union-attr]
                current = current.parent  # type: ignore[union-attr]
            parts.reverse()
            return "/" + "/".join(parts)

    # Fallback: treat as root
    return f"/{sch_uuid}"
