# Architecture Overview

This document describes the high-level architecture of kicad-tools and how its modules work together.

---

## Design Philosophy

1. **Agent-First API** - Every operation callable from code with structured I/O
2. **Rich Feedback** - Validation returns actionable information, not just pass/fail
3. **Round-Trip Fidelity** - Edits preserve existing file structure
4. **Leverage KiCad** - Use kicad-cli for complex operations; focus on agent-specific capabilities
5. **Hierarchical Abstractions** - Work with circuit blocks, not just primitives

---

## System Diagram

```
┌─────────────────────────────────────────────────────────────────────────┐
│                              CLI (kct)                                   │
│  symbols │ nets │ bom │ drc │ erc │ route │ reason │ placement │ ...   │
└────────────────────────────────┬────────────────────────────────────────┘
                                 │
┌────────────────────────────────▼────────────────────────────────────────┐
│                           Python API                                     │
│                                                                          │
│  ┌──────────────────────────────────────────────────────────────────┐   │
│  │                         Project                                   │   │
│  │   Unified interface for complete KiCad projects                   │   │
│  │   - Schematic + PCB + Libraries                                   │   │
│  │   - Cross-referencing and validation                              │   │
│  │   - Manufacturing export                                          │   │
│  └──────────────────────────────────────────────────────────────────┘   │
│                                 │                                        │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  ┌────────────┐   │
│  │  Schematic   │  │     PCB      │  │   Library    │  │   Parts    │   │
│  │              │  │              │  │              │  │            │   │
│  │ - Symbols    │  │ - Footprints │  │ - Symbols    │  │ - LCSC DB  │   │
│  │ - Nets       │  │ - Tracks     │  │ - Footprints │  │ - Search   │   │
│  │ - Sheets     │  │ - Zones      │  │ - 3D Models  │  │ - Lookup   │   │
│  │ - Wires      │  │ - Vias       │  │              │  │            │   │
│  └──────┬───────┘  └──────┬───────┘  └──────────────┘  └────────────┘   │
│         │                 │                                              │
│  ┌──────▼─────────────────▼──────────────────────────────────────────┐  │
│  │                         Query API                                  │  │
│  │   Fluent interface: sch.symbols.filter(value="100nF").smd()       │  │
│  └───────────────────────────────────────────────────────────────────┘  │
│                                                                          │
│  ┌─────────────────────────────────────────────────────────────────┐    │
│  │                      Operations Layer                            │    │
│  │  ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌───────────┐  │    │
│  │  │ Router  │ │  Optim  │ │   DRC   │ │   ERC   │ │  Export   │  │    │
│  │  │         │ │         │ │         │ │         │ │           │  │    │
│  │  │ A* path │ │Placement│ │ Pure Py │ │Electric │ │ Gerbers   │  │    │
│  │  │ Diff pr │ │ Scoring │ │ Rules   │ │ Rules   │ │ BOM/CPL   │  │    │
│  │  └─────────┘ └─────────┘ └─────────┘ └─────────┘ └───────────┘  │    │
│  └─────────────────────────────────────────────────────────────────┘    │
│                                                                          │
│  ┌─────────────────────────────────────────────────────────────────┐    │
│  │                      Reasoning Layer                             │    │
│  │   PCBReasoningAgent - LLM-driven layout decisions                │    │
│  │   CommandInterpreter - Parse agent commands                      │    │
│  │   PCBState - Board state representation for LLMs                 │    │
│  └─────────────────────────────────────────────────────────────────┘    │
│                                                                          │
└────────────────────────────────┬────────────────────────────────────────┘
                                 │
┌────────────────────────────────▼────────────────────────────────────────┐
│                           Core Layer                                     │
│                                                                          │
│  ┌────────────────────┐  ┌────────────────────┐  ┌──────────────────┐   │
│  │       SExp         │  │      Schema        │  │   Manufacturers  │   │
│  │                    │  │                    │  │                  │   │
│  │ - S-expr parser    │  │ - Data models      │  │ - JLCPCB rules   │   │
│  │ - Round-trip edit  │  │ - Type definitions │  │ - OSHPark rules  │   │
│  │ - File I/O         │  │ - Validation       │  │ - PCBWay rules   │   │
│  └────────────────────┘  └────────────────────┘  └──────────────────┘   │
│                                                                          │
└─────────────────────────────────────────────────────────────────────────┘
                                 │
                                 ▼
                    ┌────────────────────────┐
                    │     KiCad Files        │
                    │                        │
                    │ .kicad_sch  .kicad_pcb │
                    │ .kicad_sym  .kicad_mod │
                    │ .kicad_pro             │
                    └────────────────────────┘
```

---

## Module Descriptions

### Core Layer

| Module | Purpose | Key Classes |
|--------|---------|-------------|
| `sexp/` | S-expression parsing with round-trip fidelity | `SExp` |
| `core/` | File loading and saving | `load_schematic()`, `load_pcb()` |
| `schema/` | Data models for KiCad objects | `Schematic`, `PCB`, `SymbolInstance` |
| `manufacturers/` | Manufacturer-specific design rules | `JLCPCBRules`, `OSHParkRules` |

### Query Layer

| Module | Purpose | Key Classes |
|--------|---------|-------------|
| `query/` | Fluent query interface | `SymbolQuery`, `FootprintQuery` |

