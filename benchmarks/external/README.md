# External benchmark boards (DeepPCB comparison)

Acquisition layer for Epic #4932's benchmark harness: fetches the three
open-source, human-routed KiCad boards DeepPCB publishes numbers for
(https://deeppcb.ai/benchmarks/), and normalizes each into a route-ready
input by ripping up the existing copper while preserving placement, net
assignments, netclasses, zones, and the board outline.

Like the rest of `benchmarks/`, **nothing in this directory runs under
pytest or CI** (`pyproject.toml` sets `testpaths = ["tests"]`) — these are
scripts a human (or a later phase of Epic #4932, e.g. `kct bench external`)
runs deliberately. Only `tests/test_benchmark_external.py` exercises this
code, against a small tracked fixture and a mocked HTTP layer (no live
network access from the test suite).

## Why board files are NOT committed here

Unlike `benchmarks/hierarchical/` (which commits its generated
`results.md`/`results.json` because its *inputs* are already-tracked
boards under `boards/`), this suite's inputs are third-party repos with
their own licenses and their own size. `fetch_boards.py` downloads each
board's `.kicad_pcb` **at run time** into a gitignored cache directory —
never into a tracked path. If you find yourself tempted to "fix" a missing
board file by copying it into this repo: don't — that reintroduces the
license and repo-size problem this design avoids. Record any new licensing
fact in `boards.toml` instead.

This cache directory (`.cache/kct-benchmarks/external/` by default) is a
**different mechanism** from `tests/conftest.py`'s
`KICAD_TOOLS_EXTERNAL_BOARDS_DIR` / `boards/external/` convention, which
resolves locally-symlinked sibling hardware-fixture repos checked out next
to this repo. Don't conflate the two — see the module docstrings in
`fetch_boards.py` for the distinction.

## Contents

| File | Kind | What it is |
|---|---|---|
| `boards.toml` | tracked manifest | One table per board: pinned repo URL + commit SHA, path to the `.kicad_pcb`, upstream license, and DeepPCB's published reference numbers. |
| `fetch_boards.py` | driver | Downloads each board's pinned commit (GitHub tarball / GitLab archive API) into the cache dir, verifying the fetched tree actually references the pinned SHA before extracting. |
| `normalize.py` | driver | Loads a fetched board (upgrading via `kicad-cli pcb upgrade` first if this repo's parser can't read the legacy format directly), captures the pre-rip-up "human baseline" routing stats, strips all tracks/vias, and saves the route-ready result plus a `<slug>.baseline.json` sidecar. |

## Boards

| Slug | Board | Source | DeepPCB reference |
|---|---|---|---|
| `strf` | STRF RF mixed-signal | `github.com/pms67/STRF-Kicad` | 100% routed, 68 vias, <3 min, 98 airwires |
| `pocketbeagle` | PocketBeagle | `github.com/beagleboard/pocketbeagle` (`KiCAD/`) | 290 airwires (vs-Quilter comparison) |
| `beagleconnect_freedom` | BeagleConnect Freedom | `git.beagleboard.org/beagleconnect/freedom` (`hw/KICAD/`) | 414 airwires (vs-Quilter comparison) |

Each board's `.kicad_pcb` predates KiCad 8 (STRF: KiCad 5.1.5-era format;
PocketBeagle: pre-KiCad-6 format; BeagleConnect Freedom: KiCad 6-era
format) — `normalize.py`'s `kicad-cli pcb upgrade` fallback exists
specifically for these boards. It requires a `kicad-cli` on `PATH` (see the
"Fresh worktree checklist" in this repo's top-level `README.md`); without
one, `normalize.py` reports a clear error rather than a raw parser
traceback.

## Running it

```bash
# 1. Fetch all three boards into the cache dir (no credentials required)
uv run python benchmarks/external/fetch_boards.py

# 2. Normalize them (rip up copper, capture the human baseline, save
#    route-ready output + <slug>.baseline.json)
uv run python benchmarks/external/normalize.py
```

Both scripts accept `--board <slug>` (repeatable) to operate on a subset,
and `--cache-dir` to override where fetched/normalized boards land. See
`--help` on either script for the full option list, or
`KCT_BENCHMARK_EXTERNAL_CACHE_DIR` to override the cache dir via the
environment instead of a flag.

The normalized output plus each board's `<slug>.baseline.json` (the
human's original segment/via counts, trace length, and unrouted-pad count
from `PCB.routing_status()` before rip-up) are what a later phase of Epic
#4932 (#4934's metrics module, `kct bench external`) consumes to run the
router and report both the human baseline and the router's result
side-by-side with DeepPCB's published numbers.
