"""Post-pass pair-skew invariant (Issue #6447, Epic #6441 Phase 5).

A post-routing pass that edits a diff pair's copper can silently break the
pair's skew.  The #7784 via-hairpin collapse put a via on its own SMT land;
``drc_verify_and_nudge``'s via-in-pad sweep then moved it and re-grew a
doubled stub, and board 07's DQS skew went from 0.119 mm to 1.291 mm with
nothing noticing until #7812.  Before this module each pass carried its own
guard (class-pad repair #6437, the match-group tuner #6675) or none at all.

This module is the shared guard.  A caller measures every detected pair's
skew before its pass (:meth:`PairSkewGuard.arm`), runs the pass, and asks
:meth:`PairSkewGuard.regressions` which pairs the pass broke.  A pair has
regressed when it is now off its skew target and either

* it met the target before the pass (``"newly_exceeds"``), or
* it was already off it and the pass made it worse by more than
  :data:`PAIR_SKEW_REGRESSION_TOL_MM` (``"worsened"``; in ps, that length at
  the slowest layer delay).

The caller then reverts that pass's edits to the regressed pair (or rejects
the pass) and logs the :class:`PairSkewRegression`.

The target and the measure are the checker's own (Issue #7893): the target is
:func:`~kicad_tools.router.pair_skew_target.resolve_pair_skew_target` (the
derived ps budget and the error-level mm caps, from the resolver ``kct check``
reads) and each leg is measured with ``Autorouter._checker_route_parts`` (the
kicad-cli-exact model, #6725, itemised per layer so its delay is the
checker's), so the guard never disagrees with the check it protects.  Pairs
with no target (an advisory length limit and no derived budget) are not
guarded.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .lattice.class_pad_repair import PAIR_LENGTH_TOLERANCE_MM

if TYPE_CHECKING:
    from .kicad_length import NetLengthParts
    from .pair_skew_target import PairSkewTarget
    from .primitives import Route

logger = logging.getLogger(__name__)

#: How much worse an already-failing pair may get before the pass is blamed.
#: The same 0.01 mm budget class-pad repair (#6437) holds pair drift to.
PAIR_SKEW_REGRESSION_TOL_MM = PAIR_LENGTH_TOLERANCE_MM

#: Float slack on the limit comparison itself.
_EPS = 1e-6


@dataclass(frozen=True)
class PairSkew:
    """One pair's skew: ``ps`` (``None`` without a budget to weigh by) and ``mm``."""

    ps: float | None
    mm: float


@dataclass(frozen=True)
class GuardedPair:
    """A detected pair with a skew target ``kct check`` judges it against."""

    p_id: int
    n_id: int
    p_name: str
    n_name: str
    target: PairSkewTarget

    @property
    def nets(self) -> tuple[int, int]:
        return (self.p_id, self.n_id)

    @property
    def name(self) -> str:
        return f"{self.p_name}/{self.n_name}"

    @property
    def limit_mm(self) -> float | None:
        """The target's tightest length form (for reports)."""
        return self.target.tolerance_mm


@dataclass(frozen=True)
class PairSkewRegression:
    """One pair a pass pushed off (or further off) its skew target."""

    pair: GuardedPair
    before: PairSkew
    after: PairSkew
    kind: str  # "newly_exceeds" | "worsened"

    @property
    def before_mm(self) -> float:
        return self.before.mm

    @property
    def after_mm(self) -> float:
        return self.after.mm

    def describe(self, pass_name: str) -> str:
        return (
            f"{pass_name}: {self.pair.name} skew {_skew_text(self.before)} -> "
            f"{_skew_text(self.after)} (target {self.pair.target.short()}, {self.kind})"
        )


def _skew_text(skew: PairSkew) -> str:
    if skew.ps is None:
        return f"{skew.mm:.3f} mm"
    return f"{skew.ps:.3f} ps [{skew.mm:.3f} mm]"


