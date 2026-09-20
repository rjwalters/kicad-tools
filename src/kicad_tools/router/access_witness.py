"""Ordered commit journal -- what copper landed, when, and in what order.

Epic #5508 / Phase 1b (issue #5517), first half.  Phase 1a
(:mod:`kicad_tools.router.pad_access`) answers *"given the copper committed so
far, can this pad still be reached?"*.  To turn that read-only predicate into a
**witness** -- "commit #142, net ``COMP``, iteration 0, is what closed U3.1" --
something has to remember the order copper landed in.  That is this module.

Design
------

The journal is an append-only list of :class:`CommitRecord`, populated by a
single observer installed on :class:`~kicad_tools.router.grid.RoutingGrid`.
Every physical copper mutation on the routing grid flows through exactly three
grid methods -- :meth:`~kicad_tools.router.grid.RoutingGrid.mark_route`,
:meth:`~kicad_tools.router.grid.RoutingGrid.unmark_route` and
:meth:`~kicad_tools.router.grid.RoutingGrid.resync_route_occupancy` -- so one
observer on the grid sees them all, including the commit paths that bypass
:meth:`~kicad_tools.router.core.Autorouter._mark_route` entirely (the escape
pre-pass marks through ``EscapeRouter.apply_escape_routes``; the negotiated
rip-up paths unmark through ``NegotiatedRouter`` directly).

Two properties are load-bearing:

1. **Rip-ups are journaled too.**  A commit-only journal cannot reconstruct the
   copper present when a pad's access set closed: once any rip-up has happened,
   a replay that only knows about commits counts ripped copper as still
   present.  Each record therefore carries :attr:`CommitRecord.added` -- ``True``
   when copper appeared, ``False`` when it went away -- and the replay simply
   follows that flag.

2. **Records are geometry snapshots, not references.**  The post-route
   optimizer and DRC nudge mutate :class:`~kicad_tools.router.primitives.Route`
   objects *in place*, so holding a reference would silently rewrite history.
   Every record stores a :meth:`~kicad_tools.router.primitives.Route.copy_geometry`
   snapshot taken at record time, which also makes the journal serializable to
   the ``<stem>.access_witness.json`` sidecar without a second pass.

Cost
----

Recording is an append plus one shallow geometry copy per grid mutation --
``O(segments + vias)`` against ``mark_route``'s ``O(blocked cells)``.  There is
no access-set evaluation, no clearance arithmetic and no grid read inside the
observer, which is what lets journaling be **always on** with no router knob
(Epic #5508's scope guard).  :data:`MAX_JOURNAL_RECORDS` caps memory on
pathological boards; past the cap the journal stops recording and sets
:attr:`CommitJournal.truncated` so a consumer can say so rather than silently
replaying a prefix as if it were the whole run.

The *serialized* size needs its own care, because the sidecar is written on
every route with no flag to turn it off.  Two things keep it proportionate:
geometry is written as fixed-order arrays rather than per-field objects, and a
record whose geometry repeats an earlier record's (every rip repeats its
commit) stores a back-reference instead of a second copy.  Board 03's journal
is 261 records over ~42 000 segments: ~11 MB written naively, ~0.4 MB written
this way.

Phase 1b's second half adds ``replay(journal, router)`` here, walking these
records against :func:`~kicad_tools.router.pad_access.compute_access_set` to
name the commit that stranded each unrouted pad.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Literal

from .layers import Layer
from .primitives import Route, Segment, Via

__all__ = [
    "ACCESS_WITNESS_SIDECAR_SUFFIX",
    "JOURNAL_SCHEMA_VERSION",
    "MAX_JOURNAL_RECORDS",
    "CommitJournal",
    "CommitRecord",
    "PASS_ESCAPE",
    "PASS_FIXED",
    "PASS_GRACE",
    "PASS_INITIAL",
    "PASS_ITERATION",
    "PASS_POST",
    "PASS_RELIEF",
    "PASS_RESET",
    "PASS_ROUTING",
]

#: Sidecar basename suffix, stem-keyed off the routed PCB
#: (``simple_led_routed.access_witness.json`` next to
#: ``simple_led_routed.kicad_pcb``).  Mirrors the ``.routing_plan.json``
#: contract (#5519): a stem-keyed derived name can never collide with a
#: user-authored input file, because no CLI flag reads one.
ACCESS_WITNESS_SIDECAR_SUFFIX = ".access_witness.json"

#: Bumped when the serialized record shape changes incompatibly.
JOURNAL_SCHEMA_VERSION = 1

#: Soft cap on retained records.  A dense board's negotiated loop commits and
#: rips a few thousand routes; this is two orders of magnitude above that, so
#: it only ever fires on a pathological run -- where truncating beats an
#: unbounded list of geometry snapshots.
MAX_JOURNAL_RECORDS = 200_000

# Pass labels.  Free-form strings by contract (a caller may tag a bespoke
# rescue pass), but these are the ones the negotiated loop uses.
PASS_FIXED = "fixed"  #: Preserved / input-board copper marked before routing.
PASS_ROUTING = "routing"  #: A ``route_all_*`` entry point with no finer stage tagging.
PASS_ESCAPE = "escape"  #: Escape pre-phase stubs (the replay's baseline).
PASS_INITIAL = "initial"  #: Initial negotiated pass (iteration 0).
PASS_GRACE = "grace"  #: Budget-cliff grace pass (#3452), still iteration 0.
PASS_ITERATION = "iteration"  #: Rip-up / reroute iteration N.
PASS_RELIEF = "relief"  #: Relief-rescue probe / victim re-land.
PASS_RESET = "reset"  #: Monte Carlo / evolutionary trial reset discarded the grid.
PASS_POST = "post"  #: Post-route optimizer, nudge, clearance correction.

CommitKind = Literal["commit", "rip", "restore", "rollback"]

#: Grid events the observer distinguishes, mapped to ``(kind, added)``.
_EVENT_MAP: dict[str, tuple[CommitKind, bool]] = {
    "mark": ("commit", True),
    "unmark": ("rip", False),
    "resync_remove": ("rollback", False),
    "resync_add": ("rollback", True),
}


# Geometry is serialized as fixed-order arrays rather than per-field objects.
# A dense board journals tens of thousands of segments (board 03: ~42 000
# across 261 records), and a ``{"x1": ..., "y1": ...}`` object per segment
# costs ~136 bytes against ~34 for ``[x1, y1, x2, y2, w, layer, net]`` -- an
# 11 MB sidecar versus a 2 MB one for the same run.  Combined with the
# geometry de-duplication in :meth:`CommitJournal.to_dict`, board 03's sidecar
# lands at ~0.4 MB.

#: Coordinate rounding, in decimal places.  Matches the precision a
#: ``.kicad_pcb`` itself stores (6 dp = 1 nm), and is four orders of magnitude
#: finer than the finest routing grid, so it cannot move a replayed segment
#: into or out of a cell.  Without it, float noise like ``87.19999999999999``
#: costs ~18 characters per coordinate.
_COORD_DP = 6

#: ``Via`` flag bits packed into the trailing element of a via array.
_VIA_FLAG_IN_PAD = 1
_VIA_FLAG_IS_MICRO = 2


def _segment_to_list(seg: Segment, net_name: str) -> list[Any]:
    """``[x1, y1, x2, y2, width, layer, net]``, plus ``net_name`` if it differs.

    A segment's name is all but always the owning record's, so it is omitted
    and re-derived on decode; the optional trailing element keeps the round
    trip lossless for the rare route whose segments disagree.
    """
    encoded: list[Any] = [
        round(seg.x1, _COORD_DP),
        round(seg.y1, _COORD_DP),
        round(seg.x2, _COORD_DP),
        round(seg.y2, _COORD_DP),
        round(seg.width, _COORD_DP),
        seg.layer.value,
        seg.net,
    ]
    if seg.net_name != net_name:
        encoded.append(seg.net_name)
    return encoded


def _segment_from_list(payload: Sequence[Any], net_name: str) -> Segment:
    return Segment(
        x1=float(payload[0]),
        y1=float(payload[1]),
        x2=float(payload[2]),
        y2=float(payload[3]),
        width=float(payload[4]),
        layer=Layer(int(payload[5])),
        net=int(payload[6]),
        net_name=str(payload[7]) if len(payload) > 7 else net_name,
    )


def _via_to_list(via: Via, net_name: str) -> list[Any]:
    """``[x, y, drill, diameter, layer_from, layer_to, net, flags]`` (+ ``net_name``)."""
    flags = (_VIA_FLAG_IN_PAD if via.in_pad else 0) | (_VIA_FLAG_IS_MICRO if via.is_micro else 0)
    encoded: list[Any] = [
        round(via.x, _COORD_DP),
        round(via.y, _COORD_DP),
        round(via.drill, _COORD_DP),
        round(via.diameter, _COORD_DP),
        via.layers[0].value,
        via.layers[1].value,
        via.net,
        flags,
    ]
    if via.net_name != net_name:
        encoded.append(via.net_name)
    return encoded


def _via_from_list(payload: Sequence[Any], net_name: str) -> Via:
    flags = int(payload[7])
    return Via(
        x=float(payload[0]),
        y=float(payload[1]),
        drill=float(payload[2]),
        diameter=float(payload[3]),
        layers=(Layer(int(payload[4])), Layer(int(payload[5]))),
        net=int(payload[6]),
        net_name=str(payload[8]) if len(payload) > 8 else net_name,
        in_pad=bool(flags & _VIA_FLAG_IN_PAD),
        is_micro=bool(flags & _VIA_FLAG_IS_MICRO),
    )


@dataclass(frozen=True)
class CommitRecord:
    """One physical copper mutation on the routing grid, in commit order.

    ``kind`` is descriptive; ``added`` is the one field a replay must obey:

    ``commit``
        New search-derived copper landed (the common case).
    ``restore``
        A route object that was committed and later ripped came back -- the
        negotiated rip-up paths re-mark the *same* :class:`Route` instance when
        a reroute attempt fails and the victim has to be put back.
    ``rip``
        Copper was removed (rip-up, victim eviction, corridor clearing).
    ``rollback``
        A wholesale occupancy resync: best-iteration rollback, the post-route
        optimizer swapping new geometry in for old, or a connectivity-invariant
        revert.  Emitted as a ``added=False`` record for the stale geometry and
        an ``added=True`` record for the current geometry.
    """

    index: int
    """Position in the journal, 0-based.  Also the replay's step number."""

    kind: CommitKind
    added: bool
    """True when this record ADDED copper, False when it removed copper."""

    pass_name: str
    """Which routing stage was running -- one of the ``PASS_*`` constants."""

    iteration: int
    """Negotiated-loop iteration.  0 for escape / initial / grace."""

    net: int
    net_name: str
    route_id: int
    """``id()`` of the live Route object, so re-marks can be tied to their rip."""

    is_escape: bool
    segments: tuple[Segment, ...]
    vias: tuple[Via, ...]

    @property
    def route(self) -> Route:
        """Materialise this record's geometry snapshot as a :class:`Route`."""
        return Route(
            net=self.net,
            net_name=self.net_name,
            segments=list(self.segments),
            vias=list(self.vias),
            is_escape=self.is_escape,
        )

    @property
    def stage(self) -> str:
        """``"pass[iteration]"`` label, e.g. ``"iteration[3]"``."""
        return f"{self.pass_name}[{self.iteration}]"

    def geometry_key(self) -> tuple[Any, ...]:
        """Hashable identity of this record's geometry, for de-duplication.

        A rip re-states the geometry of the commit it undoes, and a restore
        re-states it a third time, so most of a journal's records repeat
        geometry verbatim -- 186 of board 03's 261 records.  The serializer
        writes each distinct shape once and refers back to it by index.

        ``net_name`` is part of the key because segments and vias take their
        name from the owning record on decode; two records may only share a
        geometry entry when that name agrees.
        """
        return (
            self.net_name,
            tuple(tuple(_segment_to_list(seg, self.net_name)) for seg in self.segments),
            tuple(tuple(_via_to_list(via, self.net_name)) for via in self.vias),
        )

    def to_dict(self, geom_ref: int | None = None) -> dict[str, Any]:
        """JSON-serializable form (the sidecar's record shape).

        Args:
            geom_ref: When given, the index of an earlier record holding the
                identical geometry.  This record then carries ``geom_ref``
                instead of its own ``segments`` / ``vias`` arrays.
        """
        payload: dict[str, Any] = {
            "index": self.index,
            "kind": self.kind,
            "added": self.added,
            "pass": self.pass_name,
            "iteration": self.iteration,
            "net": self.net,
            "net_name": self.net_name,
            "route_id": self.route_id,
            "is_escape": self.is_escape,
        }
        if geom_ref is not None:
            payload["geom_ref"] = geom_ref
        else:
            payload["segments"] = [_segment_to_list(seg, self.net_name) for seg in self.segments]
            payload["vias"] = [_via_to_list(via, self.net_name) for via in self.vias]
        return payload

    @classmethod
    def from_dict(
        cls,
        payload: dict[str, Any],
        geometry: tuple[tuple[Segment, ...], tuple[Via, ...]] | None = None,
    ) -> CommitRecord:
        """Inverse of :meth:`to_dict`.

        Args:
            payload: One serialized record.
            geometry: Geometry resolved from ``payload["geom_ref"]`` by
                :meth:`CommitJournal.from_dict`.  Required when the record
                carries a ``geom_ref`` -- a reference cannot be resolved from
                the record alone.
        """
        net_name = str(payload.get("net_name", ""))
        if "geom_ref" in payload:
            if geometry is None:
                raise ValueError(
                    f"record {payload.get('index')} refers to geometry "
                    f"{payload['geom_ref']}, which was not supplied"
                )
            segments, vias = geometry
        else:
            segments = tuple(_segment_from_list(s, net_name) for s in payload.get("segments", ()))
            vias = tuple(_via_from_list(v, net_name) for v in payload.get("vias", ()))
        return cls(
            index=int(payload["index"]),
            kind=payload["kind"],
            added=bool(payload["added"]),
            pass_name=str(payload["pass"]),
            iteration=int(payload["iteration"]),
            net=int(payload["net"]),
            net_name=net_name,
            route_id=int(payload.get("route_id", 0)),
            is_escape=bool(payload.get("is_escape", False)),
            segments=segments,
            vias=vias,
        )


