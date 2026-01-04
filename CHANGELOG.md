# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.7.0] - 2026-01-04

### Added

#### Rich Error Diagnostics (`kicad_tools.exceptions`)

Compiler-style error reporting with actionable context:

- **Source Position Tracking** (`SourcePosition`)
  - File, line, and column tracking for all errors
  - Element type and board coordinates for PCB errors
  - Layer information for multi-layer issues

- **S-expression Snippet Extraction** (`SExpSnippetExtractor`)
  - Extract code context around error locations
  - Line numbers and visual markers
  - Complete element extraction by reference

- **Error Accumulation** (`ErrorAccumulator`)
  - Collect multiple errors instead of failing on first
  - Batch validation for comprehensive feedback
  - `ValidationErrorGroup` for aggregated reporting

- **Rich Terminal Rendering**
  - Syntax-highlighted error output
  - Color-coded severity levels
  - Visual separators for multi-error reports

#### Actionable Feedback (`kicad_tools.drc`, `kicad_tools.analysis`, `kicad_tools.constraints`)

Transform error codes into specific fix suggestions:

- **DRC Fix Suggestions** (`drc/suggestions.py`)
  - "Move C1 0.5mm left to clear U1 pad"
  - Specific component and direction recommendations
  - Clearance violation resolution strategies

- **ERC Root Cause Analysis** (`cli/erc_explain_cmd.py`)
  - Deep analysis of electrical rule violations
  - Root cause identification
  - Step-by-step fix instructions
  - CLI: `kct erc explain <schematic>`

- **Routing Congestion Analysis** (`analysis/congestion.py`)
  - Grid-based density hotspot detection
  - Track length and via count per area
  - Unrouted connection identification
  - Severity classification (LOW → CRITICAL)
  - CLI: `kct analyze congestion <pcb>`

- **Constraint Conflict Detection** (`constraints/conflict.py`)
  - Detects keepout/grouping/region conflicts
  - Conflict types: OVERLAP, CONTRADICTION, IMPOSSIBLE
  - Multiple resolution options with trade-off analysis
  - CLI: `kct constraints check <pcb>`

#### Design Quality Metrics (`kicad_tools.analysis`)

Proactive design quality analysis:

- **Trace Length Reports** (`analysis/trace_length.py`)
  - Per-net and per-segment length calculation
  - Automatic timing-critical net detection (CLK, USB, DDR, LVDS)
  - Differential pair skew calculation
  - Layer change tracking
  - Tolerance checking (actual vs target)
  - CLI: `kct analyze trace-lengths <pcb>`

- **Thermal Analysis** (`analysis/thermal.py`)
  - Heat source identification (regulators, MOSFETs, drivers)
  - Power dissipation estimation by package type
  - Thermal resistance calculations
  - Nearby heat source clustering
  - Copper area and via effectiveness estimation
  - Temperature rise prediction
  - CLI: `kct analyze thermal <pcb>`

- **Signal Integrity Estimates** (`analysis/signal_integrity.py`)
  - Crosstalk risk detection between adjacent traces
  - Impedance discontinuity analysis (width changes, vias, layer transitions)
  - High-speed net identification (USB, LVDS, MIPI, HDMI, DDR, Ethernet)
  - Coupling coefficient calculation
  - Risk level classification (LOW → HIGH)
  - CLI: `kct analyze signal-integrity <pcb>`

#### Cross-Domain Validation (`kicad_tools.validate`)

Consistency checks across design artifacts:

- **Schematic↔PCB Consistency** (`validate/consistency.py`)
  - Component matching between schematic and PCB
  - Net consistency verification
  - Reference designator, value, and footprint sync
  - CLI: `kct validate --consistency`

- **Net Connectivity Validation** (`validate/connectivity.py`)
  - Unrouted net detection
  - Partial connection (island) detection
  - Isolated pad identification
  - Actionable fix suggestions
  - CLI: `kct validate --connectivity`, `kct net-status <pcb>`

- **BOM↔Placement Verification**
  - Component count verification
  - Placement status for all BOM items
  - Missing component detection

#### Cost Awareness (`kicad_tools.cost`)

Manufacturing cost visibility:

- **Manufacturing Cost Estimation** (`cost/estimator.py`)
  - PCB fabrication cost breakdown
  - Component and assembly costs
  - Quantity-based pricing tiers
  - Manufacturer-specific costs (JLCPCB, PCBWay, OSHPark, Seeed)
  - Surface finish and color adjustments
  - Layer and thickness premiums
  - CLI: `kct estimate cost <pcb>`

- **Part Availability Checking** (`cost/availability.py`)
  - LCSC stock level queries
  - Availability status (AVAILABLE, LOW_STOCK, OUT_OF_STOCK, DISCONTINUED)
  - Lead time reporting
  - Minimum order quantity handling
  - Price break calculations
  - CLI: `kct parts availability <schematic>`

- **Alternative Part Suggestions** (`cost/alternatives.py`)
  - Suggest replacements for unavailable parts
  - Price difference comparison
  - Pin-compatible alternatives
  - Basic part preferences for JLCPCB assembly
  - CLI: `kct suggest alternatives <schematic>`

#### CLI Commands

New commands for v0.7.0 features:

- `kct erc explain <file>` - ERC root cause analysis with fix suggestions
- `kct analyze congestion <pcb>` - Routing congestion hotspots
- `kct analyze trace-lengths <pcb>` - Timing-critical trace analysis
- `kct analyze thermal <pcb>` - Thermal hotspot detection
- `kct analyze signal-integrity <pcb>` - Crosstalk and impedance analysis
- `kct constraints check <pcb>` - Constraint conflict detection
- `kct validate --consistency` - Schematic↔PCB sync check
- `kct validate --connectivity` - Net connectivity validation
- `kct estimate cost <pcb>` - Manufacturing cost estimation
- `kct parts availability <schematic>` - LCSC stock checking
- `kct suggest alternatives <schematic>` - Alternative part suggestions

## [0.6.0] - 2026-01-03

### Added

#### Intelligent Placement Engine (`kicad_tools.optim`)

Comprehensive placement optimization for PCB component positioning:

- **Functional Clustering** (`optim/clustering.py`)
  - `ClusterDetector` - Detects related component groups
  - `detect_functional_clusters()` - Find MCU+bypass caps, timing circuits, etc.
  - `ClusterType` enum: POWER, TIMING, INTERFACE, ANALOG

- **Thermal-Aware Placement** (`optim/thermal.py`)
  - `ThermalClass` - Heat source, heat sensitive, neutral classification
  - `classify_thermal_properties()` - Auto-detect thermal components
  - `detect_thermal_constraints()` - Generate separation constraints
  - Heat sources pushed to edges, separated from sensitive components

- **Signal Integrity Hints** (`optim/signal_integrity.py`)
  - `SignalClass` - CLOCK, HIGH_SPEED, DIFFERENTIAL, ANALOG, POWER, GENERAL
  - `classify_nets()` - Auto-classify nets by name patterns
  - `analyze_placement_for_si()` - Get SI warnings
  - `get_si_score()` - Placement quality metric

- **Edge Placement** (`optim/edge_placement.py`)
  - `detect_edge_components()` - Find connectors, mounting holes
  - `EdgeConstraint` - Keep components at board edges
  - `BoardEdges` - Edge detection and constraint generation

- **Keep-out Zones** (`optim/keepout.py`)
  - `KeepoutZone` - Define no-go areas
  - `create_keepout_from_component()` - Auto-generate from components
  - `load_keepout_zones_from_yaml()` - Load from config file
  - `validate_keepout_violations()` - Check placement against zones

#### Placement Constraints (`kicad_tools.optim`)

Declarative constraint system for component placement:

- **Component Grouping** (`optim/constraints.py`)
  - `GroupingConstraint` - Keep components together
  - `SpatialConstraint` - Position constraints
  - `validate_grouping_constraints()` - Check constraint satisfaction

- **Alignment** (`optim/alignment.py`)
  - `snap_to_grid()` - Grid alignment
  - `align_components()` - Row/column alignment
  - `distribute_components()` - Even spacing
  - `AlignmentConstraint` - Declarative alignment rules

#### Agent Integration (`kicad_tools.optim`)

AI-friendly APIs for placement optimization:

- **Placement Suggestions** (`optim/suggestions.py`)
  - `PlacementSuggestion` - Suggested position with rationale
  - `generate_placement_suggestions()` - Get improvement ideas
  - `explain_placement()` - Why is component here?
  - `suggest_improvement()` - Specific move suggestions

