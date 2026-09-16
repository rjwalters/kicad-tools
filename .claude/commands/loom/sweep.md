# Sweep

Process an explicit list of issues — **or an explicit/NL-described set of open PRs** — through the appropriate lifecycle from the current Claude session, no external daemon required. Runs sequentially by default, or in **parallel waves** of up to `N` builders when `--builders-per-wave N` is supplied (issue-set modes only). Supports `--dry-run` to preview the candidate plan without mutating anything.

> **Scope.** This skill accepts either an explicit list of issue numbers, a natural-language description of which issues to process, **or an explicit/NL-described list of open PRs** (Mode C, the "back half" of the lifecycle: Judge → Doctor → Merge per PR's current label). Runs the appropriate lifecycle in waves. Supports `--dry-run` to preview the plan without mutations. Other knobs sketched in #3298 are **deliberately deferred** — see "Limitations" below.
>
> If you need multi-account autonomous dispatch across many issues, use `/loom:loom` (it drives the `loom-daemon`). `/loom:sweep` is itself the single-issue lifecycle, and also covers the in-between case: "I have these N issues (or PRs), run them in this session, without spinning up a daemon."

## ⚠️ `--body @path` Does NOT Expand — It Posts the Literal String

If you (or a role you dispatch) post a comment via `gh issue comment` / `gh pr
comment` / `gh api ... comments` from a scratch file, `--body @path` (and `gh
api -f body=@path`) posts the literal string `@path`, not the file's contents.
**Full pitfall, incident citation, and fixes**:
[`comment-body-literal-path.md`](comment-body-literal-path.md).

## How to use this skill (progressive disclosure)

This file is a **dispatcher**. It carries only what every `/loom:sweep`
invocation needs: the scope note above, the reference-file map below, the
hard Constraints, and the pointer list. **Every procedure — argument parsing,
backend detection, the dry-run gate, both wave lifecycles, the summary
vocabulary — lives in a sibling `sweep-*.md` file in this same directory.**
Read the files the run actually reaches, and only those.

**Structure rules (do not violate when editing):**

- **One level deep, always.** A `sweep-*.md` file never requires opening a
  *third* file to follow its own procedure. Cross-references between siblings
  are pointers into files this dispatcher has already told you to load, not a
  chain.
- **Nothing was dropped in the split (#7726).** Every sibling's body is
  verbatim from the pre-split `sweep.md`. If a rule seems to be missing, it is
  in another sibling — not deleted. Find it before re-deriving it.
- **New sweep detail goes into the sibling that owns that phase**, not back
  into this dispatcher.

### Reference file map

| File | Lines | Load when |
|------|-------|-----------|
| [`sweep-arguments.md`](sweep-arguments.md) | ~377 | **Always, first.** Mode A / Mode B / Mode C classification, the `all` build-everything sentinel, every flag (`--builders-per-wave`, `--dry-run`, `--prs`, `--no-daemon`, `--claim-owned`, `--depends-on`, `--auto-stack`), edge cases, and validation rules. |
| [`sweep-examples.md`](sweep-examples.md) | ~155 | Optional. Worked invocation examples per mode, including the `all` sentinel and the Mode B clarification triggers. Never required to execute a run. |
| [`sweep-execution-model.md`](sweep-execution-model.md) | ~462 | **Always, before dispatching any subagent.** The three CRITICAL dispatch invariants (only Builders parallelize / async-only dispatch / one level deep), model selection + escalation ladder, credit- and spend-limit fallbacks, the Doctor-cycle cap, model-cost experiment mode, and the `gh-cached` read wrapper. |
| [`sweep-backend-detection.md`](sweep-backend-detection.md) | ~493 | **Always, at sweep start.** Step 0a (stable run id), Step 0b (peer-`/loom:sweep` detection), and Stage -1 (daemon-vs-subagent decision tree, the three probes, auto wave-size resolution, the daemon-dispatch path, smoke tests). |
| [`sweep-scheduling-signals.md`](sweep-scheduling-signals.md) | ~161 | Before the confirmation gate / first wave. Overlap-aware wave partitioning (#4161), the capability-aware `loom:operator-mechanical` lane (#6893 — **also needed in Mode C**, C0 references it), and the operator-gate advisory scan (#5137/#6391). |
| [`sweep-dry-run.md`](sweep-dry-run.md) | ~197 | `--dry-run` is present (any mode). The gate prints the plan and EXITs, so nothing after it is reached on that run. |
| [`sweep-mode-c-lifecycle.md`](sweep-mode-c-lifecycle.md) | ~183 | **Mode C only.** C0 pre-flight → C1 per-PR routing → C2 merge → C3 advance, plus the Mode C summary format. |
| [`sweep-wave-lifecycle.md`](sweep-wave-lifecycle.md) | ~662 | **Modes A and B only** (including the `all` sentinel). Baseline snapshot, checkpoint resume, steps 1-8a (pre-flight incl. Step 1a/1b lease handling, Curator, approval gate, Builder, stacking, Judge, Doctor, Merge, wave boundary). |
| [`sweep-summary-output.md`](sweep-summary-output.md) | ~81 | The run is settling. Summary table format, the `merged`/`blocked`/`skipped`/`rate-limited`/`completed externally` outcome vocabulary, and the transcript-archival completion hook. |
| [`sweep-run-hygiene.md`](sweep-run-hygiene.md) | ~158 | Stop conditions; the three advisory **pre-wave** checks (host sleep, main-branch freshness, outstanding quarantine stashes); the sweep-child working-set contract; peer-sweep / legacy-daemon / role-runner coexistence. |
| [`sweep-reference.md`](sweep-reference.md) | ~171 | Look-up only: the Limitations (deferred vs. implemented) status table, and the daemon event-bus wire contract — **required whenever the in-process `loom-daemon` is running**, since the sweep child must publish phase events onto its bus. |

`sweep-mode-c-lifecycle.md` and `sweep-wave-lifecycle.md` are **mutually
exclusive per run** — a run is either issue-set (Modes A/B) or PR-set (Mode C),
never both (mixed invocations are explicitly unsupported; see
`sweep-arguments.md` → "Mode C validation rules").

### Load order for a run

1. **`sweep-arguments.md`** — classify `$ARGUMENTS` into Mode A / Mode B /
   Mode C / the `all` sentinel, strip and store the flags, run the validation
   rules. Mode B/C candidate resolution (and its GraphQL→REST fallback) is
   here too.
2. **`sweep-backend-detection.md`** — Step 0a, Step 0b, then Stage -1.
   - `DECIDE = use_daemon` → dispatch each candidate via
     `mcp__loom__dispatch_sweep` and **exit**; only
     `sweep-run-hygiene.md`'s pre-dispatch advisory checks and
     `sweep-summary-output.md`'s archival hook also apply on that path.
   - `DECIDE = use_subagent` → continue below.
3. **`sweep-execution-model.md`** — read before dispatching any subagent
   (dispatch invariants, model resolution, Doctor-cycle cap).
4. **`sweep-run-hygiene.md`** — the three advisory pre-wave checks, run before
   the first wave (or before the first `mcp__loom__dispatch_sweep` call on the
   daemon path). Advisory only: never block on them.
5. **`sweep-scheduling-signals.md`** — overlap partitioning + operator-gate
   advisory annotations feeding the confirmation gate; the capability lane.
6. **`sweep-dry-run.md`** — only if `--dry-run`. Prints the plan and EXITs.
7. **The lifecycle for the resolved mode** —
   `sweep-wave-lifecycle.md` (Modes A/B) **or**
   `sweep-mode-c-lifecycle.md` (Mode C).
8. **`sweep-summary-output.md`** — transcript archival, then the summary table.

`sweep-reference.md` is loaded on demand: whenever the daemon is reachable (the
event-bus publish contract) or whenever you need the deferred-feature status
table. `sweep-examples.md` is loaded only to disambiguate an invocation.

### Section lookup — where a cited `sweep.md` section now lives

Other role prompts and docs cite sweep sections by name (`sweep.md` → "X").
Those sections all still exist, verbatim; the split (#7726) only moved them into
the siblings below. Resolve a citation here, in one hop:

| Cited section | Now in |
|---|---|
| "Arguments", "Mode A/B/C", "Optional flags", "Validation rules", "Build-everything sentinel (`all`)", "Aggressive candidate taxonomy", "GraphQL-exhaustion fallback", "Resolve the repository locally", "Forge write failure diagnosis" | `sweep-arguments.md` |
| "Examples" | `sweep-examples.md` |
| "Execution Model", "Only Builders parallelize", "Subagent dispatch is async-only", "ending your turn IS the kill signal", "One level deep", "Model selection for subagent dispatch", "Model escalation on Judge rejection", "Tier 2.5 — complexity marker", "Credit-exhaustion fallback", "Spend-limit fallback", "Doctor-cycle cap" (incl. "Distinct-defect exception"), "Model-cost experiment mode", "Cached forge reads (`gh-cached`)" | `sweep-execution-model.md` |
| "Sweep Run Identity + Peer-`/loom:sweep` Detection", "Step 0a", "Step 0b", "Stage -1: Backend detection", "Resolve auto wave size", "The daemon-dispatch path" | `sweep-backend-detection.md` |
| "Overlap-aware wave partitioning", "Capability-aware `loom:operator-mechanical` lane", "Operator-gate advisory scan" | `sweep-scheduling-signals.md` |
| "0. Dry-run gate" | `sweep-dry-run.md` |
| "PR-set Wave Lifecycle (Mode C only)", "C0"/"C1"/"C2"/"C3", "Mode C summary output" | `sweep-mode-c-lifecycle.md` |
| "Wave Lifecycle (Modes A and B only — issue-set)", "1. Per-issue pre-flight", "Step 1a", "Step 1b", "Existing-PR probe", "Checkpoint-driven resume", "Genuine no-op conclusion vs. builder failure", "Stacked dependency", "`--auto-stack` detection", steps 2-8a | `sweep-wave-lifecycle.md` |
| "Summary Output", "Session Transcript Archival" | `sweep-summary-output.md` |
| "Stop Conditions", "Host Sleep Readiness", "Main Branch Freshness", "Outstanding Quarantine Stashes", "Sweep Child Working-Set Contract", "Coexistence" (incl. "Modern daemon coexistence") | `sweep-run-hygiene.md` |
| "Limitations", "Daemon event bus" | `sweep-reference.md` |
| "Constraints", "Reference Documentation" | this file (below) |

## Constraints

- **Wave model, one level deep.** When `--builders-per-wave > 1` (Modes A/B only), dispatch `loom-builder` / `loom-judge` / `loom-doctor` subagents **directly from this orchestrator session** in a single tool-call block. In Mode C, dispatch `loom-judge` and `loom-doctor` as **single subagent Tasks** per PR (size-1 waves). **Never invoke `/loom:sweep`, `/loom:judge`, or `/loom:doctor` as a subagent from `/loom:sweep`** — that is the two-levels-deep pattern that triggers the #3289 stall. See "CRITICAL: One level deep" in the Execution Model.
- **Per-PR Judge is sequential within a wave.** Builders parallelize (Modes A/B); judges do not. Mode C inherits this: PRs are processed one per size-1 wave. Don't parallelize judges or PRs without a separate design pass.
- **Configurable Doctor→Judge cycle cap per PR (`sweep.max_doctor_cycles`, default 1).** Inline within the wave (Modes A/B issue-side and Mode C PR-side both enforce this). If Judge still requests changes after the configured number of Doctor passes, the PR is blocked — do not retry indefinitely. At the default cap of 1, the orchestrator may grant one extra bounded cycle when the second rejection is a demonstrably distinct defect (logged, single-use, never on an operator-raised cap) — see "Doctor-cycle cap".
- **Mode C skips Curator, Approval gate, and Builder.** These phases already ran (the PR exists). Re-running them would be incorrect.
- **No new labels.** Use only the existing Loom label set (see `.github/labels.yml`). Mode C operates entirely on `loom:review-requested`, `loom:changes-requested`, `loom:pr`, `loom:blocked`, `loom:operator-only`, `loom:needs-capability` — all existing.
- **No `gh pr merge`.** Always use `./.loom/scripts/merge-pr.sh` (uniform across Modes A/B/C).
- **No daemon-state writes.** Read-only access to `daemon-state.json` for situational awareness.
- **Read the issue body** (`gh issue view N --json body`) before briefing the builder (Modes A/B). Mode C uses the PR diff + comments as the source of truth and does not need the issue body.
- **Skip operator-only / needs-capability items.** Issues labeled `loom:operator-only` or `loom:needs-capability` (Modes A/B, see issue-set Wave Lifecycle step 1) and PRs labeled `loom:operator-only` or `loom:needs-capability` (Mode C, see C0) are skipped. Log and move on.

## Reference Documentation

- **Per-issue lifecycle**: the "Wave Lifecycle (Modes A and B only — issue-set)" section of this skill — canonical phase-by-phase reference (Curator → Builder → Judge → Doctor → Merge).
- **Builder skill**: `.claude/commands/loom/builder.md`
- **Judge skill**: `.claude/commands/loom/judge.md`
- **Doctor skill**: `.claude/commands/loom/doctor.md`
- **Curator skill**: `.claude/commands/loom/curator.md`
- **Label definitions**: `.github/labels.yml`
- **Merge script**: `./.loom/scripts/merge-pr.sh`
- **Sweep checkpoint helper**: `./.loom/scripts/sweep-checkpoint.sh` — read/write/delete per-issue phase checkpoints for resume after kill (#3373). Mode C reuses this via the PR's closing-issue number when available.
- **Original proposal & open questions**: issue #3298
- **PR-set mode (Mode C) design**: issue #3384
- **Nested-dispatch stall hazard**: issue #3289
- **Checkpoint/resume design**: issue #3373 (Phase 0 of #3372 shepherd/daemon deprecation epic)
- **Daemon backend detection (Stage -1)**: issue #3454 (Phase D of #3449 daemon rebuild epic)
- **Daemon dispatch MCP tool (`mcp__loom__dispatch_sweep`)**: issue #3452 (Phase A of #3449)
- **Daemon event bus (Phase B)**: issue #3453 (Phase B of #3449)
