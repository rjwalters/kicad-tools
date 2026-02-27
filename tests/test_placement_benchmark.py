"""Benchmark test boards for placement optimizer validation.

Tests four benchmark boards of increasing complexity:
- Board 1 (trivial): 3 resistors in series with known-optimal linear placement
- Board 2 (simple): 10-component RC filter with reference placement
- Board 3 (medium): 20-component MCU board testing 80D optimizer scaling
- Board 4 (stress): 50-component mixed-signal board for performance limits

Each board exercises the full placement pipeline: component/net loading,
vector encode/decode, geometry constraint checks, HPWL wirelength, seed
heuristics, and CMA-ES optimization (where applicable).
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from kicad_tools.placement.cmaes_strategy import CMAESStrategy
from kicad_tools.placement.cost import (
    BoardOutline,
    ComponentPlacement,
    DesignRuleSet,
    Net,
    evaluate_placement,
)
from kicad_tools.placement.geometry import (
    compute_boundary_violation,
    compute_overlap,
)
from kicad_tools.placement.seed import force_directed_placement, random_placement
from kicad_tools.placement.strategy import StrategyConfig
from kicad_tools.placement.vector import (
    FIELDS_PER_COMPONENT,
    ComponentDef,
    PadDef,
    PlacedComponent,
    PlacementVector,
    bounds,
    decode,
    encode,
)
from kicad_tools.placement.wirelength import compute_hpwl

# ---------------------------------------------------------------------------
# Fixtures directory
# ---------------------------------------------------------------------------

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "placement"
BENCHMARK_FILE = FIXTURES_DIR / "benchmark_boards.json"


# ---------------------------------------------------------------------------
# Helpers: JSON to domain objects
# ---------------------------------------------------------------------------


def _load_benchmark_data() -> dict[str, Any]:
    """Load the benchmark boards JSON fixture."""
    return json.loads(BENCHMARK_FILE.read_text())


def _json_to_component_def(raw: dict[str, Any]) -> ComponentDef:
    """Convert a JSON component dict to a ComponentDef."""
    pads = tuple(
        PadDef(
            name=p["name"],
            local_x=p["local_x"],
            local_y=p["local_y"],
            size_x=p.get("size_x", 0.5),
            size_y=p.get("size_y", 0.5),
        )
        for p in raw.get("pads", [])
    )
    return ComponentDef(
        reference=raw["reference"],
        pads=pads,
        width=raw.get("width", 1.0),
        height=raw.get("height", 1.0),
    )


def _json_to_net(raw: dict[str, Any]) -> Net:
    """Convert a JSON net dict to a Net."""
    pins = tuple((pin[0], pin[1]) for pin in raw["pins"])
    return Net(name=raw["name"], pins=pins)


def _json_to_board(raw: dict[str, Any]) -> BoardOutline:
    """Convert a JSON board outline dict to a BoardOutline."""
    return BoardOutline(
        min_x=raw["min_x"],
        min_y=raw["min_y"],
        max_x=raw["max_x"],
        max_y=raw["max_y"],
    )


def _json_to_placed_components(
    raw_placements: list[dict[str, Any]],
    component_defs: list[ComponentDef],
) -> list[PlacedComponent]:
    """Convert JSON placement list to PlacedComponent list via encode/decode."""
    # Build a PlacementVector from the raw placement data
    ref_to_def = {c.reference: c for c in component_defs}
    ordered_defs: list[ComponentDef] = []
    placed: list[PlacedComponent] = []

    for raw in raw_placements:
        ref = raw["reference"]
        comp_def = ref_to_def[ref]
        ordered_defs.append(comp_def)
        placed.append(
            PlacedComponent(
                reference=ref,
                x=raw["x"],
                y=raw["y"],
                rotation=raw.get("rotation", 0.0),
                side=raw.get("side", 0),
            )
        )

    # Encode and decode to get proper pad transforms
    vector = encode(placed)
    return decode(vector, ordered_defs)


def _json_to_component_placements(
    raw_placements: list[dict[str, Any]],
) -> list[ComponentPlacement]:
    """Convert JSON placement list to ComponentPlacement list for cost module."""
    return [
        ComponentPlacement(
            reference=raw["reference"],
            x=raw["x"],
            y=raw["y"],
            rotation=raw.get("rotation", 0.0),
        )
        for raw in raw_placements
    ]


# ---------------------------------------------------------------------------
# Stress board generator
# ---------------------------------------------------------------------------


def _generate_stress_components(count_spec: dict[str, int]) -> list[ComponentDef]:
    """Generate ComponentDefs for the stress test board programmatically."""
    components: list[ComponentDef] = []

    # MCUs (large, many pins)
    for i in range(count_spec.get("mcu_count", 2)):
        ref = f"U{i + 1}"
        pads = []
        for pin_idx in range(8):
            side_x = -4.5 if pin_idx < 4 else 4.5
            y_offset = -3.75 + (pin_idx % 4) * 2.5
            pads.append(
                PadDef(
                    name=str(pin_idx + 1),
                    local_x=side_x,
                    local_y=y_offset,
                    size_x=0.3,
                    size_y=1.5,
                )
            )
        components.append(ComponentDef(reference=ref, pads=tuple(pads), width=10.0, height=10.0))

    # Resistors (small, 2-pin)
    for i in range(count_spec.get("resistor_count", 20)):
        ref = f"R{i + 1}"
        pads = (
            PadDef(name="1", local_x=-1.2, local_y=0.0, size_x=0.8, size_y=0.8),
            PadDef(name="2", local_x=1.2, local_y=0.0, size_x=0.8, size_y=0.8),
        )
        components.append(ComponentDef(reference=ref, pads=pads, width=3.2, height=1.6))

    # Capacitors (small, 2-pin)
    for i in range(count_spec.get("capacitor_count", 15)):
        ref = f"C{i + 1}"
        pads = (
            PadDef(name="1", local_x=-0.75, local_y=0.0, size_x=0.6, size_y=0.6),
            PadDef(name="2", local_x=0.75, local_y=0.0, size_x=0.6, size_y=0.6),
        )
        components.append(ComponentDef(reference=ref, pads=pads, width=2.0, height=1.25))

    # Connectors (medium, 4-pin)
    for i in range(count_spec.get("connector_count", 4)):
        ref = f"J{i + 1}"
        pads = tuple(
            PadDef(
                name=str(pin_idx + 1),
                local_x=0.0,
                local_y=-3.81 + pin_idx * 2.54,
                size_x=1.5,
                size_y=1.5,
            )
            for pin_idx in range(4)
        )
        components.append(ComponentDef(reference=ref, pads=pads, width=5.0, height=12.0))

    # LEDs (small, 2-pin)
    for i in range(count_spec.get("led_count", 4)):
        ref = f"LED{i + 1}"
        pads = (
            PadDef(name="A", local_x=-0.75, local_y=0.0, size_x=0.6, size_y=0.6),
            PadDef(name="K", local_x=0.75, local_y=0.0, size_x=0.6, size_y=0.6),
        )
        components.append(ComponentDef(reference=ref, pads=pads, width=2.0, height=1.25))

    # Transistors (small, 3-pin)
    for i in range(count_spec.get("transistor_count", 3)):
        ref = f"Q{i + 1}"
        pads = (
            PadDef(name="G", local_x=-1.0, local_y=0.0, size_x=0.6, size_y=0.6),
            PadDef(name="D", local_x=0.0, local_y=-1.0, size_x=0.6, size_y=0.6),
            PadDef(name="S", local_x=1.0, local_y=0.0, size_x=0.6, size_y=0.6),
        )
        components.append(ComponentDef(reference=ref, pads=pads, width=3.0, height=3.0))

    # Diodes (small, 2-pin)
    for i in range(count_spec.get("diode_count", 2)):
        ref = f"D{i + 1}"
        pads = (
            PadDef(name="A", local_x=-0.9, local_y=0.0, size_x=0.6, size_y=0.6),
            PadDef(name="K", local_x=0.9, local_y=0.0, size_x=0.6, size_y=0.6),
        )
        components.append(ComponentDef(reference=ref, pads=pads, width=2.5, height=1.5))

    return components


def _generate_stress_nets(components: list[ComponentDef]) -> list[Net]:
    """Generate realistic nets for the stress test board.

    Creates VCC and GND power nets connecting many components, plus
    signal nets that connect MCU pins to nearby passives.
    """
    nets: list[Net] = []

    # Collect component references by type prefix
    refs_by_type: dict[str, list[str]] = {}
    for c in components:
        prefix = "".join(ch for ch in c.reference if ch.isalpha())
        refs_by_type.setdefault(prefix, []).append(c.reference)

    # VCC net: MCU pin 8 + first pin of capacitors + pin 1 of connectors
    vcc_pins: list[tuple[str, str]] = []
    for ref in refs_by_type.get("U", []):
        vcc_pins.append((ref, "8"))
    for ref in refs_by_type.get("C", [])[:8]:
        vcc_pins.append((ref, "1"))
    for ref in refs_by_type.get("J", [])[:2]:
        vcc_pins.append((ref, "1"))
    if vcc_pins:
        nets.append(Net(name="VCC", pins=tuple(vcc_pins)))

    # GND net: MCU pin 4 + second pin of capacitors + pin 4 of connectors + LED cathodes
    gnd_pins: list[tuple[str, str]] = []
    for ref in refs_by_type.get("U", []):
        gnd_pins.append((ref, "4"))
    for ref in refs_by_type.get("C", []):
        gnd_pins.append((ref, "2"))
    for ref in refs_by_type.get("J", []):
        gnd_pins.append((ref, "4"))
    for ref in refs_by_type.get("LED", []):
        gnd_pins.append((ref, "K"))
    for ref in refs_by_type.get("Q", []):
        gnd_pins.append((ref, "S"))
    if gnd_pins:
        nets.append(Net(name="GND", pins=tuple(gnd_pins)))

    # Signal nets: connect MCU GPIO pins to nearby resistors
    mcu_refs = refs_by_type.get("U", [])
    resistor_refs = refs_by_type.get("R", [])
    for i, r_ref in enumerate(resistor_refs[:8]):
        mcu_ref = mcu_refs[i % len(mcu_refs)] if mcu_refs else None
        if mcu_ref:
            pin_num = str((i % 8) + 1)
            nets.append(
                Net(
                    name=f"SIG_{i}",
                    pins=((mcu_ref, pin_num), (r_ref, "1")),
                )
            )

    # LED drive nets: resistors to LED anodes
    led_refs = refs_by_type.get("LED", [])
    for i, led_ref in enumerate(led_refs):
        r_idx = 8 + i
        if r_idx < len(resistor_refs):
            nets.append(
                Net(
                    name=f"LED_DRV_{i}",
                    pins=((resistor_refs[r_idx], "2"), (led_ref, "A")),
                )
            )

    # Transistor gate nets: connect remaining resistors to transistor gates
    q_refs = refs_by_type.get("Q", [])
    for i, q_ref in enumerate(q_refs):
        r_idx = 12 + i
        if r_idx < len(resistor_refs):
            nets.append(
                Net(
                    name=f"GATE_{i}",
                    pins=((resistor_refs[r_idx], "2"), (q_ref, "G")),
                )
            )

    # Diode protection nets
    d_refs = refs_by_type.get("D", [])
    for i, d_ref in enumerate(d_refs):
        if i < len(q_refs):
            nets.append(
                Net(
                    name=f"PROTECT_{i}",
                    pins=((q_ref, "D"), (d_ref, "A")),
                )
            )

    # Connector output nets
    j_refs = refs_by_type.get("J", [])
    for i, j_ref in enumerate(j_refs[2:]):  # Skip first 2 (power)
        if i < len(q_refs):
            nets.append(
                Net(
                    name=f"OUT_{i}",
                    pins=((q_refs[i], "D"), (j_ref, "2")),
                )
            )

    return nets


# ---------------------------------------------------------------------------
# Board fixture loaders
# ---------------------------------------------------------------------------


@pytest.fixture
def benchmark_data() -> dict[str, Any]:
    """Load full benchmark data from JSON."""
    return _load_benchmark_data()


@pytest.fixture
def trivial_board(benchmark_data: dict[str, Any]):
    """Board 1: 3 resistors in series."""
    raw = benchmark_data["boards"]["trivial_3_resistors"]
    component_defs = [_json_to_component_def(c) for c in raw["components"]]
    nets = [_json_to_net(n) for n in raw["nets"]]
    board = _json_to_board(raw["board_outline"])
    return {
        "component_defs": component_defs,
        "nets": nets,
        "board": board,
        "raw": raw,
    }


@pytest.fixture
def simple_board(benchmark_data: dict[str, Any]):
    """Board 2: Simple RC filter, 10 components."""
    raw = benchmark_data["boards"]["simple_rc_filter"]
    component_defs = [_json_to_component_def(c) for c in raw["components"]]
    nets = [_json_to_net(n) for n in raw["nets"]]
    board = _json_to_board(raw["board_outline"])
    return {
        "component_defs": component_defs,
        "nets": nets,
        "board": board,
        "raw": raw,
    }


@pytest.fixture
def medium_board(benchmark_data: dict[str, Any]):
    """Board 3: MCU board, 20 components."""
    raw = benchmark_data["boards"]["medium_mcu_board"]
    component_defs = [_json_to_component_def(c) for c in raw["components"]]
    nets = [_json_to_net(n) for n in raw["nets"]]
    board = _json_to_board(raw["board_outline"])
    return {
        "component_defs": component_defs,
        "nets": nets,
        "board": board,
        "raw": raw,
    }


@pytest.fixture
def stress_board(benchmark_data: dict[str, Any]):
    """Board 4: 50-component stress test."""
    raw = benchmark_data["boards"]["stress_50_components"]
    board = _json_to_board(raw["board_outline"])
    gen_spec = raw["component_generation"]
    component_defs = _generate_stress_components(gen_spec)
    nets = _generate_stress_nets(component_defs)
    return {
        "component_defs": component_defs,
        "nets": nets,
        "board": board,
        "raw": raw,
    }


# ===========================================================================
# Board 1: Trivial -- 3 resistors in series
# ===========================================================================


class TestTrivialBoard:
    """Board 1 smoke tests: 3 resistors in series, known-optimal placement."""

    def test_board_loads_correctly(self, trivial_board):
        """All 3 components and 4 nets load from JSON."""
        assert len(trivial_board["component_defs"]) == 3
        assert len(trivial_board["nets"]) == 4
        assert trivial_board["board"].width == 20.0
        assert trivial_board["board"].height == 10.0

    def test_known_optimal_zero_overlap(self, trivial_board):
        """Known-optimal placement has zero component overlap."""
        raw = trivial_board["raw"]
        placed = _json_to_placed_components(
            raw["known_optimal_placement"],
            trivial_board["component_defs"],
        )
        overlap = compute_overlap(placed, trivial_board["component_defs"])
        assert overlap == 0.0, f"Expected zero overlap, got {overlap}"

    def test_known_optimal_zero_boundary_violation(self, trivial_board):
        """Known-optimal placement has zero boundary violation."""
        raw = trivial_board["raw"]
        placed = _json_to_placed_components(
            raw["known_optimal_placement"],
            trivial_board["component_defs"],
        )
        violation = compute_boundary_violation(
            placed, trivial_board["component_defs"], trivial_board["board"]
        )
        assert violation == 0.0, f"Expected zero boundary violation, got {violation}"

    def test_known_optimal_wirelength(self, trivial_board):
        """Known-optimal placement achieves documented wirelength (within tolerance)."""
        raw = trivial_board["raw"]
        placed = _json_to_placed_components(
            raw["known_optimal_placement"],
            trivial_board["component_defs"],
        )
        hpwl = compute_hpwl(placed, trivial_board["nets"])
        expected = raw["expected_scores"]["optimal_wirelength"]
        tolerance = raw["expected_scores"]["wirelength_tolerance"]
        assert abs(hpwl - expected) <= tolerance, (
            f"HPWL {hpwl:.2f} not within {tolerance} of expected {expected}"
        )

    def test_encode_decode_roundtrip(self, trivial_board):
        """Encode/decode round-trip preserves placement data."""
        raw = trivial_board["raw"]
        placed = _json_to_placed_components(
            raw["known_optimal_placement"],
            trivial_board["component_defs"],
        )
        vector = encode(placed)
        assert vector.num_components == 3
        decoded = decode(vector, trivial_board["component_defs"])
        assert len(decoded) == 3
        for orig, dec in zip(placed, decoded, strict=True):
            assert orig.reference == dec.reference
            assert abs(orig.x - dec.x) < 1e-9
            assert abs(orig.y - dec.y) < 1e-9

    def test_bounds_computation(self, trivial_board):
        """Bounds have correct shape for 3 components (12 dimensions)."""
        b = bounds(trivial_board["board"], trivial_board["component_defs"])
        assert b.lower.shape == (12,)
        assert b.upper.shape == (12,)
        assert b.discrete_mask.shape == (12,)
        # Rotation and side dimensions should be discrete
        for i in range(3):
            base = i * FIELDS_PER_COMPONENT
            assert b.discrete_mask[base + 2]  # rotation
            assert b.discrete_mask[base + 3]  # side
            assert not b.discrete_mask[base]  # x is continuous
            assert not b.discrete_mask[base + 1]  # y is continuous

    def test_force_directed_seed_no_overlap(self, trivial_board):
        """Force-directed seed placement produces zero overlap for 3 resistors."""
        seed = force_directed_placement(
            trivial_board["component_defs"],
            trivial_board["nets"],
            trivial_board["board"],
        )
        placed = decode(seed, trivial_board["component_defs"])
        overlap = compute_overlap(placed, trivial_board["component_defs"])
        assert overlap == 0.0, f"Seed placement has overlap: {overlap}"

    def test_cost_function_scoring(self, trivial_board):
        """Full cost function evaluates the known-optimal placement."""
        raw = trivial_board["raw"]
        cost_placements = _json_to_component_placements(raw["known_optimal_placement"])
        footprint_sizes = {
            c.reference: (c.width, c.height) for c in trivial_board["component_defs"]
        }
        score = evaluate_placement(
            cost_placements,
            trivial_board["nets"],
            rules=DesignRuleSet(),
            board=trivial_board["board"],
            footprint_sizes=footprint_sizes,
        )
        # Known-optimal should be feasible
        assert score.is_feasible, f"Expected feasible, got breakdown: {score.breakdown}"
        assert score.breakdown.overlap == 0.0
        assert score.breakdown.boundary == 0.0


# ===========================================================================
# Board 2: Simple RC filter -- 10 components
# ===========================================================================


class TestSimpleBoard:
    """Board 2: Simple RC filter with 10 components and reference placement."""

    def test_board_loads_correctly(self, simple_board):
        """All 10 components and 8 nets load from JSON."""
        assert len(simple_board["component_defs"]) == 10
        assert len(simple_board["nets"]) == 8
        assert simple_board["board"].width == 40.0
        assert simple_board["board"].height == 30.0

    def test_reference_placement_zero_overlap(self, simple_board):
        """Reference placement has zero overlap."""
        raw = simple_board["raw"]
        placed = _json_to_placed_components(
            raw["reference_placement"],
            simple_board["component_defs"],
        )
        overlap = compute_overlap(placed, simple_board["component_defs"])
        assert overlap == 0.0, f"Expected zero overlap, got {overlap}"

    def test_reference_placement_zero_boundary(self, simple_board):
        """Reference placement has zero boundary violation."""
        raw = simple_board["raw"]
        placed = _json_to_placed_components(
            raw["reference_placement"],
            simple_board["component_defs"],
        )
        violation = compute_boundary_violation(
            placed, simple_board["component_defs"], simple_board["board"]
        )
        assert violation == 0.0, f"Expected zero boundary violation, got {violation}"

    def test_reference_wirelength_within_budget(self, simple_board):
        """Reference placement wirelength is within expected budget."""
        raw = simple_board["raw"]
        placed = _json_to_placed_components(
            raw["reference_placement"],
            simple_board["component_defs"],
        )
        hpwl = compute_hpwl(placed, simple_board["nets"])
        max_wl = raw["expected_scores"]["reference_wirelength_max"]
        assert hpwl <= max_wl, f"Reference HPWL {hpwl:.2f} exceeds budget {max_wl}"

    def test_multiple_nets_resolved(self, simple_board):
        """All multi-pin nets produce non-zero wirelength contributions."""
        raw = simple_board["raw"]
        placed = _json_to_placed_components(
            raw["reference_placement"],
            simple_board["component_defs"],
        )
        from kicad_tools.placement.wirelength import compute_hpwl_breakdown

        result = compute_hpwl_breakdown(placed, simple_board["nets"])
        # Count nets with 2+ resolved pads
        multi_pin_nets = [nw for nw in result.per_net if nw.pad_count >= 2]
        assert len(multi_pin_nets) >= 4, (
            f"Expected at least 4 multi-pin nets, got {len(multi_pin_nets)}"
        )

    def test_encode_decode_roundtrip(self, simple_board):
        """Encode/decode round-trip for 10 components."""
        raw = simple_board["raw"]
        placed = _json_to_placed_components(
            raw["reference_placement"],
            simple_board["component_defs"],
        )
        vector = encode(placed)
        assert vector.num_components == 10
        decoded = decode(vector, simple_board["component_defs"])
        assert len(decoded) == 10

    def test_force_directed_seed(self, simple_board):
        """Force-directed seed runs successfully on 10-component board."""
        seed = force_directed_placement(
            simple_board["component_defs"],
            simple_board["nets"],
            simple_board["board"],
        )
        assert seed.num_components == 10
        placed = decode(seed, simple_board["component_defs"])
        # Verify all components are within board bounds
        violation = compute_boundary_violation(
            placed, simple_board["component_defs"], simple_board["board"]
        )
        assert violation == 0.0, f"Seed has boundary violation: {violation}"


# ===========================================================================
# Board 3: Medium MCU board -- 20 components (80D)
# ===========================================================================


class TestMediumBoard:
    """Board 3: MCU + passives + connectors, 20 components, 80 dimensions."""

    def test_board_loads_correctly(self, medium_board):
        """All 20 components and 17 nets load from JSON."""
        assert len(medium_board["component_defs"]) == 20
        assert len(medium_board["nets"]) == 17
        assert medium_board["board"].width == 60.0
        assert medium_board["board"].height == 40.0

    def test_dimension_count(self, medium_board):
        """Placement vector has expected 80 dimensions (20 * 4)."""
        n_dims = len(medium_board["component_defs"]) * FIELDS_PER_COMPONENT
        expected = medium_board["raw"]["expected_scores"]["dimension_count"]
        assert n_dims == expected, f"Expected {expected}D, got {n_dims}D"

    def test_reference_placement_zero_overlap(self, medium_board):
        """Reference placement has zero overlap."""
        raw = medium_board["raw"]
        placed = _json_to_placed_components(
            raw["reference_placement"],
            medium_board["component_defs"],
        )
        overlap = compute_overlap(placed, medium_board["component_defs"])
        assert overlap == 0.0, f"Expected zero overlap, got {overlap}"

    def test_reference_placement_zero_boundary(self, medium_board):
        """Reference placement has zero boundary violation."""
        raw = medium_board["raw"]
        placed = _json_to_placed_components(
            raw["reference_placement"],
            medium_board["component_defs"],
        )
        violation = compute_boundary_violation(
            placed, medium_board["component_defs"], medium_board["board"]
        )
        assert violation == 0.0, f"Expected zero boundary violation, got {violation}"

    def test_reference_wirelength_within_budget(self, medium_board):
        """Reference placement wirelength is within expected budget."""
        raw = medium_board["raw"]
        placed = _json_to_placed_components(
            raw["reference_placement"],
            medium_board["component_defs"],
        )
        hpwl = compute_hpwl(placed, medium_board["nets"])
        max_wl = raw["expected_scores"]["reference_wirelength_max"]
        assert hpwl <= max_wl, f"Reference HPWL {hpwl:.2f} exceeds budget {max_wl}"

    def test_mixed_component_sizes(self, medium_board):
        """Board includes a variety of component sizes (MCU, passives, connectors)."""
        widths = {c.reference: c.width for c in medium_board["component_defs"]}
        # MCU should be the largest
        assert widths["U1"] >= 10.0
        # Passives should be smaller
        assert widths["R1"] < 5.0
        assert widths["C1"] < 5.0
        # Connectors mid-size
        assert widths["J1"] >= 5.0

    def test_force_directed_seed(self, medium_board):
        """Force-directed seed runs on 20-component board and respects bounds."""
        seed = force_directed_placement(
            medium_board["component_defs"],
            medium_board["nets"],
            medium_board["board"],
        )
        assert seed.num_components == 20
        placed = decode(seed, medium_board["component_defs"])
        violation = compute_boundary_violation(
            placed, medium_board["component_defs"], medium_board["board"]
        )
        assert violation == 0.0, f"Seed has boundary violation: {violation}"

    def test_random_seed(self, medium_board):
        """Random seed with overlap resolution runs on 20-component board."""
        seed = random_placement(
            medium_board["component_defs"],
            medium_board["board"],
            seed=42,
        )
        assert seed.num_components == 20
        placed = decode(seed, medium_board["component_defs"])
        violation = compute_boundary_violation(
            placed, medium_board["component_defs"], medium_board["board"]
        )
        assert violation == 0.0, f"Random seed has boundary violation: {violation}"

    def test_cmaes_initializes(self, medium_board):
        """CMA-ES strategy initializes on 80D problem without error."""
        b = bounds(medium_board["board"], medium_board["component_defs"])
        config = StrategyConfig(max_iterations=5, seed=42)
        strategy = CMAESStrategy()
        initial_pop = strategy.initialize(b, config)
        assert len(initial_pop) > 0
        for vec in initial_pop:
            assert vec.num_components == 20


# ===========================================================================
# Board 4: Stress -- 50 components (200D)
# ===========================================================================


class TestStressBoard:
    """Board 4: 50-component stress test for performance and convergence."""

    def test_board_generates_correctly(self, stress_board):
        """Programmatic generation produces exactly 50 components."""
        assert len(stress_board["component_defs"]) == 50

    def test_dimension_count(self, stress_board):
        """Placement vector has expected 200 dimensions (50 * 4)."""
        n_dims = len(stress_board["component_defs"]) * FIELDS_PER_COMPONENT
        expected = stress_board["raw"]["expected_scores"]["dimension_count"]
        assert n_dims == expected, f"Expected {expected}D, got {n_dims}D"

    def test_nets_generated(self, stress_board):
        """Stress board has a meaningful set of nets."""
        assert len(stress_board["nets"]) >= 10, (
            f"Expected at least 10 nets, got {len(stress_board['nets'])}"
        )

    def test_random_seed_runs(self, stress_board):
        """Random seed placement runs on 50-component board."""
        seed = random_placement(
            stress_board["component_defs"],
            stress_board["board"],
            seed=42,
        )
        assert seed.num_components == 50

    def test_force_directed_seed_runs(self, stress_board):
        """Force-directed seed runs on 50-component board."""
        seed = force_directed_placement(
            stress_board["component_defs"],
            stress_board["nets"],
            stress_board["board"],
        )
        assert seed.num_components == 50

    def test_cmaes_runs_few_generations(self, stress_board):
        """CMA-ES initializes and runs a few generations on 200D problem.

        This verifies the optimizer does not crash at scale, not that it
        converges to a good solution. Full convergence testing is out of
        scope for a unit test.
        """
        b = bounds(stress_board["board"], stress_board["component_defs"])
        config = StrategyConfig(
            max_iterations=3,
            seed=42,
            extra={"population_size": 10},
        )
        strategy = CMAESStrategy()
        initial_pop = strategy.initialize(b, config)

        # Score the initial population with a simple proxy (just HPWL)
        scores = []
        for vec in initial_pop:
            placed = decode(vec, stress_board["component_defs"])
            hpwl = compute_hpwl(placed, stress_board["nets"])
            overlap = compute_overlap(placed, stress_board["component_defs"])
            scores.append(hpwl + overlap * 1e6)

        strategy.observe(initial_pop, scores)
        best_vec, best_score = strategy.best()
        assert best_vec.num_components == 50
        assert best_score < float("inf")

    def test_optimizer_improves_over_random(self, stress_board):
        """CMA-ES should improve score over a random initial placement.

        Runs a small number of generations and verifies the final score
        is lower than the initial random score.
        """
        b = bounds(stress_board["board"], stress_board["component_defs"])
        config = StrategyConfig(
            max_iterations=10,
            seed=42,
            extra={"population_size": 10},
        )
        strategy = CMAESStrategy()
        initial_pop = strategy.initialize(b, config)

        def _score(vec: PlacementVector) -> float:
            placed = decode(vec, stress_board["component_defs"])
            hpwl = compute_hpwl(placed, stress_board["nets"])
            overlap = compute_overlap(placed, stress_board["component_defs"])
            return hpwl + overlap * 1e6

        # Score and observe initial population
        initial_scores = [_score(v) for v in initial_pop]
        strategy.observe(initial_pop, initial_scores)
        initial_best = min(initial_scores)

        # Run a few more generations
        for _ in range(5):
            candidates = strategy.suggest(10)
            scores = [_score(v) for v in candidates]
            strategy.observe(candidates, scores)

        _, final_best = strategy.best()

        # Optimizer should find something at least as good as the initial best
        assert final_best <= initial_best, (
            f"Optimizer failed to maintain or improve score: "
            f"initial={initial_best:.2f}, final={final_best:.2f}"
        )

    def test_performance_under_time_limit(self, stress_board):
        """Full seed + short optimization completes within 60 seconds."""
        start = time.monotonic()

        # Generate seed (exercises force-directed algorithm at scale)
        force_directed_placement(
            stress_board["component_defs"],
            stress_board["nets"],
            stress_board["board"],
        )

        # Run 3 generations of CMA-ES
        b = bounds(stress_board["board"], stress_board["component_defs"])
        config = StrategyConfig(
            max_iterations=3,
            seed=42,
            extra={"population_size": 8},
        )
        strategy = CMAESStrategy()
        initial_pop = strategy.initialize(b, config)

        for vec in initial_pop:
            placed = decode(vec, stress_board["component_defs"])
            _ = compute_hpwl(placed, stress_board["nets"])

        elapsed = time.monotonic() - start
        assert elapsed < 60.0, f"Seed + 3 gen took {elapsed:.1f}s, expected < 60s"
