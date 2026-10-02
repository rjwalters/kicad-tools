"""Access-loss **rip-up targeting** -- the witness as a rip-up target list.

Epic #5508 / Phase 3a (issue #5913).  The first two phases built the read side
of the mechanism and then the refusal side:

* :mod:`kicad_tools.router.pad_access` (Phase 1a) answers *"can this pad still
  be reached?"* and, when the answer is no, names the copper that closed it
  (:attr:`~kicad_tools.router.pad_access.AccessSet.closing_copper`);
* :mod:`kicad_tools.router.access_witness` (Phase 1b) replays an ordered commit
  journal against that predicate offline;
* :mod:`kicad_tools.router.pad_access_invariant` (Phase 2, #5891) refuses a
  commit that would reduce another unrouted pad's access set to empty.

None of them helps once a pad's access set **is** empty.  The enclosing copper
is legal and unshared, so it produces no overflow and the negotiated loop's
overuse-based rip-up cannot see it at all; the Bresenham blocker scan
(:meth:`~kicad_tools.router.algorithms.negotiated.NegotiatedRouter.find_blocking_nets_for_connection`)
only looks along the direct pad-to-pad line, which a sealing trace need not
touch.  The commit gate itself is not a complete answer either, by its own
documented bounds: it fails **open** past its evaluation budget, it does not
protect a terminal whose net already landed copper, and it only ever judges one
candidate at a time -- so a strand produced by a sequence of individually
admissible commits still happens.

This module is the rip-up half.  Given a net the loop failed to route, it asks
Phase 1a which of that net's **own** terminals have no way out, reads the
witness's closing copper, and returns the committed nets it names as rip-up
*targets*.  The caller feeds them to the existing
:meth:`~kicad_tools.router.algorithms.negotiated.NegotiatedRouter.targeted_ripup`
as ``blocking_nets``, so the rip-up, the reroute, the all-or-nothing rollback
and the ``ripup_history`` / ``max_ripups_per_net`` budgets are all the existing
transaction -- this phase changes *which nets go in*, nothing about what
happens to them.

It generalises ``DiffPairRouter._plan_corridor_yields`` /
``_apply_corridor_yields`` (issue #4463), which already does "which committed
coupled bodies seal a still-unconnected net in, and rip them" for exactly one
copper class.  Where that planner probes with a relaxed A* and is specific to
coupled bodies, this one reads a geometric witness and applies to any committed
net the negotiated loop owns.

Scope and bounds (the honest limits of Phase 3a)
------------------------------------------------

* **Only copper the negotiated loop owns is a target.**  The caller passes the
  rippable set, which is ``{net for net, routes in net_routes.items() if
  routes}`` -- the same ``net_routes.get(v)`` test
  ``Autorouter._relief_rescue_txn`` already uses to pick its victims.  Escape
  stubs, ``--preserve-existing`` copper and coupled diff-pair bodies committed
  by the pre-phase are not in ``net_routes``, so they are never named; that is
  the epic's binding scope guard, enforced by construction rather than by a
  list of exceptions.
* **Only rippable *kinds* are targets** (:data:`RIPPABLE_CLOSING_KINDS`).  A
  foreign pad, a fixed fill, a keepout, a hard reservation and the board edge
  all close access and none of them can be ripped up; naming them would charge
  a net's rip-up budget for copper no rip-up removes.  ``kelvin_isolated`` is
  excluded for the same reason from the other direction: it is the failed net's
  *own* branch, isolated for one edge search, and ripping it would delete the
  copper the search is departing from.
* **A named-but-unrippable net is reported, not acted on.**  The witness keeps
  it in :attr:`AccessLossWitness.held_nets` so a run can say "the pad is sealed
  by copper nobody here may touch" instead of looking like it found nothing.
* **A bounded evaluation budget.**  :data:`MAX_TARGETING_EVALUATIONS` caps the
  access evaluations one run may spend on targeting.  Past the cap the targeter
  stops and sets :attr:`AccessLossTargeter.truncated`; it then names nothing,
  which degrades to the pre-#5913 rip-up targeting rather than to a cliff.
* **No budget of the router's is touched.**  The targeting adds no routing
  time: ``per_net_timeout``, the node caps, ``max_iterations`` and the grace
  tiers are all unchanged, and no clearance is relaxed anywhere (every legality
  decision is Phase 1a's, at the configured rule values).
"""

