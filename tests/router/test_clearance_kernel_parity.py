"""C++/Python parity for the exact-geometry clearance kernel (Epic #5509, 1b).

The kernel ships as two implementations -- ``router/cpp/src/clearance_kernel.cpp``
and ``router/clearance_kernel.py`` -- and the Python one is a *port*, not an
independent model.  This module is the standing proof that they have not
drifted, and it is **mandatory for every future kernel edit**: a divergence is
a bug on whichever side disagrees with kicad-cli, never a reason to widen the
tolerance.

What is asserted, per seed:

* identical :func:`clear` verdicts on every non-boundary pair;
* ``|gap_py - gap_cpp| <= 1e-7`` mm on **every** pair, boundary or not (it is a
  pure-arithmetic bound, independent of any threshold);
* ``copper_gap`` / ``hole_gap`` agree about *inapplicability* too -- one side
  returning ``+inf`` while the other returns a number is a divergence.

Two shape sources are driven through both ports:

1. the Phase 1a corpus generator (``tests/conformance/generator.py``), whose
   pairs are placed analytically at a known gap near the requirement; and
2. a seeded raw-shape sampler, which deliberately produces crossing,
   overlapping and coincident geometry the corpus never emits (negative gaps
   and the proper-intersection early-out in ``segment_to_segment_distance``).

Scope: Phase 1b is complete here -- all five shape classes (``KSegment``,
``KVia``, ``KEdge``, ``KPad``, ``KZonePoly``) and all **fifteen** unordered
pair kinds five shape classes admit.  (#5589's acceptance criteria call that
"25 kinds", counting ordered pairs; the kernel's answer is order-independent by
construction, so the unordered count is the one a coverage assertion can use --
``test_every_pair_kind_is_covered`` checks 15 and the order-independence of
every pair is asserted separately, on every pair, in ``_compare_shapes``.)

Shaped after ``tests/router/test_pairwise_cpp_parity.py``: module-level
``requires_cpp`` skipif, ``router_cpp`` imported lazily inside helpers, and a
per-test ``random.Random(seed)`` rather than module-global RNG state (the CI
bulk run is ``pytest -n auto``).
"""

from __future__ import annotations

import math
import os
import random
import subprocess
from pathlib import Path

import pytest

from kicad_tools.router import clearance_kernel as ck
from kicad_tools.router.cpp_backend import is_cpp_available
from tests.conformance.generator import (
    BOUNDARY_BAND_MM,
    PAD_SHAPES,
    PadSpec,
    generate_case,
)

requires_cpp = pytest.mark.skipif(
    not is_cpp_available(),
    reason="C++ router backend not available",
)

# Parity bound on the gap itself.  Pure arithmetic in float64 on both sides,
# so the only expected difference is last-bit rounding from FMA contraction in
# the optimised C++ build -- ~1e-16 relative, twelve orders below this bound.
GAP_TOLERANCE_MM = 1e-7

# Pairs whose gap sits inside +/-this of the requirement are excluded from
# *verdict* comparison and counted separately: there, an identical gap can
# still land on opposite sides of the threshold under any rounding at all, so
# a verdict mismatch would measure the comparison, not the model.  Same band
# the Phase 1a generator uses (10x CLEARANCE_EPSILON_MM).
VERDICT_BOUNDARY_BAND_MM = BOUNDARY_BAND_MM

# Copper layer name -> kernel layer index.  The kernel takes an opaque int;
# vias and the board outline span every layer and carry no layer at all.
_LAYER_INDEX = {"F.Cu": 0, "In1.Cu": 1, "In2.Cu": 2, "B.Cu": 3}

# Every shape kind the kernel covers.
PAIR_KINDS = ("segment", "via", "edge", "pad", "zone")


# ---------------------------------------------------------------------------
# Shape construction: one Python shape, one C++ shape, from the same numbers
# ---------------------------------------------------------------------------


def _cpp_segment(seg: ck.KSegment):
    from kicad_tools.router import router_cpp

    return router_cpp.KSegment(seg.x1, seg.y1, seg.x2, seg.y2, seg.width, seg.layer)


def _cpp_via(via: ck.KVia):
    from kicad_tools.router import router_cpp

    return router_cpp.KVia(via.x, via.y, via.diameter, via.drill)


def _cpp_edge(edge: ck.KEdge):
    from kicad_tools.router import router_cpp

    return router_cpp.KEdge([(x, y) for x, y in edge.points])


def _cpp_pad(pad: ck.KPad):
    """Twin the *already-built* Python pad, core vertices and all.

    Deliberately not ``router_cpp.make_pad(...)`` from the same parameters:
    passing the Python core across means a divergence in ``make_pad`` itself
    cannot hide behind matching gaps.  ``make_pad`` parity is asserted
    separately, and exactly, by ``test_make_pad_agrees_on_the_core``.
    """
    from kicad_tools.router import router_cpp

    return router_cpp.KPad(
        [(x, y) for x, y in pad.core],
        pad.corner_radius,
        pad.cx,
        pad.cy,
        pad.drill,
        pad.layer,
    )


def _cpp_zone(zone: ck.KZonePoly):
    from kicad_tools.router import router_cpp

    return router_cpp.KZonePoly(
        [[(x, y) for x, y in ring] for ring in zone.rings],
        zone.layer,
    )


def _to_cpp(shape: ck.KShape):
    """Build the C++ twin of a Python kernel shape."""
    if isinstance(shape, ck.KSegment):
        return _cpp_segment(shape)
    if isinstance(shape, ck.KVia):
        return _cpp_via(shape)
    if isinstance(shape, ck.KEdge):
        return _cpp_edge(shape)
    if isinstance(shape, ck.KPad):
        return _cpp_pad(shape)
    return _cpp_zone(shape)


def _kind(shape: ck.KShape) -> str:
    if isinstance(shape, ck.KSegment):
        return "segment"
    if isinstance(shape, ck.KVia):
        return "via"
    if isinstance(shape, ck.KEdge):
        return "edge"
    if isinstance(shape, ck.KPad):
        return "pad"
    return "zone"


# ---------------------------------------------------------------------------
# Shape sources
# ---------------------------------------------------------------------------


