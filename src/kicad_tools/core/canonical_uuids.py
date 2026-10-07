"""Make a board's item UUIDs a function of the board's content (Issue #6052).

Why this exists
---------------
KiCad's zone fill is a function of the board's content **and of the order
it loads the tracks in** (file order).  Measured on board 03 (KiCad 10.0.1):
two boards whose items are identical as a multiset, differing only in
track order, refill to different ``filled_polygon`` geometry (B.Cu GND
3154 vs 3151 vertices); sort the tracks the same way and the fills come
out byte-identical.  And:

* ``kicad-cli ... --save-board`` writes tracks and vias **sorted by UUID**
  (#4536), so every saved board's track order is its UUID order;
* every item that reaches KiCad *without* a ``(uuid ...)`` gets a fresh
  random one on load (#5591), and ``--save-board`` persists it.

``kct route`` fed KiCad a board whose UUIDs were not a function of its
copper: router-emitted segments/vias drew theirs from the process-global
``random`` stream (or ``uuid.uuid4``), whose state at serialization time
depends on how much other code consumed it first, and footprint pads,
``fp_line``/``fp_rect``/``fp_text``/``property`` nodes had no UUID at all.
After the first ``--save-board`` the track order was therefore random, and
every later refill (thermal remediation passes, each oracle-closer round)
loaded identical copper in a different order and emitted slightly
different pour polygons.  The pour fill that shipped varied run to run
with identical copper (Issue #6052).

:func:`canonicalize_board_uuids` fixes the *input* side once, before KiCad
first loads the routed board:

1. **Missing UUIDs** are filled in for every node type KiCad assigns a
   ``KIID`` to, and UUIDs KiCad *invented* on an earlier round trip (not
   in ``keep``, not version 5) are replaced.  Each new UUID is ``uuid5`` of
   the parent's UUID (or the board root), the node's tag and its ordinal
   among same-tag siblings -- a pure function of the file's structure.
2. **Router-minted copper** -- a top-level ``segment``/``via``/``arc``
   whose UUID is *not* in ``keep`` (the input board's UUIDs) -- is re-keyed
   to ``uuid5`` of its own content, like ``stitch_cmd._stitch_uuid``.
   Copper carried over from the input board keeps its authored UUID.
3. **Top-level copper is put in UUID order**, so the first KiCad load sees
   the same track order every later ``--save-board`` round trip will.

Identical copper then always reaches KiCad with identical UUIDs in an
identical order, so the fill it produces is reproducible.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Collection
from pathlib import Path

from kicad_tools.sexp import SExp

__all__ = [
    "UUID_BEARING_TAGS",
    "canonicalize_board_uuids",
    "canonicalize_pcb_file_uuids",
    "uuids_in_file",
]

#: Fixed namespace for every UUID minted here.  Changing it renumbers every
#: routed board, so treat it as a pinned constant.
_NAMESPACE = uuid.UUID("2b0d7f0e-6052-5a1c-9e4b-6b9a0c3f6052")

#: Node tags KiCad gives a ``KIID`` (and so invents a random one for when
#: absent).  ``property`` is only UUID-bearing inside a footprint -- a
#: board-level ``(property "k" "v")`` has no UUID -- see
#: :func:`_wants_uuid`.
UUID_BEARING_TAGS: frozenset[str] = frozenset(
    {
        "footprint",
        "pad",
        "property",
        "fp_text",
        "fp_text_box",
        "fp_line",
        "fp_rect",
        "fp_circle",
        "fp_arc",
        "fp_poly",
        "fp_curve",
        "gr_text",
        "gr_text_box",
        "gr_line",
        "gr_rect",
        "gr_circle",
        "gr_arc",
        "gr_poly",
        "gr_curve",
        "segment",
        "via",
        "arc",
        "zone",
        "dimension",
        "target",
    }
)

#: Top-level copper the router emits.  Only these are ever *re*-keyed.
_COPPER_TAGS: frozenset[str] = frozenset({"segment", "via", "arc"})

#: Containers whose children are not standalone board items (pad custom
#: primitives, polygon point lists): never give their children a UUID.
_OPAQUE_PARENTS: frozenset[str] = frozenset({"primitives", "pts", "polygon", "filled_polygon"})

_UUID_RE = re.compile(r'\(uuid\s+"?([0-9A-Fa-f-]{36})"?\s*\)')


def uuids_in_file(path: str | Path) -> set[str]:
    """Every ``(uuid ...)`` value in a board file (cheap regex scan)."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return set()
    return {m.lower() for m in _UUID_RE.findall(text)}


def _id_child(node: SExp) -> SExp | None:
    for child in node.children:
        if child.name in ("uuid", "tstamp"):
            return child
    return None


def _id_value(node: SExp) -> str | None:
    child = _id_child(node)
    if child is None:
        return None
    value = child.get_string(0)
    return value.lower() if value else None


def _wants_uuid(node: SExp, parent: SExp, root: SExp) -> bool:
    if node.name not in UUID_BEARING_TAGS:
        return False
    if parent.name in _OPAQUE_PARENTS:
        return False
    if node.name == "property":
        # Board-level ``(property "k" "v")`` carries no UUID in KiCad.
        return parent is not root and parent.name == "footprint"
    if node.name == "arc" and parent is not root:
        # ``(arc (start) (mid) (end))`` inside a polygon outline is geometry,
        # not a board item; only top-level / footprint-level arcs are items.
        return parent.name == "footprint"
    return True


