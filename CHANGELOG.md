# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.10.0] - 2026-01-16

### Added

#### Design Pattern Library (`patterns/`)

Encode expert PCB design knowledge for agent use. Agents can instantiate validated patterns instead of solving common layouts from first principles.

- **Pattern Schema** (#823) - Foundational schema for PCB patterns with placement rules
  - `PCBPattern` base class for placement-based patterns
  - `IntentPattern` base class for constraint-based patterns
  - `Placement`, `PlacementRule`, `RoutingConstraint` schema types
  - Validation and spec compliance checking

- **Core Patterns** (#824) - Comprehensive library of validated design patterns
  - **Power**: `LDOPattern`, `BuckPattern` with thermal and decoupling placement
  - **Timing**: `CrystalPattern`, `OscillatorPattern` with load capacitor placement
  - **Interface**: `USBPattern`, `I2CPattern`, `SPIPattern`, `UARTPattern`, `EthernetPattern`
  - **Analog**: `ADCInputFilter`, `DACOutputFilter`, `OpAmpCircuit`, `SensorInterface`
  - **Protection**: `ESDProtection`, `OvercurrentProtection`, `OvervoltageProtection`, `ReversePolarityProtection`, `ThermalShutdown`

- **Pattern Validation & Adaptation** (#825)
  - `PatternValidator` checks instantiated patterns meet spec requirements
  - `PatternAdapter` generates pattern parameters for specific components
  - Component requirements database for automatic parameter lookup
  - Validation checks: distance, presence, trace length, value matching

- **User-Defined Patterns** (#826)
  - YAML pattern definitions with `PatternLoader`
  - Pattern definition DSL: `define_pattern()`, `placement_rule()`, `routing_constraint()`
  - `PatternRegistry` for registering and discovering patterns
  - Custom validation checks via `register_check()`

#### Explanation System (`explain/`)

Queryable explanations for design rules with spec references and fix suggestions.

- **Queryable Explanations** (#827)
  - `explain(rule_id, context)` returns contextualized rule explanations
  - `explain_violations(violations)` attaches explanations to DRC results
  - `explain_net_constraints(net)` explains why nets have certain constraints
  - Spec references with document name, section, and URL
  - Auto-generated fix suggestions with calculated deltas

- **Common Mistake Detection** (#828)
  - `MistakeDetector` identifies common PCB design mistakes
  - `detect_mistakes(pcb)` scans design for typical errors
  - Categories: decoupling, thermal, signal integrity, power distribution
  - Each mistake includes explanation and fix suggestions

- **Design Decision Rationale** (#829)
  - `DecisionStore` tracks design decisions with rationale
  - `record_decision()` captures why choices were made
  - `PlacementRationale`, `RoutingRationale` for structured tracking
  - `explain_placement()`, `explain_route()` retrieve decision context
  - Persistent storage for decision history

#### Multi-Resolution Abstraction (`design/`)

High-level operations that decompose into low-level commands automatically.

- **Multi-Resolution API** (#830)
  - **High-level**: `design.add_subsystem("power_supply", components=[...], near_edge="left")`
  - **Medium-level**: `optimizer.group_components(refs, strategy="power_supply")`
  - **Low-level**: `session.apply_move("U_REG", x=10, y=50)` (existing API)
  - Automatic decomposition of high-level commands
  - Consistent results across abstraction levels

- **Subsystem Types** (`design/subsystems.py`)
  - `POWER_SUPPLY`, `MCU_CORE`, `CONNECTOR`, `TIMING`, `ANALOG_INPUT`, `INTERFACE`
  - Optimization goals: thermal, routing, compact, signal integrity, mechanical
  - Built-in placement hints and typical component lists

- **Command Decomposition** (`design/decomposition.py`)
  - Breaks high-level operations into atomic moves
  - Pattern-aware placement strategies
  - Constraint propagation from subsystem to component level

### Changed

- **MCP Types Refactored** (#857) - Split `mcp/types.py` into domain-specific modules
  - `types/assembly.py`, `types/board.py`, `types/clearance.py`, `types/drc.py`
  - `types/gerber.py`, `types/intent.py`, `types/placement.py`, `types/routing.py`
  - `types/session.py`, `types/warnings.py`, `types/drc_delta.py`
  - Improved maintainability and reduced file size

## [0.9.3] - 2026-01-06

### Fixed

- **Pad/Via/Obstacle Clearance** (#587) - Include `trace_width/2` in clearance zone calculations
  - Pathfinder checks trace centers, but grid marking must account for trace edges
  - Fixes DRC violations where traces were placed too close to pads/vias
  - Affects `_add_pad_unsafe()`, `_add_pad_vectorized_unsafe()`, `_mark_via()`, `add_obstacle()`

- **build-native CMake Path** (#586) - Use `cpp/` directory CMakeLists.txt for pip-installed packages
  - Fixes `kct build-native` failing when installed via pip
  - Now correctly locates CMakeLists.txt in package installation directory

## [0.9.2] - 2026-01-06

### Added

#### Parallel Processing Infrastructure

- **Thread-Safe Grid** (`router/grid.py`) (#584)
  - Optional `thread_safe=True` parameter for `RoutingGrid`
  - RLock-based synchronization for concurrent grid access
  - `locked()` context manager for atomic multi-operation sequences
  - Zero overhead when disabled (default)

- **Parallel Routing Operations**
  - Parallelize Monte Carlo routing trials (#576)
  - Parallel fitness evaluation in evolutionary optimizer (#578)
  - Parallelize placement conflict detection (#577)
  - Parallelize congestion grid processing (#582)

- **C++ Backend Integration** (`router/core.py`) (#581)
  - Autorouter class now automatically uses C++ backend when available
  - Seamless fallback to Python implementation

#### New Commands

- **`kct build-native`** (`cli/__init__.py`) (#580)
  - Install C++ backend for ~100x performance boost
  - Automatic compilation with nanobind
  - `kct build-native --check` to verify installation

#### Configurable Units

- **Unit System** (`units.py`) (#574)
  - Configurable output units: millimeters (default) or mils
  - `kct config units mm|mils` to set preference
  - Affects all CLI output (congestion, spacing, dimensions)

### Fixed

- **mfr compare --layers** (#568) - Fix `--layers` flag being ignored in manufacturer comparison

## [0.9.1] - 2026-01-06

### Added

#### Router Performance Optimization (Phase 4)

- **C++/nanobind Core** (`router/cpp/`)
  - High-performance C++ implementation of core routing operations
  - nanobind bindings for Python integration
  - ~100x speedup potential for A* neighbor evaluation
  - ~100x speedup potential for route marking operations
  - Graceful fallback when C++ module is not built

- **Algorithm Improvements** (`router/pathfinder.py`)
  - Optimized grid operations for JLCPCB's 0.0635mm grid constraints
  - NumPy-based grid for improved performance
  - Benchmarking infrastructure with `--profile` flag (#554)

#### New Commands

- **`kct init` Command** (`cli/init_cmd.py`)
  - Initialize projects with manufacturer-specific design rules
  - Support for all registered manufacturers

- **Parts Suggest CLI** - Exposed parts suggestion feature as command-line tool

#### Manufacturers

- **FlashPCB** (`manufacturers/flashpcb.py`, `manufacturers/data/flashpcb.yaml`)
  - USA-based PCB fabrication and assembly house
  - 2 and 4 layer boards with 1oz/2oz copper
  - 5 mil trace/space, 8 mil minimum drill
  - 10" × 10" max board size
  - 2-sided assembly (down to 0201)
  - 3, 5, and 10 day lead times

### Fixed

- **Trace Blocking Radius** (#553) - Include clearance in trace blocking radius calculation for DRC compliance
- **fp_text Reference Format** (#565, #547) - Support `fp_text` reference format in placement fix apply
- **mfr export-dru FileNotFoundError** (#550) - Fix FileNotFoundError when running outside project
- **Trace Deletion** (#555) - Delete traces from sexp.children instead of sexp.values
- **Test Cleanup** (#557) - Remove unused pytest import in cpp_backend tests

### Changed

- **Version Management** - `__version__` now automatically reads from `pyproject.toml` via `importlib.metadata`

## [0.9.0] - 2026-01-06

### Added

#### Design Intent System (`kicad_tools.intent`)

Declare high-level design intent and automatically derive constraints:

- **Interface Declarations** (`intent/types.py`, `intent/constraints.py`)
  - `IntentDeclaration` - Declare design intent for net groups
  - `Constraint` - Auto-derived constraints from interface specs
  - `create_intent_declaration()` - Create declarations with automatic constraint derivation
  - `validate_intent()` - Validate declarations against design

- **Built-in Interface Specifications** (`intent/interfaces/`)
  - `USB2HighSpeedSpec`, `USB2FullSpeedSpec`, `USB3Spec` - USB with impedance/length matching
  - `SPISpec` - SPI bus with clock/data timing constraints
  - `I2CSpec` - I2C with pull-up and capacitance requirements
  - `PowerRailSpec` - Power rails with decoupling and current requirements

- **Interface Registry** (`intent/registry.py`)
  - `REGISTRY` - Global registry of interface types
  - Extensible for custom interface definitions
  - Auto-registration of built-in specs

- **MCP Integration** (`mcp/tools/intent.py`)
  - `declare_interface` - Declare interface intent via MCP
  - `list_interfaces` - List available interface types
  - `get_intent_status` - Check constraint satisfaction

#### Continuous Validation (`kicad_tools.drc`)

Real-time DRC during placement sessions:

- **Incremental DRC Engine** (`drc/incremental.py`)
  - `IncrementalDRC` - Efficient DRC with cached state
  - `SpatialIndex` - R-tree spatial indexing for O(log n) region queries
  - `check_move()` - Preview DRC impact without applying
  - `apply_move()` - Apply move and update cached state
  - Performance: <10ms incremental checks for 200+ components

- **DRC Delta in Responses** (`drc/incremental.py`)
  - `DRCDelta` - New vs resolved violations after changes
  - `Violation` - Rich violation details with location and items
  - Integrated into MCP `query_move` and `apply_move` responses

- **Predictive Warnings** (`drc/predictive.py`)
  - `PredictiveAnalyzer` - Anticipate problems before they occur
  - Routing difficulty estimation based on placement
  - Congestion analysis for component density
  - Intent risk checking for declared interfaces
  - Confidence-scored warnings with suggestions

#### Intelligent Failure Recovery (`kicad_tools.router`)

Root cause analysis and resolution strategies for failures:

- **Failure Analysis** (`router/failure_analysis.py`)
  - `RootCauseAnalyzer` - Determine why operations failed
  - `FailureCause` enum - CONGESTION, BLOCKED_PATH, CLEARANCE, etc.
  - `BlockingElement` - Identify what's blocking desired operations
  - `CongestionMap` - Grid-based congestion heatmap

- **Resolution Strategies** (`router/resolution.py`)
  - `ResolutionStrategy` - Actionable fix with difficulty rating
  - Multiple strategies per failure with trade-off analysis
  - Strategy types: MOVE_COMPONENT, ADD_VIA, REROUTE_NET, USE_LAYER
  - Difficulty estimation: EASY, MEDIUM, HARD

#### Context Persistence (`kicad_tools.mcp.context`)

Maintain design context across MCP sessions:

- **Decision Tracking** (`mcp/context.py`)
  - `Decision` - Record design decisions with rationale
  - `DecisionOutcome` - Track success/failure of decisions
  - Decision history for learning and explanation

- **Session Context** (`mcp/context.py`)
  - `SessionContext` - Extended session state
  - `AgentPreferences` - Learned preferences from behavior
  - `StateSnapshot` - Efficient state checkpoints

- **State Summaries** (`mcp/context.py`)
  - Compact state encoding for reduced token overhead
  - Incremental updates instead of full state
  - Queryable decision history

### Changed

- MCP session tools now return DRC delta in `query_move` and `apply_move` responses
- Placement sessions integrate with incremental DRC for real-time validation

### Dependencies

- `rtree>=1.0` - R-tree spatial indexing (optional, falls back to linear scan)

## [0.8.0] - 2026-01-05

### Added

#### MCP Server for AI Agent Integration (`kicad_tools.mcp`)

FastMCP-based server enabling AI agents like Claude to interact with KiCad designs:

- **Core Infrastructure** (`mcp/server.py`)
  - FastMCP server implementation with stdio and HTTP transports
  - Comprehensive error handling with actionable MCP responses
  - Session management for stateful operations
  - CLI: `kct mcp serve` (stdio), `kct mcp serve --http` (HTTP transport)

- **Analysis Tools** (`mcp/tools/analysis.py`)
  - `analyze_board` - Get board summary (layers, components, nets, dimensions)
  - `get_drc_violations` - Run DRC and return violations with locations
  - `measure_clearance` - Check clearance between components/nets

- **Export Tools** (`mcp/tools/export.py`)
  - `export_gerbers` - Generate Gerber files for manufacturing
  - `export_bom` - Generate bill of materials in various formats
  - `export_assembly` - Generate complete manufacturing package (BOM + pick-and-place)

- **Placement Tools** (`mcp/tools/placement.py`)
  - `placement_analyze` - Analyze current placement quality with metrics
  - `placement_suggestions` - Get AI-friendly placement recommendations

- **Session Tools** (`mcp/tools/session.py`)
  - `start_session` - Begin a placement refinement session
  - `query_move` - Preview effect of moving a component
  - `apply_move` - Apply a component move within session
  - `commit` - Commit session changes to file
  - `rollback` - Discard session changes

- **Routing Tools** (`mcp/tools/routing.py`)
  - `route_net` - Route a specific net with configurable strategy
  - `get_unrouted_nets` - List nets that need routing

#### Layout Preservation System (`kicad_tools.layout`)

Preserve component placement and routing when regenerating PCB from schematic:

- **Hierarchical Address Matching** - Match components by hierarchical path (e.g., `power.ldo.C1`)
- **Anchor-Based Positioning** - Calculate subcircuit offsets from anchor components
- **Net Remapping** - Handle net name changes during regeneration
- **Incremental Updates** - Only touch changed components, preserve manual adjustments

#### BOM Command Enhancements

- **Availability Checking** (`--check-availability` flag)
  - Check LCSC/JLCPCB stock availability for BOM parts
  - `--quantity` flag to specify board count (multiplies quantities)
  - Exit code 2 when parts are unavailable
  - CLI: `kicad-bom design.kicad_sch --check-availability --quantity 5`

- **JLCPCB Assembly Validation** (#510)
  - Validate BOM compatibility with JLCPCB assembly service
  - Check for missing LCSC part numbers
  - Verify part availability and assembly category
  - CLI: `kicad-bom design.kicad_sch --validate-jlcpcb`

#### Documentation

- **MCP Server Setup Guide** - Configuration for Claude Desktop and other MCP clients
- **Example Workflows** - End-to-end agent-driven PCB design examples

### Dependencies

- `fastmcp>=2.0,<3` - MCP server framework (optional, in `[mcp]` extra)
- `pydantic>=2.0` - Request/response validation (optional, in `[mcp]` extra)

## [0.7.2] - 2026-01-04

### Added

- `--format json` flag for `kct placement optimize` command (#449)

### Fixed

- Handle empty reference in BOM `grouped()` to prevent IndexError (#452)
- Net-status now correctly detects pad-to-zone connectivity (#451)
- Estimate cost reads components from PCB footprints (#448)

## [0.7.1] - 2026-01-04

### Added

- `--layers` option to autorouter for multi-layer board support (#426)
- Positional argument support to `validate --sync` command (#422)
- Auto-detect target layer from zones in stitch command (#417)
- `name` property to PadState for state export (#430)
- `graphic_items` property to PCB schema (#427)
- pyyaml added to required dependencies (#419)

### Fixed

- Reason agent reporting incorrect board size and layer count (#425)
- Parts availability command silent exit (#424)
- Net-status via-to-zone connectivity detection (#418)
- Router `--grid` and `--clearance` parameters being ignored (#423)

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

[0.9.0]: https://github.com/rjwalters/kicad-tools/releases/tag/v0.9.0
[0.8.0]: https://github.com/rjwalters/kicad-tools/releases/tag/v0.8.0
[0.7.2]: https://github.com/rjwalters/kicad-tools/releases/tag/v0.7.2
[0.7.1]: https://github.com/rjwalters/kicad-tools/releases/tag/v0.7.1
[0.7.0]: https://github.com/rjwalters/kicad-tools/releases/tag/v0.7.0
[0.6.0]: https://github.com/rjwalters/kicad-tools/releases/tag/v0.6.0
[0.5.0]: https://github.com/rjwalters/kicad-tools/releases/tag/v0.5.0
[0.4.0]: https://github.com/rjwalters/kicad-tools/releases/tag/v0.4.0
[0.3.0]: https://github.com/rjwalters/kicad-tools/releases/tag/v0.3.0
[0.2.0]: https://github.com/rjwalters/kicad-tools/releases/tag/v0.2.0
[0.1.0]: https://github.com/rjwalters/kicad-tools/releases/tag/v0.1.0
