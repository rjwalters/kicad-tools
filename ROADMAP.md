# kicad-tools Roadmap

**Mission**: Enable AI agents to design PCBs programmatically.

kicad-tools provides the programmatic interface between AI reasoning and KiCad
file manipulation. We complement KiCad—we don't replace it.

## Design Philosophy

1. **Agent-First API** - Every operation callable from code with structured I/O
2. **Rich Feedback** - Validation returns actionable information, not just pass/fail
3. **Round-Trip Fidelity** - Edits preserve existing file structure
4. **Leverage KiCad** - Use kicad-cli for complex operations (Gerbers, rendering); focus our effort on agent-specific capabilities
5. **Hierarchical Abstractions** - Work with circuit blocks, not just primitives

---

## Agent Workflow

The complete agent PCB design workflow:

| Step | Capability | Status |
|------|------------|--------|
| Parse datasheets | `datasheet` module | v0.4.0 |
| Create symbols/footprints | `library` module, generators | v0.4.0 |
| Design schematic | `Schematic`, circuit blocks | v0.5.0 |
| Place components | `PCBEditor`, placement optimization | v0.6.0 |
| Route traces | `Autorouter`, diff pairs, zones | v0.3.0 |
| Validate design | Pure Python DRC, rich feedback | v0.7.0 |
| Export for manufacturing | `AssemblyPackage` (via kicad-cli) | v0.2.0 |
| AI agent integration | MCP server, sessions | v0.8.0 |

**Core challenges for agents** (addressed in planned versions):

| Challenge | Description | Solution |
|-----------|-------------|----------|
| Incomplete Information | Tools don't understand design intent | Design Intent System (v0.9) |
| Combinatorial Explosion | Huge placement/routing solution space | Pattern Library (v0.10) |
| Feedback Latency | Problems discovered late in process | Continuous Validation (v0.9) |
| Knowledge Gap | Agents lack PCB design expertise | Patterns + Explanations (v0.10) |
| Communication Overhead | Verbose state, repeated context | Context Persistence (v0.9) |

---

## Released Versions

### v0.1.0 - Foundation

- S-expression parser with round-trip editing
- Schematic/PCB/Library parsing
- Unified CLI with JSON output
- Manufacturer design rules (JLCPCB, OSHPark, PCBWay, Seeed)
- ERC/DRC report parsing

### v0.2.0 - Manufacturing Readiness

- LCSC parts database integration
- Assembly package export (gerbers, BOM, pick-and-place)
- Fluent query API (`sch.symbols.filter(value="100nF")`)
- A* autorouter with obstacle awareness
- Trace optimizer with length matching
- Footprint validation and repair

### v0.3.0 - Reasoning & Routing

- **LLM reasoning integration** (`kct reason`)
  - PCB state representation for LLMs
  - Command vocabulary for routing actions
  - Feedback/diagnosis for failed operations
- Differential pair routing with length matching
- Bus routing for grouped signals
- Zone-aware routing with thermal relief
- Interactive REPL mode

### v0.4.0 - Library Management & Validation

- **Symbol library tools**: create, edit, save
- **Footprint library tools**: create, edit, save
- **Parametric footprint generators**: SOIC, QFP, QFN, DFN, BGA, chip, SOT, DIP
- **Pure Python DRC** (no kicad-cli required)
- **Datasheet tools**: PDF parsing, pin extraction, symbol generation

### v0.5.0 - Workflow Polish

- **Circuit Blocks**: MCUBlock, CrystalOscillator, USBConnector, DebugHeader, LDOBlock, etc.
- **Schematic Enhancements**: auto-layout, netlist sync validation
- **API Refinements**: unified `Project` class, progress callbacks, actionable errors
- **Examples**: end-to-end PCB design, agent integration examples

### v0.6.0 - Intelligent Placement

**Focus**: Help agents make better initial component placement decisions.

Placement is where agents struggle most—random placement leads to unroutable
boards. This release adds intelligence to the placement step.

