"""The kernel swap moved geometry, not verdicts (Epic #5509 Phase 3d, #5663).

Every lattice clearance predicate used to compose a **centreline** requirement
(``own_half + stored_half + clearance``) and compare a centreline distance
against it.  Since Phase 3d they ask
:mod:`kicad_tools.router.clearance_kernel` -- through
:mod:`kicad_tools.router.lattice.kernel_adapter` -- for an **edge-to-edge**
gap and compare that against the clearance alone.

The two forms are algebraically identical, and this module is the standing
proof.  The ``_legacy_*`` functions below are the pre-kernel arithmetic,
transcribed from the predicates as they stood at ``37a30b8c`` (the merge base
of #5663) and left frozen; each test drives both forms over seeded random
configurations and asserts **identical verdicts**.

Why a differential test rather than a table of expected booleans
----------------------------------------------------------------
A hand-written expectation table can only pin the cases its author thought of,
and the risk in a geometry swap is precisely the case nobody thought of: a
stored trace wider than the querying one, a via inside a segment's body, a
zero-length "segment" (the lattice really does call ``segment_clear(p, p, r)``
for a via site), a negative derived requirement.  The generators here place
candidates *at* the requirement as often as away from it, so a sign error or a
dropped term cannot hide in a corner of the parameter space.

Scope: the migrated arithmetic only.  ``fixed_fills``, ``kelvin_guard`` and
the #4602 ``pairwise`` projection are left at their defaults (empty / ``None``)
because Phase 3d did not touch them -- ``kelvin.py`` deliberately keeps its own
region test, and the pairwise term is a *requirement*, re-compared here against
the same measured gap on both sides.
"""

from __future__ import annotations

import math
import random

from kicad_tools.router.clearance_kernel import CLEARANCE_EPSILON_MM
from kicad_tools.router.lattice import kernel_adapter as ka
from kicad_tools.router.lattice.coupled import (
    committed_point_clear_grown,
    committed_seg_clear_grown,
)
from kicad_tools.router.lattice.escape_boundary import EscapeBoundary
from kicad_tools.router.lattice.geometry import Pt, dist, seg_pt_dist, seg_seg_dist
from kicad_tools.router.lattice.obstacles import CommittedCopper, seg_body_crosses_pt

_EPS = 1e-9
"""The lattice's own comparison slack -- unchanged by the migration."""


# ---------------------------------------------------------------------------
# The pre-kernel arithmetic, frozen
# ---------------------------------------------------------------------------


def _legacy_seg_clear(
    committed: CommittedCopper, a: Pt, b: Pt, layer: int, net: int, own_half: float, own_clr: float
) -> bool:
    """``CommittedCopper.seg_clear``'s centreline form, before Phase 3d."""
    pad = own_half + own_clr + 0.5
    for c, d, cnet, hw, iclr in committed.copper[layer].query_seg(a, b, pad=pad):
        if cnet == net:
            continue
        gap = own_half + hw + max(own_clr, iclr)
        if seg_seg_dist(a, b, c, d) < gap - _EPS:
            return False
    own_via_gap = committed.via_radius + own_half + own_clr
    for point, vnet, vclr in committed.vias:
        if vnet != net:
            via_gap = own_via_gap if vclr <= own_clr else committed.via_radius + own_half + vclr
            if seg_pt_dist(a, b, point) < via_gap - _EPS:
                return False
        elif seg_body_crosses_pt(a, b, point):
            return False
    return True


def _legacy_node_clear(
    committed: CommittedCopper, point: Pt, layer: int, net: int, own_half: float, own_clr: float
) -> bool:
    """``CommittedCopper.node_clear``'s centreline form, before Phase 3d."""
    pad = own_half + own_clr + 0.5
    for c, d, cnet, hw, iclr in committed.copper[layer].query_seg(point, point, pad=pad):
        if cnet == net:
            continue
        gap = own_half + hw + max(own_clr, iclr)
        if seg_pt_dist(c, d, point) < gap - _EPS:
            return False
    own_via_gap = committed.via_radius + own_half + own_clr
    for vpt, vnet, vclr in committed.vias:
        if vnet == net:
            continue
        via_gap = own_via_gap if vclr <= own_clr else committed.via_radius + own_half + vclr
        if dist(point, vpt) < via_gap - _EPS:
            return False
    return True


