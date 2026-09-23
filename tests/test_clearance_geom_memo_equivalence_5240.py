"""Exact-equivalence controls for the memoised clearance copper geometry (#5240).

``ClearanceRule`` asks each :class:`CopperElement` for its shapely copper
footprint **once per candidate pair the element takes part in**.  On board
06's committed routed artifact that is 4,059 segment buffers spread over 169
distinct segments and 8,922 pad/via geometry builds over 270 distinct
elements -- i.e. the same ``LineString(...).buffer(w/2)`` and the same
``Point(...).buffer(r)`` rebuilt ~24x and ~33x respectively.

The shape is a pure function of ``CopperElement.geometry`` / ``.polygon``,
both assigned once in the ``from_*`` constructors and never reassigned, so
memoising it per element is a pure work reduction, not a different answer.

These tests pin that claim down rather than assuming it.  Every one of them
runs the **same** corpus twice -- once with ``_MEMOIZE_COPPER_GEOM = False``
(the exhaustive, pre-memo rebuild path, retained as a test oracle exactly as
PR #5641 retained ``_PRUNE_BY_BOUNDS``) and once with it ``True`` -- and
asserts the emitted violations are identical down to their ``repr`` (which
round-trips every float bit-exactly), not merely approximately equal.
"""

from __future__ import annotations

import random
from pathlib import Path

import pytest

from kicad_tools._shapely import has_shapely
from kicad_tools.schema.pcb import PCB
from kicad_tools.validate import DRCChecker
from kicad_tools.validate.rules import clearance as cl

pytestmark = pytest.mark.skipif(
    not has_shapely(), reason="exact clearance geometry requires shapely"
)


# ---------------------------------------------------------------------------
# Corpus construction
# ---------------------------------------------------------------------------

_PAD_SHAPES = ("rect", "roundrect", "oval", "circle")


def _pad(
    ref: str,
    x: float,
    y: float,
    *,
    shape: str,
    w: float,
    h: float,
    angle: float,
    fp_angle: float,
    net: int,
    layer: str,
    ratio: float,
) -> str:
    return (
        f'(footprint "P" (layer "{layer}") (at {x} {y} {fp_angle})\n'
        f'  (property "Reference" "{ref}")\n'
        f'  (pad "1" smd {shape} (at 0 0 {angle}) (size {w} {h})\n'
        f'    (layers "{layer}") (roundrect_rratio {ratio}) (net {net} "N{net}")))'
    )


def _segment(x1: float, y1: float, x2: float, y2: float, w: float, net: int, layer: str, uid: str):
    return (
        f"(segment (start {x1} {y1}) (end {x2} {y2}) (width {w}) "
        f'(layer "{layer}") (net {net}) (uuid "{uid}"))'
    )


def _via(x: float, y: float, size: float, net: int, uid: str) -> str:
    return (
        f"(via (at {x} {y}) (size {size}) (drill {size / 2}) "
        f'(layers "F.Cu" "B.Cu") (net {net}) (uuid "{uid}"))'
    )


def _random_board(rng: random.Random, tmp_path: Path, index: int) -> Path:
    """Write a dense, randomly-populated 2-layer board.

    Coordinates are drawn from a deliberately tight 4.5 x 4.5 mm window so the
    corpus is rich in *both* verdict classes the memoised geometry feeds:
    real area overlaps (the ``intersection(...).bounds`` penetration-depth
    branch) and disjoint near-misses (the ``distance(...)`` branch).
    """
    body: list[str] = []
    layers = ("F.Cu", "B.Cu")

    for i in range(rng.randint(4, 9)):
        shape = rng.choice(_PAD_SHAPES)
        w = round(rng.uniform(0.3, 1.6), 3)
        h = w if shape == "circle" else round(rng.uniform(0.3, 1.6), 3)
        body.append(
            _pad(
                f"U{i}",
                round(rng.uniform(0.0, 4.5), 3),
                round(rng.uniform(0.0, 4.5), 3),
                shape=shape,
                w=w,
                h=h,
                angle=round(rng.choice([0.0, 45.0, 90.0, 180.0, rng.uniform(0, 360)]), 3),
                fp_angle=round(rng.choice([0.0, 90.0, rng.uniform(0, 360)]), 3),
                net=rng.randint(0, 3),
                layer=rng.choice(layers),
                ratio=rng.choice([0.0, 0.1, 0.25]),
            )
        )

    for i in range(rng.randint(6, 16)):
        x1 = round(rng.uniform(0.0, 4.5), 3)
        y1 = round(rng.uniform(0.0, 4.5), 3)
        # Mix long traces, short stubs and the degenerate zero-length case
        # (which takes ``_segment_copper_geom``'s ``Point`` branch).
        kind = rng.random()
        if kind < 0.1:
            x2, y2 = x1, y1
        elif kind < 0.5:
            x2 = round(x1 + rng.uniform(-0.4, 0.4), 3)
            y2 = round(y1 + rng.uniform(-0.4, 0.4), 3)
        else:
            x2 = round(rng.uniform(0.0, 4.5), 3)
            y2 = round(rng.uniform(0.0, 4.5), 3)
        body.append(
            _segment(
                x1,
                y1,
                x2,
                y2,
                round(rng.uniform(0.1, 0.45), 3),
                rng.randint(0, 3),
                rng.choice(layers),
                f"seg-{index}-{i}",
            )
        )

    for i in range(rng.randint(3, 8)):
        body.append(
            _via(
                round(rng.uniform(0.0, 4.5), 3),
                round(rng.uniform(0.0, 4.5), 3),
                round(rng.uniform(0.3, 0.8), 3),
                rng.randint(0, 3),
                f"via-{index}-{i}",
            )
        )

    text = (
        "(kicad_pcb (version 20240108) (generator test)\n"
        '  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))\n'
        '  (net 0 "") (net 1 "N1") (net 2 "N2") (net 3 "N3")\n' + "\n".join(body) + ")\n"
    )
    path = tmp_path / f"corpus-{index}.kicad_pcb"
    path.write_text(text)
    return path