- **Iterative Refinement** (`optim/session.py`, `optim/query.py`)
  - `PlacementSession` - Stateful refinement session
  - `query_position()` - "What if I move X here?"
  - `query_swap()` - "What if I swap X and Y?"
  - `find_best_position()` - Optimal position search
  - `process_json_request()` - JSON API for agents

#### CLI Commands

New placement optimization commands:

- `kicad-tools placement optimize --cluster` - Enable clustering
- `kicad-tools placement optimize --thermal` - Enable thermal awareness
- `kicad-tools placement optimize --edge-detect` - Edge component detection
- `kicad-tools placement optimize --keepout FILE` - Load keepout zones
- `kicad-tools placement suggest` - Get suggestions
- `kicad-tools placement refine` - Interactive refinement

## [0.5.0] - 2026-01-02

### Added

#### Circuit Blocks (`kicad_tools.schematic.blocks`)

Reusable, tested circuit blocks for common schematic patterns:

- **MCUBlock** - Microcontroller with configurable bypass capacitors
- **CrystalOscillator** - Crystal/oscillator with load capacitors
- **USBConnector** - USB-B/Mini/Micro/Type-C with optional ESD protection
- **DebugHeader** - SWD, JTAG, and Tag-Connect programming headers
- **I2CPullups** - I2C bus pull-up resistors with optional filtering capacitors
- **ResetButton** - Reset switch with debounce capacitor and optional ESD protection
- **BarrelJackInput** - DC barrel jack with reverse polarity protection
- **USBPowerInput** - USB power input with fuse and ESD protection
- **BatteryInput** - Battery connector with protection circuitry
- **LDOBlock** - Linear regulator with input/output capacitors
- **LEDIndicator** - Status LED with current-limiting resistor
- **DecouplingCaps** - Decoupling capacitor placement helper

All blocks feature:
- Ports for inter-block wiring
- Configurable component values
- Optional protection circuits
- Factory functions for common configurations

#### Schematic Enhancements

- **Auto-layout** (`schematic.layout`)
  - Automatic symbol placement to avoid overlaps
  - Configurable spacing and alignment options

- **Netlist Sync Validation** (`validate.netlist`)
  - Compare schematic netlist to PCB netlist
  - Detect missing/extra components and nets
  - CLI: `kct validate-sync schematic.kicad_sch pcb.kicad_pcb`

#### API Refinements

- **Unified Project Class** (`kicad_tools.Project`)
  - Load complete KiCad projects (`.kicad_pro`)
  - Cross-reference schematics to PCBs
  - Find unplaced components
  - Export manufacturing packages

- **Actionable Error Messages** (`kicad_tools.exceptions`)
  - All exceptions include `error_code` field
  - Structured `to_dict()` for JSON serialization
  - Fix suggestions included in error messages

- **Progress Callbacks** (`kicad_tools.progress`)
  - `ProgressCallback` protocol for monitoring long operations
  - `ProgressContext` context manager for scoped progress
  - `create_json_callback()` for automation
  - Cancelable operations via callback return value
  - Integration with router, DRC, and export operations

#### Examples & Documentation

- **End-to-End Example** (`examples/05-end-to-end/`)
  - Complete workflow from schematic to manufacturing files
  - Demonstrates circuit blocks, routing, and export

- **Agent Integration Examples** (`examples/agent-integration/`)
  - Claude integration with tool definitions and prompts
  - OpenAI integration with function calling
  - Common utilities for error handling and API wrapping

### Changed

- Improved exception hierarchy with structured error information
- Router operations now support progress callbacks
- DRC checker supports progress reporting

## [0.4.0] - 2025-12-31

### Added

#### Library Management

- **Symbol Library Tools** (`kicad_tools.library`)
  - Create and save KiCad symbol libraries programmatically
  - Symbol creation with pin and property editing
  - Round-trip editing preserves existing content

- **Footprint Library Tools** (`kicad_tools.library`)
  - `FootprintLibrary` class for loading `.pretty` directories
  - Footprint creation and save support
  - Parametric footprint generators for common package types:
    - SOIC (8, 14, 16 pins)
    - QFP (32, 44, 48, 64, 100 pins)
    - QFN (16, 24, 32, 48 pins)
    - DFN (6, 8, 10, 12 pins)
    - BGA (grid-based ball patterns)
    - Chip resistors/capacitors (0402, 0603, 0805, 1206)
    - SOT variants (SOT-23, SOT-223, SOT-89)
    - Through-hole (DIP, SIP, pin headers)
  - CLI: `kct footprint generate <type>` for parametric generation

