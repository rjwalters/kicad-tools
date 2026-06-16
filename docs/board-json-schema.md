# `board.json` Schema (v1)

`board.json` is the normalized per-board data contract produced by
`kct board-metrics` (Epic #3674, Phase 1, issue #3676) and consumed by the
kicad-tools.org demo gallery (Phase 2, Astro site).

It is emitted to `boards/<id>/output/board.json`. Every metric is sourced from
artifacts that **already exist** under the board's `output/manufacturing/`
directory — `kct board-metrics` never recomputes anything from KiCad.

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
| `renders`               | `output/renders/*.png` (from `kct render`, #3675) | only existing files |
| `manufacturing_package` | `output/manufacturing/kicad_project.zip` | omitted if absent |
| `manifest_generated_at` | `manifest.json` → `generated_at`      | ISO-8601 string |

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
    "pcb_front": "renders/pcb-front.png",
    "pcb_back": "renders/pcb-back.png",
    "3d_front": "renders/3d-front.png",
    "3d_back": "renders/3d-back.png"
  },
  "manufacturing_package": "manufacturing/kicad_project.zip",
  "manifest_generated_at": "2026-06-12T05:03:41.535120+00:00",
  "status": "ok"
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

### Paths are relative to `board.json`

`renders` values and `manufacturing_package` are relative to the `board.json`
file location (`boards/<id>/output/`). For example `renders/pcb-front.png`
resolves to `boards/<id>/output/renders/pcb-front.png`.

### `status` values

| Value          | Meaning |
|----------------|---------|
| `ok`           | `output/manufacturing/` exists and `report.md` parsed successfully |
| `partial`      | `output/manufacturing/` exists but `report.md` is absent/unparseable; only identity and recoverable fields are present |
| `no_artifacts` | the board has no `output/manufacturing/` directory at all |

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