def classify_target_change(
    target: PairSkewTarget,
    before: PairSkew | None,
    after: PairSkew | None,
    tol_mm: float = PAIR_SKEW_REGRESSION_TOL_MM,
) -> str | None:
    """``"newly_exceeds"`` / ``"worsened"`` when a pass moved a pair off its target.

    Issue #7893: judged against the whole target (ps budget and error caps).
    "Worse" is more than ``tol_mm`` of extra length skew on a cap the pair is
    over, or more than ``tol_mm`` at the slowest layer delay of extra ps skew.
    """
    if before is None or after is None:
        return None
    if target.within(after.ps, after.mm):
        return None
    if target.within(before.ps, before.mm):
        return "newly_exceeds"
    worse = False
    delays = target.delays
    if target.budget_ps is not None and delays is not None:
        if after.ps is not None and before.ps is not None:
            worse = worse or after.ps > before.ps + tol_mm * delays.slowest
    if target.cap_mm is not None and after.mm > target.cap_mm + _EPS:
        worse = worse or after.mm > before.mm + tol_mm
    return "worsened" if worse else None


def classify_skew_change(
    before_mm: float | None,
    after_mm: float | None,
    limit_mm: float,
    tol_mm: float = PAIR_SKEW_REGRESSION_TOL_MM,
) -> str | None:
    """``"newly_exceeds"`` / ``"worsened"`` when a pass regressed a pair, else ``None``."""
    if before_mm is None or after_mm is None:
        return None
    if after_mm <= limit_mm + _EPS:
        return None
    if before_mm <= limit_mm + _EPS:
        return "newly_exceeds"
    if after_mm > before_mm + tol_mm:
        return "worsened"
    return None


def guarded_pairs(router: Any) -> list[GuardedPair]:
    """Every pair ``kct check`` would fail the board on, as the router knows it.

    Detected the way the post-route skew tuner detects them (#6664): the
    class map's declared partners plus the name-suffix layers.  Never raises
    -- a guard that cannot find pairs simply guards nothing.
    """
    net_names = getattr(router, "net_names", None) or {}
    if not net_names:
        return []
    net_class_map = getattr(router, "net_class_map", None) or {}
    try:
        from .diffpair_detection import build_synth_routing, detect_diff_pairs

        class_routing, net_to_class = build_synth_routing(net_class_map)
        detected = detect_diff_pairs(
            net_names=net_names,
            net_class_routing=class_routing or None,
            net_to_class=net_to_class or None,
        )
    except Exception as exc:  # noqa: BLE001 - a guard must never break routing
        logger.debug("pair-skew guard: pair detection failed: %s", exc)
        return []
    resolve = getattr(router, "_pair_skew_target", None)
    if resolve is None:
        return []
    out: list[GuardedPair] = []
    for dp in detected:
        p, n = dp.pair.positive, dp.pair.negative
        try:
            target = resolve(p.net_name, n.net_name)
        except Exception as exc:  # noqa: BLE001 - a guard must never break routing
            logger.debug("pair-skew guard: no target for %s/%s: %s", p.net_name, n.net_name, exc)
            continue
        if not target.enforced:
            continue
        out.append(GuardedPair(p.net_id, n.net_id, p.net_name, n.net_name, target))
    return out


def checker_length_measure(router: Any) -> Callable[[Route], NetLengthParts] | None:
    """``Route -> NetLengthParts`` measured as ``kct check`` measures, or ``None``.

    Issue #7893: itemised per layer and via, so each leg's delay is the
    checker's; ``.total`` is the kicad-cli length.
    """
    measure_factory = getattr(router, "_checker_route_parts", None)
    if measure_factory is None:
        return None
    layer_stack = getattr(router, "layer_stack", None)
    num_layers = len(layer_stack.layers) if layer_stack is not None else 2
    try:
        measure: Callable[[Route], NetLengthParts] = measure_factory(num_layers)
    except Exception as exc:  # noqa: BLE001 - a guard must never break routing
        logger.debug("pair-skew guard: no checker length model: %s", exc)
        return None
    return measure