# ---------------------------------------------------------------------------
# Arms
# ---------------------------------------------------------------------------


def _violations(pcb: PCB, *, memoise: bool) -> list[str]:
    """Run ``check_clearances`` with the memo forced on or off.

    Returns the violations as ``repr`` strings: ``DRCViolation`` is a frozen
    dataclass, so its ``repr`` embeds every float via ``repr(float)``, which
    round-trips bit-exactly.  Comparing those strings is therefore an exact
    comparison, not a tolerance-based one.
    """
    previous = cl._MEMOIZE_COPPER_GEOM
    cl._MEMOIZE_COPPER_GEOM = memoise
    try:
        results = DRCChecker(pcb, manufacturer="jlcpcb", layers=4).check_clearances()
    finally:
        cl._MEMOIZE_COPPER_GEOM = previous
    return [repr(v) for v in results.violations]


def _both_arms(path: Path) -> tuple[list[str], list[str]]:
    # A fresh ``PCB`` per arm: the reference arm must not be handed elements
    # whose cache the memo arm already populated.
    reference = _violations(PCB.load(path), memoise=False)
    memoised = _violations(PCB.load(path), memoise=True)
    return reference, memoised


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_random_corpus_violations_identical(tmp_path: Path) -> None:
    """40 random dense boards: both arms emit byte-identical violation lists."""
    rng = random.Random(5240)
    total = 0
    boards_with_violations = 0
    for index in range(40):
        path = _random_board(rng, tmp_path, index)
        reference, memoised = _both_arms(path)
        assert memoised == reference, f"corpus board {index} diverged"
        total += len(reference)
        boards_with_violations += 1 if reference else 0

    # Guard against a vacuous corpus: a suite that silently generated empty
    # boards would "pass" without exercising either geometry branch.
    assert boards_with_violations >= 30, (
        f"corpus too sparse to be a control: only {boards_with_violations}/40 "
        f"boards produced violations"
    )
    assert total >= 300, f"corpus produced only {total} violations"


@pytest.mark.parametrize(
    "board_relpath",
    [
        # Null control: this board is sparse enough that the broad-phase
        # bounds pruning rejects every pair before any shapely geometry is
        # built (0 ``_segment_copper_geom`` / 0 ``_build_pad_copper_geom``
        # calls), so it proves the memo does not perturb a board that never
        # reaches the cached path.  It does NOT exercise the cache -- the two
        # dense boards below do.
        "boards/02-charlieplex-led/output/charlieplex_3x3_routed.kicad_pcb",
        # Dense, load-bearing controls.  Measured build counts on these
        # artifacts (calls / distinct elements), i.e. the redundancy the memo
        # collapses:
        #   board 04: segments 4,315 / 140   pads+vias 6,138 / 115
        #   board 06: segments 4,059 / 169   pads+vias 8,922 / 259
        "boards/04-stm32-devboard/output/stm32_devboard_routed.kicad_pcb",
        "boards/06-diffpair-test/output/diffpair_test_routed.kicad_pcb",
    ],
)
def test_committed_routed_artifact_violations_identical(board_relpath: str) -> None:
    """Real-geometry control: the committed routed artifacts agree exactly."""
    path = Path(__file__).resolve().parents[1] / board_relpath
    if not path.is_file():
        pytest.skip(f"{board_relpath} not present in this checkout")
    reference, memoised = _both_arms(path)
    assert memoised == reference