**Example:**
```python
# Find all 100nF capacitors in SMD packages
caps = sch.symbols.filter(value="100nF").smd()

# Find all footprints on bottom layer
bottom = pcb.footprints.filter(layer="B.Cu")
```

### Operations Layer

| Module | Purpose | Key Classes |
|--------|---------|-------------|
| `router/` | A* autorouter with diff pairs, zones | `Autorouter`, `DesignRules` |
| `optim/` | Placement optimization and scoring | `PlacementSession`, `PlacementOptimizer` |
| `drc/` | Pure Python design rule checking | `DRCChecker`, `DRCViolation` |
| `erc/` | Electrical rule checking | `ERCChecker` |
| `export/` | Manufacturing file export | `AssemblyPackage` |

### Reasoning Layer

| Module | Purpose | Key Classes |
|--------|---------|-------------|
| `reasoning/` | LLM-driven PCB layout | `PCBReasoningAgent`, `PCBState` |

The reasoning module enables LLMs to make strategic decisions while tools handle geometric execution:

```python
agent = PCBReasoningAgent.from_pcb("board.kicad_pcb")

while not agent.is_complete():
    prompt = agent.get_prompt()      # State for LLM
    command = call_llm(prompt)       # Your LLM
    result, diagnosis = agent.execute(command)

agent.save("routed.kicad_pcb")
```

### High-Level Abstractions

| Module | Purpose | Key Classes |
|--------|---------|-------------|
| `project.py` | Unified project interface | `Project` |
| `schematic/blocks/` | Reusable circuit blocks | `MCUBlock`, `LDOBlock`, `USBConnector` |

---

## Data Flow Examples

### Loading and Querying a Schematic

```
User Code                    kicad_tools                        File
    │                            │                                │
    │  Schematic.load("x.sch")   │                                │
    │ ─────────────────────────> │                                │
    │                            │     read file contents         │
    │                            │ ─────────────────────────────> │
    │                            │                                │
    │                            │ <───────────────────────────── │
    │                            │                                │
    │                            │  SExp.parse() -> AST           │
    │                            │  Schematic() -> wrap AST       │
    │                            │                                │
    │ <───────────────────────── │                                │
    │  Schematic object          │                                │
    │                            │                                │
    │  sch.symbols.filter(...)   │                                │
    │ ─────────────────────────> │                                │
    │                            │  Query AST nodes               │
    │                            │  Filter by criteria            │
    │                            │                                │
    │ <───────────────────────── │                                │
    │  SymbolList                │                                │
```

### Modifying and Saving

```
User Code                    kicad_tools                        File
    │                            │                                │
    │  symbol.value = "10k"      │                                │
    │ ─────────────────────────> │                                │
    │                            │  Modify AST node in-place      │
    │                            │  (preserves structure)         │
    │                            │                                │
    │  sch.save()                │                                │
    │ ─────────────────────────> │                                │
    │                            │  Serialize AST to S-expr       │
    │                            │ ─────────────────────────────> │
    │                            │     write file                 │
```

---

## Key Design Decisions

### 1. Round-Trip Fidelity

KiCad files contain formatting, comments, and ordering that users expect to be preserved. The S-expression parser maintains the original AST structure, only modifying nodes that change.

### 2. Query API vs Direct Access

Instead of exposing raw AST nodes, the Query API provides a fluent, chainable interface that's intuitive for both humans and LLMs:

```python
# Direct access (verbose, error-prone)
for node in doc.children:
    if node.tag == "symbol" and node.get("value") == "100nF":
        ...

# Query API (fluent, type-safe)
caps = sch.symbols.filter(value="100nF")
```

### 3. Pure Python DRC

While `kicad-cli drc` is authoritative, it requires a KiCad installation. Pure Python DRC enables:
- CI/CD integration without KiCad
- Custom rule sets per manufacturer
- Programmatic access to violations

### 4. Reasoning Layer Separation

The reasoning module separates **strategic decisions** (what to do) from **geometric execution** (how to do it). This enables LLMs to make high-level decisions while specialized algorithms handle low-level geometry.

---

## Extension Points

### Adding a New CLI Command

1. Create `src/kicad_tools/cli/mycommand.py`
2. Add to `src/kicad_tools/cli/commands.py`
3. Register in `src/kicad_tools/cli/parser.py`

### Adding a New Circuit Block

1. Create `src/kicad_tools/schematic/blocks/myblock.py`
2. Inherit from `CircuitBlock`
3. Export from `src/kicad_tools/schematic/blocks/__init__.py`

### Adding a New Manufacturer

1. Create `src/kicad_tools/manufacturers/mymfr.py`
2. Define `DesignRules` with min/max values
3. Register in `src/kicad_tools/manufacturers/__init__.py`

---

## Performance Considerations

- **Large files**: The S-expression parser loads entire files into memory. For very large files (>10MB), consider streaming approaches.
- **Autorouter**: A* complexity is O(n log n) per net. For dense boards, routing can take minutes.
- **DRC**: Pure Python DRC is slower than kicad-cli. Use kicad-cli for production; use pure Python for CI.

---

## Dependencies

| Dependency | Purpose |
|------------|---------|
| Python 3.10+ | Type hints, pattern matching |
| pydantic | Data validation |
| rich | Terminal formatting |
| httpx | LCSC API calls |
| PyMuPDF | Datasheet PDF parsing |

Optional:
- `kicad-cli` - For authoritative DRC/ERC and Gerber export
