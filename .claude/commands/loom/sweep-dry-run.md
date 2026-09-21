# Sweep — Dry-run gate

> **Reference file for [`sweep.md`](sweep.md)**, the `/loom:sweep` dispatcher.
>
> **Load when:** `--dry-run` is present (any mode). The gate prints the plan and EXITs, so nothing after it is loaded on that run.
>
> **Flat, one level deep.** `sweep.md` names every file a given run needs up
> front; nothing here requires opening a *third* file to follow its own
> procedure. Cross-references worded "above"/"below" that do not resolve inside
> this file point at a sibling `sweep-*.md` section — see the reference-file map
> in [`sweep.md`](sweep.md). The body below is **verbatim** from the pre-split
> `sweep.md` (#7726): no rule, warning, edge case, table, or code block was
> reworded, softened, reordered, or dropped.

## Contents

- [0. Dry-run gate (if `--dry-run`)](#0-dry-run-gate-if---dry-run)
  - [Procedure — Modes A and B (issue-set)](#procedure--modes-a-and-b-issue-set)
  - [Procedure — Mode C (PR-set)](#procedure--mode-c-pr-set)
  - [Out of scope for dry-run output (all modes)](#out-of-scope-for-dry-run-output-all-modes)

---

## 0. Dry-run gate (if `--dry-run`)

If `--dry-run` was supplied, **this stage runs before any mutation** and EXITs after printing the plan. The dry-run gate is the single inviolable contract of `--dry-run`: no label edits, no `worktree.sh` invocation, no `gh pr create`, no `merge-pr.sh`, no daemon-state writes, no Task/subagent dispatch. This contract is uniform across Modes A, B, and C.

**`--dry-run --yes` needs no special-case code.** This stage runs and EXITs *before* the mandatory confirmation-gate machinery (see "`--yes` (non-interactive confirmation)" in `sweep-arguments.md`) is ever reached, so `--dry-run` always wins regardless of whether `--yes` is also present — as long as `--yes` is parsed as an ordinary bare flag token, stripped before mode classification like every other flag, `--dry-run --yes` "just works" with zero bespoke interaction handling.

### Procedure — Modes A and B (issue-set)

1. **Survey each candidate (read-only).** For every deduplicated, validated issue number `N` in the candidate list:
   ```bash
   "$GH_READ" issue view N --json number,title,labels,state --jq '{number, title, state, labels: [.labels[].name]}'
   ```
   This is a `gh issue view` read — it does not mutate anything. It runs through the cached-read wrapper (see "Cached forge reads (`gh-cached`)"): a dry-run survey is pure observation whose output is a printed plan, never a claim, so 30s of staleness costs nothing. The **live** path's per-issue pre-flight (step 1 of the Wave Lifecycle) deliberately does *not* use the wrapper. (If `gh` is unauthenticated or the issue is unreachable, log the error against that candidate and continue surveying the rest.)

   **Add `body` to this read unconditionally** (`gh issue view N --json number,title,labels,state,body ...`) — one extra `--json` field, **no extra API call** — and parse its `## Affected Files` section into the candidate's estimated file surface per "Overlap-aware wave partitioning" step 1. A missing / "To be determined" section leaves the surface *unknown* (that candidate is excluded from overlap analysis; never blocked). The `body` fetched here also feeds the `--auto-stack` edge-detection pass when that flag is set (see below), so it is read once and used for both. **Under the `all` sentinel it also feeds the "Operator-gate advisory scan"** (step 1c below — phrase scan + operator-only-dependency check) — same read, no extra survey call, and independent of `--auto-stack`.

   **When `AUTO_STACK=true`, run the edge-detection pass** described in "Auto-stack detection and wave ordering (`--auto-stack`, #3759)" over the same `body`. Absent `--auto-stack`, no stacking detection runs — the `body` read still feeds overlap surface estimation only, and **no stacking edge is ever created from file overlap** (scheduling signal only, #3729).

1a. **Resolve stacking edges (only when `AUTO_STACK=true`).** Detect `Depends on #A` / `Requires #A` edges, keep only those whose `#A` is a member of this candidate set, reduce to a single parent per child (first-match-wins), drop cyclic edges — all per "Auto-stack detection and wave ordering". Populate the per-issue `DEPENDS_ON[N]` map. When zero edges survive, the run proceeds exactly as if `--auto-stack` were absent.

1b. **Warn on out-of-set dependency references (unconditional, Modes A/B).** Run the detect-and-warn pass described in "Out-of-set dependency detect-and-warn (v2 item 4, #3747)": `./.loom/scripts/warn-out-of-set-deps.sh --candidates "<resolved candidate numbers>" --depends-on "<operator --depends-on values, if any>"`. For each candidate whose body declares `Depends on`/`Requires`/`Part of #A` where `#A` is **open**, **not** in this sweep's candidate set, and **not** covered by an operator `--depends-on`, it emits a non-blocking advisory warning (stderr/log; also surfaced in the candidate-set preview in interactive/Mode B contexts). This runs regardless of `--auto-stack` — it never modifies the candidate set (detection + advisory only) and never blocks the sweep. In the `--dry-run` plan the warnings are printed above the wave listing.

1c. **Scan for operator-gated candidates (only when `SWEEP_ALL_AGGRESSIVE=true`, #5137, extended #6391).** Run `./.loom/scripts/warn-operator-gated.sh --candidates "<resolved candidate numbers>"` (see "Operator-gate advisory scan" above) over the same `body` read in step 1, plus its own per-candidate `title` read for the title-prefix signal. Record each matching candidate's annotation line(s) for step 3's plan output. Advisory only — never modifies the candidate set, never changes a planned action, never blocks. Absent the `all` sentinel (Mode B's own curated candidate set), this step does not run.

2. **Compute wave partition.** Partition the candidate list into waves of size `--builders-per-wave`, or the Stage -1 resolved auto wave size when the flag was omitted (see "Resolve auto wave size"), preserving input order. Record `(issue, wave_index, total_waves)` for each candidate. Apply the same silent-clamp and pre-flight-skip rules that the live path uses (closed / `loom:building` / `loom:blocked` issues are tagged as "would skip" in the plan but still appear in the output for transparency). **When stacking edges were resolved in step 1a, first reorder** so every parent's wave is at or before its child's wave (a parent/child pair may share a wave — the child still branches off the parent's branch, not the shared pre-wave `main` snapshot) per "Auto-stack detection and wave ordering", then partition the reordered list.

2a. **Adjust the partition for file-surface overlap.** After the (possibly auto-stack-reordered) partition is computed, run the overlap adjustment in "Overlap-aware wave partitioning" step 2: detect same-wave pairs whose estimated `## Affected Files` surfaces share ≥1 path (excluding pairs already related via `DEPENDS_ON[N]` and any candidate with an unknown surface), and greedily reorder to separate them without breaking parent-before-child ordering. Record the resulting wave moves and any unavoidable-overlap groups for the plan output.

3. **Print the plan.** Emit a table or block per the issue-set format below, including the `Overlap analysis` block when any overlap was detected and the `Operator-gate advisory` block (only under the `all` sentinel) when step 1c found any match.

4. **EXIT.** Do not proceed to "Wave Lifecycle". The shell must return as soon as the plan is printed.

**Issue-set output spec** (Modes A and B; minimum useful — do **not** add token-pool selection or agent dispatch internals):

```
/loom:sweep --dry-run plan: M candidate(s) across W wave(s) (wave size 10, auto; mechanism=daemon detached-process)
  Wave sizing: daemon + multi-account pool → detached-process path (target 10)

  Wave 1:
    #123  "Add foo widget"                labels: loom:issue                    → would build
    #124  "Fix bar bug"                   labels: loom:curated                  → would curate, build
    #199  "Tweak gizmo"                   labels: loom:issue                    → would route to Judge (existing PR #200 in flight)
  Wave 2:
    #125  "Refactor baz module"           labels: loom:building                 → would skip (already in flight)
    #126  "Document quux"                 labels: (none)                        → would curate, build
    #198  "Polish frobnicator"            labels: loom:issue                    → would merge (existing PR #201 already loom:pr)
    #197  "Rework the widget pipeline"    labels: loom:issue                    → would skip (existing PR #202 is draft/unlabeled)

Total: 3 would-build, 1 would-route-to-judge, 1 would-merge, 2 would-skip. No issues were modified.
```

When `--builders-per-wave` was passed explicitly, the header shows the number without `auto` and the "Wave sizing" line reads `explicit --builders-per-wave=N` (no mechanism/disk reason). A disk- or candidate-clamped auto run reads e.g. `(wave size 3, auto; mechanism=in-session subagent)` with `Wave sizing: reduced to 3 (only 6 GB free on /Volumes/scratch/loom)`.

**Per-candidate fields (required):**
- Issue number
- Title (truncated reasonably if very long)
- Current labels (comma-separated, or `(none)`)
- Planned action (`would build`, `would curate, build`, `would skip (<reason>)`, `would route to Judge (existing PR #X in flight)`, `would merge (existing PR #X already loom:pr)`, `would skip (PR #X held by loom:operator)` — the linked PR is `loom:pr` but also carries `loom:operator`, per #6398; `would skip (existing PR #X is draft/unlabeled)` — the one open linked PR is a draft and/or has no actionable label, so neither Judge nor Builder is dispatched, per #8160). Under the `all` sentinel (`SWEEP_ALL_AGGRESSIVE=true`) the aggressive actions also appear: `would reclaim (stale loom:building), build`, `would unblock (#N merged), build`, `would skip (still blocked by #N)`, `would skip (explicit hold: "<phrase>")`, `would expand epic (→ #a #b)`, `would skip (needs decomposition)`, `would reclaim (stale loom:abort), build`, `would skip (abort flag set)`, `would skip (operator-only)`, `would skip (needs-capability)`, `would propose (mechanical lane, holds: <caps>)`, `would skip (mechanical: missing capability <name>)` (#6893).
- Wave assignment (shown via the `Wave N:` group header)
- **Operator-gate annotation (only under the `all` sentinel, appended, never replacing the planned action — #5137)**: when step 1c matched, append `⚠ body declares operator-gating: "<phrase>"` and/or `⚠ depends on #A, which is loom:operator-only` (or `loom:needs-capability`) after the planned action, one per matched signal. No match → no suffix, row unchanged from today.

**Header/footer (required):** the header states the resolved wave size (and whether it is `auto` or explicit), the chosen **mechanism** (`daemon detached-process` vs `in-session subagent`), and — on the second line — the one-line **gating reason** from "Resolve auto wave size". The footer states total candidates, total waves, count of `would-build` vs `would-skip`, and an explicit confirmation that nothing was modified. (Dry-run resolves the auto wave size via the same Stage -1 helper but performs no dispatch — it prints the plan and EXITs.)

**Detected stacking pairs block (only when `AUTO_STACK=true` and ≥1 edge survived).** When auto-stack resolved at least one in-set edge, print a `Detected stacking pairs:` block above the wave listing, one line per honored edge, naming the child, its declared dependency phrase, and the parent it will stack on:

```
Detected stacking pairs (--auto-stack):
  #125 "Fix Y"  — Depends on #124 (in this sweep's candidate set) → will stack on #124's branch (feature/issue-124)
  #126 "Add Z"  — Requires #125 (in this sweep's candidate set) → will stack on #125's branch (feature/issue-125)
```

Each stacked child's per-candidate action then reads e.g. `→ would build (stacked on #124)` and the wave grouping reflects the parent-before-child ordering. When `--auto-stack` was passed but **zero** edges survived (no in-set `Depends on`, or every candidate independent), print **no** stacking block — the plan is identical to a run without the flag. Dropped edges (a second in-set parent on the same child, or a cycle) are surfaced as one-line warnings above the block (e.g. `WARNING: #127 declares multiple in-set parents (#124, #125) — honoring #124 only (single-parent edges)` / `WARNING: dropped cyclic stacking edges among #128 #129 — building independently`).

**Overlap analysis block (only when ≥1 same-wave surface overlap was detected, #4161).** When step 2a found candidates whose estimated `## Affected Files` surfaces overlap, print an `Overlap analysis` block above the wave listing: one line per overlapping group naming the shared file(s) and the candidates, the wave move applied (if any), and an explicit `UNAVOIDABLE` marker + warning when the group could not be separated. Candidates with an unknown surface (no `## Affected Files`) are never listed here — the analysis only reasons about parseable surfaces.

```
Overlap analysis (file-surface scheduling, #4161):
  #38, #37 share `install.sh` — moved #37 to wave 2 (separated)
  #36, #38, #39 share `hooks/repo/tests/run.sh` — only 2 waves for 3 overlappers → UNAVOIDABLE
  WARNING: unavoidable same-file overlap on `hooks/repo/tests/run.sh` among #36 #38 #39
           — sibling PRs will report CLEAN until the first merges, then conflict.
           Reorder manually or re-run with --builders-per-wave 1. Step 7 revalidation
           is the reactive fallback (an extra Doctor rebase per collision).
```

When every overlapping group was separated by the reorder, print the moves without a `WARNING:` line. When no surfaces overlap (or every candidate's surface is unknown), print **no** overlap block — the plan is byte-for-byte identical to a run with no `## Affected Files` data. **No stacking edges are ever created here** — overlap is a scheduling signal only (#3729).

**Operator-gate advisory block (only under the `all` sentinel, and only when ≥1 candidate matched, #5137, extended #6391).** When step 1c's `warn-operator-gated.sh` pass matched at least one candidate, print an `Operator-gate advisory` block above the wave listing: one line per matching candidate per signal, naming the candidate, the matched phrase, dependency, or title prefix, and a pointer to that candidate's row in the listing below (which also carries the same `⚠` suffix — see "Per-candidate fields"):

```
Operator-gate advisory (body-text + title scan, #5137/#6391):
  #87 declares "operator-gated" — body: "the index is login-walled, so acquisition is operator-gated"
  #87 depends on #4, which is loom:operator-only — sweep will skip #4 and dispatch #87 anyway
  #65 declares "operator decision:" — body: "Operator decision: send this paired with the paper..."
  #94 title declares "Operator:" — title: "Operator: photograph the 1850 census page"
  ADVISORY ONLY — no candidate above was skipped, re-routed, or relabeled because of this block.
  Review before confirming; hold, --depends-on, or dispatch manually as appropriate.
```

Absent the `all` sentinel, this block never appears (Mode B/C do not run the scan — see "Operator-gate advisory scan"). Under the `all` sentinel with **zero** matches, print **no** block at all — the plan is byte-for-byte identical to a run made before this scan existed, matching the same "no block when clean" contract the `Overlap analysis` and `Detected stacking pairs` blocks already honor. **No candidate's planned action, label, or wave assignment is ever changed by this block** — advisory only, per "Operator-gate advisory scan" above.

### Procedure — Mode C (PR-set)

1. **Survey each PR candidate (read-only).** For every deduplicated, validated PR number `P` in the candidate list:
   ```bash
   "$GH_READ" pr view P --json number,title,labels,state --jq '{number, title, state, labels: [.labels[].name]}'
   ```
   This is a `gh pr view` read — it does not mutate anything. Cached, for the same reason as the Modes A/B survey above; Mode C's **live** C0 pre-flight is deliberately uncached. (If `gh` is unauthenticated or the PR is unreachable, log the error against that candidate and continue surveying the rest.)

2. **Compute wave partition.** Mode C waves are size-1 (`--builders-per-wave` is ignored). Each PR is its own wave. Record `(pr, wave_index=N, total_waves=M)` for each candidate. Apply the same skip rules the live path uses (closed PRs, multiple-label conflicts, missing required label all tagged "would skip" in the plan but still listed for transparency).

3. **Print the plan.** Emit the PR-set output spec below.

4. **EXIT.** Do not proceed to "PR-set Wave Lifecycle". The shell must return as soon as the plan is printed.

**PR-set output spec** (Mode C):

```
/loom:sweep --prs --dry-run plan: M candidate(s) across M wave(s) (PR-set mode, --builders-per-wave ignored)

  Wave 1:
    PR #200  "Add foo widget"                labels: loom:review-requested        → would Judge
  Wave 2:
    PR #201  "Fix bar bug"                   labels: loom:changes-requested       → would Doctor → Judge (cycle 1/max_doctor_cycles)
  Wave 3:
    PR #202  "Refactor baz"                  labels: loom:pr                      → would merge (via merge-pr.sh --auto)
  Wave 4:
    PR #203  "Polish frobnicator"            labels: (none)                       → would skip (no actionable label)
  Wave 5:
    PR #204  "Document quux"                 state: MERGED                        → would skip (PR already merged)

Total: 1 would-judge, 1 would-doctor-then-judge, 1 would-merge, 2 would-skip. No PRs were modified.
```

**Per-PR fields (required):**
- PR number (prefixed `PR #` to distinguish from issue numbers)
- Title (truncated reasonably if very long)
- Current labels (comma-separated, or `(none)`)
- Planned action (`would Judge`, `would Doctor → Judge (cycle 1/max_doctor_cycles)`, `would merge (via merge-pr.sh --auto)`, `would skip (<reason>)`). The `cycle 1/N` form substitutes the resolved `sweep.max_doctor_cycles` value for `N` (default 1).
- Wave assignment (one PR per wave; shown via the `Wave N:` group header)

**Footer (required):** total candidates, total waves, count of `would-judge` / `would-doctor-then-judge` / `would-merge` / `would-skip`, and an explicit confirmation that nothing was modified.

**Mode C skip reasons** (action column should clearly state which applies):
- `would skip (no actionable label)` — PR has neither `loom:review-requested`, `loom:changes-requested`, nor `loom:pr`.
- `would skip (PR already merged)` — `gh pr view` reports `state: MERGED`.
- `would skip (PR closed without merge)` — `state: CLOSED` (non-merged).
- `would skip (loom:blocked)` — PR carries `loom:blocked` (do not act on operator-flagged PRs).
- `would skip (multiple actionable labels)` — PR carries two or more of `{loom:review-requested, loom:changes-requested, loom:pr}` simultaneously (human-attention case — which transition is canonical?).
- `would skip (loom:operator)` — PR is `loom:pr` but also carries `loom:operator` (Champion's merge-risk hold, or another operator-applied hold); C1c does not route it to Merge (#6398).

### Out of scope for dry-run output (all modes)

**Explicitly out of scope for dry-run output** (do not add these — see Limitations):
- Token-pool / account selection internals
- Subagent dispatch order or parallelism counts beyond wave size
- Persisting the plan to disk
- Diffing this plan against a previous or actual sweep

**Verifying "nothing mutates":**

```bash
# EVERY `gh` read below is plain `gh` — NEVER "$GH_READ". This is a
# before/after differential check: the identical command runs twice around the
# operation under test, so a cache hit on the "after" read would replay the
# "before" value and make the check pass vacuously (#4667).
# Before:
LABELS_BEFORE=$(gh pr view P --json labels --jq '[.labels[].name]|sort')   # Mode C
ISSUE_LABELS_BEFORE=$(gh issue view N --json labels --jq '[.labels[].name]|sort')  # Modes A/B
PRS_BEFORE=$(gh pr list --state open --json number --jq '[.[].number]|sort')
WORKTREES_BEFORE=$(ls .loom/worktrees/ 2>/dev/null | wc -l)
# Run: /loom:sweep --dry-run ...   (any mode)
# All three (or four, for Mode C) must be unchanged after the dry-run returns.
```

These checks — label set per candidate (issue or PR), open PR set, worktree count — are the acceptance criteria. If any of them differ pre/post a `--dry-run` invocation, the dry-run gate is broken.

