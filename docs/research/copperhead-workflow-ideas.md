# Evaluation: copperhead (AI PCB-design agent) — adoptable workflow ideas

**Issue**: #4520
**Date**: 2026-07-31
**Upstream**: https://github.com/chouhanindustries/copperhead (cloned at `v0.9.0`, 37 stars, last push 2026-07-31)
**Upstream license**: Apache-2.0 (kicad-tools is MIT — Apache-2.0 → MIT is *not* a clean copy direction, so this is an **ideas-only** survey; no code is vendored)
**Upstream stack**: TypeScript / Node ≥ 20, shells out to `kicad-cli` for ERC/DRC, hand-rolls an s-expression reader in `src/kicad/sexp.ts`

## TL;DR

Copperhead is "cursor for circuit boards": a natural-language brief drives
direct edits to `.kicad_sch` / `.kicad_pcb` s-expressions, gated by an
LLM-free `check` and wrapped in a git snapshot that auto-rolls-back on failure.
Its *process design* is more mature than the "Phase 1, ~37 stars" framing in
the issue suggests (it ships a constraints registry, a pre-commit hook that
runs `copperhead check`, and a per-run audit trail) — but it deliberately stops
where kicad-tools is deep: it does **not** route, and its verification is
`kicad-cli` ERC/DRC only, with no LVS, no native router, no manufacturing
cross-gate. We already own the hard half.

Verdict per idea (detail + prior-art grounding below):

1. **Design memory as first-class artifacts** — **ADAPT.** We have
   `/kct:layout-journal` (LAYOUT_NOTES.md) but only as an opt-in per-session
   skill; copperhead scaffolds append-only `DECISIONS.md` + `CHANGELOG.md` per
   repo. Adapt: scaffold these per board recipe. Don't adopt the
   read-before-edit *enforcement* — Loom owns orchestration.
2. **Two invariants (spec-gate + tools-agree)** — **SPLIT.** "Nothing done
   until tools agree" is **already covered** (our `kct check` + mandatory
   `kicad-cli pcb drc` cross-gate, the 100%-LVS/0-DRC bar). "Nothing starts
   without a spec" is a **genuine gap** in board recipes → **ADAPT** the
   spec-precondition half only.
3. **LLM-free doc-drift gate** — **ADOPT.** Genuine gap. A `kct check`
   sub-gate that diffs docs (BOM / target-fab claims) against the committed
   board catches the staleness class we keep finding by hand.
4. **Git-snapshot rollback around mutations** — **ADAPT (narrowly).** Genuine
   gap for *direct* invocation; a `--transactional` wrapper for in-place
   `.kicad_pcb` mutations. Note the Loom-worktree boundary is a *different*
   safety net that doesn't cover human/agent callers outside Loom.
