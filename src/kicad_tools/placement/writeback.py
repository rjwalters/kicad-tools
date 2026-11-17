"""Write footprint placements back to a ``.kicad_pcb`` through the PCB model.

Placement tools used to patch footprint ``(at ...)`` tokens with
line/regex text edits.  That only ever touched the footprint anchor, so
geometry KiCad stores in board coordinates -- footprint-embedded zones such
as an RF module's antenna keepout, and board-absolute pad angles -- stayed at
the old placement (Issue #6119).  On KiCad 8+ files, which put a
footprint's ``(at ...)`` before its ``(property "Reference" ...)``, the
line-based variant also overwrote the property and pad ``(at ...)`` nodes
instead of the footprint's.

:func:`write_footprint_placements` routes every move through
:meth:`PCB.update_footprint_position`, which keeps all of that consistent.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path


def write_footprint_placements(
    pcb_path: str | Path,
    output_path: str | Path,
    placements: Iterable[tuple[str, float, float, float | None]],
) -> int:
    """Move footprints and save the board to *output_path*.

    Args:
        pcb_path: Board to read.
        output_path: Where to write the result (may equal *pcb_path*).
        placements: ``(reference, x, y, rotation)`` tuples with **sheet-absolute**
            ``x``/``y`` in mm (the values that appear in the file's
            ``(at ...)``).  ``rotation`` is in degrees; ``None`` keeps the
            current rotation.

    Returns:
        The number of footprints found and moved.  Unknown references are
        skipped.
    """
    from kicad_tools.schema.pcb import PCB

    pcb = PCB.load(str(pcb_path))
    ox, oy = pcb.board_origin
    moved = 0
    for reference, x, y, rotation in placements:
        if pcb.update_footprint_position(
            reference,
            float(x) - ox,
            float(y) - oy,
            None if rotation is None else float(rotation),
        ):
            moved += 1
    pcb.save(str(output_path))
    return moved


_ZONE_POINT_RE = re.compile(
    r"\((xy|start|mid|end)(\s+)(-?[\d.]+(?:[eE][-+]?\d+)?)(\s+)(-?[\d.]+(?:[eE][-+]?\d+)?)"
)


def _balanced_end(text: str, start: int) -> int:
    """Index just past the S-expression list opening at ``text[start] == "("``.

    Quoted strings (with backslash escapes) are skipped so a ``(`` inside a
    property value does not unbalance the scan.  Returns ``len(text)`` for an
    unterminated list.
    """
    depth = 0
    i = start
    in_string = False
    while i < len(text):
        ch = text[i]
        if in_string:
            if ch == "\\":
                i += 1
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return len(text)


def _fmt_mm(value: float) -> str:
    text = f"{round(value, 6):.6f}".rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


def translate_embedded_zones_in_text(
    content: str, footprint_start: int, dx: float, dy: float
) -> str:
    """Translate the ``(zone ...)`` children of one footprint in raw file text.

    For the text-patching placement fixer, which moves a footprint by editing
    its ``(at ...)`` in place: KiCad stores footprint-embedded zones (antenna
    keepouts) in board coordinates, so they have to be shifted by the same
    ``(dx, dy)`` or they stay behind (Issue #6119).  ``footprint_start`` is the
    index of the footprint's opening ``(``.  Only a pure translation is
    supported here; rotations and flips go through the PCB model.
    """
    if dx == 0.0 and dy == 0.0:
        return content
    fp_end = _balanced_end(content, footprint_start)
    block = content[footprint_start:fp_end]

    pieces: list[str] = []
    cursor = 0
    for match in re.finditer(r"\(zone\b", block):
        if match.start() < cursor:
            continue  # nested inside a zone already handled (cannot happen)
        zone_end = _balanced_end(block, match.start())
        zone_text = block[match.start() : zone_end]

        def shift(m: re.Match[str]) -> str:
            x = float(m.group(3)) + dx
            y = float(m.group(5)) + dy
            return f"({m.group(1)}{m.group(2)}{_fmt_mm(x)}{m.group(4)}{_fmt_mm(y)}"

        pieces.append(block[cursor : match.start()])
        pieces.append(_ZONE_POINT_RE.sub(shift, zone_text))
        cursor = zone_end
    if not pieces:
        return content
    pieces.append(block[cursor:])
    return content[:footprint_start] + "".join(pieces) + content[fp_end:]
