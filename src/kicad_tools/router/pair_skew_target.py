"""The skew target the router and every skew tuner aim for (Issue #7893).

Step 2 of the operator ruling on #7883 (issuecomment-6370363844).  Step 1
(#7892) made ``kct check`` judge intra-pair skew in **time**: each leg's
delay is its length on every copper layer at that layer's propagation delay,
plus its via barrels at the via delay, and the pair's skew in ps is checked
against the derived budget of
:func:`kicad_tools.physics.skew_budget.resolve_skew_budget`.  This module is
the router's side of the same rule, so the router tunes to exactly what the
checker enforces:

* :func:`resolve_pair_skew_target` is the **one** skew-limit lookup of the
  router and its tuners (the inline comb, the coupled-candidate selection,
  the end-of-recipe and lattice pair tuners, ``kct retune-lengths``, the
  post-pass pair-skew guard and the match-group tuner's pair invariant).  It
  reads :func:`~kicad_tools.validate.diffpair_engagement.resolve_pair_skew_limits`,
  the function ``kct check`` itself reads, which closes the #7797 drift risk:
  there is no second resolution that could disagree.
* :class:`PairSkewTarget` holds the budget in ps (any severity: a warning
  budget is still a ``kct check`` finding) and any **error-level**
  millimetre cap ``kct check`` keeps beside it -- a class's own explicit
  ``skew_tolerance_mm`` and, only for a pair whose protocol is completely
  unknown (no ``pair_protocol`` and no class- or net-name match), the generic
  0.5 mm (Issue #7894 ruling; the board generators no longer set class caps).
  A pair meets its target only when it meets the budget **and** every active
  cap, so the router never stops at a skew ``kct check`` fails.  When the ``.kicad_dru``
  length rule kicad-cli enforces (the budget at the slowest layer delay) is an
  error and tighter than the caps, it is a cap too, so kicad-cli does not
  fail what the router tuned either.
* :class:`PairSkewMeter` measures a route the checker's way, in mm and in ps.

A cap whose severity is only a warning, with no budget beside it (a protocol
the phase rule has no operating point for), is not a target: the router did
not tune those before #7893 either.
"""

from __future__ import annotations

import dataclasses
import logging
import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from kicad_tools.physics.skew_budget import SKEW_BUDGET_EPSILON_PS, LayerDelays

if TYPE_CHECKING:
    from kicad_tools.physics.stackup import Stackup

    from .kicad_length import NetLengthParts
    from .primitives import Route

logger = logging.getLogger(__name__)

#: Slack on a millimetre cap, the checker's ``SKEW_TOLERANCE_EPSILON_MM``.
CAP_EPSILON_MM = 1e-6


