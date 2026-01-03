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

---

## Planned Versions

### v0.6.0 - Intelligent Placement

**Focus**: Help agents make better initial component placement decisions.

Placement is where agents struggle most—random placement leads to unroutable
boards. This release adds intelligence to the placement step.

**Placement Engine**
- [ ] Functional clustering (group related components: MCU+bypass caps, USB+ESD)
- [ ] Thermal-aware placement (power components near edges, heat spreading)
- [ ] Signal integrity hints (keep high-speed traces short, minimize stubs)
- [ ] Edge placement for connectors and interfaces

**Placement Constraints**
- [ ] Keep-out zones (mechanical, thermal, RF)
- [ ] Component grouping rules (define which components belong together)
- [ ] Alignment constraints (grid snap, row/column alignment)

**Agent Integration**
- [ ] Placement suggestions with rationale (explainable to LLMs)
- [ ] Iterative refinement API (agent can query "what if I move X here?")

### v0.7.0 - Design Feedback & Iteration

**Focus**: Help agents understand failures and improve designs.

When DRC fails or routing is congested, agents need actionable guidance—not
just error codes. This release makes the feedback loop agent-friendly.

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

### v1.0.0 - Production Ready

**Focus**: API stability and production deployment.

- [ ] API stability guarantees (semantic versioning)
- [ ] Comprehensive documentation
- [ ] Performance optimization for large boards (1000+ components)
- [ ] Robust error handling across all modules
- [ ] CI/CD integration examples

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
