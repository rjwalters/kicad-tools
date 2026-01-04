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
| Place components | `PCBEditor`, placement optimization | v0.2.0 |
| Route traces | `Autorouter`, diff pairs, zones | v0.3.0 |
| Validate design | Pure Python DRC | v0.4.0 |
| Export for manufacturing | `AssemblyPackage` (via kicad-cli) | v0.2.0 |

**Current gaps** (addressed in planned versions):
- Intelligent initial placement based on circuit function
- Actionable feedback when designs fail validation
- Design quality metrics beyond pass/fail

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

---

## Planned Versions

### v0.7.0 - Design Feedback & Iteration

**Focus**: Help agents understand failures and improve designs.

When DRC fails or routing is congested, agents need actionable guidance—not
just error codes. This release makes the feedback loop agent-friendly.

**Rich Error Diagnostics** *(inspired by atopile)*
- [ ] Source-attached exceptions with file:line:position tracking
- [ ] Rich terminal rendering with syntax highlighting (via Rich library)
- [ ] Error accumulation for batch validation (report ALL DRC violations)
- [ ] S-expression snippet extraction for error context
- [ ] Fix suggestions for common errors

**Actionable Feedback**
- [ ] DRC errors with specific fix suggestions ("move C1 0.5mm left")
- [ ] Routing congestion analysis ("area around U1 pins 4-7 is congested")
- [ ] Constraint conflict detection ("keepout overlaps required via")

**Design Quality Metrics**
- [ ] Trace length reports (for timing-critical nets)
- [ ] Thermal analysis hints (identify hot spots)
- [ ] Signal integrity estimates (crosstalk risk, impedance discontinuities)

**Cross-Domain Validation**
- [ ] Schematic↔PCB consistency checks
- [ ] BOM↔placement verification (all parts placed?)
- [ ] Net connectivity validation

**Cost Awareness**
- [ ] Manufacturing cost estimation (board + assembly)
- [ ] Part availability checking (LCSC stock levels)
- [ ] Alternative part suggestions

### v0.8.0 - AI Agent Integration

**Focus**: Enable AI agents to interact with KiCad designs via MCP.

*(Inspired by atopile's MCP server architecture)*

**MCP Server**
- [ ] FastMCP server exposing kicad-tools functionality
- [ ] Two-tier tool design: discovery (read-only) + action (mutations)
- [ ] Session management for stateful placement refinement
- [ ] Support stdio and HTTP transports

**MCP Tools**
| Category | Tools | State |
|----------|-------|-------|
| Analysis | `analyze_board`, `get_drc_violations`, `measure_clearance` | Stateless |
| Export | `export_gerbers`, `export_bom`, `export_assembly` | Stateless |
| Placement | `placement_analyze`, `placement_suggestions` | Stateless |
| Placement Session | `start_session`, `query_move`, `apply_move`, `commit` | Stateful |
| Routing | `route_net`, `get_unrouted_nets` | Stateless |

**Layout Preservation** *(inspired by atopile)*
- [ ] Hierarchical address-based component matching (`power.ldo.package`)
- [ ] Preserve placement/routing when regenerating PCB from schematic
- [ ] Anchor-based offset calculation for subcircuit layouts
- [ ] Net remapping for name changes
- [ ] Incremental layout updates (only touch changed components)

### v0.9.0 - Typed Interfaces & Constraints

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

### v0.10.0 - IDE Integration

**Focus**: Language Server Protocol for KiCad files in VS Code.

*(Inspired by atopile's LSP implementation)*

**LSP Server**
- [ ] pygls-based server for `.kicad_sch` and `.kicad_pcb` files
- [ ] Full document sync with 2-second debounce
- [ ] In-memory graph storage for quick queries

**LSP Features**
| Feature | Description |
|---------|-------------|
| Diagnostics | Real-time DRC/ERC squiggly underlines |
| Hover | Component details, net info, pad specs |
| Go-to-Definition | Navigate net references, schematic↔PCB linking |
| Completion | Component refs, net names, library items |

**VS Code Extension**
- [ ] Extension packaging and distribution
- [ ] Syntax highlighting for S-expression files
- [ ] Custom commands for kicad-tools operations

### v1.0.0 - Production Ready

**Focus**: API stability and production deployment.

- [ ] API stability guarantees (semantic versioning)
- [ ] Comprehensive documentation
- [ ] Performance optimization for large boards (1000+ components)
- [ ] Robust error handling across all modules
- [ ] CI/CD integration examples

### Beyond v1.0 - Ecosystem

**Package Registry** *(inspired by atopile)*
- [ ] Package manifest format for circuit blocks
- [ ] DAG-based dependency resolution
- [ ] Git/registry/file dependency types
- [ ] Publishing workflow via GitHub Actions
- [ ] Community block ecosystem

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
