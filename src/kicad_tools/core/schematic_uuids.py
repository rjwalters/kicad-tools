"""Deterministic UUIDs for generated schematics (Issue #6076).

Why this exists
---------------
Every schematic element kicad-tools created -- placed symbols, their pins,
power symbols, wires, junctions, no-connects, labels, text notes and the
root sheet itself -- used to get a ``uuid.uuid4()``.  Two runs of the same
fleet generator therefore never wrote the same ``.kicad_sch``, so its
readiness hash could not be pinned and every regeneration diff was a wall of
UUID churn.  The board generators' own PCB-side ``generate_uuid()`` helpers
had the same problem.

This module replaces those random draws with ``uuid5`` values that are a
pure function of *what* is being identified, under one pinned namespace
(:data:`SCHEMATIC_UUID_NAMESPACE`, distinct from the PCB canonicalizer's in
:mod:`kicad_tools.core.canonical_uuids`).

Keys
----
* **Root sheet** -- :func:`root_sheet_uuid`: project name, title, page,
  parent sheet UUID (the sheet's "path").
* **Placed symbol** -- ``(sheet UUID, "symbol", reference, unit)``, minted
  when :meth:`Schematic.add_symbol` places it, so the UUID is final before
  any caller can read it (PCB footprint ``(path ...)`` values built from the
  in-memory object agree with the written file).
* **Power symbol** -- ``(sheet UUID, "power", reference)``, also at
  placement.
* **Pin** -- :func:`pin_uuid`: the owning symbol's UUID, the pin number and
  an ordinal among same-numbered pins.
* **Wires, junctions, no-connects, labels** -- their *content* (endpoints,
  position, text) under the sheet UUID, assigned when the schematic is
  written (see "Mechanism").  Content keys are more stable under edits than
  creation-order indices: adding a wire does not renumber every later one.
* **Text notes** -- ``(sheet UUID, "text", text, x, y)``.

Two items with the same key (two identical wires, two symbols both
annotated ``R?``) are told apart by an ordinal, and a :class:`UuidMinter`
never hands out a UUID it has already issued or been told to reserve, so
keys never collide.

The keep rule
-------------
UUIDs read from an existing file are **never rewritten** -- the principle
#6109 established for boards.  A schematic loaded, edited and saved keeps
every UUID it was loaded with; only elements added since get minted ones,
and the minter is seeded with every UUID in the loaded file so a new element
can never be given a UUID the file already uses (which a regenerated file's
own ``uuid5`` values otherwise could, since the keys are deterministic).

Mechanism
---------
Mint-time for anything whose UUID can escape the schematic object (the root
sheet, placed and power symbols), write-time for the rest.  Element
dataclasses (``Wire``, ``Label``, ...) still need a UUID the moment they are
constructed -- they can be built directly and appended to a schematic -- so
their default is a :class:`ProvisionalUuid`: a ``uuid4`` string *marked* as
not yet assigned.  :meth:`Schematic.to_sexp_node` replaces every provisional
UUID with its content-keyed ``uuid5`` before serializing and writes it back
onto the element, so the in-memory model and the file agree and a second
write is byte-identical.  An explicitly passed UUID, or one loaded from a
file, is a plain ``str`` and is left alone.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable
from pathlib import Path

__all__ = [
    "SCHEMATIC_UUID_NAMESPACE",
    "ProvisionalUuid",
    "UuidMinter",
    "UuidSequence",
    "is_provisional",
    "pin_uuid",
    "provisional_uuid",
    "root_sheet_uuid",
    "stable_uuid",
    "uuids_in_file",
    "uuids_in_text",
]

#: Pinned namespace for every UUID minted here.  Changing it renumbers every
#: generated schematic (and every generator-minted PCB item), so treat it as
#: a constant forever.  Deliberately not the board canonicalizer's namespace
#: (``kicad_tools.core.canonical_uuids._NAMESPACE``, Issue #6052).
SCHEMATIC_UUID_NAMESPACE = uuid.UUID("6f0a7e3c-6076-5d1b-8a2e-4c5d6e076076")

#: Separator between key parts -- a control character no reference, label
#: text or number formatting produces, so ``("a b", "c")`` and
#: ``("a", "b c")`` can never hash alike.
_SEP = "\x1f"

_UUID_RE = re.compile(r'\((?:uuid|tstamp)\s+"?([0-9A-Fa-f-]{36})"?\s*\)')


def _part(value: object) -> str:
    """Canonical text for one key part.

    Numbers are formatted to a fixed precision so ``100``, ``100.0`` and
    ``100.00000000001`` (float noise from a coordinate computation) key
    alike.  Everything else is ``str()``.
    """
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int | float):
        text = f"{float(value):.4f}"
        return "0.0000" if text == "-0.0000" else text
    return str(value)


def _key(parts: Iterable[object]) -> str:
    return _SEP.join(_part(p) for p in parts)


def stable_uuid(*parts: object) -> str:
    """``uuid5`` of ``parts`` under :data:`SCHEMATIC_UUID_NAMESPACE`.

    A pure function of its arguments -- no process state, no hash seed --
    so it returns the same value in every process.
    """
    return str(uuid.uuid5(SCHEMATIC_UUID_NAMESPACE, _key(parts)))


def root_sheet_uuid(
    project_name: str,
    title: str,
    page: str = "1",
    parent_uuid: str | None = None,
    sheet_file: str | None = None,
) -> str:
    """The UUID a schematic built without an explicit ``sheet_uuid`` gets.

    ``sheet_file`` (the sheet's file name / path within the hierarchy) is
    part of the key when given, so two sibling child sheets that share a
    title and page number still get distinct UUIDs.  Omitting it keeps the
    original ``(parent, project, title, page)`` key.
    """
    if sheet_file:
        return stable_uuid("sheet", parent_uuid or "", project_name, title, page, sheet_file)
    return stable_uuid("sheet", parent_uuid or "", project_name, title, page)


def pin_uuid(symbol_uuid: str, pin_number: str, ordinal: int = 0) -> str:
    """UUID of a placed symbol's ``(pin "N" (uuid ...))`` entry.

    ``ordinal`` separates pins that share a number (stacked pins).  Derived
    from the symbol's UUID, so a symbol whose UUID was loaded from a file
    gets the same pin UUIDs on every write.
    """
    return stable_uuid("pin", symbol_uuid, pin_number, ordinal)


class ProvisionalUuid(str):
    """A random UUID that has not been given its deterministic value yet.

    Element dataclasses default to one of these; :class:`Schematic` replaces
    it with a content-keyed ``uuid5`` when it writes the file.  It compares,
    hashes and formats exactly like the plain string it wraps.
    """

    __slots__ = ()


def provisional_uuid() -> ProvisionalUuid:
    """A fresh :class:`ProvisionalUuid` (``default_factory`` for element UUIDs)."""
    return ProvisionalUuid(uuid.uuid4())


def is_provisional(value: object) -> bool:
    """Whether ``value`` is a :class:`ProvisionalUuid` awaiting assignment."""
    return isinstance(value, ProvisionalUuid)


def uuids_in_text(text: str) -> set[str]:
    """Every ``(uuid ...)`` / legacy ``(tstamp ...)`` value in a KiCad file's text."""
    return {m.lower() for m in _UUID_RE.findall(text)}


def uuids_in_file(path: str | Path) -> set[str]:
    """:func:`uuids_in_text` of a file (empty set if it cannot be read)."""
    try:
        return uuids_in_text(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return set()


class UuidMinter:
    """Collision-free keyed ``uuid5`` minting.

    :meth:`mint` with a given key returns ``stable_uuid(*key, ordinal)``
    where ``ordinal`` counts earlier mints of the same key, skipping any
    value already issued or :meth:`reserve`-d.  The result depends only on
    the sequence of keys asked for and the reserved set, never on hash
    seeds or wall-clock state.
    """

    def __init__(self, reserved: Iterable[str] = ()) -> None:
        self._taken: set[str] = {u.lower() for u in reserved}
        self._ordinals: dict[str, int] = {}

    def reserve(self, values: Iterable[str]) -> None:
        """Never mint any of ``values`` (e.g. UUIDs loaded from a file)."""
        self._taken.update(str(v).lower() for v in values)

    def is_taken(self, value: str) -> bool:
        return value.lower() in self._taken

    def claim(self, *key: object) -> str:
        """``stable_uuid(*key)`` if unused, else the first unused ``stable_uuid(*key, n)``.

        Unlike :meth:`mint`, the common (no clash) case returns exactly
        ``stable_uuid(*key)``, so a site that used to call :func:`stable_uuid`
        directly keeps its values while gaining the reserve/issued check.
        The result is recorded, so claiming the same key again returns a
        different UUID.
        """
        candidate = stable_uuid(*key)
        n = 1
        while candidate.lower() in self._taken:
            candidate = stable_uuid(*key, f"#{n}")
            n += 1
        self._taken.add(candidate)
        return candidate

    def mint(self, *key: object) -> str:
        """The next unused deterministic UUID for ``key``."""
        k = _key(key)
        ordinal = self._ordinals.get(k, 0)
        while True:
            candidate = str(uuid.uuid5(SCHEMATIC_UUID_NAMESPACE, f"{k}{_SEP}#{ordinal}"))
            ordinal += 1
            if candidate not in self._taken:
                break
        self._ordinals[k] = ordinal
        self._taken.add(candidate)
        return candidate


class UuidSequence:
    """Deterministic drop-in for a generator's ``str(uuid.uuid4())``.

    The ``n``-th call returns ``uuid5`` of ``(scope, n)``, skipping reserved
    values.  Board generators that emit PCB text with one ``generate_uuid()``
    call per item use this so the same run emits the same file; call
    :meth:`reset` at the start of the function that builds the file so a
    second build in the same process starts the sequence over.  ``scope``
    keeps different boards' (and different files') sequences apart.
    """

    def __init__(self, scope: str, reserved: Iterable[str] = ()) -> None:
        self.scope = scope
        self._initial = {u.lower() for u in reserved}
        self._minter = UuidMinter(self._initial)

    def __call__(self) -> str:
        return self._minter.mint("seq", self.scope)

    def reserve(self, values: Iterable[str]) -> None:
        """Never return any of ``values`` (UUIDs already in the target file)."""
        self._minter.reserve(values)

    def reset(self) -> None:
        """Restart the sequence (keeping only the constructor's reserved set)."""
        self._minter = UuidMinter(self._initial)
