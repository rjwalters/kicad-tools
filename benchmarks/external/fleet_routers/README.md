# Fleet-router protocol: TraceMaker vs fastroute vs `kct route`

Issue #6334 (parent #5943). This is a research-only driver for comparing three routers on the committed demo fleet. Every router gets identical inputs and is graded by one referee, against rules none of them can rewrite. Nothing here runs in CI or ships in the `kct` CLI. The only part that runs in tests is `tests/test_fleet_router_protocol.py`, which uses fake router executables and small fixtures.

Both upstream routers are GPL-3.0. The ecosystem registry marks them `copyleft-ideas-only` (`kct ecosystem show tracemaker`). They are built and run **out of tree, in a scratch directory**. Never vendor, link or copy their code into this repository.

| File | What it is |
|---|---|
| `manifest.json` | Upstream pins, verified CLI commands, budgets, the stripping policy, the referee, and the per-board frozen inputs with sha256s |
| `run.py` | `verify` / `freeze` / `run`: preparation, the three adapters, scoring, and the outcome records |

## Prerequisites (not installed by this driver)

- **kct**: a worktree with the C++ backend built **in that worktree**: `uv run kct build-native --check` must report `available`. Without it the kct row is `tool-failure` / `native-backend`; it is never timed on the Python fallback.
- **kicad-cli**: the referee. Its `--version` is recorded in `results.json`.
- **fastroute** at the pinned commit. It needs a Rust toolchain:

  ```bash
  git init fastroute && cd fastroute
  git fetch --depth 1 https://github.com/parisxmas/fastroute e5d52c4544795422b8b28077acc0605fdf11265a
  git checkout FETCH_HEAD
  cargo build --release -p fastroute --locked -j 2   # -> target/release/fastroute
  ```

  If `RUSTC_WRAPPER` points at a missing `sccache`, prefix the build with `env -u RUSTC_WRAPPER`.
- **TraceMaker** at the pinned commit, built CPU-only:

  ```bash
  cmake -S TraceMaker -B TraceMaker/build/cpu-only -DCMAKE_BUILD_TYPE=RelWithDebInfo \
        -DTM_ENABLE_CUDA=OFF -DTM_BUILD_TESTS=OFF -DTM_WERROR=OFF -DTM_BUILD_PYTHON=OFF
  cmake --build TraceMaker/build/cpu-only --target tracemaker -j 2
  ```

  It needs CMake ≥ 3.28, a C++20 compiler, Boost ≥ 1.81, Eigen3 3.4, SQLite3, nlohmann_json 3, CLI11 and zstd. The CMake presets assume g++-15, Ninja and ccache, so the commands above bypass them. If the build fails, keep its log and pass `--tracemaker-build-log`. The row is then recorded as `tool-failure` / `build` with the log attached. Do not substitute a mock.


### Building TraceMaker's missing dependencies without touching the host

