# Atopile LSP Server Analysis for IDE Integration

**Issue**: #310
**Date**: 2026-01-03
**Status**: Research Complete

## Executive Summary

This document analyzes atopile's LSP (Language Server Protocol) implementation to understand its architecture, features, and applicability for providing IDE integration to kicad-tools. The atopile LSP implementation is a mature, well-structured server that provides real-time diagnostics, code completion, hover information, and go-to-definition for `.ato` files.

## Source Files Analyzed

- `vendor/atopile/src/atopile/lsp/lsp_server.py` - Main LSP server (1387 lines)
- `vendor/atopile/src/atopile/lsp/lsp_runner.py` - Server lifecycle management
- `vendor/atopile/src/atopile/lsp/lsp_jsonrpc.py` - JSON-RPC communication
- `vendor/atopile/src/atopile/lsp/lsp_utils.py` - Utility functions
- `vendor/atopile/src/vscode-atopile/` - VS Code extension

---

## Question 1: What LSP Features Does Atopile Implement?

### 1.1 Diagnostics (Real-Time Validation)

Atopile implements comprehensive diagnostic support:

| Event | Handler | Behavior |
|-------|---------|----------|
| `TEXT_DOCUMENT_DID_OPEN` | `on_document_did_open` | Build document, publish diagnostics |
| `TEXT_DOCUMENT_DID_CHANGE` | `on_document_did_change` | Debounced (2s), rebuild and publish |
| `TEXT_DOCUMENT_DID_SAVE` | `on_document_did_save` | Immediate rebuild and publish |
| `TEXT_DOCUMENT_DIAGNOSTIC` | `on_document_diagnostic` | On-demand diagnostics |

**Diagnostic Conversion** (`lsp_server.py:160-196`):
```python
def _convert_exc_to_diagnostic(
    exc: UserException, severity: lsp.DiagnosticSeverity = lsp.DiagnosticSeverity.Error
) -> tuple[Path | None, lsp.Diagnostic]:
    # Extracts source location from exception tokens
    # Converts 1-indexed (ANTLR) to 0-indexed (LSP)
    # Returns Diagnostic with range, message, severity, code
```

**Severity Levels**:
- `Error` - Parse errors, undefined references, type mismatches
- `Warning` - Downgraded exceptions (collected via `DowngradedExceptionCollector`)

### 1.2 Hover Information

Handler: `TEXT_DOCUMENT_HOVER` (`lsp_server.py:382-393`)

```python
@LSP_SERVER.feature(lsp.TEXT_DOCUMENT_HOVER)
def on_document_hover(params: lsp.HoverParams) -> lsp.Hover | None:
    for root in GRAPHS.get(params.text_document.uri, {}).values():
        for _, trait in root.iter_children_with_trait(front_end.from_dsl):
            if (span := trait.query_references(**_query_params(params))) is not None:
                return lsp.Hover(
                    contents=lsp.MarkupContent(
                        kind=lsp.MarkupKind.Markdown, value=trait.hover_text
                    ),
                    range=_span_to_lsp_range(span),
                )
```

**Features**:
- Returns Markdown-formatted hover content
- Uses AST traits for context-aware information
- Provides range highlighting for hovered element

### 1.3 Go-to-Definition

Handler: `TEXT_DOCUMENT_DEFINITION` (`lsp_server.py:431-495`)

**Two types of navigation**:

1. **Type References** (e.g., `new MyModule`):
   - Finds the type definition in the AST graph
   - Returns `LocationLink` with target file and position

2. **Field References** (e.g., `module.field.subfield`):
   - Parses dotted path from cursor position
   - Resolves node via `bob.resolve_node_field()`
   - Returns link to field definition

### 1.4 Code Completion

Handler: `TEXT_DOCUMENT_COMPLETION` (`lsp_server.py:967-1004`)

**Trigger Characters**: `.` (dot) and ` ` (space)

| Context | Handler | Completions |
|---------|---------|-------------|
| After `.` | `_handle_dot_completion` | Child nodes (modules, interfaces, parameters) |
| After `new ` | `_handle_new_keyword_completion` | Available Module and ModuleInterface types |
| After `import ` | `_handle_stdlib_import_keyword_completion` | Standard library types |
| After `from ` | `_handle_from_keyword_completion` | Importable `.ato` and `.py` files |
| After `from "..." import ` | `_handle_from_import_keyword_completion` | Exports from specified module |

