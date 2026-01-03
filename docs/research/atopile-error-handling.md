# Atopile Error Handling & Rich Diagnostics - Research Summary

## Overview

Atopile implements a sophisticated error handling system that provides:
1. Source-attached exceptions with file:line:col tracking
2. Rich terminal rendering with syntax highlighting
3. Error accumulation for batch validation
4. Intelligent exception deduplication
5. Multi-level logging with structured output

## Key Components

### 1. Source-Attached Exceptions (`errors.py`)

Atopile exceptions carry source location information through ANTLR tokens:

```python
class _BaseUserException(_BaseBaseUserException):
    def __init__(
        self,
        msg: str,
        token_stream: CommonTokenStream | None = None,
        origin_start: Token | None = None,
        origin_stop: Token | None = None,
        traceback: Sequence[ParserRuleContext | None] | None = None,
        markdown: bool = True,
        code: str | None = None,
    ): ...
```

**Key Features:**
- `origin_start` / `origin_stop`: Token range for error location
- `traceback`: Sequence of parser contexts for call stack
- `attach_origin_from_ctx()`: Method to attach source location from parser context

**Factory Methods:**
```python
# Create from parser context
UserException.from_ctx(ctx, "Error message")

# Create from token range
UserException.from_tokens(token_stream, start_token, stop_token, "Error message")
```

### 2. Rich Console Rendering

Exceptions implement `__rich_console__` for beautiful terminal output:

```python
def __rich_console__(self, console, options) -> list[ConsoleRenderable]:
    renderables = []

    # Title (bold)
    if self.title:
        renderables += [Text(self.title, style="bold")]

    # Message (with optional markdown)
    renderables += [Markdown(self.message) if self.markdown else Text(self.message)]

    # Traceback (highlighted source)
    for ctx in self.traceback or []:
        renderables += _render_tokens(self.token_stream, ctx.start, ctx.stop)

    # Origin code snippet
    if self.origin_start:
        renderables += [Text("Code causing the error: ", style="bold")]
        renderables += _render_tokens(self.token_stream, self.origin_start, origin_stop)

    return renderables
```

**Source Rendering Features:**
- Syntax highlighting via Pygments integration (`PygmentsLexerReconstructor`)
- Line numbers with proper alignment
- Highlighted error lines
- Relative file paths when possible
- Format: `Source: path/to/file.ato:42:10`

### 3. Exception Accumulation (`faebryk/libs/exceptions.py`)

The `accumulate` context manager collects multiple errors before failing:

```python
class accumulate:
    """Collect a group of errors and only raise an exception group at the end."""

    def __init__(
        self,
        *accumulate_types: Type,  # Default: UserException
        group_message: str | None = None,
    ):
        self.errors: list[Exception] = []

    def collect(self) -> contextlib.suppress:
        """Returns context manager to collect exceptions."""

    def raise_errors(self):
        """Raise collected errors as ExceptionGroup."""
```

**Usage Pattern:**
```python
with accumulate(UserException) as accumulator:
    for item in items:
        with accumulator.collect():
            # This error is collected, not raised immediately
            validate(item)
# All errors raised together at end as ExceptionGroup
```

**Iterator Helper:**
```python
for err_cltr, item in iter_through_errors(items, UserException):
    with err_cltr():
        process(item)  # Errors collected, iteration continues
```

### 4. Exception Deduplication

Exceptions have a `get_frozen()` method for deduplication:

```python
def get_frozen(self) -> tuple:
    """Return a hashable version for deduplication."""
    return (self.__class__, self.message, self._title)
    # With source info:
    + get_src_info_from_token(self.origin_start)
    + get_src_info_from_token(self.origin_stop)
```

The logging handler tracks seen exceptions to avoid duplicate output.

### 5. Downgrade and Suppress Patterns

**Downgrade to Warning:**
```python
with downgrade(UserException):
    raise SomeError()  # Logged as warning, not raised
```

**Suppress After Count:**
```python
with suppress_after_count(5, UserException, suppression_warning="..."):
    # Only first 5 errors shown, rest suppressed
```

