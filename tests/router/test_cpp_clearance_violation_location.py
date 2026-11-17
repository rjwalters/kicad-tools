"""Resumable search must penalize the physical conflict, even on long traces."""

import pytest

from kicad_tools.router.cpp_backend import is_cpp_available

pytestmark = pytest.mark.skipif(not is_cpp_available(), reason="C++ backend unavailable")


@pytest.mark.parametrize("reverse_candidate", [False, True])
@pytest.mark.parametrize("reverse_obstacle", [False, True])
def test_board06_segment_conflict_reports_closest_gap(
    reverse_candidate: bool, reverse_obstacle: bool
) -> None:
    from kicad_tools.router import router_cpp

    # Captured failed MIPI_RST candidate against retained MIPI_D0- copper.
    # Averaging all four endpoints reports x=113.0875, over 3.5 mm away;
    # boosting avoidance there repeats this same rejection on every resume.
    grid = router_cpp.Grid3D(600, 600, 2, 0.05, 100.0, 100.0)
    obstacle = [(108.7, 117.8), (124.5, 117.8)]
    candidate = [(109.6, 118.25), (109.55, 118.20)]
    if reverse_obstacle:
        obstacle.reverse()
    if reverse_candidate:
        candidate.reverse()
    grid.add_stored_segment(*obstacle[0], *obstacle[1], 0.225, 0, 23)
    segment = router_cpp.Segment()
    segment.x1, segment.y1 = candidate[0]
    segment.x2, segment.y2 = candidate[1]
    segment.width, segment.layer, segment.net = 0.375, 0, 26
    result = grid.validate_route([segment], [], 26, [], 0.15, 0.2, 0.102)

    assert not result.valid
    assert result.violation_type == 2
    assert result.min_clearance == pytest.approx(0.1, abs=1e-5)
    assert result.violation_x == pytest.approx(109.55, abs=1e-4)
    assert result.violation_y == pytest.approx(118.0, abs=1e-4)