**Completion Item Types** (`lsp_server.py:636-660`):
- `Field` - Module instances
- `Interface` - ModuleInterface instances
- `Unit` - Parameter values (with units)

---

## Question 2: How Does Atopile Provide Real-Time Validation Feedback?

### 2.1 Document Building Pipeline

```
File Change → Debounce (2s) → Build Document → Collect Errors → Publish Diagnostics
```

**Build Process** (`lsp_server.py:237-289`):
```python
def _build_document(uri: str, text: str) -> None:
    # 1. Initialize atopile config for the file's project
    init_atopile_config(file_path.parent)

    # 2. Index the text to find all definitions
    context = front_end.bob.index_text(text, file_path)

    # 3. For each reference, build the corresponding node
    for ref, ctx in context.refs.items():
        match ctx:
            case ap.AtoParser.BlockdefContext():
                # Build single node first (for partial builds)
                GRAPHS[uri][TypeRef.from_one("__node_" + str(ref))] = (
                    front_end.bob.build_node(text, file_path, ref)
                )
                # Then build full module
                GRAPHS[uri][ref] = front_end.bob.build_text(text, file_path, ref)
```

### 2.2 Error Collection

Uses `DowngradedExceptionCollector` for collecting multiple errors without stopping:

```python
with DowngradedExceptionCollector(UserException) as collector:
    try:
        front_end.bob.try_build_all_from_text(source_text, file_path)
    except* UserException as e:
        exc_diagnostics = [
            _convert_exc_to_diagnostic(error) for error in iter_leaf_exceptions(e)
        ]

    warning_diagnostics = [
        _convert_exc_to_diagnostic(error, severity=lsp.DiagnosticSeverity.Warning)
        for error, severity in collector
        if severity == logging.WARNING
    ]
```

### 2.3 Debouncing Strategy

```python
@debounce(2)  # 2 second delay
def _handle_document_did_change(params: lsp.DidChangeTextDocumentParams) -> None:
    _build_document(...)
    LSP_SERVER.text_document_publish_diagnostics(...)
```

**Rationale**: Prevents excessive rebuilds during rapid typing while maintaining responsive feedback.

---

## Question 3: What's the Architecture for Handling File Changes?

### 3.1 Document Sync Mode

```python
LSP_SERVER = LanguageServer(
    ...
    text_document_sync_kind=lsp.TextDocumentSyncKind.Full,  # Full document sync
)
```

**Note**: Atopile uses full document sync (not incremental). Comment in code: "we don't have incremental parsing yet".

### 3.2 In-Memory Graph Storage

```python
GRAPHS: dict[str, dict[TypeRef, Node]] = {}
```

- Key: Document URI
- Value: Dict mapping type references to parsed AST nodes
- Separate entries for:
  - `__node_{name}` - Single node builds
  - `__import__{name}` - Import placeholders
  - Actual type references - Full module builds

### 3.3 File Change Flow

```
┌─────────────────────────────────────────────────────────────────┐
│                      FILE CHANGE EVENTS                         │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  didOpen ──────────────────────────────────────────────────┐   │
│                                                             │   │
│  didChange ──► debounce(2s) ───────────────────────────────┤   │
│                                                             ▼   │
│  didSave ──────────────────────────► _build_document() ────┤   │
│                                              │              │   │
│                                              ▼              │   │
│                                      Update GRAPHS{}        │   │
│                                              │              │   │
│                                              ▼              │   │
│                                      _get_diagnostics()     │   │
│                                              │              │   │
│                                              ▼              │   │
│                                      publish_diagnostics()  │   │
│                                                             │   │
└─────────────────────────────────────────────────────────────────┘
```

### 3.4 Incomplete Document Handling

For code completion during typing (`lsp_server.py:673-689`):

```python
def _build_incomplete_document(uri: str, text: str, hint_current_line: int | None) -> None:
    """
    Create a temporary version of the document with the incomplete line removed
    This allows us to build the document even when the user is typing
    an incomplete expression
    """
    if hint_current_line is not None:
        lines = text.split("\n")
        lines[hint_current_line] = ""  # Remove incomplete line
        text = "\n".join(lines)

    _build_document(uri, text)
```

---

## Question 4: Can We Provide Similar IDE Integration for KiCad Files?

### 4.1 Feasibility Assessment

