# Champion: Critical-File Durable Hold (#6879, #9016)

A sub-step of [`champion-pr-merge.md`](champion-pr-merge.md) → "Safety Criteria"
→ "3. Critical File Exclusion Check". Read it once that check-loop has run,
whatever the verdict: FAIL opens/maintains the hold, PASS alone closes an open
episode. It owns one label (`loom:blocked` + a reason-only park record,
#11861) and three markers on one PR; it never merges and never removes
`loom:pr`.

Rationale: `.loom/docs/critical-file-hold.md`.
## The rules

- A critical-file FAIL is a **one-way terminal state**, so it gets a durable
  `loom:blocked` hold on criterion #2's pattern — **never** the "Transient
  failures" template, nor criterion #2's sticky-hold machinery (#4742).
- A FAIL skips Steps 2-3 for this PR **in every state, `released` included**:
  releasing hands the merge to the operator, never back to Champion.
  `loom:auto-merge-ok` does not release it (criterion #2's override).
- **The operator's release is the label's ABSENCE** (#9016): a hand-removed
  `loom:blocked`, while this episode is open and the change has not moved, IS
  the release: Champion never removes it mid-episode (#11861 keeps it on
  through the Doctor round-trip).
- The release is scoped to a **diff, not a commit id** (#9416): this verdict is a
  pure function of the file list, so the operator decided about that change.

## A covering standing authorization means no hold (#12528)

The FAIL above is "one-way terminal" only for a PR no operator pre-authorized.
`champion-pr-merge.md` criterion #3 runs `standing_auth_eval critical` first: a
`champion.standingAuthorizations` class with `"coversCriticalFiles": true` that
matches the whole diff (`filePatterns` exact subset; no `excludeFilePatterns`
file; no `forbiddenDiffPatterns` line in any added/removed line; no kept-set
path; `judgeApproval` and `greenCi` holding) passes #3, so this tick receives
`$CRITERION3_RESULT="PASS"` and writes no hold. Any open episode closes through
the ordinary PASS row (cleared notice + remove `loom:blocked`), naming the class
id. One file outside the class, an excluded file, a forbidden line or an
unreadable diff restores the FAIL and this whole machine. A `dependabot-bumps` class (`authorIsDependabot`, manifest and lockfile `filePatterns`; #12948) is the fleet-default example: Dependabot's own manifest bumps pass #3, a human PR with the same diff does not. The kept set (the
merge gate, guard hooks, credential selection, installers/updaters,
migrations) is never satisfiable by a class.

A `"criticalFilesOnly": true` class (#12648) passes #3 the same way, but is
matched against the critical-file hits alone (the files #3 flags, and each
one's `previous_filename`): every hit must match its `filePatterns`, none its
`excludeFilePatterns`, no forbidden line in the hits' patches, no kept-set path
anywhere in the PR, conditions holding. The other changed files are left to
criterion #2, which this class never satisfies. A PASS under it closes an open
episode through the same ordinary PASS row; if criterion #2 then holds the PR,
that is criterion #2's own hold, not this machine's.

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
notice + remove `loom:blocked`); `none` is an ordinary pass. On **FAIL**, first
matching row wins:

| State | Action on FAIL |
|---|---|
| `released`, recorded head == current head | nothing at all: no label, no comment |
| either, label present (a human put it back) | hold stands, silently — except that a changed standing-authorization digest is recorded once (#12933) |
| `held`, label absent, recorded head == current head | **respect the release**: acknowledge once, do **not** re-add the label |
| `none` | fresh hold: notice + `loom:blocked` |
| either, head moved but the PR's own change provably did not (#9416) | **respect, re-recorded at the new head** with the equivalence kind |
| either, anything else (or no head recorded — a legacy hold) | re-arm: a new hold episode at the current head |

The #9416 row: `loom-daemon forge verdict-equivalent <pr> <recorded> <head>`
(Judge's staleness verb; never re-derive it here) proves from the repository
that the change survived the move (`tree`, `clean-merge`,
`rebase-patch-identical`). **FAIL CLOSED**: only a literal `EQUIVALENCE_KIND=`
line respects the release; anything else re-arms, naming why.
CI is never exempted.

## The tick

With the check-loop's verdict in `$CRITERION3_RESULT`:

```bash
PR_NUMBER=<number>
HOLD_MARKER="<!-- champion:critical-file-hold -->"
CLEARED_MARKER="<!-- champion:critical-file-hold-cleared -->"
RELEASED_MARKER="<!-- champion:critical-file-release-respected -->"

# Plain `gh` (a cached label set misses a human's decision). TRUSTED markers
# only (#9548); unfiltered, the raw read may hold but never release (#10875).
CF_JSON=$(gh pr view "$PR_NUMBER" --json comments,labels,headRefOid)
CF_TRUST=raw; T=$(loom-daemon forge trusted-comments --fetch "$PR_NUMBER" --gh-shape) && CF_JSON=$(jq --argjson c "$T" '.comments = $c' <<<"$CF_JSON") && CF_TRUST=ok
HEAD_SHA=$(jq -r '.headRefOid' <<<"$CF_JSON")
HOLD_LABEL_NOW=$(jq -r '[.labels[].name] | any(. == "loom:blocked")' <<<"$CF_JSON")
cf_park() {
  loom-daemon park-record apply --pr "$PR_NUMBER" --reason "champion critical-file hold" --by champion >/dev/null 2>&1 || true
  # #10937: an OPEN issue/PR the hold waits on, if the notice names one; inert (Guide skips a standing Champion hold)
  for N in ${CF_BLOCKER_NUMBERS:-}; do loom-daemon park-record apply --pr "$PR_NUMBER" --blocked-by "$N" \
    --reason "champion critical-file hold: human evidence gate" --by champion >/dev/null 2>&1 || true; done
}

LAST_STATE=$(jq -r --arg h "$HOLD_MARKER" --arg c "$CLEARED_MARKER" --arg r "$RELEASED_MARKER" \
  '[.comments[] | select((.body | startswith($h)) or (.body | startswith($c)) or (.body | startswith($r)))] | last | .body // ""' <<<"$CF_JSON")
case "$LAST_STATE" in
  "$RELEASED_MARKER"*) CF_STATE=released ;;
  "$HOLD_MARKER"*)     CF_STATE=held ;;
  *)                   CF_STATE=none ;;   # cleared, or never held
esac

# Head the state was recorded against; empty (legacy hold) => re-arm.
STATE_HEAD=$(printf '%s' "$LAST_STATE" \
  | sed -n 's/.*champion:hold-state head=\([0-9a-f]*\).*/\1/p' | head -1)

# Head moved; did the PR's change? Sets CF_EQUIV_KIND (empty => re-arm), CF_EQUIV_WHY.
# requires-daemon: forge optional   Without `forge verdict-equivalent` an equivalent head move re-arms (pre-#9416 behavior: one extra removal, never an unreviewed merge).
cf_change_unmoved() {
  CF_EQUIV_KIND="" CF_EQUIV_WHY="no head recorded (legacy hold)"
  [ -n "$STATE_HEAD" ] || return 1
  CF_EQUIV_WHY=$("${LOOM_DAEMON_BIN:-loom-daemon}" forge verdict-equivalent \
    "$PR_NUMBER" "$STATE_HEAD" "$HEAD_SHA" 2>&1)
  CF_EQUIV_KIND=$(sed -n 's/^EQUIVALENCE_KIND=//p' <<<"$CF_EQUIV_WHY")
  CF_EQUIV_WHY=$(grep -v '^[A-Z_]*=' <<<"$CF_EQUIV_WHY" | tail -1)
  [ -n "$CF_EQUIV_KIND" ]
}

if [ "$CRITERION3_RESULT" = "FAIL" ]; then
  CF_EQUIV_KIND=""
  if [ "$CF_STATE" = released ] && [ "$STATE_HEAD" = "$HEAD_SHA" ]; then
    CF_ACTION=none          # released at this head, already acknowledged
  elif [ "$HOLD_LABEL_NOW" = true ] && [ "$CF_STATE" != none ]; then
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
  [ "$CF_TRUST" = ok ] || case "$CF_ACTION" in none|respect) CF_ACTION=defer ;; esac

  case "$CF_ACTION" in
    none)
      echo "Critical-file hold for #$PR_NUMBER was released at $HEAD_SHA — not re-holding (#9016)"
      ;;
    defer) echo "#$PR_NUMBER: untrusted markers, no release honored (#10875)" ;;
    stands)
      # Idempotent; this and hold|rearm alone assert the hold.
      echo "Critical-file hold already posted for #$PR_NUMBER — hold stands, no comment"
      # #12933: the daemon drew this hold because the standing-authorization
      # digest moved (or a legacy record has none). No class covered it, so
      # record the current digest ONCE at this head — the daemon reads the
      # LAST trusted `champion:hold-state` record — or every later tick
      # redraws it (the #12901 no-op loop). Not one of the three state
      # markers, so LAST_STATE / STATE_HEAD are unchanged.
      HOLD_AUTH=$(loom-daemon config get champion.standingAuthorizations --digest 2>/dev/null)
      RECORDED_AUTH=$(jq -r '[.comments[].body | capture("<!-- champion:hold-state head=[0-9a-f]+(?: auth=(?<a>[0-9a-f]+))?")] | last | .a // ""' <<<"$CF_JSON")
      if [ -n "$HOLD_AUTH" ] && [ "$RECORDED_AUTH" != "$HOLD_AUTH" ]; then
        ./.loom/scripts/post-comment.sh "$PR_NUMBER" --pr --body "<!-- champion:hold-state head=$HEAD_SHA auth=$HOLD_AUTH -->
**Champion: critical-file hold re-read under changed standing authorizations (#12933)** — no class covers this PR's critical-file hits; the hold notice above still applies. The digest is recorded so this re-read happens once per policy change."
      fi
      cf_park
      ;;
    hold|rearm)
      if [ "$CF_ACTION" = rearm ]; then
        CF_REARM_NOTE="The head moved to \`$HEAD_SHA\` after a release of \`$STATE_HEAD\`, and the change it makes is not provably the one you saw, so the hold is **re-armed** (#9416 fails closed). Why: ${CF_EQUIV_WHY:-the change differs}
"
      else
        CF_REARM_NOTE=""
      fi
      CF_WF_NOTE=""   # #10539
      gh pr diff "$PR_NUMBER" --name-only 2>/dev/null | grep -q '^\.github/workflows/' &&
        CF_WF_NOTE="
Merging workflow files needs the \`workflow\` token scope: \`gh auth refresh -h github.com -s workflow\`
"
      # Standing-authorization digest (#12933): a later change to
      # `champion.standingAuthorizations` differs from it and re-reads this hold once.
      HOLD_AUTH=$(loom-daemon config get champion.standingAuthorizations --digest 2>/dev/null)
      HOLD_AUTH_FIELD=""; [ -n "$HOLD_AUTH" ] && HOLD_AUTH_FIELD=" auth=$HOLD_AUTH"
      ./.loom/scripts/post-comment.sh "$PR_NUMBER" --pr --body "$HOLD_MARKER
<!-- champion:hold-state head=$HEAD_SHA${HOLD_AUTH_FIELD} -->
**Champion: Holding for Human Merge — Critical File**

${CF_REARM_NOTE}This PR modifies a critical file and cannot be automatically merged:

- **Critical File Exclusion Check**: <FILE_PATH> matches critical-file pattern \`<PATTERN>\`

**Next steps** — to merge it yourself, two commands, in either order, no race:

\`\`\`bash
gh pr edit $PR_NUMBER --remove-label \"loom:blocked\"
./.loom/scripts/merge-pr.sh $PR_NUMBER
\`\`\`

Removing \`loom:blocked\` is your release, and it is durable (#9016): Champion
will **not** put it back while this PR's change is the one this notice
describes. A push that changes the diff re-arms the hold (a tree-identical push or a
clean merge of \`main\` does not, #9416); one that leaves every critical-file
pattern clears it. \`loom:pr\` stays: Judge's approval stands.
${CF_WF_NOTE}
---
*Automated by Champion role*"
      cf_park
      ;;
    respect)
      # Do NOT re-add `loom:blocked`; this records the release at the current head.
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

\`loom:blocked\` was removed by hand while this hold stood, and the change is still the one the hold described${CF_EQUIV_NOTE}: your release. Champion is **not** re-adding the label. Merge with \`./.loom/scripts/merge-pr.sh $PR_NUMBER\`; a push that changes the diff re-arms the hold.

---
*Automated by Champion role*"
      echo "Critical-file release respected for #$PR_NUMBER at $HEAD_SHA (${CF_EQUIV_KIND:-same-head}) — not reapplying loom:blocked (#9016)"
      ;;
  esac
elif [ "$CF_TRUST" != ok ] && { [ "$CF_STATE" = held ] || [ "$CF_STATE" = released ]; }; then
  echo "#$PR_NUMBER: trusted comments unreadable — clear deferred, loom:blocked kept (#11861)"
elif [ "$CF_STATE" = held ] || [ "$CF_STATE" = released ]; then
  # PASS with an episode open — a push narrowed the diff. Post a one-time
  # reversal notice, then continue as an ordinary PASS. The label is shared
  # with the merge-risk hold: remove it only if no merge-risk hold stands
  # (#11861), or criterion #2 would read Champion's removal as a human release.
  MR_STANDS=$(jq -r '[.comments[].body] | (map(startswith("<!-- champion:merge-risk-hold -->")) | rindex(true)) as $h
    | (map(contains("<!-- champion:merge-risk-hold-cleared -->")) | rindex(true)) as $c | $h != null and ($c == null or $h > $c)' <<<"$CF_JSON")
  [ "$MR_STANDS" = true ] || gh pr edit "$PR_NUMBER" --remove-label "loom:blocked" 2>/dev/null || true
  ./.loom/scripts/post-comment.sh "$PR_NUMBER" --pr --body "$CLEARED_MARKER
**Champion: Critical-File Hold Cleared**

A later push narrowed this PR off every critical-file pattern. Re-evaluating normally going forward.

---
*Automated by Champion role*"
  echo "Critical-file hold cleared for #$PR_NUMBER — re-evaluating normally"
fi
```

A cleared notice ends an episode; a later FAIL is a **fresh** episode, never a
permanent exemption. A re-arm is likewise a new hold notice: no marker comment is
ever rewritten or deleted.

Regression: `defaults/scripts/tests/test-critical-file-hold-tick.sh` runs this tick.
