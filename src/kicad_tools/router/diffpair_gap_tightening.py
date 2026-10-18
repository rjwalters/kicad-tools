"""Post-route diff-pair gap tightening (Issue #7344).

When a differential pair is routed as two single-ended nets (no coupled
router engaged), nothing pulls the second rail toward the first: the rails
keep the pitch at which they left their pads.  Board 07's MIPI_DAT1 trunk
runs 15 mm at a 0.283 mm edge gap (116 ohm) where the class asks for 100 ohm
+/-10 %, which ``ImpedanceRule`` now judges on coupled segments (#7306).

This module is a geometry-only post-pass that squeezes such a trunk:

1. For each detected pair whose net class declares ``target_diff_impedance``,
   find parallel same-layer P/N segment runs whose edge gap is above the
   tolerance band (the coupled model's Zdiff is too high) but inside a
   candidate window.  An in-band run is never touched.
2. Solve the target gap ``g*`` with the coupled model ``ImpedanceRule``
   itself uses (edge-coupled microstrip on outer layers, stripline on inner
   ones), floored by the pair's own floored gap
   (:meth:`Autorouter._pair_gap_mm`).
3. Move the interior of both rails toward each other by half the excess
   each, joining the untouched ends with 45-degree jogs.  Both rails gain
   the same jog length, so the pair's skew is unchanged.  When the
   symmetric move is illegal a single-rail move is tried, then a short
   ladder of smaller (still in-band) squeezes.
4. Every candidate is gated on exact geometry before it is committed:
   foreign pad / via / copper clearance
   (:meth:`Autorouter._skew_tune_foreign_clearance_ok`), pad connectivity
   of both nets, skew (<= ``SKEW_TOLERANCE_MM`` change), and the
   ``ImpedanceRule`` post-condition (the squeezed trunk is in band and no
   segment becomes a newly judged out-of-band one).  A run that fails all
   candidates is left byte-identical and reported as ``declined:<reason>``.

The pass is opt-in (``kct route --tighten-diffpair-gap`` /
:meth:`Autorouter.apply_diffpair_gap_tightening`): the grid engine is frozen
for auto-engaged pair features until #6448.
"""

from __future__ import annotations

import dataclasses
import math
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .primitives import Route, Segment

if TYPE_CHECKING:
    from kicad_tools.physics import Stackup

    from .core import Autorouter

#: Edge gaps (mm) above this are not considered for tightening.  Wider than
#: ``ImpedanceRule``'s 0.5 mm coupling window so a pair that left its pads a
#: little too far apart to be judged at all can still be coupled; the
#: post-condition guarantees such a run is only committed in band.
DEFAULT_CANDIDATE_WINDOW_MM = 1.0

#: Minimum longitudinal overlap (mm) for a parallel P/N run to be squeezed.
#: Matches ``ImpedanceRule.NECK_DOWN_EXEMPT_LENGTH_MM``: shorter runs are not
#: judged.
MIN_RUN_MM = 1.0

#: Shortest squeezed trunk (mm) left between the two jogs.
MIN_TRUNK_MM = 0.2

#: A committed squeeze may change the pair's skew by at most this much (mm).
SKEW_TOLERANCE_MM = 0.01

#: Upper bound on runs attempted per pair (defensive loop guard).
MAX_RUNS_PER_PAIR = 32

_EPS = 1e-6
_DIR_EPS = 1e-4


def _r4(value: float) -> float:
    """Round to the 4-decimal precision the board writer emits."""
    return round(value, 4) + 0.0


@dataclass
class RunOutcome:
    """What happened to one parallel P/N run."""

    layer: str
    status: str
    gap_before_mm: float
    gap_after_mm: float
    zdiff_before: float | None
    zdiff_after: float | None
    mode: str | None = None


