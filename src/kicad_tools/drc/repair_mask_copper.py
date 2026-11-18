"""Mask-to-copper repair: re-centre a track that enters its own pad off-centre.

``mask_to_copper`` (issue #8858, ``validate/mask_copper.py``) excuses a
same-net track that crosses into its own pad cleanly (``within_own_pad``,
info).  An entry that is shallow, or that arrives at a roundrect corner, keeps
copper under the mask beside the opening and stays an error.  This module
repairs that one class, nothing else (issue #9334, split from #9332/#9326):

* the conductor is a straight ``segment`` (not a via, arc or pour),
* it is on the same net as the single pad that owns the opening,
* exactly one of its ends is inside the pad and the other outside, and
* the repair moves the inside end to the pad centre, which makes the entry
  run straight at the pad.

Everything else (different net, via, pour, arc, pass-through, straddle, both
ends inside, a junction at the moved end, an exotic pad shape) is left
untouched and listed as unrepaired with a reason.  A move that would bring the
track closer than the clearance rule to other-net copper is declined.

The repairer cannot re-run the native mask assessment itself; ``fix-drc``
re-checks the saved board and reverts any move that did not clear its finding
or produced a new one (``cli/fix_drc_cmd.py``, ``_run_mask_pass``).

Usage:
    repairer = MaskCopperRepairer("board.kicad_pcb")
    result = repairer.repair(violations, max_displacement=0.5)
    repairer.save("board-fixed.kicad_pcb")
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

from shapely.affinity import rotate  # type: ignore[import-untyped]
from shapely.geometry import LineString, Point, box  # type: ignore[import-untyped]
from shapely.geometry.base import BaseGeometry  # type: ignore[import-untyped]

from ..sexp import SExp, parse_file
from .net_compat import resolve_net_atom
from .violation import DRCViolation, ViolationType

#: Message marker the mask rule writes for a same-net segment that enters its
#: own pad (``validate/mask_copper.py``); pass-through reads "same-net
#: pass-through" and is never repaired.
OWN_PAD_ENTRY_MARKER = "same-net own-pad entry"

_TOLERANCE_MM = 0.01
# The mask rule counts an authored end as inside the pad within its construction
# budget (~10 um); a rounded-corner polygon here can differ by a few um more.
_INSIDE_EPS_MM = 0.015


@dataclass
class MaskRepairAction:
    """One applied (or, in a dry run, predicted) segment-end move."""

    action: str  # always "recentre"
    uuid: str  # the segment's uuid
    pad_uuid: str
    pad_ref: str
    net_name: str
    layer: str
    old_x: float
    old_y: float
    new_x: float
    new_y: float
    displacement_mm: float
    detail: str = ""
    # Same-net segments whose end shared the moved point and moved with it:
    # (segment uuid, "start"|"end").
    joined: list[tuple[str, str]] = field(default_factory=list)

    def __str__(self) -> str:
        return (
            f"recentre: track [{self.net_name}] end ({self.old_x:.4f}, {self.old_y:.4f}) -> "
            f"({self.new_x:.4f}, {self.new_y:.4f}) on pad {self.pad_ref} - {self.detail}"
        )


@dataclass
class MaskUnrepaired:
    """A finding the repairer left alone, with the reason."""

    reason: str
    uuid: str = ""
    location: tuple[float, float] | None = None

    def to_dict(self) -> dict:
        data: dict = {"reason": self.reason, "uuid": self.uuid}
        if self.location is not None:
            data["x"], data["y"] = self.location
        return data


@dataclass
class MaskRepairResult:
    """Summary of a mask-to-copper repair run."""

    total_violations: int = 0
    repaired: int = 0
    actions: list[MaskRepairAction] = field(default_factory=list)
    unrepaired: list[MaskUnrepaired] = field(default_factory=list)

    def skip(self, reason: str, violation: DRCViolation, uuid: str = "") -> None:
        loc = violation.locations[0] if violation.locations else None
        self.unrepaired.append(
            MaskUnrepaired(
                reason=reason,
                uuid=uuid,
                location=(loc.x_mm, loc.y_mm) if loc is not None else None,
            )
        )

    def skipped(self, reason: str) -> int:
        return sum(1 for u in self.unrepaired if u.reason == reason)

    def summary(self) -> str:
        lines = [f"Mask-to-copper repair: {self.repaired}/{self.total_violations} findings fixed"]
        reasons: dict[str, int] = {}
        for item in self.unrepaired:
            reasons[item.reason] = reasons.get(item.reason, 0) + 1
        for reason, count in sorted(reasons.items()):
            lines.append(f"  Skipped ({reason}): {count}")
        return "\n".join(lines)


def _num(node: SExp | None) -> float:
    """First atom of ``node`` as a number (0.0 when absent)."""
    atom = node.get_first_atom() if node is not None else None
    return float(atom) if atom is not None else 0.0


def _bare(value: object) -> str:
    return str(value).strip().strip('"')


def mask_copper_violations(violations: list[DRCViolation]) -> list[DRCViolation]:
    """The error-severity ``mask_to_copper`` findings among ``violations``."""
    return [v for v in violations if v.type == ViolationType.MASK_TO_COPPER and v.is_error]


def _item_uuids(violation: DRCViolation) -> list[str]:
    """Opening uuids followed by the conductor uuid, as ``mask_to_copper`` emits."""
    raw = violation.item_uuids if any(violation.item_uuids) else violation.items
    return [_bare(u) for u in raw]


def finding_key(violation: DRCViolation) -> tuple[str, ...]:
    """Identity of a mask finding: its opening uuids plus the conductor uuid."""
    return tuple(_item_uuids(violation))


class MaskCopperRepairer:
    """Re-centres own-pad track entries that ``mask_to_copper`` reports."""

    def __init__(self, pcb_path: str | Path):
        self.path = Path(pcb_path)
        self.doc = parse_file(self.path)
        self.modified = False

        self.nets: dict[int, str] = {}
        self.net_names: dict[str, int] = {}
        for child in self.doc.children:
            if child.name != "net":
                continue
            atoms = child.get_atoms()
            if len(atoms) < 2:
                continue
            try:
                num = int(atoms[0])
            except (ValueError, TypeError):
                continue
            self.nets[num] = str(atoms[1])
            self.net_names[str(atoms[1])] = num

    # ------------------------------------------------------------------
    # Board model (absolute file coordinates throughout)
    # ------------------------------------------------------------------

    def _segment_nodes(self) -> dict[str, SExp]:
        out: dict[str, SExp] = {}
        for node in self.doc.find_all("segment"):
            uuid_node = node.find("uuid")
            atom = uuid_node.get_first_atom() if uuid_node else None
            if atom is not None:
                out[_bare(atom)] = node
        return out

    def _net_name(self, node: SExp) -> str:
        net_node = node.find("net")
        atom = net_node.get_first_atom() if net_node else None
        _, name = resolve_net_atom(None if atom is None else str(atom), self.nets, self.net_names)
        return name or ""

    @staticmethod
    def _xy(node: SExp | None) -> tuple[float, float] | None:
        if node is None:
            return None
        atoms = node.get_atoms()
        if len(atoms) < 2:
            return None
        return float(atoms[0]), float(atoms[1])

    def _pads(
        self,
    ) -> dict[str, tuple[BaseGeometry, tuple[float, float], list[str], str, str, str]]:
        """``uuid -> (polygon, centre, layers, net name, ref, shape)`` in file coords."""
        from kicad_tools.core.geometry import rotate_pad_offset
        from kicad_tools.schema.pcb import PCB

        pcb = PCB(self.doc)
        ox, oy = pcb._board_origin
        pads: dict[str, tuple[BaseGeometry, tuple[float, float], list[str], str, str, str]] = {}
        for fp in pcb.footprints:
            for pad in fp.pads:
                lx, ly = pad.position
                rx, ry = rotate_pad_offset(lx, ly, fp.rotation)
                cx = fp.position[0] + rx + ox
                cy = fp.position[1] + ry + oy
                poly = self._pad_polygon(pad, cx, cy)
                if poly is None:
                    # Keep the pad as an obstacle/owner marker with its box so a
                    # clearance check still sees it, but flag the shape.
                    w, h = pad.size
                    poly = box(cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)
                    shape = "unsupported"
                else:
                    shape = pad.shape
                if pad.uuid:
                    pads[_bare(pad.uuid)] = (
                        poly,
                        (cx, cy),
                        list(pad.layers),
                        pad.net_name or "",
                        f"{fp.reference}/{pad.number}",
                        shape,
                    )
        return pads

    @staticmethod
    def _pad_polygon(pad, cx: float, cy: float) -> BaseGeometry | None:
        w, h = pad.size
        if w <= 0 or h <= 0:
            return None
        shape = pad.shape
        if shape == "rect":
            poly: BaseGeometry = box(cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)
        elif shape == "roundrect":
            r = min(max(pad.roundrect_rratio, 0.0) * min(w, h), min(w, h) / 2)
            inner = box(cx - w / 2 + r, cy - h / 2 + r, cx + w / 2 - r, cy + h / 2 - r)
            poly = inner.buffer(r, quad_segs=16) if r > 0 else inner
        elif shape == "circle":
            poly = Point(cx, cy).buffer(w / 2, quad_segs=32)
        elif shape == "oval":
            if w >= h:
                half = (w - h) / 2
                poly = LineString([(cx - half, cy), (cx + half, cy)]).buffer(h / 2, quad_segs=16)
            else:
                half = (h - w) / 2
                poly = LineString([(cx, cy - half), (cx, cy + half)]).buffer(w / 2, quad_segs=16)
        else:
            return None
        # KiCad stores the pad angle in the absolute frame with y pointing down,
        # the same negated convention ``rotate_pad_offset`` applies to centres.
        if pad.rotation % 360:
            poly = rotate(poly, -pad.rotation, origin=(cx, cy))
        return poly

    def _other_net_obstacles(
        self, layer: str, net_name: str, skip_uuid: str, pads: dict
    ) -> list[BaseGeometry]:
        obstacles: list[BaseGeometry] = []
        for uuid, node in self._segment_nodes().items():
            if uuid == skip_uuid or self._net_name(node) == net_name:
                continue
            layer_node = node.find("layer")
            if layer_node is None or _bare(layer_node.get_first_atom()) != layer:
                continue
            start, end = self._xy(node.find("start")), self._xy(node.find("end"))
            width_node = node.find("width")
            if start is None or end is None or width_node is None:
                continue
            width = _num(width_node)
            obstacles.append(LineString([start, end]).buffer(width / 2, quad_segs=16))
        for via in self.doc.find_all("via"):
            if self._net_name(via) == net_name:
                continue
            at = self._xy(via.find("at"))
            size_node = via.find("size")
            if at is None or size_node is None:
                continue
            obstacles.append(Point(at).buffer(_num(size_node) / 2, quad_segs=16))
        for poly, _, layers, pad_net, _, _ in pads.values():
            if pad_net == net_name and net_name:
                continue
            if layer in layers or "*.Cu" in layers:
                obstacles.append(poly)
        return obstacles

    def _junction(
        self, layer: str, point: tuple[float, float], skip_uuid: str, net_name: str
    ) -> tuple[list[tuple[str, SExp, str]], bool]:
        """Segment ends sharing ``point``, and whether the point is unmovable.

        Same-net segment ends at the point move with it.  A via, or a segment
        of another net or a locked one, makes the point unmovable.
        """
        members: list[tuple[str, SExp, str]] = []
        for uuid, node in self._segment_nodes().items():
            if uuid == skip_uuid:
                continue
            layer_node = node.find("layer")
            if layer_node is None or _bare(layer_node.get_first_atom()) != layer:
                continue
            for tag in ("start", "end"):
                xy = self._xy(node.find(tag))
                if xy is None or math.dist(xy, point) >= _TOLERANCE_MM:
                    continue
                if self._net_name(node) != net_name or node.find("locked") is not None:
                    return members, True
                members.append((uuid, node, tag))
        for via in self.doc.find_all("via"):
            at = self._xy(via.find("at"))
            if at is not None and math.dist(at, point) < _TOLERANCE_MM:
                return members, True
        return members, False

    # ------------------------------------------------------------------
    # Repair
    # ------------------------------------------------------------------

    def repair(
        self,
        violations: list[DRCViolation],
        max_displacement: float = 0.5,
        dry_run: bool = False,
        clearance_mm: float | None = None,
    ) -> MaskRepairResult:
        """Repair the own-pad track-entry subset of ``violations``.

        Args:
            violations: Any violations; only error-severity ``mask_to_copper``
                findings are considered.
            max_displacement: Longest end move allowed, in mm.
            dry_run: Compute actions without touching the document.
            clearance_mm: Copper clearance a moved track must keep from other-net
                copper (``None``: only a track that gets *closer* than it already
                was to zero is declined, i.e. an overlap).
        """
        result = MaskRepairResult()
        findings = mask_copper_violations(violations)
        result.total_violations = len(findings)
        if not findings:
            return result

        segments = self._segment_nodes()
        pads = self._pads()
        moved: set[str] = set()

        for violation in findings:
            uuids = _item_uuids(violation)
            if len(uuids) < 2:
                result.skip("no_item_uuids", violation)
                continue
            conductor = uuids[-1]
            openings = uuids[:-1]
            if OWN_PAD_ENTRY_MARKER not in violation.message:
                result.skip("not_own_pad_entry", violation, conductor)
                continue
            seg = segments.get(conductor)
            if seg is None:
                result.skip("not_a_straight_segment", violation, conductor)
                continue
            if len(openings) != 1 or openings[0] not in pads:
                result.skip("opening_not_a_single_pad", violation, conductor)
                continue
            if conductor in moved:
                result.skip("segment_already_moved", violation, conductor)
                continue
            reason = self._repair_one(
                seg, conductor, openings[0], pads, result, max_displacement, dry_run, clearance_mm
            )
            if reason is not None:
                result.skip(reason, violation, conductor)
            else:
                moved.add(conductor)
        return result

    def _repair_one(
        self,
        seg: SExp,
        seg_uuid: str,
        pad_uuid: str,
        pads: dict,
        result: MaskRepairResult,
        max_displacement: float,
        dry_run: bool,
        clearance_mm: float | None,
    ) -> str | None:
        """Move one segment end; return ``None`` on success, else the skip reason."""
        poly, centre, pad_layers, pad_net, pad_ref, shape = pads[pad_uuid]
        if shape == "unsupported":
            return "unverified_pad_shape"

        net_name = self._net_name(seg)
        if not net_name or net_name != pad_net:
            return "not_same_net"
        if seg.find("locked") is not None or "locked" in [str(a) for a in seg.get_atoms()]:
            return "locked_segment"

        layer_node = seg.find("layer")
        layer = _bare(layer_node.get_first_atom()) if layer_node else ""
        if layer not in pad_layers and "*.Cu" not in pad_layers:
            return "pad_not_on_segment_layer"

        start_node, end_node = seg.find("start"), seg.find("end")
        start, end = self._xy(start_node), self._xy(end_node)
        width_node = seg.find("width")
        if start is None or end is None or width_node is None:
            return "malformed_segment"
        width = _num(width_node)

        grown = poly.buffer(_INSIDE_EPS_MM)
        start_in = grown.contains(Point(start))
        end_in = grown.contains(Point(end))
        if start_in == end_in:
            return "both_ends_inside_pad" if start_in else "no_end_inside_pad"
        inside, outside = (start, end) if start_in else (end, start)
        inside_node = start_node if start_in else end_node

        displacement = math.dist(inside, centre)
        if displacement < _TOLERANCE_MM / 2:
            return "end_already_centred"
        if displacement > max_displacement:
            return "exceeds_max_displacement"
        joined, blocked = self._junction(layer, inside, seg_uuid, net_name)
        if blocked:
            return "junction_at_moved_end"

        # Clearance gate: never bring the track closer than the rule to
        # other-net copper than it already was.
        required = clearance_mm if clearance_mm is not None else 0.0
        old_shape = LineString([outside, inside]).buffer(width / 2, quad_segs=16)
        new_shape = LineString([outside, centre]).buffer(width / 2, quad_segs=16)
        moves = [(old_shape, new_shape)]
        for _, jnode, jtag in joined:
            jw = _num(jnode.find("width")) or width
            jother = self._xy(jnode.find("end" if jtag == "start" else "start"))
            if jother is None:
                return "malformed_segment"
            moves.append(
                (
                    LineString([jother, inside]).buffer(jw / 2, quad_segs=16),
                    LineString([jother, centre]).buffer(jw / 2, quad_segs=16),
                )
            )
        for obstacle in self._other_net_obstacles(layer, net_name, seg_uuid, pads):
            for old_s, new_s in moves:
                new_d = new_s.distance(obstacle)
                if new_d < required - 1e-6 and new_d < old_s.distance(obstacle) - 1e-6:
                    return "would_violate_clearance"

        action = MaskRepairAction(
            action="recentre",
            uuid=seg_uuid,
            pad_uuid=pad_uuid,
            pad_ref=pad_ref,
            net_name=net_name,
            layer=layer,
            old_x=inside[0],
            old_y=inside[1],
            new_x=round(centre[0], 4),
            new_y=round(centre[1], 4),
            displacement_mm=displacement,
            detail=f"moved track end {displacement:.4f}mm to the pad centre",
            joined=[(u, t) for u, _, t in joined],
        )
        result.actions.append(action)
        if not dry_run and inside_node is not None:
            inside_node.set_value(0, action.new_x)
            inside_node.set_value(1, action.new_y)
            for _, jnode, jtag in joined:
                end_node = jnode.find(jtag)
                if end_node is not None:
                    end_node.set_value(0, action.new_x)
                    end_node.set_value(1, action.new_y)
            self.modified = True
        result.repaired += 1
        return None

    # ------------------------------------------------------------------
    # Undo / save
    # ------------------------------------------------------------------

    def undo_action(self, action: MaskRepairAction) -> bool:
        """Put the moved segment end (and any joined ends) back where they were.

        Returns ``False`` when a segment is gone or no end sits at the
        post-move position (callers fall back to a snapshot restore).
        """
        segments = self._segment_nodes()
        targets = [(action.uuid, None), *((u, t) for u, t in action.joined)]
        restore: list[SExp] = []
        for uuid, tag in targets:
            node = segments.get(_bare(uuid))
            if node is None:
                return False
            tags = (tag,) if tag else ("start", "end")
            for candidate in tags:
                end_node = node.find(candidate)
                xy = self._xy(end_node)
                if (
                    end_node is not None
                    and xy is not None
                    and math.dist(xy, (action.new_x, action.new_y)) < _TOLERANCE_MM
                ):
                    restore.append(end_node)
                    break
            else:
                return False
        for end_node in restore:
            end_node.set_value(0, round(action.old_x, 4))
            end_node.set_value(1, round(action.old_y, 4))
        self.modified = True
        return True

    def save(self, output_path: str | Path | None = None) -> None:
        from ..core.sexp_file import save_pcb

        save_pcb(self.doc, Path(output_path) if output_path else self.path)
