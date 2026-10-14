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
from `PCB.routing_status()` before rip-up) are what Epic #4932 Phase 2's
`kct bench external` CLI (issue #4941) consumes to run the router and
report both the human baseline and the router's result side-by-side with
DeepPCB's published numbers.

## `kct bench external` -- the end-to-end zero-touch protocol (issue #4941)

`kct bench external` drives fetch -> normalize -> route -> measure as one
command, using rules as-shipped (no netclass/diff-pair tuning) -- the
"zero-touch" protocol mirroring DeepPCB's own vs-Quilter methodology:

```bash
# Every board in boards.toml, JSON + markdown reports under
# .cache/kct-benchmarks/external/results/ by default
uv run kct bench external

# Just STRF, with progress output
uv run kct bench external --board strf -v
```

It gates timing on the C++ router backend exactly as
`kct build-native --check` does (see the top-level `CLAUDE.md`): when the
backend is unavailable, routing still runs so completion/via/wirelength/
DRC metrics are still measured, but no wall-clock number is recorded --
never a Python-fallback timing in a published comparison. See
`docs/benchmark-external-report-schema.md` for the report schema and
`kct bench external --help` for the full flag list (`--seed` for
deterministic reruns, `--skip-fetch` to reuse an already-fetched board,
`--skip-kicad-cli-drc` for hosts without `kicad-cli`).

## `--tuned` -- the declared-netclass protocol for STRF (issue #4943)

`--tuned` runs the SAME fetch -> normalize -> route -> measure pipeline,
but applies a declared per-board netclass/diff-pair config before
routing, mirroring DeepPCB's own published STRF case study
(https://deeppcb.ai/benchmark/mixed-signal-rf-board-routing/): the USB
2.0 data pairs at a 0.20mm track / 0.15mm gap for the 90 ohm target, and
the SPI bus on its own compact-via netclass. See
`benchmarks/external/tuned_rules.py` for the exact values, net names, and
the sourcing/schema-mapping notes.

```bash
uv run kct bench external --board strf --tuned -v
```

Currently defined for STRF only -- `--tuned` against any other board
slug fails that board with a clear per-board error (`errors` in the JSON
mode payload) rather than silently falling back to zero-touch rules or
guessing at unrelated net names.

**The tuned and zero-touch reports are always kept separate** (Epic
#4932's stated risk register: DeepPCB's case-study numbers involved
manual netclass setup, while their vs-Quilter numbers were zero-touch --
conflating the two would misrepresent both). Output files are named per
protocol so a `--tuned` run never overwrites a prior zero-touch run in
the same `--output-dir`: `<slug>.<protocol>.json` and
`report.<protocol>.md` (e.g. `strf.tuned.json` / `report.tuned.md` vs.
`strf.zero-touch.json` / `report.zero-touch.md`).

## PCBWorld suite (`--suite pcbworld`, issue #6331)

PCBWorld's D3 is a *generated* corpus: PCBWorld's own prep code converts
PCBench source boards to KiCad 9 and writes the scored `guide` variant. The
suite therefore pins two upstream repositories in [`pcbworld.toml`](pcbworld.toml)
instead of listing boards in `boards.toml`:

| Input | Pin | License |
|---|---|---|
| PCBWorld (`LGAI-Research/PCBWorld`) | `b3d62f5c37e7528670d112e03d9a90029f23f4f3` | BSD-3-Clause; its `engine` submodule is GPL-3.0 and is never fetched or built here |
| PCBench (`PCBench/PCBench`) | `dec3be75cbdef74787625f9043c7391cd473bb64` | MIT repository, **mixed per-board licenses** -- boards are cache-only |

Nothing third-party is committed: both repos are fetched into the gitignored
cache (`.cache/kct-benchmarks/external/pcbworld/`), PCBWorld's prep steps
(`convert_v9`, `drc_fix_v9`, `make_guide`, `sort_prefix`) and scorer
(`eval.pipeline`) run **only as subprocesses** under PCBWorld's own
interpreter with its checkout as cwd (PCBWorld ships a `kicad_tools.py` that
would shadow this package if imported). Split membership comes from PCBWorld's
own `configs/datasets/d3.json` (`d3a` = easy/test, `d3b` = medium/test).

### One-time setup (manual; not automated)

You need a KiCad **9** toolchain and a Python environment for PCBWorld
(see PCBWorld's `tools/datagen/pcbench_prep/README.md`). Building PCBWorld's
GPL engine is out of scope; point at existing installs:

```bash
export PCBWORLD_PYTHON=/path/to/pcbworld-env/bin/python   # PCBWorld's own environment
export KICAD_CLI=/path/to/kicad9/kicad-cli                # must report 9.x
export PCBNEW_PYTHON=/path/to/python-with-kicad9-pcbnew
uv run kct build-native --check                           # timing needs the C++ backend
```

### Run

```bash
# Deterministic smoke subset: the first 3 boards of D3-A's test list
uv run kct bench external --suite pcbworld --split d3a --limit 3
```

Boards are identified `pcbworld/<split>/<NNNN_name>` (taken from PCBWorld's
split file). The suite skips `normalize_board` (guide boards are already
stripped; ripping up again could alter the task) and fails if a prepared
board contains any track or via.

### Two scorers, never merged

Each report keeps the existing local gates (`kct_check`, `kicad_cli_drc`,
completion) **and** an additive `external_scores.pcbworld` block with
PCBWorld's own Clean Pass, routability, error-only DRC count and *per-rollout*
`potential_gain` (PCBWorld's @5 rule selects by largest potential gain, not
"any rollout clean"). The markdown table shows them in separate columns
(`kicad-cli DRC` vs `PCBWorld CP`).

* An unavailable PCBWorld score (no routed output, scorer failure) is `null`
  with a reason -- never `0`, never a pass.
* A non-KiCad-9 `KICAD_CLI` makes the run fail before preparation; a Clean
  Pass from another major version is never reported.
* Rebuilt boards are **not** byte-identical to the paper's set (KiCad 9's
  DRC report caps change `make_guide` widths on some boards; UUIDs are
  re-minted). Provenance records the PCBWorld/PCBench SHAs, `kicad-cli`
  version, `pcbnew` Python and a per-board input SHA-256; do not claim
  byte-identity.

Phase 1 only prepares and validates the protocol. Full D3-A/D3-B runs and
CP@1/CP@5 publication belong to #6332 and #6333. The unit/CLI tests mock
PCBWorld, KiCad and the network entirely.

## Committed results (issue #4942, Epic #4932 Phase 2)

`results/` holds the committed JSON + markdown output of zero-touch runs
against `pocketbeagle` and `beagleconnect_freedom` (see `results/README.md`
for what's tracked there and why the `normalized/`/`routed/` board-copy
subdirectories `--output-dir` also produces are never committed). The original committed snapshot records both boards refusing zero-touch routing
before producing copper. Those historical outcomes motivated #4945 and #4946.
Subsequent measurements are recorded separately; see the
[2026-09-15 follow-up](results/2026-09-15/) for its exact source, inputs and
observed partial-routing outcome.
