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

### Important: Pipx vs Source Conflicts

If you have kicad-tools installed via pipx (e.g., from a release) and are also
working with the source repository, you may encounter import errors:

```
ImportError: cannot import name 'new_function' from 'kicad_tools.module'
```

This happens because Python imports from the pipx-installed version instead of
your source code. New functions added to source won't be available.

**Solution 1: Reinstall from source (recommended for testing)**

```bash
pipx install --force .
```

**Solution 2: Use an editable install for development**

```bash
pipx uninstall kicad-tools
pip install -e ".[dev]"
```

**Solution 3: Use a virtual environment (cleanest)**

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

The board generation scripts in `boards/` include automatic version mismatch
detection and will warn you if the installed version differs from source:

```
⚠️  Version mismatch detected!
   Installed: 0.9.2
   Source:    0.9.3
```

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

## C++ Native Extensions

The router and placement optimizer ship optional C++ acceleration modules
(`router_cpp`, `placement_cpp`) for 10-100x speedup over the pure-Python
implementation. They are built with nanobind + CMake and live under
`src/kicad_tools/router/cpp/` and `src/kicad_tools/placement/cpp/`.

### Building the C++ extensions

```bash
# Build (requires CMake 3.15+ and a C++20 compiler)
bash scripts/build-cpp.sh build

# Or via the CLI:
kct build-native

# Clean build artifacts:
bash scripts/build-cpp.sh clean
```

### Build version discipline (Issue #2501)

The Python side caches a compiled `.so` matching `cpython-<X>-<platform>.so`.
If the `.cpp` source moves ahead of the compiled `.so`, the import will
succeed but new symbols (`PadBounds`, `FAILURE_NONE`, etc.) will be missing,
causing hard `AttributeError` crashes deep in the routing code.

To prevent this, the build surface is gated by an integer constant
`ROUTER_CPP_BUILD_VERSION`, mirrored on both sides:

- `src/kicad_tools/router/cpp/include/types.hpp` (C++ source)
- `src/kicad_tools/router/cpp_backend.py` as `_REQUIRED_CPP_BUILD_VERSION`
  (Python side)

**When you change anything under `src/kicad_tools/router/cpp/` that affects
the binding surface (added/removed/renamed symbols, struct fields, function
signatures), you MUST:**

1. Bump `ROUTER_CPP_BUILD_VERSION` in `cpp/include/types.hpp`.
2. Bump `_REQUIRED_CPP_BUILD_VERSION` to the same value in `cpp_backend.py`.
3. Rebuild with `kct build-native` (or `bash scripts/build-cpp.sh build`).

If the two constants disagree at import time, the C++ backend is disabled
with a clear `kct build-native` rebuild hint and the router falls back to
pure Python rather than crashing at routing time. The CI `cpp-build-check`
job rebuilds from a clean checkout and asserts the guard reports the
backend as available, so a forgotten version bump or stale `.so` will fail
CI rather than silently regress production.

### How the build verifies itself (Issue #4589)

`kct build-native` verifies the extension it just installed by running
`get_backend_info()` in a **freshly spawned interpreter**
(`cpp_backend.probe_backend_info()`), not by reloading the module in the
process that did the build. This matters because a C extension cannot be
re-imported from a replaced file in the same process: once
`router_cpp.*.so` has been `dlopen`'d, CPython keeps the initialised module
in a runtime extension cache that neither `sys.modules.pop()` nor
`importlib.invalidate_caches()` clears, so the re-import returns the
*identical* module object — describing the **pre-build** extension.

`kct build-native --check` resolves availability through the same
`probe_backend_info()` helper, so the two commands cannot report different
answers for the same on-disk state. They previously could, in both
directions:

| Rebuild trigger | Old in-process probe | Reality |
|---|---|---|
| `BUILD_VERSION` mismatch | `Extension installed but not loading correctly` | fine — `--check` reported *available* seconds later |
| source newer than `.so` (mtime) | `installed successfully!` | unverified — success was asserted against code the process never loaded |

Practical consequences for contributors:

