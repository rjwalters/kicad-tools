---
name: tapeout
invocation: /kct:tapeout
suggestedModel: sonnet
description: Produce a complete, fab-ready export bundle for a routed board — or refuse loudly. Superset of /kct:manufacturing-readiness: runs the three sign-off pre-gates, then adds a BOM part-number-resolution gate, a per-rule warning-review gate (assembly-affecting warnings block unless explicitly acknowledged), schematic + assembly-view PDFs, a human-readable README, and a manifest that checksums the entire final bundle. "tapeout returned 0" means "upload this directory as-is."
---

# Tapeout

Produce a **complete, orderable manufacturing package** for a routed `.kicad_pcb`, or **refuse and exit non-zero**. This skill is the **deliverable-completeness** counterpart to sign-off: `/kct:manufacturing-readiness` proves *the copper is manufacturable*; `tapeout` proves *the package you are about to upload is complete and orderable*.

The contract is exactly: **`tapeout` returning 0 (with a manifest written) means "upload this output directory to the fab as-is."** Anything less writes **no manifest** and exits non-zero, naming the gate that failed.

> **The `kct` namespace.** This skill lives in `.claude/commands/kct/` — the kicad-tools-native, harness-agnostic agent-tool namespace, invoked as `/kct:tapeout`. It runs from inside a **consumer repo** that depends on kicad-tools as a `uv` dependency (`kct ...` and `kicad-cli ...` are on `PATH` via the venv). It does **not** assume the current directory is the kicad-tools repo, and it hardcodes no board path, no fab-tier list, and no CI-workflow context.

## Prerequisite

The native router/DRC backend must be built in the active checkout before this skill is meaningful (Epic #4054 convention #1). Run once per checkout / worktree:

```bash
uv run kct build-native --check   # expect: "C++ backend: available"
# if "not installed":
uv run kct build-native
```

`uv sync` does **not** build the native extension. A fresh checkout or git worktree needs this step explicitly.

`kicad-cli` must be on `PATH` — it is required by both the mandatory DRC cross-gate (Gate 2, delegated) and the drawing-export gates (Gates 5–6). If `kicad-cli` is missing, `tapeout` **refuses** (see Refusal conditions); there is no partial-package fallback.

## Model selection

`suggestedModel: sonnet`. Like `/kct:manufacturing-readiness`, this is deterministic checklist execution — run gates in order, read exit codes and reports, and refuse on any failure or unresolved BOM line. It does not require frontier judgment.

One open question worth flagging rather than silently resolving: the **BOM-resolution gate** (Gate 4) can involve a judgment call when the part matcher returns an *ambiguous* candidate (multiple plausible LCSC parts) rather than a clean hit or a clean miss. This skill treats ambiguity conservatively — an ambiguous line is an **unresolved** line (human selection required), never an auto-accepted guess — so `sonnet` remains appropriate. If a future consumer needs the skill to *adjudicate* ambiguous matches, that is a reason to revisit the model, and it should be raised as a follow-up rather than papered over here.

Model resolves through the harness's normal precedence chain (explicit dispatch param → harness role config → this doc's frontmatter `suggestedModel` → session default).

## Arguments

**Arguments**: `$ARGUMENTS`

`$ARGUMENTS` is `<board-path> [--mfr <tier>] [--assembly | --pcb-only] [--output <dir>] [--ack-warnings <rule,...>]`.

| Token | Meaning |
|-------|---------|
| `<board-path>` | **Required.** Path to the routed `*.kicad_pcb` to tape out (or a board directory containing one). Everything the skill operates on is derived from this token — never from a hardcoded board directory. The user supplies it. |
| `--mfr <tier>` | The fabrication tier to check and export against. **Optional**; if omitted, discover it (see "Resolving the fab tier" below) rather than assuming a default. |
| `--assembly` | Produce a **full assembly package**: gerbers + drill **and** BOM + CPL with part-number resolution. In this mode the BOM gate (Gate 4) is a **hard gate** — any unresolved BOM line fails the tapeout. |
| `--pcb-only` | Produce a **bare-board package**: gerbers + drill only. **Skips BOM and CPL entirely** (Gate 4 and the CPL half of Gate 3 are not run). Use when you are ordering the PCB alone and sourcing/assembling parts yourself. |
| `--output <dir>` | Where the bundle is written. Optional; defaults to `<pcb-dir>/manufacturing/` (the `kct export` default). |
| `--ack-warnings <rule,...>` | **Optional.** Explicitly acknowledge nonzero **assembly-affecting** warning counts for the named rules (comma-separated `rule_id`s, e.g. `--ack-warnings silk_over_copper,silk_edge_clearance`). Each acknowledged rule becomes a per-rule accepted-risk line in `README.txt` (Gate 7). Without an acknowledgement covering **every** assembly-affecting rule that has a nonzero warning count, Gate 4b **refuses** the tapeout. This is a *skill* argument (an acknowledgement ritual), not a `kct check` CLI flag. |