def _legacy_via_clear(committed: CommittedCopper, point: Pt, net: int, own_clr: float) -> bool:
    """``CommittedCopper.via_clear``'s centreline form, before Phase 3d."""
    pad = committed.via_radius + own_clr + committed.trace_half + 2.0
    for layer in range(committed.num_layers):
        for c, d, cnet, hw, iclr in committed.copper[layer].query_seg(point, point, pad=pad):
            if cnet == net:
                continue
            gap = committed.via_radius + hw + max(own_clr, iclr)
            if seg_pt_dist(c, d, point) < gap - _EPS:
                return False
    for vpt, vnet, vclr in committed.vias:
        if vnet != net:
            gap = max(
                committed.via_via_gap,
                2.0 * committed.via_radius + max(own_clr, vclr),
                committed.same_net_via_gap,
            )
        else:
            gap = committed.same_net_via_gap
        if dist(point, vpt) < gap - _EPS:
            return False
    return True


def _legacy_seg_clear_grown(
    committed: CommittedCopper, a: Pt, b: Pt, layer: int, nets: set[int], extra: float
) -> bool:
    """``coupled.committed_seg_clear_grown``'s centreline form, before Phase 3d."""
    pad = committed.trace_half + committed.clearance + extra + 0.5
    for c, d, cnet, hw, iclr in committed.copper[layer].query_seg(a, b, pad=pad):
        gap = committed.trace_half + hw + max(committed.clearance, iclr) + extra
        if cnet not in nets and seg_seg_dist(a, b, c, d) < gap - _EPS:
            return False
    base_vgap = committed.via_radius + committed.trace_half + committed.clearance + extra
    for point, vnet, vclr in committed.vias:
        vgap = base_vgap if vclr <= committed.clearance else base_vgap - committed.clearance + vclr
        if vnet not in nets and seg_pt_dist(a, b, point) < vgap - _EPS:
            return False
    return True


def _legacy_point_clear_grown(
    committed: CommittedCopper, point: Pt, layer: int, nets: set[int], extra: float
) -> bool:
    """``coupled.committed_point_clear_grown``'s centreline form, before 3d."""
    pad = committed.trace_half + committed.clearance + extra + 0.5
    for c, d, cnet, hw, iclr in committed.copper[layer].query_seg(point, point, pad=pad):
        gap = committed.trace_half + hw + max(committed.clearance, iclr) + extra
        if cnet not in nets and seg_pt_dist(c, d, point) < gap - _EPS:
            return False
    base_vgap = committed.via_radius + committed.trace_half + committed.clearance + extra
    for vpt, vnet, vclr in committed.vias:
        vgap = base_vgap if vclr <= committed.clearance else base_vgap - committed.clearance + vclr
        if vnet not in nets and dist(point, vpt) < vgap - _EPS:
            return False
    return True


def _legacy_boundary_clear(
    edges: list[tuple[Pt, Pt]], margin: float, a: Pt, b: Pt, half_width: float
) -> bool:
    """``EscapeBoundary.segment_clear``'s outline arithmetic, before Phase 3d."""
    required = half_width + margin
    return all(seg_seg_dist(a, b, c, d) >= required - _EPS for c, d in edges)


# ---------------------------------------------------------------------------
# Seeded scene construction
# ---------------------------------------------------------------------------

_NUM_LAYERS = 2
_NETS = (1, 2, 3)


