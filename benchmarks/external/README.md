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
(see PCBWorld's `tools/datagen/pcbench_prep/README.md` and `docs/QUICKSTART.md`).
**Scoring needs PCBWorld's engine build**: `eval/metrics.py` computes the
potential (and so `potential_gain`, the @5 selection key) through
`kicad_rl_router`, the GPL engine PCBWorld builds with
`tools/setup/setup_all.sh` (conda env + C++ build). kicad-tools never builds
or links it -- `PCBWORLD_PYTHON` must point at an environment where it is
already built (`python -c 'import kicad_rl_router'` succeeds with
PCBWorld's `PYTHONPATH`). Point at existing installs:

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
split file). The suite skips `normalize_board` and fails if the routing input
contains any track or via.

**Which prepared file is routed (issue #6332).** PCBWorld's `board_filename`,
`processed_v9_guide_v3.kicad_pcb`, is the *routed* guide board -- the human
copper with per-net guide widths ("the routed guide board is the artifact the
benchmark loads", `make_guide.md`); PCBWorld deletes its tracks and vias at
`reset()`. kct therefore routes the bare variant `make_guide` writes next to
it, `processed_v9_guide_v3_unrouted.kicad_pcb` (tracks, arcs and vias removed,
zones kept -- the same board `reset()` produces), with the guide's
`.kicad_pro` design rules. The routed guide is read only as the human
wirelength / via reference. (#6331 pointed the suite at the routed guide and
asserted it was unrouted, which would have refused every real board; its
tests mocked `make_guide` and could not see it.)

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

### Publishable D3 runs: rollouts, resume, bounded windows (issue #6332)

```bash
# Step 0 -- is kct route a deterministic baseline? (sequential, no fan-out)
uv run python benchmarks/external/seed_variation.py --seeds 1 2 3 4 5 \
  --timeout 600 -o seed_variation.json <5 prepared d3a *_unrouted.kicad_pcb boards>

# kct route is seed-dependent under the C++ backend (2026-10-15 measurement,
# results/2026-10-15-pcbworld-d3a/): five declared rollouts, seeds 42..46
uv run kct bench external --suite pcbworld --split d3a --full-split \
  --rollouts 5 --route-timeout 600 --output-dir .cache/kct-benchmarks/external/d3a-run

# Bounded sittings on a shared host: N new attempts per invocation, then resume
uv run kct bench external --suite pcbworld --split d3a --full-split \
  --rollouts 5 --route-timeout 600 --max-attempts 50 --resume \
  --output-dir .cache/kct-benchmarks/external/d3a-run   # repeat until complete

# Only if seed_variation.py ever shows NO seed dependence: one rollout, @5 = @1
#   ... --full-split --deterministic-baseline --route-timeout 600 ...

# Re-render the report from committed compact data (no routing, no network)
uv run kct bench external --render-d3 benchmarks/external/results/<dir>/results.json
```

* `--full-split` makes the denominator the whole split (d3a = **99** boards,
  PCBWorld's `easy.test`; the 100th easy board, `0096_*`, is train-only).
  `--offset` / `--limit` only bound which boards one invocation attempts.
* Every board x rollout ends up with a record in `results.json`: a result,
  or an explicit `prep_failed` / `route_no_output` / `scorer_unavailable` /
  `harness_error` / `missing` row. A router that wrote nothing, or a
  timed-out attempt, is a **fail**; a prep or scorer gap is **unknown** and
  the report then shows `[lower, upper]` bounds instead of a point CP.
* **@5** keeps the rollout with the largest potential gain (PCBWorld's
  `final_potential` key: then routability, error-level DRV, wirelength,
  vias, lowest index) and reads every number off it. With
  `--deterministic-baseline` (one rollout) @5 is reported equal to @1, as
  PCBWorld does for deterministic baselines; with one rollout and no such
  declaration @5 is "not measured".
* The `fair95` sub-denominator (PCBWorld's RQ2 "PCBench fair-95" list,
  `pcbworld.toml` `[subsets.fair95]`) is reported next to the full split.
* `--resume` reuses an attempt record only when its whole identity matches:
  board, rollout, seed, routing-input SHA-256, kicad-tools commit,
  PCBWorld/PCBench pins, KiCad version and router options. Anything else is
  discarded and re-attempted; without `--resume` the ledger is cleared.
  Unknown-status records inside the current window are always retried.
* Timing is recorded only under the C++ backend (`probe_backend()`); refused
  timings are counted, never shown as numbers.

**Expected runtime and storage (d3a).** These are planning figures, not
measurements -- no D3-A board has been routed yet (see
[`results/2026-10-15-pcbworld-d3a/`](results/2026-10-15-pcbworld-d3a/)).
PCBWorld's own prep runs several `kicad-cli` DRC trials per board
(`make_guide`) at `PREP_WORKERS = 2`; its duration is unmeasured. D3-A
boards are PCBWorld's *easy* tier (the smallest by pin count), so most
`kct route` attempts should finish well inside `--route-timeout 600`. As a
proxy only, the five in-tree boards of the seed-variation run took 4-60 s
per completed route and ~170 s when budget-limited. The hard ceiling is
99 x 5 x 600 s = 82.5 h for five rollouts, which is what `--max-attempts` +
`--resume` are for. Each attempt keeps a routed copy plus a scoring copy of
the board in the output dir (tens to hundreds of KB per board), plus the
per-board JSON report -- an estimated well under 1 GB for 5 x 99 attempts.
The extracted PCBWorld source at the pin measured 23 MB; PCBench is fetched
only for the selected boards. The separate PCBWorld conda env + engine build
is not counted. Only `results.json` + `report.d3.md` (+ the
hand-written verdict) are committed.

The unit/CLI tests mock PCBWorld, KiCad and the network entirely.

## Committed results (issue #4942, Epic #4932 Phase 2)

`results/` holds the committed JSON + markdown output of zero-touch runs
against `pocketbeagle` and `beagleconnect_freedom` (see `results/README.md`
for what's tracked there and why the `normalized/`/`routed/` board-copy
subdirectories `--output-dir` also produces are never committed). The original committed snapshot records both boards refusing zero-touch routing
before producing copper. Those historical outcomes motivated #4945 and #4946.
Subsequent measurements are recorded separately; see the
[2026-09-15 follow-up](results/2026-09-15/) for its exact source, inputs and
observed partial-routing outcome.
