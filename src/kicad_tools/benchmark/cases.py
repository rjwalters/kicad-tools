"""Benchmark case definitions for routing tests."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from kicad_tools.router import Autorouter


class Difficulty(Enum):
    """Difficulty classification for benchmark cases."""

    EASY = "easy"
    MEDIUM = "medium"
    HARD = "hard"


@dataclass
class BenchmarkCase:
    """Definition of a benchmark test case.

    A benchmark case can be either:
    - A real PCB file (pcb_path)
    - A synthetic board (generator)
    """

    name: str
    difficulty: Difficulty

    # Expected performance thresholds
    expected_completion: float = 1.0  # 0.0-1.0
    expected_max_vias: int | None = None

    # PCB file source (mutually exclusive with generator)
    pcb_path: str | None = None

    # Synthetic generator (mutually exclusive with pcb_path)
    generator: Callable[[], Autorouter] | None = None
    generator_name: str | None = None  # For serialization

    # Nets to skip during routing (power/ground usually)
    skip_nets: list[str] = field(default_factory=list)

    # Design rules overrides
    grid_resolution: float = 0.1
    trace_width: float = 0.2
    trace_clearance: float = 0.15

    def __post_init__(self):
        """Validate case configuration."""
        if self.pcb_path is None and self.generator is None:
            raise ValueError(f"Case {self.name} must have either pcb_path or generator")
        if self.pcb_path is not None and self.generator is not None:
            raise ValueError(f"Case {self.name} cannot have both pcb_path and generator")

    def is_synthetic(self) -> bool:
        """Return True if this is a synthetic benchmark."""
        return self.generator is not None

    def get_pcb_path(self, base_dir: Path | None = None) -> Path | None:
        """Get resolved PCB file path."""
        if self.pcb_path is None:
            return None
        path = Path(self.pcb_path)
        if not path.is_absolute() and base_dir:
            path = base_dir / path
        return path


# Registry of standard benchmark cases
BENCHMARK_CASES: list[BenchmarkCase] = [
    # Simple boards (should always achieve 100%)
    BenchmarkCase(
        name="charlieplex_3x3",
        pcb_path="boards/02-charlieplex-led/charlieplex_3x3.kicad_pcb",
        expected_completion=1.0,
        expected_max_vias=12,
        difficulty=Difficulty.EASY,
        skip_nets=["VCC", "GND"],
        grid_resolution=0.1,
        trace_width=0.3,
        trace_clearance=0.2,
    ),
    # Medium complexity
    BenchmarkCase(
        name="usb_joystick",
        pcb_path="boards/03-usb-joystick/usb_joystick.kicad_pcb",
        expected_completion=0.9,
        expected_max_vias=20,
        difficulty=Difficulty.MEDIUM,
        skip_nets=["VCC", "GND", "VBUS"],
        grid_resolution=0.1,
        trace_width=0.2,
        trace_clearance=0.15,
    ),
]


def get_case_by_name(name: str) -> BenchmarkCase | None:
    """Find a benchmark case by name."""
    for case in BENCHMARK_CASES:
        if case.name == name:
            return case
    return None


def list_case_names() -> list[str]:
    """List all available benchmark case names."""
    return [case.name for case in BENCHMARK_CASES]


def register_case(case: BenchmarkCase) -> None:
    """Register a new benchmark case."""
    if get_case_by_name(case.name) is not None:
        raise ValueError(f"Case {case.name} already registered")
    BENCHMARK_CASES.append(case)