#### Pure Python DRC (no kicad-cli required)

- **DRCChecker** (`kicad_tools.validate`) - standalone design rule checking
  - Clearance checks: trace-to-trace, trace-to-pad, pad-to-pad, via-to-trace
  - Dimension checks: trace width, via drill, annular ring
  - Edge clearance checks: copper-to-board-edge minimum
  - Silkscreen checks: line width, text height, over-pad detection
  - Manufacturer rule presets for JLCPCB, PCBWay, OSHPark, Seeed
  - Support for 2-layer, 4-layer, and 6-layer configurations
  - CLI: `kct check board.kicad_pcb --mfr jlcpcb`

#### Datasheet Tools

- **Datasheet Infrastructure** (`kicad_tools.datasheet`)
  - Datasheet search across multiple sources
  - PDF download with local caching
  - PDF to markdown conversion using MarkItDown
  - Image extraction from PDF datasheets
  - Pin table extraction from datasheets
  - Package dimension extraction and footprint matching
  - Symbol generation from datasheet pin tables
  - End-to-end part import workflow
  - CLI: `kct datasheet search/download/convert/extract-images/extract-tables`

#### CLI Enhancements

- **Netlist analysis commands**: `kct netlist analyze/list/show/check/compare/export`
- **Footprint generation**: `kct footprint generate --list` shows available types

### Changed

- Manufacturer rule presets expanded to support 2, 4, and 6-layer configurations
- DRC checking can now run without kicad-cli installed

### Fixed

- Test suite compatibility with pure Python DRC checker

## [0.3.0] - 2025-12-31

### Added

#### Documentation & Examples

- **Comprehensive API documentation** with type hints for mypy --strict compliance
- **User guide and tutorials** in `docs/` directory
- **Example projects** demonstrating common workflows:
  - Schematic analysis
  - BOM generation
  - DRC checking
  - Autorouting
  - LLM-driven routing

#### CLI Enhancements

- **Progress indicators** for long-running operations with real-time feedback
- **Configuration file support** (`.kicad-tools.yaml`) for CLI default options
- **Interactive REPL mode** (`kct repl`) for multi-step workflows
- **Interactive routing preview** for visualizing routing decisions

#### Router Enhancements

- **Differential pair routing** with length matching support
- **Bus routing** for grouped signal routing
- **45° diagonal routing** for shorter, cleaner traces
- **Zone-aware routing** with flood fill and thermal relief generation
- **Staircase pattern compression** - optimizes alternating horizontal/diagonal micro-segments into clean paths

#### LLM Integration

- **PCB Reasoning Agent** (`kicad_tools.reasoning`) for LLM-driven layout decisions
  - State representation suitable for LLM prompts
  - Command vocabulary for routing actions
  - Feedback/diagnosis for failed operations
  - CLI: `kct reason` with `--analyze`, `--export-state`, `--interactive` modes

#### File Format Support

- **`.kicad_mod`** footprint library files
- **`.kicad_dru`** design rules files
- **KiCad 7+** net class handling compatibility

#### Footprint Validation

- **Standard library comparison** - validate footprints against KiCad's official library
  - Auto-detects KiCad library path (macOS, Linux, Windows)
  - Compares pad positions, sizes, and shapes with configurable tolerance
  - CLI: `kct footprint compare-standard`

#### Testing & Quality

- **Edge case test coverage** for parser and schema modules
- **Integration tests** with real KiCad project files
- **Performance benchmarks** for large board handling

### Changed

- **S-expression parser optimized** for 50%+ performance improvement on large files
- **README updated** to highlight agent-focused development goal
- **Custom exception hierarchy** with context and actionable suggestions

### Fixed

- Pad obstacle clearance calculation for PTH routing
- All linting and formatting issues

### Removed

- Unused numpy dependency (router now uses pure Python)
- Hardcoded project paths from CLI modules
- Orphaned CLI modules consolidated into unified CLI
- Duplicate S-expression implementations

## [0.2.0] - 2025-12-30

### Added

#### Manufacturing Readiness (Planned Features)

