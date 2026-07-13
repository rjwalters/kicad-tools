# kicad-tools install pilot: `../chorus` (Epic #4054 acceptance)

Evidence report for issue **#4058** — the acceptance test for Epic #4054
(installing kicad-tools into consumer PCB-design repos). This pilot **inverts**
the historical chorus-v22 pattern: instead of doing chorus manufacturing work
from *inside* kicad-tools with a symlinked fixture, this proves the new model —
work happens *inside chorus* with kicad-tools installed as a `uv` dependency
plus vendored `kct` skills and portable CI gates.

**Date:** 2026-07-12
**kicad-tools:** v0.14.0 (installer sourced from worktree `feature/issue-4058`;
`--path` dep points at the durable main checkout `/Users/rwalters/GitHub/kicad-tools`, commit `612cf1fa`)
**Chorus pilot branch:** `pilot/kct-install-4058` (commit `1e3267c`, **local only — NOT pushed**)
**Chorus board under test:** `hardware/chorus-test-revA/kicad/chorus-test-revA_v22_mfg.kicad_pcb` @ `--mfr jlcpcb-tier1`

---

## Verdict summary

| Item | Result |
|------|--------|
| Installer (`install-kct.sh --path`) first run | **PASS** — clean, all artifacts written |
| Installer second run (idempotency) | **PASS** — byte-identical vendored files, no duplicate CLAUDE.md block, no duplicate dep, exit 0 |
| Chorus Loom install untouched | **PROVEN** — 0-line diff vs `main` on `.claude/commands/loom/`, `.loom/`, `loom.sh` after both runs |
| `uv sync` in chorus | **PASS** — kicad-tools installed as editable path dep |
| `uv run kct build-native --check` | **PASS** — `C++ backend: available (version 1.0.0)` |
| Gate 1 `kct check --mfr jlcpcb-tier1` | **1167 errors** — **drift finding**, not a board regression (see Classification) |
| Gate 2 `kicad-cli pcb drc --refill-zones` | **1147 violations** — **drift finding**, board-file carries generic (non-tier1) design rules |
| Gate 3 `kct export --mfr jlcpcb-tier1 --dry-run` | **PASS** — exit 0, full bundle planned |
| `.kct/ci/check_routed_drc.py` | **RAN** — functional; correctly gated (finding: needed a new `--mfr` flag, added in this PR) |
| `.kct/ci/check_copper_lvs.py` | **RAN** — functional; board is schematic-unbound in chorus, vacuity guard fired as designed |
| Nothing pushed to chorus remote | **CONFIRMED** — pilot branch has no upstream tracking |

**Bottom line:** the *installation* model works end-to-end and is idempotent and
Loom-safe. The *board check* reproduces the board exactly as committed and
surfaces a genuine **kicad-tools/board-provenance drift finding** — the
2026-07-04 "clean at JLC floors" sign-off was made against a **tier1-capability
KiCad referee project**, but the standalone committed `.kicad_pcb` carries no
embedded tier1 design rules, so a bare check today falls back to KiCad's generic
defaults and reports thousands of rule violations. This is expected and is
documented below rather than "fixed" by editing the board (per the issue's
honesty requirement).

---

## 1. Install transcript summary

### Pre-flight (curator-verified, reconfirmed live)

- Chorus is a `package.json` Loom workspace with **no root `pyproject.toml`** —
  `install-kct.sh` fails fast without one (`error: target has no pyproject.toml`).
- The completed v22 manufacturing board lives **only** on the local, unpushed
  chorus branch `feat/revA-manufacturing-completion` (tip `1e65cce`, 2026-07-04),
  which **predates** chorus's Loom v0.10.6 install — checking it out wholesale
  would revert Loom. Handled via **path-scoped checkout** of just the board +
  bundle onto a pilot branch cut from `main`.
- Tier from the v22 `manifest.json`: `"manufacturer": "jlcpcb-tier1"`.
- No `net_class_map.json` sidecar in chorus history — `--net-class-map` omitted
  (graceful no-op, as expected).

