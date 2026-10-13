"""Geometric board diff: which copper, pads and outline changed (issue #6417).

``kct check --diff`` compares DRC *findings*; this module compares the board
items themselves.  Every drawable item becomes a :class:`DiffItem` carrying

* a **content key** -- its parsed, normalised geometry (net by *name*,
  coordinates quantised to 1 nm, endpoint order ignored), and
* an **identity key** -- the KiCad ``uuid`` for copper/graphics, the
  reference for footprints and ``<ref>/<pad>`` for pads.

How :func:`diff_items` pairs OLD and NEW items depends on the kind of
identity:

* **Reference-keyed kinds** (footprints and pads) pair **by identity first**.
  A reference is stable and meaningful, so content never overrides it: when
  R1 and R2 (same footprint, same value) trade places, both are ``changed``
  rather than paired with each other as ``unchanged``.  A part whose
  reference disappeared is ``removed``, and a new reference is ``added``.
  Only items with no reference at all fall back to the content passes.
* **Uuid-keyed kinds** (copper, zones and graphics) match in three passes:

  1. **Content pass.**  Items with equal ``(kind, content)`` pair one-to-one
     as multisets and are ``unchanged``, whatever their uuids.  Regenerated
     uuids, re-indentation and float reformatting therefore report nothing.
     Within a bucket of identical items, a candidate with the same uuid is
     preferred, so deleting one of two duplicate segments reports the uuid
     that actually disappeared.
  2. **Identity pass** on the leftovers.  Items with equal identity are
     ``changed``.  The pair also needs equal ``kind`` *and* ``net``: a seeded
     router numbers uuids positionally, so one extra segment early in a run
     shifts every later uuid, and without the guard a GND segment would pair
     with a VBUS one.
  3. **Remainder.**  OLD leftovers are ``removed``, NEW leftovers ``added``.

An item that both moved *and* got a new uuid reports as one removed plus one
added; there is deliberately no nearest-geometry heuristic.

The diff is a pure function of two item lists; :func:`extract_items` is the
only part that touches a :class:`~kicad_tools.schema.pcb.PCB`.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kicad_tools.schema.pcb import PCB

__all__ = [
    "KINDS",
    "STATUSES",
    "DiffEntry",
    "DiffItem",
    "DiffResult",
    "diff_items",
    "diff_pcbs",
    "extract_items",
    "to_json",
]

#: Every item kind, in report order.
KINDS: tuple[str, ...] = (
    "segment",
    "arc",
    "via",
    "footprint",
    "pad",
    "zone",
    "zone_fill",
    "graphic",
)

#: Every entry status, in report order.
STATUSES: tuple[str, ...] = ("removed", "added", "changed", "unchanged")

#: Kinds whose identity is a uuid, so the identity pass also requires the net
#: to match (seeded routes number uuids positionally).
_UUID_KINDS = frozenset({"segment", "arc", "via", "zone", "zone_fill", "graphic"})


def _q(value: float) -> float:
    """Quantise a millimetre value to 1 nm (``+ 0.0`` folds ``-0.0``)."""
    return round(float(value), 6) + 0.0


def _qp(point: Sequence[float]) -> tuple[float, float]:
    return (_q(point[0]), _q(point[1]))


def _qangle(deg: float) -> float:
    return _q(float(deg) % 360.0) % 360.0


@dataclass(frozen=True)
class DiffItem:
    """One board item, normalised for diffing.

    ``content`` must be hashable; ``geometry`` is a JSON-ready description
    (keys starting with ``_`` are kept for the SVG writer and stripped from
    JSON).  ``layers`` is the set of layers the item occupies, used by the
    ``--layer`` filter and the per-layer counts.
    """

    kind: str
    identity: str
    content: tuple[Any, ...]
    net: str = ""
    layers: tuple[str, ...] = ()
    geometry: dict[str, Any] = field(default_factory=dict, compare=False, hash=False)

    @property
    def layer(self) -> str:
        """Display form of :attr:`layers`: a via span as ``F.Cu-B.Cu``."""
        if self.kind == "via" and len(self.layers) > 1:
            return f"{self.layers[0]}-{self.layers[-1]}"
        return ",".join(self.layers)

    @property
    def key(self) -> str:
        """A stable, readable key: the identity, or a content digest."""
        if self.identity:
            return self.identity
        digest = hashlib.sha1(repr(self.content).encode()).hexdigest()[:12]
        return f"content:{digest}"


@dataclass(frozen=True)
class DiffEntry:
    """A matched (or unmatched) item: ``before`` is OLD, ``after`` is NEW."""

    status: str
    before: DiffItem | None
    after: DiffItem | None

    @property
    def item(self) -> DiffItem:
        """The NEW side when present, else the OLD side."""
        item = self.after if self.after is not None else self.before
        assert item is not None
        return item

    @property
    def kind(self) -> str:
        return self.item.kind

    @property
    def net(self) -> str:
        return self.item.net

    @property
    def key(self) -> str:
        return self.item.key

    @property
    def layers(self) -> tuple[str, ...]:
        """Union of both sides' layers, in first-seen order."""
        seen: dict[str, None] = {}
        for side in (self.before, self.after):
            if side is not None:
                for layer in side.layers:
                    seen.setdefault(layer, None)
        return tuple(seen)

    @property
    def layer(self) -> str:
        """Display layer: one side's, or ``OLD->NEW`` when the layer changed."""
        before = self.before.layer if self.before is not None else None
        after = self.after.layer if self.after is not None else None
        if before is None or after is None or before == after:
            return after if after is not None else (before or "")
        return f"{before}->{after}"