@dataclass
class GapTighteningResult:
    """Per-pair result of :func:`tighten_diffpair_gaps`.

    ``reason`` is ``"tightened"`` (at least one run committed),
    ``"in_band"`` (nothing to do: no run is above the band), or
    ``"declined:<why>"``.
    """

    p_net_name: str
    n_net_name: str
    reason: str
    target_gap_mm: float | None = None
    band_max_gap_mm: float | None = None
    skew_before_mm: float = 0.0
    skew_after_mm: float = 0.0
    runs: list[RunOutcome] = field(default_factory=list)

    @property
    def runs_tightened(self) -> int:
        return sum(1 for r in self.runs if r.status == "tightened")

    def format_line(self) -> str:
        line = f"  {self.p_net_name}/{self.n_net_name}: {self.reason}"
        if self.runs:
            worst = max(self.runs, key=lambda r: r.gap_before_mm)
            z_b = f"{worst.zdiff_before:.1f}" if worst.zdiff_before is not None else "?"
            z_a = f"{worst.zdiff_after:.1f}" if worst.zdiff_after is not None else "?"
            line += (
                f" ({self.runs_tightened}/{len(self.runs)} runs, gap "
                f"{worst.gap_before_mm:.3f}->{worst.gap_after_mm:.3f}mm, "
                f"Zdiff {z_b}->{z_a} ohm"
            )
            if self.target_gap_mm is not None:
                line += f", target gap {self.target_gap_mm:.3f}mm"
            line += ")"
        return line


# ---------------------------------------------------------------------------
# Impedance model (shared with ImpedanceRule)
# ---------------------------------------------------------------------------


class _PairImpedance:
    """``ImpedanceRule`` configured for one pair, used as the oracle.

    The rule's own gap annotation and coupled-lines evaluation are reused so
    the router and ``kct check`` agree by construction.
    """

    def __init__(self, stackup: Stackup, pair: Any, target: float, tolerance: float) -> None:
        from kicad_tools.validate.rules.impedance import ImpedanceRule, NetImpedanceSpec

        self.p_name = pair.positive.net_name
        self.n_name = pair.negative.net_name
        pattern = f"^(?:{re.escape(self.p_name)}|{re.escape(self.n_name)})$"
        self.spec = NetImpedanceSpec(
            net_pattern=pattern, target_zdiff=float(target), tolerance_percent=float(tolerance)
        )
        self.rule = ImpedanceRule(specs=[self.spec], stackup=stackup, detected_pairs=[pair])
        if not self.rule._init_physics(None):  # type: ignore[arg-type]
            raise RuntimeError("physics module unavailable")
        self.stackup = stackup
        self.target = float(target)
        self.tolerance = float(tolerance)

    def zdiff(self, layer: str, width: float, gap: float) -> float | None:
        cl = self.rule._cl
        if cl is None:
            return None
        try:
            if self.stackup.is_outer_layer(layer):
                return float(cl.edge_coupled_microstrip(width, gap, layer).zdiff)
            return float(cl.edge_coupled_stripline(width, gap, layer).zdiff)
        except (ValueError, AttributeError, ZeroDivisionError):
            return None

    def gap_for(self, layer: str, width: float, zdiff: float) -> float | None:
        """Edge gap at which the coupled model gives ``zdiff`` (bisection)."""
        lo, hi = 0.005, 5.0
        z_lo = self.zdiff(layer, width, lo)
        z_hi = self.zdiff(layer, width, hi)
        if z_lo is None or z_hi is None:
            return None
        if zdiff <= z_lo:
            return lo
        if zdiff >= z_hi:
            return None
        for _ in range(60):
            mid = (lo + hi) / 2
            z = self.zdiff(layer, width, mid)
            if z is None:
                return None
            if z < zdiff:
                lo = mid
            else:
                hi = mid
        return lo

    def judge(self, segments: dict[str, list[Segment]]) -> dict[int, tuple[bool, float, float]]:
        """``{id(segment): (compliant, deviation_percent, zdiff)}`` for judged segments."""
        traces: dict[str, list[dict]] = {}
        owners: list[tuple[Segment, dict]] = []
        for name, segs in segments.items():
            bucket = traces.setdefault(name, [])
            for seg in segs:
                trace = {
                    "net_name": name,
                    "width_mm": seg.width,
                    "layer": seg.layer.kicad_name,
                    "start": (seg.x1, seg.y1),
                    "end": (seg.x2, seg.y2),
                }
                bucket.append(trace)
                owners.append((seg, trace))
        self.rule._annotate_intra_pair_clearance(traces)
        out: dict[int, tuple[bool, float, float]] = {}
        exempt = self.rule.NECK_DOWN_EXEMPT_LENGTH_MM
        for seg, trace in owners:
            if self.rule._segment_length_mm(trace) < exempt:
                continue
            res = self.rule._check_trace_impedance(trace, self.spec)
            if res is None:
                continue
            out[id(seg)] = (res.compliant, res.deviation_percent, res.calculated_z0)
        return out


