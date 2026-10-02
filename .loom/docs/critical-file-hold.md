# Champion's Critical-File Durable Hold — Rationale and History

Reference material for
`defaults/.claude/commands/loom/champion-critical-file-hold.md`, which carries
the normative rules and the tick itself. Nothing here is an instruction; this
file exists so the prompt that *is* inlined into every Champion session does not
have to carry the incident narrative that explains it (issues #6879, #9016,
#9416; same motivation as `champion-file-split-history.md`).

## Why a durable hold and not a transient retry (#6879)

Criteria #1/#4/#5/#6 of `champion-pr-merge.md` → "Safety Criteria" are mechanical
failures: they clear on their own, or on the next push, so they use the shared
"Transient failures" template in that file's "PR Rejection Workflow". A
critical-file FAIL is not like them. It is a **one-way terminal state** —
nothing about a diff's critical-file-ness changes without a human decision or a
later push that narrows the diff — so it gets a durable `loom:operator` hold on
criterion #2's pattern instead.

It does **not** need criterion #2's sticky-hold judgment-call machinery (#4742).
That machinery exists because #2 is a judgment call that a later re-read might
answer differently, so a hold has to be pinned lest it "silently evaporate".
Criterion #3's check-loop is a deterministic file-pattern match: the same diff
always produces the same verdict, and nothing but the file list can change it.

## Why the operator's release is the label's ABSENCE (#9016)

The hold's whole purpose is "a human merges this one by hand". Until #9016 that
path did not work, for a reason that is worth recording because the fix had two
plausible shapes and only one was safe:

The hold keeps `loom:pr` (Judge really did approve) and adds `loom:operator`.
`merge-pr.sh` refuses any PR carrying both at once — the verdict-contradiction
guard #8112, which has **no** override flag by design. So the operator's only
route was to remove `loom:operator` and merge; but the next Champion tick
re-added it, because the criterion-#3 verdict is deterministic and nothing on the
PR recorded that a human had already decided. On merge train #8996 (2026-09-26)
the label came back 1m42s after the operator's release comment; the PR merged
only by removing the label and running `merge-pr.sh` in the *same* shell command.

The tempting fix — relax #8112 — was rejected: that guard is the thing standing
between the fleet and a merge of a PR whose verdict state contradicts itself.
#9016 left it exactly as strict and made the **release** durable instead: a
hand-removed `loom:operator`, while the episode is open and the change the PR
makes has not moved, *is* the release signal. Champion records it, stands down on
the label, and says so once.

That inference is sound because `loom:operator` is the only label Champion ever
removes and re-adds outside a merge, so "an open episode + the label absent" can
only mean a human removed it. #7048 already makes the same inference for
criterion #2's hold.

### Two other shapes that were rejected

- **Dropping `loom:pr` while on hold.** Its human path is `merge-pr.sh
  --allow-unapproved`, which asserts "nobody reviewed this". That is false here:
  Judge did approve, and the hold is about the *file list*, not the review.
- **A distinct `loom:critical-file-hold` label.** A new fleet-wide label plus a
  `merge-pr.sh` guard change, to carry state the existing marker and label
  already carry between them.

## Why the release survives an equivalent head move (#9416)

#9016 scoped the release to a head SHA. That was one step too literal: it made
every tree-identical re-date push (#8248's freshness remedy, #8508) re-arm a hold
the operator had already released on a byte-identical diff. #9348 paid that tax
four times to land one PR on 2026-09-28.

The criterion-#3 verdict is a pure function of the PR's **file list**, so the
operator's decision is a decision about the *change*, not about a commit id. The
release is therefore scoped to the change, and whether the change survived a head
move is re-derived from the repository by `loom-daemon forge verdict-equivalent`
— the same evidence function the Judge-verdict staleness machine uses
(`loom-daemon/src/verdict_equivalence/`, exposed to shell callers as one verb so
no second comparison can drift from it; that drift is exactly what #9576 cost).

An audit of 16 merged PRs whose heads moved after approval found six moved
heads: four clean fleet merges of `main`, two rebases, all six with
byte-identical patches.

Three properties are not negotiable, and the prompt restates each one:

1. **Evidence, never shape.** No commit message, author, or ref-update shape is
   read, and a marker is never evidence — on a public repo an outsider or a
   foreign Loom install can write any marker (#9548).
2. **Fail closed.** Only a literal `EQUIVALENCE_KIND=` line respects the release.
   An absent binary, a daemon predating the verb, a `gh` outage, a shallow clone,
   a missing object, a `merge-tree` conflict, or either kill switch re-arms the
   hold — the pre-#9416 behavior, which costs one extra operator removal and
   never an unreviewed merge.
3. **The hold is what is exempted, never a check.** CI re-runs against the new
   head regardless of which equivalence carried the release; the base really did
   move, which is the whole point of the re-date remedy.

The conservative arm is still needed for what evidence cannot settle: a push that
really did change the diff between the operator's removal and the next tick.
Re-arming costs one more removal; reading it as a release for an unseen diff
would cost a critical-file change merged with nobody having looked at it.

## The audit trail

A respected release posts a `<!-- champion:critical-file-release-respected -->`
comment carrying two further markers:

- `<!-- champion:hold-state head=<sha> -->` — the same marker criterion #2
  records and `merge-pr.sh` reads back at merge time (#7419), so an operator
  merging some other head already gets a staleness warning for free. On an
  equivalent head move this line is **re-anchored** at the new head, which is
  also what lets the next tick take the cheap same-head arm with no API call.
- `<!-- champion:hold-equivalence kind=<kind> [from=<sha> to=<sha>] -->` — which
  equivalence carried the release (`tree`, `clean-merge`,
  `rebase-patch-identical`, or `same-head`). An audit record only: no code path
  reads it back as evidence, for the #9548 reason above.

No marker comment is ever rewritten or deleted. A cleared notice ends an episode;
a later FAIL is a fresh episode, never a permanent exemption.
