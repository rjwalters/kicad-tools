# Atopile Research Synthesis: Best Ideas for kicad-tools

**Date**: 2026-01-03
**Source**: 7 research documents analyzing atopile architecture

## Executive Summary

This document synthesizes research from 7 atopile analysis documents, identifying the most impactful improvements for kicad-tools. Ideas are ranked by **implementation effort** vs **user value**.

---

## Tier 1: High Value, Moderate Effort

### 1. MCP Server for AI Integration (Issue #302)

**The Idea**: Create an MCP (Model Context Protocol) server to expose kicad-tools functionality to AI agents.

**Why It's Valuable**:
- Enables Claude/GPT to interact with KiCad designs programmatically
- Two-tier tool design: discovery (read-only) + action (mutations)
- Existing `PlacementSession` already has query/apply patterns perfect for MCP

**Key Implementation Details**:
```
src/kicad_tools/mcp/
├── server.py           # FastMCP server entry point
├── session_manager.py  # Manage PlacementSession instances
└── tools/
    ├── analysis.py     # Board analysis, DRC
    ├── export.py       # Gerbers, BOM, assembly
    ├── placement.py    # Placement optimization
    └── routing.py      # Net routing
```

**Recommended Tools**:
| Category | Tools | State |
|----------|-------|-------|
| Analysis | `analyze_board`, `get_drc_violations` | Stateless |
| Placement | `start_session`, `query_move`, `apply_move` | Stateful |
| Export | `export_gerbers`, `export_bom` | Stateless |

**Effort**: Medium (wrap existing functionality with FastMCP)

---

### 2. Layout Preservation During PCB Regeneration (Issue #305)

**The Idea**: Use hierarchical address-based matching to preserve placement/routing when schematics change.

**Why It's Valuable**:
- Currently, regenerating PCB loses all layout work
- Atopile uses `atopile_address` property (semantic path like `power.ldo.package`)
- Enables iterative design: modify schematic, keep existing layout

**Key Patterns**:
1. **Hierarchical addressing**: Match components by module path, not designator
2. **Anchor-based offset**: Use largest footprint as reference for group positioning
3. **Net remapping**: Build correspondence by matching pads on matched footprints

**Implementation Approach**:
```typescript
interface FootprintAddress {
  path: string;           // e.g., "power.ldo.package"
  subaddresses?: string[]; // links to source layouts
}

function incrementalLayoutUpdate(original: PCB, updated: PCB): PCBDiff {
  const added = findNewComponents(original, updated);
  const removed = findRemovedComponents(original, updated);
  const unchanged = findUnchangedComponents(original, updated); // Preserve exactly
  return { added, removed, unchanged };
}
```

**Effort**: Medium-High (requires custom footprint properties)

---

### 3. Rich Error Diagnostics (Issue #308)

**The Idea**: Source-attached exceptions with Rich terminal rendering.

**Why It's Valuable**:
- Current DRC errors are plain text with limited context
- Atopile errors show syntax-highlighted code snippets with line numbers
- Error accumulation reports ALL violations, not just first

**Key Patterns**:

1. **Source-attached diagnostics**:
```python
class KiCadDiagnostic(Exception):
    def __init__(self, message: str, file_path: Path,
                 element_type: str,  # "footprint", "track", "via"
                 element_ref: str,   # "C1", "R2"
                 position: tuple[float, float],
                 layer: str | None = None): ...
```

2. **Error accumulation**:
```python
with accumulate(DRCError) as accumulator:
    for rule in drc_rules:
        with accumulator.collect():
            check_rule(rule, board)
# All violations reported together
```

3. **Rich console rendering**:
```python
def __rich_console__(self, console, options):
    return [
        Text("DRC Error: ", style="bold red") + Text(self.title),
        Panel(self._render_board_snippet(), title="Location"),
        Text("Suggestion: ") + Text(self.suggestion),
    ]
```

**Effort**: Low-Medium (integrate Rich library, refactor error classes)

---

## Tier 2: High Value, Higher Effort

### 4. Typed Interface System for Circuit Blocks (Issue #304)

**The Idea**: Add typed interfaces (PowerInterface, I2CInterface, USBInterface) to circuit blocks for type-checked connections.

**Why It's Valuable**:
- Current blocks use untyped `ports: dict[str, tuple[float, float]]`
- No validation that I2C connects to I2C, not SPI
- Atopile catches connection errors at design time

