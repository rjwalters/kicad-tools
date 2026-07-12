---
name: manufacturing-readiness
invocation: /kct:manufacturing-readiness
suggestedModel: sonnet
description: Sign off a routed board for fabrication — run kct check, the mandatory kicad-cli pcb drc --refill-zones cross-gate, and a kct export bundle at the board's fab tier — and refuse sign-off if any gate is skipped.
---

# Manufacturing readiness

Sign off a routed `.kicad_pcb` for fabrication. This skill codifies the **sign-off ritual**: `kct check` at the board's fab tier, the **mandatory** independent cross-gate `kicad-cli pcb drc --refill-zones`, and a `kct export` manufacturing bundle — then confirms a `manifest.json` was produced. A clean `kct check` **alone is not sign-off**.

> **The `kct` namespace.** This skill lives in `.claude/commands/kct/` — the kicad-tools-native, harness-agnostic agent-tool namespace, invoked as `/kct:manufacturing-readiness`. It runs from inside a **consumer repo** that depends on kicad-tools as a `uv` dependency (`kct ...` is on `PATH` via the venv). It does **not** assume the current directory is the kicad-tools repo, and it hardcodes no board path, no fab-tier list, and no CI-workflow context.

## Prerequisite

The native router/DRC backend must be built in the active checkout before this skill is meaningful (Epic #4054 convention #1). Run once per checkout / worktree:

```bash
uv run kct build-native --check   # expect: "C++ backend: available"
# if "not installed":
uv run kct build-native
```

`uv sync` does **not** build the native extension. A fresh checkout or git worktree needs this step explicitly.

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

### Gate 1 — `kct check` at the fab tier

```bash
kct check <board.kicad_pcb> --mfr <tier>
```

- This is the DRC / manufacturing-rules check (clearances, dimensions, edge, silkscreen, plus the ERC / LVS / Manifest meta sub-checks).
- **`--net-class-map` is auto-discovered** — `kct check` probes `<pcb_dir>/net_class_map.json`, `<pcb_dir>/output/net_class_map.json`, and `<pcb_dir>/../output/net_class_map.json` (the sidecar `kct route` itself writes). Do **not** over-specify `--net-class-map` unless the sidecar lives somewhere non-conventional; if it does, pass it explicitly:
  ```bash
  kct check <board.kicad_pcb> --mfr <tier> --net-class-map <path/to/net_class_map.json>
  ```
- A clean exit here is necessary but **not sufficient**. Proceed to Gate 2 regardless — do not treat a green Gate 1 as sign-off.

### Gate 2 — the mandatory independent cross-gate (NOT optional)

```bash
kicad-cli pcb drc --refill-zones <board.kicad_pcb>
```

This is **not decorative and not skippable.** It is a *second, independent* DRC engine (KiCad's own), and it is the gate that catches what `kct check` misses:

- On 2026-07-04 a live copper short shipped past a clean `kct check` because `kct` read **stale zone fills**. The `--refill-zones` flag is load-bearing: it forces KiCad to refill the pours before checking, so the DRC reasons over the *actual* copper, not a stale cache.
- A clean `kct check` **does not by itself constitute manufacturing sign-off.** Both engines must agree.

Read the KiCad DRC report. **Zero new errors** is required. If `kicad-cli` is not installed on the machine, this is a **hard blocker for sign-off** — say so explicitly; do not silently sign off on Gate 1 alone.

### Gate 3 — export the manufacturing bundle at the fab tier

```bash
kct export <board.kicad_pcb> --output <dir> --mfr <tier>
```

- Produces gerbers, drill, BOM, CPL, a report, and `manifest.json`.
- **Confirm `<dir>/manifest.json` exists and was freshly written** (its mtime should be newer than the routed PCB). No manifest ⇒ no sign-off.
- Useful variants: `--dry-run` (show what would be generated without writing) and `--no-report`.

## Sign-off verdict

Sign off **only if all three** hold:

1. Gate 1 `kct check --mfr <tier>` exits clean at the resolved tier.
2. Gate 2 `kicad-cli pcb drc --refill-zones` reports **0 new errors** (and actually ran — a missing `kicad-cli` is a blocker, not a pass).
3. Gate 3 `kct export --mfr <tier>` wrote a fresh `manifest.json`.

If any gate fails or could not run, report **NOT signed off**, name the failing gate, and quote the specific violation(s). Never mark a board fab-ready on a partial run.

## What this skill does NOT do

- It does not route copper, place parts, or edit the `.kicad_pcb`.
- It does not depend on any CI workflow, GitHub Actions context, or repo-internal `scripts/ci/*` gate. It runs identically as an interactive agent invocation inside any consumer repo's working tree. (Consumer repos that installed kicad-tools get portable gates under their own `.kct/ci/` — reference those if you need a scripted gate — but this skill needs only the `kct` / `kicad-cli` commands above.)
- It does not enumerate fab tiers; the tier set is owned by `kicad_tools.manufacturers`.

## References

- `kct check --help` / `kct export --help` — authoritative `--mfr` tier choices (sourced from `kicad_tools.manufacturers.get_manufacturer_ids()`).
- The `--refill-zones` cross-gate convention (Epic #4054 convention #2) was established after a 2026-07-04 defect where `kct check` alone missed a live short and read stale zone fills.
- `/kct:ee-review` — sibling `kct` skill for analog/placement-blocked boards (advisory decisions, not copper).