def _scene(rng: random.Random) -> CommittedCopper:
    """A committed-copper model with random widths, clearances and vias.

    Widths and clearances are drawn independently per object so the
    ``max(own_clr, stored_clr)`` and per-object half-width terms are really
    exercised -- a scene where every trace is the board default would pass
    even if the migration had dropped them.
    """
    committed = CommittedCopper(
        _NUM_LAYERS,
        trace_half=rng.uniform(0.05, 0.3),
        clearance=rng.uniform(0.1, 0.4),
        via_radius=rng.uniform(0.15, 0.4),
        via_via_gap=rng.uniform(0.4, 1.2),
        same_net_via_gap=rng.uniform(0.3, 1.0),
    )
    for _ in range(rng.randint(1, 6)):
        layer = rng.randrange(_NUM_LAYERS)
        start = (rng.uniform(0.0, 10.0), rng.uniform(0.0, 10.0))
        end = (start[0] + rng.uniform(-3.0, 3.0), start[1] + rng.uniform(-3.0, 3.0))
        committed.add_run(
            layer,
            [start, end],
            rng.choice(_NETS),
            rng.uniform(0.05, 0.5),
            clearance=rng.choice([None, rng.uniform(0.05, 0.6)]),
        )
    for _ in range(rng.randint(0, 4)):
        committed.add_via(
            (rng.uniform(0.0, 10.0), rng.uniform(0.0, 10.0)),
            rng.choice(_NETS),
            clearance=rng.choice([None, rng.uniform(0.05, 0.6)]),
        )
    return committed


def _probe_points(rng: random.Random, committed: CommittedCopper) -> list[Pt]:
    """Query points: some uniform, some pinned near committed copper.

    Uniform points alone would almost always land far from everything and
    every predicate would answer ``True`` on both sides -- a green test that
    measured nothing.  Half the probes are therefore placed a small offset
    from a stored via or a stored segment endpoint, i.e. exactly in the band
    where the requirement is decided.
    """
    points: list[Pt] = [(rng.uniform(0.0, 10.0), rng.uniform(0.0, 10.0)) for _ in range(6)]
    anchors: list[Pt] = [pt for pt, _net, _clr in committed.vias]
    # Read the stored segments out of the spatial hash's buckets directly: a
    # ``query_seg`` wide enough to cover the whole scene would materialise
    # hundreds of thousands of empty ``defaultdict`` buckets.
    for layer in range(committed.num_layers):
        seen: set[int] = set()
        for bucket in committed.copper[layer].buckets.values():
            for item in bucket:
                if id(item) in seen:
                    continue
                seen.add(id(item))
                anchors.extend((item[0], item[1]))
    for anchor in anchors:
        theta = rng.uniform(0.0, 2.0 * math.pi)
        radius = rng.uniform(0.0, 1.5)
        points.append((anchor[0] + radius * math.cos(theta), anchor[1] + radius * math.sin(theta)))
    return points


# ---------------------------------------------------------------------------
# Verdict parity
# ---------------------------------------------------------------------------


def test_seg_clear_matches_the_pre_kernel_arithmetic() -> None:
    decided = 0
    for seed in range(60):
        rng = random.Random(seed)
        committed = _scene(rng)
        for point in _probe_points(rng, committed):
            other = (point[0] + rng.uniform(-2.0, 2.0), point[1] + rng.uniform(-2.0, 2.0))
            layer = rng.randrange(_NUM_LAYERS)
            net = rng.choice(_NETS)
            own_half = rng.uniform(0.05, 0.4)
            own_clr = rng.uniform(0.05, 0.5)
            new = committed.seg_clear(point, other, layer, net, own_half, own_clr)
            old = _legacy_seg_clear(committed, point, other, layer, net, own_half, own_clr)
            assert new == old, (
                f"seed {seed}: seg_clear {point}->{other} layer={layer} net={net} "
                f"half={own_half} clr={own_clr}: kernel={new} legacy={old}"
            )
            decided += int(not new)
    assert decided, "no candidate was ever rejected -- the parity check is vacuous"


def test_node_clear_matches_the_pre_kernel_arithmetic() -> None:
    decided = 0
    for seed in range(60):
        rng = random.Random(1000 + seed)
        committed = _scene(rng)
        for point in _probe_points(rng, committed):
            layer = rng.randrange(_NUM_LAYERS)
            net = rng.choice(_NETS)
            own_half = rng.uniform(0.05, 0.4)
            own_clr = rng.uniform(0.05, 0.5)
            new = committed.node_clear(point, layer, net, own_half, own_clr)
            old = _legacy_node_clear(committed, point, layer, net, own_half, own_clr)
            assert new == old, (
                f"seed {seed}: node_clear {point} layer={layer} net={net}: "
                f"kernel={new} legacy={old}"
            )
            decided += int(not new)
    assert decided, "no candidate was ever rejected -- the parity check is vacuous"


