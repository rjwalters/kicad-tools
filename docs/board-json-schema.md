# `board.json` Schema (v1)

`board.json` is the normalized per-board data contract produced by
`kct board-metrics` (Epic #3674, Phase 1, issue #3676) and consumed by the
kicad-tools.org demo gallery (Phase 2, Astro site).

It is emitted to `boards/<id>/output/board.json`. Every metric is sourced from
artifacts that **already exist** under the board's `output/manufacturing/`
directory or `output/` — `kct board-metrics` never recomputes anything from KiCad.
It validates saved readiness evidence using file content hashes.

## Source artifacts

| `board.json` field      | Source artifact                       | Notes |
|-------------------------|---------------------------------------|-------|
| `slug`                  | board directory name                  | always present |
| `name`                  | `manifest.json` → `board.name`        | omitted if absent |
| `description`           | `report.md` → `### Theory of Operation` (fallback: front-matter `title`) | omitted if absent |
| `layer_count`           | `report.md` → `\| Layers \|` row      | integer copper layers |
| `board_size_mm`         | `report.md` → `\| Board Size \|` row  | `{width, height}` in mm |
| `part_count`            | `report.md` → `\| Footprints \|` row (fallback: `bom_jlcpcb.csv` rows − 1) | integer |
| `nets_routed_pct`       | `report.md` → `\| Signal Net Completion \|` row | float percent |
| `drc_violations`        | `report.md` → `## DRC Status` → `\| Errors \|` row | integer |
| `cost`                  | `report.md` → `## Cost Estimate` block | omitted if section absent |
| `renders`               | `output/renders/*.{svg,png}` (2D plots are `.svg`, 3D renders are `.png`; from `kct render`, #3675) | only existing files |
| `manufacturing_package` | `output/manufacturing/kicad_project.zip` | omitted if absent |
| `manifest_generated_at` | `manifest.json` → `generated_at`      | ISO-8601 string |
| `lvs_clean`             | `output/lvs.json` → `clean`           | omitted when `lvs.json` is absent (#3748, #3749) |
| `lvs_mismatches`        | `output/lvs.json` → `len(mismatches)` | omitted when `lvs.json` is absent (#3748, #3749) |
| `readiness` | `output/readiness.json` | current evidence, or `unverified` with reasons |

## Example

```json
{
  "$schema": "https://kicad-tools.org/schemas/board/v1.json",
  "schema_version": 1,
  "generated_at": "2026-06-15T00:00:00+00:00",
  "slug": "05-bldc-motor-controller",
  "name": "bldc_controller_routed",
  "description": "BLDC Motor Controller 3-Phase Brushless DC Motor Driver ...",
  "layer_count": 4,
  "board_size_mm": { "width": 80.0, "height": 100.0 },
  "part_count": 55,
  "nets_routed_pct": 82.1,
  "drc_violations": 14,
  "cost": { "per_board_usd": 9.16, "batch_qty": 5, "batch_total_usd": 45.78 },
  "renders": {
    "pcb_front": "renders/pcb-front.svg",
    "pcb_back": "renders/pcb-back.svg",
    "3d_front": "renders/3d-front.png",
    "3d_back": "renders/3d-back.png"
  },
  "manufacturing_package": "manufacturing/kicad_project.zip",
  "manifest_generated_at": "2026-06-12T05:03:41.535120+00:00",
  "lvs_clean": true,
  "lvs_mismatches": 0,
  "status": "partial"
}
```

## Field reference

| Field                   | Type    | Required | Description |
|-------------------------|---------|----------|-------------|
| `$schema`               | string  | yes      | Schema URL identifier |
| `schema_version`        | integer | yes      | Schema version (currently `1`) |
| `generated_at`          | string  | yes      | ISO-8601 UTC timestamp of extraction |
| `slug`                  | string  | yes      | Board directory name |
| `status`                | string  | yes      | `ok` \| `partial` \| `no_artifacts` |
| `name`                  | string  | no       | Human board name |
| `description`           | string  | no       | Theory-of-operation summary |
| `layer_count`           | integer | no       | Copper layer count |
| `board_size_mm`         | object  | no       | `{ "width": number, "height": number }` |
| `part_count`            | integer | no       | Number of footprints / BOM parts |
| `nets_routed_pct`       | number  | no       | Signal-net routing completion percent |
| `drc_violations`        | integer | no       | DRC error count |
| `cost`                  | object  | no       | `{ per_board_usd?, batch_qty?, batch_total_usd? }` |
| `renders`               | object  | no       | Map of render id → path relative to `board.json` |
| `manufacturing_package` | string  | no       | Path to downloadable `kicad_project.zip` |
| `manifest_generated_at` | string  | no       | Manifest build timestamp (ISO-8601) |
| `lvs_clean`             | boolean | no       | `true` iff `output/lvs.json` reports `clean: true`. Omitted when `lvs.json` is absent (board has not run LVS yet). |
| `lvs_mismatches`        | integer | no       | Count of mismatches recorded in `output/lvs.json`. Omitted when `lvs.json` is absent. |

### Paths are relative to `board.json`

`renders` values and `manufacturing_package` are relative to the `board.json`
file location (`boards/<id>/output/`). For example `renders/pcb-front.svg`
resolves to `boards/<id>/output/renders/pcb-front.svg`.

### `status` values

| Value          | Meaning |
|----------------|---------|
| `ok`           | `output/manufacturing/` exists, `report.md` parsed successfully, `drc_violations == 0`, and (when `lvs.json` is present) `lvs_clean == true`, and current `readiness.status == "ready"` |
| `partial`      | `output/manufacturing/` exists but `report.md` is absent/unparseable (only identity and recoverable fields are present), OR `drc_violations > 0`, OR an explicit `lvs_clean == false`, OR readiness is missing, stale or blocked |
| `no_artifacts` | the board has no `output/manufacturing/` directory at all |

The gallery additionally requires explicit zero DRC and clean LVS fields. A missing
or stale readiness report displays “Readiness unverified”; a current failing
report displays “Needs work”. A passing report displays “PCB fabrication ready”
for `pcb_only` or “Assembly ready” for `assembly`.

## Current manufacturing readiness

`readiness` is an additive optional object in schema v1. The producer attaches
it from `output/readiness.json`. The site independently loads and validates the
sidecar at build time, so an old `board.json` cannot preserve stale success.
The report writer must run the checks against the actual released design and
write the sidecar **after** finalizing all hashed artifacts:

```json
{
  "schema_version": 1,
  "checked_at": "2026-09-09T22:00:00Z",
  "mode": "pcb_only",
  "status": "blocked",
  "blockers": ["Native DRC reports 13 unconnected items"],
  "inputs": {
    "output/example_routed.kicad_pcb": "<64 lowercase SHA256 hex characters>",
    "output/example.kicad_sch": "<SHA256>",
    "output/manufacturing/manifest.json": "<SHA256>"
  },
  "checks": [
    {"name": "kct_check", "status": "passed", "detail": "DRC, ERC and LVS passed"},
    {"name": "native_drc", "status": "failed", "detail": "13 unconnected items"},
    {"name": "artifacts", "status": "passed", "detail": "Bundle matches checked sources"},
    {"name": "bom", "status": "passed", "detail": "PCB-only fabrication; assembly not included"}
  ]
}
```

`checked_at` is ISO-8601. Status is `ready`, `blocked`, or `unverified`;
check status is `passed`, `failed`, or `not_run`. All four named checks above
must be present and passed for Ready, with no blockers. For `assembly`, the
BOM check must verify actual procurement identifiers and consistent BOM/CPL
references. `pcb_only` makes no component procurement or assembly claim.

`inputs` paths are relative to the **board directory**, unlike render/download
paths. Every listed file must exist and match its SHA256. Absolute paths,
parent traversal and symlinks outside the board directory are rejected. Ready
requires PCB, schematic and manufacturing-manifest hashes. All existing
`output/*.kicad_pro`, `output/*.kicad_dru`, `output/*net_class_map.json` and
`output/fab_profile.json` files must also be included. The writer should include
all check evidence and delivered BOM/CPL/Gerber/project archives. Artifact
verification must check manifest hashes **and** source correspondence (for
example, compare the project's archived PCB against the checked PCB); mtime
or internal bundle consistency alone does not establish freshness.

### Producing the report: `kct readiness`

`kct readiness <board-dir|board.kicad_pcb>` is the scripted producer for this
sidecar (issue #4977). It implements the `/kct:manufacturing-readiness` and
`/kct:tapeout` skill contracts as an orchestrated command over the engines that
already exist — `kct check`, `kicad-cli pcb drc --refill-zones` and
`kct export` — and writes `output/readiness.json` only after every hashed
artifact is final:

1. Refill the copper pours and **save the canonical PCB**, then confirm the
   saved fill still matches a fresh refill per layer (evidence:
   `output/readiness/fill-consistency.json`). This happens before **both** the
   native check and the export, because a saved-vs-refilled divergence can
   survive two zero-error reports.
2. `kct check --mfr <tier>`, writing the machine-readable report the per-rule
   warning review reuses (no second check run).
3. The mandatory independent `kicad-cli pcb drc --refill-zones` cross-gate, on
   the saved board, judged by its **violation counts** — `kicad-cli` exits 0
   with errors, and "ran" is not "passed".
4. LVS evidence (a vacuous comparison is `not_run`, never clean), the per-rule
   assembly-affecting warning review, and — for boards carrying an `HV` net
   class — an explicit isolation requirement.
5. `kct export`, the schematic and assembly-view PDFs, a `README.txt`, and a
   regenerated `manifest.json` that checksums the **entire** bundle; then
   manifest integrity **and** archived-source provenance are verified.
6. `--assembly` additionally requires real procurement identifiers on every BOM
   line (a wholly empty part-number column is treated as a broken matcher, not
   exotic parts) and that through-hole parts excluded from the CPL are named as
   hand-solder items in the README.
7. `output/manufacturing.zip` is built **outside** the checksummed directory.

`ready` is emitted only when every applicable gate passed; otherwise the report
is `blocked` (a gate failed) or `unverified` (a gate could not run), always with
named `blockers`. The command exits non-zero for anything other than `ready`,
and there is no flag that produces `ready` on a partial run.

### Engine fingerprint

`engine` is an additive optional object in schema v1 (no `schema_version` bump,
per the additive-only policy below). It records the identity of the **checker**,
not just the board, so a changed engine does not silently imply that old
evidence is still qualified. A version string alone is insufficient: local rule
corrections have changed sign-off outcomes with no board edit at all while every
report still read `kicad-tools 0.20.0`.

```json
"engine": {
  "kicad_tools_version": "0.20.0",
  "commit": "47a53ea4...",
  "dirty": false,
  "source_digest": "<SHA256 over the installed package sources>",
  "kicad_cli_version": "kicad-cli 9.0.1",
  "manufacturer": "jlcpcb",
  "rules_digest": "<SHA256 over the resolved project/rule/net-class inputs>"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `kicad_tools_version` | string | yes | Release string of the producing kicad-tools |
| `dirty` | boolean | yes | `true` when the producing checkout had uncommitted changes |
| `manufacturer` | string | yes | Resolved fabrication tier the gates ran against |
| `commit` | string | no | Git commit of the producing checkout; omitted outside a checkout |
| `source_digest` | string | no | SHA256 over the installed package's Python sources — distinguishes two runs from the same commit but different working trees |
| `kicad_cli_version` | string | no | Native KiCad version used for the cross-gate; omitted when `kicad-cli` was unavailable |
| `rules_digest` | string | no | SHA256 over the resolved `.kicad_pro` / `.kicad_dru` / net-class-map inputs — a rule change with no board edit is visible here |
| `recipe` | string | no | Board recipe identity, when the board declares one |

Consumers comparing two reports should treat any change in `source_digest`,
`commit` (with `dirty: false`), `kicad_cli_version` or `rules_digest` as
grounds to present the older report as **historical evidence from a different
engine** rather than as a current qualification. The field is optional, so a
report written before this addition simply omits it.

An optional `evidence` object maps check names to board-relative report paths;
these evidence files should also appear in `inputs`. The gallery presents
check details and the verification timestamp. Missing, malformed, incomplete
or changed evidence produces an `unverified` object with `blockers` explaining
why; this object omits unverifiable timestamps and check results.

An optional `metrics` object can supply current `drc_violations` (nonnegative
integer), `nets_routed_pct` (0–100), `lvs_clean` (boolean), and
`lvs_mismatches` (nonnegative integer). Fresh `ready` or `blocked` reports
override the historical export metrics in both producer and site. Missing,
stale or unverified evidence cannot override them. The DRC aggregate should
use the maximum of the two engines' blocking counts to avoid naively adding
duplicate detections; per-engine counts remain in check details. Values without
current evidence are labeled as export-report metrics in the gallery.

The existing `manufacturing_package` field retains its historical path to the
**KiCad project ZIP** for compatibility. The gallery labels that download
“KiCad project (ZIP)” and separately serves the fabrication archive at
`manufacturing/gerbers/gerbers.zip` as “Gerber fabrication files (ZIP)”.

When present, `output/manufacturing.zip` is staged at
`/boards/<slug>/manufacturing.zip` and linked as “Full manufacturing package
(ZIP)”. This archive contains the complete manufacturing directory and lives
outside it to avoid a manifest checksum cycle. Include its hash in readiness
inputs after building it. Presence is detected at build time without a new
`board.json` field. Schematic/assembly PDFs, manufacturing instructions,
procurement review, the manifest, and electrical/native DRC JSON reports are
also offered individually when present in `output/manufacturing/`.

## Optional fields are omitted, never `null`

All fields except `$schema`, `schema_version`, `generated_at`, `slug` and
`status` are optional. When a source artifact is missing or a field cannot be
parsed, the field is **omitted** from the output rather than emitted as `null`.
Downstream consumers should treat a missing key as "unknown".

## Schema versioning policy

This file is the data contract for the Phase 2 Astro site, so stability matters:

- **Additive changes only** within `schema_version: 1`. New optional fields may
  be added without a version bump.
- **No renames and no type changes** to existing fields without bumping
  `schema_version`.
- Breaking changes (renames, type changes, removed fields, changed semantics)
  require incrementing `schema_version` and updating the `$schema` URL.

Consumers should read `schema_version` and reject documents whose major version
they do not understand.

## `lvs.json` Schema (v1)

`lvs.json` is the per-board LVS (Layout-vs-Schematic) verification report
produced by the board recipe's LVS step (issue #3748; board 00 only in v1, with
the fleet-wide rollout tracked by issue #3742). It is emitted next to
`board.json` at `boards/<id>/output/lvs.json` and records whether every
schematic pin's net name matches the corresponding PCB pad's net name.

### Source artifacts

| `lvs.json` field | Source artifact | Notes |
|------------------|-----------------|-------|
| `clean`          | comparison result | `true` iff `mismatches == []` |
| `mismatches[*].ref`            | schematic / PCB reference designator | e.g. `"D1"` |
| `mismatches[*].pad`            | pin or pad number                   | e.g. `"1"` |
| `mismatches[*].schematic_net`  | `Schematic.get_net_for_pin(ref, pad)` | `null` for floating |
| `mismatches[*].pcb_net`        | `(pad N ... (net K "NAME"))` in `.kicad_pcb` | `null` for unconnected |

### Example (clean)

```json
{
  "$schema": "https://kicad-tools.org/schemas/lvs/v1.json",
  "clean": true,
  "mismatches": []
}
```

### Example (dirty — D1 polarity flipped)

```json
{
  "$schema": "https://kicad-tools.org/schemas/lvs/v1.json",
  "clean": false,
  "mismatches": [
    {
      "ref": "D1",
      "pad": "1",
      "schematic_net": "LED_ANODE",
      "pcb_net": "GND"
    },
    {
      "ref": "D1",
      "pad": "2",
      "schematic_net": "GND",
      "pcb_net": "LED_ANODE"
    }
  ]
}
```

### Field reference

| Field                          | Type    | Required | Description |
|--------------------------------|---------|----------|-------------|
| `$schema`                      | string  | yes      | Schema URL identifier |
| `clean`                        | boolean | yes      | `true` iff `mismatches` is empty |
| `mismatches`                   | array   | yes      | Always present; empty when clean (never omitted, never `null`) |
| `mismatches[*].ref`            | string  | yes      | Reference designator (e.g. `"R1"`) |
| `mismatches[*].pad`            | string  | yes      | Pin/pad number as a string (e.g. `"1"`) |
| `mismatches[*].schematic_net`  | string &#124; null | yes | Net the pin sits on in the schematic, or `null` for floating |
| `mismatches[*].pcb_net`        | string &#124; null | yes | Net the pad sits on in the PCB, or `null` for unconnected |

### `mismatches` is always present

Unlike `board.json`, where optional fields are *omitted*, `lvs.json` always
emits `mismatches` (as `[]` when clean). This keeps the type contract simple
for downstream consumers — they can always `len(report["mismatches"])` without
a presence check.

### Schema versioning policy

Same rules as `board.json`: additive changes are allowed within v1; renames,
type changes, or removed fields require bumping the version in the `$schema`
URL. Consumers should reject documents whose `$schema` references a major
version they do not understand.

### Development boards without manufacturing export

`board-metrics` also reads boards that have no `output/manufacturing/` directory.
A usable PCB or schematic produces `status: partial`; no usable artifact produces
`no_artifacts`. Project identity alone does not certify that artifacts exist.
The original `readiness` verdict and blockers are retained, including stale or
blocked evidence. Static geometry and renders never produce `ok`.

Source selection uses explicit `project.artifacts.pcb` and `.schematic` paths,
relative to the board directory. A missing/invalid explicit path is diagnosed;
it is not replaced with another revision. Without an explicit path, exactly one
matching file directly under `output/` is required. Ambiguous candidates are
reported without choosing by name or modification time. Paths outside the board
directory are rejected. Invalid project specifications prevent fallback.

Additive development fields:

| Field | Meaning |
|---|---|
| `sources` | Selected artifact paths relative to the board directory and SHA-256 hashes; PCB/schematic also record `selection` (`project.artifacts` or `unambiguous_output`). |
| `diagnostics` | Explanations of missing, ambiguous, malformed or unsupported metadata/evidence. |
| `native_drc_geometry_violations` | Count of all entries in the bound native `violations` array, including warnings. |
| `native_drc_unconnected_items` | Separate count from the bound native `unconnected_items` array. |

`part_count` counts PCB footprints (including non-BOM footprints), not unique
BOM rows or schematic symbols. `layer_count` counts actual PCB copper layers.
`board_size_mm` describes the Edge.Cuts bounding envelope for supported closed
linear outlines; missing/open or curved outlines currently omit dimensions with
a diagnostic rather than supply an inaccurate or zero size. The project supplies
name/description. Schematic-only boards identify their source but do not invent
PCB counts or dimensions.

Native measurements require fresh readiness hashing both the selected PCB and
the report. `evidence.native_drc` selects the report when present; otherwise one
hash-bound `native-drc.json` or `placement-drc.json` is required. Missing, stale,
ambiguous or malformed reports omit measurements. `drc_violations` counts native
error-severity geometry findings in this development path; opens are separate.
Zero geometry errors with nonzero opens is not a clean connectivity result.
Neither label LVS nor an unbound report supplies copper-LVS proof. This command
does not reroute, refill, export manufacturing files or alter readiness evidence.