def _corpus_pad(pad: PadSpec) -> ck.KPad:
    """A corpus ``PadSpec`` as a kernel pad, through the ``_pad_polygon`` port.

    The probe footprints are SMD (``drill`` 0), single-layer, and carry their
    shape keyword / local size / roundrect ratio on ``PadSpec.shape``.  The
    rotation is already absolute -- the generator's own ``local_x_axis`` uses
    ``rotate_pad_offset(.., pad.rotation)`` for the same reason (#3902).
    """
    shape = pad.shape
    return ck.make_pad(
        shape.shape,
        shape.size[0],
        shape.size[1],
        0.25 if shape.roundrect_rratio is None else shape.roundrect_rratio,
        pad.rotation,
        pad.x,
        pad.y,
        _LAYER_INDEX[pad.layer],
        0.0,
    )


def _corpus_shapes(seed: int) -> tuple[list[ck.KShape], float]:
    """Kernel shapes for one Phase 1a corpus case, plus its requirement.

    Every copper object the case declares is converted: segments, vias, pads
    (through :func:`_corpus_pad`) and the pour, plus the board outline as a
    closed ``KEdge`` so copper-to-edge pairs are exercised on every seed.

    The pour is modelled as its **declared boundary** -- one outer ring, no
    interior rings.  The corpus writes zones *unfilled* (``ZoneSpec``: "only
    meaningful after a refill"), so there is no committed ``filled_polygon`` to
    read here and inventing a knockout would be fiction.  Rings *with* holes --
    the structure ``KZonePoly`` exists for -- are covered by
    :func:`_random_shapes` and pinned semantically by
    ``test_zone_interior_rings_are_not_copper``.
    """
    case = generate_case(seed)
    shapes: list[ck.KShape] = []

    for seg in case.segments:
        shapes.append(
            ck.KSegment(
                x1=seg.start[0],
                y1=seg.start[1],
                x2=seg.end[0],
                y2=seg.end[1],
                width=seg.width,
                layer=_LAYER_INDEX[seg.layer],
            )
        )
    for via in case.vias:
        shapes.append(ck.KVia(x=via.x, y=via.y, diameter=via.diameter, drill=via.drill))
    for pad in case.pads:
        shapes.append(_corpus_pad(pad))
    if case.zone is not None:
        boundary = tuple(case.zone.boundary)
        shapes.append(
            ck.KZonePoly(
                rings=((*boundary, boundary[0]),),
                layer=_LAYER_INDEX[case.zone.layer],
            )
        )

    w, h = case.width, case.height
    shapes.append(ck.KEdge(points=((0.0, 0.0), (w, 0.0), (w, h), (0.0, h), (0.0, 0.0))))
    return shapes, case.rules.project_clearance


def _random_zone(rng: random.Random) -> ck.KZonePoly:
    """A rectangular pour, half the time with a rectangular hole punched in it.

    The hole is what makes ``KZonePoly`` more than a polygon: a point inside it
    is *not* copper.  Sampling holed and un-holed pours in the same corpus is
    what makes a containment bug on either side show up as a verdict mismatch
    rather than as a quietly-agreed wrong answer.
    """
    cx = round(rng.uniform(-2.0, 2.0), 6)
    cy = round(rng.uniform(-2.0, 2.0), 6)
    half_w = round(rng.uniform(0.8, 2.0), 6)
    half_h = round(rng.uniform(0.8, 2.0), 6)
    outer = (
        (cx - half_w, cy - half_h),
        (cx + half_w, cy - half_h),
        (cx + half_w, cy + half_h),
        (cx - half_w, cy + half_h),
    )
    rings: list[tuple[tuple[float, float], ...]] = [(*outer, outer[0])]
    if rng.random() < 0.5:
        hw = round(half_w * rng.uniform(0.2, 0.6), 6)
        hh = round(half_h * rng.uniform(0.2, 0.6), 6)
        hole = (
            (cx - hw, cy - hh),
            (cx + hw, cy - hh),
            (cx + hw, cy + hh),
            (cx - hw, cy + hh),
        )
        rings.append((*hole, hole[0]))
    return ck.KZonePoly(rings=tuple(rings), layer=rng.choice([0, 1, ck.ALL_LAYERS]))


def _random_shapes(rng: random.Random, count: int) -> list[ck.KShape]:
    """Adversarial raw shapes: crossing, overlapping and coincident geometry.

    The corpus places every pair at a positive gap near the requirement, so on
    its own it never reaches the negative-gap branch, the proper-intersection
    early-out, or the "this shape is *inside* that pour" containment branch.
    These do.
    """
    shapes: list[ck.KShape] = []
    for _ in range(count):
        pick = rng.random()
        if pick < 0.30:
            shapes.append(
                ck.KSegment(
                    x1=round(rng.uniform(-2.0, 2.0), 6),
                    y1=round(rng.uniform(-2.0, 2.0), 6),
                    x2=round(rng.uniform(-2.0, 2.0), 6),
                    y2=round(rng.uniform(-2.0, 2.0), 6),
                    width=round(rng.uniform(0.1, 0.5), 6),
                    layer=rng.choice([0, 1, ck.ALL_LAYERS]),
                )
            )
        elif pick < 0.55:
            diameter = round(rng.uniform(0.3, 0.9), 6)
            shapes.append(
                ck.KVia(
                    x=round(rng.uniform(-2.0, 2.0), 6),
                    y=round(rng.uniform(-2.0, 2.0), 6),
                    diameter=diameter,
                    # 1-in-5 vias are hole-free, to exercise the
                    # NO_INTERACTION branches of ``hole_gap``.
                    drill=0.0 if rng.random() < 0.2 else round(diameter * 0.5, 6),
                )
            )
        elif pick < 0.70:
            n = rng.randint(1, 4)
            shapes.append(
                ck.KEdge(
                    points=tuple(
                        (round(rng.uniform(-3.0, 3.0), 6), round(rng.uniform(-3.0, 3.0), 6))
                        for _ in range(n)
                    )
                )
            )
        elif pick < 0.88:
            # Pads from the whole shape catalogue, including an unknown keyword
            # so the ``rect`` fallback of ``_pad_polygon`` is exercised, and a
            # 1-in-4 drilled (through-hole) pad for the hole branches.
            catalogue = [(s.shape, s.size, s.roundrect_rratio) for s in PAD_SHAPES]
            catalogue.append(("trapezoid", (1.1, 0.7), None))
            shape_kw, (w, h), rratio = rng.choice(catalogue)
            drilled = rng.random() < 0.25
            shapes.append(
                ck.make_pad(
                    shape_kw,
                    w,
                    h,
                    0.25 if rratio is None else rratio,
                    round(rng.uniform(0.0, 360.0), 3),
                    round(rng.uniform(-2.0, 2.0), 6),
                    round(rng.uniform(-2.0, 2.0), 6),
                    ck.ALL_LAYERS if drilled else rng.choice([0, 1]),
                    round(min(w, h) * 0.4, 6) if drilled else 0.0,
                )
            )
        else:
            shapes.append(_random_zone(rng))
    return shapes