def resolve_stackup(router: Autorouter) -> Stackup | None:
    """The stackup ``kct check`` will judge the routed board with.

    The source board's own (``Stackup.from_pcb``, which falls back to the
    layer-count default when the board declares none), else the router's
    stackup, else the layer-count default ``Stackup.from_pcb`` would pick.
    """
    from kicad_tools.physics import Stackup

    source = getattr(router, "_impedance_source_pcb_path", None)
    if source:
        try:
            from kicad_tools.schema.pcb import PCB

            return Stackup.from_pcb(PCB.load(source))
        except Exception:  # noqa: BLE001 - fall through to the defaults
            pass
    own: Stackup | None = getattr(router, "_stackup", None)
    if own is not None:
        return own
    layer_stack = getattr(router, "layer_stack", None)
    num_layers = len(layer_stack.layers) if layer_stack is not None else 2
    if num_layers <= 2:
        return Stackup.default_2layer()
    if num_layers == 4:
        return Stackup.jlcpcb_4layer()
    if num_layers == 6:
        return Stackup.default_6layer()
    return Stackup._create_generic_stackup(num_layers)


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------


def _step_vector(seg: Segment) -> tuple[int, int] | None:
    """Integer 8-direction step of ``seg`` (start -> end), or None if off-angle."""
    dx, dy = seg.x2 - seg.x1, seg.y2 - seg.y1
    ax, ay = abs(dx), abs(dy)
    if ax < _DIR_EPS and ay < _DIR_EPS:
        return None
    if ay < _DIR_EPS:
        return (1 if dx > 0 else -1, 0)
    if ax < _DIR_EPS:
        return (0, 1 if dy > 0 else -1)
    if abs(ax - ay) < _DIR_EPS * 10:
        return (1 if dx > 0 else -1, 1 if dy > 0 else -1)
    return None


@dataclass
class _Run:
    """A parallel P/N run: both segments, their overlap ``[t0, t1]`` and gap."""

    p_seg: Segment
    n_seg: Segment
    layer: str
    origin: tuple[float, float]
    u: tuple[float, float]
    t0: float
    t1: float
    centre_mm: float
    gap_mm: float
    width_mm: float

    @property
    def key(self) -> tuple:
        return (
            self.layer,
            _r4(self.p_seg.x1),
            _r4(self.p_seg.y1),
            _r4(self.p_seg.x2),
            _r4(self.p_seg.y2),
            _r4(self.n_seg.x1),
            _r4(self.n_seg.y1),
            _r4(self.n_seg.x2),
            _r4(self.n_seg.y2),
        )