**Placement Engine**
- [x] Functional clustering (group related components: MCU+bypass caps, USB+ESD)
- [x] Thermal-aware placement (power components near edges, heat spreading)
- [x] Signal integrity hints (keep high-speed traces short, minimize stubs)
- [x] Edge placement for connectors and interfaces

**Placement Constraints**
- [x] Keep-out zones (mechanical, thermal, RF)
- [x] Component grouping rules (define which components belong together)
- [x] Alignment constraints (grid snap, row/column alignment)

**Agent Integration**
- [x] Placement suggestions with rationale (explainable to LLMs)
- [x] Iterative refinement API (agent can query "what if I move X here?")

### v0.7.0 - Design Feedback & Iteration

**Focus**: Help agents understand failures and improve designs.

When DRC fails or routing is congested, agents need actionable guidance—not
just error codes. This release makes the feedback loop agent-friendly.

**Rich Error Diagnostics** *(inspired by atopile)*
- [x] Source-attached exceptions with file:line:position tracking
- [x] Rich terminal rendering with syntax highlighting (via Rich library)
- [x] Error accumulation for batch validation (report ALL DRC violations)
- [x] S-expression snippet extraction for error context
- [x] Fix suggestions for common errors

**Actionable Feedback**
- [x] DRC errors with specific fix suggestions ("move C1 0.5mm left")
- [x] Routing congestion analysis ("area around U1 pins 4-7 is congested")
- [x] Constraint conflict detection ("keepout overlaps required via")

**Design Quality Metrics**
- [x] Trace length reports (for timing-critical nets)
- [x] Thermal analysis hints (identify hot spots)
- [x] Signal integrity estimates (crosstalk risk, impedance discontinuities)

**Cross-Domain Validation**
- [x] Schematic↔PCB consistency checks
- [x] BOM↔placement verification (all parts placed?)
- [x] Net connectivity validation

**Cost Awareness**
- [x] Manufacturing cost estimation (board + assembly)
- [x] Part availability checking (LCSC stock levels)
- [x] Alternative part suggestions

### v0.8.0 - AI Agent Integration

**Focus**: Enable AI agents to interact with KiCad designs via MCP.

