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

Scope: Phase 1b PR A covers ``KSegment`` / ``KVia`` / ``KEdge``.  ``KPad`` and
``KZonePoly`` arrive in the follow-up PR and extend the ``PAIR_KINDS`` table
below; corpus pads are skipped here on purpose (see ``_corpus_shapes``).

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
from tests.conformance.generator import BOUNDARY_BAND_MM, generate_case

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

# Shape kinds this PR's kernel covers.  KPad / KZonePoly extend it later.
PAIR_KINDS = ("segment", "via", "edge")


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


def _to_cpp(shape: ck.KShape):
    """Build the C++ twin of a Python kernel shape."""
    if isinstance(shape, ck.KSegment):
        return _cpp_segment(shape)
    if isinstance(shape, ck.KVia):
        return _cpp_via(shape)
    return _cpp_edge(shape)


def _kind(shape: ck.KShape) -> str:
    if isinstance(shape, ck.KSegment):
        return "segment"
    if isinstance(shape, ck.KVia):
        return "via"
    return "edge"


# ---------------------------------------------------------------------------
# Shape sources
# ---------------------------------------------------------------------------


def _corpus_shapes(seed: int) -> tuple[list[ck.KShape], float]:
    """Kernel shapes for one Phase 1a corpus case, plus its requirement.

    Pads are deliberately skipped: ``KPad`` (and the ``pad_outline`` port of
    ``validate/rules/clearance.py:_pad_polygon``) lands in the follow-up PR.
    The board outline is added as a closed ``KEdge`` so copper-to-edge pairs
    are exercised on every seed.
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

    w, h = case.width, case.height
    shapes.append(ck.KEdge(points=((0.0, 0.0), (w, 0.0), (w, h), (0.0, h), (0.0, 0.0))))
    return shapes, case.rules.project_clearance


def _random_shapes(rng: random.Random, count: int) -> list[ck.KShape]:
    """Adversarial raw shapes: crossing, overlapping and coincident geometry.

    The corpus places every pair at a positive gap near the requirement, so on
    its own it never reaches the negative-gap branch or the proper-intersection
    early-out.  These do.
    """
    shapes: list[ck.KShape] = []
    for _ in range(count):
        pick = rng.random()
        if pick < 0.45:
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
        elif pick < 0.85:
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
        else:
            n = rng.randint(1, 4)
            shapes.append(
                ck.KEdge(
                    points=tuple(
                        (round(rng.uniform(-3.0, 3.0), 6), round(rng.uniform(-3.0, 3.0), 6))
                        for _ in range(n)
                    )
                )
            )
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
    """All six unordered pair kinds this PR's kernel supports are exercised.

    A silent gap in coverage is the failure mode that makes a parity suite
    feel green while the interesting branch is never run.
    """
    rng = random.Random(0xC1EA4)
    shapes: list[ck.KShape] = [
        ck.KSegment(0.0, 0.0, 5.0, 0.0, 0.2, 0),
        ck.KSegment(0.0, 1.0, 5.0, 1.0, 0.2, 0),
        ck.KVia(2.0, 2.0, 0.6, 0.3),
        ck.KVia(3.0, 2.0, 0.6, 0.3),
        ck.KEdge(((-1.0, -1.0), (6.0, -1.0))),
        ck.KEdge(((-1.0, 4.0), (6.0, 4.0))),
    ]
    del rng
    seen = {
        tuple(sorted((_kind(a), _kind(b)))) for i, a in enumerate(shapes) for b in shapes[i + 1 :]
    }
    expected = {tuple(sorted((x, y))) for i, x in enumerate(PAIR_KINDS) for y in PAIR_KINDS[i:]}
    assert seen == expected, f"uncovered pair kinds: {sorted(expected - seen)}"

    stats = _compare_shapes(shapes, 0.25)
    assert int(stats["kinds"]) == len(expected)


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


def test_cpp_kernel_present_in_ci() -> None:
    """The parity suite above must never be silently skipped in CI.

    The ``test`` job builds the native extension before running pytest, so a
    missing backend there means that step regressed -- and every
    ``requires_cpp`` test in this module would have skipped green.
    """
    if os.environ.get("CI") and not is_cpp_available():
        pytest.fail("router_cpp not built in CI test job -- kernel parity silently skipped")


def test_no_consumer_switched_to_the_kernel() -> None:
    """Phase 1b adds the kernel; it must not wire it into any consumer.

    Phases 2-4 switch consumers over deliberately, one at a time, each with
    its own before/after measurement.  An accidental import here would make
    that measurement impossible -- so the acceptance criterion is a test, not
    a grep someone remembers to run.
    """
    repo_root = Path(__file__).resolve().parents[2]
    src = repo_root / "src" / "kicad_tools"
    hits: list[str] = []
    for path in sorted(src.rglob("*.py")):
        rel = path.relative_to(src).as_posix()
        if rel == "router/clearance_kernel.py":
            continue
        if "clearance_kernel" in path.read_text(encoding="utf-8"):
            hits.append(rel)
    assert hits == [], (
        f"clearance_kernel is referenced by a consumer, but Phase 1b switches none: {hits}"
    )


def test_cpp_kernel_is_not_referenced_outside_its_own_translation_units() -> None:
    """The C++ kernel is likewise only registered, never called by the router."""
    repo_root = Path(__file__).resolve().parents[2]
    cpp = repo_root / "src" / "kicad_tools" / "router" / "cpp"
    allowed = {"include/clearance_kernel.hpp", "src/clearance_kernel.cpp", "src/bindings.cpp"}
    hits = [
        p.relative_to(cpp).as_posix()
        for p in sorted([*cpp.rglob("*.cpp"), *cpp.rglob("*.hpp")])
        if "third_party" not in p.parts
        and p.relative_to(cpp).as_posix() not in allowed
        and ("clearance_kernel" in p.read_text(encoding="utf-8"))
    ]
    assert hits == [], f"C++ clearance kernel referenced by a consumer: {hits}"


def test_ripgrep_acceptance_criterion() -> None:
    """The issue's ``rg`` acceptance criterion, run as a test.

    The issue writes the command as ``rg "clearance_kernel" src/kicad_tools
    --glob '!router/clearance_kernel.py' --glob '!router/cpp/**'``.  Run from
    the repo root that excludes nothing: ripgrep anchors a glob containing a
    ``/`` to the **working directory**, not to the search path, so those two
    globs would have to read ``src/kicad_tools/router/...``.  Running from
    ``src/kicad_tools`` instead keeps the globs exactly as the issue wrote
    them and makes them mean what they say.

    Skipped where ``rg`` is unavailable; the two structural tests above cover
    the same property without depending on the binary.
    """
    src = Path(__file__).resolve().parents[2] / "src" / "kicad_tools"
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
            ],
            cwd=src,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:  # pragma: no cover - environment dependent
        pytest.skip("rg not installed")
    assert proc.stdout.strip() == "", f"unexpected consumer references:\n{proc.stdout}"
