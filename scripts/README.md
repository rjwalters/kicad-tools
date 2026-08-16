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
| `install-kct.sh` | Install kicad-tools into a consumer PCB-design repo (uv dependency + vendored `.claude/commands/kct/` skills and `ci/` gate scripts). |
| `audit_machine_output.py` | Audit the `--format json` machine-output idiom across every CLI leaf subcommand (walks the real argparse tree); the measurement tool behind `docs/reference/machine-output.md` (#4543) and the #4674 sweep backlog. `--markdown` emits the doc tables. |
| `changelog_gap_report.py` | Release gate: list user-visible commits since a `v*` tag whose issue number is not cited in the CHANGELOG's `[Unreleased]` section; exits non-zero when the gap set is non-empty. Invoked by `RELEASING.md` step (0) (#4638). |
| `check_trace_vs_zone_fills.py` | Verify track segments against foreign-net zone fill copper (clearance/short check DRC cannot yet do; #3527). |
| `route_chorus.py` | Canonical chorus-test-revA routing recipe runner with partial-net rescue (#3474). |

## Subdirectories

### `ci/`

Gate scripts invoked by GitHub Actions (and vendored into consumer repos by
`install-kct.sh`). They take a board path as a CLI argument and gate the build
on copper-LVS, routed-DRC, diff-pair / match-group coverage, board-specific
end-to-end checks, the mypy baseline, and route determinism:

`board06_determinism_smoke.sh`, `board_route_determinism_smoke.sh`,
`check_board_00_e2e.py`, `check_board_05_blocking.py`, `check_copper_lvs.py`,
`check_diffpair_coverage.py`, `check_matchgroup_coverage.py`,
`check_mypy_baseline.py`, `check_net_status.py`, `check_routed_drc.py`,
`net_class_map_resolver.py`.

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

### `research/`

FOM-calibration and corpus-generation scripts (pair with `data/research/`):

`calibrate_fom.py`, `check_negatives.py`, `demo_integration.py`,
`generate_negative_controls.py`, `generate_perturbations.py`,
`run_phase0_corpus.sh`, `run_phase0_fast_corpus.sh`,
`train_phase0_classifier.py`.
