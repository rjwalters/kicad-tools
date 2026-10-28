---
name: manufacturing-readiness
invocation: /kct:manufacturing-readiness
suggestedModel: sonnet
description: Sign off a routed board for fabrication — run kct check, the mandatory kicad-cli pcb drc --refill-zones cross-gate, and a kct export bundle at the board's fab tier — and refuse sign-off if any gate is skipped.
---

# Manufacturing readiness

Sign off a routed `.kicad_pcb` for fabrication. This skill codifies the **sign-off ritual**: `kct check` at the board's fab tier, the **mandatory** independent cross-gate `kicad-cli pcb drc --refill-zones`, and a `kct export` manufacturing bundle — then confirms a `manifest.json` was produced. A clean `kct check` **alone is not sign-off**.

> **The `kct` namespace.** This skill lives in `{{skills-dir}}` — the kicad-tools-native, harness-agnostic agent-tool namespace, invoked as `{{skill:manufacturing-readiness}}`. It runs from inside a **consumer repo** that depends on kicad-tools as a `uv` dependency (`kct ...` is on `PATH` via the venv). It does **not** assume the current directory is the kicad-tools repo, and it hardcodes no board path, no fab-tier list, and no CI-workflow context.

## Prerequisite

The native router/DRC backend must be built in the active checkout before this skill is meaningful (Epic #4054 convention #1). Run once per checkout / worktree:

```bash
uv run kct build-native --check   # expect: "C++ backend: available"
# if "not installed":
uv run kct build-native
```

`uv sync` does **not** build the native extension. A fresh checkout or git worktree needs this step explicitly.

The gates also need `jq` (Gates 1, 2 and 4 judge JSON reports with it) and a KiCad install for `kicad-cli` (Gate 2 resolves it itself; see there). A missing `jq` means the gates cannot be judged — refuse sign-off rather than eyeballing the JSON.

## Model selection

`suggestedModel: sonnet`. This is a deterministic checklist-execution task — run three gates in order, read their exit codes and reports, and refuse sign-off on any failure. It does not require frontier judgment. Model resolves through the harness's normal precedence chain (explicit dispatch param → harness role config → this doc's frontmatter `suggestedModel` → session default).

## Arguments

**Arguments**: `$ARGUMENTS`

`$ARGUMENTS` is `<board-path> [--mfr <tier>] [--output <dir>]`.

| Token | Meaning |
|-------|---------|
| `<board-path>` | **Required.** Path to the routed `*.kicad_pcb` to sign off (or a board directory containing one). Everything the skill operates on is derived from this token — never from a hardcoded board directory. The user supplies it. |
| `--mfr <tier>` | The fabrication tier to check and export against. **Optional**; if omitted, discover it (see "Resolving the fab tier" below) rather than assuming a default. |
| `--output <dir>` | Where the export bundle is written. Optional; defaults to `<pcb-dir>/manufacturing/` (the `kct export` default). |

## Resolving the fab tier (never hardcode a tier list)

Do **not** embed a fixed list of tier names in your reasoning — the set of tiers lives in `kicad_tools.manufacturers` and grows without any edit to this skill. Resolve `<tier>` in this order:

1. If the user passed `--mfr <tier>`, use it.
2. Otherwise read the tier from the **board's own recipe / manifest** — a `generate_design.py`, `README.md`, or existing `manufacturing/manifest.json` next to the board usually names its target fab. Use that.
3. Otherwise ask the user which tier to target.

Then **validate** the resolved tier against `kct`'s own registry rather than a memorized list:

```bash
# The authoritative choices — sourced from kicad_tools.manufacturers.get_manufacturer_ids():
kct check --help      # see the --mfr {choices} set
kct export --help     # same registry, plus the "generic" export-only pseudo-tier
# or, programmatically:
uv run python -c "from kicad_tools.manufacturers import get_manufacturer_ids; print(get_manufacturer_ids())"
```

If the resolved tier is not in that set, stop and ask — do not silently fall back to a default, because the fab tier is load-bearing (in-pad-via rescue rules, min trace/space floors, etc. differ by tier).

## The sign-off ritual (run all three, in order)

Each gate below is **one bash block, run as-is in a single tool call** (each tool call is a fresh shell, so nothing carries over between blocks). Each block starts with a `# sign-off gate N` marker line, and **the block's exit code is the gate verdict**: 0 passes, anything else refuses. Do not judge a gate by the exit code of an individual command inside it, and do not add flags the block does not show.

Placeholders, all derived from `<board-path>`:

| Placeholder | Meaning |
|-------------|---------|
| `<board.kicad_pcb>` | The routed board file to sign off. |
| `<pcb-dir>` | The directory that holds `<board.kicad_pcb>`. |
| `<tier>` | The resolved, validated fab tier (above). |
| `<dir>` | The export bundle directory: `--output <dir>` if the user passed it, else `<pcb-dir>/manufacturing`. |