**`--assembly` and `--pcb-only` are mutually exclusive.** If both are passed, refuse. **If neither is passed, default to `--assembly`** — the safe default is the *complete* package; a caller who wants the reduced bare-board package must ask for it explicitly.

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

If the resolved tier is not in that set, stop and ask — do not silently fall back to a default, because the fab tier is load-bearing (in-pad-via rescue rules, min trace/space floors, whether LCSC part-number resolution even applies, etc. differ by tier).

## The tapeout ritual (run in order; refuse on the first hard failure)

### Pre-gates (Gates 1–3): delegate to `/kct:manufacturing-readiness`, do not re-describe

`tapeout` is a **superset** of `/kct:manufacturing-readiness` — it does **not** re-invent the sign-off gates. Before generating any package, satisfy the full sign-off ritual documented in **`.claude/commands/kct/manufacturing-readiness.md`** at the resolved tier:

1. **Gate 1 — `kct check <board.kicad_pcb> --mfr <tier>`** exits clean (DRC / manufacturing-rules + ERC/LVS/Manifest sub-checks; `--net-class-map` auto-discovered). For tapeout, additionally pass `--output <dir>/check-report.json` (an existing `kct check` flag) so the run leaves a machine-readable report in the bundle: Gate 4b derives its per-rule warning table from that file, and Gate 8 checksums it. Do **not** run a separate fourth `kct check` just for the warning table — reuse this gate's report.
2. **Gate 2 — the mandatory independent cross-gate `kicad-cli pcb drc --refill-zones <board.kicad_pcb>`** reports **0 new errors**. This is **not optional and not skippable**: it is a second, independent DRC engine that catches stale-zone-fill shorts `kct check` misses. A missing `kicad-cli` is a **hard blocker**, not a pass.
3. **Gate 3 — `kct export <board.kicad_pcb> --output <dir> --mfr <tier>`** produces gerbers, drill, and (in `--assembly` mode) BOM + CPL, plus a first-pass `manifest.json`.

Run `/kct:manufacturing-readiness <board-path> --mfr <tier> --output <dir>` (or execute its three gates directly) and **confirm it signs off**. If sign-off fails, `tapeout` **refuses** — do not proceed to package generation on a board that is not manufacturing-ready. Do not paraphrase or weaken those gates here; the sibling skill is the source of truth for them.

> Sequencing note: because `kct export` runs `kct check`'s rule set, and Gate 2 forces a zone refill, running the pre-gates *in this order* also side-steps two staleness traps a hand-assembled checklist hit — rules present in the board but not the paired `.kicad_pro` (#4097), and check-vs-fill staleness (#4096). Do not reorder.

### Gate 4 — BOM part-number resolution (hard gate in `--assembly`; **skipped** in `--pcb-only`)

**In `--pcb-only` mode, skip this gate entirely** (no BOM, no CPL — see Arguments). The rest of this section applies only to `--assembly` mode.

`kct export` enriches the BOM with fab-orderable part numbers (LCSC for JLC-style tiers) via `--auto-lcsc` (the default; disabled by `--no-auto-lcsc`). See `src/kicad_tools/cli/export_cmd.py` and `src/kicad_tools/export/bom_enrich.py` for the current behavior. The tapeout BOM gate wraps that enrichment with a **completeness contract** `kct export`'s exit code does not currently give you.

Every BOM line must fall into exactly one bucket:

- **Resolved** — carries a verified, fab-orderable part number for the resolved tier (e.g. a populated LCSC column for a JLC tier).
- **Unresolved (human selection required)** — the matcher genuinely found **no candidate** for this component. This is a legitimate outcome for a non-standard part; it belongs in an explicit **"unresolved — human selection required"** report line, not a silent blank.

The gate then:

