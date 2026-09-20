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


def _segment_to_dict(seg: Segment) -> dict[str, Any]:
    return {
        "x1": seg.x1,
        "y1": seg.y1,
        "x2": seg.x2,
        "y2": seg.y2,
        "width": seg.width,
        "layer": seg.layer.value,
        "net": seg.net,
        "net_name": seg.net_name,
    }


def _segment_from_dict(payload: dict[str, Any]) -> Segment:
    return Segment(
        x1=float(payload["x1"]),
        y1=float(payload["y1"]),
        x2=float(payload["x2"]),
        y2=float(payload["y2"]),
        width=float(payload["width"]),
        layer=Layer(int(payload["layer"])),
        net=int(payload.get("net", 0)),
        net_name=str(payload.get("net_name", "")),
    )


def _via_to_dict(via: Via) -> dict[str, Any]:
    return {
        "x": via.x,
        "y": via.y,
        "drill": via.drill,
        "diameter": via.diameter,
        "layers": [via.layers[0].value, via.layers[1].value],
        "net": via.net,
        "net_name": via.net_name,
        "in_pad": via.in_pad,
        "is_micro": via.is_micro,
    }


def _via_from_dict(payload: dict[str, Any]) -> Via:
    layers = payload.get("layers", [0, 5])
    return Via(
        x=float(payload["x"]),
        y=float(payload["y"]),
        drill=float(payload["drill"]),
        diameter=float(payload["diameter"]),
        layers=(Layer(int(layers[0])), Layer(int(layers[1]))),
        net=int(payload.get("net", 0)),
        net_name=str(payload.get("net_name", "")),
        in_pad=bool(payload.get("in_pad", False)),
        is_micro=bool(payload.get("is_micro", False)),
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

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable form (the sidecar's record shape)."""
        return {
            "index": self.index,
            "kind": self.kind,
            "added": self.added,
            "pass": self.pass_name,
            "iteration": self.iteration,
            "net": self.net,
            "net_name": self.net_name,
            "route_id": self.route_id,
            "is_escape": self.is_escape,
            "segments": [_segment_to_dict(seg) for seg in self.segments],
            "vias": [_via_to_dict(via) for via in self.vias],
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> CommitRecord:
        """Inverse of :meth:`to_dict`."""
        return cls(
            index=int(payload["index"]),
            kind=payload["kind"],
            added=bool(payload["added"]),
            pass_name=str(payload["pass"]),
            iteration=int(payload["iteration"]),
            net=int(payload["net"]),
            net_name=str(payload.get("net_name", "")),
            route_id=int(payload.get("route_id", 0)),
            is_escape=bool(payload.get("is_escape", False)),
            segments=tuple(_segment_from_dict(s) for s in payload.get("segments", ())),
            vias=tuple(_via_from_dict(v) for v in payload.get("vias", ())),
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
        """JSON-serializable form written to the sidecar."""
        return {
            "schema_version": JOURNAL_SCHEMA_VERSION,
            "truncated": self.truncated,
            "record_count": len(self.records),
            "records": [record.to_dict() for record in self.records],
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
        journal.records = [CommitRecord.from_dict(r) for r in payload.get("records", ())]
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
