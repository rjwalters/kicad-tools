"""Copper transplant between two revisions of the same placement (issue #8311).

:func:`transplant_nets` copies the tracks (segments, arcs) and vias of the
named nets from a SOURCE board onto a DESTINATION board: DST's copper on those
nets is removed and SRC's inserted.  Nets match by *name*; SRC's net numbers
are remapped to DST's.  Everything else in DST's S-expression tree is left
untouched.  The swap is refused when the pads of any named net sit at
different positions in the two boards (different placements).
"""

from __future__ import annotations

import copy
import math
from dataclasses import dataclass, field

from kicad_tools.schema.pcb import PCB

__all__ = ["TransplantError", "TransplantResult", "transplant_nets"]

_COPPER_TAGS = ("segment", "arc", "via")
#: Pad-position agreement tolerance in mm.
PAD_TOLERANCE_MM = 0.001


class TransplantError(Exception):
    """The transplant was refused; nothing was changed."""


@dataclass
class NetCounts:
    removed_tracks: int = 0
    removed_vias: int = 0
    added_tracks: int = 0
    added_vias: int = 0


@dataclass
class TransplantResult:
    """What a transplant did, per net name."""

    nets: dict[str, NetCounts] = field(default_factory=dict)

    @property
    def total(self) -> NetCounts:
        t = NetCounts()
        for c in self.nets.values():
            t.removed_tracks += c.removed_tracks
            t.removed_vias += c.removed_vias
            t.added_tracks += c.added_tracks
            t.added_vias += c.added_vias
        return t


def _net_by_name(pcb: PCB, name: str) -> int | None:
    for num, net in pcb.nets.items():
        if net.name == name and num != 0:
            return num
    return None


def _pad_positions(pcb: PCB, names: set[str]) -> dict[str, dict[str, tuple[float, float]]]:
    """``net name -> {"REF/PAD": (x, y)}`` for pads on the named nets."""
    out: dict[str, dict[str, tuple[float, float]]] = {n: {} for n in names}
    for fp in pcb.footprints:
        for pad in fp.pads:
            net = pad.net_name or (
                pcb.nets[pad.net_number].name if pad.net_number in pcb.nets else ""
            )
            if net not in names:
                continue
            pos = pcb.get_pad_position(fp.reference, pad.number)
            if pos is not None:
                out[net][f"{fp.reference}/{pad.number}"] = pos
    return out


def _check_pads(src: PCB, dst: PCB, names: set[str]) -> None:
    sp, dp = _pad_positions(src, names), _pad_positions(dst, names)
    bad: list[str] = []
    for net in sorted(names):
        for key in sorted(set(sp[net]) | set(dp[net])):
            a, b = sp[net].get(key), dp[net].get(key)
            if a is None or b is None:
                where = "SRC" if a is None else "DST"
                bad.append(f"{net}: {key} missing in {where}")
            elif math.hypot(a[0] - b[0], a[1] - b[1]) > PAD_TOLERANCE_MM:
                bad.append(f"{net}: {key} SRC ({a[0]:g}, {a[1]:g}) vs DST ({b[0]:g}, {b[1]:g})")
    if bad:
        raise TransplantError(
            "pad positions differ between SRC and DST (different placements); "
            "refusing:\n  " + "\n  ".join(bad)
        )


def _node_net(node, pcb: PCB) -> str:
    """Net name of a segment/arc/via S-expression node ('' when none)."""
    net = node.find("net")
    if net is None:
        return ""
    num = net.get_int(0)
    if num is None:  # KiCad 10 name-only form: (net "name")
        return net.get_string(0) or ""
    if num in pcb.nets:
        return pcb.nets[num].name
    return net.get_string(1) or ""


def _copper_nodes(pcb: PCB, names: set[str]) -> dict[str, list]:
    out: dict[str, list] = {n: [] for n in names}
    for child in pcb._sexp.children:
        if child.is_atom or child.name not in _COPPER_TAGS:
            continue
        name = _node_net(child, pcb)
        if name in names:
            out[name].append(child)
    return out


def transplant_nets(src: PCB, dst: PCB, net_names: list[str]) -> TransplantResult:
    """Replace ``dst``'s tracks/vias on ``net_names`` with ``src``'s.

    Mutates ``dst`` in memory (save it to persist).  Raises
    :class:`TransplantError`, before any mutation, when a net is missing from
    either board or when the pads of a named net moved.
    """
    names = set(net_names)
    if not names:
        raise TransplantError("no nets named")
    for label, pcb in (("SRC", src), ("DST", dst)):
        missing = sorted(n for n in names if _net_by_name(pcb, n) is None)
        if missing:
            raise TransplantError(f"net(s) not found in {label}: {', '.join(missing)}")
    _check_pads(src, dst, names)

    dst_numbers = {n: _net_by_name(dst, n) for n in names}
    result = TransplantResult()
    removed = _copper_nodes(dst, names)
    incoming = _copper_nodes(src, names)

    for name in sorted(names):
        c = result.nets.setdefault(name, NetCounts())
        for node in removed[name]:
            if node.name == "via":
                c.removed_vias += 1
            else:
                c.removed_tracks += 1

    for node in [n for nodes in removed.values() for n in nodes]:
        dst._sexp.remove(node)
    # New copper lands after DST's remaining copper (else at the end).
    insert_at = len(dst._sexp.children)
    for i, child in enumerate(dst._sexp.children):
        if not child.is_atom and child.name in _COPPER_TAGS:
            insert_at = i + 1

    for name in sorted(names):
        c = result.nets[name]
        for node in incoming[name]:
            new = copy.deepcopy(node)
            net = new.find("net")
            if net is not None and net.get_int(0) is not None:
                net.set_value(0, dst_numbers[name])
            dst._sexp.insert(insert_at, new)
            insert_at += 1
            if new.name == "via":
                c.added_vias += 1
            else:
                c.added_tracks += 1
    return result