def _find_runs(
    p_segs: list[Segment], n_segs: list[Segment], *, min_run_mm: float, max_gap_mm: float
) -> list[_Run]:
    """Parallel, longitudinally overlapping same-layer P/N segment runs."""
    runs: list[_Run] = []
    for sp in p_segs:
        if _step_vector(sp) is None:
            continue
        lp = math.hypot(sp.x2 - sp.x1, sp.y2 - sp.y1)
        if lp < min_run_mm:
            continue
        ux, uy = (sp.x2 - sp.x1) / lp, (sp.y2 - sp.y1) / lp
        for sn in n_segs:
            if sn.layer != sp.layer or _step_vector(sn) is None:
                continue
            ln = math.hypot(sn.x2 - sn.x1, sn.y2 - sn.y1)
            if ln < min_run_mm:
                continue
            vx, vy = (sn.x2 - sn.x1) / ln, (sn.y2 - sn.y1) / ln
            if abs(ux * vy - uy * vx) > 1e-3:
                continue
            tc = (sn.x1 - sp.x1) * ux + (sn.y1 - sp.y1) * uy
            td = (sn.x2 - sp.x1) * ux + (sn.y2 - sp.y1) * uy
            t0, t1 = max(0.0, min(tc, td)), min(lp, max(tc, td))
            if t1 - t0 < min_run_mm:
                continue
            centre = abs((sn.x1 - sp.x1) * uy - (sn.y1 - sp.y1) * ux)
            gap = centre - sp.width / 2 - sn.width / 2
            if gap <= 0 or gap > max_gap_mm:
                continue
            runs.append(
                _Run(
                    p_seg=sp,
                    n_seg=sn,
                    layer=sp.layer.kicad_name,
                    origin=(sp.x1, sp.y1),
                    u=(ux, uy),
                    t0=t0,
                    t1=t1,
                    centre_mm=centre,
                    gap_mm=gap,
                    width_mm=(sp.width + sn.width) / 2,
                )
            )
    runs.sort(key=lambda r: (-(r.t1 - r.t0), r.key))
    return runs


def _rail_chain(
    seg: Segment, run: _Run, toward: tuple[float, float], shift_mm: float
) -> tuple[list[Segment], list[Segment]] | None:
    """Replace ``seg`` by a chain that runs ``shift_mm`` closer to its partner.

    The part of ``seg`` inside the run's overlap ``[t0, t1]`` is translated
    by ``shift_mm`` along ``toward`` (perpendicular to the run), joined to
    the untouched remainder by 45-degree jogs that sit inside the overlap.
    Returns ``(chain, moved)`` -- the full replacement in ``seg``'s own
    orientation and the subset of new (jog / trunk) segments -- or None when
    the overlap is too short for the jogs.
    """
    step = _step_vector(seg)
    if step is None or shift_mm <= 0:
        return None
    ex, ey = step
    diagonal = ex != 0 and ey != 0
    e_len = math.sqrt(2.0) if diagonal else 1.0
    # Perpendicular step toward the partner (integer components).
    px, py = -ey, ex
    if px * toward[0] + py * toward[1] < 0:
        px, py = -px, -py
    if diagonal:
        k = _r4(shift_mm * math.sqrt(2.0))
        j_in = (k * (ex + px) / 2, k * (ey + py) / 2)
        j_out = (k * (ex - px) / 2, k * (ey - py) / 2)
        advance_q = k / 2
    else:
        k = _r4(shift_mm)
        j_in = (k * (ex + px), k * (ey + py))
        j_out = (k * (ex - px), k * (ey - py))
        advance_q = k
    if k <= 0:
        return None
    q_total = max(abs(seg.x2 - seg.x1), abs(seg.y2 - seg.y1))
    # Run frame -> this segment's own step parameter q (P0 + q * e).
    ox, oy = run.origin
    ux, uy = run.u
    t_p0 = (seg.x1 - ox) * ux + (seg.y1 - oy) * uy
    dot = (ex * ux + ey * uy) / e_len  # +/-1 (parallel)
    if abs(abs(dot) - 1.0) > 1e-3:
        return None
    sign = 1.0 if dot > 0 else -1.0
    qa, qb = sorted(((run.t0 - t_p0) * sign / e_len, (run.t1 - t_p0) * sign / e_len))
    qa = min(max(_r4(qa), 0.0), q_total)
    qb = min(max(_r4(qb), 0.0), q_total)
    trunk_q = qb - qa - 2 * advance_q
    if trunk_q * e_len < MIN_TRUNK_MM:
        return None

    def at(q: float) -> tuple[float, float]:
        if q <= _EPS:
            return (seg.x1, seg.y1)
        if q >= q_total - _EPS:
            return (seg.x2, seg.y2)
        return (_r4(seg.x1 + q * ex), _r4(seg.y1 + q * ey))

    s1 = at(qa)
    s2 = at(qb)
    k1 = (_r4(s1[0] + j_in[0]), _r4(s1[1] + j_in[1]))
    k2 = (_r4(s2[0] - j_out[0]), _r4(s2[1] - j_out[1]))

    def mk(a: tuple[float, float], b: tuple[float, float]) -> Segment:
        return dataclasses.replace(seg, x1=a[0], y1=a[1], x2=b[0], y2=b[1])

    moved = [mk(s1, k1), mk(k1, k2), mk(k2, s2)]
    chain: list[Segment] = []
    if qa > _EPS:
        chain.append(mk((seg.x1, seg.y1), s1))
    chain.extend(moved)
    if qb < q_total - _EPS:
        chain.append(mk(s2, (seg.x2, seg.y2)))
    return chain, moved


