"""Tests for placement optimization visualization module.

Covers:
- OptimizationRecorder data accumulation and properties
- IterationRecord construction and best-tracking
- ParetoPoint and Pareto front computation
- Convergence plot generation (PNG and SVG)
- Layout visualization with overlap detection
- Layout visualization with net HPWL bounding boxes
- Pareto front scatter plot generation
- Error handling for empty data and mismatched inputs
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_tools.placement.cost import BoardOutline, CostBreakdown, Net
from kicad_tools.placement.vector import (
    ComponentDef,
    PadDef,
    PlacedComponent,
    TransformedPad,
)
from kicad_tools.placement.visualization import (
    IterationRecord,
    LayoutStyle,
    OptimizationRecorder,
    ParetoPoint,
    _compute_pareto_front,
    _find_overlapping_refs,
    plot_convergence,
    plot_layout,
    plot_pareto_front,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_breakdown(
    wirelength: float = 10.0,
    overlap: float = 0.0,
    boundary: float = 0.0,
    drc: float = 0.0,
    area: float = 25.0,
) -> CostBreakdown:
    return CostBreakdown(
        wirelength=wirelength,
        overlap=overlap,
        boundary=boundary,
        drc=drc,
        area=area,
    )


def _make_record(
    iteration: int,
    total_score: float,
    is_feasible: bool = True,
    breakdown: CostBreakdown | None = None,
) -> IterationRecord:
    if breakdown is None:
        breakdown = _make_breakdown()
    return IterationRecord(
        iteration=iteration,
        total_score=total_score,
        breakdown=breakdown,
        is_feasible=is_feasible,
    )


def _make_recorder(n_iterations: int = 10) -> OptimizationRecorder:
    """Create a recorder with n_iterations of decreasing scores."""
    recorder = OptimizationRecorder()
    for i in range(n_iterations):
        score = 100.0 - i * 5.0
        is_feasible = i >= 5  # feasibility achieved at iteration 5
        breakdown = _make_breakdown(
            wirelength=50.0 - i * 2.0,
            overlap=max(0.0, 10.0 - i * 2.0),
            boundary=max(0.0, 5.0 - i * 1.0),
            drc=max(0.0, 3.0 - i * 0.6),
            area=30.0 - i * 1.0,
        )
        recorder.record(
            IterationRecord(
                iteration=i,
                total_score=score,
                breakdown=breakdown,
                is_feasible=is_feasible,
            )
        )
    return recorder


def _make_board() -> BoardOutline:
    return BoardOutline(min_x=0.0, min_y=0.0, max_x=50.0, max_y=50.0)


def _make_placements() -> tuple[list[PlacedComponent], list[ComponentDef]]:
    """Create a simple 3-component layout."""
    comp_defs = [
        ComponentDef(reference="U1", width=10.0, height=8.0),
        ComponentDef(reference="R1", width=3.0, height=1.5),
        ComponentDef(reference="C1", width=2.0, height=2.0),
    ]
    placements = [
        PlacedComponent(reference="U1", x=15.0, y=15.0, rotation=0.0, side=0),
        PlacedComponent(reference="R1", x=30.0, y=15.0, rotation=0.0, side=0),
        PlacedComponent(reference="C1", x=25.0, y=30.0, rotation=90.0, side=0),
    ]
    return placements, comp_defs


def _make_overlapping_placements() -> tuple[list[PlacedComponent], list[ComponentDef]]:
    """Create placements where U1 and R1 overlap."""
    comp_defs = [
        ComponentDef(reference="U1", width=10.0, height=8.0),
        ComponentDef(reference="R1", width=6.0, height=3.0),
    ]
    placements = [
        PlacedComponent(reference="U1", x=15.0, y=15.0, rotation=0.0, side=0),
        PlacedComponent(reference="R1", x=18.0, y=15.0, rotation=0.0, side=0),
    ]
    return placements, comp_defs


def _make_placements_with_pads() -> tuple[list[PlacedComponent], list[ComponentDef], list[Net]]:
    """Create placements with pads and net connectivity."""
    comp_defs = [
        ComponentDef(
            reference="U1",
            pads=(
                PadDef(name="1", local_x=-2.0, local_y=0.0),
                PadDef(name="2", local_x=2.0, local_y=0.0),
            ),
            width=8.0,
            height=5.0,
        ),
        ComponentDef(
            reference="R1",
            pads=(
                PadDef(name="1", local_x=-1.0, local_y=0.0),
                PadDef(name="2", local_x=1.0, local_y=0.0),
            ),
            width=4.0,
            height=2.0,
        ),
    ]
    placements = [
        PlacedComponent(
            reference="U1",
            x=15.0,
            y=20.0,
            rotation=0.0,
            side=0,
            pads=(
                TransformedPad(name="1", x=13.0, y=20.0, size_x=0.5, size_y=0.5),
                TransformedPad(name="2", x=17.0, y=20.0, size_x=0.5, size_y=0.5),
            ),
        ),
        PlacedComponent(
            reference="R1",
            x=30.0,
            y=20.0,
            rotation=0.0,
            side=0,
            pads=(
                TransformedPad(name="1", x=29.0, y=20.0, size_x=0.5, size_y=0.5),
                TransformedPad(name="2", x=31.0, y=20.0, size_x=0.5, size_y=0.5),
            ),
        ),
    ]
    nets = [
        Net(name="VCC", pins=[("U1", "1"), ("R1", "1")]),
        Net(name="GND", pins=[("U1", "2"), ("R1", "2")]),
    ]
    return placements, comp_defs, nets


# ---------------------------------------------------------------------------
# IterationRecord tests
# ---------------------------------------------------------------------------


class TestIterationRecord:
    def test_construction(self):
        bd = _make_breakdown()
        rec = IterationRecord(
            iteration=0,
            total_score=100.0,
            breakdown=bd,
            is_feasible=True,
        )
        assert rec.iteration == 0
        assert rec.total_score == 100.0
        assert rec.is_feasible is True
        assert rec.is_new_best is False

    def test_immutable(self):
        rec = _make_record(0, 100.0)
        with pytest.raises(AttributeError):
            rec.iteration = 1  # type: ignore[misc]


# ---------------------------------------------------------------------------
# OptimizationRecorder tests
# ---------------------------------------------------------------------------


class TestOptimizationRecorder:
    def test_empty_recorder(self):
        rec = OptimizationRecorder()
        assert rec.history == []
        assert rec.iterations == []
        assert rec.total_scores == []
        assert rec.best_so_far == []
        assert rec.feasibility_boundary is None

    def test_record_single(self):
        rec = OptimizationRecorder()
        entry = _make_record(0, 100.0)
        result = rec.record(entry)
        assert result.is_new_best is True
        assert len(rec.history) == 1
        assert rec.iterations == [0]
        assert rec.total_scores == [100.0]

    def test_best_tracking(self):
        rec = OptimizationRecorder()
        r1 = rec.record(_make_record(0, 100.0))
        r2 = rec.record(_make_record(1, 90.0))
        r3 = rec.record(_make_record(2, 95.0))  # Not a new best
        r4 = rec.record(_make_record(3, 80.0))

        assert r1.is_new_best is True
        assert r2.is_new_best is True
        assert r3.is_new_best is False
        assert r4.is_new_best is True

    def test_best_so_far(self):
        rec = OptimizationRecorder()
        rec.record(_make_record(0, 100.0))
        rec.record(_make_record(1, 90.0))
        rec.record(_make_record(2, 95.0))
        rec.record(_make_record(3, 80.0))

        assert rec.best_so_far == [100.0, 90.0, 90.0, 80.0]

    def test_feasibility_boundary_found(self):
        rec = OptimizationRecorder()
        rec.record(_make_record(0, 100.0, is_feasible=False))
        rec.record(_make_record(1, 90.0, is_feasible=False))
        rec.record(_make_record(2, 85.0, is_feasible=True))
        rec.record(_make_record(3, 80.0, is_feasible=True))

        assert rec.feasibility_boundary == 2

    def test_feasibility_boundary_not_found(self):
        rec = OptimizationRecorder()
        rec.record(_make_record(0, 100.0, is_feasible=False))
        rec.record(_make_record(1, 90.0, is_feasible=False))

        assert rec.feasibility_boundary is None

    def test_pareto_points(self):
        rec = OptimizationRecorder()
        rec.add_pareto_point(ParetoPoint(wirelength=10.0, area=20.0, is_feasible=True))
        rec.add_pareto_point(ParetoPoint(wirelength=15.0, area=15.0, is_feasible=True))
        assert len(rec.pareto_points) == 2


# ---------------------------------------------------------------------------
# ParetoPoint and Pareto front computation
# ---------------------------------------------------------------------------


class TestParetoFront:
    def test_empty_points(self):
        assert _compute_pareto_front([]) == []

    def test_all_infeasible(self):
        points = [
            ParetoPoint(wirelength=10.0, area=20.0, is_feasible=False),
            ParetoPoint(wirelength=15.0, area=15.0, is_feasible=False),
        ]
        assert _compute_pareto_front(points) == []

    def test_single_feasible(self):
        points = [
            ParetoPoint(wirelength=10.0, area=20.0, is_feasible=True),
        ]
        result = _compute_pareto_front(points)
        assert len(result) == 1
        assert result[0].wirelength == 10.0

    def test_two_pareto_optimal(self):
        # Point (15, 25) is NOT dominated by (10, 30) since 25 < 30 (better area).
        # All three form a Pareto front: (10, 30), (15, 25), (20, 10).
        points = [
            ParetoPoint(wirelength=10.0, area=30.0, is_feasible=True),  # Best WL
            ParetoPoint(wirelength=20.0, area=10.0, is_feasible=True),  # Best area
            ParetoPoint(wirelength=15.0, area=25.0, is_feasible=True),  # Also Pareto
        ]
        result = _compute_pareto_front(points)
        assert len(result) == 3
        # Sorted by wirelength
        assert result[0].wirelength == 10.0
        assert result[1].wirelength == 15.0
        assert result[2].wirelength == 20.0

    def test_dominated_point_excluded(self):
        points = [
            ParetoPoint(wirelength=10.0, area=20.0, is_feasible=True),
            ParetoPoint(wirelength=15.0, area=25.0, is_feasible=True),  # Dominated by first
        ]
        result = _compute_pareto_front(points)
        assert len(result) == 1
        assert result[0].wirelength == 10.0

    def test_infeasible_excluded(self):
        points = [
            ParetoPoint(
                wirelength=5.0, area=5.0, is_feasible=False
            ),  # Would dominate but infeasible
            ParetoPoint(wirelength=10.0, area=20.0, is_feasible=True),
        ]
        result = _compute_pareto_front(points)
        assert len(result) == 1
        assert result[0].wirelength == 10.0


# ---------------------------------------------------------------------------
# Overlap detection
# ---------------------------------------------------------------------------


class TestFindOverlappingRefs:
    def test_no_overlap(self):
        placements, comp_defs = _make_placements()
        result = _find_overlapping_refs(placements, comp_defs)
        assert result == set()

    def test_with_overlap(self):
        placements, comp_defs = _make_overlapping_placements()
        result = _find_overlapping_refs(placements, comp_defs)
        assert result == {"U1", "R1"}

    def test_different_sides_no_overlap(self):
        """Components on opposite sides should not be flagged."""
        comp_defs = [
            ComponentDef(reference="U1", width=10.0, height=8.0),
            ComponentDef(reference="U2", width=10.0, height=8.0),
        ]
        placements = [
            PlacedComponent(reference="U1", x=15.0, y=15.0, rotation=0.0, side=0),
            PlacedComponent(reference="U2", x=15.0, y=15.0, rotation=0.0, side=1),
        ]
        result = _find_overlapping_refs(placements, comp_defs)
        assert result == set()


# ---------------------------------------------------------------------------
# Convergence plot tests
# ---------------------------------------------------------------------------


class TestPlotConvergence:
    def test_generates_png(self, tmp_path: Path):
        recorder = _make_recorder()
        output = tmp_path / "convergence.png"
        result = plot_convergence(recorder, output)
        assert result == output
        assert output.exists()
        assert output.stat().st_size > 0

    def test_generates_svg(self, tmp_path: Path):
        recorder = _make_recorder()
        output = tmp_path / "convergence.svg"
        result = plot_convergence(recorder, output)
        assert result == output
        assert output.exists()
        content = output.read_text()
        assert "<svg" in content

    def test_without_breakdown(self, tmp_path: Path):
        recorder = _make_recorder()
        output = tmp_path / "no_breakdown.png"
        result = plot_convergence(recorder, output, show_breakdown=False)
        assert result == output
        assert output.exists()

    def test_custom_title(self, tmp_path: Path):
        recorder = _make_recorder()
        output = tmp_path / "custom_title.png"
        result = plot_convergence(recorder, output, title="My Custom Title")
        assert result == output
        assert output.exists()

    def test_empty_recorder_raises(self, tmp_path: Path):
        recorder = OptimizationRecorder()
        with pytest.raises(ValueError, match="No iteration data"):
            plot_convergence(recorder, tmp_path / "empty.png")

    def test_creates_parent_dirs(self, tmp_path: Path):
        recorder = _make_recorder()
        output = tmp_path / "subdir" / "nested" / "convergence.png"
        result = plot_convergence(recorder, output)
        assert result == output
        assert output.exists()

    def test_all_infeasible(self, tmp_path: Path):
        """Plot with no feasibility boundary."""
        recorder = OptimizationRecorder()
        for i in range(5):
            recorder.record(_make_record(i, 100.0 - i, is_feasible=False))
        output = tmp_path / "all_infeasible.png"
        result = plot_convergence(recorder, output)
        assert result == output
        assert output.exists()


# ---------------------------------------------------------------------------
# Layout visualization tests
# ---------------------------------------------------------------------------


class TestPlotLayout:
    def test_generates_png(self, tmp_path: Path):
        placements, comp_defs = _make_placements()
        board = _make_board()
        output = tmp_path / "layout.png"
        result = plot_layout(placements, comp_defs, board, output)
        assert result == output
        assert output.exists()
        assert output.stat().st_size > 0

    def test_generates_svg(self, tmp_path: Path):
        placements, comp_defs = _make_placements()
        board = _make_board()
        output = tmp_path / "layout.svg"
        result = plot_layout(placements, comp_defs, board, output)
        assert result == output
        assert output.exists()
        content = output.read_text()
        assert "<svg" in content

    def test_overlapping_components_colored(self, tmp_path: Path):
        placements, comp_defs = _make_overlapping_placements()
        board = _make_board()
        output = tmp_path / "overlapping.png"
        result = plot_layout(placements, comp_defs, board, output)
        assert result == output
        assert output.exists()

    def test_with_net_bboxes(self, tmp_path: Path):
        placements, comp_defs, nets = _make_placements_with_pads()
        board = _make_board()
        output = tmp_path / "layout_nets.png"
        result = plot_layout(
            placements,
            comp_defs,
            board,
            output,
            nets=nets,
            show_net_bboxes=True,
        )
        assert result == output
        assert output.exists()

    def test_custom_style(self, tmp_path: Path):
        placements, comp_defs = _make_placements()
        board = _make_board()
        output = tmp_path / "custom_style.png"
        style = LayoutStyle(
            feasible_color="#00FF00",
            infeasible_color="#FF0000",
            figsize=(8, 8),
        )
        result = plot_layout(placements, comp_defs, board, output, style=style)
        assert result == output
        assert output.exists()

    def test_mismatched_lengths_raises(self, tmp_path: Path):
        placements, comp_defs = _make_placements()
        board = _make_board()
        with pytest.raises(ValueError, match="same length"):
            plot_layout(placements, comp_defs[:1], board, tmp_path / "bad.png")

    def test_creates_parent_dirs(self, tmp_path: Path):
        placements, comp_defs = _make_placements()
        board = _make_board()
        output = tmp_path / "deep" / "dir" / "layout.png"
        result = plot_layout(placements, comp_defs, board, output)
        assert result == output
        assert output.exists()


# ---------------------------------------------------------------------------
# Pareto front plot tests
# ---------------------------------------------------------------------------


class TestPlotParetoFront:
    def test_generates_png(self, tmp_path: Path):
        recorder = OptimizationRecorder()
        for i in range(20):
            recorder.add_pareto_point(
                ParetoPoint(
                    wirelength=50.0 - i * 2.0,
                    area=20.0 + i * 1.5,
                    is_feasible=i > 5,
                    iteration=i,
                )
            )
        output = tmp_path / "pareto.png"
        result = plot_pareto_front(recorder, output)
        assert result == output
        assert output.exists()
        assert output.stat().st_size > 0

    def test_generates_svg(self, tmp_path: Path):
        recorder = OptimizationRecorder()
        recorder.add_pareto_point(ParetoPoint(10.0, 20.0, True, 0))
        recorder.add_pareto_point(ParetoPoint(20.0, 10.0, True, 1))
        output = tmp_path / "pareto.svg"
        result = plot_pareto_front(recorder, output)
        assert result == output
        assert output.exists()
        content = output.read_text()
        assert "<svg" in content

    def test_all_infeasible(self, tmp_path: Path):
        recorder = OptimizationRecorder()
        recorder.add_pareto_point(ParetoPoint(10.0, 20.0, False, 0))
        recorder.add_pareto_point(ParetoPoint(20.0, 10.0, False, 1))
        output = tmp_path / "infeasible.png"
        result = plot_pareto_front(recorder, output)
        assert result == output
        assert output.exists()

    def test_empty_points_raises(self, tmp_path: Path):
        recorder = OptimizationRecorder()
        with pytest.raises(ValueError, match="No Pareto points"):
            plot_pareto_front(recorder, tmp_path / "empty.png")

    def test_creates_parent_dirs(self, tmp_path: Path):
        recorder = OptimizationRecorder()
        recorder.add_pareto_point(ParetoPoint(10.0, 20.0, True, 0))
        output = tmp_path / "sub" / "pareto.png"
        result = plot_pareto_front(recorder, output)
        assert result == output
        assert output.exists()

    def test_custom_title(self, tmp_path: Path):
        recorder = OptimizationRecorder()
        recorder.add_pareto_point(ParetoPoint(10.0, 20.0, True, 0))
        output = tmp_path / "titled.png"
        result = plot_pareto_front(recorder, output, title="Custom Pareto")
        assert result == output
        assert output.exists()


# ---------------------------------------------------------------------------
# LayoutStyle tests
# ---------------------------------------------------------------------------


class TestLayoutStyle:
    def test_defaults(self):
        style = LayoutStyle()
        assert style.feasible_color == "#4CAF50"
        assert style.infeasible_color == "#F44336"
        assert style.component_alpha == 0.6

    def test_custom_values(self):
        style = LayoutStyle(feasible_color="blue", component_alpha=0.8)
        assert style.feasible_color == "blue"
        assert style.component_alpha == 0.8
