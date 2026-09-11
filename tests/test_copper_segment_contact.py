"""Physical copper contact must survive geometry-preserving track splits (#5060).

Board 09 exposed the defect: native KiCad DRC reported zero violations, yet
``compare_copper_netlist()`` reported five ``open`` findings on ``+3V3`` and
``PMOS_SOURCE`` — at ordinary T-junctions and at SOIC pads straddled by the
*interior* of a continuous wide trace.  Splitting those very traces at the
branch / pad positions changed the buffered copper union by 0.0 mm² yet made
copper LVS pass, which is impossible for a sound physical model.

The invariant these tests pin down: **replacing a straight segment with
collinear subsegments whose copper union is unchanged must leave the pad
partition unchanged** — in both directions, and without hiding shorts.
"""

from pathlib import Path

import pytest

from kicad_tools.geometry.copper import segment_copper_polygon
from kicad_tools.lvs.copper_lvs import compare_partitions
from kicad_tools.schema.pcb import PCB
from kicad_tools.validate.connectivity import ConnectivityValidator

unary_union = pytest.importorskip("shapely.ops").unary_union

BOARD09 = Path(__file__).parents[1] / "boards/09-usbc-pd-power"

#: The exact false opens the unsplit board-09 snapshot reported before #5060.
BOARD09_FALSE_OPENS = {
    ("+3V3", "C12.1", "R12.1"),
    ("+3V3", "R12.1", "R17.1"),
    ("PMOS_SOURCE", "C4.1", "Q1.1"),
    ("PMOS_SOURCE", "Q1.1", "Q1.2"),
    ("PMOS_SOURCE", "Q1.2", "Q2.2"),
}


def _subdivide(
    start: tuple[float, float], end: tuple[float, float], parts: int
) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    """Split ``start-end`` into ``parts`` collinear pieces (same copper union)."""
    points = [
        tuple(a + (b - a) * (i / parts) for a, b in zip(start, end, strict=True))
        for i in range(parts + 1)
    ]
    return list(zip(points, points[1:], strict=False))


def _partition(
    tmp_path: Path,
    pads,
    tracks,
    *,
    splits: int = 1,
    reverse: bool = False,
    via: bool = False,
):
    """Build a synthetic PCB and return (normalized partition, copper by layer).

    All copper deliberately carries ONE declared net so that nothing in the
    extraction can lean on net labels; the schematic identity used for the
    LVS diff is supplied separately by the caller.
    """
    items = []
    for ref, x, y, layer, shape, size in pads:
        items.append(
            f'(footprint "Test:Pad" (layer "F.Cu") (at {x} {y})\n'
            f'  (property "Reference" "{ref}" (at 0 0) (layer "F.SilkS"))\n'
            f'  (pad "1" smd {shape} (at 0 0) (size {size} {size})\n'
            f'    (layers "{layer}") (net 1 "DECLARED")))'
        )
    metal = []
    for start, end, width, layer in tracks:
        for a, b in _subdivide(start, end, splits):
            if reverse:
                a, b = b, a
            metal.append((a, b, width, layer))
    if reverse:
        metal.reverse()
    for (x1, y1), (x2, y2), width, layer in metal:
        items.append(
            f"(segment (start {x1} {y1}) (end {x2} {y2})\n"
            f'  (width {width}) (layer "{layer}") (net 1))'
        )
    if via:
        items.append('(via (at 5 0) (size 0.8) (drill 0.4) (layers "F.Cu" "B.Cu") (net 1))')
    path = tmp_path / "contact.kicad_pcb"
    path.write_text(
        '(kicad_pcb (version 20240108) (generator "test")\n'
        '  (general (thickness 1.6)) (layers (0 "F.Cu" signal) (31 "B.Cu" signal))\n'
        '  (net 0 "") (net 1 "DECLARED")\n' + "\n".join(items) + ")"
    )
    partition = set(ConnectivityValidator(PCB.load(path)).extract_pad_partition())
    copper = {
        layer: unary_union(
            [segment_copper_polygon(a, b, w) for a, b, w, lay in metal if lay == layer]
        )
        for layer in {t[3] for t in metal}
    }
    return partition, copper


def _pad(ref, x, y, layer="F.Cu", shape="rect", size=0.6):
    return ref, x, y, layer, shape, size


def _assert_same_copper(before: dict, after: dict) -> None:
    """The split must be geometry-preserving: identical buffered copper union."""
    assert before.keys() == after.keys()
    for layer, geom in before.items():
        assert geom.symmetric_difference(after[layer]).area == pytest.approx(0.0, abs=1e-12), (
            f"split changed the copper union on {layer}"
        )


