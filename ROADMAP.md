# kicad-tools Roadmap

**Mission**: Enable AI agents to design PCBs programmatically.

kicad-tools provides the programmatic interface between AI reasoning and KiCad
file manipulation. We complement KiCad—we don't replace it.

## Design Philosophy

1. **Agent-First API** - Every operation callable from code with structured I/O
2. **Rich Feedback** - Validation returns actionable information, not just pass/fail
3. **Round-Trip Fidelity** - Edits preserve existing file structure
4. **Leverage KiCad** - Use kicad-cli for complex operations; focus on agent-specific capabilities
5. **Hierarchical Abstractions** - Work with circuit blocks, not just primitives

---

## Current State (v0.10.0)

The complete agent PCB design workflow is now supported:

| Step | Capability | Version |
|------|------------|---------|
| Parse datasheets | `datasheet` module | v0.4.0 |
| Create symbols/footprints | `library` module, generators | v0.4.0 |
| Design schematic | `Schematic`, circuit blocks | v0.5.0 |
| Place components | `PCBEditor`, placement optimization | v0.6.0 |
| Route traces | `Autorouter`, diff pairs, zones | v0.3.0 |
| Validate design | Pure Python DRC, rich feedback | v0.7.0 |
| Export for manufacturing | `AssemblyPackage` (via kicad-cli) | v0.2.0 |
| AI agent integration | MCP server, sessions | v0.8.0 |
| Design intent & feedback | `intent` module, incremental DRC | v0.9.0 |
| Pattern library & explanations | `patterns`, `explain` modules | v0.10.0 |

See [CHANGELOG.md](CHANGELOG.md) for detailed release history.

---

## Core Challenges for Agents

These are the fundamental challenges AI agents face when designing PCBs, and how
we plan to address them:

| Challenge | Description | Solution |
|-----------|-------------|----------|
| Incomplete Information | Tools don't understand design intent | ✅ Design Intent System (v0.9) |
| Combinatorial Explosion | Huge placement/routing solution space | ✅ Pattern Library (v0.10) |
| Feedback Latency | Problems discovered late in process | ✅ Continuous Validation (v0.9) |
| Knowledge Gap | Agents lack PCB design expertise | ✅ Patterns + Explanations (v0.10) |
| Communication Overhead | Verbose state, repeated context | ✅ Context Persistence (v0.9) |

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
- [x] Interface declarations with automatic constraint derivation
  ```python
  # Declare intent - system derives constraints automatically
  design.declare_interface("USB_D+", "USB_D-", type="usb2_high_speed")
  # System now enforces: 90Ω diff impedance, length matching, diff pair routing
  ```
- [x] Built-in interface types: `USB2`, `USB3`, `SPI`, `I2C`, `LVDS`, `DDR`, `Ethernet`
- [x] Intent-aware validation ("violates USB 2.0 spec" vs "clearance too small")
- [x] Intent-aware suggestions ("USB traces should be shorter for high-speed")
- [x] Custom interface definitions for project-specific needs

**Continuous Validation**
- [x] Real-time DRC during placement sessions (not just at end)
- [x] Predictive warnings based on current trajectory
  ```python
  session.apply_move("C1", x=45, y=32)
  # Response includes:
  # {"warnings": [{"type": "predictive",
  #   "message": "This position may make routing USB_D+ difficult",
  #   "suggestion": "Consider moving 2mm left"}]}
  ```
- [x] Routing difficulty estimation before routing
- [x] Incremental validation (only check affected areas)
- [x] "What-if" analysis API for evaluating changes without committing

**Intelligent Failure Recovery**
- [x] Root cause analysis for routing/placement failures
- [x] Multiple resolution strategies with trade-offs
  ```python
  result = router.route_net("CLK")
  # On failure, returns:
  # {"resolution_strategies": [
  #   {"strategy": "move_component", "target": "C3", "difficulty": "easy"},
  #   {"strategy": "add_via", "position": [44, 30], "difficulty": "medium"},
  #   {"strategy": "reroute_blocking_net", "net": "SPI_MOSI", "difficulty": "hard"}
  # ]}
  ```
- [x] Similar problem pattern matching ("this looks like bypass_cap_blocking")
- [x] Difficulty estimation for each fix option

**Context Persistence**
- [x] Persistent design context across MCP calls
- [x] Decision history with rationale tracking
- [x] Learned preferences from agent behavior
- [x] Efficient state encoding (reduce token overhead)

---

### v0.10.0 - Pattern Library & Explanations ✅ RELEASED

**Focus**: Encode expert PCB design knowledge for agent use.

Agents shouldn't solve USB layout from first principles every time. A curated
library of validated design patterns accelerates iteration and improves quality.

**Design Pattern Library**
- [x] Pattern schema with placement rules and constraints
  ```python
  from kicad_tools.patterns import USBPattern

  pattern = USBPattern(
      speed="high_speed",
      connector="type_c",
      esd_protection=True
  )
  placements = pattern.get_placements(connector_at=(10, 50))
  # Returns validated placement meeting USB spec
  ```
- [x] Core patterns:
  - Power: LDO, buck converter, battery charging, reverse polarity
  - Interfaces: USB 2.0/3.0, SPI, I2C, UART, Ethernet, HDMI
  - MCU: Bypass caps, crystal, debug header, reset circuit
  - Analog: ADC filtering, op-amp configs, sensor interfaces
  - Protection: ESD, TVS, overcurrent, thermal shutdown
- [x] Pattern validation (verify instantiated pattern meets specs)
- [x] Pattern adaptation (customize for specific components)
- [x] User-defined patterns with validation rules

**Explanation System**
- [x] Queryable explanations for DRC rules
  ```python
  explain("trace_length", net="USB_D+")
  # Returns: {"explanation": "USB 2.0 high-speed signals require...",
  #           "spec_reference": "USB 2.0 section 7.1.5",
  #           "current_value": 45.2, "target_range": [40, 50]}
  ```
- [x] Design decision rationale tracking
- [x] Spec references for all constraints
- [x] Common mistake detection with explanations
- [x] Learning resources for unfamiliar patterns

**Multi-Resolution Abstraction**
- [x] High-level operations for common tasks
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
- [x] Automatic decomposition of high-level commands
- [x] Consistent results across abstraction levels

---

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

---

### v1.0.0 - Production Ready

**Focus**: API stability, performance, and production deployment.

- [ ] API stability guarantees (semantic versioning)
- [ ] Comprehensive documentation with examples
- [ ] Performance optimization for large boards (1000+ components)
- [ ] Robust error handling across all modules
- [ ] CI/CD integration examples
- [ ] Benchmark suite for regression testing

---

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