@dataclass
class DiffResult:
    """Every entry of a diff, unchanged ones included."""

    entries: list[DiffEntry] = field(default_factory=list)

    def filtered(
        self,
        nets: Iterable[str] | None = None,
        layers: Iterable[str] | None = None,
    ) -> DiffResult:
        """Entries whose net is in ``nets`` AND which touch one of ``layers``.

        Each filter is OR within itself and skipped when empty.  Filtering
        happens after matching, so it can never change how items pair.
        """
        net_set = set(nets or ())
        layer_set = set(layers or ())
        kept = [
            e
            for e in self.entries
            if (not net_set or e.net in net_set)
            and (not layer_set or any(layer in layer_set for layer in e.layers))
        ]
        return DiffResult(kept)

    def counts(self) -> dict[str, int]:
        out = dict.fromkeys(STATUSES, 0)
        for e in self.entries:
            out[e.status] += 1
        return out

    @property
    def has_changes(self) -> bool:
        return any(e.status != "unchanged" for e in self.entries)

    def changes(self) -> list[DiffEntry]:
        """The non-unchanged entries, in byte-stable report order."""
        rows = [e for e in self.entries if e.status != "unchanged"]
        return sorted(rows, key=_entry_sort_key)


def _entry_sort_key(e: DiffEntry) -> tuple[Any, ...]:
    return (
        STATUSES.index(e.status),
        e.kind,
        e.net,
        e.key,
        json.dumps(_public(e.before), sort_keys=True),
        json.dumps(_public(e.after), sort_keys=True),
    )


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


def _identity_key(item: DiffItem) -> tuple[str, str, str] | None:
    if not item.identity:
        return None
    net = item.net if item.kind in _UUID_KINDS else ""
    return (item.kind, item.identity, net)


def _ref_keyed(item: DiffItem) -> bool:
    """Whether ``item`` pairs by identity before content (footprints, pads)."""
    return item.kind not in _UUID_KINDS and bool(item.identity)


