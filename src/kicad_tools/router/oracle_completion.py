"""KiCad-oracle completion loop for pour nets (Issue #5785, Epic #5784 Phase 1).

``kct route`` deliberately leaves power nets (GND, VCC, ...) to a copper pour
instead of routing them (:func:`kicad_tools.router.auto_pour.auto_skip_pour_nets`).
When the pour cannot reach a pad -- an SMD pad on the outer layer while the
plane is on an inner layer, a pad boxed in by signal traces, a pour island
that never got a via -- the pad is stranded.  Our own connectivity model
(:class:`~kicad_tools.analysis.net_status.NetStatusAnalyzer`) cannot see a
real zone fill well enough to be trusted with that verdict, and before this
module it labelled such pads "advisory" while ``kct route`` exited 0 and
``kicad-cli pcb drc`` reported them as ``unconnected_items`` (board 02: 2,
board 03: 24 -- ``docs/research/kicad-routing-tools-comparison.md``).

This module makes KiCad itself the authority.  The loop is:

1. Run the existing :func:`kicad_tools.drc.geometric.run_geometric_drc`
   (``kicad-cli pcb drc --refill-zones``) and read its ``unconnected_items``.
2. Close **exactly those links** on the pour nets this run left to the fill,
   against all existing copper held fixed:

   * a stranded **pad** is welded into its net's plane with the ``kct stitch``
     placement cascade (straight / dog-leg / escape trace + via), restricted to
     the pads KiCad named (``run_stitch(only_pads=...)``);
   * two **zone fills on different layers** that KiCad reports as unjoined are
     welded with one via placed inside both fills.

3. Refill the zones and ask KiCad again.  A round is kept only when the
   unconnected count fell **and** the error-severity DRC count did not rise;
   otherwise the board is restored byte-for-byte.  A DRC regression is first
   attributed to the closers whose new copper sits at the violation, those
   closers are banned, and the round is retried without them.
4. Repeat up to ``max_rounds``, stopping early at zero links or when the
   count stops falling.

Credit: the design -- route the exact links KiCad's own DRC reports as
unconnected, after a zone refill, and repeat for a few rounds until KiCad is
satisfied -- is KiCadRoutingTools' ``py_router/kicad_oracle.py:oracle_reconnect``
plus its in-run plane finalize (https://github.com/drandyhaas/KiCadRoutingTools,
MIT, pinned at ``64df3f58``).  This is a reimplementation on our own stitch
placement and DRC helpers; no KRT code is copied.

The loop itself (:func:`run_oracle_completion`) takes the oracle and the link
closer as injected callables so its termination and keep/restore rules are
unit-testable without kicad-cli.
"""

from __future__ import annotations

import logging
import math
import re
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kicad_tools.drc.geometric import GeometricDRCResult
    from kicad_tools.drc.violation import DRCViolation

__all__ = [
    "DEFAULT_ORACLE_ROUNDS",
    "STOP_CONVERGED",
    "STOP_DRC_REGRESSED",
    "STOP_MAX_ROUNDS",
    "STOP_NO_CLOSER",
    "STOP_NO_PROGRESS",
    "STOP_NOT_RUN",
    "STOP_ORACLE_FAILED",
    "ClosureAttempt",
    "OracleCompletionResult",
    "OracleEndpoint",
    "OracleLink",
    "OracleRound",
    "PourLinkCloser",
    "links_from_violations",
    "run_oracle_completion",
]

logger = logging.getLogger(__name__)

#: Default number of oracle rounds (KRT uses 3 as well).
DEFAULT_ORACLE_ROUNDS = 3

#: Most floating fill islands bonded per net in one attempt.
MAX_ISLAND_JOINS = 16

#: Why the loop stopped (``OracleCompletionResult.stop_reason``).
STOP_NOT_RUN = "oracle_not_run"  # kicad-cli absent / timed out on the first call
STOP_CONVERGED = "converged"  # zero in-scope links left
STOP_NO_CLOSER = "no_closer"  # nothing could be placed for the remaining links
STOP_NO_PROGRESS = "no_progress"  # a round did not lower the count (restored)
STOP_DRC_REGRESSED = "drc_regressed"  # a round raised DRC errors (restored)
STOP_ORACLE_FAILED = "oracle_failed"  # kicad-cli failed mid-loop (restored)
STOP_MAX_ROUNDS = "max_rounds"  # round budget spent while still falling
STOP_DEADLINE = "deadline"  # the next kicad-cli call would overrun --timeout (#6273)

#: Issue #6273: a kicad-cli call is only started when the time left covers
#: its estimated cost times this factor (KiCad load + DRC time varies run to
#: run, and an overrun is fatal: the route supervisor kills the whole run).
ORACLE_DEADLINE_SAFETY = 1.25