# ---------------------------------------------------------------------------
# The comparison itself
# ---------------------------------------------------------------------------


def _compare_gap(label: str, py_gap: float, cpp_gap: float) -> float:
    """Assert one gap pair agrees; return the absolute delta (0.0 for +inf)."""
    py_inf = math.isinf(py_gap)
    cpp_inf = math.isinf(cpp_gap)
    assert py_inf == cpp_inf, (
        f"{label}: inapplicability disagrees -- python={py_gap!r} cpp={cpp_gap!r}"
    )
    if py_inf:
        return 0.0
    delta = abs(py_gap - cpp_gap)
    assert delta <= GAP_TOLERANCE_MM, (
        f"{label}: gap delta {delta:.3e} mm exceeds {GAP_TOLERANCE_MM:.0e} mm "
        f"(python={py_gap!r}, cpp={cpp_gap!r})"
    )
    return delta


def _compare_shapes(shapes: list[ck.KShape], required: float) -> dict[str, float | int]:
    """Drive every unordered pair of ``shapes`` through both ports.

    Returns:
        Counters for the test's own reporting: pairs compared, boundary pairs
        excluded from verdict comparison, and the worst gap delta seen.
    """
    cpp_shapes = [_to_cpp(s) for s in shapes]
    from kicad_tools.router import router_cpp

    pairs = 0
    boundary = 0
    max_delta = 0.0
    kinds_seen: set[tuple[str, str]] = set()

    for i in range(len(shapes)):
        for j in range(i + 1, len(shapes)):
            a, b = shapes[i], shapes[j]
            ca, cb = cpp_shapes[i], cpp_shapes[j]
            label = f"{_kind(a)}[{i}] vs {_kind(b)}[{j}]"
            kinds_seen.add(tuple(sorted((_kind(a), _kind(b)))))  # type: ignore[arg-type]
            pairs += 1

            py_copper = ck.copper_gap(a, b)
            cpp_copper = router_cpp.copper_gap(ca, cb)
            max_delta = max(max_delta, _compare_gap(f"{label} copper_gap", py_copper, cpp_copper))

            py_hole = ck.hole_gap(a, b)
            cpp_hole = router_cpp.hole_gap(ca, cb)
            max_delta = max(max_delta, _compare_gap(f"{label} hole_gap", py_hole, cpp_hole))

            # Argument order must not change the answer -- the kernel has no
            # notion of "which existed first" (the #5398 defect, in miniature).
            # Held to the same 1e-7 mm bound rather than bit-exactness: the two
            # orders subtract the same two radii in the opposite sequence, so
            # they can differ in the last ulp (~1e-16 mm, ten orders below
            # KiCad's 1 nm coordinate grid) without any model disagreement.
            _compare_gap(f"{label} copper_gap (swapped)", py_copper, router_cpp.copper_gap(cb, ca))
            _compare_gap(f"{label} hole_gap (swapped)", py_hole, router_cpp.hole_gap(cb, ca))
            _compare_gap(f"{label} copper_gap (python, swapped)", py_copper, ck.copper_gap(b, a))
            _compare_gap(f"{label} hole_gap (python, swapped)", py_hole, ck.hole_gap(b, a))
            assert (
                ck.clear(b, a, required) == ck.clear(a, b, required)
                or abs(py_copper - required) <= VERDICT_BOUNDARY_BAND_MM
            ), f"{label}: python clear() is order-dependent"

            py_clear = ck.clear(a, b, required)
            cpp_clear = router_cpp.clear(ca, cb, required)
            if not math.isinf(py_copper) and abs(py_copper - required) <= VERDICT_BOUNDARY_BAND_MM:
                boundary += 1
                continue
            assert py_clear == cpp_clear, (
                f"{label}: clear() verdict mismatch at required={required} mm "
                f"(python={py_clear}, cpp={cpp_clear}, gap={py_copper!r})"
            )

    return {
        "pairs": pairs,
        "boundary": boundary,
        "max_delta": max_delta,
        "kinds": len(kinds_seen),
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
#
# 200 seeds, chunked into 8 parametrised cases so no single test function has
# to fit the CI job's ``--timeout=60`` per-test budget.


SEEDS_PER_CHUNK = 25
SEED_BASES = tuple(range(0, 200, SEEDS_PER_CHUNK))


@requires_cpp
@pytest.mark.parametrize("seed_base", SEED_BASES)
def test_corpus_parity(seed_base: int, record_property) -> None:
    """Both ports agree on every pair of every Phase 1a corpus case."""
    pairs = boundary = 0
    max_delta = 0.0
    for seed in range(seed_base, seed_base + SEEDS_PER_CHUNK):
        shapes, required = _corpus_shapes(seed)
        stats = _compare_shapes(shapes, required)
        pairs += int(stats["pairs"])
        boundary += int(stats["boundary"])
        max_delta = max(max_delta, float(stats["max_delta"]))

    assert pairs > 0, "corpus produced no pairs -- generator adapter is broken"
    record_property("kernel_parity_pairs", pairs)
    record_property("kernel_parity_boundary_excluded", boundary)
    record_property("kernel_parity_max_gap_delta_mm", max_delta)
    print(
        f"[corpus seeds {seed_base}..{seed_base + SEEDS_PER_CHUNK - 1}] "
        f"pairs={pairs} boundary_excluded={boundary} max_gap_delta={max_delta:.3e} mm"
    )


@requires_cpp
@pytest.mark.parametrize("seed_base", SEED_BASES)
def test_random_shape_parity(seed_base: int, record_property) -> None:
    """Both ports agree on crossing / overlapping / hole-free raw shapes."""
    pairs = boundary = 0
    max_delta = 0.0
    kinds: set[int] = set()
    for seed in range(seed_base, seed_base + SEEDS_PER_CHUNK):
        rng = random.Random(seed)
        shapes = _random_shapes(rng, 10)
        # A requirement inside the sampled gap range, so both verdicts occur.
        required = round(rng.uniform(0.1, 0.4), 6)
        stats = _compare_shapes(shapes, required)
        pairs += int(stats["pairs"])
        boundary += int(stats["boundary"])
        max_delta = max(max_delta, float(stats["max_delta"]))
        kinds.add(int(stats["kinds"]))

    assert pairs > 0
    record_property("kernel_random_pairs", pairs)
    record_property("kernel_random_boundary_excluded", boundary)
    record_property("kernel_random_max_gap_delta_mm", max_delta)
    print(
        f"[random seeds {seed_base}..{seed_base + SEEDS_PER_CHUNK - 1}] "
        f"pairs={pairs} boundary_excluded={boundary} max_gap_delta={max_delta:.3e} mm"
    )


@requires_cpp
def test_every_pair_kind_is_covered() -> None:
    """All fifteen unordered pair kinds the kernel supports are exercised.

    A silent gap in coverage is the failure mode that makes a parity suite
    feel green while the interesting branch is never run.

    Fifteen, not twenty-five: five shape classes admit 25 *ordered* pairs, and
    #5589's acceptance criteria count them that way, but the kernel's answer is
    order-independent by construction (that absence is the #5398 fix), so the
    unordered count is what a coverage set can assert.  Order-independence is
    checked separately on *every* pair in :func:`_compare_shapes`.
    """
    shapes: list[ck.KShape] = [
        ck.KSegment(0.0, 0.0, 5.0, 0.0, 0.2, 0),
        ck.KSegment(0.0, 1.0, 5.0, 1.0, 0.2, 0),
        ck.KVia(2.0, 2.0, 0.6, 0.3),
        ck.KVia(3.0, 2.0, 0.6, 0.3),
        ck.KEdge(((-1.0, -1.0), (6.0, -1.0))),
        ck.KEdge(((-1.0, 4.0), (6.0, 4.0))),
        ck.make_pad("roundrect", 1.0, 1.0, 0.25, 30.0, 1.0, 2.5, 0, 0.0),
        ck.make_pad("circle", 0.9, 0.9, 0.25, 0.0, 4.0, 2.5, ck.ALL_LAYERS, 0.4),
        ck.KZonePoly(
            rings=(((-2.0, 5.0), (7.0, 5.0), (7.0, 7.0), (-2.0, 7.0), (-2.0, 5.0)),),
            layer=0,
        ),
        ck.KZonePoly(
            rings=(
                ((-2.0, 8.0), (7.0, 8.0), (7.0, 12.0), (-2.0, 12.0), (-2.0, 8.0)),
                ((1.0, 9.0), (4.0, 9.0), (4.0, 11.0), (1.0, 11.0), (1.0, 9.0)),
            ),
            layer=0,
        ),
    ]
    seen = {
        tuple(sorted((_kind(a), _kind(b)))) for i, a in enumerate(shapes) for b in shapes[i + 1 :]
    }
    expected = {tuple(sorted((x, y))) for i, x in enumerate(PAIR_KINDS) for y in PAIR_KINDS[i:]}
    assert len(expected) == 15
    assert seen == expected, f"uncovered pair kinds: {sorted(expected - seen)}"

    stats = _compare_shapes(shapes, 0.25)
    assert int(stats["kinds"]) == len(expected)


@requires_cpp
def test_make_pad_agrees_on_the_core() -> None:
    """``make_pad`` itself is a port, so its output is compared, not assumed.

    :func:`_cpp_pad` deliberately twins the *Python* core rather than calling
    the C++ ``make_pad``, so a divergence in the shape branches or the rotation
    sign could not show up as a gap mismatch.  This is where it would.
    """
    from kicad_tools.router import router_cpp

    catalogue = [(s.shape, s.size, s.roundrect_rratio) for s in PAD_SHAPES]
    catalogue += [
        ("trapezoid", (1.1, 0.7), None),
        ("obround", (0.8, 0.8), None),
        ("roundrect", (1.0, 1.0), 0.0),
        ("roundrect", (1.0, 1.0), 0.5),
        ("roundrect", (1.4, 0.8), 0.5),
    ]
    for shape_kw, (w, h), rratio in catalogue:
        rr = 0.25 if rratio is None else rratio
        for rotation in (0.0, 45.0, 90.0, 137.5, 270.0, -33.25):
            py = ck.make_pad(shape_kw, w, h, rr, rotation, 3.0, -4.0, 1, 0.3)
            cpp = router_cpp.make_pad(shape_kw, w, h, rr, rotation, 3.0, -4.0, 1, 0.3)
            label = f"{shape_kw} @ {rotation} deg"
            assert cpp.corner_radius == pytest.approx(py.corner_radius, abs=1e-15), label
            assert len(cpp.core) == len(py.core), f"{label}: core vertex count differs"
            for (pxv, pyv), (cxv, cyv) in zip(py.core, cpp.core, strict=True):
                assert cxv == pytest.approx(pxv, abs=GAP_TOLERANCE_MM), label
                assert cyv == pytest.approx(pyv, abs=GAP_TOLERANCE_MM), label
            assert cpp.cx == pytest.approx(py.cx) and cpp.cy == pytest.approx(py.cy)
            assert cpp.drill == pytest.approx(py.drill)
            assert cpp.layer == py.layer


@requires_cpp
def test_pad_outline_agrees_vertex_for_vertex() -> None:
    """Both ports tessellate a pad arc into the *same* vertex list.

    The segment count per arc is derived from the chord-error rule rather than
    chosen per side, so the lists must match in length as well as in position.
    A length mismatch is the drift this assertion exists to catch.
    """
    from kicad_tools.router import router_cpp

    catalogue = [(s.shape, s.size, s.roundrect_rratio) for s in PAD_SHAPES]
    catalogue += [
        ("trapezoid", (1.1, 0.7), None),
        # rratio 0 -> a plain rectangle; 0.5 (KiCad's maximum) -> the inner box
        # collapses to a point and the pad is a true disc.  Both are edge cases
        # of the ``roundrect`` branch and both must still produce a valid ring.
        ("roundrect", (1.0, 1.0), 0.0),
        ("roundrect", (1.0, 1.0), 0.5),
        ("roundrect", (1.4, 0.8), 0.5),
    ]
    for shape_kw, (w, h), rratio in catalogue:
        rr = 0.25 if rratio is None else rratio
        for rotation in (0.0, 45.0, 137.5):
            py = ck.pad_outline(shape_kw, w, h, rr, rotation, 2.0, 1.0)
            cpp = router_cpp.pad_outline(shape_kw, w, h, rr, rotation, 2.0, 1.0)
            label = f"{shape_kw} @ {rotation} deg"
            assert len(py) == len(cpp), f"{label}: {len(py)} vs {len(cpp)} vertices"
            for (pxv, pyv), (cxv, cyv) in zip(py, cpp, strict=True):
                assert cxv == pytest.approx(pxv, abs=GAP_TOLERANCE_MM), label
                assert cyv == pytest.approx(pyv, abs=GAP_TOLERANCE_MM), label
            # Closed ring, and every vertex on or inside the true outline: the
            # tessellation is *inscribed*, so it may under-reach by at most the
            # chord error and must never over-reach.
            assert py[0] == py[-1], f"{label}: outline is not closed"
            pad = ck.make_pad(shape_kw, w, h, rr, rotation, 2.0, 1.0)
            for vx, vy in py:
                reach = -ck.copper_gap(pad, ck.KVia(vx, vy, 0.0, 0.0))
                assert reach >= -1e-12, f"{label}: vertex outside the pad copper"
                assert reach <= ck.ARC_CHORD_ERROR_MM + 1e-12, (
                    f"{label}: vertex {reach:.3e} mm inside the outline, over the chord budget"
                )


@requires_cpp
def test_zone_interior_rings_are_not_copper() -> None:
    """A hole in a pour is not copper -- on both sides, with the same numbers.

    ``KZonePoly`` exists to carry interior rings; a port that ignored them
    would still agree with itself on every *outer*-ring pair, which is exactly
    why this is asserted directly rather than left to the random corpus.
    """
    from kicad_tools.router import router_cpp

    outer = ((0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0), (0.0, 0.0))
    hole = ((4.0, 4.0), (6.0, 4.0), (6.0, 6.0), (4.0, 6.0), (4.0, 4.0))
    solid = ck.KZonePoly(rings=(outer,), layer=0)
    holed = ck.KZonePoly(rings=(outer, hole), layer=0)

    # A via at the hole's centre: buried in copper in the solid pour, 1.0 mm
    # from the hole wall (minus its own radius) in the holed one.
    via = ck.KVia(5.0, 5.0, 0.6, 0.3)
    assert ck.copper_gap(solid, via) == pytest.approx(-0.3, abs=1e-12)
    assert ck.copper_gap(holed, via) == pytest.approx(1.0 - 0.3, abs=1e-12)

    for zone, expected in ((solid, -0.3), (holed, 0.7)):
        assert router_cpp.copper_gap(_to_cpp(zone), _to_cpp(via)) == pytest.approx(
            expected, abs=1e-12
        )

    # And the verdict flips with it at a 0.5 mm requirement.
    assert ck.clear(solid, via, 0.5) is False
    assert ck.clear(holed, via, 0.5) is True
    assert router_cpp.clear(_to_cpp(solid), _to_cpp(via), 0.5) is False
    assert router_cpp.clear(_to_cpp(holed), _to_cpp(via), 0.5) is True


@requires_cpp
def test_kernel_matches_reference_arithmetic() -> None:
    """Sanity anchors: the kernel's numbers are the ones a human would write.

    Parity alone cannot catch "both ports are wrong the same way", so a few
    closed-form values are pinned independently of either implementation.
    """
    from kicad_tools.router import router_cpp

    # Two parallel 0.15 mm tracks, centrelines 0.35 mm apart -> 0.20 mm gap.
    a = ck.KSegment(0.0, 0.0, 10.0, 0.0, 0.15, 0)
    b = ck.KSegment(0.0, 0.35, 10.0, 0.35, 0.15, 0)
    assert ck.copper_gap(a, b) == pytest.approx(0.20, abs=1e-12)
    assert router_cpp.copper_gap(_to_cpp(a), _to_cpp(b)) == pytest.approx(0.20, abs=1e-12)

    # Different layers -> no interaction at all.
    c = ck.KSegment(0.0, 0.35, 10.0, 0.35, 0.15, 1)
    assert math.isinf(ck.copper_gap(a, c))
    assert math.isinf(router_cpp.copper_gap(_to_cpp(a), _to_cpp(c)))

    # Crossing tracks overlap: negative gap equal to minus the half-widths.
    d = ck.KSegment(5.0, -1.0, 5.0, 1.0, 0.15, 0)
    assert ck.copper_gap(a, d) == pytest.approx(-0.15, abs=1e-12)

    # Via pair: centres 1.0 mm apart, 0.6 copper / 0.3 drill.
    v1 = ck.KVia(0.0, 0.0, 0.6, 0.3)
    v2 = ck.KVia(1.0, 0.0, 0.6, 0.3)
    assert ck.copper_gap(v1, v2) == pytest.approx(0.4, abs=1e-12)
    assert ck.hole_gap(v1, v2) == pytest.approx(0.7, abs=1e-12)

    # A hole-free via has no hole query against a track.
    assert math.isinf(ck.hole_gap(ck.KVia(0.0, 0.0, 0.6, 0.0), a))

    # Board outline: track centreline 2.0 mm away, half width 0.075.
    edge = ck.KEdge(((-5.0, 2.0), (15.0, 2.0)))
    assert ck.copper_gap(a, edge) == pytest.approx(2.0 - 0.075, abs=1e-12)
    assert math.isinf(ck.copper_gap(edge, ck.KEdge(((0.0, 0.0), (1.0, 0.0)))))

    # A 1.0 mm circular pad is a disc: reach 0.5 mm in every direction, so a
    # via 2.0 mm away with a 0.6 copper pad leaves 2.0 - 0.5 - 0.3.
    disc = ck.make_pad("circle", 1.0, 1.0, 0.25, 0.0, 0.0, 5.0, 0, 0.5)
    v3 = ck.KVia(2.0, 5.0, 0.6, 0.3)
    assert ck.copper_gap(disc, v3) == pytest.approx(2.0 - 0.5 - 0.3, abs=1e-12)
    assert router_cpp.copper_gap(_to_cpp(disc), _to_cpp(v3)) == pytest.approx(1.2, abs=1e-12)
    # ... and its drill-to-drill reading uses the two drills, not the coppers.
    assert ck.hole_gap(disc, v3) == pytest.approx(2.0 - 0.25 - 0.15, abs=1e-12)

    # A 1.2 x 0.8 rect pad, unrotated: reach 0.6 along +X.
    rect = ck.make_pad("rect", 1.2, 0.8, 0.25, 0.0, 0.0, 10.0, 0)
    assert ck.copper_gap(rect, ck.KVia(2.0, 10.0, 0.6, 0.3)) == pytest.approx(
        2.0 - 0.6 - 0.3, abs=1e-12
    )
    # An SMD pad has no hole at all.
    assert math.isinf(ck.hole_gap(rect, ck.KVia(2.0, 10.0, 0.6, 0.0)))

    # A roundrect at KiCad's maximum rratio 0.5 IS a disc -- the inner box
    # collapses -- so it must read exactly like the circle above.
    maxed = ck.make_pad("roundrect", 1.0, 1.0, 0.5, 33.0, 0.0, 5.0, 0)
    assert len(maxed.core) == 1
    assert maxed.corner_radius == pytest.approx(0.5, abs=1e-15)
    assert ck.copper_gap(maxed, v3) == pytest.approx(1.2, abs=1e-12)
    # rratio 0 is the opposite end: a plain 4-vertex rectangle, no rounding.
    flat = ck.make_pad("roundrect", 1.0, 1.0, 0.0, 0.0, 0.0, 5.0, 0)
    assert len(flat.core) == 4
    assert flat.corner_radius == 0.0

    # A 1.6 x 0.8 oval is a stadium: 0.8 along its long axis, 0.4 across.
    oval = ck.make_pad("oval", 1.6, 0.8, 0.25, 0.0, 0.0, 15.0, 0)
    assert ck.copper_gap(oval, ck.KVia(2.0, 15.0, 0.0, 0.0)) == pytest.approx(2.0 - 0.8, abs=1e-12)
    assert ck.copper_gap(oval, ck.KVia(0.0, 17.0, 0.0, 0.0)) == pytest.approx(2.0 - 0.4, abs=1e-12)

    # A pour is copper with no width of its own: a track whose centreline is
    # 1.0 mm outside the fill edge leaves 1.0 - half width.
    zone = ck.KZonePoly(
        rings=(((0.0, 20.0), (10.0, 20.0), (10.0, 30.0), (0.0, 30.0), (0.0, 20.0)),),
        layer=0,
    )
    track = ck.KSegment(-1.0, 20.0, -1.0, 30.0, 0.2, 0)
    assert ck.copper_gap(zone, track) == pytest.approx(1.0 - 0.1, abs=1e-12)
    # Different layer -> no interaction, pours included.
    assert math.isinf(ck.copper_gap(zone, ck.KSegment(-1.0, 20.0, -1.0, 30.0, 0.2, 1)))
    # A pour is never drilled.
    assert math.isinf(ck.hole_gap(zone, track))


@requires_cpp
def test_clear_uses_the_same_epsilon_on_both_sides() -> None:
    """``clear`` accepts exactly down to ``required - CLEARANCE_EPSILON_MM``."""
    from kicad_tools.router import router_cpp

    assert router_cpp.CLEARANCE_EPSILON_MM == ck.CLEARANCE_EPSILON_MM

    required = 0.2
    eps = ck.CLEARANCE_EPSILON_MM
    for gap, expected in ((required, True), (required - eps, True), (required - 2 * eps, False)):
        # Two parallel 0.2 mm tracks placed to realise ``gap`` exactly.
        offset = gap + 0.2
        a = ck.KSegment(0.0, 0.0, 10.0, 0.0, 0.2, 0)
        b = ck.KSegment(0.0, offset, 10.0, offset, 0.2, 0)
        assert ck.clear(a, b, required) is expected, f"python clear() wrong at gap={gap}"
        assert router_cpp.clear(_to_cpp(a), _to_cpp(b), required) is expected, (
            f"cpp clear() wrong at gap={gap}"
        )


# ---------------------------------------------------------------------------
# Indexed-consumer primitives (Phase 3f)
# ---------------------------------------------------------------------------


def _random_ring(rng: random.Random) -> tuple[tuple[float, float], ...]:
    """A closed, non-convex ring: a star polygon with jittered radii."""
    cx, cy = rng.uniform(-5.0, 5.0), rng.uniform(-5.0, 5.0)
    n = rng.randint(5, 14)
    ring = [
        (
            cx + rng.uniform(1.0, 4.0) * math.cos(2.0 * math.pi * i / n),
            cy + rng.uniform(1.0, 4.0) * math.sin(2.0 * math.pi * i / n),
        )
        for i in range(n)
    ]
    return (*ring, ring[0])


def test_the_per_edge_steps_rebuild_copper_gap_exactly() -> None:
    """The equivalence Phase 3f's indexed consumers rest on.

    ``copper_gap_ring_edge`` and ``ring_edge_crosses_ray`` exist so a consumer
    with a spatial index over a pour's edges (``Grid3D``'s 1 mm bins and their
    Python twin) can ask the kernel per edge instead of per pour.  That is only
    legitimate if the minimum over every edge, plus the parity over the same
    edges, *is* ``copper_gap(seg, zone)``.  Asserted here on the kernel itself,
    so a future kernel edit that broke it fails in the kernel's own suite and
    not only in a consumer's.
    """
    rng = random.Random(90210)
    for _ in range(200):
        rings = (_random_ring(rng),)
        if rng.random() < 0.3:
            rings = (*rings, _random_ring(rng))
        zone = ck.KZonePoly(rings=rings, layer=0)
        seg = ck.KSegment(
            rng.uniform(-9.0, 9.0),
            rng.uniform(-9.0, 9.0),
            rng.uniform(-9.0, 9.0),
            rng.uniform(-9.0, 9.0),
            rng.uniform(0.05, 0.4),
            0,
        )

        # The decomposition, walked by hand: parity over both endpoints (what
        # ``_region_segment_distance`` tests) and the minimum over every edge.
        at_start = at_end = False
        best = math.inf
        for ring in rings:
            for i in range(1, len(ring)):
                ax, ay = ring[i - 1]
                bx, by = ring[i]
                best = min(best, ck.copper_gap_ring_edge(seg, ax, ay, bx, by))
                if ck.ring_edge_crosses_ray(seg.x1, seg.y1, ax, ay, bx, by):
                    at_start = not at_start
                if ck.ring_edge_crosses_ray(seg.x2, seg.y2, ax, ay, bx, by):
                    at_end = not at_end

        # A query with an endpoint inside the copper reads zero centreline
        # distance, which is ``-half`` edge to edge.
        decomposed = -seg.width / 2.0 if (at_start or at_end) else best
        assert ck.copper_gap(zone, seg) == pytest.approx(decomposed, abs=1e-12)


@requires_cpp
def test_indexed_consumer_primitives_agree_across_the_ports() -> None:
    """``copper_gap_ring_edge`` / ``ring_edge_crosses_ray``, py vs cpp.

    New kernel API carries the same port contract as the rest of it: the C++
    side is the model and the Python module is a line-for-line port, so both
    are driven from the same numbers and compared at :data:`GAP_TOLERANCE_MM`.
    """
    from kicad_tools.router import router_cpp

    rng = random.Random(4242)
    for _ in range(500):
        seg = ck.KSegment(
            rng.uniform(-9.0, 9.0),
            rng.uniform(-9.0, 9.0),
            rng.uniform(-9.0, 9.0),
            rng.uniform(-9.0, 9.0),
            rng.uniform(0.0, 0.5),
            rng.choice([-1, 0, 1]),
        )
        ax, ay = rng.uniform(-9.0, 9.0), rng.uniform(-9.0, 9.0)
        bx, by = rng.uniform(-9.0, 9.0), rng.uniform(-9.0, 9.0)

        py_gap = ck.copper_gap_ring_edge(seg, ax, ay, bx, by)
        cpp_gap = router_cpp.copper_gap_ring_edge(_to_cpp(seg), ax, ay, bx, by)
        assert cpp_gap == pytest.approx(py_gap, abs=GAP_TOLERANCE_MM)

        px, py_ = rng.uniform(-9.0, 9.0), rng.uniform(-9.0, 9.0)
        assert ck.ring_edge_crosses_ray(px, py_, ax, ay, bx, by) == bool(
            router_cpp.ring_edge_crosses_ray(px, py_, ax, ay, bx, by)
        )


def test_the_per_edge_gap_is_the_zone_reading_with_no_pour_width() -> None:
    """A closed-form anchor, independent of either implementation.

    A pour carries no width of its own, so the gap to one of its boundary
    edges is the centreline distance less the *segment's* half width -- the
    same reading ``copper_gap(KZonePoly, KSegment)`` gives.
    """
    seg = ck.KSegment(0.0, 0.0, 0.0, 10.0, 0.2, 0)
    assert ck.copper_gap_ring_edge(seg, 1.0, 0.0, 1.0, 10.0) == pytest.approx(0.9, abs=1e-12)
    # A ray that leaves the edge's y-span behind cannot cross it, whichever
    # way round the edge is written.
    assert ck.ring_edge_crosses_ray(0.0, 5.0, 1.0, 0.0, 1.0, 10.0) is True
    assert ck.ring_edge_crosses_ray(0.0, 5.0, 1.0, 10.0, 1.0, 0.0) is True
    assert ck.ring_edge_crosses_ray(0.0, 50.0, 1.0, 0.0, 1.0, 10.0) is False
    assert ck.ring_edge_crosses_ray(2.0, 5.0, 1.0, 0.0, 1.0, 10.0) is False


def test_cpp_kernel_present_in_ci() -> None:
    """The parity suite above must never be silently skipped in CI.

    The ``test`` job builds the native extension before running pytest, so a
    missing backend there means that step regressed -- and every
    ``requires_cpp`` test in this module would have skipped green.
    """
    if os.environ.get("CI") and not is_cpp_available():
        pytest.fail("router_cpp not built in CI test job -- kernel parity silently skipped")


MIGRATED_KERNEL_CALLERS: frozenset[str] = frozenset(
    {
        # Epic #5509 Phase 3c (#5662), consumer groups 7 and 8: the coupled
        # diff-pair search path.  ``clearance_shapes.py`` is the one
        # router-primitive -> kernel-shape translation every migrated Python
        # consumer shares, so two of them cannot end up disagreeing about the
        # copper rather than about the clearance; ``diffpair_routing.py``
        # reaches the kernel through it and is listed because its docstrings
        # cite the kernel by name (this check is a plain text scan).
        "router/clearance_shapes.py",
        "router/diffpair_routing.py",
        # Epic #5509 Phase 3d (#5663), consumer group 9: the lattice engine.
        # The adapter is the package's single import site on purpose -- the
        # lattice is geometry-only in an integer net-id space and the kernel is
        # net-agnostic, so one module owns the projection and every predicate
        # in ``router/lattice/`` goes through it.
        "router/lattice/kernel_adapter.py",
        # Epic #5509 Phase 3f (#5665), consumer group 6: the fixed-copper
        # predicate.  Same shape of migration -- one adapter module owns the
        # projection (shapely fills -> kernel ring sets) and the spatial index
        # the query path needs, and ``router/fixed_copper.py`` calls it.  Every
        # other fixed-copper caller in the package (``pathfinder``'s
        # ``_fixed_step_clear``, ``diffpair_routing``, ``pad_access``,
        # ``cpp_backend``) delegates to ``FixedFillObstacles`` and composes no
        # gap of its own, so it is switched without referencing the kernel.
        "router/fixed_copper_kernel.py",
    }
)
"""Python modules allowed to reference the kernel, one entry per migration.

Phases 2-4 switch consumers over **deliberately, one at a time**, each with
its own before/after measurement.  This set is what keeps "deliberately"
checkable: a module that starts calling the kernel without being added here
fails :func:`test_only_migrated_consumers_reference_the_kernel`, and an entry
added without a migration is dead weight a reviewer can see.

Keep it in step with ``tests/conformance/report.MIGRATED_GROUPS``, which flips
the same migration's conformance rows from report-only to gated.
"""


def test_only_migrated_consumers_reference_the_kernel() -> None:
    """The kernel reaches a consumer only where a phase deliberately wired it.

    Succeeds ``test_no_consumer_switched_to_the_kernel`` (Phase 1b, when the
    right answer was "none at all").  The property is unchanged -- an
    *accidental* import would destroy a consumer's before/after measurement --
    but the expected set is now an enumerated allowlist rather than the empty
    set, because Phases 3c and 3d really did switch three modules.
    """
    repo_root = Path(__file__).resolve().parents[2]
    src = repo_root / "src" / "kicad_tools"
    hits: list[str] = []
    for path in sorted(src.rglob("*.py")):
        rel = path.relative_to(src).as_posix()
        if rel == "router/clearance_kernel.py" or rel in MIGRATED_KERNEL_CALLERS:
            continue
        if "clearance_kernel" in path.read_text(encoding="utf-8"):
            hits.append(rel)
    assert hits == [], (
        "clearance_kernel is referenced by a consumer that no epic phase has "
        f"switched; add it to MIGRATED_KERNEL_CALLERS with its phase, or revert: {hits}"
    )

    stale = sorted(rel for rel in MIGRATED_KERNEL_CALLERS if not (src / rel).exists())
    assert stale == [], f"MIGRATED_KERNEL_CALLERS names modules that no longer exist: {stale}"
    silent = sorted(
        rel
        for rel in MIGRATED_KERNEL_CALLERS
        if "clearance_kernel" not in (src / rel).read_text(encoding="utf-8")
    )
    assert silent == [], (
        "MIGRATED_KERNEL_CALLERS names modules that do not reference the "
        f"kernel at all -- the migration was reverted or never landed: {silent}"
    )


MIGRATED_CPP_KERNEL_CALLERS: frozenset[str] = frozenset(
    {
        # Epic #5509 Phase 3f (#5665), consumer group 6: the native half of the
        # fixed-copper predicate.  ``Grid3D::fixed_fill_clear`` keeps its 1 mm
        # bins -- they only *select* candidate edges, never judge one -- and
        # takes every number from ``copper_gap_ring_edge`` /
        # ``ring_edge_crosses_ray``.
        "src/grid.cpp",
        # Epic #5509 Phase 3c (#5662), consumer group 7: the coupled rail gate.
        # ``coupled_pathfinder.cpp``'s ``rail_clear`` measures a candidate rail
        # step against the grid's stored route geometry with the kernel, which
        # is what made committed copper visible to the coupled search again
        # (#4507).  The header is listed too: it documents the migration at
        # ``rail_clear``'s declaration, and this check is a text scan.
        "src/coupled_pathfinder.cpp",
        "include/coupled_pathfinder.hpp",
    }
)
"""C++ translation units allowed to reference the kernel, one per migration.

The native counterpart of :data:`MIGRATED_KERNEL_CALLERS`, kept for the same
reason: a consumer that starts calling the kernel without a phase behind it
destroys that consumer's own before/after measurement.
"""


def test_cpp_kernel_is_not_referenced_outside_its_own_translation_units() -> None:
    """The C++ kernel reaches a consumer only where a phase deliberately wired it.

    The native mirror of
    :func:`test_only_migrated_consumers_reference_the_kernel`: everything
    outside the kernel's own translation units and the enumerated migrations
    must still be kernel-free.
    """
    repo_root = Path(__file__).resolve().parents[2]
    cpp = repo_root / "src" / "kicad_tools" / "router" / "cpp"
    allowed = {
        "include/clearance_kernel.hpp",
        "src/clearance_kernel.cpp",
        "src/bindings.cpp",
    } | set(MIGRATED_CPP_KERNEL_CALLERS)
    hits = [
        p.relative_to(cpp).as_posix()
        for p in sorted([*cpp.rglob("*.cpp"), *cpp.rglob("*.hpp")])
        if "third_party" not in p.parts
        and p.relative_to(cpp).as_posix() not in allowed
        and ("clearance_kernel" in p.read_text(encoding="utf-8"))
    ]
    assert hits == [], f"C++ clearance kernel referenced by a consumer: {hits}"

    silent = sorted(
        rel
        for rel in MIGRATED_CPP_KERNEL_CALLERS
        if "clearance_kernel" not in (cpp / rel).read_text(encoding="utf-8")
    )
    assert silent == [], (
        "MIGRATED_CPP_KERNEL_CALLERS names translation units that do not "
        f"reference the kernel at all -- the migration was reverted: {silent}"
    )


def test_ripgrep_acceptance_criterion() -> None:
    """The issue's ``rg`` acceptance criterion, run as a test.

    The issue writes the command as ``rg "clearance_kernel" src/kicad_tools
    --glob '!router/clearance_kernel.py' --glob '!router/cpp/**'``.  Run from
    the repo root that excludes nothing: ripgrep anchors a glob containing a
    ``/`` to the **working directory**, not to the search path, so those two
    globs would have to read ``src/kicad_tools/router/...``.  Running from
    ``src/kicad_tools`` instead keeps the globs exactly as the issue wrote
    them and makes them mean what they say.

    One glob per entry in :data:`MIGRATED_KERNEL_CALLERS` is appended, derived
    rather than hand-listed so the ledger stays the single place a migration is
    recorded.  Excluding a migrated consumer is not weakening the criterion:
    since Phase 3c/3d the question the command asks is *"has anything reached
    the kernel that no phase signed off"*, and a deliberate migration is not a
    violation of it.

    Skipped where ``rg`` is unavailable; the two structural tests above cover
    the same property without depending on the binary.
    """
    src = Path(__file__).resolve().parents[2] / "src" / "kicad_tools"
    migrated_globs: list[str] = []
    for rel in sorted(MIGRATED_KERNEL_CALLERS):
        migrated_globs += ["--glob", f"!{rel}"]
    try:
        proc = subprocess.run(
            [
                "rg",
                "clearance_kernel",
                ".",
                "--glob",
                "!router/clearance_kernel.py",
                "--glob",
                "!router/cpp/**",
                *migrated_globs,
            ],
            cwd=src,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:  # pragma: no cover - environment dependent
        pytest.skip("rg not installed")
    assert proc.stdout.strip() == "", f"unexpected consumer references:\n{proc.stdout}"
