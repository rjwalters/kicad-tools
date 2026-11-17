"""Bottom-up baseline vs. spacing-proxy GA placement comparison.

Runs the experiment requested by issue #2721 (Stelios's bottom-up
hypothesis): place each benchmark board with both algorithms and report
wirelength, overlap area, and wall-clock.

This script intentionally stays in pure-Python and uses only kicad-tools
public APIs so the comparison numbers are reproducible across machines.

Usage::

    uv run python benchmarks/hierarchical/run_baseline_comparison.py

Writes a Markdown table to ``benchmarks/hierarchical/results.md`` and a
JSON snapshot to ``benchmarks/hierarchical/results.json``.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from kicad_tools.optim.bottom_up_placement import (
    HierarchicalPlacementConfig,
    place_hierarchical_from_pcb,
)
from kicad_tools.optim.components import Component, Pin
from kicad_tools.optim.evolutionary import (
    EvolutionaryConfig,
    EvolutionaryPlacementOptimizer,
)
from kicad_tools.schema.pcb import PCB

REPO_ROOT = Path(__file__).resolve().parents[2]


BOARDS = [
    (
        "01-voltage-divider",
        REPO_ROOT / "boards/01-voltage-divider/output/voltage_divider.kicad_pcb",
    ),
    (
        "02-charlieplex-led",
        REPO_ROOT / "boards/02-charlieplex-led/output/charlieplex_3x3.kicad_pcb",
    ),
    (
        "03-usb-joystick",
        REPO_ROOT / "boards/03-usb-joystick/output/usb_joystick.kicad_pcb",
    ),
]


@dataclass
class BoardMetrics:
    """Metrics for one placement run on one board."""

    board: str
    algorithm: str
    components: int
    nets: int
    wall_clock_s: float
    total_wirelength_mm: float
    overlap_pairs: int
    overlap_area_mm2: float
    converged_generations: int | None = None  # GA-only


def _components_from_pcb(pcb: PCB) -> list[Component]:
    """Mirror the Component construction in PlacementOptimizer.from_pcb()."""
    components: list[Component] = []
    for fp in pcb.footprints:
        if fp.pads:
            pad_xs = [p.position[0] for p in fp.pads]
            pad_ys = [p.position[1] for p in fp.pads]
            width = max(pad_xs) - min(pad_xs) + 2.0
            height = max(pad_ys) - min(pad_ys) + 2.0
        else:
            width, height = 2.0, 2.0
        components.append(
            Component(
                ref=fp.reference,
                x=fp.position[0],
                y=fp.position[1],
                rotation=fp.rotation,
                width=max(width, 1.0),
                height=max(height, 1.0),
                pins=[
                    Pin(
                        number=p.number,
                        x=fp.position[0] + p.position[0],
                        y=fp.position[1] + p.position[1],
                        net=p.net_number,
                        net_name=p.net_name,
                    )
                    for p in fp.pads
                ],
            )
        )
    return components


def _wirelength_estimate(
    components: list[Component],
    positions: dict[str, tuple[float, float, float]],
) -> float:
    """Half-perimeter wirelength sum across all multi-pin nets.

    For each net touching >=2 components, sum the bounding-box half-perimeter
    of the touched component centers. This is the standard placement-quality
    proxy used by every academic placer (e.g. NTUPlace).
    """
    # Group component centers by net.
    net_to_points: dict[int, list[tuple[float, float]]] = {}
    for comp in components:
        if comp.ref not in positions:
            continue
        cx, cy, _ = positions[comp.ref]
        for pin in comp.pins:
            if pin.net <= 0:
                continue
            net_to_points.setdefault(pin.net, []).append((cx, cy))

    total = 0.0
    for net_id, pts in net_to_points.items():
        if len(pts) < 2:
            continue
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        total += (max(xs) - min(xs)) + (max(ys) - min(ys))
    return total


def _overlap_metrics(
    components: list[Component],
    positions: dict[str, tuple[float, float, float]],
) -> tuple[int, float]:
    """Count overlapping component pairs and total overlap area."""
    pairs = 0
    total_area = 0.0
    cmap = {c.ref: c for c in components}
    refs = list(positions)
    for i in range(len(refs)):
        for j in range(i + 1, len(refs)):
            ref_a = refs[i]
            ref_b = refs[j]
            a = cmap.get(ref_a)
            b = cmap.get(ref_b)
            if a is None or b is None:
                continue
            ax, ay, _ = positions[ref_a]
            bx, by, _ = positions[ref_b]
            ax_min = ax - a.width / 2.0
            ax_max = ax + a.width / 2.0
            ay_min = ay - a.height / 2.0
            ay_max = ay + a.height / 2.0
            bx_min = bx - b.width / 2.0
            bx_max = bx + b.width / 2.0
            by_min = by - b.height / 2.0
            by_max = by + b.height / 2.0

            ox = min(ax_max, bx_max) - max(ax_min, bx_min)
            oy = min(ay_max, by_max) - max(ay_min, by_min)
            if ox > 0 and oy > 0:
                pairs += 1
                total_area += ox * oy
    return pairs, total_area


def _run_bottom_up(pcb: PCB, board_label: str) -> BoardMetrics:
    """Run the bottom-up baseline and collect metrics."""
    components = _components_from_pcb(pcb)
    n_nets = len({pin.net for c in components for pin in c.pins if pin.net > 0})

    t0 = time.perf_counter()
    result = place_hierarchical_from_pcb(pcb, HierarchicalPlacementConfig())
    elapsed = time.perf_counter() - t0

    pairs, area = _overlap_metrics(components, result.positions)
    wl = _wirelength_estimate(components, result.positions)

    return BoardMetrics(
        board=board_label,
        algorithm="bottom-up",
        components=len(components),
        nets=n_nets,
        wall_clock_s=elapsed,
        total_wirelength_mm=wl,
        overlap_pairs=pairs,
        overlap_area_mm2=area,
    )


def _run_ga(
    pcb: PCB,
    board_label: str,
    generations: int = 20,
    population_size: int = 20,
) -> BoardMetrics:
    """Run the spacing-proxy GA (current default) and collect metrics."""
    components = _components_from_pcb(pcb)
    n_nets = len({pin.net for c in components for pin in c.pins if pin.net > 0})

    # Pin a deterministic random seed at module level so GA results are
    # reproducible. EvolutionaryConfig itself does not expose a random_seed
    # field today; the global random state is what the GA samples.
    import random

    random.seed(42)

    config = EvolutionaryConfig(
        generations=generations,
        population_size=population_size,
        parallel=False,  # serial keeps the comparison wall-clock single-threaded
    )
    t0 = time.perf_counter()
    optimizer = EvolutionaryPlacementOptimizer.from_pcb(pcb, config=config)
    best = optimizer.optimize()
    elapsed = time.perf_counter() - t0

    # Build positions dict from best individual.
    positions: dict[str, tuple[float, float, float]] = {}
    for ref, (x, y) in best.positions.items():
        rot = best.rotations.get(ref, 0.0)
        positions[ref] = (x, y, rot)

    pairs, area = _overlap_metrics(components, positions)
    wl = _wirelength_estimate(components, positions)

    return BoardMetrics(
        board=board_label,
        algorithm=f"ga (gen={generations}, pop={population_size})",
        components=len(components),
        nets=n_nets,
        wall_clock_s=elapsed,
        total_wirelength_mm=wl,
        overlap_pairs=pairs,
        overlap_area_mm2=area,
        converged_generations=len(getattr(optimizer, "_fitness_history", [])),
    )


def _run_baseline_input(pcb: PCB, board_label: str) -> BoardMetrics:
    """Record the as-shipped placement (in the input PCB) as a control."""
    components = _components_from_pcb(pcb)
    n_nets = len({pin.net for c in components for pin in c.pins if pin.net > 0})
    positions = {c.ref: (c.x, c.y, c.rotation) for c in components}

    pairs, area = _overlap_metrics(components, positions)
    wl = _wirelength_estimate(components, positions)
    return BoardMetrics(
        board=board_label,
        algorithm="input (as-shipped)",
        components=len(components),
        nets=n_nets,
        wall_clock_s=0.0,
        total_wirelength_mm=wl,
        overlap_pairs=pairs,
        overlap_area_mm2=area,
    )


def _format_markdown(rows: list[BoardMetrics]) -> str:
    lines: list[str] = []
    lines.append("# Bottom-up baseline vs. spacing-proxy GA")
    lines.append("")
    lines.append(
        "Issue #2721 -- Stelios's hypothesis: bottom-up hierarchical placement "
        'gets "80% of the way there" on non-Analog boards without a '
        "cascaded GA."
    )
    lines.append("")
    lines.append(
        "| Board | Algorithm | Components | Nets | "
        "Wirelength (mm) | Overlap pairs | Overlap area (mm^2) | "
        "Wall-clock (s) | Generations |"
    )
    lines.append("|---|---|---:|---:|---:|---:|---:|---:|---:|")
    for m in rows:
        gens = "" if m.converged_generations is None else str(m.converged_generations)
        lines.append(
            f"| {m.board} | {m.algorithm} | {m.components} | {m.nets} | "
            f"{m.total_wirelength_mm:.2f} | {m.overlap_pairs} | "
            f"{m.overlap_area_mm2:.2f} | {m.wall_clock_s:.3f} | {gens} |"
        )
    lines.append("")
    lines.append("## Reading the table")
    lines.append("")
    lines.append(
        "- **Wirelength** is the half-perimeter sum across all multi-pin nets; lower is better."
    )
    lines.append(
        "- **Overlap pairs** counts pairs of components whose bounding-box "
        "rectangles intersect. Zero is required for a routable placement."
    )
    lines.append("- **Overlap area** is total intersected area; zero is required.")
    lines.append(
        "- **Wall-clock** is single-thread Python wall time on the dev machine; not normalized."
    )
    lines.append(
        "- **Generations** is the number of GA generations actually run "
        "(may be < requested if convergence triggered)."
    )
    return "\n".join(lines) + "\n"


def main() -> None:
    rows: list[BoardMetrics] = []
    for label, pcb_path in BOARDS:
        if not pcb_path.exists():
            print(f"SKIP {label}: file not found ({pcb_path})")
            continue
        print(f"== {label} ==")
        pcb = PCB.load(str(pcb_path))

        m_input = _run_baseline_input(pcb, label)
        rows.append(m_input)
        print(
            f"  input  : wl={m_input.total_wirelength_mm:8.2f}  "
            f"overlap_pairs={m_input.overlap_pairs}"
        )

        m_bu = _run_bottom_up(pcb, label)
        rows.append(m_bu)
        print(
            f"  bottom : wl={m_bu.total_wirelength_mm:8.2f}  "
            f"overlap_pairs={m_bu.overlap_pairs}  "
            f"t={m_bu.wall_clock_s:.3f}s"
        )

        # GA at two settings: fast (20 gen, 20 pop) and tuned (50 gen, 50 pop).
        for gens, pop in [(20, 20), (50, 50)]:
            try:
                m_ga = _run_ga(pcb, label, generations=gens, population_size=pop)
                rows.append(m_ga)
                print(
                    f"  ga gen={gens:3d} pop={pop:3d}: "
                    f"wl={m_ga.total_wirelength_mm:8.2f}  "
                    f"overlap_pairs={m_ga.overlap_pairs}  "
                    f"t={m_ga.wall_clock_s:.3f}s  "
                    f"gens={m_ga.converged_generations}"
                )
            except Exception as exc:  # noqa: BLE001
                print(f"  ga gen={gens} pop={pop} FAILED: {exc!r}")

    out_dir = REPO_ROOT / "benchmarks/hierarchical"
    out_dir.mkdir(parents=True, exist_ok=True)
    md_path = out_dir / "results.md"
    json_path = out_dir / "results.json"
    md_path.write_text(_format_markdown(rows))
    json_path.write_text(json.dumps([asdict(r) for r in rows], indent=2))
    print(f"\nWrote {md_path}")
    print(f"Wrote {json_path}")


if __name__ == "__main__":
    main()
