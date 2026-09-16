"""Bounding-box pruning preserves exact clearance decisions and diagnostics."""

import random
from unittest.mock import patch

import pytest

from kicad_tools.core import geometry
from kicad_tools.router import match_group_tuning as tuning
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Route, Segment


def check(pass_name, candidate, others):
    routes = {2: Route(net=2, net_name="neighbor", segments=others)}
    if pass_name == "paired":
        return tuning._post_insertion_clearance_detail_pair_group(
            new_p_segments=[candidate],
            new_n_segments=others,
            candidate_p_id=1,
            candidate_n_id=2,
            group_net_ids={1, 2},
            routes_by_net={},
            intra_group_clearance_mm=0.05,
            intra_pair_clearance_mm=0.2,
        )
    return tuning._post_insertion_clearance_detail_group(
        new_segments=[candidate],
        candidate_net_id=1,
        group_net_ids={1, 2} if pass_name != "inter" else {1},
        routes_by_net=routes,
        # Make the partner floor stricter to exercise pass 4 independently.
        intra_group_clearance_mm=0.05 if pass_name == "partner" else 0.2,
        diff_pair_partners={1: 2} if pass_name == "partner" else None,
        intra_pair_clearance_mm=0.2,
    )


@pytest.mark.parametrize("pass_name", ["intra", "inter", "partner", "paired"])
def test_exact_diagnostic_equivalence(pass_name):
    rng = random.Random(5471)
    candidates = []
    for _ in range(600):
        x, y = rng.uniform(-100, 100), rng.uniform(-100, 100)
        a = Segment(
            x,
            y,
            x + rng.uniform(-5, 5),
            y + rng.uniform(-5, 5),
            rng.uniform(0.05, 0.8),
            Layer.F_CU,
            net=1,
        )
        others = [
            Segment(
                x + rng.uniform(-10, 10),
                y + rng.uniform(-10, 10),
                x + rng.uniform(-10, 10),
                y + rng.uniform(-10, 10),
                rng.uniform(0.05, 0.8),
                rng.choice([Layer.F_CU, Layer.B_CU]),
                net=2,
            )
            for _ in range(3)
        ]
        candidates.append((a, others))
    for offset in [0, -1000, 1000]:
        for width in [0.05, 0.2, 0.7]:
            for delta in [-2e-9, -1e-9, 0, 1e-9, 2e-9]:
                for reverse in [False, True]:
                    for length in [0, 5]:
                        a = Segment(
                            offset, offset, offset + length, offset, width, Layer.F_CU, net=1
                        )
                        y = offset + 0.2 + width / 2 + 0.15 + delta
                        ends = (
                            (offset, offset + length) if not reverse else (offset + length, offset)
                        )
                        b = Segment(ends[0], y, ends[1], y, 0.3, Layer.F_CU, net=2)
                        candidates.append((a, [b]))
        # Crossing and coincident copper must always reach exact geometry.
        candidates.append(
            (
                Segment(offset, 0, offset + 5, 5, 0.2, Layer.F_CU, net=1),
                [Segment(offset, 5, offset + 5, 0, 0.2, Layer.F_CU, net=2)],
            )
        )
        candidates.append(
            (
                Segment(offset, 0, offset, 0, 0.2, Layer.F_CU, net=1),
                [Segment(offset, 0, offset, 0, 0.2, Layer.F_CU, net=2)],
            )
        )
    for candidate, others in candidates:
        filtered = check(pass_name, candidate, others)
        # Disable only the broad phase; all original loops, exact distances,
        # ordering and first-violation text remain the reference implementation.
        with patch.object(tuning, "_segments_within", return_value=True):
            assert filtered == check(pass_name, candidate, others)


@pytest.mark.parametrize("pass_name", ["intra", "inter", "partner", "paired"])
def test_far_geometry_skipped_and_boundary_checked(pass_name):
    candidate = Segment(0, 0, 5, 0, 0.2, Layer.F_CU, net=1)
    far = Segment(100, 100, 105, 100, 0.2, Layer.F_CU, net=2)
    with patch.object(geometry, "segment_clearance", wraps=geometry.segment_clearance) as exact:
        assert check(pass_name, candidate, [far]) is None
        assert exact.call_count == 0
    near = Segment(0, 0.4, 5, 0.4, 0.2, Layer.F_CU, net=2)
    with patch.object(geometry, "segment_clearance", wraps=geometry.segment_clearance) as exact:
        assert check(pass_name, candidate, [near]) is None
        assert exact.call_count >= 1
