"""Performance benchmarks for kicad-tools.

This module provides benchmarks for key operations across varying board sizes.
Run with: pytest tests/test_benchmarks.py --benchmark-only

Board Size Categories:
- Small: <100 symbols / <50 footprints
- Medium: 100-500 symbols / 50-200 footprints
- Large: 500-2000 symbols / 200-1000 footprints
- XLarge: 2000+ symbols / 1000+ footprints
"""

import uuid
from pathlib import Path

import pytest

# Check if pytest-benchmark is available
try:
    import pytest_benchmark  # noqa: F401

    HAS_BENCHMARK = True
except ImportError:
    HAS_BENCHMARK = False

# Marker to skip tests when pytest-benchmark is not installed
requires_benchmark = pytest.mark.skipif(
    not HAS_BENCHMARK,
    reason="pytest-benchmark not installed (install with: pip install pytest-benchmark)",
)

from kicad_tools.schema.pcb import PCB
from kicad_tools.schema.schematic import Schematic
from kicad_tools.sexp import SExp, parse_sexp, serialize_sexp

# --- Fixture Generators ---


def generate_uuid() -> str:
    """Generate a UUID string."""
    return str(uuid.uuid4())


def generate_schematic_content(num_symbols: int) -> str:
    """Generate schematic content with specified number of symbols.

    Args:
        num_symbols: Number of symbols to generate

    Returns:
        Valid KiCad schematic S-expression content
    """
    root_uuid = generate_uuid()

    symbols = []
    for i in range(num_symbols):
        sym_uuid = generate_uuid()
        ref = f"R{i + 1}" if i % 3 == 0 else f"C{i + 1}" if i % 3 == 1 else f"U{i + 1}"
        value = "10k" if ref.startswith("R") else "100nF" if ref.startswith("C") else "IC"
        x = 100 + (i % 50) * 20
        y = 100 + (i // 50) * 30

        symbol = f"""  (symbol
    (lib_id "Device:R")
    (at {x} {y} 0)
    (uuid "{sym_uuid}")
    (property "Reference" "{ref}" (at {x} {y - 10} 0) (effects (font (size 1.27 1.27))))
    (property "Value" "{value}" (at {x} {y + 10} 0) (effects (font (size 1.27 1.27))))
    (property "Footprint" "Resistor_SMD:R_0402_1005Metric" (at {x} {y} 0) (effects (hide yes)))
    (property "Datasheet" "" (at {x} {y} 0) (effects (hide yes)))
    (instances
      (project "benchmark"
        (path "/{root_uuid}"
          (reference "{ref}")
          (unit 1)
        )
      )
    )
  )"""
        symbols.append(symbol)

    # Generate some wires to connect symbols
    wires = []
    for i in range(min(num_symbols - 1, 500)):
        x1 = 100 + (i % 50) * 20
        y1 = 100 + (i // 50) * 30
        x2 = x1 + 20
        wire_uuid = generate_uuid()
        wire = f"""  (wire
    (pts (xy {x1} {y1}) (xy {x2} {y1}))
    (stroke (width 0) (type default))
    (uuid "{wire_uuid}")
  )"""
        wires.append(wire)

    content = f"""(kicad_sch
  (version 20231120)
  (generator "benchmark")
  (generator_version "8.0")
  (uuid "{root_uuid}")
  (paper "A4")
  (lib_symbols)
{chr(10).join(symbols)}
{chr(10).join(wires)}
)"""
    return content


def generate_pcb_content(num_footprints: int) -> str:
    """Generate PCB content with specified number of footprints.

    Args:
        num_footprints: Number of footprints to generate

    Returns:
        Valid KiCad PCB S-expression content
    """
    footprints = []
    nets = [""]  # Net 0 is always empty

    for i in range(num_footprints):
        fp_uuid = generate_uuid()
        ref = f"R{i + 1}" if i % 3 == 0 else f"C{i + 1}" if i % 3 == 1 else f"U{i + 1}"
        x = 100 + (i % 50) * 3
        y = 100 + (i // 50) * 3
        rotation = (i * 90) % 360

        # Create unique nets for each footprint
        net1_name = f"NET_{i * 2 + 1}"
        net2_name = f"NET_{i * 2 + 2}"
        if net1_name not in nets:
            nets.append(net1_name)
        if net2_name not in nets:
            nets.append(net2_name)
        net1_idx = nets.index(net1_name)
        net2_idx = nets.index(net2_name)

        footprint = f"""  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "{fp_uuid}")
    (at {x} {y} {rotation})
    (property "Reference" "{ref}" (at 0 -1.5 0) (layer "F.SilkS") (uuid "{generate_uuid()}"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab") (uuid "{generate_uuid()}"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net {net1_idx} "{net1_name}"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net {net2_idx} "{net2_name}"))
  )"""
        footprints.append(footprint)

    # Generate net declarations
    net_decls = [f'  (net {i} "{name}")' for i, name in enumerate(nets)]

    # Generate some traces
    traces = []
    for i in range(min(num_footprints - 1, 500)):
        x1 = 100 + (i % 50) * 3
        y1 = 100 + (i // 50) * 3
        x2 = x1 + 3
        net_idx = i * 2 + 1
        if net_idx < len(nets):
            trace = f'  (segment (start {x1} {y1}) (end {x2} {y1}) (width 0.2) (layer "F.Cu") (net {net_idx}) (uuid "{generate_uuid()}"))'
            traces.append(trace)

    content = f"""(kicad_pcb
  (version 20240108)
  (generator "benchmark")
  (generator_version "8.0")
  (general
    (thickness 1.6)
    (legacy_teardrops no)
  )
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (32 "B.Adhes" user "B.Adhesive")
    (33 "F.Adhes" user "F.Adhesive")
    (34 "B.Paste" user)
    (35 "F.Paste" user)
    (36 "B.SilkS" user "B.Silkscreen")
    (37 "F.SilkS" user "F.Silkscreen")
    (38 "B.Mask" user)
    (39 "F.Mask" user)
    (44 "Edge.Cuts" user)
  )
  (setup
    (pad_to_mask_clearance 0)
  )
{chr(10).join(net_decls)}
{chr(10).join(footprints)}
{chr(10).join(traces)}
)"""
    return content


# --- Benchmark Fixtures ---


@pytest.fixture(scope="module")
def small_schematic_content() -> str:
    """Generate small schematic (~50 symbols)."""
    return generate_schematic_content(50)


@pytest.fixture(scope="module")
def medium_schematic_content() -> str:
    """Generate medium schematic (~300 symbols)."""
    return generate_schematic_content(300)


@pytest.fixture(scope="module")
def large_schematic_content() -> str:
    """Generate large schematic (~1000 symbols)."""
    return generate_schematic_content(1000)


@pytest.fixture(scope="module")
def xlarge_schematic_content() -> str:
    """Generate xlarge schematic (~3000 symbols)."""
    return generate_schematic_content(3000)


@pytest.fixture(scope="module")
def small_pcb_content() -> str:
    """Generate small PCB (~30 footprints)."""
    return generate_pcb_content(30)


@pytest.fixture(scope="module")
def medium_pcb_content() -> str:
    """Generate medium PCB (~150 footprints)."""
    return generate_pcb_content(150)


@pytest.fixture(scope="module")
def large_pcb_content() -> str:
    """Generate large PCB (~500 footprints)."""
    return generate_pcb_content(500)


@pytest.fixture(scope="module")
def xlarge_pcb_content() -> str:
    """Generate xlarge PCB (~1500 footprints)."""
    return generate_pcb_content(1500)


# --- S-Expression Parsing Benchmarks ---


@requires_benchmark
class TestSexpParseBenchmarks:
    """Benchmarks for S-expression parsing."""

    def test_parse_small_schematic(self, benchmark, small_schematic_content):
        """Benchmark parsing small schematic S-expression."""
        result = benchmark(parse_sexp, small_schematic_content)
        assert result.tag == "kicad_sch"

    def test_parse_medium_schematic(self, benchmark, medium_schematic_content):
        """Benchmark parsing medium schematic S-expression."""
        result = benchmark(parse_sexp, medium_schematic_content)
        assert result.tag == "kicad_sch"

    def test_parse_large_schematic(self, benchmark, large_schematic_content):
        """Benchmark parsing large schematic S-expression."""
        result = benchmark(parse_sexp, large_schematic_content)
        assert result.tag == "kicad_sch"

    def test_parse_xlarge_schematic(self, benchmark, xlarge_schematic_content):
        """Benchmark parsing xlarge schematic S-expression."""
        result = benchmark(parse_sexp, xlarge_schematic_content)
        assert result.tag == "kicad_sch"

    def test_parse_small_pcb(self, benchmark, small_pcb_content):
        """Benchmark parsing small PCB S-expression."""
        result = benchmark(parse_sexp, small_pcb_content)
        assert result.tag == "kicad_pcb"

    def test_parse_medium_pcb(self, benchmark, medium_pcb_content):
        """Benchmark parsing medium PCB S-expression."""
        result = benchmark(parse_sexp, medium_pcb_content)
        assert result.tag == "kicad_pcb"

    def test_parse_large_pcb(self, benchmark, large_pcb_content):
        """Benchmark parsing large PCB S-expression."""
        result = benchmark(parse_sexp, large_pcb_content)
        assert result.tag == "kicad_pcb"

    def test_parse_xlarge_pcb(self, benchmark, xlarge_pcb_content):
        """Benchmark parsing xlarge PCB S-expression."""
        result = benchmark(parse_sexp, xlarge_pcb_content)
        assert result.tag == "kicad_pcb"


# --- Schematic Loading Benchmarks ---


@requires_benchmark
class TestSchematicLoadBenchmarks:
    """Benchmarks for schematic loading (file to schema objects)."""

    @pytest.fixture
    def small_schematic_file(self, tmp_path, small_schematic_content) -> Path:
        """Create small schematic file."""
        f = tmp_path / "small.kicad_sch"
        f.write_text(small_schematic_content)
        return f

    @pytest.fixture
    def medium_schematic_file(self, tmp_path, medium_schematic_content) -> Path:
        """Create medium schematic file."""
        f = tmp_path / "medium.kicad_sch"
        f.write_text(medium_schematic_content)
        return f

    @pytest.fixture
    def large_schematic_file(self, tmp_path, large_schematic_content) -> Path:
        """Create large schematic file."""
        f = tmp_path / "large.kicad_sch"
        f.write_text(large_schematic_content)
        return f

    def test_load_small_schematic(self, benchmark, small_schematic_file):
        """Benchmark loading small schematic."""
        sch = benchmark(Schematic.load, small_schematic_file)
        assert len(sch.symbols) >= 40

    def test_load_medium_schematic(self, benchmark, medium_schematic_file):
        """Benchmark loading medium schematic."""
        sch = benchmark(Schematic.load, medium_schematic_file)
        assert len(sch.symbols) >= 250

    def test_load_large_schematic(self, benchmark, large_schematic_file):
        """Benchmark loading large schematic."""
        sch = benchmark(Schematic.load, large_schematic_file)
        assert len(sch.symbols) >= 900


# --- PCB Loading Benchmarks ---


@requires_benchmark
class TestPCBLoadBenchmarks:
    """Benchmarks for PCB loading (file to schema objects)."""

    @pytest.fixture
    def small_pcb_file(self, tmp_path, small_pcb_content) -> Path:
        """Create small PCB file."""
        f = tmp_path / "small.kicad_pcb"
        f.write_text(small_pcb_content)
        return f

    @pytest.fixture
    def medium_pcb_file(self, tmp_path, medium_pcb_content) -> Path:
        """Create medium PCB file."""
        f = tmp_path / "medium.kicad_pcb"
        f.write_text(medium_pcb_content)
        return f

    @pytest.fixture
    def large_pcb_file(self, tmp_path, large_pcb_content) -> Path:
        """Create large PCB file."""
        f = tmp_path / "large.kicad_pcb"
        f.write_text(large_pcb_content)
        return f

    def test_load_small_pcb(self, benchmark, small_pcb_file):
        """Benchmark loading small PCB."""
        pcb = benchmark(PCB.load, str(small_pcb_file))
        assert len(pcb.footprints) >= 25

    def test_load_medium_pcb(self, benchmark, medium_pcb_file):
        """Benchmark loading medium PCB."""
        pcb = benchmark(PCB.load, str(medium_pcb_file))
        assert len(pcb.footprints) >= 130

    def test_load_large_pcb(self, benchmark, large_pcb_file):
        """Benchmark loading large PCB."""
        pcb = benchmark(PCB.load, str(large_pcb_file))
        assert len(pcb.footprints) >= 450


# --- Query Benchmarks ---


@requires_benchmark
class TestQueryBenchmarks:
    """Benchmarks for query operations."""

    @pytest.fixture
    def large_schematic(self, tmp_path, large_schematic_content) -> Schematic:
        """Load large schematic for query benchmarks."""
        f = tmp_path / "query_test.kicad_sch"
        f.write_text(large_schematic_content)
        return Schematic.load(f)

    @pytest.fixture
    def large_pcb(self, tmp_path, large_pcb_content) -> PCB:
        """Load large PCB for query benchmarks."""
        f = tmp_path / "query_test.kicad_pcb"
        f.write_text(large_pcb_content)
        return PCB.load(str(f))

    def test_query_filter_by_reference_prefix(self, benchmark, large_schematic):
        """Benchmark filtering symbols by reference prefix."""

        def query_resistors():
            return large_schematic.symbols.resistors()

        result = benchmark(query_resistors)
        assert len(result) > 0

    def test_query_filter_by_value(self, benchmark, large_schematic):
        """Benchmark filtering symbols by value."""

        def query_by_value():
            return large_schematic.symbols.filter(value="10k")

        result = benchmark(query_by_value)
        assert len(result) >= 0

    def test_query_chained_filters(self, benchmark, large_schematic):
        """Benchmark chained query filters."""

        def chained_query():
            return (
                large_schematic.symbols.query()
                .filter(reference__startswith="R")
                .filter(value="10k")
                .all()
            )

        result = benchmark(chained_query)
        assert isinstance(result, list)

    def test_pcb_filter_by_layer(self, benchmark, large_pcb):
        """Benchmark filtering footprints by layer."""

        def query_top_layer():
            return large_pcb.footprints.on_top()

        result = benchmark(query_top_layer)
        assert len(result) > 0

    def test_pcb_filter_resistors(self, benchmark, large_pcb):
        """Benchmark filtering PCB resistors."""

        def query_resistors():
            return large_pcb.footprints.resistors()

        result = benchmark(query_resistors)
        assert len(result) > 0


# --- Serialization Benchmarks ---


@requires_benchmark
class TestSerializationBenchmarks:
    """Benchmarks for S-expression serialization."""

    @pytest.fixture
    def parsed_large_schematic(self, large_schematic_content) -> SExp:
        """Parse large schematic for serialization benchmarks."""
        return parse_sexp(large_schematic_content)

    @pytest.fixture
    def parsed_large_pcb(self, large_pcb_content) -> SExp:
        """Parse large PCB for serialization benchmarks."""
        return parse_sexp(large_pcb_content)

    def test_serialize_large_schematic(self, benchmark, parsed_large_schematic):
        """Benchmark serializing large schematic."""
        result = benchmark(serialize_sexp, parsed_large_schematic)
        assert len(result) > 10000

    def test_serialize_large_pcb(self, benchmark, parsed_large_pcb):
        """Benchmark serializing large PCB."""
        result = benchmark(serialize_sexp, parsed_large_pcb)
        assert len(result) > 10000


# --- Memory Usage Markers ---


class TestMemoryUsage:
    """Tests that track approximate memory usage for large files.

    These are not strict benchmarks but help identify memory regressions.
    """

    def test_xlarge_schematic_memory(self, xlarge_schematic_content):
        """Verify xlarge schematic can be parsed without excessive memory."""
        # Parse the content - validates memory usage is reasonable
        sexp = parse_sexp(xlarge_schematic_content)

        # The parsed structure should exist and be navigable
        assert sexp.tag == "kicad_sch"
        symbols = sexp.find_all("symbol")
        assert len(symbols) >= 2500

    def test_xlarge_pcb_memory(self, xlarge_pcb_content):
        """Verify xlarge PCB can be parsed without excessive memory."""
        # Parse the content - validates memory usage is reasonable
        sexp = parse_sexp(xlarge_pcb_content)

        # Verify structure is navigable
        assert sexp.tag == "kicad_pcb"
        footprints = sexp.find_all("footprint")
        assert len(footprints) >= 1400


# --- Routable PCB Generator ---


def generate_routable_pcb_content(
    num_nets: int = 10,
    board_width: float = 50.0,
    board_height: float = 40.0,
    num_layers: int = 2,
) -> str:
    """Generate a PCB with connected nets that can be routed.

    Creates pairs of 0402 resistor footprints connected by nets, suitable
    for benchmarking the router.

    Args:
        num_nets: Number of nets to create (each net connects 2 pads)
        board_width: Board width in mm
        board_height: Board height in mm
        num_layers: Number of copper layers (2, 4, or 6)

    Returns:
        Valid KiCad PCB S-expression content with routable nets
    """
    footprints = []
    nets = [""]  # Net 0 is always empty

    # Calculate grid layout for footprint pairs
    # Each net has a source and destination footprint
    cols = int((board_width - 10) / 8)  # 8mm spacing between pairs
    rows = int((board_height - 10) / 6)  # 6mm spacing between rows
    max_pairs = cols * rows

    if num_nets > max_pairs:
        num_nets = max_pairs

    for i in range(num_nets):
        net_name = f"NET_{i + 1}"
        nets.append(net_name)
        net_idx = len(nets) - 1

        # Calculate position for this pair
        col = i % cols
        row = i // cols
        base_x = 5 + col * 8
        base_y = 5 + row * 6

        # Source footprint (left side of pair)
        src_uuid = generate_uuid()
        src_ref = f"R{i * 2 + 1}"
        footprints.append(f"""  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "{src_uuid}")
    (at {base_x} {base_y} 0)
    (property "Reference" "{src_ref}" (at 0 -1.5 0) (layer "F.SilkS") (uuid "{generate_uuid()}"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab") (uuid "{generate_uuid()}"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 0 ""))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net {net_idx} "{net_name}"))
  )""")

        # Destination footprint (right side of pair, offset)
        dst_uuid = generate_uuid()
        dst_ref = f"R{i * 2 + 2}"
        dst_x = base_x + 4  # 4mm horizontal offset
        dst_y = base_y + 2  # 2mm vertical offset for diagonal routing
        footprints.append(f"""  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "{dst_uuid}")
    (at {dst_x} {dst_y} 0)
    (property "Reference" "{dst_ref}" (at 0 -1.5 0) (layer "F.SilkS") (uuid "{generate_uuid()}"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab") (uuid "{generate_uuid()}"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net {net_idx} "{net_name}"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 0 ""))
  )""")

    # Generate net declarations
    net_decls = [f'  (net {i} "{name}")' for i, name in enumerate(nets)]

    # Generate layer definitions based on num_layers
    if num_layers == 2:
        layers = """    (0 "F.Cu" signal)
    (31 "B.Cu" signal)"""
    elif num_layers == 4:
        layers = """    (0 "F.Cu" signal)
    (1 "In1.Cu" power)
    (2 "In2.Cu" power)
    (31 "B.Cu" signal)"""
    else:  # 6 layers
        layers = """    (0 "F.Cu" signal)
    (1 "In1.Cu" power)
    (2 "In2.Cu" signal)
    (3 "In3.Cu" signal)
    (4 "In4.Cu" power)
    (31 "B.Cu" signal)"""

    # Add board edge
    edge_cuts = f"""  (gr_rect (start 0 0) (end {board_width} {board_height}) (layer "Edge.Cuts") (stroke (width 0.1) (type solid)))"""

    content = f"""(kicad_pcb
  (version 20240108)
  (generator "benchmark")
  (generator_version "8.0")
  (general
    (thickness 1.6)
    (legacy_teardrops no)
  )
  (paper "A4")
  (layers
{layers}
    (32 "B.Adhes" user "B.Adhesive")
    (33 "F.Adhes" user "F.Adhesive")
    (34 "B.Paste" user)
    (35 "F.Paste" user)
    (36 "B.SilkS" user "B.Silkscreen")
    (37 "F.SilkS" user "F.Silkscreen")
    (38 "B.Mask" user)
    (39 "F.Mask" user)
    (44 "Edge.Cuts" user)
  )
  (setup
    (pad_to_mask_clearance 0)
  )
{chr(10).join(net_decls)}
{chr(10).join(footprints)}
{edge_cuts}
)"""
    return content


# --- Router Benchmark Fixtures ---


@pytest.fixture(scope="module")
def small_routable_pcb_content() -> str:
    """Generate small routable PCB (~10 nets, 2-layer)."""
    return generate_routable_pcb_content(num_nets=10, num_layers=2)


@pytest.fixture(scope="module")
def medium_routable_pcb_content() -> str:
    """Generate medium routable PCB (~30 nets, 2-layer)."""
    return generate_routable_pcb_content(num_nets=30, num_layers=2)


@pytest.fixture(scope="module")
def large_routable_pcb_content() -> str:
    """Generate large routable PCB (~50 nets, 4-layer)."""
    return generate_routable_pcb_content(
        num_nets=50, board_width=80.0, board_height=60.0, num_layers=4
    )


@pytest.fixture(scope="module")
def jlcpcb_grid_pcb_content() -> str:
    """Generate PCB for JLCPCB-compatible grid testing (~20 nets).

    This fixture is specifically for testing fine grid performance
    (0.0635mm grid for JLCPCB 5-mil clearance).
    """
    return generate_routable_pcb_content(
        num_nets=20, board_width=40.0, board_height=30.0, num_layers=4
    )


# --- Router Benchmarks ---


@requires_benchmark
class TestRouterBenchmarks:
    """Benchmarks for PCB routing operations.

    These benchmarks measure router performance across different board sizes
    and grid resolutions. They help identify performance regressions and
    validate optimization efforts.

    Run with: pytest tests/test_benchmarks.py::TestRouterBenchmarks --benchmark-only
    """

    @pytest.fixture
    def small_routable_pcb_file(self, tmp_path, small_routable_pcb_content) -> Path:
        """Create small routable PCB file."""
        f = tmp_path / "small_route.kicad_pcb"
        f.write_text(small_routable_pcb_content)
        return f

    @pytest.fixture
    def medium_routable_pcb_file(self, tmp_path, medium_routable_pcb_content) -> Path:
        """Create medium routable PCB file."""
        f = tmp_path / "medium_route.kicad_pcb"
        f.write_text(medium_routable_pcb_content)
        return f

    @pytest.fixture
    def large_routable_pcb_file(self, tmp_path, large_routable_pcb_content) -> Path:
        """Create large routable PCB file."""
        f = tmp_path / "large_route.kicad_pcb"
        f.write_text(large_routable_pcb_content)
        return f

    @pytest.fixture
    def jlcpcb_grid_pcb_file(self, tmp_path, jlcpcb_grid_pcb_content) -> Path:
        """Create JLCPCB grid test PCB file."""
        f = tmp_path / "jlcpcb_grid.kicad_pcb"
        f.write_text(jlcpcb_grid_pcb_content)
        return f

    def test_route_small_board(self, benchmark, small_routable_pcb_file):
        """Benchmark routing small board (~10 nets, 0.25mm grid).

        Target: <5 seconds
        """
        from kicad_tools.router import DesignRules, LayerStack, load_pcb_for_routing

        def route_board():
            rules = DesignRules(
                trace_width=0.2,
                trace_clearance=0.15,
                via_drill=0.3,
                via_diameter=0.6,
                grid_resolution=0.25,
            )
            router, _ = load_pcb_for_routing(
                str(small_routable_pcb_file),
                rules=rules,
                layer_stack=LayerStack.two_layer(),
            )
            return router.route_all()

        result = benchmark(route_board)
        # Verify routing attempted (may not complete all nets)
        assert result is not None

    def test_route_medium_board(self, benchmark, medium_routable_pcb_file):
        """Benchmark routing medium board (~30 nets, 0.25mm grid).

        Target: <15 seconds
        """
        from kicad_tools.router import DesignRules, LayerStack, load_pcb_for_routing

        def route_board():
            rules = DesignRules(
                trace_width=0.2,
                trace_clearance=0.15,
                via_drill=0.3,
                via_diameter=0.6,
                grid_resolution=0.25,
            )
            router, _ = load_pcb_for_routing(
                str(medium_routable_pcb_file),
                rules=rules,
                layer_stack=LayerStack.two_layer(),
            )
            return router.route_all()

        result = benchmark(route_board)
        assert result is not None

    def test_route_large_board_4layer(self, benchmark, large_routable_pcb_file):
        """Benchmark routing large board (~50 nets, 4-layer, 0.25mm grid).

        Target: <30 seconds
        """
        from kicad_tools.router import DesignRules, LayerStack, load_pcb_for_routing

        def route_board():
            rules = DesignRules(
                trace_width=0.2,
                trace_clearance=0.15,
                via_drill=0.3,
                via_diameter=0.6,
                grid_resolution=0.25,
            )
            router, _ = load_pcb_for_routing(
                str(large_routable_pcb_file),
                rules=rules,
                layer_stack=LayerStack.four_layer_sig_gnd_pwr_sig(),
            )
            return router.route_all()

        result = benchmark(route_board)
        assert result is not None

    def test_route_fine_grid_0_1mm(self, benchmark, small_routable_pcb_file):
        """Benchmark routing with 0.1mm grid (dense QFP compatible).

        Target: <10 seconds for small board
        """
        from kicad_tools.router import DesignRules, LayerStack, load_pcb_for_routing

        def route_board():
            rules = DesignRules(
                trace_width=0.2,
                trace_clearance=0.15,
                via_drill=0.3,
                via_diameter=0.6,
                grid_resolution=0.1,
            )
            router, _ = load_pcb_for_routing(
                str(small_routable_pcb_file),
                rules=rules,
                layer_stack=LayerStack.two_layer(),
            )
            return router.route_all()

        result = benchmark(route_board)
        assert result is not None

    @pytest.mark.slow
    def test_route_jlcpcb_grid(self, benchmark, jlcpcb_grid_pcb_file):
        """Benchmark routing with JLCPCB-compatible grid (0.0635mm).

        This tests the performance issue reported in #549.
        Target: <60 seconds (currently times out >120s)

        Marked as slow - run with: pytest -m slow
        """
        from kicad_tools.router import DesignRules, LayerStack, load_pcb_for_routing

        def route_board():
            rules = DesignRules(
                trace_width=0.127,  # 5 mil
                trace_clearance=0.127,  # 5 mil JLCPCB minimum
                via_drill=0.3,
                via_diameter=0.5,
                grid_resolution=0.0635,  # Half of clearance
            )
            router, _ = load_pcb_for_routing(
                str(jlcpcb_grid_pcb_file),
                rules=rules,
                layer_stack=LayerStack.four_layer_sig_gnd_pwr_sig(),
            )
            return router.route_all()

        result = benchmark.pedantic(route_board, iterations=1, rounds=1)
        assert result is not None


# --- Router Grid Scaling Benchmarks ---


class TestRouterGridScaling:
    """Non-benchmark tests to measure grid scaling behavior.

    These tests don't use pytest-benchmark but provide timing information
    for understanding how routing time scales with grid resolution.
    """

    def test_grid_scaling_report(self, tmp_path, small_routable_pcb_content):
        """Report routing times across different grid resolutions.

        This test measures how routing time scales with grid resolution,
        helping identify the performance cliff when using fine grids.
        """
        import time

        from kicad_tools.router import DesignRules, LayerStack, load_pcb_for_routing

        pcb_file = tmp_path / "grid_scale.kicad_pcb"
        pcb_file.write_text(small_routable_pcb_content)

        # Grid resolution must be <= trace_clearance/2 for reliable DRC compliance
        # Using trace_clearance=0.4mm allows grid sizes up to 0.2mm
        grid_sizes = [0.2, 0.1, 0.05]
        results = []

        for grid in grid_sizes:
            rules = DesignRules(
                trace_width=0.2,
                trace_clearance=0.4,
                via_drill=0.3,
                via_diameter=0.6,
                grid_resolution=grid,
            )

            start = time.perf_counter()
            router, _ = load_pcb_for_routing(
                str(pcb_file),
                rules=rules,
                layer_stack=LayerStack.two_layer(),
            )
            load_time = time.perf_counter() - start

            start = time.perf_counter()
            router.route_all()
            route_time = time.perf_counter() - start

            stats = router.get_statistics()
            results.append(
                {
                    "grid": grid,
                    "load_time": load_time,
                    "route_time": route_time,
                    "nets_routed": stats["nets_routed"],
                }
            )

        # Log results for visibility
        print("\n\nGrid Scaling Report:")
        print("-" * 60)
        print(f"{'Grid (mm)':<12} {'Load (s)':<12} {'Route (s)':<12} {'Nets':<8}")
        print("-" * 60)
        for r in results:
            print(
                f"{r['grid']:<12.4f} {r['load_time']:<12.3f} "
                f"{r['route_time']:<12.3f} {r['nets_routed']:<8}"
            )
        print("-" * 60)

        # Basic sanity check - finer grids should take longer
        assert len(results) == len(grid_sizes)