from __future__ import annotations

import time
from collections.abc import Collection, Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .pad_access import AccessSet, ClosingCopper, compute_access_set, has_access
from .pad_access_invariant import net_class_trace_width
from .primitives import Pad

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .rules import DesignRules

__all__ = [
    "MAX_TARGETING_EVALUATIONS",
    "RIPPABLE_CLOSING_KINDS",
    "AccessLossTargeter",
    "AccessLossWitness",
    "format_access_loss_report",
    "rippable_closing_nets",
]

#: Ceiling on access evaluations one run may spend naming rip-up targets.  Each
#: is a geometric sweep over the copper committed so far, charged once per
#: terminal of a failed net per rip-up attempt.  The cheap existence test
#: answers the overwhelming majority of them (a terminal with a way out settles
#: on its first legal stub), so this bounds the pathological board -- every net
#: failing, every iteration -- rather than the normal one.  Past the cap the
#: targeter names nothing and says so (:attr:`AccessLossTargeter.truncated`).
MAX_TARGETING_EVALUATIONS = 8_000

#: Closing-copper kinds a rip-up can actually remove: route copper of a net the
#: negotiated loop owns.  Everything else that closes a pad's access --
#: ``foreign_pad``, ``fixed_fill``, ``keepout``, ``reserved_hard``,
#: ``board_edge`` -- survives any rip-up, and ``kelvin_isolated`` is the failed
#: net's own isolated branch.
RIPPABLE_CLOSING_KINDS: frozenset[str] = frozenset({"route_segment", "route_via"})


def rippable_closing_nets(
    access: AccessSet,
    *,
    failed_net: int,
    rippable_nets: Collection[int],
) -> tuple[dict[int, str], set[int]]:
    """Split one empty access set's witness into rip-up targets and held nets.

    Args:
        access: The access set of a terminal with no way out.  A non-empty set
            carries no ``closing_copper`` by construction, so passing one
            yields two empty results.
        failed_net: The net whose terminal this is.  Its own copper is never a
            target: ripping it would delete the copper the search departs from.
        rippable_nets: Nets the caller is willing and able to rip -- in
            production ``{net for net, routes in net_routes.items() if
            routes}``.

    Returns:
        ``(targets, held)`` -- ``targets`` maps net id to the witness's name for
        it (so a caller can log the name it saw rather than re-deriving one),
        and ``held`` is every *other* net the witness named, i.e. copper that
        closes this terminal but which no rip-up here may remove.
    """
    rippable = {int(net) for net in rippable_nets}
    targets: dict[int, str] = {}
    held: set[int] = set()
    for item in access.closing_copper:
        net = int(getattr(item, "net", 0) or 0)
        if not net or net == int(failed_net):
            continue
        if item.kind not in RIPPABLE_CLOSING_KINDS or net not in rippable:
            held.add(net)
            continue
        targets.setdefault(net, str(item.net_name or f"Net_{net}"))
    # A net that is a legitimate target through one item must not also be
    # reported as held through another (a net can close a pad with both a
    # segment and a pad).
    held -= set(targets)
    return targets, held


