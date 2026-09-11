"""Broad-phase parity against bounded exhaustive copper scans."""

from collections import defaultdict
from itertools import combinations
from types import SimpleNamespace

import pytest
from shapely.affinity import rotate
from shapely.geometry import box

from kicad_tools.validate import connectivity
from kicad_tools.validate.rules import clearance
from kicad_tools.validate.spatial import candidate_pairs


def exhaustive(bounds, margin):
    return combinations(range(len(bounds)), 2)


@pytest.mark.parametrize("count", [100, 400])
def test_separated_candidate_counts(count):
    bounds = [(i * 10, 0, i * 10 + 1, 1) for i in range(count)]
    assert list(candidate_pairs(bounds, 0.2)) == []
    # One near neighbor each: linear candidate count, no duplicates.
    bounds += [(i * 10 + 1.1, 0, i * 10 + 2, 1) for i in range(count)]
    assert list(candidate_pairs(bounds, 0.2)) == [(i, i + count) for i in range(count)]


@pytest.mark.parametrize("threshold", [0.0, 0.1, 0.2, 0.3])
def test_clearance_exhaustive_parity(monkeypatch, threshold):
    elements = []
    for i in range(18):
        x, y = (i % 6) * 0.4, (i // 6) * 0.3
        kind = ["segment", "pad", "via"][i % 3]
        geometry = (
            (x, y, x + (2 if i % 2 else 0), y + 0.2, 0.3)
            if kind == "segment"
            else (x, y, 0.3, 0.6 if kind == "pad" else 0.3)
        )
        polygon = rotate(box(x - 0.15, y - 0.3, x + 0.15, y + 0.3), 35) if kind == "pad" else None
        elements.append(
            clearance.CopperElement(kind, "F.Cu", i % 5, geometry, str(i), polygon=polygon)
        )
    rule = clearance.ClearanceRule()
    monkeypatch.setattr(rule, "_collect_elements", lambda *args: elements)
    actual = rule._check_layer(None, "F.Cu", threshold)
    monkeypatch.setattr(clearance, "candidate_pairs", exhaustive)
    assert actual == rule._check_layer(None, "F.Cu", threshold)


@pytest.mark.parametrize("bridge", [False, True])
def test_chain_exhaustive_parity(monkeypatch, bridge):
    segments = [
        SimpleNamespace(start=a, end=b, layer=layer, width=0.2)
        for a, b, layer in [
            ((0, 0), (1, 0), "F.Cu"),
            ((1.009, 0), (2, 0), "F.Cu"),
            ((2, 0), (3, 0), "B.Cu"),
            ((0.5, -1), (0.5, 1), "F.Cu"),  # interior crossing
            ((2.01, 0), (4, 0), "F.Cu"),  # tolerance boundary
            ((5, 0), (5, 0), "F.Cu"),  # zero length
        ]
    ]
    pcb = SimpleNamespace(vias=[])
    validator = connectivity.ConnectivityValidator(pcb)
    bridges = [((2, 0), frozenset({"F.Cu", "B.Cu"}))] if bridge else []
    monkeypatch.setattr(validator, "_collect_layer_bridges", lambda *args: bridges)
    extra = {i: {f"node{i}"} for i in range(len(segments))}
    extra[0].add("inline-pad")

    def run():
        return validator._build_segment_chains(
            segments, {}, defaultdict(set), segment_extra_nodes=extra
        )

    actual = run()
    monkeypatch.setattr(connectivity, "candidate_pairs", exhaustive)
    assert actual == run()
    assert "inline-pad" in actual["node1"]
    assert ("node2" in actual["node0"]) == bridge


@pytest.mark.parametrize("count", [100, 400])
def test_production_exact_predicate_counts(monkeypatch, count):
    segments = [
        SimpleNamespace(start=(i * 10, 0), end=(i * 10 + 1, 0), layer="F.Cu", width=0.2)
        for i in range(count)
    ]
    validator = connectivity.ConnectivityValidator(SimpleNamespace(vias=[]))
    chain_calls = 0

    def chain_predicate(*args):
        nonlocal chain_calls
        chain_calls += 1
        return False

    monkeypatch.setattr(validator, "_segments_chain_at_shared_point", chain_predicate)
    validator._build_segment_chains(segments, {}, defaultdict(set))
    assert chain_calls == 0

    elements = [
        clearance.CopperElement("segment", "F.Cu", i + 1, (*seg.start, *seg.end, 0.2), str(i))
        for i, seg in enumerate(segments)
    ]
    rule = clearance.ClearanceRule()
    monkeypatch.setattr(rule, "_collect_elements", lambda *args: elements)
    clearance_calls = 0
    exact = clearance._calculate_clearance

    def clearance_predicate(*args):
        nonlocal clearance_calls
        clearance_calls += 1
        return exact(*args)

    monkeypatch.setattr(clearance, "_calculate_clearance", clearance_predicate)
    assert rule._check_layer(None, "F.Cu", 0.2) == []
    assert clearance_calls == 0


@pytest.mark.parametrize("gap", [0.199, 0.199999, 0.2, 0.200001])
def test_boundary_violation_fields(monkeypatch, gap):
    elements = [
        clearance.CopperElement("segment", "F.Cu", 1, (0, 0, 20, 0, 2), "wide"),
        clearance.CopperElement("via", "F.Cu", 2, (10, 1.5 + gap, 1, 1), "via"),
        # Polygon-less rectangular fallback becomes a disc when paired with a
        # polygon. Its broad bounds must include that existing predicate too.
        clearance.CopperElement("pad", "F.Cu", 3, (30, 0, 0.2, 4), "legacy"),
        clearance.CopperElement(
            "pad", "F.Cu", 4, (31.9, 0, 0.1, 0.1), "polygon", polygon=box(31.85, -0.05, 31.95, 0.05)
        ),
    ]
    rule = clearance.ClearanceRule()
    monkeypatch.setattr(rule, "_collect_elements", lambda *args: elements)
    actual = rule._check_layer(None, "F.Cu", 0.2)
    monkeypatch.setattr(clearance, "candidate_pairs", exhaustive)
    assert actual == rule._check_layer(None, "F.Cu", 0.2)
    assert any(v.rule_id == "clearance_pad_pad" for v in actual)


def test_no_shapely_fallback(monkeypatch):
    from kicad_tools.validate import spatial

    monkeypatch.setattr(spatial, "has_shapely", lambda: False)
    assert list(candidate_pairs([(0, 0, 1, 1), (100, 100, 101, 101)], 0)) == [(0, 1)]


@pytest.mark.parametrize("layer", ["F.Cu", "B.Cu"])
@pytest.mark.parametrize("widths", [(0.2, 0.2), (0.05, 0.8)])
@pytest.mark.parametrize("extra_gap", [-0.01, 0.0, 0.001])
def test_width_only_contact_exhaustive_parity(monkeypatch, layer, widths, extra_gap):
    # Endpoint coincidence cannot account for these side contacts. The
    # copper edge controls connectivity even when the centerline boxes miss.
    separation = sum(widths) / 2 + extra_gap
    segments = [
        SimpleNamespace(start=(0, 0), end=(10, 0), layer="F.Cu", width=widths[0]),
        SimpleNamespace(start=(2, separation), end=(8, separation), layer=layer, width=widths[1]),
    ]
    validator = connectivity.ConnectivityValidator(SimpleNamespace(vias=[]))

    def run():
        return validator._build_segment_chains(
            segments, {}, defaultdict(set), segment_extra_nodes={0: {"a"}, 1: {"b"}}
        )

    indexed = run()
    monkeypatch.setattr(connectivity, "candidate_pairs", exhaustive)
    assert indexed == run()
    assert ("b" in indexed["a"]) == (layer == "F.Cu" and extra_gap <= 0)


def test_negative_width_shared_endpoint_exhaustive_parity(monkeypatch):
    # The exact contact predicate treats negative widths as bare centerlines.
    segments = [
        SimpleNamespace(start=(0, 0), end=(10, 0), layer="F.Cu", width=-2),
        SimpleNamespace(start=(0, 0), end=(-2, 0), layer="F.Cu", width=0.2),
    ]
    validator = connectivity.ConnectivityValidator(SimpleNamespace(vias=[]))

    def run():
        return validator._build_segment_chains(
            segments, {}, defaultdict(set), segment_extra_nodes={0: {"a"}, 1: {"b"}}
        )

    indexed = run()
    monkeypatch.setattr(connectivity, "candidate_pairs", exhaustive)
    assert indexed == run() == {"a": {"b"}, "b": {"a"}}