def test_via_clear_matches_the_pre_kernel_arithmetic() -> None:
    """Includes the two derived via requirements re-expressed edge-to-edge.

    ``via_via_gap`` and ``same_net_via_gap`` arrive from the pathfinder as
    **centre-to-centre** numbers, so the migration had to subtract two barrel
    radii from each to state them in the kernel's frame.  That subtraction can
    make the requirement negative (a hole-to-hole floor smaller than the via
    body), which is exactly the kind of sign detail a differential test
    catches and a spot-check does not -- ``_scene`` draws the two gaps
    independently of ``via_radius`` on purpose.
    """
    decided = 0
    for seed in range(60):
        rng = random.Random(2000 + seed)
        committed = _scene(rng)
        for point in _probe_points(rng, committed):
            net = rng.choice(_NETS)
            own_clr = rng.uniform(0.05, 0.5)
            new = committed.via_clear(point, net, own_clr)
            old = _legacy_via_clear(committed, point, net, max(committed.clearance, own_clr))
            assert new == old, (
                f"seed {seed}: via_clear {point} net={net} clr={own_clr}: kernel={new} legacy={old}"
            )
            decided += int(not new)
    assert decided, "no candidate was ever rejected -- the parity check is vacuous"


def test_grown_predicates_match_the_pre_kernel_arithmetic() -> None:
    """The coupled fat-envelope helpers, where ``extra`` widens the gap."""
    decided = 0
    for seed in range(60):
        rng = random.Random(3000 + seed)
        committed = _scene(rng)
        for point in _probe_points(rng, committed):
            other = (point[0] + rng.uniform(-2.0, 2.0), point[1] + rng.uniform(-2.0, 2.0))
            layer = rng.randrange(_NUM_LAYERS)
            nets = set(rng.sample(_NETS, rng.randint(1, 2)))
            extra = rng.uniform(0.0, 0.8)

            new_seg = committed_seg_clear_grown(committed, point, other, layer, nets, extra)
            old_seg = _legacy_seg_clear_grown(committed, point, other, layer, nets, extra)
            assert new_seg == old_seg, (
                f"seed {seed}: committed_seg_clear_grown {point}->{other} "
                f"nets={sorted(nets)} extra={extra}: kernel={new_seg} legacy={old_seg}"
            )

            new_pt = committed_point_clear_grown(committed, point, layer, nets, extra)
            old_pt = _legacy_point_clear_grown(committed, point, layer, nets, extra)
            assert new_pt == old_pt, (
                f"seed {seed}: committed_point_clear_grown {point} "
                f"nets={sorted(nets)} extra={extra}: kernel={new_pt} legacy={old_pt}"
            )
            decided += int(not new_seg) + int(not new_pt)
    assert decided, "no candidate was ever rejected -- the parity check is vacuous"


