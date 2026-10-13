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

### Opt-in board guards

`KCT_LATTICE_BOARD_GUARD=1` enables the board-05 case of
`test_lattice_boards_manufacturable_6449.py` (Issues #6449, #6463): a fresh
4-layer lattice route of board 05 that must reach 33/33 nets, 0 kicad-cli
errors and unconnected items, 0 `kct check` errors and exit 0. It takes
about 15-25 minutes, so it is skipped unless the variable is set. The
board-04 (`--layers 2`, about 40 s) case of the same test needs no variable;
it runs on every `-m slow` run, including the twice-daily `slow-tests.yml`
job. That workflow sets the variable only on its weekly (Sunday) schedule.

Both cases need kicad-cli on `PATH` and the native router backend
(`uv run kct build-native`; see below). Run it before merging changes to
lattice search geometry, pad keep-outs, clearance floors or plane-via
reservation:

```bash
uv run kct build-native
KCT_LATTICE_BOARD_GUARD=1 uv run pytest \
    tests/test_lattice_boards_manufacturable_6449.py -m slow --no-cov -s
```

## Layout

Most of the suite is a flat set of ~880 `test_*.py` modules at the root of
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
