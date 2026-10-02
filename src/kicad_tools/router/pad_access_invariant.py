"""Commit-time **pad-access invariant** -- no committed route may strand a pad.

Epic #5508 / Phase 2 (issue #5891).  Phase 1 built the two read-only halves of
the mechanism:

* :mod:`kicad_tools.router.pad_access` (Phase 1a) answers *"given the copper
  committed so far, can this pad still be reached?"* -- the **access set** of
  exit stubs and via sites, and :meth:`~kicad_tools.router.pad_access.AccessSet.is_empty`;
* :mod:`kicad_tools.router.access_witness` (Phase 1b) replays an ordered commit
  journal against that predicate **offline**, naming the commit that closed a
  stranded pad's access set after the run is over.

This module turns that offline verdict into an **inline rule**, per the epic's
"Proposed mechanism" item 2:

    A candidate route for net N is rejected (or priced prohibitively) if
    committing it would reduce any other unrouted pad's access set to empty.
    This is a hard rule, not a soft corridor.

Design
------

:class:`PadAccessInvariant` is a pure gate: it is handed a candidate
:class:`~kicad_tools.router.primitives.Route` *before* any copper is marked and
answers "would committing this strand somebody?" with an :class:`AccessVeto` or
``None``.  It never mutates the grid, never routes, and holds no opinion about
what the caller does with a veto -- the caller
(:meth:`kicad_tools.router.core.Autorouter._mark_route`) refuses the commit, and
the negotiated loop's existing per-connection failure path takes over.

Three properties make it affordable inside the commit path, where Phase 1b's
"zero cost inside the A* loop" exemption no longer applies:

1. **The pad prefilter needs no legality evaluation.**  Arming computes only a
   *conservative* access bounding box per protected terminal
   (:func:`conservative_access_bbox`) -- pad extents grown by the exit-stub
   length in all eight A* directions and dilated by the clearance halo.  That
   box provably contains the true
   :attr:`~kicad_tools.router.pad_access.AccessSet.bbox` (which is derived from
   the same candidates, minus the ones that turned out illegal), so
   :func:`~kicad_tools.router.pad_access.affected_pads`' soundness guarantee
   carries over: a terminal the prefilter rejects cannot have been changed by
   the candidate.  Arming is therefore O(pads) arithmetic, not O(pads) geometric
   sweeps.
2. **The "before" access set is cached against the grid's own mutation
   counter.**  Every copper mutation on the routing grid already appends a
   record to Phase 1b's :class:`~kicad_tools.router.access_witness.CommitJournal`
   (including the rip-up and resync paths that bypass ``_mark_route``), so
   ``len(journal)`` is an exact, monotonic copper version.  A cached "before"
   set is reused only while that version is unchanged, which makes the common
   case one access-set evaluation per affected terminal per commit instead of
   two, with no staleness window.
3. **Only a genuine non-empty -> empty transition vetoes.**  A terminal that was
   *already* stranded when the candidate arrived is not stranded *by* it (the
   #5639 rule Phase 1b's replay settled on), and a terminal belonging to the
   candidate's own net, or to a net that already has committed copper, is not
   protected at all.

Scope and bounds (stated here because they are the honest limits of Phase 2)
---------------------------------------------------------------------------

* **One enforcement point.**  The gate is consulted from
  ``Autorouter._mark_route`` and only when the caller opts in via
  ``enforce_pad_access=True``.  The single opt-in caller is the per-connection
  ``mark_route`` closure in ``Autorouter._route_net_negotiated``, which is the
  sole producer of *new, search-derived* grid-engine copper -- the initial pass,
  the grace pass, every rip-up iteration, the relief probe and the
  region-parallel path all reach the grid through it.  Rip-up *re-land* paths
  deliberately do not opt in: re-marking a victim restores copper that was
  already committed, and vetoing that would strand the victim it is rescuing.
* **The escape pre-phase is not gated.**  Its stubs are the baseline the epic
  measures access "at escape-prephase end" against, so they are by definition
  inside the baseline rather than candidates against it.
* **A bounded evaluation budget.**  :data:`MAX_INVARIANT_EVALUATIONS` caps the
  access-set evaluations one run may spend inside the gate.  Past the cap the
  gate stops evaluating and sets :attr:`PadAccessInvariant.truncated`; it then
  fails **open** (no veto), because a diagnostic that silently turns into a
  reach cliff on a pathological board is worse than one that says it ran out of
  budget.  :data:`MAX_PROTECTED_PADS` bounds the prefilter the same way, taking
  terminals in sorted ``(ref, pin)`` order so the truncation is deterministic.
* **No new reservation type.**  The epic's grounding asks Phase 2 to evaluate
  reusing ``RoutingGrid.reserve_corridor_cells`` (hard owner-set reservation of
  a pad's access cells) before inventing a second reservation primitive.  It was
  evaluated and deliberately **not** used: a hard reservation only makes
  ``_mark_segment`` / ``_mark_via`` *skip blocking* the reserved cell, it does
  not refuse the route, which is exactly how PR #4078 turned board 07's
  reversed DDR byte into copper shorts.  A veto refuses the copper outright and
  cannot produce that failure mode.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .pad_access import (
    EPS,
    AccessSet,
    compute_access_set,
    route_envelope,
    via_candidate_geometry,
)
from .primitives import Pad, Route, pad_half_extents

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .rules import DesignRules

__all__ = [
    "MAX_INVARIANT_EVALUATIONS",
    "MAX_PROTECTED_PADS",
    "AccessVeto",
    "PadAccessInvariant",
    "conservative_access_bbox",
    "format_veto_report",
]

#: Ceiling on :func:`~kicad_tools.router.pad_access.compute_access_set` calls
#: made inside the commit gate over one routing run.  Each call is a geometric
#: sweep over the copper committed so far; this bounds the gate's total cost to
#: roughly a single routing iteration's worth of work even on a board where
#: every commit lands next to a pending terminal.  Past the cap the gate fails
#: open and says so (:attr:`PadAccessInvariant.truncated`).
MAX_INVARIANT_EVALUATIONS = 20_000

#: Terminals one gate will protect.  The prefilter is O(protected) per commit,
#: so an unbounded set would make the gate's cost quadratic in a dense board's
#: pad count.  Terminals are taken in sorted ``(ref, pin)`` order, so the
#: truncation is deterministic rather than dict-insertion dependent.
MAX_PROTECTED_PADS = 2048


@dataclass(frozen=True)
class AccessVeto:
    """One refused commit: ``route``'s copper would have stranded ``pad_key``.

    Carries enough to be a *witness* rather than a bare rejection -- the same
    attribution Phase 1b's ``PadWitness`` reports offline, available at the
    moment the commit was refused.
    """

    pad_key: tuple[str, str]
    pad_net: int
    pad_net_name: str
    candidate_net: int
    candidate_net_name: str
    pass_name: str
    iteration: int
    stubs_before: int
    via_sites_before: int
    closing_refs: tuple[str, ...]
    closing_kinds: tuple[str, ...]

    @property
    def label(self) -> str:
        """``ref.pin`` (or ``ref``) label of the terminal that was protected."""
        ref, pin = self.pad_key
        return f"{ref}.{pin}" if pin else ref

    def one_line(self) -> str:
        """Compact human rendering for CLI / log output."""
        candidate = self.candidate_net_name or f"net{self.candidate_net}"
        victim = self.pad_net_name or f"net{self.pad_net}"
        closing = ", ".join(self.closing_refs) or "(unattributed)"
        return (
            f"{candidate} refused at {self.pass_name}[{self.iteration}]: "
            f"committing it would strand {self.label} ({victim}) -- "
            f"last access was {self.stubs_before} stub(s) / "
            f"{self.via_sites_before} via site(s), closed by {closing}"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "pad": self.label,
            "pad_ref": self.pad_key[0],
            "pad_pin": self.pad_key[1],
            "pad_net": self.pad_net,
            "pad_net_name": self.pad_net_name,
            "candidate_net": self.candidate_net,
            "candidate_net_name": self.candidate_net_name,
            "pass": self.pass_name,
            "iteration": self.iteration,
            "stubs_before": self.stubs_before,
            "via_sites_before": self.via_sites_before,
            "closing_refs": list(self.closing_refs),
            "closing_kinds": list(self.closing_kinds),
        }


def conservative_access_bbox(
    pad: Pad, rules: DesignRules, *, resolution: float, trace_width: float | None = None
) -> tuple[float, float, float, float]:
    """Superset of ``pad``'s :attr:`AccessSet.bbox`, computed without legality.

    :func:`~kicad_tools.router.pad_access.compute_access_set` derives its bbox
    from the pad's own extents, the eight candidate exit stubs and the via
    candidates sitting at their far ends, dilated by
    ``max(trace_width / 2 + trace_clearance, via_diameter / 2 + via_clearance)``.
    Every one of those terms is a function of pad geometry and the rules -- the
    *legality* decisions only ever REMOVE candidates -- so re-deriving the same
    envelope with all eight directions assumed legal yields a box that contains
    the real one for any copper state.

    That containment is what lets the commit gate prefilter terminals without
    evaluating a single clearance predicate at arm time, while keeping
    :func:`~kicad_tools.router.pad_access.affected_pads`' soundness guarantee:
    a terminal whose conservative box misses the candidate's dilated envelope
    cannot have had any access candidate flipped by it.
    """
    width = rules.trace_width if trace_width is None else trace_width
    stub_length = _round_up_to_cells(width + 2 * rules.trace_clearance, resolution)
    half_w, half_h = pad_half_extents(pad)
    _mfr, _drill, via_diameter = via_candidate_geometry(rules)
    # The farthest any access candidate can sit from the pad centre: the pad's
    # own circumscribing radius (an upper bound on every per-direction ray exit,
    # whatever the shape or rotation), plus the stub length, plus the radius of
    # the via candidate that sits at the stub's far end.
    reach = math.hypot(half_w, half_h) + stub_length + via_diameter / 2
    # ``compute_access_set`` dilates by ``max(width / 2 + trace_clearance,
    # rules.via_diameter / 2 + via_clearance)`` -- taking the max against the
    # fab-inflated diameter too keeps this a superset for every fab tier.
    dilation = max(
        width / 2 + rules.trace_clearance,
        max(rules.via_diameter, via_diameter) / 2 + rules.via_clearance,
    )
    span = reach + dilation
    return (
        round(pad.x - span, 6),
        round(pad.y - span, 6),
        round(pad.x + span, 6),
        round(pad.y + span, 6),
    )


def _round_up_to_cells(value: float, resolution: float) -> float:
    """Smallest whole number of grid cells that is at least ``value``.

    Mirrors :mod:`kicad_tools.router.pad_access`'s own helper so the stub length
    this module assumes is the one that module will actually use.
    """
    if resolution <= 0:
        return value
    return round(math.ceil(value / resolution - 1e-9) * resolution, 6)


def _boxes_overlap(
    a: tuple[float, float, float, float], b: tuple[float, float, float, float]
) -> bool:
    return not (a[2] < b[0] - EPS or a[0] > b[2] + EPS or a[3] < b[1] - EPS or a[1] > b[3] + EPS)


@dataclass
class PadAccessInvariant:
    """The commit-time gate.  Ask :meth:`veto_for` before marking any copper.

    Construct one per :class:`~kicad_tools.router.core.Autorouter`; it is inert
    (and costs nothing) until :meth:`arm` runs, which the router does lazily on
    the first guarded commit -- the moment the copper on the grid is exactly
    "fixed + escape pre-phase", i.e. the epic's measurement baseline.
    """

    router: Any
    enabled: bool = True

    #: Conservative access bbox per protected terminal, keyed ``(ref, pin)``.
    protected: dict[tuple[str, str], tuple[float, float, float, float]] = field(
        default_factory=dict
    )
    #: The pad object the SEARCH departs from (escape terminal when the escape
    #: pre-phase rewrote it), keyed the same way.
    pads: dict[tuple[str, str], Pad] = field(default_factory=dict)

    armed: bool = False
    truncated: bool = False
    """True once a budget (:data:`MAX_PROTECTED_PADS` /
    :data:`MAX_INVARIANT_EVALUATIONS`) was hit, so a consumer can say the gate
    stopped looking rather than that it found nothing."""

    evaluations: int = 0
    checks: int = 0
    """Candidate routes the gate was asked about (including cheap misses)."""

    vetoes: list[AccessVeto] = field(default_factory=list)

    max_protected_pads: int = MAX_PROTECTED_PADS
    max_evaluations: int = MAX_INVARIANT_EVALUATIONS

    _before: dict[tuple[str, str], tuple[int, AccessSet]] = field(default_factory=dict, repr=False)

    # -- arming ----------------------------------------------------------

    def arm(self) -> None:
        """Snapshot the protected terminal set against the current copper.

        Idempotent: a second call is a no-op, so the lazy "arm on first guarded
        commit" wiring in ``_mark_route`` does not need its own guard.
        """
        if self.armed:
            return
        self.armed = True
        if not self.enabled:
            return
        router = self.router
        rules = getattr(router, "rules", None)
        grid = getattr(router, "grid", None)
        all_pads: Mapping[tuple[str, str], Pad] = getattr(router, "pads", {}) or {}
        if rules is None or grid is None or not all_pads:
            return
        nets: Mapping[int, Sequence[tuple[str, str]]] = getattr(router, "nets", {}) or {}
        overrides: Mapping[tuple[str, str], Pad] = (
            getattr(router, "_escape_pad_overrides", {}) or {}
        )
        resolution = float(getattr(grid, "resolution", rules.grid_resolution))

        # Only terminals of real, multi-terminal nets can be stranded in a way
        # that costs reach: a net with a single terminal has nothing to connect
        # to, and net 0 is not a net.
        keys: list[tuple[str, str]] = []
        for net, members in nets.items():
            if not net or len(members) < 2:
                continue
            keys.extend(key for key in members if key in all_pads)
        keys.sort()
        if len(keys) > self.max_protected_pads:
            self.truncated = True
            keys = keys[: self.max_protected_pads]

        for key in keys:
            # The SEARCH's origin, not the physical pad centre -- a net that
            # went through the escape pre-phase departs from its escape
            # terminal (``core.py``'s ``_escape_pad_overrides``), and an access
            # set computed at the pad centre would describe a search nobody
            # runs.  Same rule Phase 1b's replay follows.
            pad = overrides.get(key, all_pads[key])
            self.pads[key] = pad
            self.protected[key] = conservative_access_bbox(
                pad,
                rules,
                resolution=resolution,
                trace_width=self._trace_width(pad, rules),
            )

    # -- the gate --------------------------------------------------------

    def veto_for(self, route: Route) -> AccessVeto | None:
        """Would committing ``route`` empty a protected terminal's access set?

        Returns the :class:`AccessVeto` naming the first such terminal (in
        sorted ``(ref, pin)`` order, so the answer is deterministic), or
        ``None`` when the commit is admissible.  Read-only: the grid is
        restored on every exit path.

        ``None`` is also returned, without evaluating anything, when the gate is
        disabled, when ``route`` carries no copper, when it is escape copper
        (the baseline, not a candidate against it), or when the evaluation
        budget is exhausted.
        """
        if not self.enabled:
            return None
        if route is None or not (route.segments or route.vias):
            return None
        if getattr(route, "is_escape", False):
            return None
        if not self.armed:
            self.arm()
        if not self.protected:
            return None
        if self.evaluations >= self.max_evaluations:
            self.truncated = True
            return None

        router = self.router
        rules = getattr(router, "rules", None)
        grid = getattr(router, "grid", None)
        if rules is None or grid is None:
            return None

        self.checks += 1
        envelope = route_envelope(route, rules)
        hits = [key for key, box in self.protected.items() if _boxes_overlap(box, envelope)]
        if not hits:
            return None
        hits.sort()

        # Resolved lazily: the prefilter above misses on the overwhelming
        # majority of commits, and walking ``grid.routes`` is the only part of
        # the check that scales with the board's committed copper.
        committed_nets = {
            int(getattr(other, "net", 0) or 0)
            for other in (getattr(grid, "routes", ()) or ())
            if not getattr(other, "is_escape", False)
        }
        candidate_net = int(getattr(route, "net", 0) or 0)

        for key in hits:
            pad = self.pads.get(key)
            if pad is None:
                continue
            pad_net = int(getattr(pad, "net", 0) or 0)
            if not pad_net or pad_net == candidate_net:
                continue
            # "Unrouted" in the epic's sense: the terminal's net has no
            # search-derived copper yet.  A net that already landed copper is
            # being served, and protecting its remaining terminals here would
            # veto the ordinary mid-net commits that connect them.
            if pad_net in committed_nets:
                continue
            before = self._access_before(key, pad, grid, rules)
            if before is None:
                return None  # budget exhausted mid-scan; fail open, already flagged
            if before.is_empty():
                # Already stranded when the candidate arrived -- the candidate
                # did not do it (the #5639 transition rule).
                continue
            after = self._access_with_candidate(key, pad, grid, rules, route)
            if after is None:
                return None
            if after.is_empty():
                return AccessVeto(
                    pad_key=key,
                    pad_net=pad_net,
                    pad_net_name=str(getattr(pad, "net_name", "")),
                    candidate_net=candidate_net,
                    candidate_net_name=str(getattr(route, "net_name", "")),
                    pass_name=self._pass_name(),
                    iteration=self._iteration(),
                    stubs_before=len(before.stubs),
                    via_sites_before=len(before.via_sites),
                    closing_refs=after.closing_refs(),
                    closing_kinds=tuple(sorted({item.kind for item in after.closing_copper})),
                )
        return None

    def record(self, veto: AccessVeto) -> None:
        """Retain ``veto`` for the run's diagnostics."""
        self.vetoes.append(veto)

    # -- reporting -------------------------------------------------------

    def summary_line(self) -> str:
        """One-line human summary for CLI output."""
        if not self.enabled:
            return "Pad-access invariant: disabled"
        suffix = " (budget truncated)" if self.truncated else ""
        return (
            f"Pad-access invariant: {len(self.vetoes)} commit(s) refused over "
            f"{self.checks} candidate(s), {self.evaluations} access-set "
            f"evaluation(s), {len(self.protected)} terminal(s) protected{suffix}"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "protected_pads": len(self.protected),
            "checks": self.checks,
            "evaluations": self.evaluations,
            "truncated": self.truncated,
            "vetoes": [veto.to_dict() for veto in self.vetoes],
        }

    def vetoed_pads(self) -> tuple[tuple[str, str], ...]:
        """Terminals the gate protected at least once, de-duplicated and sorted."""
        return tuple(sorted({veto.pad_key for veto in self.vetoes}))

    # -- internals -------------------------------------------------------

    def _copper_version(self) -> int:
        """Exact, monotonic copper version: Phase 1b's journal record count.

        Every physical copper mutation on the routing grid appends a record --
        ``mark_route`` / ``unmark_route`` / ``resync_route_occupancy``, which
        covers the escape pre-phase and the rip-up paths that bypass
        ``_mark_route`` -- so a cache entry tagged with this number is stale the
        moment anything changes.  Falls back to ``-1`` (never cacheable) when a
        router has no journal, rather than reusing a possibly-stale set.
        """
        journal = getattr(self.router, "commit_journal", None)
        if journal is None:
            return -1
        try:
            return len(journal)
        except TypeError:  # pragma: no cover - defensive; journal is a list wrapper
            return -1

    def _access_before(
        self, key: tuple[str, str], pad: Pad, grid: Any, rules: DesignRules
    ) -> AccessSet | None:
        version = self._copper_version()
        cached = self._before.get(key)
        if cached is not None and version >= 0 and cached[0] == version:
            return cached[1]
        access = self._evaluate(pad, grid, rules)
        if access is None:
            return None
        if version >= 0:
            self._before[key] = (version, access)
        return access

    def _access_with_candidate(
        self,
        key: tuple[str, str],
        pad: Pad,
        grid: Any,
        rules: DesignRules,
        route: Route,
    ) -> AccessSet | None:
        """Access set of ``pad`` **as if** ``route`` were already committed.

        Phase 1a's predicates read committed copper from ``grid.routes`` and
        nothing else, so a speculative apply is appending the candidate to that
        list for the duration of one evaluation.  The grid's blocked/usage
        raster is deliberately NOT touched: it only ever feeds the descriptive
        ``marking`` labels, never a legality decision (Phase 1a's design note
        1), so leaving it alone keeps this read-only and cheap.
        """
        routes = getattr(grid, "routes", None)
        if routes is None:
            return self._evaluate(pad, grid, rules)
        routes.append(route)
        try:
            return self._evaluate(pad, grid, rules)
        finally:
            # Remove by identity, and only our own entry: an evaluation cannot
            # mutate the list, but restoring defensively keeps a future change
            # to Phase 1a from corrupting the grid's committed copper.
            for index in range(len(routes) - 1, -1, -1):
                if routes[index] is route:
                    del routes[index]
                    break

    def _evaluate(self, pad: Pad, grid: Any, rules: DesignRules) -> AccessSet | None:
        if self.evaluations >= self.max_evaluations:
            self.truncated = True
            return None
        self.evaluations += 1
        return compute_access_set(pad, grid, rules, trace_width=self._trace_width(pad, rules))

    def _trace_width(self, pad: Pad, rules: DesignRules) -> float | None:
        """Net-class trace width for ``pad``'s net, or ``None`` for the default.

        The access set's stub width has to be the width the search would
        actually use, or the gate would protect a corridor nobody routes
        through.  ``Autorouter.net_class_map`` is the router's own
        ``dict[str, NetClassRouting]`` -- the same lookup the negotiated per-net
        width resolution does.
        """
        net_class_map = getattr(self.router, "net_class_map", None)
        if not net_class_map:
            return None
        net_name = str(getattr(pad, "net_name", ""))
        if not net_name:
            return None
        net_class = net_class_map.get(net_name)
        width = getattr(net_class, "trace_width", None) if net_class is not None else None
        return float(width) if width else None

    def _pass_name(self) -> str:
        journal = getattr(self.router, "commit_journal", None)
        return str(getattr(journal, "pass_name", "routing"))

    def _iteration(self) -> int:
        journal = getattr(self.router, "commit_journal", None)
        try:
            return int(getattr(journal, "iteration", 0))
        except (TypeError, ValueError):  # pragma: no cover - defensive
            return 0


def format_veto_report(vetoes: Iterable[AccessVeto], *, limit: int = 10) -> list[str]:
    """Human lines for the refused commits, newest last, capped at ``limit``.

    Shared by the CLI summary and the routing report so the two cannot drift.
    """
    items = list(vetoes)
    head = items[:limit]
    lines = [veto.one_line() for veto in head]
    if len(items) > len(head):
        lines.append(f"... and {len(items) - len(head)} more refused commit(s)")
    return lines