def test_escape_boundary_matches_the_pre_kernel_arithmetic() -> None:
    """Copper against the board outline, including the degenerate via probe.

    ``LatticePathfinder`` really calls ``segment_clear(p, p, via_radius)`` for
    a via site, so a zero-length candidate is a production input and not a
    fuzzing curiosity: the kernel's segment-to-polyline path has to agree with
    the lattice's on it.
    """
    decided = 0
    for seed in range(40):
        rng = random.Random(4000 + seed)
        # A closed rectangle, so ``_inside`` accepts interior probes and the
        # predicate is reached rather than short-circuited.
        w = rng.uniform(6.0, 14.0)
        h = rng.uniform(6.0, 14.0)
        corners: list[Pt] = [(0.0, 0.0), (w, 0.0), (w, h), (0.0, h)]
        edges = [(corners[i], corners[(i + 1) % 4]) for i in range(4)]
        clearance = rng.uniform(0.1, 0.6)
        boundary = EscapeBoundary(edges, clearance)
        assert boundary.valid, "the rectangle outline must polygonize"

        for _ in range(12):
            a = (rng.uniform(0.0, w), rng.uniform(0.0, h))
            b = (
                (a[0], a[1])
                if rng.random() < 0.25  # the degenerate via-site probe
                else (a[0] + rng.uniform(-2.0, 2.0), a[1] + rng.uniform(-2.0, 2.0))
            )
            half = rng.uniform(0.05, 0.5)
            new = boundary.segment_clear(a, b, half)
            old = (
                boundary._inside(a)
                and boundary._inside(b)
                and _legacy_boundary_clear(edges, boundary.margin, a, b, half)
            )
            assert new == old, (
                f"seed {seed}: segment_clear {a}->{b} half={half}: kernel={new} legacy={old}"
            )
            decided += int(not new)
    assert decided, "no candidate was ever rejected -- the parity check is vacuous"


# ---------------------------------------------------------------------------
# The adapter's own contract
# ---------------------------------------------------------------------------


def test_adapter_keeps_the_lattice_epsilon_not_the_kernels() -> None:
    """The migration swapped geometry, not the verdict tolerance.

    The kernel's ``CLEARANCE_EPSILON_MM`` is 1e-4 mm -- a thousand times the
    lattice's own 1e-9 slack.  Adopting it here would be a behaviour change
    wearing a refactor's clothes: candidates 0.1 um inside the requirement
    would start being accepted.  Collapsing the two is Phase 4's job, once
    every consumer is on the kernel, so until then the difference is an
    invariant rather than an oversight.
    """
    assert ka.LATTICE_CLEARANCE_EPSILON_MM == 1e-9
    assert ka.LATTICE_CLEARANCE_EPSILON_MM < CLEARANCE_EPSILON_MM
    assert ka.satisfies(1.0 - 1e-10, 1.0) is True
    assert ka.satisfies(1.0 - 1e-7, 1.0) is False


def test_shape_projection_reports_edge_to_edge_gaps() -> None:
    """``trace`` / ``site`` / ``outline_edge`` subtract the right extents."""
    # Two parallel traces 1.0 mm apart, centre to centre.
    left = ka.trace((0.0, 0.0), (0.0, 5.0), 0.1, layer=0)
    right = ka.trace((1.0, 0.0), (1.0, 5.0), 0.25, layer=0)
    assert ka.gap(left, right) == 1.0 - 0.1 - 0.25

    # A via barrel 2.0 mm from a trace centreline.
    via = ka.site((2.0, 2.5), 0.3)
    assert ka.gap(left, via) == 2.0 - 0.1 - 0.3

    # Two barrels, centre to centre.
    assert ka.gap(via, ka.site((2.0, 5.5), 0.4)) == 3.0 - 0.3 - 0.4

    # Copper against one outline edge: only the copper's half-width applies.
    assert ka.gap(left, ka.outline_edge((-1.5, 0.0), (-1.5, 5.0))) == 1.5 - 0.1


def test_a_probe_is_not_an_obstacle_to_its_own_net() -> None:
    """The one net-id rule the kernel cannot express, kept in the adapter."""
    probe = ka.LatticeProbe(ka.site((0.0, 0.0), 0.3), net=7)
    assert probe.foreign(8) is True
    assert probe.foreign(7) is False


def test_different_layers_never_interact() -> None:
    """A layer-tagged trace pair on different layers is not a clearance pair.

    ``seg_clear`` already queries a single layer's ``SegHash``, so this can
    never fire in production -- which is exactly why it is asserted here: the
    adapter passes the layer through, and a future caller that stops
    pre-filtering must not silently start comparing copper across layers.
    """
    top = ka.trace((0.0, 0.0), (0.0, 5.0), 0.1, layer=0)
    bottom = ka.trace((0.0, 0.0), (0.0, 5.0), 0.1, layer=1)
    assert ka.gap(top, bottom) == math.inf
    assert ka.satisfies(ka.gap(top, bottom), 99.0) is True