def _net_length(segments: list[Segment]) -> float:
    return sum(math.hypot(s.x2 - s.x1, s.y2 - s.y1) for s in segments)


def _covered_by(seg: Segment, old: Segment) -> bool:
    """True when ``seg`` lies on ``old``'s centreline, within its extent."""
    if seg.layer != old.layer:
        return False
    lo = math.hypot(old.x2 - old.x1, old.y2 - old.y1)
    if lo < _EPS:
        return False
    ux, uy = (old.x2 - old.x1) / lo, (old.y2 - old.y1) / lo
    for x, y in ((seg.x1, seg.y1), (seg.x2, seg.y2)):
        t = (x - old.x1) * ux + (y - old.y1) * uy
        d = abs((x - old.x1) * uy - (y - old.y1) * ux)
        if d > 1e-3 or t < -1e-3 or t > lo + 1e-3:
            return False
    return True


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


@dataclass
class _PairState:
    """Mutable working copy of one pair's fragments."""

    p_id: int
    n_id: int
    fragments: list[Route]  # current self.routes (working list)


def _pair_segments(routes: list[Route], net: int, *, editable: bool) -> list[Segment]:
    return [
        s for r in routes if r.net == net and (not editable or not r.is_escape) for s in r.segments
    ]


def _replace_segment(routes: list[Route], old: Segment, chain: list[Segment]) -> list[Route] | None:
    """Copy of ``routes`` with ``old`` replaced by ``chain`` in its fragment."""
    out = list(routes)
    for i, route in enumerate(out):
        if route.is_escape:
            continue
        for j, seg in enumerate(route.segments):
            if seg is old:
                segs = route.segments[:j] + chain + route.segments[j + 1 :]
                out[i] = dataclasses.replace(route, segments=segs)
                return out
    return None


