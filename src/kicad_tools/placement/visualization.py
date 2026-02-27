"""Placement optimization progress visualization.

Provides three visualization capabilities for monitoring placement optimization:

1. **Score convergence plot** -- Plot score vs iteration with per-component
   breakdown, best-so-far markers, and feasibility boundary.

2. **Layout visualization** -- Render component bounding boxes at current
   placement positions with color-coding for feasibility, board outline,
   and optional HPWL net bounding boxes.

3. **Pareto front scatter** -- Multi-objective scatter plot of wirelength
   vs area for evaluated placements with Pareto-optimal highlight.

Data is accumulated during optimization via :class:`OptimizationRecorder`
and rendered on demand. Requires matplotlib (optional dependency).

Usage::

    from kicad_tools.placement.visualization import (
        OptimizationRecorder,
        IterationRecord,
        plot_convergence,
        plot_layout,
        plot_pareto_front,
    )

    recorder = OptimizationRecorder()
    for gen in range(max_gens):
        # ... optimization step ...
        recorder.record(IterationRecord(
            iteration=gen,
            total_score=score.total,
            breakdown=score.breakdown,
            is_feasible=score.is_feasible,
        ))
    plot_convergence(recorder, "convergence.png")
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from .cost import BoardOutline, CostBreakdown, Net
from .geometry import _aabb
from .vector import ComponentDef, PlacedComponent


def _import_matplotlib():
    """Lazily import matplotlib, raising a clear error if missing."""
    try:
        import matplotlib

        matplotlib.use("Agg")  # Non-interactive backend for file output
        import matplotlib.patches as mpatches
        import matplotlib.pyplot as plt

        return plt, mpatches
    except ImportError as exc:
        raise ImportError(
            "matplotlib is required for placement visualization. "
            "Install it with: pip install matplotlib, or: "
            "pip install kicad-tools[visualization]"
        ) from exc


# ---------------------------------------------------------------------------
# Data recording
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IterationRecord:
    """Record of a single optimization iteration.

    Attributes:
        iteration: Iteration/generation number.
        total_score: Composite scalar score (lower is better).
        breakdown: Per-component cost breakdown.
        is_feasible: Whether the placement is feasible (no violations).
        is_new_best: Whether this iteration produced a new best score.
    """

    iteration: int
    total_score: float
    breakdown: CostBreakdown
    is_feasible: bool
    is_new_best: bool = False


@dataclass
class ParetoPoint:
    """A point in the Pareto objective space.

    Attributes:
        wirelength: Total wirelength cost (mm).
        area: Bounding-box area cost (mm^2).
        is_feasible: Whether the placement is feasible.
        iteration: Iteration where this point was evaluated.
    """

    wirelength: float
    area: float
    is_feasible: bool
    iteration: int = 0


@dataclass
class OptimizationRecorder:
    """Accumulates optimization iteration data for visualization.

    Call :meth:`record` at each iteration to store score history.
    The recorder tracks the best score seen and marks new-best iterations.

    Attributes:
        history: List of iteration records.
        pareto_points: Points for multi-objective Pareto front visualization.
    """

    history: list[IterationRecord] = field(default_factory=list)
    pareto_points: list[ParetoPoint] = field(default_factory=list)
    _best_score: float = field(default=float("inf"), repr=False)

    def record(self, entry: IterationRecord) -> IterationRecord:
        """Record an iteration and update best-so-far tracking.

        If the entry's total_score improves on the current best, the
        returned record has ``is_new_best=True``.

        Args:
            entry: Iteration data to record.

        Returns:
            The recorded entry (possibly with ``is_new_best`` updated).
        """
        is_new_best = entry.total_score < self._best_score
        if is_new_best:
            self._best_score = entry.total_score

        final = IterationRecord(
            iteration=entry.iteration,
            total_score=entry.total_score,
            breakdown=entry.breakdown,
            is_feasible=entry.is_feasible,
            is_new_best=is_new_best,
        )
        self.history.append(final)
        return final

    def add_pareto_point(self, point: ParetoPoint) -> None:
        """Add a point for Pareto front visualization.

        Args:
            point: Objective-space point with wirelength and area.
        """
        self.pareto_points.append(point)

    @property
    def iterations(self) -> list[int]:
        """List of iteration numbers."""
        return [r.iteration for r in self.history]

    @property
    def total_scores(self) -> list[float]:
        """List of total scores per iteration."""
        return [r.total_score for r in self.history]

    @property
    def best_so_far(self) -> list[float]:
        """Cumulative minimum score up to each iteration."""
        result: list[float] = []
        current_best = float("inf")
        for r in self.history:
            current_best = min(current_best, r.total_score)
            result.append(current_best)
        return result

    @property
    def feasibility_boundary(self) -> int | None:
        """Iteration where feasibility was first achieved, or None."""
        for r in self.history:
            if r.is_feasible:
                return r.iteration
        return None


# ---------------------------------------------------------------------------
# Convergence plot
# ---------------------------------------------------------------------------


def plot_convergence(
    recorder: OptimizationRecorder,
    output_path: str | Path,
    *,
    title: str = "Placement Optimization Convergence",
    show_breakdown: bool = True,
    figsize: tuple[float, float] = (12, 7),
) -> Path:
    """Generate a score convergence plot.

    Plots total score and best-so-far score vs iteration. Optionally
    includes per-component breakdown (wirelength, overlap, boundary, DRC,
    area) on a secondary y-axis. Marks iterations where a new best was
    found and draws the feasibility boundary.

    Args:
        recorder: Optimization recorder with iteration history.
        output_path: File path for the output image (PNG or SVG).
        title: Plot title.
        show_breakdown: Whether to show per-component cost breakdown.
        figsize: Figure size in inches (width, height).

    Returns:
        Path to the saved image file.

    Raises:
        ImportError: If matplotlib is not installed.
        ValueError: If recorder has no history.
    """
    if not recorder.history:
        raise ValueError("No iteration data to plot -- recorder history is empty")

    plt, mpatches = _import_matplotlib()
    output_path = Path(output_path)

    fig, ax1 = plt.subplots(figsize=figsize)

    iterations = recorder.iterations
    total_scores = recorder.total_scores
    best_so_far = recorder.best_so_far

    # Plot total score
    ax1.plot(iterations, total_scores, "b-", alpha=0.3, linewidth=0.8, label="Score")
    ax1.plot(iterations, best_so_far, "b-", linewidth=2.0, label="Best so far")

    # Mark new-best iterations
    new_best_iters = [r.iteration for r in recorder.history if r.is_new_best]
    new_best_scores = [r.total_score for r in recorder.history if r.is_new_best]
    if new_best_iters:
        ax1.scatter(
            new_best_iters,
            new_best_scores,
            color="green",
            marker="v",
            s=40,
            zorder=5,
            label="New best",
        )

    # Feasibility boundary
    feas_iter = recorder.feasibility_boundary
    if feas_iter is not None:
        ax1.axvline(
            x=feas_iter,
            color="green",
            linestyle="--",
            linewidth=1.5,
            alpha=0.7,
            label=f"Feasible (iter {feas_iter})",
        )

    ax1.set_xlabel("Iteration")
    ax1.set_ylabel("Total Score", color="blue")
    ax1.tick_params(axis="y", labelcolor="blue")
    ax1.set_title(title)

    # Per-component breakdown on secondary axis
    if show_breakdown and recorder.history:
        ax2 = ax1.twinx()

        wirelengths = [r.breakdown.wirelength for r in recorder.history]
        overlaps = [r.breakdown.overlap for r in recorder.history]
        boundaries = [r.breakdown.boundary for r in recorder.history]
        drcs = [r.breakdown.drc for r in recorder.history]
        areas = [r.breakdown.area for r in recorder.history]

        ax2.plot(iterations, wirelengths, "r--", alpha=0.5, linewidth=0.8, label="Wirelength")
        ax2.plot(iterations, overlaps, "m--", alpha=0.5, linewidth=0.8, label="Overlap")
        ax2.plot(iterations, boundaries, "c--", alpha=0.5, linewidth=0.8, label="Boundary")
        ax2.plot(iterations, drcs, "y--", alpha=0.5, linewidth=0.8, label="DRC")
        ax2.plot(iterations, areas, "k--", alpha=0.5, linewidth=0.8, label="Area")

        ax2.set_ylabel("Component Costs", color="red")
        ax2.tick_params(axis="y", labelcolor="red")
        ax2.legend(loc="upper right", fontsize="small")

    ax1.legend(loc="upper left", fontsize="small")
    ax1.grid(True, alpha=0.3)
    fig.tight_layout()

    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(str(output_path), dpi=150, bbox_inches="tight")
    plt.close(fig)

    return output_path


# ---------------------------------------------------------------------------
# Layout visualization
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LayoutStyle:
    """Configuration for layout visualization appearance.

    Attributes:
        feasible_color: Fill color for feasible (no overlap) components.
        infeasible_color: Fill color for components with overlaps.
        board_color: Board outline color.
        board_fill: Board background fill color.
        net_bbox_color: Color for HPWL net bounding boxes.
        violation_color: Color for DRC violation markers.
        component_alpha: Opacity of component rectangles.
        net_bbox_alpha: Opacity of net bounding boxes.
        figsize: Figure size in inches.
    """

    feasible_color: str = "#4CAF50"
    infeasible_color: str = "#F44336"
    board_color: str = "#333333"
    board_fill: str = "#FAFAFA"
    net_bbox_color: str = "#2196F3"
    violation_color: str = "#FF9800"
    component_alpha: float = 0.6
    net_bbox_alpha: float = 0.15
    figsize: tuple[float, float] = (10, 10)


def _find_overlapping_refs(
    placements: Sequence[PlacedComponent],
    component_defs: Sequence[ComponentDef],
) -> set[str]:
    """Find component references involved in any pairwise overlap.

    Args:
        placements: Placed components.
        component_defs: Component definitions (same order).

    Returns:
        Set of reference designators that overlap with at least one other.
    """
    n = len(placements)
    overlapping: set[str] = set()
    boxes = [_aabb(p, d) for p, d in zip(placements, component_defs, strict=True)]

    for i in range(n):
        for j in range(i + 1, n):
            if placements[i].side != placements[j].side:
                continue
            # Check for overlap
            x_overlap = max(0.0, min(boxes[i][2], boxes[j][2]) - max(boxes[i][0], boxes[j][0]))
            y_overlap = max(0.0, min(boxes[i][3], boxes[j][3]) - max(boxes[i][1], boxes[j][1]))
            if x_overlap > 0 and y_overlap > 0:
                overlapping.add(placements[i].reference)
                overlapping.add(placements[j].reference)

    return overlapping


def plot_layout(
    placements: Sequence[PlacedComponent],
    component_defs: Sequence[ComponentDef],
    board: BoardOutline,
    output_path: str | Path,
    *,
    nets: Sequence[Net] | None = None,
    show_net_bboxes: bool = False,
    title: str = "Component Placement Layout",
    style: LayoutStyle | None = None,
) -> Path:
    """Render component placement layout as an image.

    Draws the board outline and component bounding boxes. Components are
    color-coded green (feasible / no overlap) or red (overlapping).
    Optionally draws HPWL net bounding boxes as a blue overlay.

    Args:
        placements: Placed components with positions.
        component_defs: Component definitions (same order as placements).
        board: Board outline rectangle.
        output_path: File path for the output image (SVG or PNG).
        nets: Optional net connectivity for HPWL bounding boxes.
        show_net_bboxes: Whether to draw net bounding boxes.
        title: Plot title.
        style: Visual style configuration.

    Returns:
        Path to the saved image file.

    Raises:
        ImportError: If matplotlib is not installed.
        ValueError: If placements and component_defs lengths differ.
    """
    if len(placements) != len(component_defs):
        raise ValueError(
            f"placements ({len(placements)}) and component_defs "
            f"({len(component_defs)}) must have the same length"
        )

    plt, mpatches = _import_matplotlib()
    output_path = Path(output_path)
    if style is None:
        style = LayoutStyle()

    fig, ax = plt.subplots(figsize=style.figsize)

    # Draw board outline
    board_rect = mpatches.Rectangle(
        (board.min_x, board.min_y),
        board.width,
        board.height,
        linewidth=2.5,
        edgecolor=style.board_color,
        facecolor=style.board_fill,
        zorder=0,
    )
    ax.add_patch(board_rect)

    # Find overlapping components for color-coding
    overlapping_refs = _find_overlapping_refs(placements, component_defs)

    # Draw component bounding boxes
    for comp, comp_def in zip(placements, component_defs, strict=True):
        box = _aabb(comp, comp_def)
        w = box[2] - box[0]
        h = box[3] - box[1]

        is_overlapping = comp.reference in overlapping_refs
        color = style.infeasible_color if is_overlapping else style.feasible_color

        rect = mpatches.Rectangle(
            (box[0], box[1]),
            w,
            h,
            linewidth=1.2,
            edgecolor=color,
            facecolor=color,
            alpha=style.component_alpha,
            zorder=2,
        )
        ax.add_patch(rect)

        # Label with reference designator
        cx = (box[0] + box[2]) / 2
        cy = (box[1] + box[3]) / 2
        ax.text(
            cx,
            cy,
            comp.reference,
            ha="center",
            va="center",
            fontsize=7,
            fontweight="bold",
            zorder=3,
        )

    # Draw HPWL net bounding boxes
    if show_net_bboxes and nets:
        # Build pad lookup for net bbox computation
        pad_lookup: dict[tuple[str, str], tuple[float, float]] = {}
        for comp in placements:
            for pad in comp.pads:
                pad_lookup[(comp.reference, pad.name)] = (pad.x, pad.y)

        for net in nets:
            xs: list[float] = []
            ys: list[float] = []
            for ref, pin_name in net.pins:
                pos = pad_lookup.get((ref, pin_name))
                if pos is not None:
                    xs.append(pos[0])
                    ys.append(pos[1])

            if len(xs) >= 2:
                min_x, max_x = min(xs), max(xs)
                min_y, max_y = min(ys), max(ys)
                net_rect = mpatches.Rectangle(
                    (min_x, min_y),
                    max_x - min_x,
                    max_y - min_y,
                    linewidth=0.8,
                    edgecolor=style.net_bbox_color,
                    facecolor=style.net_bbox_color,
                    alpha=style.net_bbox_alpha,
                    zorder=1,
                )
                ax.add_patch(net_rect)

    # Set axis limits with some padding
    padding = max(board.width, board.height) * 0.1
    ax.set_xlim(board.min_x - padding, board.max_x + padding)
    ax.set_ylim(board.min_y - padding, board.max_y + padding)
    ax.set_aspect("equal")
    ax.set_xlabel("X (mm)")
    ax.set_ylabel("Y (mm)")
    ax.set_title(title)
    ax.grid(True, alpha=0.2)

    # Legend
    legend_handles = [
        mpatches.Patch(color=style.feasible_color, alpha=style.component_alpha, label="Valid"),
        mpatches.Patch(
            color=style.infeasible_color, alpha=style.component_alpha, label="Overlapping"
        ),
    ]
    if show_net_bboxes and nets:
        legend_handles.append(
            mpatches.Patch(color=style.net_bbox_color, alpha=style.net_bbox_alpha, label="Net HPWL")
        )
    ax.legend(handles=legend_handles, loc="upper right", fontsize="small")

    fig.tight_layout()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(str(output_path), dpi=150, bbox_inches="tight")
    plt.close(fig)

    return output_path


# ---------------------------------------------------------------------------
# Pareto front visualization
# ---------------------------------------------------------------------------


def _compute_pareto_front(points: Sequence[ParetoPoint]) -> list[ParetoPoint]:
    """Identify the Pareto-optimal points from a set of objective-space points.

    A point is Pareto-optimal if no other point dominates it (i.e., no other
    point is better on all objectives simultaneously).

    Only feasible points are considered for the Pareto front.

    Args:
        points: All evaluated points with wirelength and area.

    Returns:
        List of Pareto-optimal points, sorted by wirelength.
    """
    feasible = [p for p in points if p.is_feasible]
    if not feasible:
        return []

    # Sort by wirelength (primary), area (secondary)
    sorted_pts = sorted(feasible, key=lambda p: (p.wirelength, p.area))

    pareto: list[ParetoPoint] = []
    min_area = float("inf")

    for pt in sorted_pts:
        if pt.area < min_area:
            pareto.append(pt)
            min_area = pt.area

    return pareto


def plot_pareto_front(
    recorder: OptimizationRecorder,
    output_path: str | Path,
    *,
    title: str = "Pareto Front: Wirelength vs Area",
    figsize: tuple[float, float] = (10, 8),
) -> Path:
    """Generate a Pareto front scatter plot for multi-objective visualization.

    Plots all evaluated placements in wirelength-vs-area space and
    highlights the Pareto-optimal points. Infeasible points are shown
    in a different color.

    Args:
        recorder: Optimization recorder with Pareto points.
        output_path: File path for the output image (PNG or SVG).
        title: Plot title.
        figsize: Figure size in inches.

    Returns:
        Path to the saved image file.

    Raises:
        ImportError: If matplotlib is not installed.
        ValueError: If recorder has no Pareto points.
    """
    if not recorder.pareto_points:
        raise ValueError("No Pareto points to plot -- add points with add_pareto_point()")

    plt, mpatches = _import_matplotlib()
    output_path = Path(output_path)

    fig, ax = plt.subplots(figsize=figsize)

    # Separate feasible and infeasible points
    feasible = [p for p in recorder.pareto_points if p.is_feasible]
    infeasible = [p for p in recorder.pareto_points if not p.is_feasible]

    # Plot infeasible points
    if infeasible:
        ax.scatter(
            [p.wirelength for p in infeasible],
            [p.area for p in infeasible],
            c="#CCCCCC",
            alpha=0.4,
            s=15,
            label="Infeasible",
            zorder=1,
        )

    # Plot feasible points
    if feasible:
        ax.scatter(
            [p.wirelength for p in feasible],
            [p.area for p in feasible],
            c="#2196F3",
            alpha=0.5,
            s=25,
            label="Feasible",
            zorder=2,
        )

    # Highlight Pareto front
    pareto_front = _compute_pareto_front(recorder.pareto_points)
    if pareto_front:
        pareto_wl = [p.wirelength for p in pareto_front]
        pareto_area = [p.area for p in pareto_front]
        ax.scatter(
            pareto_wl,
            pareto_area,
            c="#FF5722",
            s=80,
            marker="*",
            edgecolors="black",
            linewidths=0.5,
            label="Pareto optimal",
            zorder=4,
        )
        # Connect Pareto front points with a line
        ax.plot(
            pareto_wl,
            pareto_area,
            "r--",
            linewidth=1.0,
            alpha=0.5,
            zorder=3,
        )

    ax.set_xlabel("Wirelength (mm)")
    ax.set_ylabel("Area (mm^2)")
    ax.set_title(title)
    ax.legend(loc="upper right", fontsize="small")
    ax.grid(True, alpha=0.3)

    fig.tight_layout()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(str(output_path), dpi=150, bbox_inches="tight")
    plt.close(fig)

    return output_path