# ---------------------------------------------------------------------------
# Positives: real contact, invariant under geometry-preserving splits
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("splits", [1, 4, 16])
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize(
    "contact",
    ["tee", "tee_edge", "inline", "inline_edge", "side", "crossing", "point"],
)
def test_split_invariant_contact(tmp_path, contact, reverse, splits):
    """Every physical-contact topology yields the SAME partition, split or not.

    ``tee`` / ``tee_edge`` / ``crossing`` carry NO pad at the junction itself
    — the branch is joined to the trunk purely by copper overlap.
    """
    pads = [_pad("A", 0, 0), _pad("B", 10, 0)]
    tracks = [((0, 0), (10, 0), 0.6, "F.Cu")]
    if contact == "tee":
        # Branch centerline lands on the trunk centerline, no pad at (5, 0).
        pads.append(_pad("C", 5, 5))
        tracks.append(((5, 0), (5, 5), 0.4, "F.Cu"))
    elif contact == "tee_edge":
        # Branch stops at the trunk's copper EDGE (y = 0.3), not its center.
        pads.append(_pad("C", 5, 5))
        tracks.append(((5, 0.3), (5, 5), 0.4, "F.Cu"))
    elif contact == "inline":
        # Pad straddled by the trunk's interior, far from either vertex.
        pads.append(_pad("C", 5, 0))
    elif contact == "inline_edge":
        # Inline pad offset so only its copper (not its center) is touched.
        pads.append(_pad("C", 5, 0.4))
    elif contact == "side":
        # Parallel run overlapping the trunk side-on: no shared endpoint at all.
        pads.append(_pad("C", 7, 0.5))
        tracks.append(((3, 0.5), (7, 0.5), 0.6, "F.Cu"))
    elif contact == "crossing":
        # Same-layer X crossover: copper genuinely fused, no endpoint shared.
        pads.append(_pad("C", 5, 5))
        tracks.append(((5, -5), (5, 5), 0.4, "F.Cu"))
    else:  # "point" — zero-length track (a round copper land)
        pads.append(_pad("C", 5, 0))
        tracks.append(((5, 0), (5, 0), 0.4, "F.Cu"))

    before, before_copper = _partition(tmp_path, pads, tracks, reverse=reverse)
    after, after_copper = _partition(tmp_path, pads, tracks, splits=splits, reverse=reverse)
    _assert_same_copper(before_copper, after_copper)

    expected = {frozenset({"A.1", "B.1", "C.1"})}
    assert before == expected, f"unsplit copper misread as {before}"
    assert after == expected, f"split copper misread as {after}"

    # Physical contact is a SHORT when the schematic disagrees, even though
    # every piece of copper on the board is labelled with the same net.
    result = compare_partitions(
        {("A", "1"): "SIG", ("B", "1"): "SIG", ("C", "1"): "OTHER"}, list(before)
    )
    assert len(result.shorts) == 1
    assert not result.opens


def test_repeated_splits_are_idempotent(tmp_path):
    """Splitting an already-split trace again changes nothing."""
    pads = [_pad("A", 0, 0), _pad("B", 10, 0), _pad("C", 5, 5)]
    tracks = [((0, 0), (10, 0), 0.6, "F.Cu"), ((5, 0), (5, 5), 0.4, "F.Cu")]
    partitions = [_partition(tmp_path, pads, tracks, splits=n)[0] for n in (1, 2, 3, 5, 8)]
    assert all(p == partitions[0] for p in partitions)
    assert partitions[0] == {frozenset({"A.1", "B.1", "C.1"})}


# ---------------------------------------------------------------------------
# Negatives: near misses, layer gaps and foreign copper stay open
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("splits", [1, 4])
@pytest.mark.parametrize(
    "kind", ["gap", "layer", "round_corner", "pad_gap", "smd_other_layer", "parallel_gap"]
)
def test_split_does_not_create_contact(tmp_path, kind, splits):
    """A positive copper gap stays open — splitting either segment cannot fuse it."""
    pads = [_pad("A", 0, 0), _pad("B", 10, 0)]
    tracks = [((0, 0), (10, 0), 0.6, "F.Cu")]
    if kind == "gap":
        # Branch endpoint 1 µm clear of the trunk's copper edge.
        pads.append(_pad("C", 5, 5))
        tracks.append(((5, 0.601), (5, 5), 0.6, "F.Cu"))
    elif kind == "parallel_gap":
        # Parallel run held off by a DRC-legal 0.2 mm clearance for its
        # whole length: no endpoint anywhere near the trunk.
        pads.append(_pad("C", 7, 0.8))
        tracks.append(((3, 0.8), (7, 0.8), 0.6, "F.Cu"))
    elif kind == "layer":
        # Cross-layer overlap with no via/PTH bridge.
        pads.append(_pad("C", 5, 5, "B.Cu"))
        tracks.append(((5, 0), (5, 5), 0.6, "B.Cu"))
    elif kind == "smd_other_layer":
        # An F.Cu-only SMD pad sitting over the INTERIOR of a B.Cu trace is
        # not touched by it — the trace passes underneath.
        pads = [_pad("A", 0, 0, "B.Cu"), _pad("B", 10, 0, "B.Cu"), _pad("C", 5, 0, "F.Cu")]
        tracks = [((0, 0), (10, 0), 0.6, "B.Cu")]
    elif kind == "round_corner":
        # Circle pad at (5, 1), radius 0.85: the diagonal line y = x - 2.7
        # cuts through its size-BOX (a bbox contact model would fuse it)
        # while staying 0.019 mm clear of its true round copper.
        pads = [_pad("A", 0, -2.7), _pad("B", 10, 7.3), _pad("C", 5, 1, shape="circle", size=1.7)]
        tracks = [((0, -2.7), (10, 7.3), 0.1, "F.Cu")]
    else:  # "pad_gap" — inline pad held 1 µm clear of the trunk copper
        pads.append(_pad("C", 5, 0.601))

    partition, _ = _partition(tmp_path, pads, tracks, splits=splits)
    assert partition == {frozenset({"A.1", "B.1"}), frozenset({"C.1"})}, f"got {partition}"

    # Same schematic net on all three pads: the gap must surface as an open,
    # and nothing may be reported as a short.
    result = compare_partitions({(p[0], "1"): "SIG" for p in pads}, list(partition))
    assert len(result.opens) == 1
    assert not result.shorts