def tighten_pair(
    router: Autorouter,
    dp: Any,
    net_class: Any,
    stackup: Stackup,
    *,
    candidate_window_mm: float = DEFAULT_CANDIDATE_WINDOW_MM,
) -> GapTighteningResult:
    """Squeeze every out-of-band parallel run of one pair (see module docs)."""
    from .connectivity_invariant import build_multi_pad_net_pads
    from .observability import validate_net_connectivity

    p_id, n_id = dp.pair.positive.net_id, dp.pair.negative.net_id
    p_name, n_name = dp.pair.positive.net_name, dp.pair.negative.net_name
    result = GapTighteningResult(p_net_name=p_name, n_net_name=n_name, reason="in_band")

    target = getattr(net_class, "target_diff_impedance", None) if net_class else None
    if target is None:
        result.reason = "declined:no_target_diff_impedance"
        return result
    tolerance = float(getattr(net_class, "impedance_tolerance_percent", 10.0) or 10.0)
    try:
        model = _PairImpedance(stackup, dp.pair, float(target), tolerance)
    except Exception:  # noqa: BLE001 - no physics -> nothing to do
        result.reason = "declined:no_impedance_model"
        return result
    z_max = model.target * (1 + tolerance / 100.0)
    floor_gap = float(router._pair_gap_mm(net_class))

    def skew_of(routes: list[Route]) -> float:
        return _net_length(_pair_segments(routes, p_id, editable=False)) - _net_length(
            _pair_segments(routes, n_id, editable=False)
        )

    def judge(routes: list[Route]) -> dict[int, tuple[bool, float, float]]:
        return model.judge(
            {
                p_name: _pair_segments(routes, p_id, editable=False),
                n_name: _pair_segments(routes, n_id, editable=False),
            }
        )

    net_pads = build_multi_pad_net_pads(router, {p_id, n_id})

    def connected(routes: list[Route]) -> dict[int, int]:
        pair_routes = [r for r in routes if r.net in (p_id, n_id)]
        info = validate_net_connectivity(pair_routes, net_pads)
        return {net: int(v.get("connected_pads", 0)) for net, v in info.items()}

    foreign_preserved = []
    try:
        foreign_preserved = router._preserved_fragments({p_id, n_id}, invert=True)
    except Exception:  # noqa: BLE001 - partial routers in tests
        foreign_preserved = []

    routes = list(router.routes)
    result.skew_before_mm = abs(skew_of(routes))
    attempted: set[tuple] = set()
    first_decline: str | None = None
    target_gaps: list[float] = []
    band_gaps: list[float] = []

    for _ in range(MAX_RUNS_PER_PAIR):
        runs = [
            r
            for r in _find_runs(
                _pair_segments(routes, p_id, editable=True),
                _pair_segments(routes, n_id, editable=True),
                min_run_mm=MIN_RUN_MM,
                max_gap_mm=candidate_window_mm,
            )
            if r.key not in attempted
        ]
        # Only runs whose coupled Zdiff is above the band.
        chosen: tuple[_Run, float] | None = None
        for candidate_run in runs:
            run_band_gap = model.gap_for(candidate_run.layer, candidate_run.width_mm, z_max)
            if run_band_gap is None or candidate_run.gap_mm <= run_band_gap + 1e-4:
                continue
            chosen = (candidate_run, run_band_gap)
            break
        if chosen is None:
            break
        run, band_gap = chosen
        attempted.add(run.key)
        z_before = model.zdiff(run.layer, run.width_mm, run.gap_mm)
        g_exact = model.gap_for(run.layer, run.width_mm, model.target)
        gap_target = max(g_exact if g_exact is not None else floor_gap, floor_gap)
        target_gaps.append(gap_target)
        band_gaps.append(band_gap)
        outcome = RunOutcome(
            layer=run.layer,
            status="declined:band_unreachable",
            gap_before_mm=run.gap_mm,
            gap_after_mm=run.gap_mm,
            zdiff_before=z_before,
            zdiff_after=z_before,
        )
        result.runs.append(outcome)
        if gap_target > band_gap - 1e-4:
            # The floored pair gap is itself out of band: squeezing can only
            # turn an unjudged / out-of-band run into a judged one.
            first_decline = first_decline or outcome.status
            continue

        # Unit vector from P's centreline toward N's.
        ux, uy = run.u
        ox, oy = run.origin
        nx_, ny_ = -uy, ux
        side = (run.n_seg.x1 - ox) * nx_ + (run.n_seg.y1 - oy) * ny_
        toward_p = (nx_, ny_) if side > 0 else (-nx_, -ny_)
        toward_n = (-toward_p[0], -toward_p[1])

        before_judged = judge(routes)
        before_conn = connected(routes)
        p_len = _net_length(_pair_segments(routes, p_id, editable=False))
        n_len = _net_length(_pair_segments(routes, n_id, editable=False))
        shorter, longer = ("p", "n") if p_len <= n_len else ("n", "p")
        old_bad = [
            s
            for s in _pair_segments(routes, p_id, editable=False)
            + _pair_segments(routes, n_id, editable=False)
            if id(s) in before_judged and not before_judged[id(s)][0]
        ]

        ladder = [
            gap_target,
            gap_target + 0.5 * (band_gap - gap_target),
            gap_target + 0.9 * (band_gap - gap_target),
        ]
        # The reported reason is the first failure: the one that stopped the
        # preferred (full, symmetric) squeeze.
        reason: str | None = None
        committed = False
        for gap_goal in ladder:
            excess = run.gap_mm - gap_goal
            if excess <= 1e-4:
                continue
            for mode in ("symmetric", f"single_{shorter}", f"single_{longer}"):
                shifts = {
                    "symmetric": (excess / 2, excess / 2),
                    "single_p": (excess, 0.0),
                    "single_n": (0.0, excess),
                }[mode]
                candidate = routes
                moved: dict[int, list[Segment]] = {p_id: [], n_id: []}
                ok = True
                for net, seg, toward, shift in (
                    (p_id, run.p_seg, toward_p, shifts[0]),
                    (n_id, run.n_seg, toward_n, shifts[1]),
                ):
                    if shift <= 0:
                        continue
                    built = _rail_chain(seg, run, toward, shift)
                    if built is None:
                        ok = False
                        break
                    chain, new_segs = built
                    replaced = _replace_segment(candidate, seg, chain)
                    if replaced is None:
                        ok = False
                        break
                    candidate = replaced
                    moved[net] = new_segs
                if not ok:
                    reason = reason or "declined:jog_does_not_fit"
                    continue
                verdict = _gate(
                    router,
                    candidate,
                    moved,
                    p_id=p_id,
                    n_id=n_id,
                    floor_gap=floor_gap,
                    foreign_preserved=foreign_preserved,
                    skew_before=skew_of(routes),
                    skew_after=skew_of(candidate),
                    before_conn=before_conn,
                    connected=connected,
                    judge=judge,
                    old_bad=old_bad,
                    before_judged=before_judged,
                )
                if verdict is not None:
                    reason = reason or verdict
                    continue
                routes = candidate
                committed = True
                trunk = moved[p_id][1] if moved[p_id] else moved[n_id][1]
                after_judged = judge(routes)
                outcome.status = "tightened"
                outcome.mode = mode
                outcome.gap_after_mm = gap_goal
                outcome.zdiff_after = (
                    after_judged[id(trunk)][2]
                    if id(trunk) in after_judged
                    else model.zdiff(run.layer, run.width_mm, gap_goal)
                )
                break
            if committed:
                break
        if not committed:
            reason = reason or "declined:jog_does_not_fit"
            outcome.status = reason
            first_decline = first_decline or reason

    if any(r.status == "tightened" for r in result.runs):
        result.reason = "tightened"
        _publish(router, routes)
    elif first_decline is not None:
        result.reason = first_decline
    else:
        result.reason = "in_band"
    result.skew_after_mm = abs(skew_of(routes))
    result.target_gap_mm = min(target_gaps) if target_gaps else None
    result.band_max_gap_mm = min(band_gaps) if band_gaps else None
    return result