**Proposed Interface Hierarchy**:
```python
class Interface(Protocol):
    @property
    def interface_type(self) -> str: ...

class PowerInterface(Interface):
    vcc: Port
    gnd: Port
    voltage: float | None = None
    max_current: float | None = None

class I2CInterface(Interface):
    sda: Port
    scl: Port
    frequency: int = 100_000
```

**Benefits**:
- Catch misconnections at design time
- Self-documenting block interfaces
- Enable future auto-wiring features

**Effort**: Medium-High (refactor all existing blocks)

---

### 5. Constraint-Based Part Selection (Issue #303)

**The Idea**: Symbolic constraint solver for automatic component selection from LCSC.

**Why It's Valuable**:
- Currently: `LDOBlock(sch, ref="U1", value="AMS1117-3.3")`
- Proposed: `LDOBlock(sch, input_voltage=Interval(4.5, 5.5), output_voltage=3.3)`
- System queries LCSC, selects parts meeting constraints

**Key Components**:
1. **Interval arithmetic** for tolerance propagation
2. **Constraint solver** for equation systems
3. **Part picker** querying LCSC API

**Phased Implementation**:
1. Phase 1: Simple interval types for parameters
2. Phase 2: Parameter constraints with equations
3. Phase 3: LCSC API integration
4. Phase 4: Auto footprint/symbol assignment

**Effort**: High (significant new architecture)

---

### 6. LSP Server for IDE Integration (Issue #310)

**The Idea**: Language Server Protocol implementation for KiCad files in VS Code.

**Why It's Valuable**:
- Real-time DRC/ERC feedback as you edit
- Hover info for components/nets
- Go-to-definition for net references
- Code completion for component refs

**Priority Features**:
1. **Diagnostics**: Map DRC/ERC checks to squiggly underlines
2. **Hover**: Component details, net info
3. **Go-to-definition**: Navigate between schematic/PCB
4. **Completion**: Suggest component refs, net names

**Implementation Approach** (from atopile):
- Use pygls for Python LSP server
- Debounce changes (2s delay)
- Full document sync initially
- Store parsed graphs in memory

**Effort**: High (requires KiCad file parser integration)

---

## Tier 3: Future Considerations

### 7. Package Registry for Circuit Blocks (Issue #309)

**The Idea**: npm-style package ecosystem for sharing circuit blocks.

**Why It's Valuable**:
- Reuse community blocks (USB-C, power supplies, etc.)
- Dependency management with semver
- Could leverage existing package infrastructure

**Key Features**:
- Package manifest (`kicad-tools-block.yaml`)
- DAG-based dependency resolution
- Git/registry/file dependency types
- Publishing workflow via GitHub Actions

**Effort**: Very High (infrastructure heavy)

---

## Prioritized Roadmap

### Phase 1: Quick Wins (1-2 weeks each)
1. **Rich Error Diagnostics** - Low effort, immediate UX improvement
2. **MCP Server Skeleton** - Wrap existing tools with FastMCP

### Phase 2: Core Improvements (1-2 months)
3. **Layout Preservation** - Essential for iterative workflows
4. **Typed Interfaces** - Foundation for better block system

### Phase 3: Advanced Features (3+ months)
5. **Constraint-Based Part Selection** - Game-changing UX
6. **LSP Server** - Professional IDE experience
7. **Package Registry** - Community ecosystem

---

## Key Takeaways

| Atopile Pattern | kicad-tools Application |
|-----------------|-------------------------|
| FastMCP server | AI agent integration |
| Hierarchical addresses | Layout preservation |
| Rich console rendering | Better error messages |
| Typed interfaces | Connection validation |
| Interval arithmetic | Tolerance/constraint handling |
| pygls LSP | IDE integration |
| Package registry | Community ecosystem |

The most impactful near-term improvements are:
1. **MCP server** - Enables AI-assisted PCB design
2. **Rich diagnostics** - Better DX with minimal effort
3. **Layout preservation** - Critical for iterative design

---

## References

| Issue | Topic | Source File |
|-------|-------|-------------|
| #302 | MCP Server | `issue-302/docs/research/atopile-mcp-analysis.md` |
| #303 | Constraint Solving | `issue-303/docs/research/atopile-constraint-solving.md` |
| #304 | Interface Patterns | `issue-304/docs/research/atopile-interface-patterns.md` |
| #305 | Layout Reuse | `issue-305/docs/research/atopile-layout-reuse.md` |
| #308 | Error Handling | `issue-308/docs/research/atopile-error-handling.md` |
| #309 | Package Registry | `issue-309/docs/research/atopile-package-registry.md` |
| #310 | LSP Server | `issue-310/docs/research/atopile-lsp-analysis.md` |
