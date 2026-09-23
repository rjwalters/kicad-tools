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
| `install-kct.sh` | Install kicad-tools into a consumer PCB-design repo: uv dependency + vendored `ci/` gate scripts + `.kct/CONVENTIONS.md`, plus per-client workflow content selected via `--client claude\|codex\|both` (default `claude`, backward-compatible) — Claude gets `.claude/commands/kct/` skills + a guarded `CLAUDE.md` block, Codex gets `.agents/skills/kct-<name>/SKILL.md` + a guarded `AGENTS.md` block, both generated from the same source `.md` files (#4905). Codex generation adapts sibling paths and skill invocations, and includes a namespace README beside `kct-help`. |
| `audit_machine_output.py` | Audit the `--format json` machine-output idiom across every CLI leaf subcommand (walks the real argparse tree); the measurement tool behind `docs/reference/machine-output.md` (#4543) and the #4674 sweep backlog. `--markdown` emits the doc tables. |
| `changelog_gap_report.py` | Release gate: list user-visible commits since a `v*` tag whose issue number is not cited in the CHANGELOG's `[Unreleased]` section; exits non-zero when the gap set is non-empty. Invoked by `RELEASING.md` step (0) (#4638). |
| `check_fill_fragment_bonding_vs_native.py` | Re-derive the #5362 measurement: emits `tests/test_fill_fragment_bonding_5362.py`'s boards, DRCs each with native `kicad-cli` inside `kicad/kicad:10.0` (no refill, hash-checked), and prints native `unconnected_items` beside this repo's net-status / copper-partition verdicts. Non-zero exit on any disagreement. |
| `check_trace_vs_zone_fills.py` | Verify track segments against foreign-net zone fill copper (clearance/short check DRC cannot yet do; #3527). |
| `replay_pairwise_gate.py` | Replay the router's own pairwise (HV-isolation) gate over a routed board. |
| `route_chorus.py` | Canonical chorus-test-revA routing recipe runner with partial-net rescue (#3474). |
| `verify-site-deployment.sh` | Standalone drift check: does the live kicad-tools.org deployment match the built site artifacts (uses `lib/site-verify.sh`). |
| `regen_net_status_why_golden.py` | Regenerate the `net-status --why` golden fixtures (#5521). |
| `routing_plan_fleet_table.py` | Fleet precision/recall table for the routing plan (#5521). |

## Subdirectories

### `ci/`

Gate scripts invoked by GitHub Actions (and vendored into consumer repos by
`install-kct.sh`). They take a board path as a CLI argument and gate the build
on copper-LVS, routed-DRC, diff-pair / match-group coverage, board-specific
end-to-end checks, the mypy baseline, and route determinism:

`analyze_native_observations.py`, `board06_determinism_smoke.sh`,
`board_recipe_artifacts.py`, `board_route_determinism_smoke.sh`, `check-
content-contracts.sh`, `check_board_00_e2e.py`,
`check_board_05_blocking.py`, `check_copper_lvs.py`,
`check_diffpair_coverage.py`, `check_mask_copper_native.py`,
`check_matchgroup_coverage.py`, `check_mypy_baseline.py`,
`check_net_status.py`, `check_routed_drc.py`,
`collect_failed_routing_bundle.py`, `init_kicad_libraries.py`, `local-
gate.sh`, `native_observer.md`, `native_observer.py`,
`native_observer_pytest.py`, `net_class_map_resolver.py`,
`normalize_copper.py`, `run_native_routed_drc.sh`, `run_observed.py`,
`select_routed_pcbs.py`, `verify_changes_filter.mjs`.
`normalize_copper.py` is the routed-copper normalizer
`board_route_determinism_smoke.sh` compares runs with: it reduces a
`.kicad_pcb` to the sorted multiset of whole, paren-balanced
`(segment ...)` / `(via ...)` / `(arc ...)` nodes (`uuid`/`tstamp` stripped,
pour fills excluded), one record per line. It replaced a line-based `grep`
that kept only the multi-line nodes' header lines and so compared element
*counts* rather than geometry (#5580).

`check_mask_copper_native.py` is the repository's mandatory native mask-to-copper
suite gate. It probes matching KiCad 10.0.5 CLI/pcbnew and Gerbonara >=1.6.3,
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

The pure helpers `corpus/parse_taxonomy.py` and `corpus/corpus_manifest.py` (no
network, no CLI) are unit-tested by `tests/test_corpus_manifest.py`;
`corpus/omnieda_sample.py` (offline) by `tests/test_corpus_omnieda.py`;
`corpus/benchmark_readiness.py` (offline) by `tests/test_corpus_readiness.py`.

### `lib/`

Shell helpers sourced by the top-level scripts: `site-verify.sh` is the
deployed-artifact verification library behind `verify-site-deployment.sh`.

### `research/`

FOM-calibration and corpus-generation scripts (pair with `data/research/`),
plus standalone router benchmark / profiling scripts (`bench_*.py`,
`profile_*.py`) that back measured performance PRs (#5240, #5617):

`bench_python_astar_via_kernel.py`, `bench_rtree_ripup.py`,
`bench_stitch_fill_predicates.py`, `bench_stitch_track_index.py`,
`calibrate_fom.py`, `check_negatives.py`, `demo_integration.py`,
`generate_negative_controls.py`, `generate_perturbations.py`,
`profile_board06_rtree_deletes.py`, `run_phase0_corpus.sh`,
`run_phase0_fast_corpus.sh`, `train_phase0_classifier.py`.
