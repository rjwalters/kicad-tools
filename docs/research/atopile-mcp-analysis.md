# Research: Atopile MCP Server Analysis

This document analyzes atopile's Model Context Protocol (MCP) server implementation to identify patterns and ideas that could improve kicad-tools AI integration.

## Source Material

The atopile MCP server is located in `vendor/atopile/src/atopile/mcp/` and consists of:

- `mcp_server.py` - Server entry point and configuration
- `util.py` - Shared utilities, types, and tool registration
- `tools/` - Domain-specific tool implementations:
  - `library.py` - Standard library inspection
  - `packages.py` - Community package search/install
  - `project.py` - Project location helpers
  - `cli.py` - CLI command wrappers (build, install, verify)

## Key Findings

### 1. Server Architecture

Atopile uses the **FastMCP** library from the `mcp` package:

```python
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("atopile", stateless_http=True)
mcp.run(transport="streamable-http" if http else "stdio")
```

**Design choices:**

| Choice | Atopile's Approach | Rationale |
|--------|-------------------|-----------|
| Transport | Stdio (default) or HTTP | Flexible deployment |
| State | Stateless HTTP mode | Simpler scaling, no session management |
| Registration | Decorator-based | Clean, Pythonic API |

### 2. Tool Registration Pattern

Atopile uses a custom `MCPTools` class that enables modular tool registration:

```python
class MCPTools:
    def __init__(self):
        self._tools: dict[Callable, MCP_DECORATOR] = {}

    def register(self, decorator: MCP_DECORATOR = lambda mcp: mcp.tool()):
        def decorator_wrapper(func: Callable):
            self._tools[func] = decorator
            return func
        return decorator_wrapper

    def install(self, mcp: FastMCP):
        for func, decorator in self._tools.items():
            d = decorator(mcp)
            d(func)
```

**Usage pattern:**

```python
library_tools = MCPTools()

@library_tools.register()
def inspect_library_module_or_interface(name: str) -> NodeInfo:
    """Inspect a standard library module..."""
    return _get_library_node(name)
```

**Benefits:**
- Tools are self-contained in domain modules
- Easy to add/remove tool categories
- Clear separation of concerns

### 3. Tool Categories and Granularity

| Category | Tools | Granularity |
|----------|-------|-------------|
| Library | `inspect_library_module_or_interface()`, `get_library_modules_or_interfaces()` | Fine-grained inspection |
| Packages | `inspect_package()`, `find_packages()` | Search + detailed lookup |
| Project | `find_project_from_filepath()` | Helper utilities |
| CLI | `build_project()`, `search_and_install_jlcpcb_part()`, `install_package()`, `verify_package()` | Coarse operations |

**Pattern:** Tools follow a "list then inspect" or "search then detail" pattern - enabling efficient AI discovery workflows.

### 4. Response Types

Atopile uses **Pydantic BaseModel** for structured responses:

```python
class NodeInfo(BaseModel):
    name: str
    docstring: str
    locator: str
    language: Language
    code: str

class BuildResult(Result):
    target: str
    logs: str
```

**Key observations:**
- All responses are typed dataclasses/models
- Consistent base class for operation results (`Result`)
- Error variants with descriptive fields (`ErrorResult`)

## Answers to Issue Questions

### Q1: How does their MCP server handle state vs stateless operations?

**Answer:** Atopile runs in **stateless HTTP mode** by default:

```python
mcp = FastMCP("atopile", stateless_http=True)
```

This means:
- Each request is independent
- No session/context persists between calls
- The client (AI agent) is responsible for maintaining workflow state
- Project context is passed via absolute paths in each request

**Implication for kicad-tools:** Our `PlacementSession` already provides session-based state, which is valuable for iterative refinement. An MCP server could offer both:
- Stateless tools for one-shot operations (export, DRC, analysis)
- Stateful session tools for iterative workflows (placement refinement)

### Q2: What's the best granularity for exposing kicad-tools functionality?

**Answer:** Based on atopile's pattern, recommend a **two-tier approach**:

**Tier 1: Discovery/Query Tools** (fine-grained, read-only)
- `list_footprints()` - Browse available footprints
- `inspect_component(ref)` - Get component details
- `list_nets()` - List all nets
- `get_net_info(net)` - Get net details
- `analyze_board()` - Get board summary

**Tier 2: Action Tools** (coarse-grained, side effects)
- `optimize_placement(options)` - Run full placement optimization
- `route_net(net, options)` - Route a specific net
- `run_drc(rules)` - Run design rule check
- `export_gerbers(format, output)` - Export manufacturing files

This matches atopile's "browse library → install package → build project" pattern.

### Q3: Should we expose placement optimization as MCP tools?