# ---------------------------------------------------------------------------
# Parsing kicad-cli ``unconnected_items`` into links
# ---------------------------------------------------------------------------

_NET_RE = re.compile(r"\[([^\]]+)\]")
# "Pad 2 [GND] of C2 on F.Cu", "PTH pad 2 [GND] of J1", "SMD pad A4 [X] of J1"
_PAD_RE = re.compile(r"\bpad\s+(?P<pad>\S+)\s+\[(?P<net>[^\]]+)\]\s+of\s+(?P<ref>[^\s,]+)", re.I)
_LAYER_RE = re.compile(r"\bon\s+([A-Za-z0-9_.]+\.Cu)\b")


@dataclass(frozen=True)
class OracleEndpoint:
    """One end of a KiCad unconnected link.

    ``kind`` is ``"pad"``, ``"zone"``, ``"via"``, ``"track"`` or ``"other"``.
    ``layer`` is ``None`` for items spanning layers (vias, PTH pads).
    """

    description: str
    kind: str
    net: str | None
    x: float
    y: float
    layer: str | None = None
    ref: str | None = None
    pad: str | None = None

    @property
    def pad_key(self) -> str | None:
        """``"REF.PAD"`` for a pad endpoint, else ``None``."""
        if self.kind == "pad" and self.ref and self.pad:
            return f"{self.ref}.{self.pad}"
        return None

    @classmethod
    def parse(cls, description: str, x: float, y: float) -> OracleEndpoint:
        net_match = _NET_RE.search(description)
        net = net_match.group(1) if net_match else None
        if net == "<no net>":
            net = None
        head = description.split("[", 1)[0].lower()
        pad_ref: str | None = None
        pad_num: str | None = None
        if description.startswith("Zone"):
            kind = "zone"
        elif description.startswith("Via"):
            kind = "via"
        elif "pad" in head:
            kind = "pad"
            m = _PAD_RE.search(description)
            if m:
                pad_num, pad_ref = m.group("pad"), m.group("ref")
        elif description.startswith(("Track", "Arc")):
            kind = "track"
        else:
            kind = "other"
        # A "F.Cu - B.Cu" span (via) has no single layer.
        layer_match = None if " - " in description else _LAYER_RE.search(description)
        return cls(
            description=description,
            kind=kind,
            net=net,
            x=float(x),
            y=float(y),
            layer=layer_match.group(1) if layer_match else None,
            ref=pad_ref,
            pad=pad_num,
        )


@dataclass(frozen=True)
class OracleLink:
    """A missing connection KiCad reports between two same-net items."""

    net: str
    a: OracleEndpoint
    b: OracleEndpoint

    @property
    def key(self) -> tuple:
        return (
            self.net,
            round(self.a.x, 2),
            round(self.a.y, 2),
            round(self.b.x, 2),
            round(self.b.y, 2),
        )

    def describe(self) -> str:
        return f"{self.net}: {self.a.description} <-> {self.b.description}"


def _item_positions(violation: DRCViolation) -> list[tuple[float, float]]:
    """Per-item positions of a parsed kicad-cli violation.

    The JSON parser appends the violation's own top-level ``pos`` (when
    present) before the per-item positions, so a list one longer than
    ``items`` starts with that extra point and it is dropped here.
    """
    locs = [(loc.x_mm, loc.y_mm) for loc in violation.locations]
    if len(locs) == len(violation.items) + 1:
        locs = locs[1:]
    return locs


def links_from_violations(violations: Iterable[DRCViolation]) -> list[OracleLink]:
    """Turn kicad-cli ``unconnected_items`` records into :class:`OracleLink` s.

    Records with fewer than two located items, or whose two ends resolve to
    different (or no) nets, are dropped -- they are not a same-net link that
    copper could close.
    """
    links: list[OracleLink] = []
    for v in violations:
        locs = _item_positions(v)
        if len(v.items) < 2 or len(locs) < 2:
            continue
        a = OracleEndpoint.parse(v.items[0], *locs[0])
        b = OracleEndpoint.parse(v.items[1], *locs[1])
        net = a.net or b.net
        if net is None or (a.net and b.net and a.net != b.net):
            continue
        links.append(OracleLink(net=net, a=a, b=b))
    return links


# ---------------------------------------------------------------------------
# Canonical link order (Issue #5934)
# ---------------------------------------------------------------------------
#
# ``kicad-cli pcb drc`` does NOT report a byte-identical board's
# ``unconnected_items`` reproducibly (measured on board 03, KiCad 10.0.1: 8
# DRC runs on one file gave 3-5 distinct reports, also with KiCad capped to
# one thread via ``MaximumThreads=1``).  The number of links per net is
# stable -- it is the number of pad-bearing copper clusters minus one -- but
# KiCad's ratsnest picks an arbitrary spanning tree over those clusters and,
# where several items share one anchor point (a via and the track ending on
# it, a pad and the track leaving it, a zone island and the via inside it),
# names an arbitrary one of them.  Nothing downstream may depend on that
# choice, starting with the order the links are handled in.

