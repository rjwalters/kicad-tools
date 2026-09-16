"""Float-coordinate geometry must not turn disjoint copper into a crossing."""

import itertools
import math
import shutil
import subprocess
from pathlib import Path

import pytest

# Exact float endpoints and authored widths from the USB2_D- / USB_CC1
# rejection. Both segments lie on x+y=178.75; their closest endpoints are
# 1.80312444994434 mm apart. Native38 instead reported a proper crossing.
CAPTURE_A = (112.19999694824219, 66.55000305175781, 112.2249984741211, 66.5250015258789)
CAPTURE_B = (113.5250015258789, 65.2249984741211, 113.5, 65.25)
CAPTURE_DISTANCE = math.hypot(CAPTURE_A[2] - CAPTURE_B[2], CAPTURE_A[3] - CAPTURE_B[3])
OFFSET = 2**-16

# Distances are analytic: collinear endpoints, axis-aligned offsets, exact
# crossings/contacts, parallel 45-degree lines, or a 3-4-5 point separation.
# segments_intersect means PROPER crossing: overlap/touch use distance=0.
CASES = [
    pytest.param(CAPTURE_A, CAPTURE_B, CAPTURE_DISTANCE, False, id="captured-disjoint-collinear"),
    pytest.param((100, 60, 102, 62), (100, 62, 102, 60), 0, True, id="proper-crossing"),
    pytest.param((100, 60, 102, 62), (101, 61, 103, 63), 0, False, id="collinear-overlap"),
    pytest.param((100, 60, 102, 62), (102, 62, 103, 61), 0, False, id="endpoint-touch"),
    pytest.param((100, 60, 102, 60), (101, 60, 101, 61), 0, False, id="t-junction-touch"),
    pytest.param((100, 60, 102, 60), (100, 60.25, 102, 60.25), 0.25, False, id="subclearance"),
    pytest.param((100, 60, 102, 60), (100, 60.5, 102, 60.5), 0.5, False, id="clear-parallel"),
    pytest.param(
        (100, 60, 102, 62),
        (100.5, 60.5 + OFFSET, 101.5, 61.5 + OFFSET),
        OFFSET / math.sqrt(2),
        False,
        id="near-collinear-overlapping-boxes-disjoint",
    ),
    pytest.param(
        (100, 60, 102, 62),
        (100.5, 60.5 + OFFSET, 101.5, 61.5 - OFFSET),
        0,
        True,
        id="near-collinear-overlapping-boxes-crossing",
    ),
    pytest.param((100, 60, 100, 60), (103, 64, 103, 64), 5, False, id="two-points"),
    pytest.param((100, 60, 100, 60), (100, 60, 100, 60), 0, False, id="same-point"),
    pytest.param((101, 60.5, 101, 60.5), (100, 60, 102, 60), 0.5, False, id="point-to-line"),
    pytest.param((101, 60, 101, 60), (100, 60, 102, 60), 0, False, id="point-on-line"),
]
PERMUTATIONS = list(itertools.product([False, True], repeat=3))


def _permuted(a, b, permutation):
    reverse_a, reverse_b, swap = permutation
    if reverse_a:
        a = a[2:] + a[:2]
    if reverse_b:
        b = b[2:] + b[:2]
    return (b, a) if swap else (a, b)


@pytest.fixture(scope="module")
def native():
    return pytest.importorskip("kicad_tools.router.router_cpp")


@pytest.mark.parametrize("a,b,distance,proper", CASES)
@pytest.mark.parametrize("permutation", PERMUTATIONS)
def test_native_distance_and_clearance(native, a, b, distance, proper, permutation):
    # Widths move with the physical segment when roles swap.
    widths = (0.18030124093439862, 0.3339900000000007) if a == CAPTURE_A else (0.2, 0.2)
    a, b = _permuted(a, b, permutation)
    if permutation[2]:
        widths = widths[::-1]
    assert native.segments_intersect(*(a + b)) is proper
    actual = native.segment_to_segment_distance(*(a + b))
    assert actual == pytest.approx(distance, rel=2e-6, abs=1e-7)
    if distance > 0:
        assert actual > 0

    grid = native.Grid3D(161, 161, 1, 0.1, 99.0, 59.0)
    grid.add_stored_segment(*b, widths[1], 0, 24)
    segment = native.Segment()
    segment.x1, segment.y1, segment.x2, segment.y2 = a
    segment.width, segment.layer, segment.net = widths[0], 0, 7
    result = grid.validate_route([segment], [], 7, [], 0.15, 0.2, 0.1)
    expected_gap = distance - sum(widths) / 2
    assert result.valid is (expected_gap >= 0.15)
    if not result.valid:
        assert result.violation_type == 2  # trace versus stored trace
        assert result.min_clearance == pytest.approx(expected_gap, rel=2e-5, abs=1e-6)


@pytest.fixture(scope="module")
def fast_geometry(tmp_path_factory):
    compiler = shutil.which("c++") or shutil.which("g++")
    if compiler is None:
        pytest.skip("C++ compiler required for standalone geometry controls")
    root = Path(__file__).resolve().parents[1]
    binary = tmp_path_factory.mktemp("geometry-fast") / "geometry-control"
    subprocess.run(
        [
            compiler,
            "-std=c++17",
            "-O3",
            "-ffast-math",
            "-ffp-contract=fast",
            "-I",
            str(root / "src/kicad_tools/router/cpp/include"),
            str(root / "tests/cpp/segment_intersection_control.cpp"),
            str(root / "src/kicad_tools/router/cpp/src/geometry.cpp"),
            "-o",
            str(binary),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return binary


@pytest.mark.parametrize("a,b,distance,proper", CASES)
@pytest.mark.parametrize("permutation", PERMUTATIONS)
def test_standalone_fast_math_geometry(fast_geometry, a, b, distance, proper, permutation):
    a, b = _permuted(a, b, permutation)
    output = subprocess.check_output([str(fast_geometry), *map(str, a + b)], text=True)
    intersection, actual = output.split()
    assert bool(int(intersection)) is proper
    assert float(actual) == pytest.approx(distance, rel=2e-6, abs=1e-7)