1. **Opens the exported BOM and inspects the actual part-number column.** Do **not** trust `kct export`'s exit code as proof the BOM is populated.
2. **Fails hard** if the resolution machinery is broken rather than merely finding no match — the canonical case is the missing `[parts]` extra, where `--auto-lcsc` **soft-fails to an empty part-number column while `kct export` still exits 0** (this is the defect tracked in **#4104**). An **entirely empty** part-number column (0 resolved lines) when resolution was expected is treated as **matcher-broken**, not "every part is exotic" — that is a **hard failure of this gate**, not an unresolved-lines report.
3. **Fails** `--assembly` tapeout if **any** line is unresolved. A complete assembly package has zero human-selection-required lines by definition; surface the unresolved report and refuse.

The distinction is load-bearing: **matcher-broken → refuse the whole tapeout** (the package is not trustworthy); **no-match on specific parts → unresolved report → refuse `--assembly`** (the human must pick parts before this can ship as an assembly order). Never let a silently-empty part-number column pass as "resolved."

### Gate 4b — warning review (per-rule table + assembly-affecting acknowledgement)

**"0 errors" and "fit to fabricate" are not the same claim.** `kct check`'s exit code is driven by errors — warnings never fail Gate 1 — but some warning classes are genuine **assembly risks** (silkscreen printed over an exposed pad can interfere with solder wetting), and they must not ride through a gate whose stated meaning is "upload this directory as-is." This gate forces the warning population to be *looked at*, per rule, before the bundle is declared upload-ready. It runs in both `--assembly` and `--pcb-only` modes.

1. **Derive a per-rule warning table** from Gate 1's machine-readable report (`<dir>/check-report.json` — see the pre-gates note; do not re-run `kct check`). Group the JSON `violations[]` by `rule_id`, warnings only:

   ```bash
   jq '[.violations[] | select(.severity=="warning")]
       | group_by(.rule_id)
       | map({rule: .[0].rule_id, count: length})' <dir>/check-report.json
   ```

   **Print this table in the tapeout report.** Reporting an aggregate warning total alone ("N warnings") is **non-compliant** — an aggregate can satisfy a "matches baseline / non-regressive" check indefinitely while concealing an entire defect class. The per-rule table is the artifact that prevents that.

2. **Check the assembly-affecting rule set.** At minimum, the following rules are **assembly-affecting** (treat this set as a floor, not a ceiling — extend it when a new rule clearly affects assembly or is a manufacturer-profile floor):

   - `silk_over_copper` — silk over an exposed pad interferes with solder wetting
   - `silkscreen_line_width` — the compared floor comes from the manufacturer profile, so every warning is a hard fab-constraint violation, not a preference
   - `silk_edge_clearance`
   - `silk_overlap`

3. **Require explicit acknowledgement of any nonzero count in that set.** A nonzero assembly-affecting warning count is acceptable **only** when explicitly acknowledged: the caller passed `--ack-warnings` naming that exact rule, or a human-authored accepted-risk decision exists and you record it as one accepted-risk line **per rule** in `README.txt` (Gate 7). Silence is not acknowledgement, and a passing Gate 1 exit code is not acknowledgement. **Unacknowledged assembly-affecting warnings ⇒ TAPEOUT REFUSED** — never exit 0, never write the final manifest. Warnings outside the assembly-affecting set appear in the table but do not require acknowledgement.

**Gate condition:** the per-rule warning table was derived from Gate 1's JSON report and printed in the tapeout report, **and** every assembly-affecting rule with a nonzero warning count carries an explicit acknowledgement (or all assembly-affecting counts are zero).

### Gate 5 — schematic PDF (full hierarchy)

`kct export` does **not** produce human-reviewable drawings; generate them with `kicad-cli` into the same output directory:

```bash
kicad-cli sch export pdf <board-or-project>.kicad_sch --output <dir>/<board>-schematic.pdf
```

Export the **full hierarchy** (all sheets), not just the root sheet. **Gate condition:** the PDF exists, is non-empty, and its mtime is newer than the schematic source. No schematic PDF ⇒ no tapeout.

### Gate 6 — assembly-view PDFs (front and back)

Produce two board drawings a human (and the assembler) can read, with explicit, named layer sets:

```bash
# Front assembly view: front copper + front silkscreen + board outline
kicad-cli pcb export pdf <board.kicad_pcb> \
  --layers F.Cu,F.SilkS,Edge.Cuts \
  --output <dir>/<board>-assembly-front.pdf

# Back assembly view: back copper + back silkscreen + board outline
kicad-cli pcb export pdf <board.kicad_pcb> \
  --layers B.Cu,B.SilkS,Edge.Cuts \
  --output <dir>/<board>-assembly-back.pdf
```

**Gate condition:** both PDFs exist and are non-empty. Optionally also generate `kct render` 2D/3D preview images and drop them in the output dir — nice-to-have, not a gate.

### Gate 7 — README.txt (human-facing bundle index)

Write `<dir>/README.txt` enumerating **every** deliverable in the bundle so a human opening the zip knows what they have. It must include:

- The exact **board file** taped out and the **resolved fab tier**.
- The mode (`--assembly` or `--pcb-only`) and what that implies (BOM/CPL present or intentionally absent).
- A list of every file in the output directory with a one-line description (gerbers/drill archive, BOM, CPL, schematic PDF, assembly-view PDFs, renders, manifest).
- **Hand-solder / THT items** (carried from the CPL's THT-exclusion note) — parts the assembler will *not* place and someone must hand-solder.
- The **per-rule warning table** from Gate 4b (rule → warning count; not an aggregate total), and **one acknowledgement line per assembly-affecting rule with a nonzero count** — naming the rule, the count, and why the risk is accepted (from `--ack-warnings` or the recorded accepted-risk decision). If every assembly-affecting count is zero, state that explicitly.
- Any **accepted-risk notes** carried forward from sign-off docs (e.g. a documented DRC waiver). If none, say "no accepted-risk waivers."

**Gate condition:** `README.txt` exists, names the board file, the tier, and the mode, **and** contains the per-rule warning table plus one acknowledgement line per assembly-affecting rule with a nonzero count (or the explicit statement that all assembly-affecting counts are zero).

### Gate 8 — full-bundle manifest (checksums the entire output dir)

The `manifest.json` `kct export` wrote in Gate 3 only covers **what `kct export` itself produced** — it does **not** include the PDFs, renders, or README added by Gates 5–7. Regenerate/extend the manifest so it checksums **every file present in the output directory at completion**:

- Enumerate **all** files in `<dir>` (recursively), and record a checksum (e.g. SHA-256) for each.
- Record the **exact board file** name and the **resolved tier**.
- Record the mode (`--assembly` / `--pcb-only`).
- Record the **per-rule warning counts** from Gate 4b (a `rule_id` → warning-count map, plus the list of acknowledged assembly-affecting rules), so a later re-verification can diff the warning population **per rule** rather than against an aggregate total. An aggregate-only warning number in the manifest is non-compliant for the same reason it is in Gate 4b.

**Gate condition:** a `manifest.json` exists, is newer than every other file it lists, its file set equals the actual contents of `<dir>` (nothing in the directory is unchecksummed, nothing checksummed is missing), and it records the per-rule warning counts. A stale or partial manifest is a **failure** — it breaks the "upload as-is" contract.

## Tapeout verdict

Emit a **complete, orderable package** (manifest written, exit 0) **only if all applicable gates hold**:

1. Pre-gates 1–3 (`/kct:manufacturing-readiness`) sign off at the resolved tier (Gate 1 run with `--output <dir>/check-report.json`).
2. Gate 4 BOM resolution: in `--assembly`, **zero** unresolved lines **and** resolution machinery intact (part-number column actually populated, not silently empty). Skipped in `--pcb-only`.
3. Gate 4b warning review: the **per-rule** warning table derived from Gate 1's JSON report and printed (an aggregate total alone does not satisfy this), **and** every assembly-affecting rule with a nonzero warning count explicitly acknowledged — or all assembly-affecting counts are zero. **"0 errors + an aggregate warning total" is never sufficient for this verdict.**
4. Gate 5 schematic PDF present and fresh.
5. Gate 6 both assembly-view PDFs present and non-empty.
6. Gate 7 `README.txt` present, naming board + tier + mode, and carrying the per-rule warning table + per-rule acknowledgement lines (or the all-zero statement).
7. Gate 8 full-bundle `manifest.json` covers **every** file in the output dir, records the per-rule warning counts, and is the newest artifact.

If any applicable gate fails or could not run, **do not write the final manifest**. Report **TAPEOUT REFUSED**, name the failing gate, and quote the specific violation(s). Never emit a manifest — and never imply "upload as-is" — on a partial run.

## Refusal conditions (exit non-zero, no final manifest written)

Refuse the tapeout, loudly, when any of these hold:

- **Any pre-gate fails** — `kct check` not clean, the `kicad-cli pcb drc --refill-zones` cross-gate reports new errors, or `kct export` did not produce its first-pass bundle.
- **Unacknowledged assembly-affecting warnings (Gate 4b)** — any rule in the assembly-affecting set (`silk_over_copper`, `silkscreen_line_width`, `silk_edge_clearance`, `silk_overlap`, at minimum) has a nonzero warning count with no matching `--ack-warnings` entry or per-rule accepted-risk line. Quote the per-rule warning table in the refusal. "0 errors" is not "fit to fabricate" — these two claims must never be conflated by this skill.
- **`--assembly` with unresolved BOM lines** — one or more components have no fab-orderable part number (human selection required). Surface the unresolved report; do not ship an incomplete assembly order.
- **BOM-resolution machinery unavailable** — the requested capability could not run: the `[parts]` extra is missing (so `--auto-lcsc` soft-fails to an empty column — **#4104**), or there is no network for part matching. This is a **refusal**, not a silently-empty BOM that exits 0.
- **Requested capability unavailable** generally — e.g. `kicad-cli` not on `PATH` (drawings and the cross-gate cannot run), or the resolved tier is not in `get_manufacturer_ids()`.
- **Board has no paired project rules** — the board file has no accompanying `.kicad_pro` (or the rules live only in the board / `.kicad_prl` and not the project file, echoing #4097). Without paired project rules the sign-off tier is not trustworthy; refuse.
- **`--assembly` and `--pcb-only` both passed** — mutually exclusive; refuse rather than guessing.

## What this skill does NOT do

- **It does not implement a `kct tapeout` CLI subcommand.** This is a **skill only** — an agent orchestrating existing `kct check` / `kicad-cli pcb drc` / `kct export` / `kicad-cli sch|pcb export pdf` invocations, exactly as `/kct:manufacturing-readiness` orchestrates its gates without a dedicated binary. A scriptable `kct tapeout` command (e.g. for CI) is real but **separable future work** — file it as its own issue; this skill does not block on it and does not add CLI tests.
- **It does not fix the `--auto-lcsc` soft-fail defect (#4104).** It *surfaces* that defect as a refusal condition — the BOM gate independently verifies the part-number column is populated rather than trusting `kct export`'s exit code — but it does not change `kct export`'s behavior. When #4104 is fixed upstream, the BOM gate's independent check becomes belt-and-suspenders; until then it is the only thing standing between you and an empty BOM that "succeeded."
- **It does not route copper, place parts, or edit the `.kicad_pcb` / schematic.** If the board is not manufacturing-ready, tapeout refuses; making it ready is `/kct:manufacturing-readiness`'s upstream concern and the router's job.
- **It does not enumerate fab tiers.** The tier set is owned by `kicad_tools.manufacturers.get_manufacturer_ids()`.
- **It does not depend on any CI workflow or GitHub Actions context.** It runs identically as an interactive agent invocation inside any consumer repo's working tree, needing only `kct` and `kicad-cli` on `PATH`.

## References

- **`/kct:manufacturing-readiness`** (`.claude/commands/kct/manufacturing-readiness.md`) — the sign-off ritual `tapeout` delegates its three pre-gates to. `tapeout` is a strict superset: sign-off + BOM-resolution gate + drawings + README + full-bundle manifest.
- **#4104** — `kct export --auto-lcsc` soft-fails to an empty LCSC column when the `[parts]` extra is missing (export still exits 0). This is the known BOM-resolution gap the Gate 4 contract must **not** silently swallow: verify the part-number column independently; do not trust the exit code.
- **#4614** — a board returned a clean tapeout while carrying dozens of `silk_over_copper` warnings (silkscreen printed across exposed pads); the fab order was aborted at the vendor site. Every finding was warning-severity, so the error-driven gates never looked at them. Gate 4b (the per-rule warning table + assembly-affecting acknowledgement) exists because of that incident.
- `kct check --help` / `kct export --help` — authoritative `--mfr` tier choices (sourced from `kicad_tools.manufacturers.get_manufacturer_ids()`).
- `src/kicad_tools/cli/export_cmd.py`, `src/kicad_tools/export/bom_enrich.py` — the `--auto-lcsc` / `--no-auto-lcsc` enrichment behavior Gate 4 wraps.
- `/kct:ee-review` — sibling `kct` skill for analog/placement-blocked boards (advisory decisions, not copper).