_KIND_RANK = {"pad": 0, "via": 1, "track": 2, "other": 3, "zone": 4}


def _natural(text: str | None) -> tuple:
    """Sort key ordering ``"R2"`` before ``"R10"`` and ``"A4"`` before ``"B1"``."""
    return tuple(
        (0, int(part), "") if part.isdigit() else (1, 0, part)
        for part in re.split(r"(\d+)", text or "")
        if part
    )


def _endpoint_key(end: OracleEndpoint) -> tuple:
    """Order-defining key of one link end.

    A zone end's layer is deliberately left out: KiCad anchors every zone end
    at the zone outline's first corner and names whichever of the net's zones
    shares the tie (``Zone [GND] on F.Cu <-> ... on B.Cu`` in one run,
    ``... <-> ... on In1.Cu`` in the next), and the closer treats a zone-zone
    link per net anyway.
    """
    return (
        _KIND_RANK.get(end.kind, 3),
        _natural(end.ref),
        _natural(end.pad),
        round(end.x, 3),
        round(end.y, 3),
        "" if end.kind == "zone" else (end.layer or ""),
    )


def canonical_links(links: Iterable[OracleLink]) -> list[OracleLink]:
    """``links`` in an order, and with an end orientation, KiCad cannot perturb.

    Each link's two ends are oriented by :func:`_endpoint_key` and the links
    sorted by net then ends, so two reports holding the same links in a
    different order hand the closer the same sequence (Issue #5934).
    """

    def full(end: OracleEndpoint) -> tuple:
        # The description only breaks exact ties (two zone ends of one net),
        # so it can never move a link past one with a different key.
        return (_endpoint_key(end), end.description)

    out: list[OracleLink] = []
    for lk in links:
        if full(lk.b) < full(lk.a):
            lk = OracleLink(net=lk.net, a=lk.b, b=lk.a)
        out.append(lk)
    out.sort(key=lambda lk: (lk.net, full(lk.a), full(lk.b)))
    return out


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------


@dataclass
class ClosureAttempt:
    """What a link closer did to the board in one attempt.

    ``footprints`` maps a closer key (e.g. ``"pad:GND:C2.2"``) to the new
    copper it placed, as ``(x1, y1, x2, y2, half_width)`` capsules (a via is
    a zero-length capsule), so a DRC regression can be attributed to it.
    """

    applied: int = 0
    footprints: dict[str, list[tuple[float, float, float, float, float]]] = field(
        default_factory=dict
    )
    notes: list[str] = field(default_factory=list)


#: ``closer(pcb_path, links, banned_keys) -> ClosureAttempt``; edits the board.
LinkCloser = Callable[[Path, Sequence[OracleLink], frozenset[str]], ClosureAttempt]
#: ``oracle(pcb_path) -> GeometricDRCResult``; never edits the board.
Oracle = Callable[[Path], "GeometricDRCResult"]


@dataclass
class OracleRound:
    """One executed round of :func:`run_oracle_completion`."""

    index: int
    links_before: int
    links_after: int
    errors_before: int
    errors_after: int
    applied: int
    attempts: int
    kept: bool
    banned: tuple[str, ...] = ()


@dataclass
class OracleCompletionResult:
    """Outcome of the oracle completion loop.

    ``initial_links`` / ``final_links`` count only the *in-scope* links (the
    pour nets handed to the loop).  ``final_link_details`` are the in-scope
    links KiCad still reports at the end.
    """

    ran: bool
    stop_reason: str
    initial_links: int = 0
    final_links: int = 0
    rounds: list[OracleRound] = field(default_factory=list)
    final_link_details: list[OracleLink] = field(default_factory=list)
    note: str | None = None

    @property
    def closed(self) -> int:
        return max(self.initial_links - self.final_links, 0)

    @property
    def converged(self) -> bool:
        return self.ran and self.final_links == 0


def _capsule_distance(px: float, py: float, cap: tuple[float, float, float, float, float]) -> float:
    x1, y1, x2, y2, half = cap
    dx, dy = x2 - x1, y2 - y1
    seg2 = dx * dx + dy * dy
    t = 0.0 if seg2 == 0 else max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / seg2))
    return math.hypot(px - (x1 + t * dx), py - (y1 + t * dy)) - half


def _violation_signature(v: DRCViolation) -> tuple:
    return (v.type_str, tuple(sorted((round(x, 3), round(y, 3)) for x, y in _item_positions(v))))