@dataclass
class CommitJournal:
    """Append-only, ordered record of every copper mutation in one run.

    Install with :meth:`attach` on the routing grid; the router sets the
    ``(pass, iteration)`` context around each stage with :meth:`context`.
    Nothing here reads the grid or evaluates geometry, so attaching a journal
    cannot change what the router does -- it is a pure observer.
    """

    records: list[CommitRecord] = field(default_factory=list)
    truncated: bool = False
    """True once :data:`MAX_JOURNAL_RECORDS` was hit and records were dropped."""

    pass_name: str = PASS_FIXED
    iteration: int = 0

    _ripped: set[int] = field(default_factory=set, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # -- context ---------------------------------------------------------

    def set_context(self, pass_name: str, iteration: int = 0) -> None:
        """Set the ``(pass, iteration)`` tag applied to subsequent records."""
        self.pass_name = pass_name
        self.iteration = int(iteration)

    @contextmanager
    def context(self, pass_name: str, iteration: int = 0) -> Iterator[CommitJournal]:
        """Scoped :meth:`set_context`, restoring the previous tag on exit."""
        previous = (self.pass_name, self.iteration)
        self.set_context(pass_name, iteration)
        try:
            yield self
        finally:
            self.pass_name, self.iteration = previous

    # -- recording -------------------------------------------------------

    def observe(self, event: str, route: Route) -> None:
        """Grid observer entry point.  ``event`` is a key of :data:`_EVENT_MAP`.

        Called from inside the grid's own lock, so append order is commit order
        even under region-parallel routing (where several worker threads mark
        routes concurrently).
        """
        mapped = _EVENT_MAP.get(event)
        if mapped is None:
            return
        kind, added = mapped
        route_id = id(route)
        with self._lock:
            if len(self.records) >= MAX_JOURNAL_RECORDS:
                self.truncated = True
                return
            if kind == "commit" and route_id in self._ripped:
                kind = "restore"
                self._ripped.discard(route_id)
            elif not added:
                self._ripped.add(route_id)
            self.records.append(
                CommitRecord(
                    index=len(self.records),
                    kind=kind,
                    added=added,
                    pass_name=self.pass_name,
                    iteration=self.iteration,
                    net=route.net,
                    net_name=route.net_name,
                    route_id=route_id,
                    is_escape=bool(route.is_escape),
                    segments=tuple(_copy_segments(route.segments)),
                    vias=tuple(_copy_vias(route.vias)),
                )
            )

    def attach(self, grid: Any) -> None:
        """Install this journal as ``grid.commit_observer``."""
        grid.commit_observer = self.observe

    @staticmethod
    def detach(grid: Any) -> None:
        """Remove any observer from ``grid`` (used by tests and teardown)."""
        grid.commit_observer = None

    # -- queries ---------------------------------------------------------

    def __len__(self) -> int:
        return len(self.records)

    def __iter__(self) -> Iterator[CommitRecord]:
        return iter(self.records)

    def __getitem__(self, index: int) -> CommitRecord:
        return self.records[index]

    def commits(self) -> list[CommitRecord]:
        """Records that ADDED copper, in order (``commit`` + ``restore``)."""
        return [r for r in self.records if r.added]

    def rips(self) -> list[CommitRecord]:
        """Records that REMOVED copper, in order."""
        return [r for r in self.records if not r.added]

    def net_commit_order(self) -> list[str]:
        """Net names in first-commit order, de-duplicated.

        The AC-facing view: on the Phase 1a fixture this reads
        ``["<escape>", "COMP", "ISENSE_A+"]``-shaped, i.e. escape stubs first,
        then the nets in the order the negotiated loop landed them.
        """
        seen: set[str] = set()
        order: list[str] = []
        for record in self.records:
            if not record.added:
                continue
            name = record.net_name or f"net{record.net}"
            if name not in seen:
                seen.add(name)
                order.append(name)
        return order

    def records_for_net(self, net: int) -> list[CommitRecord]:
        """Every record touching ``net``, in order."""
        return [r for r in self.records if r.net == net]

    def summary_line(self) -> str:
        """One-line human summary for CLI output."""
        commits = len(self.commits())
        rips = len(self.records) - commits
        passes = len({(r.pass_name, r.iteration) for r in self.records})
        suffix = " (truncated)" if self.truncated else ""
        return (
            f"Commit journal: {len(self.records)} record(s) "
            f"-- {commits} commit(s), {rips} rip(s), across {passes} stage(s){suffix}"
        )

    # -- serialization ---------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable form written to the sidecar.

        Repeated geometry is written once: a record whose segments and vias
        are identical to an earlier record's carries ``geom_ref: <index>``
        instead.  Most of a journal *is* repeated geometry -- a rip re-states
        what its commit stated, and a restore states it again -- so this is
        the difference between a 2 MB and a 0.4 MB sidecar on board 03.
        """
        first_seen: dict[tuple[Any, ...], int] = {}
        records: list[dict[str, Any]] = []
        for record in self.records:
            key = record.geometry_key()
            ref = first_seen.get(key)
            if ref is None:
                first_seen[key] = record.index
            records.append(record.to_dict(geom_ref=ref))
        return {
            "schema_version": JOURNAL_SCHEMA_VERSION,
            "truncated": self.truncated,
            "record_count": len(self.records),
            "records": records,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> CommitJournal:
        """Inverse of :meth:`to_dict`.

        Unknown / future schema versions raise :class:`ValueError` rather than
        silently replaying records whose shape has changed.
        """
        version = int(payload.get("schema_version", 0))
        if version != JOURNAL_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported commit-journal schema_version {version} "
                f"(expected {JOURNAL_SCHEMA_VERSION})"
            )
        journal = cls()
        journal.truncated = bool(payload.get("truncated", False))
        # ``geom_ref`` always points BACKWARDS (to the first record that used
        # the shape), so a single forward pass resolves every reference.
        by_index: dict[int, tuple[tuple[Segment, ...], tuple[Via, ...]]] = {}
        records: list[CommitRecord] = []
        for entry in payload.get("records", ()):
            ref = entry.get("geom_ref")
            geometry = None
            if ref is not None:
                geometry = by_index.get(int(ref))
                if geometry is None:
                    raise ValueError(
                        f"record {entry.get('index')} refers to geometry {ref}, "
                        "which is missing or does not precede it"
                    )
            record = CommitRecord.from_dict(entry, geometry=geometry)
            by_index[record.index] = (record.segments, record.vias)
            records.append(record)
        journal.records = records
        return journal


def _copy_segments(segments: Sequence[Segment]) -> list[Segment]:
    return [
        Segment(
            x1=s.x1,
            y1=s.y1,
            x2=s.x2,
            y2=s.y2,
            width=s.width,
            layer=s.layer,
            net=s.net,
            net_name=s.net_name,
        )
        for s in segments
    ]


def _copy_vias(vias: Sequence[Via]) -> list[Via]:
    return [
        Via(
            x=v.x,
            y=v.y,
            drill=v.drill,
            diameter=v.diameter,
            layers=v.layers,
            net=v.net,
            net_name=v.net_name,
            in_pad=v.in_pad,
            is_micro=v.is_micro,
        )
        for v in vias
    ]