The order matches `kct readiness` and is deliberate: check, then the native DRC, then export. Do not reorder (#4096/#4097).

### Gate 1 — `kct check` at the fab tier, judged from its JSON report

```bash
# sign-off gate 1
mkdir -p <dir> && rm -f <dir>/check-report.json
kct check <board.kicad_pcb> --mfr <tier> --output <dir>/check-report.json
jq -e '(.summary.errors == 0)
  and ((.summary.stale_waivers // 0) == 0)
  and (((.summary.coverage_blocking_unknown // []) | length) == 0)
  and .meta_checks.drc.status == "PASSED"
  and .meta_checks.erc.status == "PASSED"
  and .meta_checks.lvs.status == "PASSED"
  and (.meta_checks.lvs.copper_vacuous != true)
  and ((.meta_checks.lvs.copper_bound_pad_count // 1) != 0)
  and ((((.meta_checks.lvs.mismatches // []) + (.meta_checks.lvs.copper_mismatches // [])) | length) == 0)
  and (.meta_checks.manifest.status == "NOT RUN" or .meta_checks.manifest.status == "PASSED")' \
  <dir>/check-report.json
```

- This is the DRC / manufacturing-rules check (clearances, dimensions, edge, silkscreen) plus the ERC / LVS / Manifest meta sub-checks.
- **`kct check` exits 2 on a good board here, and that is expected.** Gate 1 runs before the export, so the Manifest sub-check is always `NOT RUN` and the overall line reads `INCOMPLETE`. The verdict is the `jq` rule, which mirrors `kct readiness`'s `kct_check` + `lvs` gates:
  - 0 errors, 0 stale waivers, and no blocking check whose coverage is `unknown` ("unknown is not pass").
  - DRC, ERC and LVS sub-checks all `PASSED`, and LVS non-vacuous (it compared real copper and found no mismatches).
  - **Any `NOT RUN` other than Manifest is a refusal.** ERC or LVS `NOT RUN` (no schematic found, kicad-cli missing) means the board was never checked against its design intent.
  - Manifest `NOT RUN` is the only deferral, and it is deferred to Gate 3, which must write one.
- **Never pass `--allow-incomplete` or `--drc-only` for sign-off.** `--allow-incomplete` hides ERC/LVS `NOT RUN` from the exit code; that is exactly how a schematic-less board was signed off by hand (#7136).
- **`--net-class-map` is auto-discovered** — `kct check` probes `<pcb_dir>/net_class_map.json`, `<pcb_dir>/output/net_class_map.json`, and `<pcb_dir>/../output/net_class_map.json` (the sidecar `kct route` itself writes). Only if the sidecar lives somewhere non-conventional, add `--net-class-map <path/to/net_class_map.json>` to the `kct check` line; that is the one permitted addition.
- **Quote the per-rule warning breakdown, not the aggregate.** `kct check`'s table output prints a per-rule `BY RULE:` breakdown of warnings; the sign-off verdict must quote that breakdown (rule → warning count) rather than a single aggregate total. A warning **baseline is only meaningful per rule**: "N warnings, non-regressive vs. the baseline" can be satisfied by an aggregate while an entire defect class (e.g. silkscreen over exposed pads — a real assembly risk) hides inside it across repeated sign-offs. When recording or re-verifying a warning baseline, compare counts rule-by-rule. The JSON report's `violations[]` carry a `rule_id` and severity per finding, so per-rule counts are a one-line `jq` derivation. Warnings do not affect the `jq` rule, which is exactly why the per-rule quote is mandatory.
- A passing Gate 1 is necessary but **not sufficient**. Proceed to Gate 2 regardless.

### Gate 2 — the mandatory independent cross-gate (NOT optional)

```bash
# sign-off gate 2
KICAD_CLI=$(uv run python -c "from kicad_tools.cli.runner import find_kicad_cli; print(find_kicad_cli() or '')")
test -n "$KICAD_CLI" || { echo "kicad-cli not found (run: kct doctor)"; exit 3; }
rm -f <pcb-dir>/drc_report.json
"$KICAD_CLI" pcb drc --refill-zones --severity-error --exit-code-violations \
  --format json --output <pcb-dir>/drc_report.json <board.kicad_pcb>
rc=$?
[ "$rc" -eq 0 ] && jq -e '((.violations | map(select(.severity == "error")) | length) == 0)
  and ((.unconnected_items | length) == 0)' <pcb-dir>/drc_report.json
```

When a `<board>.kicad_dru` exists, also confirm the report cites at least one custom `rule '...'` (not only `netclass '...'`) on its findings. KiCad silently drops a whole `.kicad_dru` that holds a `;` comment or a `=~ /regex/` condition, leaving the gate judging netclass clearances only (Issue #8039). `kct readiness` fails with a `dru_loaded` check when every attributed finding is netclass-only; a clean (zero-violation) report proves nothing either way.

This is **not decorative and not skippable.** It is a *second, independent* DRC engine (KiCad's own), and it is the gate that catches what `kct check` misses:

- On 2026-07-04 a live copper short shipped past a clean `kct check` because `kct` read **stale zone fills**. The `--refill-zones` flag is load-bearing: it forces KiCad to refill the pours before checking, so the DRC reasons over the *actual* copper, not a stale cache.
- `--exit-code-violations` is load-bearing too. Without it `kicad-cli pcb drc` exits 0 even when it finds errors, and it counts unrouted connections separately from "violations" (an unrouted board prints `Found 0 violations`).
- **Pass means: kicad-cli exit 0 AND 0 error-severity violations AND 0 unconnected items.** There is no baseline; every error and every unconnected item counts. Exit 5 is a FAILED gate. Any other non-zero exit, or a missing or empty report, means the DRC did **not run**, and that refuses too.
- Warnings are not gated here: Gate 1's per-rule table owns them.
- The report goes to `<pcb-dir>/drc_report.json` on purpose: Gate 3 hands it to `kct export`, and it is also the name the export preflight auto-discovers.
- **Resolve `kicad-cli` the way `kct` does** — the first line uses `find_kicad_cli()`, which checks `PATH`, then the macOS app bundle, Homebrew, and the Linux and Windows default install paths (`kct doctor` prints the path it found). The venv does not provide `kicad-cli`, and a stock macOS install is not on `PATH`. Only an empty result means kicad-cli is missing — then this gate is a **hard blocker for sign-off**; say so explicitly and do not sign off on Gate 1 alone.
- A clean `kct check` **does not by itself constitute manufacturing sign-off.** Both engines must agree.

### Gate 3 — export the manufacturing bundle with Gate 2's report

```bash
# sign-off gate 3
rm -f <dir>/manifest.json
kct export <board.kicad_pcb> --output <dir> --mfr <tier> --drc-report <pcb-dir>/drc_report.json \
  && test -s <dir>/manifest.json
```

- Produces gerbers, drill, BOM, CPL, a report, and `manifest.json`.
- `--drc-report` hands Gate 2's report to the export preflight, whose safety floor refuses a board with shorts or unconnected items (exit 1, no manifest) independently of how Gates 1–2 were read. Without it the preflight only warns "No DRC report found" and exports anyway.
- Deleting the old manifest first makes freshness mechanical: a `manifest.json` that exists after this block was written by this run. No manifest ⇒ no sign-off.
- **Do not add flags outside the block** — no `--bom-source pcb`, `--skip-drc`, `--skip-drc-floor` or `--skip-preflight`. If the export refuses (for example because the BOM needs a schematic that does not exist), that refusal is the verdict.

### Gate 4 — HV / isolation creepage gate (conditional: mains/HV boards only)

**Skip this gate entirely for boards with no high-voltage / mains net.** For any board carrying an HV net group (mains input, primary-side switcher, etc.), a below-standard creepage/clearance pair is a safety defect that neither Gate 1 nor Gate 2 derives from an insulation standard. Run the isolation audit and judge **only its isolation section**:

```bash
# sign-off gate 4
rm -f <pcb-dir>/kct-audit.json
kct audit <board.kicad_pcb> --mfr <tier> \
  --net-class-map <net_class_map.json> \
  --hv-standard <iec60664|iec62368> \
  --hv-working-voltage <V-rms> \
  --hv-pollution-degree <1|2|3> \
  [--hv-material-group <I|II|IIIa|IIIb>] \
  [--hv-overvoltage-category <I|II|III|IV>] \
  [--hv-insulation <basic|supplementary|reinforced>] \
  --format json > <pcb-dir>/kct-audit.json
jq -e '.isolation | .hv_present and .threshold_supplied and .passed
  and (.could_not_verify | not) and (.mains_suspected_unclassified | not)' <pcb-dir>/kct-audit.json
```

- The `jq` rule mirrors `kct readiness`'s `hv_isolation` gate: HV nets were found, a requirement was supplied, every HV pair meets it, the measurement actually ran, and no mains-looking net was left unclassified.
- **The audit's headline verdict (`READY FOR MANUFACTURING`) is not sign-off**, and neither is its exit code. ERC, DRC and LVS are owned by Gates 1–3; this gate reads only the `.isolation` fields.
- HV nets are selected from the `--net-class-map` sidecar (the net whose class name is `HV`, or `--hv-net-class <name>`). Without a map the HV group will not resolve, `hv_present` is false, and the gate refuses — on an HV board that is the right answer.
- The required creepage is derived from the IEC 60664-1 / 62368-1 tables for the `(working voltage, pollution degree, material group)` triple. The required clearance is the larger of Table F.2 at the rated impulse withstand voltage (Table F.1, from the working voltage and `--hv-overvoltage-category`, default `II` = mains appliance) and Table F.8 at the steady-state peak, floored by pollution degree — so 230 V mains needs 1.5 mm at OVC II with basic insulation (issue #7057). `--hv-insulation` defaults to `basic` (issue #7082), so results are unchanged unless you opt in. Pass `--hv-insulation reinforced` for an HV-to-SELV barrier: `U_imp` steps up one preferred-series value (230 V OVC II: 4000 V -> 3.0 mm) and creepage doubles. The audit output states the assumed insulation type. To gate against a manually specified creepage instead, replace the `--hv-standard ...` flags with `--hv-min <mm>` (phase-1).
- The derived values are an engineering aid, **NOT a certification** — the governing standard and a qualified engineer remain authoritative.

## Sign-off verdict

Sign off **only if every applicable gate block exited 0**:

1. **Gate 1** — the `jq` rule over `kct check --output`'s JSON holds: 0 errors, 0 stale waivers, no blocking `unknown` coverage, DRC/ERC/LVS all `PASSED` with a non-vacuous LVS. **Any `NOT RUN` other than Manifest refuses**, and the run never used `--allow-incomplete` or `--drc-only`. The verdict also quotes the per-rule `BY RULE:` warning breakdown (not just an aggregate total).
2. **Gate 2** — `kicad-cli pcb drc --refill-zones --severity-error --exit-code-violations` exited 0 and its report has **0 error-severity violations and 0 unconnected items**. A kicad-cli that could not be found or did not write a report is NOT RUN, which refuses — it is a blocker, not a pass.
3. **Gate 3** — `kct export --drc-report <pcb-dir>/drc_report.json` exited 0 and wrote a fresh, non-empty `manifest.json` (the old one was deleted first).
4. **(HV/mains boards only)** Gate 4 — the `.isolation` rule holds. The audit headline is not consulted.

If any applicable gate fails or could not run, report **NOT signed off**, name the failing gate, and quote the specific violation(s). Never mark a board fab-ready on a partial run, and never improvise around a refusing gate (extra flags, a different order, a relaxed rule).

## Scripted equivalent: `kct readiness`

These gates are also available non-interactively as `kct readiness <board-path>
[--mfr <tier>] [--generate | --verify] [--assembly | --pcb-only]` (issue
#4977). The command runs the same ritual — refill + save the canonical PCB,
`kct check` at the resolved tier, the mandatory `kicad-cli pcb drc
--refill-zones` cross-gate, the per-rule warning review, export/packaging —
and additionally emits the hash-bound `output/readiness.json` evidence
documented in `docs/board-json-schema.md`. It exits non-zero for anything
other than a `ready` verdict and has no bypass flag.

`--verify` and `--generate` are mutually exclusive; **`--verify` is the
default**, so a bare `kct readiness <board-path>` checks an already-produced
package without writing gerbers, BOM/CPL or the manifest. Pass `--generate`
explicitly to (re)produce the package — the first sign-off for a board, or any
time the checked sources changed since the last package was built. `--generate`
refuses to replace a package a manufacturing recipe has already finalized;
re-verify or regenerate through that recipe instead (issue #5816).

Use the command for CI and batch sign-off. Use this skill when a human-in-the-loop
judgment is needed (an ambiguous BOM candidate, an accepted-risk warning decision)
— the command refuses those rather than adjudicating them.

## What this skill does NOT do

- It does not route copper, place parts, or edit the `.kicad_pcb`.
- It does not depend on any CI workflow, GitHub Actions context, or repo-internal `scripts/ci/*` gate. It runs identically as an interactive agent invocation inside any consumer repo's working tree. (Consumer repos that installed kicad-tools get portable gates under their own `.kct/ci/` — reference those if you need a scripted gate — but this skill needs only the `kct` / `kicad-cli` / `jq` commands above.)
- It does not enumerate fab tiers; the tier set is owned by `kicad_tools.manufacturers`.

## References

- `kct check --help` / `kct export --help` — authoritative `--mfr` tier choices (sourced from `kicad_tools.manufacturers.get_manufacturer_ids()`).
- The `--refill-zones` cross-gate convention (Epic #4054 convention #2) was established after a 2026-07-04 defect where `kct check` alone missed a live short and read stale zone fills.
- The per-rule warning-quote requirement was established after #4614: an aggregate "warnings match baseline" result concealed dozens of `silk_over_copper` warnings (silkscreen over exposed pads — an assembly risk) across two sign-offs; the fab order was aborted at the vendor site. A baseline compared per rule would have surfaced it immediately.
- `{{skill:ee-review}}` — sibling `kct` skill for analog/placement-blocked boards (advisory decisions, not copper).