@pytest.mark.parametrize("splits", [1, 4])
def test_midtrack_via_bridges_layers(tmp_path, splits):
    """A via mid-track still bridges layers, split or not."""
    pads = [_pad("A", 0, 0), _pad("B", 5, 5, "B.Cu")]
    tracks = [((0, 0), (10, 0), 0.6, "F.Cu"), ((5, 0), (5, 5), 0.6, "B.Cu")]
    partition, _ = _partition(tmp_path, pads, tracks, splits=splits, via=True)
    assert partition == {frozenset({"A.1", "B.1"})}


# ---------------------------------------------------------------------------
# Board-09 reproducer (committed snapshot; no refill, no regeneration)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "relative_pcb",
    [
        "engineering/connectivity-investigation/before-split.kicad_pcb",
        "output/usbc_pd_power.kicad_pcb",
    ],
    ids=["before-split", "canonical-split"],
)
def test_board09_copper_lvs_agrees_across_split_revisions(relative_pcb):
    """Both board-09 revisions of the SAME copper must read identically.

    ``before-split`` is the fully routed board before explicit mid-track
    junction splitting (see the investigation README); the canonical output
    is the same copper with vertices inserted at the branch / pad positions.
    Their buffered trace unions differ by 0.0 mm², so the copper-LVS verdict
    must not differ either.
    """
    from kicad_tools.lvs.copper_lvs import compare_copper_netlist

    result = compare_copper_netlist(
        BOARD09 / "output/usbc_pd_power.kicad_sch", BOARD09 / relative_pcb
    )

    # Evidence preserved: the diff still ran against all 153 bound pads.
    assert result.bound_pad_count == 153
    # Net-specific: none of the five historical false opens survive.
    reported = {(m.net_a, m.pad_a, m.pad_b) for m in result.mismatches}
    assert not (reported & BOARD09_FALSE_OPENS), f"false opens still reported: {reported}"
    # And nothing else appeared in their place.
    assert result.mismatches == (), f"unexpected mismatches: {result.mismatches}"
    assert result.clean


def test_board09_split_revisions_share_a_pad_partition():
    """The affected pads land in the SAME copper component in both revisions.

    Compares actual partitions rather than the top-level clean flag, so a
    change in plane-advisory filtering cannot make this pass vacuously.
    """
    affected = ["C12.1", "R12.1", "R17.1", "C4.1", "Q1.1", "Q1.2", "Q2.2"]
    before_pcb = "engineering/connectivity-investigation/before-split.kicad_pcb"
    after_pcb = "output/usbc_pd_power.kicad_pcb"

    partitions = {
        name: ConnectivityValidator(BOARD09 / name).extract_pad_partition()
        for name in (before_pcb, after_pcb)
    }

    def component_of(name: str, pad: str) -> frozenset[str]:
        hits = [comp for comp in partitions[name] if pad in comp]
        assert len(hits) == 1, f"{pad} appears in {len(hits)} components of {name}"
        return hits[0]

    for pad in affected:
        assert component_of(before_pcb, pad) == component_of(after_pcb, pad), (
            f"{pad}: splitting unchanged copper changed its copper component"
        )

    # The two nets are each a single island in both revisions (the defect
    # was that +3V3 and PMOS_SOURCE fragmented before splitting).
    for name in (before_pcb, after_pcb):
        assert component_of(name, "C12.1") == component_of(name, "R17.1")
        assert component_of(name, "C4.1") == component_of(name, "Q2.2")
