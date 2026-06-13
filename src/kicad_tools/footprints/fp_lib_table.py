"""Parse KiCad ``fp-lib-table`` files and resolve library nicknames.

KiCad maintains two ``fp-lib-table`` files:

* The **global** table (per KiCad install), typically at
  ``~/Library/Preferences/kicad/<ver>/fp-lib-table`` on macOS,
  ``~/.config/kicad/<ver>/fp-lib-table`` on Linux, etc.
* The **project** table, a sibling of the ``.kicad_pro`` file in the
  project root.

Both tables map library *nicknames* (e.g. ``MCU_ST_STM32F0``) to URIs that
contain ``${KIPRJMOD}`` (project root), ``${KICAD<N>_FOOTPRINT_DIR}``
(install footprint root), and other environment variables.

This module only handles the project table (and serves as a helper for
shipping `(lib_name, .pretty dir)` pairs for the project-aware
``suggest-footprint`` and ``preflight`` commands).  The existing
directory-scan over the global ``footprints/`` root is a reasonable proxy
for the global table because KiCad ships the global table pre-populated
with every shipped ``.pretty`` library.

Only ``type="KiCad"`` entries (raw ``.pretty`` directories on disk) are
honored.  Legacy- and Github-typed entries are intentionally skipped with a
warning -- KiCad 6+ deprecates Legacy, and Github type is rare and remote.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from kicad_tools.sexp import parse_string

# ${VAR} expansion -- matches both ${KIPRJMOD} and ${KICAD8_FOOTPRINT_DIR}
# style references in fp-lib-table URIs.
_VAR_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


@dataclass
class FpLibEntry:
    """One ``(lib ...)`` row from an ``fp-lib-table`` file.

    Attributes:
        name: The library nickname (e.g. ``"MyLib"``).
        type: The library type (``"KiCad"``, ``"Legacy"``, ``"Github"``, ...).
        uri: The raw URI string before environment-variable expansion.
        resolved_path: The absolute directory on disk after expansion, or
            ``None`` if expansion failed (unknown variable, type not
            ``"KiCad"``, or the directory does not exist).
    """

    name: str
    type: str
    uri: str
    resolved_path: Path | None


def find_project_fp_lib_table(start: Path) -> Path | None:
    """Locate the project ``fp-lib-table`` for a ``.kicad_sch`` or ``.kicad_pro``.

    Walks upward from *start* (or its parent if *start* is a file) until it
    finds a sibling ``fp-lib-table``.  Walking stops at the first directory
    that contains a ``.kicad_pro`` file (the project root) regardless of
    whether ``fp-lib-table`` is present -- ``${KIPRJMOD}`` always resolves
    against the project root, not against an arbitrary ancestor.

    Returns the path to the ``fp-lib-table`` file, or ``None`` if there is
    no project table (a perfectly valid configuration that means "use
    only the global table").
    """
    if start.is_file():
        current = start.parent
    else:
        current = start

    current = current.resolve()
    # Walk upward until filesystem root.
    while True:
        # If we hit a project root (.kicad_pro sibling), check for the
        # table there and stop.  KIPRJMOD == project root, never an ancestor.
        if any(current.glob("*.kicad_pro")):
            table = current / "fp-lib-table"
            return table if table.is_file() else None

        # No .kicad_pro yet -- still look for a sibling fp-lib-table
        # (some project layouts ship the table without a .kicad_pro nearby).
        table = current / "fp-lib-table"
        if table.is_file():
            return table

        parent = current.parent
        if parent == current:
            return None
        current = parent


def expand_kicad_vars(
    uri: str,
    kiprjmod: Path | None,
    env: Mapping[str, str] | None = None,
) -> Path | None:
    """Expand ``${VAR}`` references in a fp-lib-table URI to an absolute Path.

    Substitutes:

    * ``${KIPRJMOD}`` with *kiprjmod* (the project root).
    * Any other ``${VAR}`` with the corresponding entry in *env* (which
      defaults to ``os.environ``).

    Returns ``None`` if any referenced variable is missing or *kiprjmod*
    is required but not provided.  Returns a ``Path`` (not validated to
    exist on disk -- callers do that separately).

    File URIs (``file://``) are stripped of their scheme prefix.
    """
    if env is None:
        env = os.environ

    # Strip file:// scheme if present (rare but valid in KiCad URIs).
    if uri.startswith("file://"):
        uri = uri[len("file://") :]

    def _replace(match: re.Match) -> str:
        var = match.group(1)
        if var == "KIPRJMOD":
            if kiprjmod is None:
                # Signal failure by raising; the outer try/except converts
                # this into ``None``.
                raise KeyError(var)
            return str(kiprjmod)
        if var in env:
            return env[var]
        raise KeyError(var)

    try:
        expanded = _VAR_PATTERN.sub(_replace, uri)
    except KeyError:
        return None

    return Path(expanded)


def parse_fp_lib_table(path: Path) -> list[FpLibEntry]:
    """Parse an ``fp-lib-table`` file into a list of :class:`FpLibEntry`.

    The fp-lib-table grammar is a small s-expression::

        (fp_lib_table
            (version 7)
            (lib (name "X") (type "KiCad") (uri "${KIPRJMOD}/X.pretty")
                 (options "") (descr ""))
            ...)

    Only ``type="KiCad"`` entries get a non-``None`` ``resolved_path``;
    Legacy/Github/etc. are returned with ``resolved_path=None`` so callers
    can choose to log and skip them.  KIPRJMOD is set to *path*'s parent
    directory.
    """
    if not path.is_file():
        return []

    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []

    try:
        root = parse_string(text)
    except Exception:
        # Malformed file -- treat as empty rather than crashing the caller.
        return []

    kiprjmod = path.parent.resolve()
    entries: list[FpLibEntry] = []

    for lib in root.find_all("lib"):
        name = ""
        lib_type = ""
        uri = ""
        for field in lib.children:
            if field.name == "name":
                name = field.get_string(0) or ""
            elif field.name == "type":
                lib_type = field.get_string(0) or ""
            elif field.name == "uri":
                uri = field.get_string(0) or ""

        if not name or not uri:
            continue

        resolved: Path | None = None
        if lib_type == "KiCad":
            resolved = expand_kicad_vars(uri, kiprjmod)
            # We do NOT require the directory to exist here.  Callers
            # interested in "valid library nicknames" should check
            # ``resolved_path.is_dir()`` themselves; that lets us still
            # surface an entry whose directory has been deleted, which is
            # often the bug we want to diagnose.

        entries.append(FpLibEntry(name=name, type=lib_type, uri=uri, resolved_path=resolved))

    return entries