def diff_items(old: Sequence[DiffItem], new: Sequence[DiffItem]) -> DiffResult:
    """Match two item lists (see the module docstring for the passes)."""
    entries: list[DiffEntry] = []
    new_used = [False] * len(new)

    # Pass 0: reference-keyed kinds pair by identity only.
    new_by_ref: dict[tuple[str, str], list[int]] = defaultdict(list)
    for j, item in enumerate(new):
        if _ref_keyed(item):
            new_by_ref[(item.kind, item.identity)].append(j)
    old_rest: list[int] = []
    removed: list[int] = []
    for i, item in enumerate(old):
        if not _ref_keyed(item):
            old_rest.append(i)
            continue
        bucket = new_by_ref.get((item.kind, item.identity))
        if bucket:
            j = bucket.pop(0)
            new_used[j] = True
            status = "unchanged" if item.content == new[j].content else "changed"
            entries.append(DiffEntry(status, item, new[j]))
        else:
            removed.append(i)

    # Pass 1: content, as multisets; prefer a same-identity candidate.
    new_by_content: dict[tuple[Any, ...], list[int]] = defaultdict(list)
    for j, item in enumerate(new):
        if not new_used[j] and not _ref_keyed(item):
            new_by_content[(item.kind, item.content)].append(j)
    old_left: list[int] = []
    pending: list[int] = []
    for i in old_rest:
        item = old[i]
        bucket = new_by_content.get((item.kind, item.content))
        if not bucket:
            old_left.append(i)
            continue
        same = next((j for j in bucket if item.identity and new[j].identity == item.identity), None)
        if same is None:
            pending.append(i)
            continue
        bucket.remove(same)
        new_used[same] = True
        entries.append(DiffEntry("unchanged", item, new[same]))
    for i in pending:
        item = old[i]
        bucket = new_by_content.get((item.kind, item.content))
        if not bucket:
            old_left.append(i)
            continue
        j = bucket.pop(0)
        new_used[j] = True
        entries.append(DiffEntry("unchanged", item, new[j]))

    # Pass 2: identity on the leftovers (kind + net guarded for uuid kinds).
    new_by_identity: dict[tuple[str, str, str], list[int]] = defaultdict(list)
    for j, item in enumerate(new):
        if not new_used[j] and not _ref_keyed(item):
            ident = _identity_key(item)
            if ident is not None:
                new_by_identity[ident].append(j)
    for i in old_left:
        ident = _identity_key(old[i])
        bucket = new_by_identity.get(ident) if ident is not None else None
        if bucket:
            j = bucket.pop(0)
            new_used[j] = True
            entries.append(DiffEntry("changed", old[i], new[j]))
        else:
            removed.append(i)

    # Pass 3: remainder.
    entries.extend(DiffEntry("removed", old[i], None) for i in sorted(removed))
    entries.extend(DiffEntry("added", None, new[j]) for j, used in enumerate(new_used) if not used)
    return DiffResult(entries)


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------


def _canonical_ring(points: Sequence[Sequence[float]]) -> tuple[tuple[float, float], ...]:
    """Quantise a closed polygon into a start- and winding-independent form.

    The ring is rotated to start at its minimum vertex, in whichever winding
    direction gives the smaller tuple, so the same outline traced from a
    different vertex or in reverse compares equal.  KiCad fills a zone the
    same way regardless of winding, so direction carries no meaning here.
    """
    pts = [_qp(p) for p in points]
    if len(pts) > 1 and pts[0] == pts[-1]:
        pts = pts[:-1]
    if not pts:
        return ()

    def rotated(ring: list[tuple[float, float]]) -> tuple[tuple[float, float], ...]:
        start = min(range(len(ring)), key=lambda k: ring[k])
        return tuple(ring[start:] + ring[:start])

    return min(rotated(pts), rotated(pts[::-1]))


def _expand_layers(spec: Iterable[str], copper: Sequence[str]) -> tuple[str, ...]:
    """Expand KiCad layer wildcards (``*.Cu``, ``F&B.Cu``) to copper names."""
    out: dict[str, None] = {}
    for layer in spec:
        if layer == "*.Cu":
            for name in copper:
                out.setdefault(name, None)
        elif layer == "F&B.Cu":
            out.setdefault("F.Cu", None)
            out.setdefault("B.Cu", None)
        else:
            out.setdefault(layer, None)
    return tuple(out)


def _via_span(layers: Sequence[str], copper: Sequence[str]) -> tuple[str, ...]:
    """Copper layers a via passes through, from its two end layers."""
    ends = [layer for layer in _expand_layers(layers, copper) if layer in copper]
    if len(ends) < 2:
        return tuple(_expand_layers(layers, copper))
    idx = sorted(copper.index(layer) for layer in ends)
    return tuple(copper[idx[0] : idx[-1] + 1])


def _copper_order(pcb: PCB) -> list[str]:
    names = [layer.name for layer in sorted(pcb.copper_layers, key=lambda layer: layer.number)]
    if not names:
        return ["F.Cu", "B.Cu"]
    # KiCad 8+ numbers B.Cu 2 and inner layers 4, 6, ...; keep B.Cu last.
    if "B.Cu" in names:
        names = [n for n in names if n != "B.Cu"] + ["B.Cu"]
    if "F.Cu" in names:
        names = ["F.Cu"] + [n for n in names if n != "F.Cu"]
    return names