def measure_pair_skews(
    pairs: Iterable[GuardedPair],
    routes: Iterable[Route],
    measure: Callable[[Route], NetLengthParts],
) -> dict[tuple[int, int], PairSkew | None]:
    """``{(p_id, n_id): PairSkew}``; ``None`` where a leg is unrouted.

    Issue #7893: each leg's delay is its length per layer at that layer's
    delay plus its vias at the via delay (the pair's target supplies the
    delays), as ``kct check`` measures it.

    A net held as several fragments is measured as one merged route, as
    ``Autorouter._net_routes_merged`` hands it to the tuners (#6792).
    """
    from .primitives import Route as _Route

    pairs = list(pairs)
    wanted = {net for pair in pairs for net in pair.nets}
    fragments: dict[int, list[Route]] = {}
    seen: set[int] = set()
    for route in routes:
        if route.net in wanted and id(route) not in seen:
            seen.add(id(route))
            fragments.setdefault(route.net, []).append(route)

    def length(net: int) -> NetLengthParts | None:
        frags = [r for r in fragments.get(net, ()) if r.segments or r.vias]
        if not frags:
            return None
        merged = (
            frags[0]
            if len(frags) == 1
            else _Route(
                net=net,
                net_name=frags[0].net_name,
                segments=[s for r in frags for s in r.segments],
                vias=[v for r in frags for v in r.vias],
            )
        )
        try:
            return measure(merged)
        except Exception as exc:  # noqa: BLE001 - unmeasurable is "unknown", not a failure
            logger.debug("pair-skew guard: cannot measure net %s: %s", net, exc)
            return None

    out: dict[tuple[int, int], PairSkew | None] = {}
    for pair in pairs:
        lp, ln = length(pair.p_id), length(pair.n_id)
        if lp is None or ln is None:
            out[pair.nets] = None
            continue
        delays = pair.target.delays
        ps = None
        if pair.target.budget_ps is not None and delays is not None:
            ps = abs(
                delays.delay_ps(lp.by_layer, lp.via_mm) - delays.delay_ps(ln.by_layer, ln.via_mm)
            )
        out[pair.nets] = PairSkew(ps, abs(lp.total - ln.total))
    return out


class PairSkewGuard:
    """Before/after pair-skew comparison around one post-routing pass."""

    def __init__(
        self,
        pass_name: str,
        pairs: list[GuardedPair],
        measure: Callable[[Route], NetLengthParts],
        before: dict[tuple[int, int], PairSkew | None],
    ) -> None:
        self.pass_name = pass_name
        self.pairs = pairs
        self.measure = measure
        self.before = before

    @classmethod
    def arm(
        cls,
        router: Any,
        pass_name: str,
        *,
        pairs: list[GuardedPair] | None = None,
        routes: Iterable[Route] | None = None,
    ) -> PairSkewGuard | None:
        """Measure the guarded pairs now; ``None`` when there is nothing to guard."""
        if pairs is None:
            pairs = guarded_pairs(router)
        if not pairs:
            return None
        measure = checker_length_measure(router)
        if measure is None:
            return None
        if routes is None:
            routes = list(getattr(router, "routes", None) or ())
        before = measure_pair_skews(pairs, routes, measure)
        return cls(pass_name, pairs, measure, before)

    def regressions(
        self, routes: Iterable[Route], tol_mm: float = PAIR_SKEW_REGRESSION_TOL_MM
    ) -> list[PairSkewRegression]:
        """The guarded pairs the pass regressed, measured on ``routes``."""
        after = measure_pair_skews(self.pairs, routes, self.measure)
        out: list[PairSkewRegression] = []
        for pair in self.pairs:
            before, now = self.before.get(pair.nets), after.get(pair.nets)
            kind = classify_target_change(pair.target, before, now, tol_mm)
            if kind is not None:
                assert before is not None and now is not None
                out.append(PairSkewRegression(pair, before, now, kind))
        return out

    def report(self, regression: PairSkewRegression, action: str) -> str:
        """Log one regression and the action taken; returns the line."""
        line = f"[pair-skew guard] {regression.describe(self.pass_name)}; {action}"
        logger.warning(line)
        return line