### 6. Logging Integration

Custom `LogHandler` with exception rendering:

```python
class LogHandler(RichHandler):
    def render_message(self, record, message):
        if isinstance(record.exc_info[1], ConsoleRenderable):
            return exc  # UserExceptions render themselves
        return self._render_message(record, message)
```

**Features:**
- Exceptions are `ConsoleRenderable` - render beautifully in logs
- Traceback suppression for user errors (hide internal frames)
- Per-level log files with color support
- Warning/error counts in progress display

## Design Patterns Worth Adopting

### Pattern 1: Source-Attached Diagnostics

For DRC/ERC errors, attach KiCad source locations:

```python
class KiCadDiagnostic(Exception):
    def __init__(
        self,
        message: str,
        file_path: Path,
        element_type: str,  # "footprint", "track", "via", etc.
        element_ref: str,   # "C1", "R2", etc.
        position: tuple[float, float],
        layer: str | None = None,
    ): ...
```

### Pattern 2: Error Accumulation for Batch Validation

```python
with accumulate(DRCError) as accumulator:
    for rule in drc_rules:
        with accumulator.collect():
            check_rule(rule, board)
# Report all violations at once, not just the first
```

### Pattern 3: Rich Error Output

```python
def __rich_console__(self, console, options):
    return [
        Text("DRC Error: ", style="bold red") + Text(self.title),
        Text(f"Source: {self.file_path}:{self.position}"),
        Panel(self._render_board_snippet(), title="Location"),
        Text("Suggestion: ", style="bold") + Text(self.suggestion),
    ]
```

### Pattern 4: Progressive Disclosure

- Terminal: Brief summary with color
- Log file: Full details with context
- IDE: Rich structured diagnostics via LSP

## Questions Answered

### Q1: How do they accumulate multiple errors before failing?

The `accumulate` context manager collects exceptions using a nested `Pacman` class that catches and stores exceptions without re-raising. At context exit, all collected exceptions are raised as an `ExceptionGroup`.

### Q2: What's the UX for showing errors in terminal vs IDE?

**Terminal:**
- Rich-formatted output with syntax highlighting
- Line numbers and source context
- Markdown support for error messages
- Progress indicators with warning/error counts

**IDE (LSP):**
- Span-based locations for squigglies
- Definition-to-reference navigation
- Source ranges for both origin and related locations

### Q3: How do they handle partial success with warnings?

The `downgrade` context manager converts exceptions to log warnings:
```python
with downgrade(UserDesignCheckException):
    raise UserDesignCheckException(...)  # Logged as warning, execution continues
```

### Q4: Can we improve kicad-tools error messages similarly?

Yes! Key improvements:

1. **Add source locations** - Track board file:position for DRC/ERC errors
2. **Rich console rendering** - Use Rich library for formatted output
3. **Error accumulation** - Report all DRC violations, not just first
4. **Code snippets** - Show relevant S-expression excerpts
5. **Fix suggestions** - Add actionable remediation hints

## Recommended Next Steps

1. **Define `KiCadDiagnostic` base class** with source tracking
2. **Integrate Rich library** for terminal output
3. **Implement `accumulate` pattern** for batch validation
4. **Add snippet extraction** from KiCad files
5. **Create suggestion engine** for common errors

## File References

| File | Purpose |
|------|---------|
| `vendor/atopile/src/atopile/errors.py` | Exception classes with source tracking |
| `vendor/atopile/src/atopile/parse_utils.py` | Source info extraction, Pygments integration |
| `vendor/atopile/src/faebryk/libs/exceptions.py` | Accumulator, downgrade, iteration helpers |
| `vendor/atopile/src/faebryk/libs/app/erc.py` | ERC check with accumulation pattern |
| `vendor/atopile/src/faebryk/libs/app/checks.py` | Design checks with accumulation |
| `vendor/atopile/src/atopile/cli/logging_.py` | Rich logging handler |
| `vendor/atopile/src/atopile/cli/excepthook.py` | Global exception handler |