def extract_items(pcb: PCB) -> list[DiffItem]:
    """Every diffable item of ``pcb``, in sheet-absolute coordinates.

    ``PCB.load`` shifts copper and footprints to board-relative coordinates;
    the board origin is added back so moving the Edge.Cuts outline does not
    make every track look moved.
    """
    from kicad_tools.core.geometry import rotate_pad_offset

    ox, oy = pcb.board_origin
    copper = _copper_order(pcb)
    net_names = {num: net.name for num, net in pcb.nets.items()}

    def net_of(obj: Any) -> str:
        name = getattr(obj, "net_name", "") or ""
        if name:
            return str(name)
        return net_names.get(getattr(obj, "net_number", 0) or 0, "")

    def ab(point: Sequence[float]) -> tuple[float, float]:
        return _qp((point[0] + ox, point[1] + oy))

    items: list[DiffItem] = []

    for seg in pcb.segments:
        net = net_of(seg)
        ends = tuple(sorted((ab(seg.start), ab(seg.end))))
        width = _q(seg.width)
        items.append(
            DiffItem(
                kind="segment",
                identity=seg.uuid,
                content=(net, seg.layer, width, ends),
                net=net,
                layers=(seg.layer,),
                geometry={
                    "start": ab(seg.start),
                    "end": ab(seg.end),
                    "width": width,
                    "layer": seg.layer,
                },
            )
        )

    for arc in pcb.arcs:
        net = net_of(arc)
        ends = tuple(sorted((ab(arc.start), ab(arc.end))))
        width = _q(arc.width)
        items.append(
            DiffItem(
                kind="arc",
                identity=arc.uuid,
                content=(net, arc.layer, width, ab(arc.mid), ends),
                net=net,
                layers=(arc.layer,),
                geometry={
                    "start": ab(arc.start),
                    "mid": ab(arc.mid),
                    "end": ab(arc.end),
                    "width": width,
                    "layer": arc.layer,
                },
            )
        )

    for via in pcb.vias:
        net = net_of(via)
        pos = ab(via.position)
        size, drill = _q(via.size), _q(via.drill)
        raw_layers = tuple(sorted(via.layers))
        items.append(
            DiffItem(
                kind="via",
                identity=via.uuid,
                content=(net, pos, size, drill, raw_layers, via.via_type or ""),
                net=net,
                layers=_via_span(via.layers, copper),
                geometry={
                    "position": pos,
                    "size": size,
                    "drill": drill,
                    "layers": list(via.layers),
                    "via_type": via.via_type,
                },
            )
        )

    for fp in pcb.footprints:
        ref = fp.reference
        pos = ab(fp.position)
        rot = _qangle(fp.rotation)
        items.append(
            DiffItem(
                kind="footprint",
                identity=ref,
                content=(fp.name, fp.layer, pos, rot, fp.value),
                net="",
                layers=(fp.layer,),
                geometry={
                    "lib": fp.name,
                    "layer": fp.layer,
                    "position": pos,
                    "rotation": rot,
                    "value": fp.value,
                },
            )
        )
        for pad in fp.pads:
            dx, dy = rotate_pad_offset(pad.position[0], pad.position[1], fp.rotation)
            ppos = ab((fp.position[0] + dx, fp.position[1] + dy))
            psize = _qp(pad.size)
            prot = _qangle(pad.rotation)
            net = net_of(pad)
            pad_layers = tuple(sorted(pad.layers))
            cu = tuple(
                layer for layer in _expand_layers(pad.layers, copper) if layer.endswith(".Cu")
            )
            items.append(
                DiffItem(
                    kind="pad",
                    identity=f"{ref}/{pad.number}" if ref else "",
                    content=(net, ppos, psize, prot, pad.shape, pad_layers),
                    net=net,
                    layers=cu,
                    geometry={
                        "position": ppos,
                        "size": psize,
                        "rotation": prot,
                        "shape": pad.shape,
                        "layers": list(pad.layers),
                    },
                )
            )

    for zone in pcb.zones:
        net = net_of(zone)
        zone_layers = _expand_layers(zone.layers or ([zone.layer] if zone.layer else []), copper)
        ring = _canonical_ring([(x + ox, y + oy) for x, y in zone.polygon])
        keepout: tuple[Any, ...] = ()
        if zone.keepout is not None:
            k = zone.keepout
            keepout = (
                k.tracks_allowed,
                k.vias_allowed,
                k.pads_allowed,
                k.copperpour_allowed,
                k.footprints_allowed,
            )
        items.append(
            DiffItem(
                kind="zone",
                identity=zone.uuid,
                content=(net, tuple(sorted(zone_layers)), zone.name, zone.priority, ring, keepout),
                net=net,
                layers=zone_layers,
                geometry={
                    "layers": list(zone_layers),
                    "name": zone.name,
                    "priority": zone.priority,
                    "keepout": zone.keepout is not None,
                    "vertices": len(ring),
                    "_outline": [list(p) for p in ring],
                },
            )
        )
        if zone.filled_polygons:
            fills = []
            for idx, poly in enumerate(zone.filled_polygons):
                layer = zone.filled_polygon_layer(idx)
                fills.append((layer, _canonical_ring([(x + ox, y + oy) for x, y in poly])))
            fills.sort()
            fill_layers = tuple(dict.fromkeys(layer for layer, _ in fills))
            items.append(
                DiffItem(
                    kind="zone_fill",
                    identity=f"{zone.uuid}/fill" if zone.uuid else "",
                    content=tuple(fills),
                    net=net,
                    layers=fill_layers,
                    geometry={
                        "layers": list(fill_layers),
                        "polygons": len(fills),
                        "vertices": sum(len(ring) for _, ring in fills),
                        "_fills": [(layer, [list(p) for p in ring]) for layer, ring in fills],
                    },
                )
            )

    for gr in pcb.graphics:
        points = tuple(_qp(p) for p in gr.points)
        center = _qp(gr.center) if gr.center is not None else None
        mid = _qp(gr.mid) if gr.mid is not None else None
        start, end = _qp(gr.start), _qp(gr.end)
        width = _q(gr.stroke_width)
        items.append(
            DiffItem(
                kind="graphic",
                identity=gr.uuid,
                content=(gr.graphic_type, gr.layer, width, start, end, mid, center, points),
                net="",
                layers=(gr.layer,),
                geometry={
                    "type": gr.graphic_type,
                    "layer": gr.layer,
                    "width": width,
                    "start": start,
                    "end": end,
                    "mid": mid,
                    "center": center,
                    "points": [list(p) for p in points],
                },
            )
        )

    return items