*(Inspired by atopile's MCP server architecture)*

**MCP Server**
- [x] FastMCP server exposing kicad-tools functionality
- [x] Two-tier tool design: discovery (read-only) + action (mutations)
- [x] Session management for stateful placement refinement
- [x] Support stdio and HTTP transports

**MCP Tools**
| Category | Tools | State |
|----------|-------|-------|
| Analysis | `analyze_board`, `get_drc_violations`, `measure_clearance` | Stateless |
| Export | `export_gerbers`, `export_bom`, `export_assembly` | Stateless |
| Placement | `placement_analyze`, `placement_suggestions` | Stateless |
| Placement Session | `start_session`, `query_move`, `apply_move`, `commit`, `rollback` | Stateful |
| Routing | `route_net`, `get_unrouted_nets` | Stateless |

**Layout Preservation** *(inspired by atopile)*
- [x] Hierarchical address-based component matching (`power.ldo.package`)
- [x] Preserve placement/routing when regenerating PCB from schematic
- [x] Anchor-based offset calculation for subcircuit layouts
- [x] Net remapping for name changes
- [x] Incremental layout updates (only touch changed components)

**BOM Enhancements**
- [x] Availability checking with `--check-availability` flag
- [x] JLCPCB assembly validation with `--validate-jlcpcb` flag

---

## Planned Versions

### v0.9.0 - Design Intent & Continuous Feedback

**Focus**: Help agents understand design intent and get real-time feedback.

The biggest gap in agent-driven PCB design is that tools don't understand WHAT
the agent is trying to achieve. When an agent says "route USB_D+" the tools
don't know it's a high-speed differential pair needing impedance control. This
release adds a design intent layer that captures requirements and provides
continuous feedback.

**Design Intent System**
- [ ] Interface declarations with automatic constraint derivation
  ```python
  # Declare intent - system derives constraints automatically
  design.declare_interface("USB_D+", "USB_D-", type="usb2_high_speed")
  # System now enforces: 90Ω diff impedance, length matching, diff pair routing
  ```
- [ ] Built-in interface types: `USB2`, `USB3`, `SPI`, `I2C`, `LVDS`, `DDR`, `Ethernet`
- [ ] Intent-aware validation ("violates USB 2.0 spec" vs "clearance too small")
- [ ] Intent-aware suggestions ("USB traces should be shorter for high-speed")
- [ ] Custom interface definitions for project-specific needs

**Continuous Validation**
- [ ] Real-time DRC during placement sessions (not just at end)
- [ ] Predictive warnings based on current trajectory
  ```python
  session.apply_move("C1", x=45, y=32)
  # Response includes:
  # {"warnings": [{"type": "predictive",
  #   "message": "This position may make routing USB_D+ difficult",
  #   "suggestion": "Consider moving 2mm left"}]}
  ```
- [ ] Routing difficulty estimation before routing
- [ ] Incremental validation (only check affected areas)
- [ ] "What-if" analysis API for evaluating changes without committing

**Intelligent Failure Recovery**
- [ ] Root cause analysis for routing/placement failures
- [ ] Multiple resolution strategies with trade-offs
  ```python
  result = router.route_net("CLK")
  # On failure, returns:
  # {"resolution_strategies": [
  #   {"strategy": "move_component", "target": "C3", "difficulty": "easy"},
  #   {"strategy": "add_via", "position": [44, 30], "difficulty": "medium"},
  #   {"strategy": "reroute_blocking_net", "net": "SPI_MOSI", "difficulty": "hard"}
  # ]}
  ```
- [ ] Similar problem pattern matching ("this looks like bypass_cap_blocking")
- [ ] Difficulty estimation for each fix option

**Context Persistence**
- [ ] Persistent design context across MCP calls
- [ ] Decision history with rationale tracking
- [ ] Learned preferences from agent behavior
- [ ] Efficient state encoding (reduce token overhead)

### v0.10.0 - Pattern Library & Explanations

**Focus**: Encode expert PCB design knowledge for agent use.

Agents shouldn't solve USB layout from first principles every time. A curated
library of validated design patterns accelerates iteration and improves quality.

**Design Pattern Library**
- [ ] Pattern schema with placement rules and constraints
  ```python
  from kicad_tools.patterns import USBDevicePattern

  pattern = USBDevicePattern(
      speed="high_speed",
      connector="type_c",
      esd_protection=True
  )
  placements = pattern.get_placements(connector_at=(10, 50))
  # Returns validated placement meeting USB spec
  ```
- [ ] Core patterns:
  - Power: LDO, buck converter, battery charging, reverse polarity
  - Interfaces: USB 2.0/3.0, SPI, I2C, UART, Ethernet, HDMI
  - MCU: Bypass caps, crystal, debug header, reset circuit
  - Analog: ADC filtering, op-amp configs, sensor interfaces
  - Protection: ESD, TVS, overcurrent, thermal shutdown
- [ ] Pattern validation (verify instantiated pattern meets specs)
- [ ] Pattern adaptation (customize for specific components)
- [ ] User-defined patterns with validation rules

**Explanation System**
- [ ] Queryable explanations for DRC rules
  ```python
  explain("trace_length", net="USB_D+")
  # Returns: {"explanation": "USB 2.0 high-speed signals require...",
  #           "spec_reference": "USB 2.0 section 7.1.5",
  #           "current_value": 45.2, "target_range": [40, 50]}
  ```
- [ ] Design decision rationale tracking
- [ ] Spec references for all constraints
- [ ] Common mistake detection with explanations
- [ ] Learning resources for unfamiliar patterns

**Multi-Resolution Abstraction**
- [ ] High-level operations for common tasks
  ```python
  # High level - declarative
  design.add_subsystem("power_supply",
      components=["U_REG", "C_IN", "C_OUT", "L1"],
      near_edge="left", optimize_for="thermal")

  # Medium level - guided
  optimizer.group_components(refs, strategy="power_supply", anchor="U_REG")

  # Low level - explicit (existing API)
  session.apply_move("U_REG", x=10, y=50)
  ```
- [ ] Automatic decomposition of high-level commands
- [ ] Consistent results across abstraction levels

### v0.11.0 - Typed Interfaces & Constraints

**Focus**: Type-safe circuit blocks and parametric part selection.

*(Inspired by atopile's interface system and constraint solver)*

**Typed Interface System**
- [ ] Interface protocols: `PowerInterface`, `I2CInterface`, `SPIInterface`, `USBInterface`
- [ ] Type-checked connections (catch I2C→SPI misconnections at design time)
- [ ] Automatic reference management (power/ground auto-wiring)
- [ ] Parameter validation on connection (voltage/current compatibility)

**Constraint-Based Part Selection**
- [ ] Interval types for tolerances (`Interval(9.5k, 10.5k)` for 10kΩ ±5%)
- [ ] Unit-aware interval arithmetic (prevent ohms + volts)
- [ ] Parameter constraints with equations (`v_out = v_in * ratio`)
- [ ] LCSC API integration for parametric queries
- [ ] Auto-select parts meeting constraints

**Example**:
```python
# Current approach
ldo = LDOBlock(sch, ref="U1", value="AMS1117-3.3")

# Constraint-based approach
ldo = LDOBlock(sch, ref="U1",
               input_voltage=Interval(4.5, 5.5),
               output_voltage=Interval.from_center_rel(3.3, 0.05),
               output_current=0.5)
# System auto-selects LDO and caps from LCSC
```

### v1.0.0 - Production Ready

**Focus**: API stability, performance, and production deployment.

- [ ] API stability guarantees (semantic versioning)
- [ ] Comprehensive documentation with examples
- [ ] Performance optimization for large boards (1000+ components)
- [ ] Robust error handling across all modules
- [ ] CI/CD integration examples
- [ ] Benchmark suite for regression testing

### Beyond v1.0 - Ecosystem & IDE

**Package Registry** *(inspired by atopile)*
- [ ] Package manifest format for circuit blocks
- [ ] DAG-based dependency resolution
- [ ] Git/registry/file dependency types
- [ ] Publishing workflow via GitHub Actions
- [ ] Community block ecosystem

**IDE Integration**
- [ ] LSP server for `.kicad_sch` and `.kicad_pcb` files
- [ ] Real-time DRC/ERC diagnostics in VS Code
- [ ] Hover for component details, net info
- [ ] Go-to-definition for net references
- [ ] Syntax highlighting for S-expression files

**Simulation Integration**
- [ ] Basic power consumption estimation
- [ ] Thermal estimation without full simulation
- [ ] Signal integrity preview (crosstalk, impedance)
- [ ] Integration hooks for external simulators

---

## Non-Goals

These are explicitly **not** planned:

- **Schematic capture GUI** - Use KiCad for interactive design
- **3D rendering** - Use KiCad's 3D viewer
- **SPICE simulation** - Use dedicated simulators
- **Gerber generation** - Use kicad-cli (battle-tested, reliable)
- **Replacing KiCad** - We complement it, not replace it

---

## Contributing

1. Maintain round-trip fidelity for all file modifications
2. Add tests for new functionality
3. Support `--format json` in CLI commands
4. Return actionable errors from every API
5. Test with real KiCad files (8.0+)

---

## Release History

| Version | Date | Focus |
|---------|------|-------|
| 0.1.0 | 2025-12-29 | Foundation: parsing, CLI, manufacturer rules |
| 0.2.0 | 2025-12-30 | Manufacturing: LCSC, export, autorouter |
| 0.3.0 | 2025-12-31 | Reasoning: LLM integration, diff pairs, zones |
| 0.4.0 | 2025-12-31 | Libraries: symbol/footprint creation, pure Python DRC, datasheets |
| 0.5.0 | 2026-01-02 | Workflow: circuit blocks, Project class, examples |
| 0.6.0 | 2026-01-03 | Intelligent Placement: clustering, thermal, edge, agent API |
| 0.7.0 | 2026-01-04 | Design Feedback: rich errors, actionable suggestions, cost awareness |
| 0.8.0 | 2026-01-05 | AI Agent Integration: MCP server, layout preservation, BOM validation |
