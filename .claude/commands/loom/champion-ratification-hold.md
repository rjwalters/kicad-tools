# Champion: Ratification Hold (#12992)

A sub-step of [`champion-pr-merge.md`](champion-pr-merge.md) → "Safety Criteria"
→ "`ratification`: Ratification Hold". Read it on **every** PR that reaches the criteria,
whatever `merge-risk` and `critical-files` said. It owns `loom:blocked` + a reason-only park
record and three markers on one PR; it never merges and never removes
`loom:pr`.

Rationale: `.loom/docs/ratification-hold.md`.

## The rules

- In a chip repo, merging a spec or decision record can **be** the act that
  ratifies it ("ratified on merge", "merging this PR ratifies"). Merging that is
  the operator's decision, so the PR gets a **hard hold**: the notice says
  plainly **"merging ratifies; the operator approves"**.
- The detector is mechanical and a pure function of the PR's head contents:
  `loom-daemon champion-hold ratification --pr N`. Never re-derive it, and never
  replace it with merge-risk judgment (that is the observed miss: it would have
  merged a private design repo's spec PR). It holds when a file the PR adds, modifies
  or renames under `spec/**`, `decisions/**`, `docs/decisions/**`,
  `*decision*.md` (or a `champion.ratificationPaths` glob) carries a
  ratification phrase at the head. A repo extends the phrases with
  `champion.ratificationPhrases`; it can never remove a built-in. A PR that only
  cites a spec touches no such file and is not held.
- **Never waived.** `standing_auth_eval` is not consulted: no
  `champion.standingAuthorizations` class, with or without `coversCriticalFiles`
  or `criticalFilesOnly`, passes this step. The #12950 self-release path skips
  it, `loom:auto-merge-ok` does not release it, and `merge-risk`'s
  "Sticky holds" release phrases (`merge anyway`, `ok to merge`) do not.
- **The release is a person's comment, never the label's absence.** A trusted,
  non-`[bot]`, non-Champion comment opening with `clear the hold —` releases it
  (the trusted-person rule of #12599). A hand-removed `loom:blocked` is **not** a
  release: the next tick puts it back. This is the opposite of the critical-file
  hold's #9016 rule, on purpose: the operator must *act*.
- The release is scoped to a **head**: a later push that still declares
  ratification re-arms the hold, because the text the operator released may not
  be the text now on the PR.
- Fail closed: a candidate file whose head content cannot be read is never
  `clear`. With no hold standing the tick defers (no merge, no marker); with one
  standing it stays.

## The state machine

The state is whichever of these markers is the **latest** trusted comment
(`startswith`, never `contains`, #5371):

| Latest marker | State |
|---|---|
| `<!-- champion:ratification-hold -->` | `held` |
| `<!-- champion:ratification-release-respected -->` | `released`, at the head it records |
| `<!-- champion:ratification-hold-cleared -->`, or none | `none` |

The verb folds the state, the detector and the release comment into one
`action=`:

| `action` | Tick |
|---|---|
| `none` | nothing; continue to the other criteria |
| `hold` | post the notice, add `loom:blocked`, write the park record; **stop** |
| `stands` | re-assert `loom:blocked` + park record, no comment; **stop** |
| `release` | post the released marker, remove `loom:blocked`; continue (a later tick sees `none` at this head) |
| `clear` | the head no longer declares it: post the cleared marker, remove `loom:blocked` unless another Champion hold stands; continue |
| `defer` | unreadable candidate, no hold standing: do not merge this tick, write nothing; **stop** |

Exit 3 or no stdout (a failed read) is `defer`; exit 2 (a `loom-daemon` older
than the verb) is `none`, since `merge-pr.sh` still refuses a held PR.

## The tick

```bash
PR_NUMBER=<number>
RT_LINE=$(loom-daemon champion-hold ratification --pr "$PR_NUMBER" 2>/dev/null); RT_RC=$?
RT_ACTION=$(sed -n 's/^LOOM-CHAMPION-RATIFICATION .*action=\([a-z]*\).*/\1/p' <<<"$RT_LINE")
RT_HEAD=$(sed -n 's/.* head=\([0-9a-f]*\).*/\1/p' <<<"$RT_LINE")
RT_FILES=$(sed -n 's/.* files=\([^ ]*\).*/\1/p' <<<"$RT_LINE")
case "$RT_RC" in 0) ;; 2) RT_ACTION=none ;; *) RT_ACTION=defer ;; esac
[ -n "$RT_ACTION" ] || RT_ACTION=defer

rt_park() {
  loom-daemon park-record apply --pr "$PR_NUMBER" --reason "champion ratification hold" --by champion >/dev/null 2>&1 || true
}

case "$RT_ACTION" in
  none) echo "#$PR_NUMBER: no ratification hold" ;;
  defer) echo "#$PR_NUMBER: ratification check unreadable or inconclusive — not merging this tick" ;;
  stands)
    echo "Ratification hold already posted for #$PR_NUMBER — hold stands, no comment"
    rt_park
    ;;
  hold)
    ./.loom/scripts/post-comment.sh "$PR_NUMBER" --pr --body "<!-- champion:ratification-hold -->
<!-- champion:hold-state head=$RT_HEAD -->
**Champion: Holding for the Operator — merging ratifies; the operator approves**

This PR adds or changes a spec or decision record that declares it is ratified by merging:

- \`${RT_FILES//,/\`, \`}\`

Champion will not merge it, and nothing but you can release it: no standing authorization covers this hold, and removing \`loom:blocked\` by hand does not release it (Champion puts it back).

**To release**, reply with a comment that opens with:

\`\`\`
clear the hold — <why you approve ratifying this>
\`\`\`

then merge with \`./.loom/scripts/merge-pr.sh $PR_NUMBER\`. A push that still declares ratification re-arms the hold. \`loom:pr\` stays: Judge's approval stands.

---
*Automated by Champion role*"
    rt_park
    ;;
  release)
    ./.loom/scripts/post-comment.sh "$PR_NUMBER" --pr --body "<!-- champion:ratification-release-respected -->
<!-- champion:hold-state head=$RT_HEAD -->
**Champion: Ratification Hold Released by the Operator**

A trusted \`clear the hold —\` comment released this hold at \`$RT_HEAD\`. Merge with \`./.loom/scripts/merge-pr.sh $PR_NUMBER\`; a push that still declares ratification re-arms it.

---
*Automated by Champion role*"
    gh pr edit "$PR_NUMBER" --remove-label "loom:blocked" >/dev/null 2>&1 || true
    ;;
  clear)
    ./.loom/scripts/post-comment.sh "$PR_NUMBER" --pr --body "<!-- champion:ratification-hold-cleared -->
**Champion: Ratification Hold Cleared**

A later push removed every ratification-by-merge declaration from this PR. Re-evaluating normally going forward.

---
*Automated by Champion role*"
    # The label is shared with the other holds: keep it while one still stands.
    OTHER=$(gh pr view "$PR_NUMBER" --json comments --jq '[.comments[].body] | any(startswith("<!-- champion:merge-risk-hold -->") or startswith("<!-- champion:critical-file-hold -->"))' 2>/dev/null)
    [ "$OTHER" = true ] || gh pr edit "$PR_NUMBER" --remove-label "loom:blocked" >/dev/null 2>&1 || true
    ;;
esac
case "$RT_ACTION" in hold|stands|defer) SKIP_REMAINING=true ;; esac
```

`SKIP_REMAINING=true` ends this PR's pass: no Step 2 pre-merge comment, no
Step 3 merge. A cleared notice ends an episode; a later hit is a **fresh**
episode, never a permanent exemption. No marker comment is ever rewritten or
deleted.

Regression: `defaults/scripts/tests/test-champion-ratification-hold.sh` runs this
tick; `loom-daemon/src/champion_hold/ratification/tests.rs` pins the detector,
the 2026-10-20 fixtures and the release rules.