def _set_uuid(node: SExp, value: str) -> None:
    child = _id_child(node)
    if child is not None and child.name == "uuid":
        child.set_value(0, value)
        return
    node.append(SExp.list("uuid", value))


def _mint(key: str, taken: set[str]) -> str:
    candidate = str(uuid.uuid5(_NAMESPACE, key))
    n = 0
    while candidate in taken:
        n += 1
        candidate = str(uuid.uuid5(_NAMESPACE, f"{key}#{n}"))
    taken.add(candidate)
    return candidate


def _content_key(node: SExp) -> str:
    """Whitespace-free text of ``node`` with its UUID child left out."""
    parts = [
        child.to_string(compact=True)
        for child in node.children
        if child.name not in ("uuid", "tstamp")
    ]
    return f"{node.name}:" + " ".join(parts)


def canonicalize_board_uuids(doc: SExp, *, keep: Collection[str] = ()) -> int:
    """Give ``doc``'s items content-derived UUIDs in place; return the edit count.

    Args:
        doc: A parsed ``kicad_pcb`` tree.
        keep: UUIDs to leave untouched -- pass the input board's UUIDs
            (:func:`uuids_in_file`) so authored/preserved items keep their
            identity and only router-minted or KiCad-invented UUIDs are
            re-keyed.  Missing UUIDs are filled in regardless.

    The result depends only on the file's content and structure, so two
    boards that differ only in random UUIDs come out identical.  Safe to run
    again after a KiCad round trip; a second run on its own output is a
    no-op.
    """
    keep_set = {k.lower() for k in keep}
    taken: set[str] = set()

    def collect(node: SExp) -> None:
        value = _id_value(node)
        if value:
            taken.add(value)
        for child in node.children:
            if child.name is not None:
                collect(child)

    collect(doc)
    edits = 0

    # (2) Re-key router-minted top-level copper from its content.  Done first
    # so a footprint child can never claim one of these values.  Twins with
    # identical content are interchangeable, so numbering them in file order
    # is still a function of the copper.
    seen: dict[str, int] = {}
    for child in doc.children:
        if child.name not in _COPPER_TAGS:
            continue
        current = _id_value(child)
        if current is not None and current in keep_set:
            continue
        key = _content_key(child)
        ordinal = seen.get(key, 0)
        seen[key] = ordinal + 1
        if current is not None:
            taken.discard(current)
        new = _mint(f"copper:{key}:{ordinal}", taken)
        if new != current:
            _set_uuid(child, new)
            edits += 1

    # (3) Put top-level copper in UUID order, in the slots it already
    # occupies.  ``--save-board`` writes tracks in UUID order and the fill
    # depends on load order, so this makes the *first* KiCad load see the
    # same order every later (saved) load will.
    slots = [i for i, child in enumerate(doc.children) if child.name in _COPPER_TAGS]
    ordered = sorted((doc.children[i] for i in slots), key=lambda n: _id_value(n) or "")
    for i, node in zip(slots, ordered, strict=True):
        if doc.children[i] is not node:
            doc.children[i] = node
            edits += 1

    # (1) Fill in every missing UUID -- and replace every one KiCad invented
    # -- from (parent UUID, tag, sibling ordinal).
    def fill(node: SExp, parent_key: str) -> None:
        nonlocal edits
        ordinals: dict[str, int] = {}
        for child in node.children:
            if child.name is None:
                continue
            ordinal = ordinals.get(child.name, 0)
            ordinals[child.name] = ordinal + 1
            child_key = parent_key
            if _wants_uuid(child, node, doc):
                value = _id_value(child)
                if value is None or _invented(child, node, value):
                    if value is not None:
                        taken.discard(value)
                    value = _mint(f"item:{parent_key}/{child.name}[{ordinal}]", taken)
                    _set_uuid(child, value)
                    edits += 1
                child_key = value
            fill(child, child_key)

    def _invented(child: SExp, parent: SExp, value: str) -> bool:
        """A UUID nobody authored: not from the input board and not minted here.

        KiCad gives a random (version 4) UUID to every item that reaches it
        without one -- including mandatory footprint fields it *adds* on
        load -- and ``--save-board`` persists it, so after a KiCad round trip
        those can no longer be told from missing ones by absence alone.
        Everything this module mints is version 5.  Zones are left alone:
        their UUID is KiCad's equal-priority tie-break (#5578) and the zone
        generator already derives it from content.  Top-level copper is
        handled by step (2).
        """
        if child.name == "zone" or value in keep_set:
            return False
        if parent is doc and child.name in _COPPER_TAGS:
            return False
        try:
            return uuid.UUID(value).version != 5
        except ValueError:
            return True

    fill(doc, "board")
    return edits


def canonicalize_pcb_file_uuids(path: str | Path, *, keep: Collection[str] = ()) -> int:
    """File wrapper for :func:`canonicalize_board_uuids`; rewrites only on change."""
    from kicad_tools.core.sexp_file import load_pcb, save_pcb

    doc = load_pcb(path)
    edits = canonicalize_board_uuids(doc, keep=keep)
    if edits:
        save_pcb(doc, path)
    return edits
