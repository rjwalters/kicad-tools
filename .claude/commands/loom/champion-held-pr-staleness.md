# Champion: Held-PR Base-Staleness Tick (#8552)

A sub-step of [`champion-pr-merge.md`](champion-pr-merge.md) → "Held-PR Census"
→ "Per-PR Digest" → Step 1. It runs **inside that loop**, once per held PR,
reusing `$HELD_JSON`'s own fields plus local read-only `git` — no second
`gh pr list`.

**Detection only.** This tick changes no label (never `loom:operator`, never
`loom:blocked`), routes nothing to Doctor, rebases nothing, pushes nothing, and
never touches a working tree. Its entire output is one digest cell per held PR
and, for a conflicting one, at most one comment per divergence episode per
class.

## Why

A held PR's approval never goes stale — verdict-staleness keys on the PR's own
head, which does not move while a human decision is outstanding — and its CI
stays green on the old base. `main` moves anyway. On 2026-09-20/21 three of six
PRs coming off operator holds needed a rebase: two mechanical append-collisions
(`ci.yml`, `ci-excluded.txt`, `shell-allowlist.txt`) and one re-integration
(#8426 — `main` restructured the same `merge-pr.sh --auto` block via #8048 and
#8429 while the PR sat held). All three were discovered *at merge time*: the
worst moment, right after a human had made and announced a merge decision.

## The two signals

- **`mergeStateStatus`** (from `$HELD_JSON`). `DIRTY` means the merge commit
  cannot be created cleanly. **It is not an early warning**: it reports the same
  condition as `mergeable == CONFLICTING`, at the same time. It is the trigger
  for the comment, not the thing that sees rot coming. `UNKNOWN` means GitHub
  has not finished computing mergeability yet; it falls through to the
  non-`DIRTY` branch and the next pass re-reads it — never comment on it.
- **File overlap** (local `git`). The PR's own changed files intersected with
  what `main` changed since this PR diverged. This is the signal that sees rot
  *before* a conflict exists, and the only one that says anything about *what
  kind* of rebase is owed.

An empty-overlap test alone cannot separate the two rot classes, because a git
conflict requires both sides to have touched the same file — a `DIRTY` PR's
overlap is essentially never empty, and an append-collision in `ci.yml`
overlaps exactly as much as a restructure of `merge-pr.sh` does. What separates
them is **shape**: an append-collision is pure addition on both sides; a
restructure deletes or rewrites lines. `git diff --numstat` reports that
directly, so the classification keys on it.

| Overlap | Shape | Class |
|---|---|---|
| empty | — | mechanical — but on a `DIRTY` PR an empty overlap is an artifact (truncated file list, stale objects), not evidence |
| non-empty | every overlapping file is pure-addition on **both** sides | mechanical append-collision — an ordinary rebase should resolve it |
| non-empty | any overlapping file has deletions on either side | **possible structural overlap** — a human/Doctor read before rebasing |

Do not classify further than these two. "Possible structural overlap" is the
end of this tick's authority; deciding whether it really is a re-integration is
a human's or Doctor's call.

Known limits, all of them one-directional and worth stating wherever this
output is read:

- **File names and line counts only.** Two unrelated edits to one large file
  look exactly like a structural overlap; a caller `main` changed in a file
  this PR does not touch looks exactly like a fresh base.
- **`CLEAN` + no overlap is not "safe to merge"**, only "no evidence of rot in
  this PR's own files". #8426 — where `main` restructured the same
  `merge-pr.sh` block — is caught here only because it overlaps a file the PR
  changes; a semantic dependency that crosses files is invisible to this test
  and remains a real detection gap.
- **`$HELD_JSON.files` is GitHub's first page of changed files** (100). On a
  larger PR the overlap is a lower bound, so the class can only err toward
  "mechanical".
- **It never re-reads after `main` moves.** The cell and the notice describe
  the merge base at the moment of this pass.

## The tick

```bash
# Once per pass, before the Step 1 loop. Read-only: fetch refs, never
# checkout/pull/merge — no working tree is touched by anything here.
git fetch -q origin main 2>/dev/null || true

# Inside the Step 1 loop, with $PR_NUM and $ROW already set:
PR_HEAD=$(jq -r '.headRefOid' <<<"$ROW")
PR_STATE=$(jq -r '.mergeStateStatus' <<<"$ROW")
git cat-file -e "$PR_HEAD^{commit}" 2>/dev/null || \
  git fetch -q origin "pull/$PR_NUM/head" 2>/dev/null || true
MERGE_BASE=$(git merge-base origin/main "$PR_HEAD" 2>/dev/null || true)
ROT_CLASS=""

if [ -z "$MERGE_BASE" ]; then
  # Head objects unavailable (fork, deleted branch, fetch declined). Report the
  # gap; never guess a class from it.
  BASE_STALENESS="$PR_STATE, overlap unknown (no merge base)"
else
  # --no-renames throughout: a rename must show up as BOTH paths, or a PR that
  # edits a file main renamed away reads as "no overlap" — the loudest possible
  # false negative.
  OVERLAP=$(comm -12 <(jq -r '.files[].path' <<<"$ROW" | sort -u) \
                     <(git diff --no-renames --name-only "$MERGE_BASE"..origin/main | sort -u))
  OVERLAP_N=$(printf '%s' "$OVERLAP" | grep -c . || true)
  # A file with deletions on either side was EDITED, not appended to.
  RESHAPED=$(printf '%s\n' "$OVERLAP" | while read -r f; do
    [ -z "$f" ] && continue
    D_MAIN=$(git diff --no-renames --numstat "$MERGE_BASE"..origin/main -- "$f" | awk '{print $2}')
    D_PR=$(git diff --no-renames --numstat "$MERGE_BASE".."$PR_HEAD" -- "$f" | awk '{print $2}')
    if [ "${D_MAIN:-0}" != "0" ] || [ "${D_PR:-0}" != "0" ]; then printf '%s\n' "$f"; fi
  done)
  if [ -n "$RESHAPED" ]; then
    SHAPE="possible structural overlap"
  else
    SHAPE="mechanical append-collision"
  fi

  if [ "$PR_STATE" = "DIRTY" ]; then
    ROT_CLASS="$SHAPE"               # comment-worthy: the rebase is owed NOW
    BASE_STALENESS="DIRTY — $SHAPE ($OVERLAP_N overlapping file(s))"
  elif [ "$OVERLAP_N" -gt 0 ]; then
    # Rebases cleanly today, but main edited the same files — #8426's class.
    BASE_STALENESS="$PR_STATE, drift risk: $OVERLAP_N file(s) also changed on main ($SHAPE)"
  else
    BASE_STALENESS="$PR_STATE, base fresh for this PR's files"
  fi
fi
```

`$BASE_STALENESS` is this tick's contract with the census: it is the digest
row's **Base** cell, always set, for every held PR. `$ROT_CLASS` is non-empty
only for a `DIRTY` PR, and is the comment gate below.

## The comment — `DIRTY` only

A drift-risk cell is digest-only. Commenting on every clean-but-overlapping
held PR would notify on `ci.yml` alone most days; the digest carries that
without a notification.

```bash
if [ -n "$ROT_CLASS" ]; then
  # Keyed on merge base + class: main moving does NOT re-key it (the merge base
  # only moves when this PR's own head does), so a held PR gets at most one
  # notice per divergence episode, plus one more if the class ever hardens from
  # mechanical to structural. A 10-minute tick never re-posts.
  STALENESS_MARKER="<!-- champion:base-staleness-notice:$MERGE_BASE:$ROT_CLASS -->"
  # Cached ("$GH_READ") — a marker grep only answers "did I already post this?".
  if [ "$("$GH_READ" pr view "$PR_NUM" --json comments --jq "[.comments[].body] | any(startswith(\"$STALENESS_MARKER\"))")" = "true" ]; then
    echo "Base-staleness notice already posted for #$PR_NUM — skipping"
  else
    gh pr comment "$PR_NUM" --body "$STALENESS_MARKER
**Champion: Held PR Has Gone Stale Against \`main\`**

This PR is on an operator hold and \`main\` has moved underneath it: \`mergeStateStatus\` is \`DIRTY\` against merge base \`${MERGE_BASE:0:8}\`, with **$OVERLAP_N** of this PR's files also changed on \`main\` since it diverged.

**Rot class: $ROT_CLASS.**

- *mechanical append-collision* — both sides only added lines to the files they share. An ordinary rebase should resolve it.
- *possible structural overlap* — \`main\` rewrote lines in a file this PR also changes. The rebase may be a re-integration task rather than a conflict resolution (#8426), so it wants a human or Doctor read first.

**What this classification can and cannot tell you.** It compares file names and added/deleted line counts, nothing else:
- a *possible structural overlap* is often still benign — two unrelated edits to a large file look identical to this test;
- a *mechanical* result cannot prove a rebase is safe — \`main\` may have changed a caller in a file this PR does not touch;
- a held PR that still rebases cleanly can already be semantically stale. That case never reaches this notice at all (it is reported as \"drift risk\" in the merge-risk hold digest instead) and is a known detection gap.

**Nothing has been changed by this notice.** The hold still stands, no label moved, and no rebase or Doctor route was triggered — it exists so the rot is visible *before* someone attempts the merge, not after.

---
*Automated by Champion role*"
    "$GH_READ" --clear-cache   # your own write must not be masked by your own cache
    echo "Posted base-staleness notice on #$PR_NUM ($ROT_CLASS)"
  fi
fi
```

## Relationship to the held-PR conflict notice

`champion-pr-merge.md` → "Held-PR Health Pass" → "#4 under a hold" already
posts `champion:held-pr-conflict-notice` when a held PR reads `CONFLICTING`.
Since `DIRTY` and `CONFLICTING` are the same condition, both notices can appear
on one PR. That is intended, and they are deliberately not merged:

- the conflict notice answers **"this PR conflicts, and here is what happens
  next"** (the Doctor/recency route, how to clear the hold). It is unchanged by
  this tick and must not be edited or suppressed by it;
- this notice answers **"here is what kind of rebase is owed"**, and says
  nothing about routing or remedies — deliberately, so the two do not restate
  each other.

Their markers are separately keyed, so neither idempotency guard can suppress
the other.

## What this tick must never do

- **Never remove or add a label.** `loom:operator` and `loom:blocked` are
  human-owned here; a hold is released only by the four signals in
  `champion-pr-merge.md` → "Sticky holds".
- **Never trigger a rebase, a force-push, or a Doctor dispatch.** Routing a
  held PR to Doctor for a rebase is the treadmill #6852 closed; nothing in this
  tick reopens it.
- **Never gate a merge decision on it.** Like the rest of the census, if any
  part of it fails (fetch declined, no git checkout, rate limit), log and
  continue — criteria #1-6 are unaffected, and the digest still writes with
  `overlap unknown` for that PR.
- **Never state a class more specific than the two above** from file names and
  line counts alone.
