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

Replay (Phase 1b, second half)
------------------------------

:func:`replay` walks these records against
:func:`~kicad_tools.router.pad_access.compute_access_set`, stepping the copper
forward one record at a time, and reports for every terminal that ended the run
unrouted: the first ``(pass, iteration)`` at which its access set became empty,
the nets whose copper closed it, and whether it still had a way out when the
escape pre-phase finished.  A terminal whose access set is **non-empty** at the
end is reported as such -- that is a search that refused legal copper (#5509's
category), not a stranding, and the witness must say so rather than inventing a
culprit.
"""

from __future__ import annotations

import contextlib
import json
import threading
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from .layers import Layer
from .primitives import Route, Segment, Via

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .pad_access import AccessSet
    from .rules import DesignRules

__all__ = [
    "ACCESS_WITNESS_SIDECAR_SUFFIX",
    "ACCESS_NON_EMPTY",
    "ACCESS_EMPTY",
    "ACCESS_NOT_EVALUATED",
    "JOURNAL_SCHEMA_VERSION",
    "MAX_JOURNAL_RECORDS",
    "MAX_WITNESS_EVALUATIONS",
    "MAX_WITNESS_PADS",
    "NO_NET_LABEL",
    "WITNESS_SCHEMA_VERSION",
    "AccessWitness",
    "CommitJournal",
    "CommitRecord",
    "PadWitness",
    "PASS_ESCAPE",
    "PASS_FIXED",
    "PASS_GRACE",
    "PASS_INITIAL",
    "PASS_ITERATION",
    "PASS_POST",
    "PASS_RELIEF",
    "PASS_RESET",
    "PASS_ROUTING",
    "load_access_witness_sidecar",
    "replay",
    "stranded_terminals",
    "witness_for_router",
]

#: Sidecar basename suffix, stem-keyed off the routed PCB
#: (``simple_led_routed.access_witness.json`` next to
#: ``simple_led_routed.kicad_pcb``).  Mirrors the ``.routing_plan.json``
#: contract (#5519): a stem-keyed derived name can never collide with a
#: user-authored input file, because no CLI flag reads one.
ACCESS_WITNESS_SIDECAR_SUFFIX = ".access_witness.json"

#: Bumped when the serialized record shape changes incompatibly.
JOURNAL_SCHEMA_VERSION = 1

#: Net label for copper that belongs to **no net at all** -- the board outline,
#: a keepout, an unconnected pad: KiCad net 0 with an empty net name.  Issue
#: #5639: the previous fallback spelled this ``net0``, which a reader (or a
#: script filtering ``closing_nets`` for real net names) cannot tell apart from
#: a net actually called ``net0``.  Angle-bracketed to match ``<board-edge>``
#: in ``closing_refs``, a spelling KiCad's own net names never take.
NO_NET_LABEL = "<no-net>"


def _net_label(net: int, net_name: str) -> str:
    """Human/reader-safe label for a piece of copper's net.

    A named net is its own label.  An *unnamed but real* net (a nonzero index
    whose name did not survive into the router, e.g. numeric-dialect input)
    keeps the ``net<N>`` spelling, which is unambiguous because the index is
    real.  Net 0 is not a net -- it gets :data:`NO_NET_LABEL`.
    """
    if net_name:
        return net_name
    if net:
        return f"net{net}"
    return NO_NET_LABEL


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
            name = _net_label(record.net, record.net_name)
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


# ===========================================================================
# Offline witness replay
# ===========================================================================

#: Bumped when the serialized witness shape changes incompatibly.  Independent
#: of :data:`JOURNAL_SCHEMA_VERSION`: the two blocks share a sidecar but not a
#: shape, and either can move without the other.
WITNESS_SCHEMA_VERSION = 1

#: ``access_at_escape_end`` / ``final_access`` vocabulary.  Spelled with a
#: hyphen because these strings are read by humans in ``net-status --why``.
ACCESS_NON_EMPTY = "non-empty"
ACCESS_EMPTY = "empty"
#: The replay never got far enough to evaluate this state (the evaluation
#: budget below ran out).  Distinct from ``empty`` on purpose: "we did not
#: look" must never read as "there was no way out".
ACCESS_NOT_EVALUATED = "not-evaluated"

#: Terminals a single replay will track.  The replay is O(pads) per journal
#: record, so a board that ends with hundreds of stranded pads would otherwise
#: turn a diagnostic into the run's dominant cost.  Terminals are taken in
#: sorted ``(ref, pin)`` order, so the truncation is deterministic.
MAX_WITNESS_PADS = 64

#: Ceiling on :func:`~kicad_tools.router.pad_access.compute_access_set` calls
#: in one replay.  Each call is a full geometric sweep over the copper
#: committed so far; this bounds a pathological board's replay to something
#: comparable to a single routing iteration.  On exhaustion the replay stops
#: and :attr:`AccessWitness.truncated` says so -- a partial witness that
#: admits it beats a complete one nobody waited for.
MAX_WITNESS_EVALUATIONS = 5000

#: Passes that precede the negotiated search.  The replay's baseline -- Epic
#: #5508's "access at escape-prephase end" -- is the state after the last of
#: these.
_BASELINE_PASSES = frozenset({PASS_FIXED, PASS_ESCAPE})


@dataclass(frozen=True)
class PadWitness:
    """What the journal says about one terminal's access set.

    The fields answer, in order: *did this pad ever lose every way out*, *when*,
    and *whose copper did it*.

    ``first_closed_at`` is ``None`` when no journal record ever took this pad
    from a non-empty access set to an empty one -- which is a real and
    important answer, not a missing one.  It covers two cases:

    * the access set never emptied: a pad that ends the run unrouted with a
      non-empty access set was not stranded by committed copper at all, so no
      commit can be blamed for it (the #5509 category, and the negative
      control the epic requires);
    * the access set was **already** empty before the first record landed:
      placement stranded the pad, and again no commit can be blamed (#5639 --
      attributing the first record that merely re-evaluated such a pad is what
      produced the impossible ``empty`` / non-null / ``empty`` verdict).
    """

    ref: str
    pin: str
    net: int
    net_name: str

    access_at_escape_end: str
    """:data:`ACCESS_NON_EMPTY` / :data:`ACCESS_EMPTY` / :data:`ACCESS_NOT_EVALUATED`
    at the end of the escape pre-phase (the replay's baseline)."""

    final_access: str
    """The same vocabulary, evaluated after the last journal record."""

    first_closed_at: tuple[str, int] | None = None
    """``(pass, iteration)`` of the record that first emptied the access set."""

    first_closed_index: int | None = None
    """Journal index of that record, so a reader can find it verbatim."""

    first_closed_kind: str | None = None
    """That record's :attr:`CommitRecord.kind` (normally ``"commit"``)."""

    closing_nets: tuple[str, ...] = ()
    """Net names of the copper that rejected every remaining candidate.

    Copper that belongs to no net (the board outline, a keepout) is reported
    as :data:`NO_NET_LABEL`, never as a synthetic ``net0`` a reader could
    mistake for a real net name (#5639)."""

    closing_refs: tuple[str, ...] = ()
    """``ref.pin`` labels of that copper (``ref`` alone for route copper)."""

    closing_copper_class: tuple[str, ...] = ()
    """Phase 1a's :attr:`~kicad_tools.router.pad_access.ClosingCopper.kind`
    classification -- ``foreign_pad`` / ``route_segment`` / ``route_via`` /
    ``fixed_fill`` / ``keepout`` / ``board_edge`` / ..."""

    closing_markings: tuple[str, ...] = ()
    """Raster markings read at the closing copper (descriptive labels only --
    they never took part in the legality decision)."""

    reopened: bool = False
    """True when a later rip-up gave this terminal a way out again.  The pad
    was stranded at ``first_closed_at`` and is not stranded now, so a reader
    must not present the closing commit as the final cause."""

    @property
    def pad_key(self) -> tuple[str, str]:
        return (self.ref, self.pin)

    @property
    def label(self) -> str:
        return f"{self.ref}.{self.pin}" if self.pin else self.ref

    @property
    def stranded(self) -> bool:
        """True when the terminal has no legal first move left."""
        return self.final_access == ACCESS_EMPTY

    def one_line(self) -> str:
        """Compact human rendering, e.g. ``U3.1: closed at initial[0] by COMP``."""
        if self.first_closed_at is None:
            # #5639: an empty access set with nothing to blame is the
            # placement-stranded case, not the #5509 "refused legal copper"
            # one -- saying the latter of a pad that never had a way out
            # misreads the verdict in the one place a human reads it.
            reason = (
                "stranded before the first commit"
                if self.final_access == ACCESS_EMPTY
                else "search refused legal copper"
            )
            return f"{self.label}: access {self.final_access}, no commit closed it ({reason})"
        pass_name, iteration = self.first_closed_at
        nets = ", ".join(self.closing_nets) or "(unattributed)"
        suffix = " (reopened by a later rip-up)" if self.reopened else ""
        return f"{self.label}: closed at {pass_name}[{iteration}] by {nets}{suffix}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "ref": self.ref,
            "pin": self.pin,
            "net": self.net,
            "net_name": self.net_name,
            "access_at_escape_end": self.access_at_escape_end,
            "final_access": self.final_access,
            "first_closed_at": (
                {"pass": self.first_closed_at[0], "iteration": self.first_closed_at[1]}
                if self.first_closed_at is not None
                else None
            ),
            "first_closed_index": self.first_closed_index,
            "first_closed_kind": self.first_closed_kind,
            "closing_nets": list(self.closing_nets),
            "closing_refs": list(self.closing_refs),
            "closing_copper_class": list(self.closing_copper_class),
            "closing_markings": list(self.closing_markings),
            "reopened": self.reopened,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> PadWitness:
        closed = payload.get("first_closed_at")
        return cls(
            ref=str(payload.get("ref", "")),
            pin=str(payload.get("pin", "")),
            net=int(payload.get("net", 0)),
            net_name=str(payload.get("net_name", "")),
            access_at_escape_end=str(payload.get("access_at_escape_end", ACCESS_NOT_EVALUATED)),
            final_access=str(payload.get("final_access", ACCESS_NOT_EVALUATED)),
            first_closed_at=(
                (str(closed["pass"]), int(closed["iteration"])) if closed is not None else None
            ),
            first_closed_index=(
                int(payload["first_closed_index"])
                if payload.get("first_closed_index") is not None
                else None
            ),
            first_closed_kind=(
                str(payload["first_closed_kind"])
                if payload.get("first_closed_kind") is not None
                else None
            ),
            closing_nets=tuple(str(n) for n in payload.get("closing_nets", ())),
            closing_refs=tuple(str(n) for n in payload.get("closing_refs", ())),
            closing_copper_class=tuple(str(n) for n in payload.get("closing_copper_class", ())),
            closing_markings=tuple(str(n) for n in payload.get("closing_markings", ())),
            reopened=bool(payload.get("reopened", False)),
        )


@dataclass(frozen=True)
class AccessWitness:
    """Replay-derived verdict for every terminal the replay tracked.

    Carries the clearance values the access sets were decided with, because a
    witness is only as good as the resolver that produced it: Epic #5508's
    Phase 1a inherits #5509's caveat that the via-site branch uses today's
    rule resolver, so a witness can be off by exactly that delta until #5509
    Phase 2 lands.  Recording the numbers is what lets Phase 1c flag it
    instead of guessing.
    """

    pads: tuple[PadWitness, ...] = ()
    record_count: int = 0
    """Journal records the replay walked."""

    evaluations: int = 0
    """:func:`~kicad_tools.router.pad_access.compute_access_set` calls made."""

    truncated: bool = False
    """True when the journal, the pad list, or the evaluation budget was cut."""

    clearance: tuple[tuple[str, float], ...] = ()
    """Resolver values in force, as sorted ``(name, mm)`` pairs."""

    def __bool__(self) -> bool:
        return bool(self.pads)

    def __len__(self) -> int:
        return len(self.pads)

    def __iter__(self) -> Iterator[PadWitness]:
        return iter(self.pads)

    @property
    def stranded_pads(self) -> tuple[PadWitness, ...]:
        """Terminals whose access set is empty at the end of the run."""
        return tuple(p for p in self.pads if p.stranded)

    def for_net(self, net_name: str) -> AccessWitness:
        """A view carrying only ``net_name``'s terminals (metadata preserved)."""
        return AccessWitness(
            pads=tuple(p for p in self.pads if p.net_name == net_name),
            record_count=self.record_count,
            evaluations=self.evaluations,
            truncated=self.truncated,
            clearance=self.clearance,
        )

    def for_pad(self, ref: str, pin: str) -> PadWitness | None:
        for pad in self.pads:
            if pad.ref == ref and pad.pin == pin:
                return pad
        return None

    def summary_line(self) -> str:
        closed = sum(1 for p in self.pads if p.first_closed_at is not None)
        suffix = " (truncated)" if self.truncated else ""
        return (
            f"Access witness: {len(self.pads)} terminal(s) replayed over "
            f"{self.record_count} journal record(s) -- {closed} closed by a "
            f"commit{suffix}"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": WITNESS_SCHEMA_VERSION,
            "record_count": self.record_count,
            "evaluations": self.evaluations,
            "truncated": self.truncated,
            "clearance": dict(self.clearance),
            "pads": [p.to_dict() for p in self.pads],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> AccessWitness:
        """Inverse of :meth:`to_dict`; rejects an unknown schema version."""
        version = int(payload.get("schema_version", 0))
        if version != WITNESS_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported access-witness schema_version {version} "
                f"(expected {WITNESS_SCHEMA_VERSION})"
            )
        clearance = payload.get("clearance") or {}
        return cls(
            pads=tuple(PadWitness.from_dict(p) for p in payload.get("pads", ())),
            record_count=int(payload.get("record_count", 0)),
            evaluations=int(payload.get("evaluations", 0)),
            truncated=bool(payload.get("truncated", False)),
            clearance=tuple(sorted((str(k), float(v)) for k, v in clearance.items())),
        )


def stranded_terminals(router: Any) -> list[tuple[str, str]]:
    """Terminals of every net that ended the run unrouted, in ``(ref, pin)`` order.

    Two sources, unioned because neither alone is complete:

    * ``router.get_failed_nets()`` -- nets with no committed copper at all
      (an escape stub does not count, per #3441).  Every pad of such a net is
      unrouted by definition.
    * ``router.routing_failures`` -- the per-edge failures, which also catch a
      *partially* routed net whose remaining pad never landed.

    Returns an empty list for a router that exposes neither (a stub, a
    non-grid engine), so a caller never has to type-check first.
    """
    pads: Mapping[tuple[str, str], Any] = getattr(router, "pads", {}) or {}
    if not pads:
        return []
    nets: Mapping[int, Sequence[tuple[str, str]]] = getattr(router, "nets", {}) or {}

    keys: set[tuple[str, str]] = set()
    failed_nets = getattr(router, "get_failed_nets", None)
    if callable(failed_nets):
        try:
            for net in failed_nets():
                keys.update(nets.get(net, ()))
        except Exception:  # pragma: no cover - defensive; router API is stable
            pass
    for failure in getattr(router, "routing_failures", ()) or ():
        for key in (getattr(failure, "source_pad", None), getattr(failure, "target_pad", None)):
            if isinstance(key, tuple) and key in pads:
                keys.add(key)
    return sorted(k for k in keys if k in pads)


class _ReplayCopper:
    """The copper present at one point in the replay, as ``grid.routes`` sees it.

    Records carry geometry *snapshots*, not references, so a removal cannot be
    matched by object identity against what the replay has appended.  Matching
    is by ``route_id`` (the ``id()`` of the live Route the journal observed,
    which a rip and its commit share) with a geometry-key fallback for the
    resync case, where the stale and current geometry are different objects.
    """

    def __init__(self) -> None:
        self.routes: list[Route] = []
        self._by_id: dict[int, list[Route]] = {}
        self._keys: dict[int, tuple[Any, ...]] = {}

    def apply(self, record: CommitRecord) -> None:
        if record.added:
            route = record.route
            self.routes.append(route)
            self._by_id.setdefault(record.route_id, []).append(route)
            self._keys[id(route)] = record.geometry_key()
            return
        bucket = self._by_id.get(record.route_id)
        if bucket:
            route = bucket.pop()
            self._drop(route)
            return
        # Fallback: a wholesale resync removes geometry the replay appended
        # under a different route_id.  Match the most recent identical shape.
        key = record.geometry_key()
        for route in reversed(self.routes):
            if self._keys.get(id(route)) == key:
                self._drop(route)
                return

    def _drop(self, route: Route) -> None:
        self._keys.pop(id(route), None)
        for index in range(len(self.routes) - 1, -1, -1):
            if self.routes[index] is route:
                del self.routes[index]
                return


def _closing_summary(access: AccessSet) -> dict[str, tuple[str, ...]]:
    """Attribution fields from an empty access set's closing copper."""
    nets: set[str] = set()
    refs: set[str] = set()
    kinds: set[str] = set()
    markings: set[str] = set()
    for item in access.closing_copper:
        # #5639: board-edge / keepout copper is net 0 with no name; it must not
        # be reported as a net called "net0".
        nets.add(_net_label(item.net, item.net_name))
        refs.add(f"{item.ref}.{item.pin}" if item.pin else item.ref)
        kinds.add(item.kind)
        markings.update(item.marking)
    return {
        "closing_nets": tuple(sorted(nets)),
        "closing_refs": tuple(sorted(refs)),
        "closing_copper_class": tuple(sorted(kinds)),
        "closing_markings": tuple(sorted(markings)),
    }


def _clearance_values(rules: DesignRules) -> tuple[tuple[str, float], ...]:
    names = ("trace_width", "trace_clearance", "via_clearance", "min_hole_to_hole")
    return tuple(
        sorted((name, float(getattr(rules, name))) for name in names if hasattr(rules, name))
    )


def replay(
    journal: CommitJournal,
    router: Any,
    *,
    pad_keys: Sequence[tuple[str, str]] | None = None,
    rules: DesignRules | None = None,
    max_pads: int = MAX_WITNESS_PADS,
    max_evaluations: int = MAX_WITNESS_EVALUATIONS,
) -> AccessWitness:
    """Name the commit that closed each tracked terminal's access set.

    Walks ``journal`` from an empty board, applying each record's copper in
    order, and re-evaluates
    :func:`~kicad_tools.router.pad_access.compute_access_set` for the terminals
    a record could actually have changed
    (:func:`~kicad_tools.router.pad_access.affected_pads` over the record's
    dilated envelope).  **Read-only and offline**: no search runs, no routing
    decision is consulted, and nothing about the router's outcome changes.

    Args:
        journal: The run's ordered commit journal.
        router: The :class:`~kicad_tools.router.core.Autorouter` that produced
            it.  Supplies the grid (pads, board bounds, clearance predicates),
            the design rules, and -- when ``pad_keys`` is omitted -- the set of
            terminals that ended unrouted.
        pad_keys: Explicit terminals to track, instead of
            :func:`stranded_terminals`.  Used by the negative control, where
            the point is to interrogate a pad that is *not* stranded.
        rules: Design rules override; defaults to ``router.rules``.
        max_pads: Cap on tracked terminals (:data:`MAX_WITNESS_PADS`).
        max_evaluations: Cap on access-set evaluations
            (:data:`MAX_WITNESS_EVALUATIONS`).

    Returns:
        The :class:`AccessWitness`.  Empty (falsy) when nothing ended unrouted,
        which is the overwhelmingly common case on a board that routes.

    Note:
        The replay temporarily rebinds ``grid.routes`` to its own list so the
        grid's clearance predicates -- which read that attribute and nothing
        else for committed copper -- see the copper of the replayed *step*
        rather than the finished board.  The original list is restored on every
        exit path.  The grid's blocked/usage **raster** is not replayed, so the
        ``closing_markings`` labels describe the finished board; they are
        descriptive only and never take part in a legality decision.
    """
    from .pad_access import affected_pads, compute_access_set, route_envelope

    grid = getattr(router, "grid", None)
    resolved_rules = rules if rules is not None else getattr(router, "rules", None)
    if grid is None or resolved_rules is None:
        return AccessWitness(record_count=len(journal), truncated=journal.truncated)

    all_pads: Mapping[tuple[str, str], Any] = getattr(router, "pads", {}) or {}
    keys = list(pad_keys) if pad_keys is not None else stranded_terminals(router)
    keys = [k for k in keys if k in all_pads]
    truncated = journal.truncated or len(keys) > max_pads
    keys = keys[:max_pads]
    clearance = _clearance_values(resolved_rules)
    if not keys:
        return AccessWitness(
            record_count=len(journal),
            truncated=truncated,
            clearance=clearance,
        )

    # The SEARCH's origin, not the physical pad centre: a net that went through
    # the escape pre-phase starts from its escape terminal (core.py's
    # ``_escape_pad_overrides``), and an access set computed at the pad centre
    # would describe a search nobody ran.
    overrides = getattr(router, "_escape_pad_overrides", {}) or {}
    pads = {key: overrides.get(key, all_pads[key]) for key in keys}

    copper = _ReplayCopper()
    budget = max_evaluations
    evaluations = 0

    saved_routes = getattr(grid, "routes", [])
    try:
        grid.routes = copper.routes

        def evaluate(key: tuple[str, str]) -> AccessSet:
            nonlocal evaluations
            evaluations += 1
            return compute_access_set(pads[key], grid, resolved_rules)

        state: dict[tuple[str, str], AccessSet] = {}
        for key in keys:
            if evaluations >= budget:
                truncated = True
                break
            state[key] = evaluate(key)

        first_closed: dict[tuple[str, str], tuple[CommitRecord, dict[str, tuple[str, ...]]]] = {}
        baseline: dict[tuple[str, str], str] = {}
        seen_search_pass = False

        for record in journal.records:
            if not seen_search_pass and record.pass_name not in _BASELINE_PASSES:
                # First record of the negotiated search: everything before it
                # is the escape-pre-phase baseline.
                baseline = {k: _state_label(a) for k, a in state.items()}
                seen_search_pass = True
            copper.apply(record)
            if evaluations >= budget:
                truncated = True
                break
            envelope = route_envelope(record.route, resolved_rules)
            for key in affected_pads(state, envelope):
                if evaluations >= budget:
                    truncated = True
                    break
                access = evaluate(key)
                previous = state.get(key)
                state[key] = access
                # #5639: attribute a record only on a genuine non-empty ->
                # empty TRANSITION.  A pad that was already empty when this
                # record landed was not closed by it -- the record merely
                # happened to be the first one whose envelope brought the pad
                # back up for re-evaluation.  Blaming it produced the
                # impossible ``empty`` / non-null / ``empty`` verdict seen on
                # board-07's U5.1..U5.8, where the "closing copper" was the
                # board outline (which no commit can place).
                if (
                    access.is_empty()
                    and key not in first_closed
                    and previous is not None
                    and not previous.is_empty()
                ):
                    first_closed[key] = (record, _closing_summary(access))
        if not seen_search_pass:
            baseline = {k: _state_label(a) for k, a in state.items()}
    finally:
        grid.routes = saved_routes

    witnesses: list[PadWitness] = []
    for key in keys:
        pad = pads[key]
        closed = first_closed.get(key)
        final = _state_label(state.get(key))
        attribution = closed[1] if closed is not None else {}
        witnesses.append(
            PadWitness(
                ref=key[0],
                pin=key[1],
                net=int(getattr(pad, "net", 0)),
                net_name=str(getattr(pad, "net_name", "")),
                access_at_escape_end=baseline.get(key, ACCESS_NOT_EVALUATED),
                final_access=final,
                first_closed_at=(
                    (closed[0].pass_name, closed[0].iteration) if closed is not None else None
                ),
                first_closed_index=closed[0].index if closed is not None else None,
                first_closed_kind=closed[0].kind if closed is not None else None,
                reopened=closed is not None and final == ACCESS_NON_EMPTY,
                closing_nets=attribution.get("closing_nets", ()),
                closing_refs=attribution.get("closing_refs", ()),
                closing_copper_class=attribution.get("closing_copper_class", ()),
                closing_markings=attribution.get("closing_markings", ()),
            )
        )

    return AccessWitness(
        pads=tuple(witnesses),
        record_count=len(journal),
        evaluations=evaluations,
        truncated=truncated,
        clearance=clearance,
    )


def _state_label(access: AccessSet | None) -> str:
    if access is None:
        return ACCESS_NOT_EVALUATED
    return ACCESS_EMPTY if access.is_empty() else ACCESS_NON_EMPTY


def witness_for_router(router: Any) -> AccessWitness | None:
    """Replay ``router``'s own journal, memoizing the result on the router.

    Two post-route consumers want the same witness -- the
    ``<stem>.access_witness.json`` sidecar and ``kct route --format json`` --
    and the replay is the expensive half.  Computing it once per router keeps
    the second consumer free.

    Returns ``None`` when the router has no journal (a stub, a non-grid
    engine); returns an empty-but-present witness when the journal exists and
    nothing ended unrouted, so a caller can tell "not applicable" from
    "nothing stranded".
    """
    journal = getattr(router, "commit_journal", None)
    if journal is None:
        return None
    cached = getattr(router, "_access_witness_cache", None)
    if isinstance(cached, AccessWitness) and cached.record_count == len(journal):
        return cached
    witness = replay(journal, router)
    with contextlib.suppress(Exception):  # a frozen / slotted stub router
        router._access_witness_cache = witness
    return witness


def load_access_witness_sidecar(path: str | Path) -> AccessWitness | None:
    """Load the witness block of an ``.access_witness.json`` sidecar.

    ``net-status --why`` classifies a *saved* board: the live router that knew
    the commit order is long gone, so the sidecar is the only thing that can
    carry the witness across that boundary.

    Args:
        path: Either the sidecar itself, or -- the usual case -- the
            ``.kicad_pcb`` it belongs to, in which case
            ``<stem>.access_witness.json`` beside that board is read.
            Discovery is scoped to the board's own directory, matching the
            other single-directory sidecar consumers.

    Returns:
        ``None`` -- never raises -- when there is no sidecar, when it holds no
        witness block (a run where nothing was stranded writes none), or when
        it cannot be parsed.  A malformed diagnostic sidecar must not break
        output that worked without it.
    """
    given = Path(path)
    sidecar = (
        given
        if given.name.endswith(ACCESS_WITNESS_SIDECAR_SUFFIX)
        else given.parent / f"{given.stem}{ACCESS_WITNESS_SIDECAR_SUFFIX}"
    )
    if not sidecar.is_file():
        return None
    try:
        payload = json.loads(sidecar.read_text())
        block = payload.get("witness")
        if not isinstance(block, dict):
            return None
        return AccessWitness.from_dict(block)
    except (OSError, ValueError, TypeError, AttributeError):
        return None
