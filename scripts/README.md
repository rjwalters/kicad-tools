# scripts/

Developer and CI helper scripts. These are **not** part of the installed `kct`
package — they support building, deploying, diagnosing, and gating the project.

Run shell scripts with `bash scripts/<name>.sh` (or `./scripts/<name>.sh`) and
Python helpers with `uv run python scripts/<name>.py`, from the repository root.

## Top-level scripts

| Script | Purpose |
|--------|---------|
| `build-cpp.sh` | Build/install the nanobind C++ router extension (`clean`, `check` subcommands). Wrapped by `kct build-native`. Requires the `native` extra (`uv sync --extra native` / `pip install "kicad-tools[native]"`) so nanobind stays lockfile-tracked; the default dev group already composes it in. |
| `deploy-site.sh` | Manual one-command deploy of the kicad-tools.org demo gallery to Cloudflare Pages (via a locally authenticated `wrangler`). |
| `install-kct.sh` | Install kicad-tools into a consumer PCB-design repo: dependency (PyPI `kicad-tools[...]>=<version>` by default, #8077 — `--tag`/`--rev`/`--path` are escape hatches; `--upgrade-workflow` vendors a weekly upgrade-PR workflow from `templates/kct-upgrade.yml`; the pyproject edit is `lib/install_kct_pypi_pin.py`) + vendored `ci/` gate scripts + `.kct/CONVENTIONS.md`, plus per-client workflow content selected via `--client claude\|codex\|both` (default `claude`, backward-compatible) — Claude gets `.claude/commands/kct/` skills + a guarded `CLAUDE.md` block, Codex gets `.agents/skills/kct-<name>/SKILL.md` + a guarded `AGENTS.md` block, both generated from the same source `.md` files (#4905). Codex generation adapts sibling paths and skill invocations, and includes a namespace README beside `kct-help`. |
| `audit_machine_output.py` | Audit the `--format json` machine-output idiom across every CLI leaf subcommand (walks the real argparse tree); the measurement tool behind `docs/reference/machine-output.md` (#4543) and the #4674 sweep backlog. `--markdown` emits the doc tables. |
| `changelog_gap_report.py` | Release gate: list user-visible commits since a `v*` tag whose issue number is not cited in the CHANGELOG's `[Unreleased]` section; exits non-zero when the gap set is non-empty. Invoked by `RELEASING.md` step (0) (#4638). |
| `check_fill_fragment_bonding_vs_native.py` | Re-derive the #5362 measurement: emits `tests/test_fill_fragment_bonding_5362.py`'s boards, DRCs each with native `kicad-cli` inside `kicad/kicad:10.0` (no refill, hash-checked), and prints native `unconnected_items` beside this repo's net-status / copper-partition verdicts. Non-zero exit on any disagreement. |
| `check_trace_vs_zone_fills.py` | Verify track segments against foreign-net zone fill copper (clearance/short check DRC cannot yet do; #3527). |
| `replay_pairwise_gate.py` | Replay the router's own pairwise (HV-isolation) gate over a routed board. |
| `route_chorus.py` | Canonical chorus-test-revA routing recipe runner with partial-net rescue (#3474). |
| `verify-site-deployment.sh` | Standalone drift check: does the live kicad-tools.org deployment match the built site artifacts (uses `lib/site-verify.sh`). |
| `regen_net_status_why_golden.py` | Regenerate the `net-status --why` golden fixtures (#5521). |
| `routing_plan_fleet_table.py` | Fleet precision/recall table for the routing plan (#5521). |
| `changelog_assemble.py` | Assemble `changelog.d/` fragments into a versioned CHANGELOG section (#5775). |
| `ecosystem_refresh.py` | Report drift between the ecosystem registry (`src/kicad_tools/ecosystem/data/projects.toml`) and upstream reality; prints the TOML a human can apply and never writes the registry (#5839). |
| `ecosystem_render.py` | Render the README's Related Projects block from the ecosystem registry; `--check` (the default) fails when the committed block differs from a fresh render, `--write` re-renders it (#5839). |
| `ecosystem_reporecon_diff.py` | List RepoRecon repos missing from the ecosystem registry; reports only, never edits `projects.toml` (#6749). |
| `measure_bundle_crossings.py` | Read-only fleet measurement for the bundle crossing report: hits, false alarms and misses against each committed routed board's unfinished nets (#6339). |
| `release_plan.py` | Plan, gate and open the daily automated release PR; run by `.github/workflows/release-pr.yml` (#5776). |
| `release_tag.py` | Tag a merged release, then create its GitHub Release once PyPI has it; called by `.github/workflows/release-tag.yml` (#5777). |
| `render_agent_guidance.py` | Render the shared contributor guidance (`docs/contributing/agent-guidance.md`) into the marked blocks of `CLAUDE.md` and `AGENTS.md` (#5964). |
| `sync_agent_skills.py` | Keep `.claude/commands/kct/` a byte-identical copy of the packaged skills in `src/kicad_tools/agent_skills/kct/` (`--write` after editing the package data; #5950). |
| `update_3dmodel_manifest.py` | Regenerate `src/kicad_tools/data/kicad-3dmodels/manifest.json` from the stock 3D models that boards and package data reference; needs network (#7683). |

## Subdirectories

### `boards/`

Producers for committed demo-board evidence that `kct readiness` cannot regenerate,
because the package is recipe-finalised or the report records board-specific checks.

| Script | Purpose |
|--------|---------|
| `refresh_readiness.py` | Gated re-pin of `readiness.json` for boards 05/06/07. Works on a staged copy: re-runs native ERC, rebuilds `kicad_project.zip` / `design-source.zip` / `manufacturing.zip` deterministically (sorted entries, fixed timestamps), re-hashes `manifest.json`, then runs `kicad-cli pcb drc --refill-zones`, `kct check --mfr`, `kct net-status` and `kct validate --sync`. It publishes nothing if a gate fails or a measured metric differs from the report (#6076). |
| `regenerate_uuids_6076.py` | One-shot deterministic-UUID regeneration of boards 05/06/07 design files. Regenerates the schematic and unrouted PCB with each board's generator, and remaps the routed PCB's generator-sourced UUIDs without re-routing. Evidence is left to `refresh_readiness.py` (#6076). |
| `regenerate_boards_03_04_6076.py` | Deterministic-UUID regeneration of board 03 plus its evidence, and the routed-PCB UUID remap (`remap_routed`). Reuses the two scripts above' helpers. Board 03 re-runs its own recipe, `check_manufacturing.py` and `kct readiness --verify`. Board 04 is no longer regenerated here: `refresh_bundles_6404.py` refreshes and re-pins it (#6076, #7357). |
| `refresh_bundles_6404.py` | Refill, re-plot and re-pin a board's hash-bound bundle on a staged copy (boards 04 and 07), publishing nothing unless every gate passes; the tail is `refresh_readiness.repin_staged`. Board 04 re-plots only the copper and F.Silkscreen Gerbers, keeps its `check_manufacturing.py` gate, and captures the recipe's `run_drc` into `output/drc_report.json`; it replaces the former `regenerate_board_04_6269.py` / `regenerate_board_04_6404.py` (#6404, #7353, #7357). |
| `generate_local_3d_bodies_7685.py` | Writes the project-local VRML bodies (board 03 J2; board 09 J1, RSH1, L1) whose STEP files do not exist upstream, plus each board's `output/3dshapes/local_models.json`, and attaches them with `kicad_tools.pcb.models3d.attach_local_models`. Generated from nominal envelopes, so they are ours to commit; idempotent (#7685). |
| `render_modelgen_evidence_7687.py` | Generates `kct models generate` STEP bodies for board 03 J2 and board 09 RSH1/L1 on one-footprint scratch boards and renders their top views on the pinned evidence toolchain into `docs/guides/images/parametric-3d-models/`. Needs the `[models]` extra and `KCT_KICAD_DOCKER=1`; leaves the committed boards untouched (#7687). |
| `annotate_pintypes_6025.py` | Carry schematic pad pin types onto boards 04-07's committed PCBs without re-routing (#6025). |
| `declare_router_netclasses_6390.py` | Declare the router's net classes in the committed routed sidecars (`.kicad_pro` / `.kicad_dru`) without re-routing; `--refill` also recomputes the saved zone fills (#6390). |
| `native_refill_board_04_7337.py` | Re-save board 04's zone fills as kicad-cli's own refill computes them (#7337). |
| `refill_thermal_ladder_7295.py` | Re-fill boards 00/01/04 under the per-pad thermal-spoke ladder (#7295). |
| `refresh_copper_gerbers_6404.py` | Replace only the copper-layer Gerbers of a committed bundle's `gerbers.zip`; every other member keeps its committed bytes (#6404). |
| `regenerate_board_00_6076.py` | Deterministic-UUID regeneration of board 00-simple-led, with its package (#6076). |
| `_bundle_links.py` | Helper module: keeps relative markdown links valid when a board file is copied into a bundle (#6899). |
| `_cpl_frame.py` | Helper module: expected CPL coordinates in the Gerber/drill frame, computed independently of `kct export` (#7199). |

### `ci/`

Gate scripts invoked by GitHub Actions (and vendored into consumer repos by
`install-kct.sh`). They take a board path as a CLI argument and gate the build
on copper-LVS, routed-DRC, diff-pair / match-group coverage, board-specific
end-to-end checks, the mypy baseline, and route determinism:

`analyze_native_observations.py`, `board06_determinism_smoke.sh`,
`board_recipe_artifacts.py`, `board_route_determinism_smoke.sh`,
`check-content-contracts.sh`, `check_board_00_e2e.py`,
`check_board_05_blocking.py`, `check_copper_lvs.py`,
`check_diffpair_coverage.py`, `check_mask_copper_native.py`,
`check_matchgroup_coverage.py`, `check_mypy_baseline.py`,
`check_net_status.py`, `check_routed_drc.py`,
`collect_failed_routing_bundle.py`, `init_kicad_libraries.py`, `local-gate.sh`,
`native_observer.md`, `native_observer.py`, `native_observer_pytest.py`,
`net_class_map_resolver.py`, `normalize_copper.py`, `run_native_routed_drc.sh`,
`run_observed.py`, `select_routed_pcbs.py`, `verify_changes_filter.mjs`.
Also here: `board_05_unattended.py`, `check_board07_diffpair_baseline.py`,
`check_copper_safety_drc.py`, `check_pr_checks_present.py`,
`check_routed_drc_internal.py`, `compare_model_coverage.py`,
`identity_allowance.py`, `model_coverage_report.py`, `site_drift.py`
(warn-only drift check of the live kicad-tools.org against this repo, #7664),
`update_ci_extended.py` (regenerates `tests/ci_extended.txt` from measured
test times, #6013).
`normalize_copper.py` is the routed-copper normalizer
`board_route_determinism_smoke.sh` compares runs with: it reduces a
`.kicad_pcb` to the sorted multiset of whole, paren-balanced
`(segment ...)` / `(via ...)` / `(arc ...)` nodes (`uuid`/`tstamp` stripped,
pour fills excluded), one record per line. It replaced a line-based `grep`
that kept only the multi-line nodes' header lines and so compared element
*counts* rather than geometry (#5580).

`check_mask_copper_native.py` is the repository's mandatory native mask-to-copper
suite gate. It probes a matching CLI/pcbnew pair at one of the KiCad versions
qualified in `validate/mask_copper_geometry.py`
(`QUALIFIED_NATIVE_VERSIONS`, imported rather than restated) and Gerbonara >=1.6.3,
runs every case in `tests/test_mask_copper_native.py`, and rejects failures,
empty results, missing material witnesses, or any skips. For a local equivalent
in a KiCad environment with shared scratch paths:

```bash
KCT_MASK_NATIVE_PYTHON='["/usr/bin/python3"]' uv run --frozen python scripts/ci/check_mask_copper_native.py --artifacts /tmp/mask-copper-native
```

Each invocation retains a new run directory containing prerequisite details,
pytest output, JUnit, and generated native artifacts. Ordinary local pytest
execution keeps its optional-prerequisite skips. CI runs this dedicated gate
once and excludes the file from the later general suite.

### `corpus/`

Opt-in, **local-only** tooling for external KiCad corpora (network I/O; never
invoked from CI; the network scripts are never imported by `tests/`). See
[`corpus/README.md`](corpus/README.md) for flags, output layout, the failure
taxonomy, the manifest schema, and the CC-BY-4.0 attribution requirement.

`probe_open_schematics.py` — sample N random records from the Hugging Face
`bshada/open-schematics` dataset (87,931 real KiCad designs, CC-BY-4.0), parse
them with this repo's own `Schematic.load` / `PCB.load`, and report a parser
failure taxonomy (JSON + human-readable) under the gitignored
`scripts/corpus/.cache/` (#4830):

```bash
uv run python scripts/corpus/probe_open_schematics.py --n 50 --seed 1234
uv run python scripts/corpus/probe_open_schematics.py --dry-run   # offline path
```

`check_manifest.py` / `build_manifest.py` — score the parsers against the
committed curated sample (`corpus/manifests/open-schematics-sample.json`: 30
real-world artifacts pinned by URL + `sha256`, **no payloads committed**), or
rebuild that manifest from a seeded, stratified scan:

```bash
uv run python scripts/corpus/check_manifest.py            # fetch (cold) + score
uv run python scripts/corpus/check_manifest.py --offline  # cache only, ~8 s
uv run python scripts/corpus/build_manifest.py --n 30 --scan 90 --seed 4830
```

`omnieda_sample.py` — **offline**; characterize a manually-downloaded
OmniLayout / OmniRouting sample (Eagle-derived JSON, *not* KiCad files) and
census the fields that have no `schema.pcb` counterpart. Download steps, the
measured numbers, and the adopt/adapt/drop verdict live in
[`docs/research/omnilayout-recon.md`](../docs/research/omnilayout-recon.md):

```bash
uv run python scripts/corpus/omnieda_sample.py --sample /path/to/extracted --out /tmp/omni
```

`benchmark_readiness.py` — **offline**; score the cached corpus boards (and any
local `.kicad_pcb`) for whether they can serve as capacity-predictor
calibration examples (#4799) or route-vs-human benchmark cases, with one
blocker code per distinct cause. Measured verdicts and the three pilot routes
are in
[`docs/research/corpus-benchmark-feasibility.md`](../docs/research/corpus-benchmark-feasibility.md):

```bash
uv run python scripts/corpus/check_manifest.py        # populate the cache once
uv run python scripts/corpus/benchmark_readiness.py   # census, ~40 s, offline
uv run python scripts/corpus/benchmark_readiness.py --no-manifest \
  --board boards/03-usb-joystick/output/usb_joystick_routed.kicad_pcb
```

`corpus/hf_hub.py` is the shared Hugging Face dataset access module (retry/backoff)
behind the probe, the manifest builder and the manifest runner (#4830).

The pure helpers `corpus/parse_taxonomy.py` and `corpus/corpus_manifest.py` (no
network, no CLI) are unit-tested by `tests/test_corpus_manifest.py`;
`corpus/omnieda_sample.py` (offline) by `tests/test_corpus_omnieda.py`;
`corpus/benchmark_readiness.py` (offline) by `tests/test_corpus_readiness.py`.

### `lib/`

Shell helpers sourced by the top-level scripts: `site-verify.sh` is the
deployed-artifact verification library behind `verify-site-deployment.sh`.
`install_kct_pypi_pin.py` is the stdlib-only `pyproject.toml` editor behind
`install-kct.sh`'s default PyPI mode (#8077): it writes the
`kicad-tools[...]>=<version>` floor and removes the `[tool.uv.sources]` entry.

### `templates/`

Files `install-kct.sh` vendors into consumer repos verbatim:
`kct-upgrade.yml` is the opt-in weekly upgrade-PR workflow
(`install-kct.sh --upgrade-workflow`, #8077).

### `research/`

FOM-calibration and corpus-generation scripts (pair with `data/research/`),
plus standalone router benchmark / profiling scripts (`bench_*.py`,
`profile_*.py`) that back measured performance PRs (#5240, #5617):

`bench_python_astar_via_kernel.py`, `bench_rtree_ripup.py`,
`bench_stitch_fill_predicates.py`, `bench_stitch_track_index.py`,
`calibrate_fom.py`, `check_negatives.py`, `demo_integration.py`,
`diffpair_phase_skew_table.py` (#7883, write-up in
[`docs/research/diffpair-phase-skew-tolerance.md`](../docs/research/diffpair-phase-skew-tolerance.md)),
`generate_negative_controls.py`, `generate_perturbations.py`,
`placement_score_calibration.py` (#5948, write-up in
[`docs/research/placement_score_calibration.md`](../docs/research/placement_score_calibration.md)),
`profile_board06_rtree_deletes.py`, `run_phase0_corpus.sh`,
`run_phase0_fast_corpus.sh`, `train_phase0_classifier.py`.

Also here: `bench_collision_obstacle_scan.py`, `bench_sexp_traversal.py`,
`diffpair_bench.py` (#6442, writes `data/research/diffpair_bench/`),
`embedded_checksum_probe.py`, `escape_flow_experiment.py`,
`expand_route_experiment.py`, `fom_decoupling_ab.py`, `krt_compare.py`,
`measure_3d_catalog_coverage.py`, `net_order_experiment.py`,
`nogood_cache_experiment.py`, `profile_route_obstacle_scan.py`,
`ripup_experiment.py`, `route_phase_profile.py`, `shrink_experiment.py`.
