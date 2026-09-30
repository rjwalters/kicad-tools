# Champion: Critical-File Durable Hold (#6879, #9016)

A sub-step of [`champion-pr-merge.md`](champion-pr-merge.md) → "Safety Criteria"
→ "3. Critical File Exclusion Check". Read it once that check-loop has actually
run, whatever the verdict: FAIL opens/maintains the hold, and PASS is the only
thing that closes an open episode.

It owns one label (`loom:operator`) and three comment markers on one PR; it never
merges and never removes `loom:pr`.

## Why a durable hold, not a transient retry (#6879)

Unlike criteria #1/#4/#5/#6 (mechanical failures that clear on their own or on
the next push), a critical-file FAIL is a **one-way terminal state** — nothing
about a diff's critical-file-ness changes without a human decision or a later
push that narrows the diff. It gets this durable hold, mirroring criterion #2's
`loom:operator` pattern, never the shared "Transient failures" template in
`champion-pr-merge.md` → "PR Rejection Workflow".

It does **not** need criterion #2's sticky-hold judgment-call machinery (#4742):
the check-loop is a **deterministic** file-pattern match, so the same diff always
produces the same verdict — nothing but the file list can change it, and a hold
cannot "silently evaporate" on a re-read.

A FAIL skips Steps 2-3 for this PR this pass **in every state below, including
`released`**. Champion never auto-merges a critical-file FAIL; releasing the hold
hands the merge to the operator, not back to Champion. `loom:auto-merge-ok` does
not release it either — that override is scoped to criterion #2 alone.

## Why the operator's release is the label's ABSENCE (#9016)

