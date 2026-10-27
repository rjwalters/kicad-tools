"""Project library-table registration.

Creates or merges a KiCad ``sym-lib-table`` so a ``.kicad_sym`` library is
resolvable by a project. Written from kicad-tools' own table-merge logic;
no upstream code is reused.
"""

from __future__ import annotations

from pathlib import Path

from kicad_tools.sexp import SExp, parse_string

SYM_LIB_TABLE_VERSION = 7


def register_symbol_library(
    table_path: str | Path,
    nickname: str,
    uri: str,
    descr: str = "",
) -> bool:
    """Register a symbol library in a ``sym-lib-table``, creating it if absent.

    Idempotent on ``nickname``: an existing entry (and every other entry) is
    left untouched.

    Args:
        table_path: Path to the ``sym-lib-table`` file.
        nickname: Library nickname (the ``Nickname:Symbol`` prefix).
        uri: Library URI, e.g. ``${KIPRJMOD}/my_lib.kicad_sym``.
        descr: Optional description.

    Returns:
        True if an entry was added, False if the nickname was already present.

    Raises:
        ValueError: If the existing file cannot be parsed or is not a
            ``sym_lib_table``. The file is not modified.
    """
    path = Path(table_path)
    entry = SExp.list(
        "lib",
        SExp.list("name", SExp.quoted_atom(nickname)),
        SExp.list("type", SExp.quoted_atom("KiCad")),
        SExp.list("uri", SExp.quoted_atom(uri)),
        SExp.list("options", SExp.quoted_atom("")),
        SExp.list("descr", SExp.quoted_atom(descr)),
    )

    if not path.exists():
        table = SExp.list("sym_lib_table", SExp.list("version", SYM_LIB_TABLE_VERSION), entry)
        path.write_text(table.to_string() + "\n")
        return True

    try:
        table = parse_string(path.read_text())
    except Exception as exc:
        raise ValueError(f"Cannot parse sym-lib-table at {path}: {exc}") from exc
    if table.name != "sym_lib_table":
        raise ValueError(f"{path} is not a sym_lib_table")

    for lib_node in table.find_all("lib"):
        name_node = lib_node.get("name")
        if name_node and str(name_node.get_first_atom() or "") == nickname:
            return False
    table.append(entry)
    path.write_text(table.to_string() + "\n")
    return True