def attribute_regressions(
    before: GeometricDRCResult,
    after: GeometricDRCResult,
    footprints: dict[str, list[tuple[float, float, float, float, float]]],
    tolerance: float = 0.5,
) -> set[str]:
    """Closer keys whose new copper sits at an error that ``after`` added.

    An error is "new" when its (type, item positions) signature is absent from
    ``before``.  It is pinned on every closer with copper within ``tolerance``
    mm of one of its item positions.
    """
    old = {_violation_signature(v) for v in before.error_violations}
    culprits: set[str] = set()
    for v in after.error_violations:
        if _violation_signature(v) in old:
            continue
        for px, py in _item_positions(v):
            for key, caps in footprints.items():
                if any(_capsule_distance(px, py, c) <= tolerance for c in caps):
                    culprits.add(key)
    return culprits


def run_oracle_completion(
    pcb_path: Path,
    *,
    oracle: Oracle,
    closer: LinkCloser,
    nets: Iterable[str],
    max_rounds: int = DEFAULT_ORACLE_ROUNDS,
    max_retries: int = 2,
    log: Callable[[str], None] | None = None,
    time_left: Callable[[], float | None] | None = None,
    call_cost_estimate: float = 0.0,
) -> OracleCompletionResult:
    """Close the KiCad-reported unconnected links on ``nets``, round by round.

    Termination is guaranteed: at most ``max_rounds`` rounds, each with at
    most ``1 + max_retries`` closer attempts, and the loop stops early when

    * the in-scope link count reaches zero (``converged``),
    * the closer places nothing (``no_closer``),
    * a round does not lower the count (``no_progress``),
    * a round raises the error-severity DRC count and no closer can be
      blamed for it (``drc_regressed``), or
    * kicad-cli fails mid-loop (``oracle_failed``).

    Every round that is not kept restores the board byte-for-byte, so the
    loop never leaves the board with more links or more DRC errors than it
    started with.

    Args:
        pcb_path: Routed, zone-filled board; edited in place.
        oracle: Runs KiCad's DRC on the board (see
            :func:`kicad_tools.drc.geometric.run_geometric_drc`).
        closer: Places copper for a list of links (see :class:`PourLinkCloser`).
        nets: The pour nets in scope.  Links on other nets are reported by
            KiCad but neither counted nor closed here.
        max_rounds: Round budget (``0`` disables the loop).
        max_retries: Attribution retries per round after a DRC regression.
        log: Optional progress sink.
        time_left: Issue #6273.  Seconds the caller can still spend (``None``
            = unbounded).  ``kct route --timeout`` is a hard total enforced by
            killing the run, so the loop never starts a kicad-cli call it
            cannot finish: before every call it checks the time left against
            the call's estimated cost and stops with ``deadline`` instead
            (the board is left exactly as the last kept round wrote it).
        call_cost_estimate: Seconds one kicad-cli DRC on this board is
            expected to take before any has been measured (the caller passes
            its zone-fill time, the same KiCad load + fill).  Measured calls
            replace it.
    """
    scope = set(nets)
    say = log or (lambda _msg: None)
    #: Issue #6273: measured seconds of each bare DRC call and of each
    #: attempt (closer, which refills the zones, plus its DRC).
    drc_costs: list[float] = []
    attempt_costs: list[float] = []

    def affordable(*, attempt: bool) -> bool:
        if time_left is None:
            return True
        left = time_left()
        if left is None:
            return True
        drc = max([call_cost_estimate, *drc_costs])
        # An unmeasured attempt is a refill plus a DRC: about two DRC calls.
        estimate = (
            max(attempt_costs) if (attempt and attempt_costs) else drc * (2 if attempt else 1)
        )
        return left >= estimate * ORACLE_DEADLINE_SAFETY

    def in_scope(geo: GeometricDRCResult) -> list[OracleLink]:
        # Canonical order: KiCad's report order is not reproducible (#5934).
        return canonical_links(
            lk for lk in links_from_violations(geo.unconnected_items) if lk.net in scope
        )

    if not affordable(attempt=False):
        return OracleCompletionResult(
            ran=False,
            stop_reason=STOP_DEADLINE,
            note="the --timeout budget left cannot cover a kicad-cli DRC run (issue #6273)",
        )
    started = time.monotonic()
    geo = oracle(pcb_path)
    drc_costs.append(time.monotonic() - started)
    if not geo.ran:
        return OracleCompletionResult(ran=False, stop_reason=STOP_NOT_RUN, note=geo.note)

    links = in_scope(geo)
    result = OracleCompletionResult(
        ran=True,
        stop_reason=STOP_MAX_ROUNDS,
        initial_links=len(links),
        final_links=len(links),
        final_link_details=list(links),
    )
    if not links:
        result.stop_reason = STOP_CONVERGED
        return result

    #: Closer keys applied in a KEPT round.  If their link is still reported,
    #: re-applying the same copper cannot help (the first one did not connect
    #: it), so later rounds try something else for that link.
    settled: set[str] = set()
    for rnd in range(max_rounds):
        count, errors = len(links), geo.error_count
        say(
            f"  Oracle round {rnd + 1}: {count} unconnected link(s) on {', '.join(sorted({lk.net for lk in links}))}"
        )
        banned: set[str] = set(settled)
        backup = pcb_path.read_bytes()
        kept = False
        stop: str | None = None
        attempt = None
        attempts = 0
        geo_after = geo
        for _ in range(1 + max_retries):
            if not affordable(attempt=True):
                # The board holds the last kept round's bytes here: the first
                # attempt has not touched it and a refused one was restored.
                stop = STOP_DEADLINE
                break
            attempts += 1
            started = time.monotonic()
            attempt = closer(pcb_path, links, frozenset(banned))
            if attempt.applied == 0:
                pcb_path.write_bytes(backup)
                stop = STOP_NO_CLOSER
                break
            geo_after = oracle(pcb_path)
            attempt_costs.append(time.monotonic() - started)
            if not geo_after.ran:
                pcb_path.write_bytes(backup)
                stop = STOP_ORACLE_FAILED
                break
            if geo_after.error_count > errors:
                culprits = attribute_regressions(geo, geo_after, attempt.footprints) - banned
                pcb_path.write_bytes(backup)
                if not culprits:
                    stop = STOP_DRC_REGRESSED
                    break
                say(
                    f"    DRC +{geo_after.error_count - errors}; retrying without {', '.join(sorted(culprits))}"
                )
                banned |= culprits
                continue
            if len(in_scope(geo_after)) >= count:
                pcb_path.write_bytes(backup)
                stop = STOP_NO_PROGRESS
                break
            kept = True
            break
        else:
            stop = STOP_DRC_REGRESSED

        after_links = in_scope(geo_after) if kept else links
        result.rounds.append(
            OracleRound(
                index=rnd + 1,
                links_before=count,
                links_after=len(after_links),
                errors_before=errors,
                errors_after=geo_after.error_count if kept else errors,
                applied=attempt.applied if attempt is not None else 0,
                attempts=attempts,
                kept=kept,
                banned=tuple(sorted(banned)),
            )
        )
        if not kept:
            result.stop_reason = stop or STOP_NO_PROGRESS
            break
        if attempt is not None:
            settled |= set(attempt.footprints)
        geo, links = geo_after, after_links
        say(f"    kept: {count} -> {len(links)} link(s)")
        if not links:
            result.stop_reason = STOP_CONVERGED
            break

    result.final_links = len(links)
    result.final_link_details = list(links)
    return result