### Step 1 — pilot branch + pyproject bootstrap

```
git -C ../chorus checkout -b pilot/kct-install-4058     # cut from main (563a403)
# wrote a minimal root pyproject.toml (see Finding 1)
```

The bootstrapped `pyproject.toml` (pilot-branch-only, additive, reversible):

```toml
[project]
name = "chorus"
version = "0.1.0"
description = "Chorus PCB-design workspace (kicad-tools consumer)"
requires-python = ">=3.10"
dependencies = []

[tool.uv]
package = false
```

`requires-python = ">=3.10"` matches kicad-tools' own floor (a stricter consumer
floor could conflict with dep resolution). `package = false` tells uv this is a
non-packaged workspace root — chorus is a board-design repo with no importable
`chorus/` Python package to build. Coexists with the existing `package.json`
(Loom/JS + uv/Python at the same root, exactly like kicad-tools' own layout).

### Step 2 — installer, first run (`--path` mode, network-free)

```
scripts/install-kct.sh --path /Users/rwalters/GitHub/kicad-tools /Users/rwalters/GitHub/chorus
```

Clean run. All eight stages OK. Wrote:
- `pyproject.toml`: `dependencies = ["kicad-tools"]` + `[tool.uv.sources] kicad-tools = { path = "../kicad-tools", editable = true }`
- `.claude/commands/kct/`: `README.md` + 4 skills (`board-recipe-scaffold`, `ee-review`, `layout-journal`, `manufacturing-readiness`)
- `.kct/ci/`: `check_copper_lvs.py`, `check_routed_drc.py`, `net_class_map_resolver.py` (sibling triple) + `README.md`
- `CLAUDE.md`: guarded `<!-- BEGIN KICAD-TOOLS -->…<!-- END KICAD-TOOLS -->` block **appended after** the existing Loom block
- `.kct/install-metadata.json`

The `--path` dep deliberately points at the **durable main checkout**
(`/Users/rwalters/GitHub/kicad-tools`), not the transient worktree, so the
owner's ongoing use survives worktree cleanup. The *installer binary itself* was
run from the worktree so the fix in this PR (see Finding 4) is what got tested.

### Step 3 — `uv sync` + native backend

```
cd ../chorus
uv sync                      # built + installed kicad-tools==0.14.0 (editable path) + 12 deps
uv run kct build-native      # C++ backend installed successfully! (router_cpp.cpython-314-darwin.so)
uv run kct build-native --check   # → C++ backend: available (version 1.0.0)
```

Convention #1 (Epic #4054) satisfied: `uv sync` alone does NOT build the native
extension; `kct build-native` was run explicitly in the fresh consumer venv.

---

## 2. Idempotency proof (second installer run)

Second run with identical args:

- **Stage 5 (dependency):** `ok: kicad-tools dependency already present and up to date (no-op)` — no duplicate `uv add`.
- **Vendored files:** all 10 files (`.kct/ci/*`, `.claude/commands/kct/*`, metadata) **byte-for-byte identical** — `shasum` diff before/after the second run is empty.
- **CLAUDE.md:** unchanged SHA (`8f661087…`); exactly **1** `BEGIN KICAD-TOOLS` and **1** `END KICAD-TOOLS` marker.
- **pyproject.toml:** unchanged SHA (`3a9ee05c…`); exactly **1** `"kicad-tools"` dependency entry and **1** `[tool.uv.sources]` `kicad-tools =` line.
- Exit 0.

---

## 3. "Loom untouched" proof

After **both** installer runs, on the pilot branch:

```
git -C ../chorus diff main -- .claude/commands/loom .loom loom.sh
# → 0 lines (empty)
git -C ../chorus status --short -- .claude/commands/loom .loom loom.sh
# → (empty: no staged/modified/untracked additions under Loom paths)
```

The installer never writes under `.claude/commands/loom/`, `.loom/`, or
`loom.sh`; the CLAUDE.md merge appends *after* the existing Loom block via
guarded markers, preserving it verbatim (including its no-trailing-newline
state, which the installer handled correctly).

---

## 4. Manufacturing-readiness flow (v22 board @ jlcpcb-tier1)

Followed the vendored `/kct:manufacturing-readiness` playbook.

### Gate 1 — `kct check … --mfr jlcpcb-tier1`

`DRC: FAILED (36 rules checked, 1167 error(s), 251 warning(s))`. Error families:

| count | rule_id |
|------:|---------|
| 692 | `clearance_segment_zone` |
| 385 | `clearance_via_zone` |
| 49 | `dimension_drill_clearance` |
| 21 | `clearance_pad_zone` |
| 19 | `clearance_pad_segment` |
| 1 | `edge_clearance_pad_hole` |

1117 of 1167 (96%) are `clearance_*_zone` — nearly all worded
`Short: X overlaps zone fill of net 'GNDD'`. That is the classic
**stale-zone-fill** signature: `kct check` reasons over the pours as committed in
the static `.kicad_pcb`, which is why Convention #2 mandates the
`--refill-zones` cross-gate.

`ERC/LVS: NOT RUN (no schematic discovered next to PCB)` — the v22 `_mfg.kicad_pcb`
is a standalone manufacturing artifact with no adjacent `.kicad_sch`. `Manifest: STALE`.

### Gate 2 — `kicad-cli pcb drc --refill-zones` (mandatory cross-gate)

`Found 1147 violations` + `1 unconnected`. Families:

| count | type | rule floor KiCad used |
|------:|------|-----------------------|
| 507 | `clearance` | 0.2000 mm |
| 199 | `drill_out_of_range` | min hole 0.3000 mm (actual 0.2000) |
| 199 | `track_width` | min width 0.2000 mm (actual 0.1300) |
| 199 | `via_diameter` | min diameter 0.5000 mm (actual 0.4500) |
| 27 | `hole_clearance` | 0.2500 mm |
| 11 | `courtyards_overlap` | — |
| 5 | `copper_edge_clearance` | 0.5000 mm (actual 0.3350) |

**This is the decisive evidence for the drift classification.** KiCad is checking
against **its own generic application defaults** (min track 0.2, min hole 0.3,
min via 0.5, clearance 0.2, edge 0.5). Inspection of the board's `(setup)` block
confirms it carries **no explicit design-rule constraints** — no
`min_track_width`, `min_hole`, `min_clearance`, etc. — so KiCad falls back to
those conservative defaults. The board was actually routed to **jlcpcb-tier1
floors** (0.13 mm track, 0.20 mm drill, 0.45 mm via), all of which are
*legal* at tier1 but *illegal* against KiCad's generic defaults.

The committed `manufacturing_v22/LAYOUT_NOTES.md` (the append-only fab-clean
journal) makes this explicit: the 2026-07-04 sign-off referee was
`kicad-cli … capability project, --refill-zones, severity-error` — i.e. a
**separate KiCad project that loaded tier1 design rules**. The final journal
entry recorded `shorting 0, clearance 0, hole 1 (NT3 net-tie artifact),
edge 4, courtyards 12, unconnected 95, strict 51/51` — a clean *tier1* referee,
carrying a handful of known/accepted referee items. The standalone `.kicad_pcb`
never embedded tier1 rules; the referee project supplied them.

### Gate 3 — `kct export --mfr jlcpcb-tier1 --dry-run`

**PASS (exit 0).** Would generate: `bom_jlcpcb-tier1.csv`, `cpl_jlcpcb-tier1.csv`,
`gerbers.zip`, `report.pdf`, `kicad_project.zip`, `manifest.json` into
`hardware/chorus-test-revA/kicad/manufacturing/`. Ran dry-run only — did **not**
clobber the committed `manufacturing_v22/` shipping-truth bundle (Convention #3,
artifact-first).

### Classification of each delta from the 2026-07-04 "clean at JLC floors" baseline

| Delta | Classification | Why |
|-------|----------------|-----|
| 1117 `clearance_*_zone` (kct) / 507 `clearance` + zone shorts (kicad-cli) | **Board-file provenance**, not a defect | Stale pours + generic embedded rules; the board is a routed static artifact whose `(setup)` never carried tier1 rules. 2026-07-04 checked with a refilled tier1 capability project. |
| 199 `track_width` / 199 `drill_out_of_range` / 199 `via_diameter` / 5 `copper_edge_clearance` | **Tier-config mismatch** | KiCad's generic defaults (0.2/0.3/0.5) are stricter than tier1's real floors (0.13/0.2/0.45). These are legal tier1 geometry flagged only because the board embeds no tier1 rules. |
| 49 `dimension_drill_clearance` (kct) / 27 `hole_clearance` (kicad-cli) | **Board-file provenance / known referee items** | Hole-to-hole spacing; LAYOUT_NOTES carried `hole 1` as an accepted NT3 net-tie artifact at sign-off. Bare-default checking amplifies these. |
| 11 `courtyards_overlap`, 95 unconnected (referee) | **Advisory / known-accepted** | Present and accepted at 2026-07-04 sign-off per LAYOUT_NOTES. |
| kct-vs-kicad count difference (1167 vs 1147) | **kct drift (benign)** | Two independent DRC engines with different rule taxonomies and sliver/zone accounting; both agree on the *root cause* (rules + stale fills), not on exact counts. |

**No board edits were made.** The board is exactly as committed on
`feat/revA-manufacturing-completion`. The gap is a *provenance/tooling* gap: the
committed `.kicad_pcb` was never re-saved with tier1 board-setup rules baked in,
and today's checks (correctly) refuse to assume a tier the board file doesn't
declare.

> **Follow-up recommendation for chorus (owner-side, not this PR):** re-open the
> v22 board in KiCad, load the jlcpcb-tier1 design rules into Board Setup, refill
> zones, and re-save — so the standalone `.kicad_pcb` self-certifies at tier1
> without an external referee project. That would make a bare
> `kct check --mfr jlcpcb-tier1` / `kicad-cli … --refill-zones` reproduce the
> 2026-07-04 clean result directly. This is a chorus board-hygiene task, not a
> kicad-tools bug.

---

## 5. Vendored `.kct/ci` gate results

### `check_routed_drc.py`

**Functional — ran and gated correctly.** At the default `--mfr jlcpcb` with
`--allow 0` it reported `1247 blocking error(s)` and failed (exit 2) with the
correct GitHub-Actions `::error::` annotation.

**Friction found → fixed in this PR:** the gate hardcoded `--mfr jlcpcb` with the
*only* override path being a `manufacturers:` map inside
`.github/routed-drc-tolerance.yml` — a kicad-tools-repo-internal file that the
vendored README explicitly tells consumers **not** to rely on. So a fresh
consumer repo (chorus) had **no way** to gate a tier1 board at tier1 without
authoring a repo-internal allowlist YAML just to name a profile. Added a
`--mfr TIER` CLI flag (mirrors the existing `--allow N` per-invocation override;
precedence: `--mfr` > YAML `manufacturers:` map > `jlcpcb` default). Now:

```
uv run python .kct/ci/check_routed_drc.py <board>.kicad_pcb --mfr jlcpcb-tier1 --allow 0
```

Covered by new tests in `tests/test_check_routed_drc_advisory.py::TestMfrCliOverride`
and documented in the vendored `.kct/ci/README.md` (installer heredoc updated).

### `check_copper_lvs.py`

**Functional — ran and behaved exactly as designed.** The producer
(`python -m kicad_tools.lvs.copper_lvs <sch> <routed_pcb>`) emitted the
**vacuity-guard verdict** (`clean=false`, single `kind='vacuous'` mismatch,
`bound_pads=0` vs `board_pads=289`): the v22 `_mfg.kicad_pcb` (from the
2026-07-04 branch) does **not** bind to chorus's current-`main` schematics — no
shared netlist/UUID provenance, so zero pads bind. The gate correctly reports
this as non-clean by default, and its purpose-built `--expect-vacuous` mode
returns exit 0 with `copper-LVS vacuity guard fired as expected`.

**Honest scope note:** chorus has **no LVS schematic pairing for this board**, so
the copper-LVS gate cannot do a real short/open comparison here — it can only
confirm (via the vacuity guard) that the board is schematic-unbound. This is a
property of the chorus fixture (a standalone manufacturing PCB), not a gate
defect. In a consumer repo whose routed PCB is generated from a paired schematic,
this gate performs a genuine copper-LVS assertion.

---

## 6. Friction list (real-consumer-repo findings)

1. **No root `pyproject.toml`** — installer fails fast (by design). A consumer
   must `uv init`-equivalent first. *Mitigation:* documented; a minimal
   hand-written `pyproject.toml` is sufficient and coexists with `package.json`.
   Candidate epic follow-up: an installer `--bootstrap-pyproject` convenience
   flag (out of scope here).
2. **`check_routed_drc.py` had no `--mfr` escape hatch** for non-default tiers in
   a fresh consumer repo. **Fixed in this PR** (new `--mfr` flag + tests + README).
3. **`check_copper_lvs.py` needs a schematic-paired routed board** to do real
   LVS. Standalone manufacturing artifacts only exercise the vacuity guard.
   Working as intended, but worth calling out for consumers whose "board" is a
   fab artifact rather than a recipe output.
4. **Board self-certification gap (chorus-side, not kicad-tools):** the committed
   v22 `.kicad_pcb` carries no embedded tier1 design rules, so bare checks fall
   back to KiCad generic defaults. A consumer expecting `kct check --mfr <tier>`
   to reproduce an external-referee sign-off must first bake the tier rules into
   Board Setup and re-save. See §4 follow-up recommendation.
5. **Tool artifacts:** `uv sync` creates `.venv` (gitignored — harmless) and
   `uv.lock` (committed to the pilot branch as a legit install artifact);
   opening the board with `kct`/`kicad-cli` creates a `.kicad_prl` (KiCad
   project-local settings) — this was removed so chorus's working tree was
   restored to its exact pre-pilot state.

---

## 7. Chorus end-state (for owner review — nothing pushed)

- **Original branch restored:** chorus is back on `main` with a working tree
  matching its pre-pilot state (only pre-existing untracked scratch
  `.june20-port/` and `hardware/chorus-test-revA/output/` remain; `.venv` is
  gitignored).
- **Pilot branch preserved locally:** `pilot/kct-install-4058` @ `1e3267c`, **no
  upstream tracking** (confirmed `git branch -vv` shows no `[origin/...]`).
  Nothing was pushed to `git@github.com:project-shamrock/chorus.git`.

**Pilot branch diffstat vs `main` (38 files, +72445 / -1):**

- Install layer (13 files, +2497 / -1): `pyproject.toml`, `CLAUDE.md` (+guarded
  block), `.claude/commands/kct/*` (5), `.kct/ci/*` (4), `.kct/install-metadata.json`,
  `uv.lock`.
- v22 board + bundle (25 files): `chorus-test-revA_v22_mfg.kicad_pcb` (69,250
  lines) + `manufacturing_v22/` (manifest, reports, BOM/CPL, gerbers.zip,
  kicad_project.zip, images, LAYOUT_NOTES).

To inspect:

```
git -C /Users/rwalters/GitHub/chorus log --oneline main..pilot/kct-install-4058
git -C /Users/rwalters/GitHub/chorus diff --stat main pilot/kct-install-4058
```

---

## 8. Installer fix shipped with this PR

`scripts/ci/check_routed_drc.py`: added a `--mfr TIER` CLI flag — a
per-invocation manufacturer-profile override (precedence `--mfr` > YAML
`manufacturers:` map > `jlcpcb` default), so a vendored consumer repo can gate a
non-default-tier board at its real tier without authoring a repo-internal
allowlist YAML. `scripts/install-kct.sh`: vendored `.kct/ci/README.md` now
documents the `--mfr` usage. Tests:
`tests/test_check_routed_drc_advisory.py::TestMfrCliOverride` (default is
`jlcpcb`; `--mfr jlcpcb-tier1` threads through to `kct check`; `--help`
advertises the flag).
