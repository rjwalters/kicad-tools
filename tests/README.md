# tests/

Pytest suite for **kicad-tools** — unit, integration, and benchmark coverage
of the `src/kicad_tools` package.

## Running the suite

```bash
uv run pytest                 # full suite (coverage on by default)
uv run pytest -m "not slow"   # fast subset (skip slow tests)
uv run pytest -m benchmark    # A/B benchmarks (skipped by default)
pnpm check:ci                 # pytest + ruff + mypy (the full gate)
```

Configuration lives in `pyproject.toml` under `[tool.pytest.ini_options]`:
`testpaths = ["tests"]`, `pythonpath = ["src"]`, and coverage flags
(`--cov=kicad_tools`) are applied automatically.

## Markers

Declared in `pyproject.toml`:

| Marker | Meaning |
|--------|---------|
| `slow` | Slow tests — deselect with `-m "not slow"`. |
| `integration` | Requires real board fixtures. |
| `benchmark` | A/B benchmarks — run with `-m benchmark`, skipped by default. |

## Layout

Most of the suite is a flat set of ~850 `test_*.py` modules at the root of
`tests/`. Focused test data and subsystems live in subpackages:

| Directory | Contents |
|-----------|----------|
| `fixtures/` | Shared test data and board fixtures. |
| `router/` | Router-focused tests (A*, coupled paths, escape, DRC). |
| `placement/` | Placement and courtyard tests. |
| `parts/` | Parts / library lookup tests. |
| `perf/` | Performance and timing tests. |
| `report/` | Report / feedback rendering tests. |
| `cost/` | Cost-estimation tests. |
| `creepage/` | Creepage/HV-isolation engine, CLI, and standards tests. |
| `baselines/` | Baseline snapshots for regression checks. |
| `install/` | Installer / packaging tests. |

Root-level support files: `conftest.py` (shared fixtures) and `__init__.py`.

`benchmark_results.json` is a **local, gitignored artifact**: the routing
benchmark test in `test_router_integration.py` rewrites it with wall-clock
timings on every run. It exists only for local inspection and is intentionally
not tracked (see #4684).

## C++ backend caveat

Routing and perf tests assume the native C++ router extension is built in the
active worktree. Fresh checkouts and new git worktrees need this explicitly —
`uv sync` does **not** build it:

```bash
uv run kct build-native          # build the extension
uv run kct build-native --check  # verify it is available
```

See the "Fresh worktree checklist" in the repository `README.md` and the
routing-performance note in `CLAUDE.md` for the full setup sequence.