@dataclass(frozen=True)
class AccessLossWitness:
    """One failed net's access loss, with the committed nets to rip for it.

    The same attribution Phase 1b reports offline and Phase 2's
    :class:`~kicad_tools.router.pad_access_invariant.AccessVeto` reports at
    refusal time -- here at the moment the loop decides what to rip.
    """

    failed_net: int
    failed_net_name: str
    pass_name: str
    iteration: int
    stranded_pads: tuple[tuple[str, str], ...]
    closing_refs: tuple[str, ...]
    closing_kinds: tuple[str, ...]
    blocking_nets: tuple[int, ...]
    blocking_net_names: tuple[str, ...]
    held_nets: tuple[int, ...]

    @property
    def label(self) -> str:
        """Human name of the net that lost its access."""
        return self.failed_net_name or f"net{self.failed_net}"

    def pad_labels(self) -> tuple[str, ...]:
        """``ref.pin`` (or ``ref``) labels of the stranded terminals."""
        return tuple(f"{ref}.{pin}" if pin else ref for ref, pin in self.stranded_pads)

    def one_line(self) -> str:
        """Compact human rendering for CLI / log output."""
        pads = ", ".join(self.pad_labels()) or "(no terminal)"
        closing = ", ".join(self.closing_refs) or "(unattributed)"
        if self.blocking_nets:
            action = "ripping " + ", ".join(self.blocking_net_names)
        elif self.held_nets:
            action = "nothing rippable (held by non-rippable copper)"
        else:
            action = "nothing rippable (no committed net named)"
        return (
            f"{self.label} sealed in at {self.pass_name}[{self.iteration}]: "
            f"{pads} has no legal exit, closed by {closing} -- {action}"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "failed_net": self.failed_net,
            "failed_net_name": self.failed_net_name,
            "pass": self.pass_name,
            "iteration": self.iteration,
            "stranded_pads": list(self.pad_labels()),
            "closing_refs": list(self.closing_refs),
            "closing_kinds": list(self.closing_kinds),
            "blocking_nets": list(self.blocking_nets),
            "blocking_net_names": list(self.blocking_net_names),
            "held_nets": list(self.held_nets),
        }