@dataclass(frozen=True)
class PairSkewTarget:
    """A pair's skew target: a budget in ps and/or an error-level mm cap.

    ``budget_ps`` is ``None`` when the pair's protocol and rate give nothing
    to derive from, or no stackup supplied the per-layer delays.
    ``cap_mm`` is the error-level length cap (``None`` when there is none).
    """

    p_name: str
    n_name: str
    budget_ps: float | None = None
    budget_severity: str | None = None
    budget_provenance: str = ""
    cap_mm: float | None = None
    cap_source: str = ""
    delays: LayerDelays | None = None
    #: A length limit ``kct check`` only warns on, with no budget beside it.
    #: Not a target; only the explicit ``--length-match-diffpairs`` opt-in
    #: (:meth:`opt_in`) tunes to it.
    warning_cap_mm: float | None = None

    def opt_in(self) -> PairSkewTarget:
        """This target with its warning-only cap promoted, for an explicit opt-in."""
        if self.enforced or self.warning_cap_mm is None:
            return self
        return dataclasses.replace(
            self, cap_mm=self.warning_cap_mm, cap_source="warning-only cap (opt-in)"
        )

    @property
    def enforced(self) -> bool:
        """Whether there is anything to tune to."""
        return self.budget_ps is not None or self.cap_mm is not None

    @property
    def budget_mm_equivalent(self) -> float | None:
        """The budget at the slowest layer delay (mm), its conservative length form."""
        if self.budget_ps is None or self.delays is None:
            return None
        return float(self.delays.mm_equivalent(self.budget_ps))

    @property
    def tolerance_mm(self) -> float | None:
        """The tightest length form of the target: ``min(cap, budget at slowest layer)``.

        For the consumers that only reason in millimetres (escape corridor
        slack, the lattice fallback ranking, the match-group pair invariant,
        tuning-pattern metadata).  Meeting it in mm meets the budget whenever
        both legs share their layer mix.
        """
        values = [v for v in (self.cap_mm, self.budget_mm_equivalent) if v is not None]
        return min(values) if values else None

    def within(self, skew_ps: float | None, skew_mm: float) -> bool:
        """Whether a pair at ``skew_ps`` / ``skew_mm`` meets every part of the target."""
        if self.budget_ps is not None and skew_ps is not None:
            if skew_ps > self.budget_ps + SKEW_BUDGET_EPSILON_PS:
                return False
        if self.cap_mm is not None and skew_mm > self.cap_mm + CAP_EPSILON_MM:
            return False
        return True

    def describe(self) -> str:
        """``"4.03 ps [derived ...; warning] + cap 0.500 mm [class:USB3]"``."""
        parts: list[str] = []
        if self.budget_ps is not None:
            parts.append(
                f"{self.budget_ps:.2f} ps [{self.budget_provenance}; {self.budget_severity}]"
            )
        if self.cap_mm is not None:
            parts.append(f"cap {self.cap_mm:.3f} mm [{self.cap_source}]")
        return " + ".join(parts) if parts else "no enforced skew limit"

    def short(self) -> str:
        """``"4.03 ps"`` / ``"4.03 ps, cap 0.050 mm"`` / ``"0.500 mm"``."""
        parts: list[str] = []
        if self.budget_ps is not None:
            parts.append(f"{self.budget_ps:.2f} ps")
        if self.cap_mm is not None:
            parts.append(f"cap {self.cap_mm:.3f} mm" if parts else f"{self.cap_mm:.3f} mm")
        return ", ".join(parts) if parts else "n/a"


def resolve_pair_skew_target(
    net_class_map: dict[str, Any] | None,
    p_name: str,
    n_name: str,
    stackup: Stackup | None,
) -> PairSkewTarget:
    """The pair's skew target -- the router's single skew-limit lookup.

    ``stackup`` (a :class:`~kicad_tools.physics.stackup.Stackup`) supplies
    the per-layer delays at the class trace width; use :func:`skew_stackup`
    for the stackup ``kct check`` will read off the written board.
    """
    from kicad_tools.validate.diffpair_engagement import resolve_pair_skew_limits

    limits = resolve_pair_skew_limits(net_class_map, p_name, n_name, stackup)
    budget = limits.budget if limits.delays is not None else None
    cap = warning_cap = None
    cap_source = ""
    if limits.length_cap_mm is not None and limits.length_severity == "error":
        cap, cap_source = float(limits.length_cap_mm), limits.length_source
    elif limits.length_cap_mm is not None and budget is None:
        warning_cap = float(limits.length_cap_mm)
    if (
        budget is not None
        and limits.kicad_skew_severity == "error"
        and (cap is None or limits.kicad_skew_mm < cap - CAP_EPSILON_MM)
    ):
        # The ``.kicad_dru`` length rule kicad-cli fails as an error (the
        # budget at the slowest layer delay) is tighter than any cap: hold the
        # pair to it too, so neither engine fails what the router tunes.
        cap, cap_source = float(limits.kicad_skew_mm), "kicad_dru: budget at slowest layer"
    return PairSkewTarget(
        p_name=p_name,
        n_name=n_name,
        budget_ps=float(budget.budget_ps) if budget is not None else None,
        budget_severity=budget.severity if budget is not None else None,
        budget_provenance=budget.provenance if budget is not None else "",
        cap_mm=cap,
        cap_source=cap_source,
        delays=limits.delays,
        warning_cap_mm=warning_cap,
    )