# ---------------------------------------------------------------------------
# The default link closer: plane welds on our stitch placement cascade
# ---------------------------------------------------------------------------


def _same_comp(a: dict, b: dict) -> bool:
    """Whether two ``{layer: geometry}`` components are the same copper."""
    return a.keys() == b.keys() and all(a[k].equals(b[k]) for k in a)


def _route_key(link: OracleLink) -> str:
    return f"route:{link.net}:{link.a.description}|{link.b.description}"


@dataclass
class PourLinkCloser:
    """Close pour-net links: weld the named pads into their planes, route the rest.

    Per attempt, against all existing copper held fixed:

    1. **Pad welds.**  Every pad KiCad named is welded into its net's plane
       with ``kct stitch``'s placement cascade (straight / dog-leg / escape
       trace + via), restricted to exactly those pads.  When our own model
       believes every named pad is already connected (KiCad disagrees), the
       pads are stitched anyway (``force_pads``) -- KiCad is the authority.
       Pads whose every via site sits inside a *foreign* pour get a second
       pass that lets that pour yield, because the refill below clears it.
    2. **Routed links.**  A link no weld closed is routed by
       :func:`kicad_tools.router.link_router.route_link`: from one KiCad
       endpoint to the other, or -- when that fails, or the ends are zone
       fills whose KiCad anchor is only the zone outline's corner -- from the
       endpoint's copper island to the nearest other island of the same net.

    After placing copper the zones are refilled (``refill``) so the saved
    board carries fresh fills.  Via and trace geometry come from the router's
    own rules so the new copper meets the same fab floors as the routed
    copper.  Every placed item is recorded in ``ClosureAttempt.footprints``
    under a stable key so a DRC regression can ban it on the retry.
    """

    via_size: float
    via_drill: float
    clearance: float
    trace_width: float
    avoid_via_in_pad: bool = True
    refill: Callable[[Path], object] | None = None
    #: Route links no via weld could close (see :mod:`kicad_tools.router.link_router`).
    route_links: bool = True
    #: Close whole copper clusters instead of the items KiCad happened to name
    #: (Issue #5934).  See :meth:`_cluster_links`.
    canonical_clusters: bool = True

    def __call__(
        self, pcb_path: Path, links: Sequence[OracleLink], banned: frozenset[str]
    ) -> ClosureAttempt:
        attempt = ClosureAttempt()
        links = canonical_links(links)
        if self.canonical_clusters:
            links = self._cluster_links(pcb_path, links, attempt)
        pads_by_net: dict[str, set[str]] = {}
        for lk in links:
            for end in (lk.a, lk.b):
                key = end.pad_key
                if key and f"pad:{lk.net}:{key}" not in banned:
                    pads_by_net.setdefault(lk.net, set()).add(key)

        welded: set[str] = set()
        if pads_by_net:
            welded = self._weld_pads(pcb_path, pads_by_net, attempt)
        pending = [
            lk
            for lk in links
            if not ({lk.a.pad_key, lk.b.pad_key} & welded) and _route_key(lk) not in banned
        ]
        if pending and self.route_links:
            self._route_links(pcb_path, pending, attempt)

        if attempt.applied and self.refill is not None:
            self.refill(pcb_path)
        return attempt

    # -- canonical clusters (Issue #5934) ---------------------------------------

    def _cluster_links(
        self, pcb_path: Path, links: Sequence[OracleLink], attempt: ClosureAttempt
    ) -> list[OracleLink]:
        """Replace KiCad's links with one canonical link per stranded cluster.

        KiCad's link COUNT per net is reproducible (pad-bearing clusters minus
        one), but WHICH links it reports is not: it names an arbitrary
        spanning tree over the clusters, and an arbitrary one of the items
        sharing each anchor.  Closing the named items therefore landed
        different stitches on a byte-identical board (Issue #5934).

        A spanning tree touches every cluster, so the set of clusters KiCad
        wants joined is stable even though the names are not.  When this
        board's own copper model splits a net into exactly KiCad's cluster
        count (its pad-bearing components; KiCad's ratsnest ignores padless
        islands), the model's components ARE those clusters, and the net's
        links are rebuilt from them: for every component except the one that
        owns the plane, a link from its canonical pad (lowest reference, then
        pad number) to the net's zone.  The existing machinery then welds that
        pad, or -- when no weld lands -- grows the component to the nearest
        other island of the net.  Both are functions of the board alone.

        A net whose counts disagree keeps KiCad's links (canonically ordered,
        see :func:`canonical_links`) and is named in ``attempt.notes``; that
        fallback is still exposed to KiCad's naming.
        """
        if not links:
            return list(links)
        try:
            from kicad_tools.router.link_router import (
                _build_model,
                net_components,
                terminal_for_pad,
            )
            from kicad_tools.schema.pcb import PCB

            pcb = PCB.load(str(pcb_path))
            model = _build_model(pcb)
        except Exception as exc:  # the canonical pass must never block closing
            attempt.notes.append(f"canonical clusters unavailable ({exc}); using KiCad's links")
            return list(links)

        ox, oy = pcb.board_origin
        net_ids = {net.name: num for num, net in pcb.nets.items() if net.name}
        counts: dict[str, int] = {}
        for lk in links:
            counts[lk.net] = counts.get(lk.net, 0) + 1

        rebuilt: dict[str, list[OracleLink]] = {}
        for net in sorted(counts):
            net_number = net_ids.get(net)
            if net_number is None:
                continue
            comps = net_components(model, net_number)
            # (component index, pad key, centroid) for every pad of the net.
            pads: list[tuple[int, str, str, float, float]] = []
            for fp in pcb.footprints:
                for pad in fp.pads:
                    if getattr(pad, "net_name", None) != net:
                        continue
                    t = terminal_for_pad(pcb, fp.reference, str(pad.number))
                    if t is None:
                        continue
                    owner = next(
                        (
                            idx
                            for idx, comp in enumerate(comps)
                            if any(
                                layer in comp and comp[layer].intersects(g)
                                for layer, g in t.layers.items()
                            )
                        ),
                        None,
                    )
                    if owner is not None:
                        pads.append((owner, fp.reference, str(pad.number), *t.point))
            clusters = sorted({owner for owner, *_ in pads})
            if len(clusters) != counts[net] + 1:
                attempt.notes.append(
                    f"{net}: copper model has {len(clusters)} pad-bearing cluster(s), KiCad "
                    f"reports {counts[net] + 1}; closing KiCad's links as named"
                )
                continue
            # ``comps`` is sorted largest first, so ``comps[0]`` holds the plane.
            # When the plane carries no pad (a pour no pad reaches yet), every
            # pad cluster is stranded from it and all of them are closed.
            main = 0 if 0 in clusters else None
            out: list[OracleLink] = []
            for idx in clusters:
                if idx == main:
                    continue
                _, ref, num, cx, cy = min(
                    (p for p in pads if p[0] == idx),
                    key=lambda p: (_natural(p[1]), _natural(p[2])),
                )
                x, y = cx + ox, cy + oy
                pad_end = OracleEndpoint(
                    description=f"Pad {num} [{net}] of {ref}",
                    kind="pad",
                    net=net,
                    x=x,
                    y=y,
                    ref=ref,
                    pad=num,
                )
                zone_end = OracleEndpoint(
                    description=f"Zone [{net}]", kind="zone", net=net, x=x, y=y
                )
                out.append(OracleLink(net=net, a=pad_end, b=zone_end))
            rebuilt[net] = out
        if not rebuilt:
            return list(links)
        kept = [lk for lk in links if lk.net not in rebuilt]
        return canonical_links(kept + [lk for net in sorted(rebuilt) for lk in rebuilt[net]])

    # -- pads ---------------------------------------------------------------

    def _weld_pads(
        self, pcb_path: Path, pads_by_net: dict[str, set[str]], attempt: ClosureAttempt
    ) -> set[str]:
        from kicad_tools.cli.stitch_cmd import run_stitch, trace_to_track_segments

        nets = sorted(pads_by_net)
        only = frozenset(k for keys in pads_by_net.values() for k in keys)

        def stitch(pads: frozenset[str], force: frozenset[str], fills_yield: bool):
            return run_stitch(
                pcb_path,
                nets,
                via_size=self.via_size,
                drill=self.via_drill,
                clearance=self.clearance,
                trace_width=self.trace_width,
                avoid_pad_overlap=self.avoid_via_in_pad,
                only_pads=pads,
                force_pads=force,
                foreign_fills_yield=fills_yield,
                strict_drill_overlap=True,
            )

        force: frozenset[str] = frozenset()
        first = stitch(only, force, False)
        if not first.vias_added and first.already_connected:
            # Our connectivity model thinks the named pads are connected; KiCad
            # says they are not.  KiCad wins.
            force = only
            first = stitch(only, force, False)
        passes = [first]
        skipped = frozenset(f"{p.reference}.{p.pad_number}" for p, _ in first.pads_skipped)
        if skipped and self.refill is not None:
            # Pass 2: the pads every site of which sat inside a FOREIGN pour.
            # The refill below clears that pour around the new copper, so the
            # stale fill is not an obstacle (4-layer boards whose GND pours
            # cover every layer have no other via site).
            passes.append(stitch(skipped, force & skipped, True))

        for result in passes:
            traces = {id(t.pad): t for t in result.traces_added}
            for placement in result.vias_added:
                pad = placement.pad
                key = f"pad:{pad.net_name}:{pad.reference}.{pad.pad_number}"
                caps = attempt.footprints.setdefault(key, [])
                caps.append(
                    (
                        placement.via_x,
                        placement.via_y,
                        placement.via_x,
                        placement.via_y,
                        placement.size / 2,
                    )
                )
                trace = traces.get(id(pad))
                if trace is not None:
                    for seg in trace_to_track_segments(trace):
                        caps.append((seg.start_x, seg.start_y, seg.end_x, seg.end_y, seg.width / 2))
                attempt.applied += 1
        for pad, reason in passes[-1].pads_skipped:
            attempt.notes.append(f"{pad.net_name} {pad.reference}.{pad.pad_number}: {reason}")
        return {
            f"{placement.pad.reference}.{placement.pad.pad_number}"
            for result in passes
            for placement in result.vias_added
        }

    # -- routed links -----------------------------------------------------------

    def _route_links(
        self, pcb_path: Path, links: Sequence[OracleLink], attempt: ClosureAttempt
    ) -> None:
        from shapely.geometry import LineString, Point  # type: ignore[import-untyped]

        from kicad_tools.router.link_router import (
            LinkRouteRules,
            LinkTerminal,
            _build_model,
            _Item,
            append_link_routes,
            component_terminals,
            net_components,
            padless_components,
            route_link,
            terminal_at_copper,
            terminal_for_pad,
        )
        from kicad_tools.schema.pcb import PCB

        pcb = PCB.load(str(pcb_path))
        ox, oy = pcb.board_origin
        model = _build_model(pcb)
        rules = LinkRouteRules(
            trace_width=self.trace_width,
            clearance=self.clearance,
            via_size=self.via_size,
            via_drill=self.via_drill,
        )
        net_ids = {net.name: num for num, net in pcb.nets.items() if net.name}

        def terminal(end: OracleEndpoint, net_number: int) -> LinkTerminal | None:
            if end.kind == "pad" and end.ref and end.pad:
                return terminal_for_pad(pcb, end.ref, end.pad)
            if end.kind in ("via", "track"):
                return terminal_at_copper(
                    pcb, net_number, (end.x - ox, end.y - oy), end.layer, end.kind
                )
            return None

        def owning(comps: list[dict], t: LinkTerminal) -> int | None:
            for idx, comp in enumerate(comps):
                if any(
                    layer in comp and comp[layer].intersects(g) for layer, g in t.layers.items()
                ):
                    return idx
            return None

        joined_nets: set[str] = set()
        #: Routes committed this attempt, written to the file in ONE load/save
        #: at the end (Issue #5911).  Nothing below re-reads ``pcb_path`` --
        #: later links see earlier routes through ``model`` -- so deferring the
        #: write changes no decision and the saved bytes are identical.
        committed: list[Any] = []

        def commit(route: Any, key: str, net_number: int) -> None:
            committed.append(route)
            # Later links in this attempt must see this copper.
            for x1, y1, x2, y2, layer in route.segments:
                line = LineString([(x1, y1), (x2, y2)]) if (x1, y1) != (x2, y2) else Point(x1, y1)
                model.items.append(_Item(net_number, {layer: line.buffer(route.width / 2)}))
            for vx, vy in route.vias:
                disk = Point(vx, vy).buffer(route.via_size / 2)
                model.items.append(_Item(net_number, dict.fromkeys(model.copper, disk)))
                model.drills.append((vx, vy, route.via_drill))
            attempt.footprints.setdefault(key, []).extend(
                (x1 + ox, y1 + oy, x2 + ox, y2 + oy, hw) for x1, y1, x2, y2, hw in route.capsules()
            )
            attempt.applied += 1

        try:
            for lk in links:
                net_number = net_ids.get(lk.net)
                if net_number is None:
                    continue
                ta, tb = terminal(lk.a, net_number), terminal(lk.b, net_number)
                route = None
                if lk.a.kind == "zone" and lk.b.kind == "zone":
                    # KiCad's zone anchor is just the outline corner, so it does not
                    # say WHICH fill island floats.  Our copper model cannot be
                    # trusted to name it either (it over-splits a pour), so bond
                    # every island of the net that no pad-bearing main body owns --
                    # pad-less ones first -- to the rest.  A DRC-neutral superset;
                    # the keep/restore rule discards it if KiCad does not improve.
                    if lk.net not in joined_nets:
                        joined_nets.add(lk.net)
                        comps = net_components(model, net_number)
                        first = padless_components(model, net_number, comps)
                        islands = first + [c for c in comps[1:] if all(c is not f for f in first)]
                        for n_isl, comp in enumerate(islands[:MAX_ISLAND_JOINS]):
                            rest = [
                                c
                                for c in net_components(model, net_number)
                                if not _same_comp(c, comp)
                            ]
                            pair = component_terminals(comp, rest, lk.net)
                            if pair is None:
                                continue
                            r = route_link(
                                pcb, net_number, lk.net, pair[0], pair[1], rules, model=model
                            )
                            if r is not None:
                                commit(r, f"route:{lk.net}:island{n_isl}", net_number)
                    continue
                if ta is not None and tb is not None:
                    # Route from the pad end when there is one (its axis aligns the grid).
                    if ta.label in ("via", "track") and tb.label not in ("via", "track"):
                        ta, tb = tb, ta
                    route = route_link(pcb, net_number, lk.net, ta, tb, rules, model=model)
                if route is None and lk.net not in joined_nets:
                    # Island join: KiCad says these two items are apart.  Grow from
                    # the endpoint's island to the nearest other island of the net.
                    comps = net_components(model, net_number)
                    if len(comps) >= 2:
                        chosen = None
                        for t in (ta, tb):
                            idx = owning(comps, t) if t is not None else None
                            if idx is not None and idx != 0:
                                chosen = idx
                                break
                        if chosen is None:
                            chosen = len(comps) - 1
                        pair = component_terminals(
                            comps[chosen], [c for k, c in enumerate(comps) if k != chosen], lk.net
                        )
                        if pair is not None:
                            route = route_link(
                                pcb, net_number, lk.net, pair[0], pair[1], rules, model=model
                            )
                if route is None:
                    attempt.notes.append(f"{lk.describe()}: no clearance-legal route")
                    continue
                commit(route, _route_key(lk), net_number)
        except BaseException:
            # Routes committed before the failure are still written (their
            # copper is already counted in ``attempt``).  A failure of that
            # write must not replace the error that got us here (Issue #6023):
            # log it and re-raise the original.
            try:
                append_link_routes(pcb_path, committed, (ox, oy))
            except Exception:
                logger.warning(
                    "oracle closer: writing %d committed route(s) to %s failed while "
                    "handling an earlier error",
                    len(committed),
                    pcb_path,
                    exc_info=True,
                )
            raise
        append_link_routes(pcb_path, committed, (ox, oy))