5. **`doctor` environment preflight** — **ADOPT (extend, don't create).**
   `kct doctor` **already exists** but only checks version-record drift.
   Extend it with an env-preflight check (kicad-cli presence/version, native
   router `.so` state via existing `build-native --check`).
6. **`--json` machine output as a global flag** — **ADAPT (audit +
   consolidate).** Not greenfield: machine output is already widespread but
   split across **two inconsistent idioms** (`--json` boolean vs
   `--format {text,json}`). The work is an audit + consolidation, not a
   "should we" decision.
7. **Interop (kct as a backend for agents like copperhead)** — **NOTE, no
   adopt/skip verdict.** A judgment call with no code deliverable now; see the
   dedicated section. The reverse direction is already cross-posted upstream
   (copperhead#161).

**Net:** one already-covered (2a), one adopt (3), one adopt-by-extension (5),
three adapts (1, 2b, 4, 6), one interop note (7). Six follow-up issues filed
(see "Follow-up issues filed" at the end), each `loom:triage` only.

## What copperhead actually does (mechanisms, not the README)

Read from the cloned source so the dispositions below are grounded in the real
implementation rather than the marketing surface:

| Mechanism | Where | What it actually does |
|-----------|-------|-----------------------|
| Docs-as-memory scaffold | `src/memory/scaffold.ts` | `copperhead init` writes `DECISIONS.md` ("Append-only. Every non-trivial agent decision lands here… Entries are never rewritten.") and `CHANGELOG.md` ("Append-only, newest first. One entry per committed copperhead run."), plus a `constraints.json` registry and a per-run `.copperhead/runs/` audit trail. It also installs a `pre-commit` hook that runs `copperhead check` (idempotent; never clobbers a user hook). |
| LLM-free composite `check` | `src/commands/check.ts` | ERC + DRC (via `kicad-cli`) + doc-drift + `openspec validate` + a forbidden-pin constraint check. Contractually never imports a provider — CI-safe. |
| Doc-drift gate | `src/memory/drift.ts` | Parses fixed-column `BOM.md` / `PINOUT.md` tables and diffs claimed value/footprint/refdes against symbols read from the schematic; free-prose docs are *not* checked (only the parseable table contract). Carves out the zero-symbol bootstrap state to avoid deadlocking the create pipeline. |
| Spec gate | `src/openspec/cli.ts`, `check.ts` | `openspec validate` is run as a subprocess and folded into `check`; the create pipeline owns a propose → validate → archive flow. This is the "nothing starts without a spec" invariant. |
| Snapshot / rollback | `src/util/git.ts`, `src/agent/loop.ts` | Every run calls `snapshot(repo)` (records HEAD + stashes untracked-but-not-ignored files that `git stash create` misses) before editing; on verification failure it calls `preserveFailedRun()` (stashes the failed work as evidence) then `restore()` (`git reset --hard` + `git clean -fd` back to the snapshot). A `gitPreflight` refuses to run in a non-git repo, an unborn HEAD, or a dirty tree (without `--allow-dirty`). Worst case of a failed run is a no-op. |
| `doctor` env preflight | `src/commands/doctor.ts` | LLM-free, network-free probes: node version, `kicad-cli` presence/version, git version, model-provider credentials/privacy, and project config validity. Each probe fails soft (a missing tool is a reported `fail`, never a throw). |
| `--json` contract | `src/cli.ts` | `--json` is a single **global** commander option; every command checks `program.opts().json` and prints `JSON.stringify(res)`. Comment in source: "--json is a contract, not a suggestion." |

## Disposition table

| # | Idea | Verdict | Existing prior art in kicad-tools | Rationale (one line) |
|---|------|---------|-----------------------------------|----------------------|
| 1 | Design memory as first-class repo artifacts (append-only DECISIONS + CHANGELOG) | **Adapt** | `/kct:layout-journal` → `LAYOUT_NOTES.md` (`README.md`, `.claude/commands/kct/README.md`) — opt-in per hand-routing session, not scaffolded per recipe, not append-only-enforced | Scaffold a per-board decision log; skip the read-before-edit enforcement (Loom's job) |
| 2a | "Nothing is done until tools agree" | **Skip (already covered)** | `kct check` + mandatory `kicad-cli pcb drc --refill-zones` cross-gate; the 100%-LVS / 0-DRC manufacturable bar | This *is* our house rule already — nothing new to adopt |
| 2b | "Nothing starts without a spec" (validated proposal gate before mutation) | **Adapt** | None in board recipes | A lightweight precondition: a spec/brief artifact must exist before a recipe mutates files |
| 3 | LLM-free doc↔artifact drift gate | **Adopt** | None | Diff BOM / fab-target claims in docs against the committed board; catches the staleness class we fix by hand |
| 4 | Git-snapshot rollback around mutations | **Adapt (narrow)** | Loom builder worktrees give transactionality *for Loom runs only* — not for direct human/agent invocation | Add a `--transactional` snapshot/rollback wrapper for in-place `.kicad_pcb` mutation |
| 5 | `doctor` environment preflight | **Adopt (extend)** | `kct doctor` **exists** (`src/kicad_tools/cli/commands/doctor.py`, `src/kicad_tools/doctor.py`) but checks only version-record drift; `kct build-native --check` checks the router `.so` | Extend `kct doctor` with an env-preflight check; fold in `build-native --check` |
| 6 | `--json` machine output everywhere | **Adapt (audit)** | ~19 explicit `"--json"` boolean flags + ~105 files using `--format {text,json}` across `src/kicad_tools/cli/` | Not "should we" — audit prose-only commands and consolidate the two idioms |
| 7 | Interop (kct as a backend for copperhead-like agents) | **Note (no verdict)** | Reverse cross-post already filed at copperhead#161 | Judgment call, no code deliverable now — see interop section |

## Per-idea detail and smallest next step

### 1. Design memory as first-class artifacts — Adapt

**Prior art.** `/kct:layout-journal` already produces `LAYOUT_NOTES.md` during
hand-routing sessions, and `/kct:ee-review` produces an advisory decision
document. But both are *opt-in skills a human/agent chooses to invoke*, scoped
to a single session — there is no per-board, always-present, append-only
decision log the way copperhead scaffolds `DECISIONS.md` + `CHANGELOG.md` at
`init` time.

**What to take.** The *scaffold*, not the enforcement. Copperhead makes the
agent read these before editing and installs a `pre-commit` hook — that
read-before-edit gating is orchestration, and Loom already owns orchestration
here (explicit non-goal in the issue). What's worth adopting is the
convention that a board carries a first-class, append-only decision log as a
committed artifact, seeded when the board is scaffolded.

**Smallest next step.** Extend `/kct:board-recipe-scaffold` (or `kct board`
recipe scaffolding) to drop a `DECISIONS.md` stub (append-only header, one
entry per material design choice) and fold the existing `LAYOUT_NOTES.md`
convention into the same per-board memory directory. No new gating; purely
additive scaffolding. ~1 skill/template change + doc.

### 2. Two invariants — split verdict

**2a "nothing done until tools agree" — skip, already covered.** This is
verbatim our manufacturing-readiness rule: `kct check` plus the mandatory
`kicad-cli pcb drc --refill-zones` cross-gate, and the standing bar that
"manufacturable = 100% nets routed + 0 DRC + 0 sync drift" (two-engine DRC
honesty). Copperhead's version is *weaker* than ours (ERC/DRC only, no LVS, no
manufacturing cross-gate), so there is nothing to import here.

**2b "nothing starts without a spec" — adapt.** Copperhead refuses to mutate
files until a validated spec exists (`openspec validate`, folded into `check`).
Our board recipes have no equivalent precondition: `kct route` / `route-auto`
/ recipe pipelines will happily mutate a board with no captured intent. We do
**not** want to adopt openspec itself, but the *precondition* is sound.

**Smallest next step (2b).** A recipe precondition that a board's intent
artifact (e.g. an `ENGINEERING_PLAN.md` / recipe spec / brief) exists and is
non-empty before a mutating recipe stage runs — advisory warning first, opt-in
hard gate later. Scoped to recipe entry points, not every `kct` command.

### 3. LLM-free doc-drift gate — Adopt

**Prior art.** None. This is the cleanest genuine gap. Copperhead's
`drift.ts` diffs fixed-column `BOM.md` / `PINOUT.md` tables against symbols
read from the schematic and reports `doc claims "X" but actual is "Y"`.

**Concrete motivating case (from the issue).** The stale Seeed-vs-JLC fab
target in chorus's `ENGINEERING_PLAN.md` — a doc claim that silently decayed
away from the committed board. We keep finding this class by hand.

**What to take.** A `kct check` sub-gate (LLM-free, CI-safe) that verifies a
small, *parseable* contract of doc claims against the committed artifacts:
BOM refdes/value/footprint against the schematic, and stated fab-target /
layer-count against the board. Copy copperhead's discipline of checking only
a fixed-column table contract (never free prose) so the gate is deterministic
and doesn't fight the author.

**Smallest next step.** Add `kct check --docs-drift` (or a sub-check in the
existing composite `check`) that parses one fixed BOM table format and diffs
value/footprint/refdes against the schematic, emitting mismatches in both text
and `--json`. Ship with the chorus `ENGINEERING_PLAN.md` stale-target case as
the regression fixture. Follow copperhead's zero-symbol bootstrap carve-out to
avoid deadlocking scaffolding.

### 4. Git-snapshot rollback around mutations — Adapt (narrow)

**Prior art / boundary distinction (important).** In the Loom builder flow,
`route/complete` and recipe pipelines already run inside a throwaway git
worktree — a failed run is discarded by throwing the worktree away. That is
"safe **because it's in a throwaway worktree**," which is a *different*
transactionality boundary than copperhead's "safe **because the tool itself
snapshots and rolls back**." kct is also invoked **directly by humans and
agents outside any Loom worktree**, mutating a live `.kicad_pcb` in place —
and for those callers there is no rollback today.

**What to take.** Copperhead's per-command `snapshot()` / `restore()` pattern
(record HEAD, stash untracked-but-not-ignored files, hard-reset on failure,
preserve the failed work as a stash for forensics). Notably it snapshots
untracked files that `git stash create` misses — a real correctness detail
worth copying if we build this.

**Smallest next step.** A `--transactional` flag on the in-place mutation
commands (`kct route`, recipe stages) that snapshots HEAD + untracked state
before mutating and rolls back on non-zero exit, leaving the failed attempt in
a stash. Requires a git repo + at least one commit (copperhead's
`gitPreflight` is the model). Direct-invocation callers get the worktree-grade
safety Loom builders already enjoy.

### 5. `doctor` environment preflight — Adopt (extend, don't create)

**Prior art.** `kct doctor` **already exists**
(`src/kicad_tools/cli/commands/doctor.py` + `src/kicad_tools/doctor.py`,
issue #4347) but its only check is **version-record drift** between the
installed package and a consumer repo's stamped records. It does *not* probe
`kicad-cli` presence/version, the native router `.so` build state, or any
runtime prerequisite. Separately, `kct build-native --check` already reports
the router `.so` state (CLAUDE.md "Routing performance"), and the README
"Fresh worktree checklist" enumerates setup steps by hand.

**What to take.** Copperhead's `doctor` check *shape*: LLM-free,
network-free, fail-soft probes (a missing tool is a reported `fail`, not a
throw) with `--json` output. The framing for us is explicitly **"extend
`kct doctor`,"** not "create a new command."

**Smallest next step.** Add an env-preflight check group to `kct doctor`:
`kicad-cli` on PATH + version, native router `.so` state (call through the
existing `build-native --check` logic), and KiCad version compatibility.
Keep the existing version-drift check; add `--json` parity. This removes the
recurring "router is slow / fixture missing" false-alarm class the issue
calls out.

### 6. `--json` machine output — Adapt (audit + consolidate)

**Prior art / the real finding.** Machine-readable output is already an
established convention, so this is **not** a "should we" question. But it is
split across **two inconsistent idioms**: ~19 explicit `"--json"` boolean flags
(e.g. `sch_symbol_info.py`, `placement_cmd.py`, `export_netlist.py`, `mfr.py`)
and ~105 files that use a `--format {text,json}` choice instead. Copperhead's
lesson is that machine output should be **one global contract**, not a
per-command opt-in with two spellings.

**What to take.** The audit discipline. The deliverable is (a) an inventory of
which `kct` subcommands still emit prose-only with no machine path, and (b) a
decision to standardize on one idiom (`--format json` is the more prevalent
and more extensible choice; `--json` can remain a documented alias).

**Smallest next step.** File the audit: enumerate all 220 subparsers, tag each
as `--json` / `--format json` / prose-only, and land a short design note
picking one canonical idiom. Implementation (adding the missing surfaces) is a
separate mechanical follow-up once the idiom is settled.

### 7. Interop — Note (no adopt/skip verdict)

This idea has no code deliverable in this issue's scope, so per the acceptance
criteria it is addressed here as a paragraph rather than dropped.

Copperhead shells out to `kicad-cli` for verification and hand-rolls an
s-expression reader (`src/kicad/sexp.ts`); its verification is ERC/DRC only.
kicad-tools' round-trip s-expression parser, editor layers, native router,
LVS, and manufacturing cross-gate are all far deeper. That asymmetry is exactly
what makes interop plausible **in the direction of kicad-tools as a backend**:
a stable `kct --format json` surface (idea 6) would let an agent like
copperhead delegate the hard parts (routing, DRC/LVS, tapeout) to kct instead
of reimplementing them. The reverse direction is already in motion — the owner
cross-posted copperhead#161 suggesting they point an agent at kicad-tools to
mine our s-expression round-trip parser and KiCad-format gotchas (MIT →
Apache-2.0 is a clean copy direction that way).

**Recommendation:** do not pursue interop as a work item now, but treat idea 6
(the `--json`/`--format json` audit and consolidation) as the enabling
precondition. If interop is ever pursued, the entry point is a documented,
versioned machine-output contract on the routing / check / tapeout commands —
not a bespoke integration. No follow-up issue is filed for #7 itself; it rides
on #6.

## License note

Copperhead is Apache-2.0; kicad-tools is MIT. Apache-2.0 → MIT is not a clean
copy direction, so **no copperhead code is copied** — this is an ideas-only
survey, consistent with how `component-importer-for-kicad-evaluation.md`
handled its cross-license evaluation. Any follow-up implementation must be
clean-room, built on kicad-tools' own primitives (`kicad_tools.sexp`,
`kct check`, `kct doctor`), and should credit copperhead as the design source
in the PR description. The reverse flow (copperhead borrowing from kicad-tools'
MIT code) is clean and is already cross-posted at copperhead#161.

## Acceptance-criteria mapping

Against the issue's Revised Acceptance Criteria:

1. **All 7 ideas dispositioned with rationale** — see the disposition table and
   per-idea detail. 2a skip / 2b adapt / 1,4,6 adapt / 3,5 adopt / 7 note.
2. **Idea 7 (interop) explicitly addressed** — see the dedicated "Interop"
   section; it is a note (no adopt/skip verdict) that rides on idea 6, not a
   silent drop.
3. **One follow-up issue per adopt/adapt idea, `loom:triage` only** — six
   filed (ideas 1, 2b, 3, 4, 5, 6); see "Follow-up issues filed" below. Idea 2a
   is already-covered (no issue); idea 7 rides on idea 6 (no separate issue).
4. **No verbatim code copied (Apache-2.0 → MIT)** — see "License note";
   ideas-only, clean-room for any follow-up.

## Follow-up issues filed

Each carries `loom:triage` only (no self-curation in this same pass, per
curator convention):

| Idea | Verdict | Follow-up issue |
|------|---------|-----------------|
| 1 — per-board append-only decision log | Adapt | #4538 |
| 2b — spec-precondition gate for mutating recipes | Adapt | #4539 |
| 3 — LLM-free doc-drift sub-gate (highest leverage) | Adopt | #4540 |
| 4 — `--transactional` snapshot/rollback wrapper | Adapt | #4541 |
| 5 — extend `kct doctor` with env preflight | Adopt (extend) | #4542 |
| 6 — audit + consolidate machine-output idiom | Adapt (audit) | #4543 |

Idea 2a ("nothing done until tools agree") is already covered — no issue.
Idea 7 (interop) is a note that rides on idea 6 (#4543) — no separate issue.

## Recommendation

**Mine the process ideas; don't touch the agent loop.** Copperhead's value to
us is its *discipline conventions*, exactly the ones the issue flagged: an
append-only decision log, a doc-drift gate, a snapshot/rollback wrapper, and a
real env-preflight `doctor`. Three of those (doc-drift #3, doctor-extension #5,
json-audit #6) are unambiguous wins with clear prior-art anchors; two (memory
scaffold #1, spec-precondition #2b) are lightweight scaffolding adaptations;
one (transactional wrapper #4) closes a real gap for direct callers. We should
adopt **none** of copperhead's orchestration or LLM-provider routing — Loom
owns that, and our verification stack is already deeper than theirs. The
highest-leverage single follow-up is the **doc-drift gate (#3)**: it's a
genuine gap, it's LLM-free and CI-safe, and it directly attacks the stale-doc
class we keep fixing by hand.