def skew_stackup(source_pcb: Any | None, num_layers: int) -> Stackup | None:
    """The stackup ``kct check`` will read delays from on the routed board.

    ``source_pcb`` is the board the router writes into (a path or a loaded
    :class:`~kicad_tools.schema.pcb.PCB`); the routed file keeps its setup,
    so its stackup is that board's
    (:func:`kicad_tools.validate.diffpair_skew.board_stackup`, the checker's
    own resolution).  Without a board, the checker's default for the layer
    count.
    """
    from kicad_tools.validate.diffpair_skew import board_stackup, default_board_stackup

    pcb = source_pcb
    if pcb is not None and not hasattr(pcb, "segments_in_net"):
        try:
            from kicad_tools.schema.pcb import PCB

            pcb = PCB.load(str(pcb))
        except Exception as exc:  # noqa: BLE001 - fall back to the default stackup
            logger.debug("skew target: cannot load %s (%s); default stackup", source_pcb, exc)
            pcb = None
    if pcb is not None:
        stackup: Stackup | None = board_stackup(pcb, num_layers)
        if stackup is not None and len(stackup.copper_layers) == num_layers:
            return stackup
    fallback: Stackup | None = default_board_stackup(num_layers)
    return fallback


def _layer_name(layer: Any) -> str:
    return getattr(layer, "kicad_name", None) or str(layer)


def segment_length_parts(route: Route) -> NetLengthParts:
    """A route's plain segment lengths per layer, no trims and no vias.

    The ``--length-match-diffpairs`` path's segment-only measure, itemised so
    it can be weighted by layer delay like the checker's.
    """
    from .kicad_length import NetLengthParts

    return NetLengthParts(
        tuple((_layer_name(s.layer), math.hypot(s.x2 - s.x1, s.y2 - s.y1)) for s in route.segments),
        (),
    )


@dataclass(frozen=True)
class PairSkewMeter:
    """Measures routes against a :class:`PairSkewTarget`, in mm and in ps.

    ``parts`` is the itemised length of a route (``KicadLengthModel.route_length_parts``
    for the checker-exact measure); ``length`` optionally overrides the mm
    figure (a quantized measure).
    """

    target: PairSkewTarget
    parts: Callable[[Route], NetLengthParts]
    length: Callable[[Route], float] | None = None

    @property
    def in_time(self) -> bool:
        """Whether the target has a ps budget to measure against."""
        return self.target.budget_ps is not None and self.target.delays is not None

    def length_mm(self, route: Route) -> float:
        return self.length(route) if self.length is not None else self.parts(route).total

    def delay_ps(self, route: Route) -> float | None:
        delays = self.target.delays
        if delays is None:
            return None
        parts = self.parts(route)
        return float(delays.delay_ps(parts.by_layer, parts.via_mm))

    def layer_delay(self, layer: Any) -> float:
        """Delay (ps/mm) of a router layer; 1.0 without delays."""
        delays = self.target.delays
        return float(delays.of(_layer_name(layer))) if delays is not None else 1.0

    def skew(self, a: Route, b: Route) -> tuple[float | None, float]:
        """``(skew ps or None, skew mm)`` of a pair."""
        ta, tb = self.delay_ps(a), self.delay_ps(b)
        skew_ps = abs(ta - tb) if ta is not None and tb is not None else None
        return skew_ps, abs(self.length_mm(a) - self.length_mm(b))

    def within(self, a: Route, b: Route) -> bool:
        skew_ps, skew_mm = self.skew(a, b)
        return self.target.within(skew_ps, skew_mm)


__all__ = [
    "CAP_EPSILON_MM",
    "PairSkewMeter",
    "PairSkewTarget",
    "resolve_pair_skew_target",
    "segment_length_parts",
    "skew_stackup",
]