**Answer:** Yes, but with careful consideration:

**Recommended MCP tools for placement:**

| Tool | Type | Purpose |
|------|------|---------|
| `placement_analyze` | Query | Get current placement score and issues |
| `placement_query_move` | Query | Evaluate hypothetical move (what-if) |
| `placement_apply_move` | Action | Apply a move |
| `placement_optimize` | Action | Run full optimization |
| `placement_suggestions` | Query | Get AI-friendly placement suggestions |

**Session management options:**

1. **Stateless:** Each call works on file path, changes saved immediately
2. **Session-based:** Start session → query/apply moves → commit/rollback
3. **Hybrid:** Stateless for analysis, session for refinement

Recommendation: **Hybrid approach** - expose both patterns.

### Q4: How can agents query/modify placement interactively via MCP?

**Answer:** Build on our existing `PlacementSession`:

```python
# Proposed MCP tools wrapping PlacementSession

@placement_tools.register()
def start_placement_session(pcb_path: str) -> SessionInfo:
    """Start interactive placement session. Returns session_id."""
    ...

@placement_tools.register()
def query_placement_move(
    session_id: str,
    ref: str,
    x: float,
    y: float,
    rotation: float | None = None
) -> MoveResult:
    """Query impact of moving component without applying."""
    ...

@placement_tools.register()
def apply_placement_move(
    session_id: str,
    ref: str,
    x: float,
    y: float,
    rotation: float | None = None
) -> MoveResult:
    """Apply move to session (can be undone)."""
    ...

@placement_tools.register()
def commit_placement_session(session_id: str) -> CommitResult:
    """Commit all pending changes to PCB file."""
    ...
```

The existing `PlacementSession.query_move()` and `apply_move()` methods already return structured `MoveResult` objects with score deltas and routing impact - perfect for AI decision-making.

## Recommendations for kicad-tools

### Immediate Opportunities

1. **Create MCP server using FastMCP**
   - Wrap existing functionality with minimal new code
   - Use Pydantic models for responses (we already have dataclasses)
   - Support both stdio and HTTP transports

2. **Expose existing agent-integration tools via MCP**
   - Our `examples/agent-integration/claude/tools.py` defines 30+ tools
   - Convert these to MCP tool registrations
   - Provides immediate value without new functionality

3. **Add session-based placement tools**
   - `PlacementSession` already has the API
   - Need session management (dict of active sessions)
   - Add timeout/cleanup for abandoned sessions

### Architecture Proposal

```
src/kicad_tools/mcp/
├── __init__.py
├── server.py           # FastMCP server entry point
├── session_manager.py  # Manage PlacementSession instances
└── tools/
    ├── __init__.py
    ├── analysis.py     # Board analysis, DRC
    ├── export.py       # Gerbers, BOM, assembly
    ├── placement.py    # Placement optimization and refinement
    ├── routing.py      # Net routing
    └── schematic.py    # Schematic operations
```

### Tool Categories for kicad-tools MCP

| Category | Tools | State |
|----------|-------|-------|
| **Analysis** | `analyze_board`, `get_drc_violations`, `measure_clearance` | Stateless |
| **Export** | `export_gerbers`, `export_bom`, `export_assembly` | Stateless |
| **Placement** | `placement_analyze`, `placement_suggestions` | Stateless |
| **Placement Session** | `start_session`, `query_move`, `apply_move`, `commit`, `rollback` | Stateful |
| **Routing** | `route_net`, `route_all`, `get_unrouted_nets` | Stateless/Stateful |
| **Schematic** | `list_symbols`, `list_nets`, `add_symbol`, `wire_components` | Stateful |

## Comparison: Current vs MCP Approach

| Aspect | Current (Tool Definitions) | MCP Approach |
|--------|---------------------------|--------------|
| Transport | In-process only | Stdio, HTTP, WebSocket |
| Client | Claude/OpenAI specific | Any MCP-compatible client |
| Discovery | Static tool list | Dynamic tool enumeration |
| State | Manual session management | Built-in session support |
| Deployment | Embedded in app | Standalone server |

## Next Steps

1. **Issue:** Create MCP server skeleton with FastMCP
2. **Issue:** Add analysis tools (board analysis, DRC)
3. **Issue:** Add export tools (Gerbers, BOM)
4. **Issue:** Add stateful placement refinement tools
5. **Issue:** Add routing tools

## References

- **MCP Specification:** https://modelcontextprotocol.io/
- **FastMCP Library:** Part of the `mcp` Python package
- **Atopile Source:** `vendor/atopile/src/atopile/mcp/`
- **Existing Agent Integration:** `examples/agent-integration/`
- **PlacementSession:** `src/kicad_tools/optim/session.py`
