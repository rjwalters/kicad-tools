# The `loom:operator` state — "a human is needed"

Loom's coordination substrate is labels (see `CLAUDE.md` § "Label-Based
Workflow") — every pipeline transition (`loom:triage` → `loom:curated` →
`loom:issue` → `loom:building`, `loom:review-requested` → `loom:pr`, etc.) is
a label change on an issue or PR. Before `loom:operator` existed, one state
was the exception: "the engine has stopped and a human is the only way
forward." Champion's merge-risk hold expressed that state as an HTML comment
marker (`<!-- champion:merge-risk-hold -->`) buried inside a PR comment —
invisible to `gh pr list`, the dashboard, or any label-filtered query. See
[#5502](https://github.com/rjwalters/loom/issues/5502) for the incident that
prompted this (four Judge-approved PRs sat held-but-invisible for up to 126
hours).

`loom:operator` moves that state onto the label substrate, where every other
pipeline state already lives.

## Definition

> `loom:operator`: the engine will not work this item further; a human is the
> only transition out.

## Relationship to `loom:blocked` and `loom:operator-only`

Three labels now sit in similar territory. They are **not** consolidated into
one — each answers a different question, and the differences are load-bearing
enough to keep separate (see `.github/labels.yml` inline comments, next to
each definition, for the terse version of this same table):

| Label | Question it answers | Does sweep/shepherd skip it? |
|---|---|---|
| `loom:blocked` | Waiting on a dependency, but still automatable once that clears | No |
| `loom:operator-only` | Requires human action or ruling *outside* automation entirely (credentials, infra, hardware, an owner-gated decision) | **Yes** — sweep/shepherd skip it |
| `loom:operator` | The engine has stopped on this specific artifact and a human must act, but the item stays live in its normal queue so the engine's own release conditions can still fire | **No** — stays in the normal re-evaluation queue |

The distinguishing property of `loom:operator` is that it is **re-evaluable**:
unlike `loom:operator-only`, applying it must never cause sweep/shepherd
dispatch to skip the item. That is what makes it safe to apply to a PR that
still needs to pass through its normal Champion tick — the hold that put the
label on can also be the mechanism that takes it back off, without a human
having to remember to remove it.

## Entry points

| Role | Trigger | Status |
|---|---|---|
| Champion (PR merge) | Posts a merge-risk hold (`champion:merge-risk-hold`) because a safety axis is red | **Wired** — `defaults/.claude/commands/loom/champion-pr-merge.md`, "Hold behavior" |
| Builder / Doctor | Encounters work that needs credentials, infra, or a policy ruling outside automation (today's `loom:operator-only` use case) | Not yet wired — follow-up work |
| Judge | A review surfaces a question only a human can answer | Not yet wired — follow-up work |
| Human | Applies the label directly to any issue or PR | Always available (labels are always human-writable) |

**Scope note**: this first pass (#5502) wires only the Champion merge-risk
hold entry point end-to-end. `curator.md`, `builder.md`, `doctor.md`,
`judge.md`, `champion.md`, `champion-common.md`, `champion-issue-promo.md`,
`champion-reference.md`, `loom.md`, `sweep.md`, and `watch.md` all reference
`loom:operator-only` and/or `loom:blocked` today; none of them assume that set
is exhaustive in a way that required editing for this PR, but none of them
have been migrated to *use* `loom:operator` yet either. Extending
`loom:operator` to the Builder/Doctor/Judge entry points above is explicitly
out of scope here — file a follow-up issue per entry point once the Champion
wiring has run in production.

## Exit rule

`loom:operator` is cleared when the artifact the engine judged **materially
changes** — never merely because a role re-read the same artifact and changed
its mind. For the Champion hold, this reuses the *existing* release precheck
(`champion-pr-merge.md`, "Sticky holds" / criterion #2), which already
computes exactly this distinction for the hold marker itself. `loom:operator`
does not add a second, independent state-tracking mechanism — it piggybacks
on the same four precheck outcomes:

| Precheck outcome | `loom:operator` |
|---|---|
| Never held (`PRIOR_HOLD=false`) | Never applied |
| Held, no release signal yet | Stays applied (label add is idempotent — re-asserted, not re-added, each tick the hold stands) |
| Held, released by `loom:auto-merge-ok` override | Removed in the same pass as the reversal comment |
| Held, released by an explicit operator-comment, a new push (head SHA changed), or a new Judge review | Removed in the same pass as the reversal comment |

A human can also clear `loom:operator` directly at any time by removing the
label — the automated exit rule above is the *default* path, not the only
one.

## Current implementation

Only the Champion merge-risk-hold entry/exit pair is wired today:

- **Entry** — `defaults/.claude/commands/loom/champion-pr-merge.md`, criterion
  #2's "Hold behavior" block (`gh pr edit ... --add-label loom:operator`,
  posted alongside the `champion:merge-risk-hold` marker).
- **Exit** — the same file's Step 2 ("Add Pre-Merge Comment"), gated on the
  non-empty `$HOLD_REVERSAL_BLOCK` built by the release precheck (`gh pr edit
  ... --remove-label loom:operator`, posted alongside the
  `champion:merge-risk-hold-cleared` marker).

Both reuse the single release precheck at `champion-pr-merge.md` ("Sticky
holds — a hold does NOT clear on a re-read alone") rather than re-deriving
release state independently.

## Follow-up work

- Wire `loom:operator` into Builder/Doctor's credential-or-policy stop path
  (today's `loom:operator-only` usage).
- Wire `loom:operator` into Judge's unanswerable-question path.
- Decide whether `loom:operator-only` should eventually be subsumed by
  `loom:operator` + a separate "skip dispatch" signal, or remain a distinct
  label permanently — the #5502 issue thread leaves this open pending
  experience with the Champion-only rollout.