@dataclass
class AccessLossTargeter:
    """Names rip-up targets for a net whose own pads have lost their access.

    Construct one per :class:`~kicad_tools.router.core.Autorouter`.  It holds no
    state of its own beyond counters and the witnesses it produced, reads the
    router's live grid, and **never mutates anything** -- every answer is a
    read-only geometric evaluation at the configured clearances.
    """

    router: Any
    enabled: bool = True
    max_evaluations: int = MAX_TARGETING_EVALUATIONS

    queries: int = 0
    """Failed nets the targeter was asked about."""

    hits: int = 0
    """Failed nets that turned out to have a stranded terminal."""

    evaluations: int = 0
    """Access evaluations paid for (cheap existence tests plus full sweeps)."""

    truncated: bool = False
    """True once :attr:`max_evaluations` was hit, so a consumer can say the
    targeting stopped looking rather than that it found nothing."""

    seconds: float = 0.0
    """Wall-clock seconds spent inside :meth:`blockers_for` over the run."""

    witnesses: list[AccessLossWitness] = field(default_factory=list)

    # -- the query -------------------------------------------------------

    def blockers_for(
        self, failed_net: int, rippable_nets: Collection[int]
    ) -> AccessLossWitness | None:
        """Which committed nets seal ``failed_net``'s own terminals in?

        Returns ``None`` when the targeter is disabled, when the net is unknown,
        when every terminal still has a legal first move, or when the evaluation
        budget is exhausted (in which case :attr:`truncated` is set).  A returned
        witness may still carry an empty :attr:`AccessLossWitness.blocking_nets`
        -- the terminal is sealed, but by copper no rip-up here may remove.
        """
        start = time.perf_counter()
        try:
            return self._blockers_for(failed_net, rippable_nets)
        finally:
            self.seconds += time.perf_counter() - start

    def _blockers_for(
        self, failed_net: int, rippable_nets: Collection[int]
    ) -> AccessLossWitness | None:
        if not self.enabled:
            return None
        net = int(failed_net or 0)
        if not net:
            return None
        router = self.router
        rules: DesignRules | None = getattr(router, "rules", None)
        grid = getattr(router, "grid", None)
        if rules is None or grid is None:
            return None
        terminals = self._terminals(net)
        if not terminals:
            return None

        self.queries += 1
        rippable = {int(other) for other in rippable_nets if int(other) != net}

        stranded: list[tuple[str, str]] = []
        refs: set[str] = set()
        kinds: set[str] = set()
        targets: dict[int, str] = {}
        held: set[int] = set()

        for key, pad in terminals:
            width = net_class_trace_width(router, pad)
            if self.evaluations >= self.max_evaluations:
                self.truncated = True
                break
            self.evaluations += 1
            # The hot question is "any way out?", answered with early exit --
            # the same split Phase 2 relies on.  The full sweep runs only for a
            # terminal that has already come back sealed and needs to describe
            # itself.
            if has_access(pad, grid, rules, trace_width=width):
                continue
            if self.evaluations >= self.max_evaluations:
                self.truncated = True
                break
            self.evaluations += 1
            access = compute_access_set(pad, grid, rules, trace_width=width)
            if not access.is_empty():  # pragma: no cover - defensive, see has_access
                continue
            stranded.append(key)
            refs.update(access.closing_refs())
            kinds.update(self._kinds(access.closing_copper))
            pad_targets, pad_held = rippable_closing_nets(
                access, failed_net=net, rippable_nets=rippable
            )
            for target_net, target_name in pad_targets.items():
                targets.setdefault(target_net, target_name)
            held |= pad_held

        if not stranded:
            return None
        held -= set(targets)
        self.hits += 1
        blocking = tuple(sorted(targets))
        witness = AccessLossWitness(
            failed_net=net,
            failed_net_name=self._net_name(net),
            pass_name=self._pass_name(),
            iteration=self._iteration(),
            stranded_pads=tuple(stranded),
            closing_refs=tuple(sorted(refs)),
            closing_kinds=tuple(sorted(kinds)),
            blocking_nets=blocking,
            blocking_net_names=tuple(targets[target_net] for target_net in blocking),
            held_nets=tuple(sorted(held)),
        )
        self.witnesses.append(witness)
        return witness

    # -- reporting -------------------------------------------------------

    def summary_line(self) -> str:
        """One-line human summary for CLI output."""
        if not self.enabled:
            return "Access-loss rip-up targeting: disabled"
        suffix = " (budget truncated)" if self.truncated else ""
        ripped = sum(len(witness.blocking_nets) for witness in self.witnesses)
        return (
            f"Access-loss rip-up targeting: {len(self.witnesses)} net(s) sealed in "
            f"over {self.queries} failed-net query(ies), {ripped} rip-up target(s) "
            f"named, {self.evaluations} access evaluation(s), {self.seconds:.1f}s"
            f"{suffix}"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "queries": self.queries,
            "hits": self.hits,
            "evaluations": self.evaluations,
            "seconds": round(self.seconds, 3),
            "truncated": self.truncated,
            "witnesses": [witness.to_dict() for witness in self.witnesses],
        }

    # -- internals -------------------------------------------------------

    def _terminals(self, net: int) -> list[tuple[tuple[str, str], Pad]]:
        """``(key, pad)`` for every terminal of ``net``, in sorted key order.

        The pad is the one the SEARCH departs from -- a net that went through
        the escape pre-phase departs from its escape terminal
        (``Autorouter._escape_pad_overrides``), and an access set computed at
        the physical pad centre would describe a search nobody runs.  Same rule
        Phase 1b's replay and Phase 2's gate both follow.
        """
        nets = getattr(self.router, "nets", None) or {}
        pads = getattr(self.router, "pads", None) or {}
        overrides = getattr(self.router, "_escape_pad_overrides", None) or {}
        members = nets.get(net)
        if not members:
            return []
        resolved: list[tuple[tuple[str, str], Pad]] = []
        for key in sorted(members):
            pad = overrides.get(key) or pads.get(key)
            if pad is not None:
                resolved.append((key, pad))
        return resolved

    @staticmethod
    def _kinds(items: Iterable[ClosingCopper]) -> set[str]:
        return {str(item.kind) for item in items}

    def _net_name(self, net: int) -> str:
        names = getattr(self.router, "net_names", None) or {}
        return str(names.get(net, "") or "")

    def _pass_name(self) -> str:
        journal = getattr(self.router, "commit_journal", None)
        return str(getattr(journal, "pass_name", "routing"))

    def _iteration(self) -> int:
        journal = getattr(self.router, "commit_journal", None)
        try:
            return int(getattr(journal, "iteration", 0))
        except (TypeError, ValueError):  # pragma: no cover - defensive
            return 0


def format_access_loss_report(
    witnesses: Iterable[AccessLossWitness], *, limit: int = 10
) -> list[str]:
    """Human lines for the access-loss rip-ups, newest last, capped at ``limit``.

    Shared by the CLI summary and the routing log so the two cannot drift --
    the same contract
    :func:`~kicad_tools.router.pad_access_invariant.format_veto_report` has.
    """
    items = list(witnesses)
    head = items[:limit]
    lines = [witness.one_line() for witness in head]
    if len(items) > len(head):
        lines.append(f"... and {len(items) - len(head)} more sealed net(s)")
    return lines