def diff_pcbs(old: PCB, new: PCB) -> DiffResult:
    """Convenience: :func:`extract_items` on both boards, then :func:`diff_items`."""
    return diff_items(extract_items(old), extract_items(new))


# ---------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------


def _public(item: DiffItem | None) -> dict[str, Any] | None:
    if item is None:
        return None
    return {k: _jsonable(v) for k, v in item.geometry.items() if not k.startswith("_")}


def _jsonable(value: Any) -> Any:
    if isinstance(value, tuple):
        return [_jsonable(v) for v in value]
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    return value


def _bucket_counts(result: DiffResult, keys_of: Any) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for e in result.entries:
        for key in keys_of(e):
            out.setdefault(key, dict.fromkeys(STATUSES, 0))[e.status] += 1
    return out


def to_json(
    result: DiffResult,
    *,
    old: dict[str, Any] | None = None,
    new: dict[str, Any] | None = None,
    filters: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    """The documented JSON document (serialise with ``sort_keys=True``)."""
    items = [
        {
            "status": e.status,
            "kind": e.kind,
            "key": e.key,
            "net": e.net,
            "layer": e.layer,
            "before": _public(e.before),
            "after": _public(e.after),
        }
        for e in result.changes()
    ]
    return {
        "old": old or {},
        "new": new or {},
        "filters": filters or {"net": [], "layer": []},
        "summary": result.counts(),
        "by_kind": _bucket_counts(result, lambda e: (e.kind,)),
        "by_net": _bucket_counts(result, lambda e: (e.net,)),
        "by_layer": _bucket_counts(result, lambda e: e.layers),
        "items": items,
    }