- **A build failure warning now names the cause**: the
  `unavailable_reason` (ImportError text / ABI mismatch / `BUILD_VERSION`
  mismatch), the `.so` path probed, and the interpreter used.
- **`--check` still compiles nothing** and stays sub-second in a warm venv;
  it returns before `build_native()` is ever called. A multi-minute
  `uv run kct build-native --check` in a fresh worktree is `uv sync`
  creating the venv, not this command building.
- **`--check` also prints `BUILD_VERSION`**, which is the number the
  staleness guard actually checks — the `version 1.0.0` string is the
  extension's own version and never changes on a bindings bump.
- **`kct route`'s silent auto-build** goes through the same probe. When a
  rebuild succeeds but the running process cannot hot-swap the replaced
  extension, it now says exactly that ("re-run the command") instead of
  announcing a 10-100x-slower Python fallback as if the build had failed.
- The `.so` is installed with a temp file + `os.replace()`, so a
  concurrently running process never reads a half-written image, and
  `_find_installed_so()` resolves the extension suffix of the **running**
  interpreter (`importlib.machinery.EXTENSION_SUFFIXES`) instead of the
  first `glob` hit — a checkout carrying 312/313/314 builds side by side no
  longer makes rebuild decisions against a `.so` it never loads.

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

CI does not run bare `mypy` — it runs the baseline gate, which fails only on
errors beyond the committed `.github/mypy-baseline.txt` ledger:

```bash
uv run python scripts/ci/check_mypy_baseline.py
```

#### The stale `.mypy_cache/` trap

**If a local type error names a file that is not in your diff, clear the cache
and re-run before investigating it.**

```bash
rm -rf .mypy_cache     # or: /repo:tidy --caches
```

Bare `mypy`, `pnpm typecheck`, and `pnpm check:all` are all **incremental**:
mypy reuses `<cwd>/.mypy_cache/`, validating each cached module by
`(mtime, size)`. `.mypy_cache/` is gitignored, so `git reset --hard`,
`git clean -fd` (ignored paths need `-x`), a rebase, a force-push, and a Loom
worktree reset **all leave it in place** — a cache computed against an older
tree can outlive that tree and replay an error that no longer exists.

CI never hits this: there is no `actions/cache` in `.github/workflows/` and the
Type Check job pins `astral-sh/setup-uv` with `enable-cache: false`, so every
CI run is cold. That asymmetry is why a local "NEW type error" can be green on
CI — it cost two independent PR reviews a cycle each during the 2026-08-08/09
sweep (PRs #4746, #4762), both on a phantom error in a file neither PR touched.

`scripts/ci/check_mypy_baseline.py` is immune by construction: it passes
`--no-incremental`, so its verdict never depends on a cache. That costs ~20 s
per run (vs ~0.5 s warm) on `src/`; pass `--incremental` (alias `--warm-cache`)
for a tight burn-down loop, and re-confirm any finding cold before acting on it.

Do **not** reach for `--update` to make a phantom error go away — that bakes it
into the ledger. The remedy is a cold re-run.

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
    suggestion="Move C1 0.2mm right",
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

**[`RELEASING.md`](../../RELEASING.md) is the canonical process — follow it, not
this summary.** Releases are PR-based: the version-bump commit reaches `main`
through a pull request, and the `vX.Y.Z` tag is created only afterwards, on the
merged `main` SHA.

1. Reconcile `CHANGELOG.md` against `git log <last-tag>..main` —
   `uv run python scripts/changelog_gap_report.py` must exit 0 with an empty gap
   set (`RELEASING.md` step (0))
2. Bump the version in `pyproject.toml` (the source of truth) and regenerate
   `uv.lock`, on a `release/vX.Y.Z` branch
3. Open a PR and merge it via `./.loom/scripts/merge-pr.sh <PR>`
4. Tag the **merged `main` SHA**: `git tag -a vX.Y.Z -m "Release X.Y.Z"`
5. Push the tag — GitHub Actions builds the tagged commit and publishes to PyPI

---

## Getting Help

- **Issues**: https://github.com/rjwalters/kicad-tools/issues
- **Discussions**: https://github.com/rjwalters/kicad-tools/discussions