def _gate(
    router: Autorouter,
    candidate: list[Route],
    moved: dict[int, list[Segment]],
    *,
    p_id: int,
    n_id: int,
    floor_gap: float,
    foreign_preserved: list[Route],
    skew_before: float,
    skew_after: float,
    before_conn: dict[int, int],
    connected: Any,
    judge: Any,
    old_bad: list[Segment],
    before_judged: dict[int, tuple[bool, float, float]],
) -> str | None:
    """None when ``candidate`` may be committed, else ``declined:<reason>``."""
    # Skew: unchanged to first order for a symmetric squeeze.
    if abs(skew_after) > abs(skew_before) + SKEW_TOLERANCE_MM:
        return "declined:skew"
    # Foreign pads / vias / copper at the class clearance, the partner at the
    # floored intra-pair gap (the same gate the skew tuner uses).
    routes_by_net: dict[int, Route] = {}
    for route in [*candidate, *foreign_preserved]:
        merged = routes_by_net.get(route.net)
        if merged is None:
            routes_by_net[route.net] = Route(
                net=route.net,
                net_name=route.net_name,
                segments=list(route.segments),
                vias=list(route.vias),
            )
        else:
            merged.segments.extend(route.segments)
            merged.vias.extend(route.vias)
    clearance_ok = router._skew_tune_foreign_clearance_ok(
        routes_by_net,
        partner_of={p_id: n_id, n_id: p_id},
        intra_pair_clearance_mm=floor_gap,
    )
    for net, segs in moved.items():
        if segs and not clearance_ok(segs, net):
            return "declined:clearance"
    # Connectivity: no pad of either net may fall off.
    after_conn = connected(candidate)
    for net, count in before_conn.items():
        if after_conn.get(net, 0) < count:
            return "declined:connectivity"
    # Impedance post-condition.
    after = judge(candidate)
    new_ids = {id(s) for segs in moved.values() for s in segs}
    trunks = [segs[1] for segs in moved.values() if segs]
    for trunk in trunks:
        verdict = after.get(id(trunk))
        if verdict is None or not verdict[0]:
            return "declined:band_unreachable"
    all_segs = _pair_segments(candidate, p_id, editable=False) + _pair_segments(
        candidate, n_id, editable=False
    )
    for seg in all_segs:
        verdict = after.get(id(seg))
        if verdict is None or verdict[0]:
            continue
        if id(seg) in new_ids:
            return "declined:band_unreachable"
        if id(seg) in before_judged:
            if verdict[1] <= before_judged[id(seg)][1] + 1e-6:
                continue
            return "declined:impedance_regression"
        # A split remainder of an already out-of-band segment, no worse.
        if any(
            _covered_by(seg, old) and verdict[1] <= before_judged[id(old)][1] + 1e-6
            for old in old_bad
        ):
            continue
        return "declined:impedance_regression"
    return None