- **LCSC Parts Integration** (`kicad_tools.parts`)
  - `LCSCClient` for direct part lookups from JLCPCB's LCSC database
  - Part search with filtering by stock, category, and specifications
  - Local caching for offline use and reduced API calls
  - CLI: `kct parts lookup`, `kct parts search`, `kct parts cache`

- **Assembly Package Export** (`kicad_tools.export`)
  - Complete manufacturing packages (Gerbers + BOM + CPL) for fabrication
  - Multi-manufacturer format support (JLCPCB, PCBWay, OSHPark, Seeed)
  - CLI: `kct export assembly`, `kct export gerbers`, `kct export pnp`

- **Fluent Query API** (`kicad_tools.query`)
  - Django-ORM style filtering for symbols and footprints
  - `sch.symbols.filter(value="100nF")`, `pcb.footprints.by_reference("U1")`
  - Chainable filters with field lookups

- **Project Class** (`kicad_tools.project`)
  - Cross-reference schematics to PCBs
  - Unified project-level queries for finding unplaced components

#### Bonus Features (Beyond Roadmap)

- **A* Autorouter with Obstacle Awareness** (`kicad_tools.router`)
  - Intelligent obstacle detection and avoidance
  - Net-class aware routing strategies (power, clock, audio, digital)
  - Multi-layer support with automatic via placement
  - Negotiated and greedy routing strategies
  - `CommandInterpreter` for high-level routing commands
  - CLI: `kct route`

- **Trace Optimizer** (`kicad_tools.optim`)
  - Post-routing trace optimization
  - Length matching support for differential pairs

- **Manufacturer DRC Configuration**
  - Configurable design rules per manufacturer
  - Rule comparison tools between manufacturers
  - CLI: `kct mfr compare`

- **Footprint Validation & Repair** (`kicad_tools.footprints`)
  - Detect pad spacing issues, overlaps, courtyard violations
  - Automatic repair with configurable minimum gaps
  - CLI: `kct validate-footprints`, `kct fix-footprints`

- **Placement Conflict Detection** (`kicad_tools.optim`)
  - Detect component overlaps and courtyard violations
  - Conflict resolution suggestions

### Changed

- CLI version now dynamically reads from package metadata instead of hardcoded value

### Fixed

- Test for `LCSCClient.close()` method now patches at correct module level

## [0.1.0] - 2025-12-29

### Added

- **Core S-expression parser** with round-trip editing support
- **Schematic parsing** - symbols, wires, labels, hierarchy traversal
- **PCB parsing** - footprints, nets, traces, vias, zones
- **Symbol library parsing** - read and query KiCad symbol libraries
- **ERC report parsing** - parse KiCad Electrical Rules Check reports
- **DRC report parsing** - parse KiCad Design Rules Check reports
- **Manufacturer profiles** - design rules for JLCPCB, OSHPark, PCBWay, Seeed
- **PCB autorouter** - A* pathfinding with pluggable heuristics
  - Net class awareness (power, clock, audio, digital)
  - Multi-layer support with via management
  - Congestion-aware routing
- **Unified CLI** (`kct` or `kicad-tools`) with subcommands:
  - `kct symbols` - list symbols in schematics
  - `kct nets` - trace and analyze nets
  - `kct bom` - generate bill of materials
  - `kct erc` - run/parse ERC reports
  - `kct drc` - run/parse DRC reports with manufacturer rules
- **PCB tools** - `kicad-pcb-query` and `kicad-pcb-modify`
- **Library tools** - `kicad-lib-symbols`
- JSON output for all CLI commands

### Dependencies

- Python 3.10+
- numpy >= 1.20

[0.7.0]: https://github.com/rjwalters/kicad-tools/releases/tag/v0.7.0
[0.6.0]: https://github.com/rjwalters/kicad-tools/releases/tag/v0.6.0
[0.5.0]: https://github.com/rjwalters/kicad-tools/releases/tag/v0.5.0
[0.4.0]: https://github.com/rjwalters/kicad-tools/releases/tag/v0.4.0
[0.3.0]: https://github.com/rjwalters/kicad-tools/releases/tag/v0.3.0
[0.2.0]: https://github.com/rjwalters/kicad-tools/releases/tag/v0.2.0
[0.1.0]: https://github.com/rjwalters/kicad-tools/releases/tag/v0.1.0