| Aspect | Atopile | KiCad S-Expression | Feasibility |
|--------|---------|-------------------|-------------|
| **File Format** | Custom DSL (.ato) | S-expression (.kicad_sch, .kicad_pcb) | High |
| **Parser** | ANTLR-based | Custom/Kikit | High |
| **AST Structure** | Node-based graph | Hierarchical tree | High |
| **Error Sources** | Syntax, types, references | DRC, ERC, connectivity | High |
| **Hover Data** | Component info | Component/net info | High |
| **Go-to-Definition** | Type/field refs | Component/net refs | Medium |
| **Completion** | Types, imports, fields | Component refs, nets | Medium |

### 4.2 Recommended LSP Features for KiCad

#### Priority 1: Diagnostics
- **DRC Violations** - Map existing DRC checks to diagnostics with file locations
- **ERC Warnings** - Show electrical rule check issues inline
- **Symbol/Footprint Validation** - Validate references exist

#### Priority 2: Hover Information
- **Component Details** - Value, footprint, description
- **Net Information** - Connected pins, net class
- **Pad Details** - Size, shape, drill info

#### Priority 3: Go-to-Definition
- **Net Navigation** - Jump between connected elements
- **Component Navigation** - Schematic ↔ PCB linking
- **Hierarchical Sheet Navigation**

#### Priority 4: Code Completion
- **Component References** - Suggest existing component refs
- **Net Names** - Complete from existing nets
- **Footprint/Symbol Names** - From libraries

### 4.3 Implementation Approach

#### Option A: Python LSP (Like Atopile)

**Pros**:
- Direct reuse of patterns from atopile
- Integration with existing Python kicad-tools
- Pygls is mature and well-documented

**Cons**:
- Python startup overhead
- Requires Python environment management

**Stack**:
```
Python → pygls → kicad-tools parsers → LSP
```

#### Option B: TypeScript LSP

**Pros**:
- Native VS Code integration
- Faster startup
- Simpler deployment

**Cons**:
- Would need to port/wrap kicad-tools functionality

**Stack**:
```
TypeScript → vscode-languageserver → kicad-tools (WASM?) → LSP
```

#### Option C: Hybrid Approach

**Architecture**:
```
VS Code Extension (TypeScript)
        ↓
    JSON-RPC
        ↓
kicad-tools CLI (existing Node.js)
        ↓
    Parser/Analysis
```

**Pros**:
- Reuses existing kicad-tools CLI commands
- Similar to how atopile's runner works
- Can evolve into full LSP incrementally

### 4.4 Implementation Roadmap

#### Phase 1: Core Infrastructure (Foundation)
- [ ] Create LSP server skeleton using existing kicad-tools parsers
- [ ] Implement document sync (full mode initially)
- [ ] Add diagnostic publishing for parse errors

#### Phase 2: DRC/ERC Integration
- [ ] Wire existing DRC checker to diagnostics
- [ ] Add ERC rule checking
- [ ] Map violations to source locations

#### Phase 3: Navigation Features
- [ ] Implement hover for component/net info
- [ ] Add go-to-definition for net references
- [ ] Support component cross-referencing

#### Phase 4: Code Completion
- [ ] Component reference completion
- [ ] Net name completion
- [ ] Library item completion

---

## Key Learnings from Atopile

1. **Use pygls**: It handles LSP boilerplate, letting you focus on language semantics

2. **Debounce changes**: 2-second delay prevents excessive recompilation

3. **Build incomplete documents**: Remove the current line for completion support

4. **Store graphs in memory**: Quick access for hover/completion without re-parsing

5. **Use full document sync initially**: Incremental sync is complex and can be added later

6. **Leverage existing tooling**: Atopile reuses its compiler frontend for LSP analysis

7. **Custom notifications**: `atopile/didChangeBuildTarget` shows how to extend LSP

---

## Related Issues

- Future: Create LSP server for KiCad S-expression files
- Future: Add diagnostics (DRC/ERC) as you type
- Future: Implement hover for component info
- Future: Add go-to-definition for net references
- Future: Support code completion for component refs

---

## Conclusion

Atopile's LSP implementation provides an excellent reference architecture for adding IDE integration to kicad-tools. The pygls-based approach, combined with reusing existing parsers and analysis tools, offers a practical path forward. The recommended starting point is diagnostics integration, as it provides immediate value with the existing DRC/ERC infrastructure.