def _publish(router: Autorouter, routes: list[Route]) -> None:
    """Commit ``routes`` as the router's copper, re-syncing the grid."""
    previous = list(router.routes)
    router.restore_route_snapshot(routes, replaced_routes=previous)


def tighten_diffpair_gaps(
    router: Autorouter,
    detected_pairs: list | None = None,
    *,
    verbose: bool = True,
    stackup: Stackup | None = None,
    candidate_window_mm: float = DEFAULT_CANDIDATE_WINDOW_MM,
) -> dict[tuple[str, str], GapTighteningResult]:
    """Run the gap-tightening pass over every detected pair of ``router``."""
    if detected_pairs is None:
        from .diffpair_detection import build_synth_routing, detect_diff_pairs

        class_routing, net_to_class = build_synth_routing(
            getattr(router, "net_class_map", None) or {}
        )
        detected_pairs = detect_diff_pairs(
            net_names=router.net_names,
            net_class_routing=class_routing or None,
            net_to_class=net_to_class or None,
        )
    results: dict[tuple[str, str], GapTighteningResult] = {}
    if not detected_pairs:
        return results
    if stackup is None:
        stackup = resolve_stackup(router)
    for dp in detected_pairs:
        p_name = dp.pair.positive.net_name
        n_name = dp.pair.negative.net_name
        net_class = router._resolve_net_class_for_pair(dp)
        if stackup is None:
            res = GapTighteningResult(p_name, n_name, reason="declined:no_stackup")
        else:
            res = tighten_pair(
                router, dp, net_class, stackup, candidate_window_mm=candidate_window_mm
            )
        results[(p_name, n_name)] = res
        if verbose:
            print(res.format_line())
    return results
