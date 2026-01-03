# Development Guide

This guide covers setting up a development environment, running tests, and contributing to kicad-tools.

---

## Prerequisites

- Python 3.10 or higher
- Git
- (Optional) KiCad 8.0+ for integration tests

---

## Setup

### 1. Clone the Repository

```bash
git clone https://github.com/rjwalters/kicad-tools.git
cd kicad-tools
```

### 2. Create Virtual Environment

```bash
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
```

### 3. Install Development Dependencies

```bash
pip install -e ".[dev]"
```

This installs:
- The package in editable mode
- Testing dependencies (pytest, pytest-cov)
- Linting tools (ruff, mypy)
- Documentation tools

---

## Project Structure

```
kicad-tools/
├── src/kicad_tools/      # Main source code
│   ├── cli/              # CLI commands
│   ├── core/             # S-expression parsing
│   ├── schema/           # Data models
│   ├── query/            # Query API
│   ├── router/           # Autorouter
│   ├── optim/            # Placement optimization
│   ├── drc/              # Design rule checking
│   ├── erc/              # Electrical rule checking
│   ├── manufacturers/    # Manufacturer rules
│   ├── schematic/        # Schematic operations
│   ├── pcb/              # PCB operations
│   ├── parts/            # LCSC integration
│   ├── datasheet/        # PDF parsing
│   ├── export/           # Manufacturing export
│   └── reasoning/        # LLM integration
├── tests/                # Test suite
│   ├── fixtures/         # Test KiCad files
│   └── ...
├── examples/             # Example scripts
├── docs/                 # Documentation
└── pyproject.toml        # Project configuration
```

---

## Running Tests

### Run All Tests

```bash
pytest
```

### Run with Coverage

```bash
pytest --cov=kicad_tools --cov-report=html
open htmlcov/index.html  # View coverage report
```

### Run Specific Tests

```bash
# Run tests in a specific file
pytest tests/test_schematic.py

# Run tests matching a pattern
pytest -k "test_symbols"

# Run with verbose output
pytest -v
```

### Test Fixtures

Test KiCad files are in `tests/fixtures/`. When adding new tests:

1. Add minimal KiCad files that demonstrate the feature
2. Keep files small to speed up tests
3. Use descriptive names (e.g., `simple_led_circuit.kicad_sch`)

---

## Code Style

### Formatting with Ruff

```bash
# Check formatting
ruff check src/

# Auto-fix issues
ruff check --fix src/

# Format code
ruff format src/
```

### Type Checking with Mypy

```bash
mypy src/kicad_tools/
```

### Pre-commit Hooks

Install pre-commit hooks to run checks automatically:

```bash
pip install pre-commit
pre-commit install
```

---

## Making Changes

### 1. Create a Branch

```bash
git checkout -b feature/my-feature
```

### 2. Make Your Changes

- Follow existing code patterns
- Add tests for new functionality
- Update documentation as needed

### 3. Run Checks

```bash
# Run tests
pytest

# Check formatting
ruff check src/

# Type check
mypy src/kicad_tools/
```

### 4. Commit

```bash
git add -A
git commit -m "Add feature X"
```

Follow commit message conventions:
- Use imperative mood ("Add feature" not "Added feature")
- Keep first line under 50 characters
- Add details in body if needed

### 5. Push and Create PR

```bash
git push -u origin feature/my-feature
```

Then create a pull request on GitHub.

---

## Architecture Guidelines

### Round-Trip Fidelity

When modifying KiCad files:
- Preserve existing formatting where possible
- Don't reorder elements unnecessarily
- Maintain comments and whitespace

```python
# Good: Modify specific node
symbol.get_property("Value").value = "10k"

# Bad: Rebuild entire structure
symbol = Symbol(name="R1", value="10k", ...)
```

### Error Handling

Return actionable errors:

```python
# Good: Specific, actionable
raise DRCViolation(
    "Clearance violation: C1 pad 1 too close to R2 pad 2",
    location=(10.5, 20.3),
    suggestion="Move C1 0.2mm right"
)

# Bad: Generic
raise ValueError("DRC failed")
```

### Query API Design

Extend the fluent query API for new filters:

```python
class SymbolQuery:
    def filter(self, **kwargs) -> "SymbolQuery":
        """Filter by any attribute."""
        ...

    def smd(self) -> "SymbolQuery":
        """Filter to SMD components only."""
        return self.filter(mounting="smd")
```

### CLI Commands

Add new commands by:

1. Create `src/kicad_tools/cli/mycommand.py`
2. Implement `main(argv)` function
3. Register in `commands.py` and `parser.py`
4. Support `--format json` for machine-readable output

```python
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--format", choices=["table", "json"], default="table")
    args = parser.parse_args(argv)

    result = do_something()

    if args.format == "json":
        print(json.dumps(result, indent=2))
    else:
        print_table(result)

    return 0
```

---

## Adding a New Module

1. Create directory: `src/kicad_tools/mymodule/`
2. Add `__init__.py` with public exports
3. Add tests in `tests/test_mymodule.py`
4. Document in `docs/reference/`
5. Export from main `__init__.py` if public API

---

## Release Process

1. Update version in `src/kicad_tools/__init__.py`
2. Update `CHANGELOG.md`
3. Create a git tag: `git tag v0.X.0`
4. Push tag: `git push origin v0.X.0`
5. GitHub Actions builds and publishes to PyPI

---

## Getting Help

- **Issues**: https://github.com/rjwalters/kicad-tools/issues
- **Discussions**: https://github.com/rjwalters/kicad-tools/discussions