Dependencies the host lacks are built into a scratch prefix, outside the repository, never installed. On a host with Boost, Eigen3, SQLite3 and zstd development packages, only the two header-only libraries were missing (Ubuntu 24.04, CMake 3.28, g++ 13; the presets' g++-15 is not needed):

```bash
P=/tmp/fleet/prefix
git clone --depth 1 -b v3.11.3 https://github.com/nlohmann/json  && cmake -S json  -B json/b  -DJSON_BuildTests=OFF -DCMAKE_INSTALL_PREFIX=$P && cmake --install json/b
git clone --depth 1 -b v2.4.2  https://github.com/CLIUtils/CLI11 && cmake -S CLI11 -B CLI11/b -DCLI11_BUILD_TESTS=OFF -DCLI11_BUILD_EXAMPLES=OFF -DCMAKE_INSTALL_PREFIX=$P && cmake --install CLI11/b
# then the TraceMaker configure command above with -DCMAKE_PREFIX_PATH=$P
```

## Live smoke: board 00 (2026-10-15)

`run.py run --boards 00 --wall-cap 300` at kct `ccfc97260`, kicad-cli 10.0.6, native backend 1.0.0, TraceMaker `89ff58ca` (built as above; build environment recorded in the manifest's `tracemaker.build_environment`; binary sha256 prefix `49d99f58...`) and fastroute `e5d52c45` (binary sha256 prefix `f141bce5...`). The full binary hashes and the run's `results.json` were not retained from the original run host; the next live run records them (see #6335). Fleet inputs for boards 03 and 06 had drifted since the first freeze, so the manifest was re-frozen.

| Tool | Outcome | Connections | DRC errors | Unconnected | Vias | Wirelength mm | Wall s |
|---|---|---|---|---|---|---|---|
| input | reference | 0/3 | 0 | 3 | 0 | 0 | |
| kct | success | 3/3 | 0 | 0 | 0 | 8.71 | 9.41 |
| tracemaker | success | 3/3 | 0 | 0 | 0 | 29.66 | 0.16 |
| fastroute | non-comparable (11 constraints lost at the DSN/SES boundary) | 3/3 | 0 | 0 | 0 | 30.17 | 0.01 |

The fastroute row is `non-comparable` because 11 constraints are lost, none of them caused by #6593 or #6594 (both closed on 2026-10-14, before this run's base):

- **10 `dru` entries:** JLCPCB fab-floor rules the DSN exporter does not represent (silk clearance, `hole_clearance` x2, `annular_width` x2, `edge_clearance`, `physical_hole_clearance`, text thickness, text height, and the `Reviewed clearance - Power` clearance). Tracked in #6765.
- **1 `ses_import` entry** ("SES resolution um 10: importer assumes 1 unit = 1 um"). This was a false loss: since #6593 the importer honours `(resolution um 10)` and the imported 30.17 mm agrees with TraceMaker's 29.66 mm. `run.py:ses_import` now reports a loss only when the resolution the importer applied differs from the one the session declares.

This is a single run on one simple board, so it says nothing about router quality. The full matrix is #6335.

## Running

```bash
uv run python benchmarks/external/fleet_routers/run.py verify      # hashes still match?
uv run python benchmarks/external/fleet_routers/run.py run \
    --work-dir /tmp/fleet --boards 00 \
    --tracemaker-dir /scratch/TraceMaker --tracemaker-build-log /scratch/tm-build.log \
    --fastroute-dir /scratch/fastroute
```

- `--work-dir` must be outside the repository. `run` refuses to start if the manifest no longer matches the tree.
- `--<tool>-dir` is the pinned checkout. The driver checks `git rev-parse HEAD` against the pin, and a mismatch is `tool-failure` / `pin`. The executable defaults to the checkout's `default_executable` (see the manifest) and can be overridden with `--<tool>-exe`.
- `--wall-cap` overrides `budgets.wall_cap_s`. Record any override in the write-up.
- Output goes to `results.json` (environment plus one record per board × tool, including a `reference` row for the normalized input) and a Markdown table on stdout. Logs are under `<work>/<board>/<tool>/run/`. Each run directory is wiped first, so a rerun can never pick up stale artifacts.

## What is frozen

- **Inputs.** Each board's unrouted `output/<stem>.kicad_pcb`, `.kicad_pro` and `.kicad_sch` are recorded with their sha256s. Any routing sidecars (`net_class_map.json`, `*constraints*.json`) are recorded but not used by baseline rows.
- **Rules.** For boards 00–07 the manufacturer rule set is the `<stem>_routed.kicad_dru` beside the committed routed reference. This is kct-managed JLCPCB fab-floor content (`# BEGIN kct fab floors`), not router tuning. Board 09 has none, so its manifest entry is marked `"provenance": "absent"` and it is graded on `.kicad_pro` alone. No DRU is synthesized.
- **Exclusions.** Board 08 has no committed `.kicad_pcb`. It is an explicit `excluded` entry, and its rows stay in the table as `unsupported`.
- **Changing inputs.** If a fleet input changes, re-freeze deliberately with `run.py freeze` and commit the manifest. `tests/test_fleet_router_protocol.py` fails until you do.
- **Budgets.** `wall_cap_s` 600, `threads` 1, `seed` 1, and `tracemaker_work` 5e7 (TraceMaker's deterministic work budget). The thread count is passed to every tool's thread flags. fastroute's `--multi-start` stays at its upstream default.
- **Environment.** Each `results.json` records the kct revision and dirty flag, the native backend probe, the kicad-cli version, the host, Python, the pins, the manifest sha256, and every executable's sha256.

## Preparation: one stripping policy

The policy is `strip-tracks-and-vias`, applied identically to every board. It removes every **top-level** `segment`, `arc` and `via` form. Everything else is kept byte for byte: footprints, pads and pad nets, outline, zones, rule areas, layer stack and setup. The scan is depth-aware, so an `(arc ...)` inside a zone outline is never touched. Boards 00–05 carry no copper, so their prepared input is byte-identical to the committed file. Boards 06, 07 and 09 arrive (partly) pre-routed and are stripped. Preserved-copper, fanout and length-tuning experiments are separate declared variants and are not part of these baseline rows.

## Adapters

| Tool | Command (verified at the pin) | Notes |
|---|---|---|
| kct | `kct route IN -o OUT --backend cpp --seed 1` | Uses the worktree venv's `kct`, and refuses to run unless the C++ backend is available from this worktree. |
| TraceMaker | `tracemaker route IN -o OUT --work N --seed 1 --threads 1 --no-gpu --no-kb --json summary.json` | `--no-kb` isolates learned state: the cross-run knowledge base is neither read nor written. The flags were read from `src/app/main.cpp` at the pin. |
| fastroute | `fastroute -de IN.dsn -do OUT.ses --router.autorouter.max_threads=1 --router.optimizer.max_threads=1 --report=...` | The DSN comes from `KiCadToDSNExporter`. The SES is merged back into the prepared input with `SESToKiCadImporter`, so the original board context (placement, pads, zones) is the merge base. |

Each run is placed in its own process group under the wall cap. On timeout the whole group gets SIGTERM, then SIGKILL, and the driver waits until it is gone (the helpers are reused from `scripts/research/krt_compare.py`).

## Outcomes

Exactly one per row:

| Outcome | Meaning |
|---|---|
| `success` | Ran and was scored. The fixed placement held and no rule loss is known. |
| `non-comparable` | Ran and was scored, but the router saw a lossy view of the rules. `lost_constraints` lists each one. |
| `invalid-fixed-placement` | The output moved, rotated or flipped a footprint, changed a pad net, the outline or the copper layers, or removed or changed an input zone. The baseline is the prepared input outside the router's run directory, and the router's own working copy must still hash to it after the run; otherwise the row is `invalid-fixed-placement` with reason "router modified its input". Zones **added** by a router (kct pours GND/VCC) are allowed and recorded in `zones_added_by_tool`. |
| `timeout` | Killed at the cap. A partial board or session is kept as `partial_artifact` and is **never** scored as a result. |
| `tool-failure` | The pin mismatched, the build failed, the executable is missing, the native backend is missing, or the tool exited non-zero without output. `stage` says which. |
| `missing-output` | Exit 0, but no fresh output was written. |
| `scorer-failure` | Our side failed: DSN export, SES import, fingerprinting, or the headline kicad-cli DRC was unavailable. |
| `unsupported` | The board is excluded or the tool was not configured. |
| `reference` | The normalized input, scored for pre-existing errors. |

## Scoring

Every output is copied into a **fresh** grading directory beside copies of the frozen `.kicad_pro` and `.kicad_dru`. The sha256s of the rules actually graded against are recorded. A `.kicad_pro` or `.kicad_dru` emitted by the router is never used. The checks reported for each row are:

- **DRC:** `kicad-cli pcb drc --refill-zones --severity-all --all-track-errors --save-board`. Total error-level violations, warnings and unconnected items are reported separately. Pre-existing errors are not subtracted; the `reference` row shows them.
- **Completion, vias and wirelength:** measured independently on the refilled board (`kicad_tools.benchmark.external.metrics`), never taken from a router's log.
- **Manufacturing:** the `kct check` engine with the JLCPCB profile and the board's copper layer count.
- **Sync:** `kct validate --sync` against the frozen schematic.
- **`clean_pass`:** `true` only if every referee ran and found nothing, and completion is 100%. It is `false` as soon as any referee reports a problem. It is `null` (unavailable) if a referee could not run. A referee that did not run is never counted as zero.

## DSN comparability audit (fastroute)

`dsn_audit` exports the DSN and compares what it **emitted** against what the board, the frozen project and the frozen DRU declare. It flags:

- net classes missing from the DSN;
- class values exported differently, whether relaxed or tightened, read from each class's own `(class ...)` block;
- net-class assignments the DSN's class membership does not reproduce;
- project board rules (edge clearance, stricter floors);
- DRU constraints the DSN's global values do not already imply;
- rule areas whose track/via restriction has no matching `keepout`/`wire_keepout`/`via_keepout` on each of their copper layers;
- copper zones with no matching `(plane ...)` for their net on each of their layers;
- copper-layer text;
- copper layers, pads or nets missing from the export.

`ses_import` then counts the wires and vias in the raw session, compares that with what the importer kept, and checks the session's resolution. Any loss makes the row `non-comparable`, and the concrete constraints are listed in the row.

Findings at the build revision (issues filed separately; out of scope here):

- **#6593 (closed):** the SES importer dropped every wire from a standard Freerouting/fastroute session, because nets nest the wires under `network_out`. It also ignored `(resolution um 10)`.
- **#6594 (closed):** the DSN exporter read neither the `.kicad_pro` net classes nor the `.kicad_dru`, and emitted no zones or keepouts. On board 09, whose pad nets use the KiCad-10 name-only `(net "X")` form, it exported **no nets or pads at all**.
- **#6765 (open):** the DSN exporter still does not represent the JLCPCB fab-floor `.kicad_dru` rules. Until it does, a fastroute row stays `non-comparable` on any board whose `.kicad_dru` carries them (10 losses on board 00).