def test_segment_geom_matches_freshly_built_reference() -> None:
    """``_segment_copper_geom`` returns the pre-memo geometry, bit-for-bit."""
    from shapely.geometry import LineString, Point

    rng = random.Random(4225)
    for _ in range(200):
        x1 = rng.uniform(-50, 50)
        y1 = rng.uniform(-50, 50)
        degenerate = rng.random() < 0.15
        x2, y2 = (x1, y1) if degenerate else (rng.uniform(-50, 50), rng.uniform(-50, 50))
        width = 0.0 if rng.random() < 0.1 else rng.uniform(0.05, 1.0)

        seg = cl.CopperElement(
            element_type="segment",
            layer="F.Cu",
            net_number=1,
            geometry=(x1, y1, x2, y2, width),
            reference="Trace-test",
            net_name="N1",
        )

        # Independently re-transcribed reference (not a call into the
        # production helper), mirroring the pre-#5240 inline body.
        half = width / 2.0
        base = Point(x1, y1) if (x1 == x2 and y1 == y2) else LineString([(x1, y1), (x2, y2)])
        expected = base.buffer(half) if half > 0 else base

        first = cl._segment_copper_geom(seg)
        assert first.wkb == expected.wkb

        # Second call is served from the memo and is the SAME object -- the
        # property that makes the saving real.
        second = cl._segment_copper_geom(seg)
        assert second is first


def test_pad_and_via_geom_match_freshly_built_reference() -> None:
    """``_element_to_shapely_geom`` is unchanged by the memo, including ``None``."""
    from shapely.geometry import Point

    rng = random.Random(626)
    for _ in range(200):
        x = rng.uniform(-50, 50)
        y = rng.uniform(-50, 50)
        size = rng.uniform(0.2, 1.2)
        via = cl.CopperElement(
            element_type="via",
            layer="*",
            net_number=2,
            geometry=(x, y, size, size),
            reference="Via-test",
            net_name="N2",
        )
        expected = Point(x, y).buffer(size / 2.0)
        first = cl._element_to_shapely_geom(via)
        assert first.wkb == expected.wkb
        assert cl._element_to_shapely_geom(via) is first

    # A pad hands back its own precomputed polygon object, memo or not.
    polygon = Point(1.0, 2.0).buffer(0.5)
    pad = cl.CopperElement(
        element_type="pad",
        layer="*",
        net_number=1,
        geometry=(1.0, 2.0, 1.0, 1.0),
        reference="U1-1",
        net_name="N1",
        polygon=polygon,
        pad_shape="circle",
    )
    assert cl._element_to_shapely_geom(pad) is polygon
    assert cl._element_to_shapely_geom(pad) is polygon

    # Degenerate zero-size element: ``None`` is a RESULT, not "unset", so the
    # memo must return it without rebuilding (the sentinel's whole purpose).
    degenerate = cl.CopperElement(
        element_type="via",
        layer="*",
        net_number=3,
        geometry=(0.0, 0.0, 0.0, 0.0),
        reference="Via-zero",
        net_name="N3",
    )
    assert cl._element_to_shapely_geom(degenerate) is None
    assert degenerate._pad_geom_cache is None
    assert cl._element_to_shapely_geom(degenerate) is None


def test_reference_arm_never_populates_the_memo() -> None:
    """With the memo disabled the cache slots stay at the unset sentinel.

    This is what makes the oracle arm a genuine control: if the "off" arm
    quietly cached, the two arms would share state and the equivalence tests
    above would be comparing the memoised path against itself.
    """
    seg = cl.CopperElement(
        element_type="segment",
        layer="F.Cu",
        net_number=1,
        geometry=(0.0, 0.0, 1.0, 0.0, 0.2),
        reference="Trace-test",
        net_name="N1",
    )
    via = cl.CopperElement(
        element_type="via",
        layer="*",
        net_number=2,
        geometry=(0.0, 0.0, 0.6, 0.6),
        reference="Via-test",
        net_name="N2",
    )
    previous = cl._MEMOIZE_COPPER_GEOM
    cl._MEMOIZE_COPPER_GEOM = False
    try:
        a = cl._segment_copper_geom(seg)
        b = cl._segment_copper_geom(seg)
        c = cl._element_to_shapely_geom(via)
        d = cl._element_to_shapely_geom(via)
    finally:
        cl._MEMOIZE_COPPER_GEOM = previous

    assert seg._seg_geom_cache is cl._GEOM_UNSET
    assert via._pad_geom_cache is cl._GEOM_UNSET
    # Rebuilt each time (distinct objects) but equal geometry.
    assert a is not b and a.wkb == b.wkb
    assert c is not d and c.wkb == d.wkb


def test_memo_is_invisible_to_element_equality_and_repr() -> None:
    """Populating the cache must not change ``__eq__`` or ``__repr__``.

    ``CopperElement`` is a plain (comparable) dataclass, so a cache field
    that participated in comparison would make an element stop comparing
    equal to its own twin as soon as one of them was measured.
    """
    kwargs = {
        "element_type": "segment",
        "layer": "F.Cu",
        "net_number": 1,
        "geometry": (0.0, 0.0, 1.0, 1.0, 0.25),
        "reference": "Trace-abc",
        "net_name": "N1",
    }
    left = cl.CopperElement(**kwargs)  # type: ignore[arg-type]
    right = cl.CopperElement(**kwargs)  # type: ignore[arg-type]
    before = repr(left)

    cl._segment_copper_geom(left)

    assert left._seg_geom_cache is not cl._GEOM_UNSET
    assert right._seg_geom_cache is cl._GEOM_UNSET
    assert left == right
    assert repr(left) == before