The hold's whole purpose is "a human merges this one by hand". Until #9016 that
path did not work: the hold kept `loom:pr` and added `loom:operator`, and
`merge-pr.sh` refuses any PR carrying both at once (the verdict-contradiction
guard #8112, which has **no** override flag by design) — so the operator removed
`loom:operator`, and the next tick re-added it, because the FAIL verdict is
deterministic and nothing recorded that a human had already decided. On merge
train #8996 (2026-09-26) the label came back 1m42s after the operator's release
comment; the PR merged only by removing the label and running `merge-pr.sh` in
the *same* shell command.

The fix leaves the contradiction guard exactly as strict as it was and makes the
release durable instead: **a hand-removed `loom:operator`, while this episode is
open and the head has not moved, IS the release signal.** Champion records it,
stands down on the label at that head, and says so once.

`loom:operator` is the only label Champion ever removes and re-adds outside a
merge, so "an open episode + the label absent" can only mean a human removed it
— the same inference #7048 already makes for criterion #2's hold. What is new
here is that the release is **scoped to a head SHA**: this criterion's verdict is
a pure function of the file list, so the operator's decision is a decision about
*that diff*, and a push produces one they never saw.

Rejected: dropping `loom:pr` on hold (its human path, `merge-pr.sh
--allow-unapproved`, asserts "nobody reviewed this" — false here, Judge did
approve); and a distinct `loom:critical-file-hold` label (a new fleet-wide label
plus a guard change, for state the marker and label already carry).

## The state machine

The state is whichever of these three markers is the **latest** trusted comment
on the PR (`startswith`, never `contains` — #5371):

| Latest marker | State |
|---|---|
| `<!-- champion:critical-file-hold -->` | `held` — a hold notice is standing |
| `<!-- champion:critical-file-release-respected -->` | `released` — the operator released the head named in that comment |
| `<!-- champion:critical-file-hold-cleared -->`, or none | `none` — no open episode |

Both `held` and `released` comments carry
`<!-- champion:hold-state head=<sha> -->` as their second line — the same marker
criterion #2 records and `merge-pr.sh` reads back at merge time (#7419), so an
operator merging a head other than the recorded one already gets a staleness
warning for free.

On **PASS**, the state alone decides: `held` or `released` closes the episode
(cleared notice + remove `loom:operator`); `none` is an ordinary pass with no
side effects. On **FAIL**:

| State | Action on FAIL |
|---|---|
| `none` | fresh hold: notice + `loom:operator` |
| `held`, label present | hold stands, silently |
| `held`, label absent, recorded head == current head | **respect the release**: acknowledge once, do **not** re-add the label |
| `released`, recorded head == current head | nothing at all: no label, no comment |
| either, recorded head != current head (or none recorded — a legacy hold) | re-arm: a new hold episode at the current head |

The last row is conservative for the one race state cannot resolve: a push
between the operator's removal and the next tick. Re-arming costs one more
removal (after which the recorded head is current and the release is honored);
reading it as a release for an unseen diff would cost a critical-file change
merged with nobody having looked at it.

## The tick

With the check-loop's verdict in `$CRITERION3_RESULT`:

```bash
PR_NUMBER=<number>
HOLD_MARKER="<!-- champion:critical-file-hold -->"
CLEARED_MARKER="<!-- champion:critical-file-hold-cleared -->"
RELEASED_MARKER="<!-- champion:critical-file-release-respected -->"

# Plain `gh` — NOT "$GH_READ": a cached label set misses a human's decision.
# Markers count from TRUSTED authors only (#9548). Unauthenticated -> the raw
# read: it only books notices/labels, and criterion #3's FAIL never merges.
CF_JSON=$(gh pr view "$PR_NUMBER" --json comments,labels,headRefOid)
T=$(loom-daemon forge trusted-comments --fetch "$PR_NUMBER" --gh-shape) && CF_JSON=$(jq --argjson c "$T" '.comments = $c' <<<"$CF_JSON")
HEAD_SHA=$(jq -r '.headRefOid' <<<"$CF_JSON")
OPERATOR_LABEL_NOW=$(jq -r '[.labels[].name] | any(. == "loom:operator")' <<<"$CF_JSON")

# Latest of the three episode markers decides the state.
LAST_STATE=$(jq -r --arg h "$HOLD_MARKER" --arg c "$CLEARED_MARKER" --arg r "$RELEASED_MARKER" \
  '[.comments[] | select((.body | startswith($h)) or (.body | startswith($c)) or (.body | startswith($r)))] | last | .body // ""' <<<"$CF_JSON")
case "$LAST_STATE" in
  "$RELEASED_MARKER"*) CF_STATE=released ;;
  "$HOLD_MARKER"*)     CF_STATE=held ;;
  *)                   CF_STATE=none ;;   # cleared, or never held
esac

# The head that state was recorded against. Empty for a legacy hold posted
# before the hold-state line existed — treated as "unknown", i.e. re-arm.
STATE_HEAD=$(printf '%s' "$LAST_STATE" \
  | sed -n 's/.*champion:hold-state head=\([0-9a-f]*\).*/\1/p' | head -1)

if [ "$CRITERION3_RESULT" = "FAIL" ]; then
  # Decide once (table above), then act once.
  if [ "$CF_STATE" = released ] && [ "$STATE_HEAD" = "$HEAD_SHA" ]; then
    CF_ACTION=none          # already released at this head, already acknowledged
  elif [ "$CF_STATE" = released ]; then
    CF_ACTION=rearm
  elif [ "$CF_STATE" = held ] && [ "$OPERATOR_LABEL_NOW" = true ]; then
    CF_ACTION=stands
  elif [ "$CF_STATE" = held ] && [ -n "$STATE_HEAD" ] && [ "$STATE_HEAD" = "$HEAD_SHA" ]; then
    CF_ACTION=respect
  elif [ "$CF_STATE" = held ]; then
    CF_ACTION=rearm         # head moved since the hold, or legacy hold
  else
    CF_ACTION=hold          # fresh episode
  fi

  case "$CF_ACTION" in
    none)
      echo "Critical-file hold for #$PR_NUMBER was released by the operator at $HEAD_SHA — not re-holding (#9016)"
      ;;
    stands)
      # `--add-label` on a label already there is a no-op; this branch and
      # hold|rearm are the ONLY ones that may assert it.
      echo "Critical-file hold already posted for #$PR_NUMBER — hold stands, no comment"
      gh pr edit "$PR_NUMBER" --add-label "loom:operator" 2>/dev/null || true
      ;;
    hold|rearm)
      if [ "$CF_ACTION" = rearm ]; then
        CF_REARM_NOTE="This PR's head moved to \`$HEAD_SHA\` after a release of \`$STATE_HEAD\`, so the hold is **re-armed**: the release covered the diff you saw, not this one.
"
      else
        CF_REARM_NOTE=""
      fi
      gh pr comment "$PR_NUMBER" --body "$HOLD_MARKER
<!-- champion:hold-state head=$HEAD_SHA -->
**Champion: Holding for Human Merge — Critical File**

${CF_REARM_NOTE}This PR modifies a critical file and cannot be automatically merged:

- **Critical File Exclusion Check**: <FILE_PATH> matches critical-file pattern \`<PATTERN>\`

**Next steps** — to merge it yourself, two commands, in either order, no race:

\`\`\`bash
gh pr edit $PR_NUMBER --remove-label \"loom:operator\"
./.loom/scripts/merge-pr.sh $PR_NUMBER
\`\`\`

Removing \`loom:operator\` is your release, and it is durable: while this PR's
head is \`$HEAD_SHA\` Champion will **not** put it back (#9016), so
\`merge-pr.sh\` sees no contradictory verdict state (#8112) and merges. You do
not have to beat a Champion tick to it. A new push re-arms the hold — the diff
this notice was written against would no longer exist. (A refusal on check
*freshness* is a different guard, #8248: re-run with \`--redate-stale-checks\`.)

Or: a later push that narrows the diff so it no longer touches any
critical-file pattern clears this hold automatically on the next tick.

Keeping \`loom:pr\` — Judge's approval of this head stands, and this PR stays in
the queue, re-checked each tick against both release conditions above.

---
*Automated by Champion role*"
      gh pr edit "$PR_NUMBER" --add-label "loom:operator" 2>/dev/null || true
      ;;
    respect)
      # Respect the human decision instead of overriding it: do NOT re-add
      # `loom:operator`. The ORIGINAL hold notice/marker is left untouched —
      # never re-posted, never edited. This comment is the durable record that
      # the release was seen, and its head-state line is what keeps a later
      # push from being read as part of it.
      gh pr comment "$PR_NUMBER" --body "$RELEASED_MARKER
<!-- champion:hold-state head=$HEAD_SHA -->
**Champion: Critical-File Hold Released by the Operator (#9016)**

\`loom:operator\` was removed by hand while this critical-file hold was
standing, and this PR's head is still \`$HEAD_SHA\` — the head the hold was
written against. That is your release: Champion is **not** putting the label
back at this head.

The verdict itself has not changed (this PR still touches a critical file, so
Champion still will not merge it), so the merge is yours to run:
\`./.loom/scripts/merge-pr.sh $PR_NUMBER\`. A new push re-arms the hold.

---
*Automated by Champion role*"
      echo "Critical-file hold release respected for #$PR_NUMBER at $HEAD_SHA — not reapplying loom:operator (#9016)"
      ;;
  esac
elif [ "$CF_STATE" = held ] || [ "$CF_STATE" = released ]; then
  # PASS with an episode still open — a later push narrowed the diff. Close the
  # episode: clear the label (a no-op on the `released` path, where the operator
  # already removed it), post a one-time reversal notice, and fall through to
  # the rest of the criteria as an ordinary PASS.
  gh pr edit "$PR_NUMBER" --remove-label "loom:operator" 2>/dev/null || true
  gh pr comment "$PR_NUMBER" --body "$CLEARED_MARKER
**Champion: Critical-File Hold Cleared**

A later push narrowed this PR so it no longer touches any critical-file pattern. Re-evaluating normally on this and subsequent ticks.

---
*Automated by Champion role*"
  echo "Critical-file hold cleared for #$PR_NUMBER — re-evaluating normally"
fi
```

A cleared notice ends an episode; a later FAIL is then a **fresh** episode (a
new hold comment), never a permanent exemption. A re-arm is likewise a new hold
notice: no marker comment is ever rewritten or deleted.

Regression coverage: `defaults/scripts/tests/test-champion-critical-file-check.sh`
mirrors this tick, release and re-arm paths included, and pins its commands.
