# Champion: Critical-File Durable Hold (#6879, #9016)

A sub-step of [`champion-pr-merge.md`](champion-pr-merge.md) → "Safety Criteria"
→ "3. Critical File Exclusion Check". Read it once that check-loop has run,
whatever the verdict: FAIL opens/maintains the hold, PASS alone closes an open
episode. It owns one label (`loom:operator`) and three comment markers on one PR;
it never merges and never removes `loom:pr`.

Rationale: `.loom/docs/critical-file-hold.md`.

## The rules

- A critical-file FAIL is a **one-way terminal state**, so it gets a durable
  `loom:operator` hold on criterion #2's pattern — **never** the "Transient
  failures" template in `champion-pr-merge.md` → "PR Rejection Workflow", nor
  criterion #2's sticky-hold machinery (#4742).
- A FAIL skips Steps 2-3 for this PR this pass **in every state below, including
  `released`**. Champion never auto-merges a critical-file FAIL; releasing the
  hold hands the merge to the operator, not back to Champion.
  `loom:auto-merge-ok` does not release it — that override is criterion #2's.
- **The operator's release is the label's ABSENCE** (#9016): a hand-removed
  `loom:operator`, while this episode is open and the change this PR makes has
  not moved, IS the release signal. It is the only label Champion removes and
  re-adds outside a merge, so "open episode + label absent" can only mean a human
  removed it (as #7048 infers for criterion #2).
- The release is scoped to a **diff, not a commit id** (#9416): this verdict is a
  pure function of the file list, so the operator decided about that change.

## The state machine

The state is whichever of these three markers is the **latest** trusted comment
(`startswith`, never `contains` — #5371):

| Latest marker | State |
|---|---|
| `<!-- champion:critical-file-hold -->` | `held` — a hold notice is standing |
| `<!-- champion:critical-file-release-respected -->` | `released` — at the head that comment records |
| `<!-- champion:critical-file-hold-cleared -->`, or none | `none` — no open episode |

Both `held` and `released` comments carry
`<!-- champion:hold-state head=<sha> -->` as their second line — the marker #2
records and `merge-pr.sh` reads back at merge time (#7419).

On **PASS** the state alone decides: `held`/`released` closes the episode (cleared
notice + remove `loom:operator`); `none` is an ordinary pass. On **FAIL**:

First matching row wins.

| State | Action on FAIL |
|---|---|
| `released`, recorded head == current head | nothing at all: no label, no comment |
| either, label present (a human put it back) | hold stands, silently |
| `held`, label absent, recorded head == current head | **respect the release**: acknowledge once, do **not** re-add the label |
| `none` | fresh hold: notice + `loom:operator` |
| either, head moved but the PR's own change provably did not (#9416) | **respect, re-recorded at the new head** with the equivalence kind |
| either, anything else (or no head recorded — a legacy hold) | re-arm: a new hold episode at the current head |

Row 5 is #9416: `loom-daemon forge verdict-equivalent <pr> <recorded> <head>`
re-derives **from the repository** whether the change survived the move, naming
the kind that proved it (`tree` — a #8248/#8508 re-date push; `clean-merge`;
`rebase-patch-identical`). Same verb the Judge-verdict staleness machine uses —
never re-derive a comparison here. Evidence only: no commit message, no author,
no ref-update shape, never a marker (prose anyone can write, #9548).
**FAIL CLOSED**: only a literal `EQUIVALENCE_KIND=` line respects the release; an
absent binary, a daemon predating the verb, a `gh` outage, a shallow clone, a
`merge-tree` conflict, or either kill switch re-arms it. **CI is never exempted**
— only the hold is; every check re-runs against the new head.

## The tick

With the check-loop's verdict in `$CRITERION3_RESULT`:

```bash
PR_NUMBER=<number>
HOLD_MARKER="<!-- champion:critical-file-hold -->"
CLEARED_MARKER="<!-- champion:critical-file-hold-cleared -->"
RELEASED_MARKER="<!-- champion:critical-file-release-respected -->"
# Mail (#10000): `.loom/docs/inbox-mail.md`.
_im=$(awk '/^```bash inbox-mail/{f=1;next} /^```/{f=0} f' .loom/docs/inbox-mail.md 2>/dev/null)
[ -n "$_im" ] && eval "$_im"
type inbox_mail >/dev/null 2>&1 || inbox_mail() { [ "$1" != on ]; }
CF_MAIL_KEY=$(inbox_mail key crithold-pr "$PR_NUMBER")

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

# Did the PR's own change survive a head move? Sets CF_EQUIV_KIND; empty => re-arm.
# requires-daemon: forge optional   Without `forge verdict-equivalent` an equivalent head move re-arms (pre-#9416 behavior: one extra removal, never an unreviewed merge).
cf_change_unmoved() {
  CF_EQUIV_KIND=""
  [ -n "$STATE_HEAD" ] || return 1
  CF_EQUIV_KIND=$("${LOOM_DAEMON_BIN:-loom-daemon}" forge verdict-equivalent \
    "$PR_NUMBER" "$STATE_HEAD" "$HEAD_SHA" 2>/dev/null | sed -n 's/^EQUIVALENCE_KIND=//p')
  [ -n "$CF_EQUIV_KIND" ]
}

if [ "$CRITERION3_RESULT" = "FAIL" ]; then
  # Decide once (table above), then act once. Same-head arms come first: they
  # need no binary or network.
  CF_EQUIV_KIND=""
  if [ "$CF_STATE" = released ] && [ "$STATE_HEAD" = "$HEAD_SHA" ]; then
    CF_ACTION=none          # released at this head, already acknowledged
  elif [ "$OPERATOR_LABEL_NOW" = true ] && [ "$CF_STATE" != none ]; then
    CF_ACTION=stands        # label back on: a human re-asserted the hold
  elif [ "$CF_STATE" = held ] && [ -n "$STATE_HEAD" ] && [ "$STATE_HEAD" = "$HEAD_SHA" ]; then
    CF_ACTION=respect
  elif [ "$CF_STATE" = none ]; then
    CF_ACTION=hold          # fresh episode
  elif cf_change_unmoved; then
    CF_ACTION=respect       # head moved, the PR's own change did not (#9416)
  else
    CF_ACTION=rearm         # a genuinely different diff, or a legacy hold
  fi

  # Mail (#10000) once otherwise mergeable; a human merge: `resolve-merged`.
  case "$CF_ACTION" in hold|rearm|stands) inbox_mail on &&
    [ "$(gh pr view "$PR_NUMBER" --json mergeStateStatus -q .mergeStateStatus 2>/dev/null)" = CLEAN ] &&
      inbox_mail send "$CF_MAIL_KEY" "PR #$PR_NUMBER changes a critical file and needs a human merge: $(gh pr view "$PR_NUMBER" --json url -q .url)" ;; esac

  case "$CF_ACTION" in
    none)
      echo "Critical-file hold for #$PR_NUMBER was released at $HEAD_SHA — not re-holding (#9016)"
      ;;
    stands)
      # `--add-label` on a label already there is a no-op; this branch and
      # hold|rearm are the ONLY ones that may assert it.
      echo "Critical-file hold already posted for #$PR_NUMBER — hold stands, no comment"
      gh pr edit "$PR_NUMBER" --add-label "loom:operator" 2>/dev/null || true
      ;;
    hold|rearm)
      if [ "$CF_ACTION" = rearm ]; then
        CF_REARM_NOTE="The head moved to \`$HEAD_SHA\` after a release of \`$STATE_HEAD\`, and the change it makes is not provably the one you saw, so the hold is **re-armed** (#9416 fails closed).
"
      else
        CF_REARM_NOTE=""
      fi
      ./.loom/scripts/post-comment.sh "$PR_NUMBER" --pr --body "$HOLD_MARKER
<!-- champion:hold-state head=$HEAD_SHA -->
**Champion: Holding for Human Merge — Critical File**

${CF_REARM_NOTE}This PR modifies a critical file and cannot be automatically merged:

- **Critical File Exclusion Check**: <FILE_PATH> matches critical-file pattern \`<PATTERN>\`

**Next steps** — to merge it yourself, two commands, in either order, no race:

\`\`\`bash
gh pr edit $PR_NUMBER --remove-label \"loom:operator\"
./.loom/scripts/merge-pr.sh $PR_NUMBER
\`\`\`

Removing \`loom:operator\` is your release, and it is durable (#9016): Champion
will **not** put it back while this PR's change is the one this notice describes,
so \`merge-pr.sh\` sees no contradictory state (#8112): no need to beat a Champion tick to it. A re-date push or clean
merge of \`main\` does not re-arm the hold (#9416); a push that changes the diff
does. A later push that narrows the diff off every critical-file pattern clears
the hold on the next tick. \`loom:pr\` stays: Judge's approval stands.

---
*Automated by Champion role*"
      gh pr edit "$PR_NUMBER" --add-label "loom:operator" 2>/dev/null || true
      ;;
    respect)
      # Do NOT re-add `loom:operator`; the original notice stays. This comment
      # records the release and re-anchors it at the current head.
      if [ -n "$CF_EQUIV_KIND" ]; then
        CF_EQUIV_LINE="<!-- champion:hold-equivalence kind=$CF_EQUIV_KIND from=$STATE_HEAD to=$HEAD_SHA -->"
        CF_EQUIV_NOTE=" — carried from \`$STATE_HEAD\` by equivalence \`$CF_EQUIV_KIND\`, re-derived from the repository (#9416)"
      else
        CF_EQUIV_LINE="<!-- champion:hold-equivalence kind=same-head -->"
        CF_EQUIV_NOTE=""
      fi
      ./.loom/scripts/post-comment.sh "$PR_NUMBER" --pr --body "$RELEASED_MARKER
<!-- champion:hold-state head=$HEAD_SHA -->
$CF_EQUIV_LINE
**Champion: Critical-File Hold Released by Operator (#9016)**

\`loom:operator\` was removed by hand while this hold stood, and the change this PR makes is still the one the hold was written against${CF_EQUIV_NOTE}. That is your release: Champion is **not** re-adding the label.

The merge is yours: \`./.loom/scripts/merge-pr.sh $PR_NUMBER\`. A push that changes the diff re-arms the hold.

---
*Automated by Champion role*"
      inbox_mail resolve "$CF_MAIL_KEY"   # the human acted
      echo "Critical-file release respected for #$PR_NUMBER at $HEAD_SHA (${CF_EQUIV_KIND:-same-head}) — not reapplying loom:operator (#9016)"
      ;;
  esac
elif [ "$CF_STATE" = held ] || [ "$CF_STATE" = released ]; then
  # PASS with an episode still open — a later push narrowed the diff. Close it:
  # clear the label (a no-op on the `released` path), post a one-time reversal
  # notice, then fall through to the rest of the criteria as an ordinary PASS.
  gh pr edit "$PR_NUMBER" --remove-label "loom:operator" 2>/dev/null || true
  ./.loom/scripts/post-comment.sh "$PR_NUMBER" --pr --body "$CLEARED_MARKER
**Champion: Critical-File Hold Cleared**

A later push narrowed this PR off every critical-file pattern. Re-evaluating normally going forward.

---
*Automated by Champion role*"
  inbox_mail resolve "$CF_MAIL_KEY"
  echo "Critical-file hold cleared for #$PR_NUMBER — re-evaluating normally"
fi
```

A cleared notice ends an episode; a later FAIL is a **fresh** episode, never a
permanent exemption. A re-arm is likewise a new hold notice: no marker comment is
ever rewritten or deleted.

Regression coverage: `defaults/scripts/tests/test-champion-critical-file-check.sh`
mirrors this tick, release/re-arm/equivalence paths included.
