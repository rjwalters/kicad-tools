# Research: Atopile Build System & DAG Targets

## Summary

This document analyzes atopile's build system implementation, focusing on its DAG-based target management, partial build capabilities, and progress reporting. These patterns could inform future improvements to kicad-tools workflow automation.

## Source Files Analyzed

- `vendor/atopile/src/atopile/build_steps.py` - Core build system with Muster target registry
- `vendor/atopile/src/atopile/build.py` - Build initialization
- `vendor/atopile/src/atopile/buildutil.py` - Build execution entry point
- `vendor/atopile/src/faebryk/libs/util.py` - DAG implementation
- `vendor/atopile/src/atopile/cli/logging_.py` - Progress reporting with LoggingStage

## Key Findings

### 1. DAG-Based Build Targets (Muster System)

Atopile uses a "Muster" pattern for managing build targets as a directed acyclic graph:

```python
@dataclass
class MusterTarget:
    name: str
    aliases: list[str]
    func: MusterFuncType
    description: str | None = None
    implicit: bool = True          # Hidden from user, auto-included as dependency
    virtual: bool = False          # Grouping target with no actual work
    dependencies: list["MusterTarget"] = field(default_factory=list)
    tags: set[Tags] = field(default_factory=set)
    produces_artifact: bool = False
    success: bool | None = None    # Tracks execution status
```

**Key Design Decisions:**
- Targets are registered using a decorator pattern for clean syntax
- Dependencies reference other `MusterTarget` objects directly
- `virtual` targets act as aggregation points (e.g., "build-design", "all")
- `implicit` flag distinguishes auto-included dependencies from explicit user requests
- `produces_artifact` marks targets that generate output files

### 2. Partial Build Handling

The system handles partial builds through `muster.select()`:

```python
def select(self, selected_targets: set[str]) -> Generator[MusterTarget, None, None]:
    # 1. Get subgraph containing selected targets and ALL their dependencies
    subgraph = self.dependency_dag.get_subgraph(
        selector_func=lambda name: name in selected_targets
        or any(alias in selected_targets for alias in self.targets[name].aliases)
    )

    # 2. Topologically sort to ensure correct execution order
    sorted_names = subgraph.topologically_sorted()

    # 3. Only yield targets whose dependencies have ALL succeeded
    for target in targets:
        if all(dep.succeeded for dep in target.dependencies or []):
            yield target
```

**How Partial Builds Work:**
1. User requests specific targets (e.g., `{"gerber", "bom"}`)
2. System computes minimal subgraph including all transitive dependencies
3. Returns targets in topological order (dependencies first)
4. Skips targets whose dependencies failed (cascading failure)

**DAG Implementation Highlights:**
- `get_subgraph()` finds all parent nodes of selected targets
- `topologically_sorted()` uses Kahn's algorithm with cycle detection
- Supports disconnected components (multiple independent build chains)

### 3. Progress Reporting

Atopile uses `LoggingStage` for rich terminal progress:

```python
class LoggingStage(Advancable):
    def __init__(self, name: str, description: str, steps: int | None = None):
        self._progress = IndentedProgress(
            CompletableSpinnerColumn(),    # ✓/✗/⚠ completion indicators
            TextColumn("{task.description}"),
            StyledMofNCompleteColumn(),    # N/M progress
            ShortTimeElapsedColumn(),      # [1.2s]
            ...
        )
```

**Features:**
- Spinner → checkmark/X on completion
- Warning/error count tracking per stage
- Time elapsed display
- Log capture to files (debug/info/warning/error levels)
- Context manager pattern for clean enter/exit

**Usage in Build System:**
```python
with LoggingStage(
    self.name,
    self.description or f"Building [green]'{self.name}'[/green]",
) as log_context:
    self.func(app, solver, pcb, log_context)
```

### 4. Caching Strategy

**Finding: No build-level caching implemented**

The codebase shows:
- `@once` decorator used for memoization of expensive single-call operations
- Parser has `# TODO: caching` comment indicating future plans
- LSP server has `# TODO: caching` for document building
- No content-addressed or file-hash based caching for build targets

**Implication:** Each build runs all required targets from scratch. Incremental builds would be a significant future enhancement.

### 5. Build Target Registry

Current atopile targets (in dependency order):

```
prepare-build
├── post-design-checks
│   └── load-pcb
│       └── picker (part selection)
│           └── prepare-nets
│               └── post-solve-checks
│                   └── update-pcb
│                       ├── build-design (virtual)
│                       │   ├── bom
│                       │   ├── netlist
│                       │   ├── manifest
│                       │   ├── variable-report
│                       │   └── i2c-tree
│                       └── post-pcb-checks
│                           └── mfg-data (gerber, pnp, etc.)
```

**Grouping Targets:**
- `default` - Standard build outputs (bom, netlist, manifest, etc.)
- `all` - Everything including manufacturing data
- `3d-models` - GLB and STEP exports

## Recommendations for kicad-tools

### Applicable Patterns

1. **Target Registry Pattern**
   ```python
   # Define targets with decorator
   @workflow.register("validate", dependencies=[parse_target])
   def validate_design(ctx): ...
   ```

2. **DAG-Based Execution**
   - Use topological sort for dependency resolution
   - Support partial builds (only run what's needed)
   - Track success/failure for cascading skips

3. **Progress Reporting**
   - Rich terminal UI with spinners
   - Per-stage timing and error counts
   - Log file generation for debugging

### Potential kicad-tools Workflow Targets

```
parse-board
├── validate-design
│   └── run-drc
│       └── auto-route (freerouting)
│           └── export-gerber
│           └── export-bom
│           └── export-3d
```

### Implementation Considerations

**Do Not Adopt Yet:**
- No caching means full rebuilds - okay for prototyping
- Complex initialization (faebryk module system) not needed for kicad-tools

**Worth Adopting:**
- `MusterTarget` dataclass pattern for metadata
- `DAG` class for dependency management
- `LoggingStage` pattern for progress UI
- Virtual targets for grouping

## Questions Answered

1. **How do they handle partial builds?**
   - `get_subgraph()` extracts only needed targets + dependencies
   - Topological sort ensures correct order
   - Failed dependencies cascade to skip dependents

2. **What's the caching strategy?**
   - Currently none for build targets
   - `@once` decorator for one-time computations
   - Future improvement opportunity

3. **How do they report progress?**
   - `LoggingStage` context manager per target
   - Rich progress bars with spinners
   - Multi-level log files (debug/info/warn/error)
   - Success/failure/warning status indicators

4. **Can we adopt for kicad-tools?**
   - Yes, the core patterns (DAG, Muster, LoggingStage) are well-designed
   - Would need to create our own target definitions
   - Start simple: parse → validate → export workflow
