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
regressed when its skew now exceeds the limit ``kct check`` enforces as an
error and either

* it was inside that limit before the pass (``"newly_exceeds"``), or
* it was already over it and the pass made it worse by more than
  :data:`PAIR_SKEW_REGRESSION_TOL_MM` (``"worsened"``).

The caller then reverts that pass's edits to the regressed pair (or rejects
the pass) and logs the :class:`PairSkewRegression`.

The limit and the measure are the checker's own: the limit comes from
:func:`checker_skew_limit_mm` (``resolve_pair_thresholds``, the single source
shared with ``kct check`` and the coupled router since #7803) and the length
from ``Autorouter._checker_route_length`` (the kicad-cli-exact model, #6725),
so the guard never disagrees with the check it protects.  Pairs whose skew
limit is only advisory (warning/info) are not guarded -- ``kct check`` does
not fail the board on them.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .lattice.class_pad_repair import PAIR_LENGTH_TOLERANCE_MM

if TYPE_CHECKING:
    from .primitives import Route

logger = logging.getLogger(__name__)

#: How much worse an already-failing pair may get before the pass is blamed.
#: The same 0.01 mm budget class-pad repair (#6437) holds pair drift to.
PAIR_SKEW_REGRESSION_TOL_MM = PAIR_LENGTH_TOLERANCE_MM

#: Float slack on the limit comparison itself.
_EPS = 1e-6


def checker_skew_limit_mm(
    net_class_map: dict[str, Any] | None, p_name: str, n_name: str
) -> float | None:
    """The pair's ``kct check`` skew limit when it is an ERROR (#5333).

    ``None`` when the limit is only advisory (warning/info).  The one lookup
    behind ``DiffPairRouter._checker_skew_limit_mm`` and this guard.
    """
    from kicad_tools.validate.diffpair_engagement import resolve_pair_thresholds

    thresholds = resolve_pair_thresholds(net_class_map, p_name, n_name)
    if thresholds.skew_severity != "error":
        return None
    return float(thresholds.skew_mm)


@dataclass(frozen=True)
class GuardedPair:
    """A detected pair whose skew ``kct check`` enforces as an error."""

    p_id: int
    n_id: int
    p_name: str
    n_name: str
    limit_mm: float

    @property
    def nets(self) -> tuple[int, int]:
        return (self.p_id, self.n_id)

    @property
    def name(self) -> str:
        return f"{self.p_name}/{self.n_name}"


@dataclass(frozen=True)
class PairSkewRegression:
    """One pair a pass pushed past (or further past) its skew limit."""

    pair: GuardedPair
    before_mm: float
    after_mm: float
    kind: str  # "newly_exceeds" | "worsened"

    def describe(self, pass_name: str) -> str:
        return (
            f"{pass_name}: {self.pair.name} skew {self.before_mm:.3f} -> "
            f"{self.after_mm:.3f} mm (limit {self.pair.limit_mm:.3f} mm, {self.kind})"
        )


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
    out: list[GuardedPair] = []
    for dp in detected:
        p, n = dp.pair.positive, dp.pair.negative
        limit = checker_skew_limit_mm(net_class_map, p.net_name, n.net_name)
        if limit is None:
            continue
        out.append(GuardedPair(p.net_id, n.net_id, p.net_name, n.net_name, limit))
    return out


def checker_length_measure(router: Any) -> Callable[[Route], float] | None:
    """``Route -> mm`` measured as ``kct check`` measures, or ``None``."""
    measure_factory = getattr(router, "_checker_route_length", None)
    if measure_factory is None:
        return None
    layer_stack = getattr(router, "layer_stack", None)
    num_layers = len(layer_stack.layers) if layer_stack is not None else 2
    try:
        measure: Callable[[Route], float] = measure_factory(num_layers)
    except Exception as exc:  # noqa: BLE001 - a guard must never break routing
        logger.debug("pair-skew guard: no checker length model: %s", exc)
        return None
    return measure


def measure_pair_skews(
    pairs: Iterable[GuardedPair],
    routes: Iterable[Route],
    measure: Callable[[Route], float],
) -> dict[tuple[int, int], float | None]:
    """``{(p_id, n_id): |len(P) - len(N)|}``; ``None`` where a leg is unrouted.

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

    def length(net: int) -> float | None:
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
            return float(measure(merged))
        except Exception as exc:  # noqa: BLE001 - unmeasurable is "unknown", not a failure
            logger.debug("pair-skew guard: cannot measure net %s: %s", net, exc)
            return None

    out: dict[tuple[int, int], float | None] = {}
    for pair in pairs:
        lp, ln = length(pair.p_id), length(pair.n_id)
        out[pair.nets] = None if lp is None or ln is None else abs(lp - ln)
    return out


class PairSkewGuard:
    """Before/after pair-skew comparison around one post-routing pass."""

    def __init__(
        self,
        pass_name: str,
        pairs: list[GuardedPair],
        measure: Callable[[Route], float],
        before: dict[tuple[int, int], float | None],
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
            before_mm, after_mm = self.before.get(pair.nets), after.get(pair.nets)
            kind = classify_skew_change(before_mm, after_mm, pair.limit_mm, tol_mm)
            if kind is not None:
                assert before_mm is not None and after_mm is not None
                out.append(PairSkewRegression(pair, before_mm, after_mm, kind))
        return out

    def report(self, regression: PairSkewRegression, action: str) -> str:
        """Log one regression and the action taken; returns the line."""
        line = f"[pair-skew guard] {regression.describe(self.pass_name)}; {action}"
        logger.warning(line)
        return line
