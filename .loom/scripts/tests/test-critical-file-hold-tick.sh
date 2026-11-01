#!/usr/bin/env bash
# test-critical-file-hold-tick.sh - runs the SHIPPED critical-files criterion hold tick
# (#10875).
#
# test-champion-critical-file-check.sh mirrors the tick in local functions, and
# a mirror can drift from the prompt it copies. This file extracts the one
# ```bash block from champion-critical-file-hold.md and executes it unchanged,
# against stub `gh`, `loom-daemon` and `post-comment.sh`, asserting the forge
# writes it makes (comments posted, labels added/removed) tick by tick.
#
# Incident (#10875, PR #10857): an operator-released critical-file hold came
# back after head moves the operator saw as no-ops. The trace showed both
# re-arms were fail-closed on genuine evidence (a Doctor commit editing ci.yml;
# a merge of main with a hand-resolved conflict), and that the equivalence proof
# could not see through re-date commits stacked around a merge — fixed in
# loom-daemon/src/verdict_equivalence/clean_merge.rs and covered by
# noop_chain_tests.rs. This file pins the tick side: a release survives one or
# many repository-proven equivalent head moves (acknowledged or not), a real
# change re-arms, every unknown fails closed with its reason in the notice, an
# operator's re-added label stands, and no untrusted or quoted marker — even
# during a trust-filter outage — can carry a release.
#
# Equivalence comes from a disposable git fixture: the `loom-daemon forge
# verdict-equivalent` stub answers `tree` only when the two commits' trees are
# identical in that repository, and never reads a commit message. The real
# verb's proofs are unit-tested in loom-daemon/src/verdict_equivalence/.
#
# Usage: ./.loom/scripts/tests/test-critical-file-hold-tick.sh

set -uo pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$TEST_DIR/.." && pwd)"
# Installed copy: .loom/scripts -> ../../.claude; source repo: defaults/scripts -> ../.claude.
if [[ -d "$SCRIPTS_DIR/../../.claude/commands/loom" ]]; then
    PROMPT_DIR="$(cd "$SCRIPTS_DIR/../../.claude/commands/loom" && pwd)"
else
    PROMPT_DIR="$(cd "$SCRIPTS_DIR/../.claude/commands/loom" && pwd)"
fi
HOLD_MD="$PROMPT_DIR/champion-critical-file-hold.md"

command -v jq >/dev/null || { echo "SKIP: jq not installed"; exit 0; }

T=$(mktemp -d)
trap 'rm -rf "$T"' EXIT
PR=10857
BOT='loom-fleet-dispatch[bot]'

TESTS_RUN=0
TESTS_FAILED=0
check() { # <expected> <actual> <message>
    TESTS_RUN=$((TESTS_RUN + 1))
    if [[ "$1" == "$2" ]]; then
        echo "  PASS: $3"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo "  FAIL: $3"
        echo "    expected: '$1'"
        echo "    actual:   '$2'"
    fi
}

# --- the shipped tick, verbatim but for the PR number placeholder -----------
awk '/^```bash$/{f=1;next} /^```$/{f=0} f' "$HOLD_MD" | sed "s/^PR_NUMBER=<number>$/PR_NUMBER=$PR/" >"$T/tick.sh"
grep -q "^PR_NUMBER=$PR$" "$T/tick.sh" || { echo "FAIL: could not extract the tick from $HOLD_MD"; exit 1; }

# --- disposable repository: A, then tree-identical B and C, then D changes ---
R="$T/repo"
g() { git -C "$R" -c user.name=t -c user.email=t@example.com "$@"; }
mkdir -p "$R/.github/workflows"
g init --quiet .
echo "on: push" >"$R/.github/workflows/ci.yml"
g add -A && g commit --quiet -m "the PR's change"
A=$(g rev-parse HEAD)
g commit --quiet --allow-empty -m "chore: re-date required checks (#8248 guard, automated by #8508)"
B=$(g rev-parse HEAD)
g commit --quiet --allow-empty -m "chore: re-run CI against current main (tree-identical no-op)"
C=$(g rev-parse HEAD)
echo "on: [push, pull_request]" >"$R/.github/workflows/ci.yml"
# Worded like a no-op on purpose: the message must not be evidence.
g commit --quiet -am "chore: re-date required checks (tree-identical no-op)"
D=$(g rev-parse HEAD)
ABSENT=0123456789abcdef0123456789abcdef01234567

# --- stubs ---------------------------------------------------------------------
mkdir -p "$T/bin" "$T/w/.loom/scripts"
cat >"$T/bin/gh" <<EOF
#!/usr/bin/env bash
S="$T"
printf '%s\n' "\$*" >>"\$S/gh.argv"
case "\$1 \$2" in
  "pr view")
    case "\$*" in
      *"--json comments,labels,headRefOid"*)
        jq -n --slurpfile c "\$S/comments.json" --arg h "\$(cat "\$S/head")" --rawfile l "\$S/labels" \
          '{comments: \$c[0], labels: (\$l | split("\n") | map(select(length > 0)) | map({name: .})), headRefOid: \$h}' ;;
      *) echo BLOCKED ;;
    esac ;;
  "pr edit")
    if [ "\$4" = --add-label ]; then
      echo "LABEL+:\$5" >>"\$S/writes"; grep -qxF "\$5" "\$S/labels" || echo "\$5" >>"\$S/labels"
    else
      echo "LABEL-:\$5" >>"\$S/writes"; grep -vxF "\$5" "\$S/labels" >"\$S/l.tmp"; mv "\$S/l.tmp" "\$S/labels"
    fi ;;
  "pr diff") echo ".github/workflows/ci.yml" ;;
  "api repos/{owner}/{repo}/issues/$PR/events")
    f=""; while [ \$# -gt 0 ]; do [ "\$1" = --jq ] && f=\$2; shift; done
    jq -r ".[]? as \\\$e | [\\\$e] | \$f" "\$S/events.json" ;;
esac
exit 0
EOF
cat >"$T/bin/loom-daemon" <<EOF
#!/usr/bin/env bash
S="$T"
case "\$1 \$2" in
  "park-record apply")
    echo "TRIGGER:\${LOOM_HOLD_EQUIVALENCE:-}" >>"\$S/triggers"
    echo "LABEL+:loom:blocked" >>"\$S/writes"; grep -qxF loom:blocked "\$S/labels" || echo loom:blocked >>"\$S/labels" ;;
  "forge trusted-comments")
    [ -e "\$S/trust_down" ] && { echo "forge trusted-comments: --fetch failed" >&2; exit 1; }
    jq -c --arg bot "$BOT" '[.[] | select(.author.login == \$bot or .author.login == "rjwalters")]' "\$S/comments.json" ;;
  "forge merge-queue")
    [ "\$3" = mode ] || exit 0
    case "\$(cat "\$S/qmode" 2>/dev/null)" in
      queue) echo "merge-queue: mode=queue source=config execution=enabled" ;;
      broken) exit 2 ;;
      *) echo "merge-queue: mode=direct source=config execution=enabled" ;;
    esac ;;
  "forge verdict-equivalent")
    rec=\$4 head=\$5
    printf '%s\n' "\$*" >>"\$S/ve.argv"
    case "\$(cat "\$S/mode")" in
      unread) echo "loom-daemon forge verdict-equivalent: could not decide — Why: rebase-patch-identical: equivalence=unread — forge compare unreadable after retries" >&2; exit 1 ;;
      baseunread) echo "loom-daemon forge verdict-equivalent: could not decide — Why: equivalence=base-unread — could not read PR #$PR's base branch from the forge: gh exited with status 1" >&2; exit 1 ;;
      scoped)
        # The real verb's rule, stubbed: carry only when --allowed-paths names the whole delta.
        case "\$*" in *"--allowed-paths docs/run.md"*) echo VERDICT_EQUIVALENT=1; echo EQUIVALENCE_KIND=requested-change; exit 0 ;; esac
        echo VERDICT_EQUIVALENT=0; exit 0 ;;
      unknown) echo "loom-daemon forge verdict-equivalent: could not decide — Why: tree: forge compare \$rec...\$head unavailable" >&2; exit 1 ;;
      disabled) echo "loom-daemon forge verdict-equivalent: could not decide — Why: verdict equivalence is switched off (LOOM_VERDICT_TREE_CARVEOUT)" >&2; exit 1 ;;
    esac
    for c in "\$rec" "\$head"; do
      git -C "$R" cat-file -e "\$c^{commit}" 2>/dev/null ||
        { echo "loom-daemon forge verdict-equivalent: could not decide — Why: commit \$c is not in the local clone" >&2; exit 1; }
    done
    if [ "\$(git -C "$R" rev-parse "\$rec^{tree}")" = "\$(git -C "$R" rev-parse "\$head^{tree}")" ]; then
      echo VERDICT_EQUIVALENT=1; echo EQUIVALENCE_KIND=tree
    else
      echo VERDICT_EQUIVALENT=0
    fi ;;
esac
EOF
cat >"$T/w/.loom/scripts/post-comment.sh" <<EOF
#!/usr/bin/env bash
S="$T"
body=\$4
first=\$(printf '%s\n' "\$body" | head -1)
case "\$first" in
  "<!-- champion:critical-file-hold -->") kind=hold ;;
  "<!-- champion:critical-file-release-respected -->") kind=released ;;
  "<!-- champion:critical-file-hold-cleared -->") kind=cleared ;;
  *) kind=other ;;
esac
equiv=\$(printf '%s\n' "\$body" | sed -n 's/.*hold-equivalence kind=\([a-z-]*\).*/\1/p')
grep -q 're-armed' <<<"\$body" && kind="\$kind(rearm)"
echo "COMMENT:\$kind\${equiv:+:\$equiv}" >>"\$S/writes"
why=\$(printf '%s\n' "\$body" | sed -n 's/.*Why: //p' | head -1)
[ -n "\$why" ] && printf '%s\n' "\$why" >"\$S/last_why"
jq --arg b "\$body" --arg a "$BOT" '. + [{author: {login: \$a}, body: \$b}]' "\$S/comments.json" >"\$S/c.tmp" && mv "\$S/c.tmp" "\$S/comments.json"
EOF
cat >"$T/w/.loom/scripts/merge-pr.sh" <<EOF
#!/usr/bin/env bash
echo "MERGE" >>"$T/writes"
EOF
chmod +x "$T/bin/gh" "$T/bin/loom-daemon" "$T/w/.loom/scripts/"*.sh

# --- harness -------------------------------------------------------------------
reset() { # <head>
    echo '[]' >"$T/comments.json"; echo '[]' >"$T/events.json"; : >"$T/labels"; echo "$1" >"$T/head"
    echo repo >"$T/mode"; echo direct >"$T/qmode"; rm -f "$T/trust_down" "$T/last_why" "$T/ve.argv" "$T/triggers"
}
tick() { # [FAIL|PASS] — sets WRITES (";"-joined) and OUT
    : >"$T/writes"
    OUT=$(cd "$T/w" && env -u LOOM_DAEMON_BIN PATH="$T/bin:$PATH" CRITICAL_FILES_RESULT="${1:-FAIL}" bash "$T/tick.sh" 2>&1)
    WRITES=$(paste -sd';' "$T/writes")
}
label() { grep -qxF loom:blocked "$T/labels" && echo present || echo absent; }
push() { echo "$1" >"$T/head"; }
# A removal by <actor type> (#11861): only a human one is a release.
unlabel() { # <User|Bot> <login>
    grep -vxF loom:blocked "$T/labels" >"$T/l.tmp"; mv "$T/l.tmp" "$T/labels"
    jq --arg t "$1" --arg l "$2" '. + [{event: "unlabeled", label: {name: "loom:blocked"}, created_at: "9999", actor: {type: $t, login: $l}}]' \
        "$T/events.json" >"$T/e.tmp" && mv "$T/e.tmp" "$T/events.json"
}
operator_release() { unlabel User rjwalters; }
operator_reassert() { grep -qxF loom:blocked "$T/labels" || echo loom:blocked >>"$T/labels"; }
comment_as() { # <author> <body>
    jq --arg b "$2" --arg a "$1" '. + [{author: {login: $a}, body: $b}]' "$T/comments.json" >"$T/c.tmp"
    mv "$T/c.tmp" "$T/comments.json"
}

echo "--- release survives repository-proven tree-identical moves; a real change re-arms ---"
reset "$A"
tick; check "COMMENT:hold;LABEL+:loom:blocked" "$WRITES" "fresh FAIL at A: hold notice + loom:blocked"
tick; check "LABEL+:loom:blocked" "$WRITES" "repeat tick: hold stands, no second notice"
operator_release
tick; check "COMMENT:released:same-head" "$WRITES" "operator removed the label at A: release acknowledged once, label not re-added"
tick; check "" "$WRITES" "repeat tick at A: no writes at all"
push "$B"
tick; check "COMMENT:released:tree" "$WRITES" "re-date A->B: release re-recorded at B by kind tree, no label"
tick; check "" "$WRITES" "repeat tick at B: idempotent"
push "$C"
tick; check "COMMENT:released:tree" "$WRITES" "second no-op B->C: release carried again"
tick; check "absent" "$(label)" "loom:blocked still absent after the no-op chain"

push "$D"
tick; check "COMMENT:hold(rearm);LABEL+:loom:blocked" "$WRITES" "content change C->D (no-op wording): hold re-armed"
tick; check "LABEL+:loom:blocked" "$WRITES" "repeat tick at D: re-armed hold stands, no second notice"

echo "--- an unacknowledged release survives one or many moves ---"
reset "$A"
tick; operator_release; push "$B"
tick; check "COMMENT:released:tree" "$WRITES" "label removed, then a re-date before any tick: release respected at B"
reset "$A"
tick; operator_release; push "$B"; push "$C"
tick; check "COMMENT:released:tree" "$WRITES" "label removed, then two re-dates before any tick: A->C proven, release respected"
tick; check "" "$WRITES" "and idempotent afterwards"

echo "--- operator reassertion stands ---"
reset "$A"
tick; operator_release; tick; push "$B"; tick
operator_reassert
# Same head: row 1 ("released at this head") answers first and writes nothing,
# so the operator's label is left exactly where they put it.
tick; check "" "$WRITES" "operator re-added the label at the released head: nothing written"
check "present" "$(label)" "label stays on"
push "$C"
tick; check "LABEL+:loom:blocked" "$WRITES" "a no-op move after the reassertion: hold stands, the old release is NOT carried"
check "present" "$(label)" "label still on after the move"

echo "--- every unknown fails closed, naming why ---"
for mode in unknown disabled; do
    reset "$A"; tick; operator_release; tick
    echo "$mode" >"$T/mode"; push "$B"
    tick; check "COMMENT:hold(rearm);LABEL+:loom:blocked" "$WRITES" "equivalence $mode: tree-identical move still re-arms (fail closed)"
    case "$mode" in
        unknown) needle="forge compare" ;;
        disabled) needle="switched off" ;;
    esac
    grep -q "$needle" "$T/last_why" 2>/dev/null; check 0 "$?" "equivalence $mode: the re-arm notice names the reason"
done
reset "$A"; tick; operator_release; tick; push "$ABSENT"
tick; check "COMMENT:hold(rearm);LABEL+:loom:blocked" "$WRITES" "missing commit object: re-arms"
grep -q "not in the local clone" "$T/last_why"; check 0 "$?" "missing commit object: reason surfaced"
# A legacy hold: no hold-state line, so no recorded head to prove anything from.
reset "$B"
comment_as "$BOT" "<!-- champion:critical-file-hold -->
**Champion: Holding for Human Merge — Critical File**"
tick; check "COMMENT:hold(rearm);LABEL+:loom:blocked" "$WRITES" "legacy hold (no recorded head), label absent: re-arms"
grep -q "legacy hold" "$T/last_why"; check 0 "$?" "legacy hold: reason surfaced"

echo "--- #13114: unread compare, requested-change carry ---"
reset "$A"; tick; operator_release; tick; echo unread >"$T/mode"; push "$D"
tick; check "COMMENT:hold(rearm);LABEL+:loom:blocked" "$WRITES" "unreadable compare: re-arms"
grep -q "equivalence=unread" "$T/last_why"; check 0 "$?" "unread: the notice quotes equivalence=unread"
grep -qx "TRIGGER:unread" "$T/triggers"; check 0 "$?" "unread: the park write is tagged for hold.transition telemetry"
reset "$A"; tick; operator_release; tick; echo unknown >"$T/mode"; push "$D"; tick
grep -qx "TRIGGER:" "$T/triggers"; check 0 "$?" "a changed/unknown re-arm is not tagged unread"

echo "--- #13149: unreadable PR base branch ---"
reset "$A"; tick; operator_release; tick; echo baseunread >"$T/mode"; push "$D"; tick
grep -qx "TRIGGER:base-unread" "$T/triggers"; check 0 "$?" "base-unread: the park write carries the closed cause token only"

reset "$A"; tick; operator_release; tick
echo scoped >"$T/mode"; push "$D"
comment_as rjwalters "Doctor: please fix the runbook.
release-scope: docs/run.md"
tick; check "COMMENT:released:requested-change" "$WRITES" "operator release-scope naming the delta: carried as requested-change"
grep -q -- "--allowed-paths docs/run.md" "$T/ve.argv"; check 0 "$?" "the named path is passed to the verb"
reset "$A"; tick; operator_release; tick; echo repo >"$T/mode"; rm -f "$T/ve.argv"; push "$D"
LOOM_REPO=acme/widgets tick
grep -q -- "forge verdict-equivalent --repo acme/widgets" "$T/ve.argv"; check 0 "$?" "LOOM_REPO known: --repo is passed to the verb (#13150)"
reset "$A"; tick; operator_release; tick; echo repo >"$T/mode"; rm -f "$T/ve.argv"; push "$D"; tick
grep -q -- "--repo" "$T/ve.argv"; check 1 "$?" "no LOOM_REPO: the flag is omitted (#13150)"
reset "$A"; tick; operator_release; tick; echo scoped >"$T/mode"; push "$D"
tick; check "COMMENT:hold(rearm);LABEL+:loom:blocked" "$WRITES" "no release-scope line: re-arms"
grep -q -- "--allowed-paths" "$T/ve.argv"; check 1 "$?" "no line, no --allowed-paths"
reset "$A"; tick; operator_release; tick; echo scoped >"$T/mode"; push "$D"
comment_as rjwalters "release-scope: src/other.rs"
tick; check "COMMENT:hold(rearm);LABEL+:loom:blocked" "$WRITES" "release-scope naming a different path: verb refuses, re-arms"
reset "$A"; tick; operator_release; tick; echo scoped >"$T/mode"; push "$D"
comment_as "$BOT" "release-scope: docs/run.md"
tick; check "COMMENT:hold(rearm);LABEL+:loom:blocked" "$WRITES" "a bot's release-scope line is ignored"
reset "$A"; comment_as rjwalters "release-scope: docs/run.md"; tick; operator_release; tick; echo scoped >"$T/mode"; push "$D"
tick; check "COMMENT:hold(rearm);LABEL+:loom:blocked" "$WRITES" "a release-scope line posted BEFORE the hold is ignored"

echo "--- untrusted and quoted markers never carry a release ---"
forged="<!-- champion:critical-file-release-respected -->
<!-- champion:hold-state head=$A -->"
reset "$A"; comment_as outsider "$forged"
tick; check "COMMENT:hold;LABEL+:loom:blocked" "$WRITES" "outsider-forged release at the current head: ignored, fresh hold"
reset "$A"; comment_as "$BOT" "As the notice said:
> $forged"
tick; check "COMMENT:hold;LABEL+:loom:blocked" "$WRITES" "quoted release marker (not at body start): ignored, fresh hold"
reset "$A"; comment_as outsider "$forged"; touch "$T/trust_down"
tick; check "" "$WRITES" "trust filter down + forged release: no release honored, no release written"
grep -q "no release honored" <<<"$OUT"; check 0 "$?" "trust filter down: the deferral is reported"
rm -f "$T/trust_down"
tick; check "COMMENT:hold;LABEL+:loom:blocked" "$WRITES" "trust filter back: the forged marker drops out and the hold lands"
reset "$A"; tick; operator_release; tick; push "$B"; touch "$T/trust_down"
tick; check "" "$WRITES" "trust filter down + genuine release + re-date: deferred, nothing re-added"
rm -f "$T/trust_down"
tick; check "COMMENT:released:tree" "$WRITES" "trust filter back: the genuine release carries to B"

echo "--- a released critical-file FAIL is never merged by the tick; PASS closes the episode ---"
reset "$A"; tick; operator_release; tick; push "$B"; tick; tick
grep -c "MERGE" "$T/writes" >/dev/null; check 1 "$?" "no merge-pr.sh call from any released tick"
grep -qE '^(pr merge|api .*merge)' "$T/gh.argv"; check 1 "$?" "no gh merge call from any tick"
tick PASS; check "LABEL-:loom:blocked;COMMENT:cleared" "$WRITES" "PASS with an open episode: cleared notice"
tick PASS; check "" "$WRITES" "repeat PASS: no writes"

echo "--- #11861: the shared loom:blocked stays while a merge-risk hold stands ---"
reset "$A"
comment_as "$BOT" "<!-- champion:merge-risk-hold -->
<!-- champion:hold-state head=$A -->
held on blast radius"
tick
tick PASS; check "COMMENT:cleared" "$WRITES" "critical-file clears, merge-risk still held: label NOT removed"
check "present" "$(label)" "loom:blocked stays for the standing merge-risk hold"
reset "$A"
comment_as "$BOT" "<!-- champion:merge-risk-hold -->
held"
comment_as "$BOT" "merging
<!-- champion:merge-risk-hold-cleared -->"
tick
tick PASS; check "LABEL-:loom:blocked;COMMENT:cleared" "$WRITES" "merge-risk already reversed: the label comes off"

echo "--- #11861: an unreadable trust filter never clears (fail closed) ---"
reset "$A"
tick; touch "$T/trust_down"
tick PASS; check "" "$WRITES" "trust filter down + PASS: no clear, no label write"
check "present" "$(label)" "loom:blocked kept while the trusted read fails"
rm -f "$T/trust_down"
tick PASS; check "LABEL-:loom:blocked;COMMENT:cleared" "$WRITES" "trust filter back: the clear lands"

echo "--- #12999: the hold notice names the matched suspect line ---"
reset "$A"
CRITERION3_SUSPECT_LINE='+serde = "1"' CRITERION3_SUSPECT_SECTION=dependencies tick
check "COMMENT:hold;LABEL+:loom:blocked" "$WRITES" "a suspect-line FAIL holds as before"
check "1" "$(jq -r '[.[].body | select(contains("**Matched suspect line** (in `[dependencies]`): `+serde = \"1\"`"))] | length' "$T/comments.json")" \
    "the hold comment shows the matched line and its section"
reset "$A"
tick
check "0" "$(jq -r '[.[].body | select(contains("Matched suspect line"))] | length' "$T/comments.json")" \
    "a plain pattern hit (no suspect line) carries no matched-line bullet"

handoff() { grep -q "^CF-RELEASED-HANDOFF pr=$PR head=$1" <<<"$OUT" && echo yes || echo no; }

echo "--- #13321: a released critical-file PR on a queue repo is handed to the queue ---"
reset "$A"; echo queue >"$T/qmode"
tick; check "no" "$(handoff "$A")" "fresh hold on a queue repo: not handed off"
operator_release
tick; check "COMMENT:released:same-head" "$WRITES" "queue repo: release recorded as before"
check "yes" "$(handoff "$A")" "release at the current head: handoff pinned to the released head"
grep -q 'merge queue' "$T/comments.json"; check 0 "$?" "release comment says the queue merges it, not the operator"
tick; check "yes" "$(handoff "$A")" "repeat tick at the released head: handoff persists"
push "$B"; tick
check "yes" "$(handoff "$B")" "tree-identical head move: carried by equivalence, handoff at the new head"
push "$D"; tick
check "COMMENT:hold(rearm);LABEL+:loom:blocked" "$WRITES" "a new head that is not equivalent re-arms the hold"
check "no" "$(handoff "$D")" "re-armed at a moved head: no handoff"
tick; check "no" "$(handoff "$D")" "label back on: still no handoff"

echo "--- #13321: unreleased, untrusted, shared-label and direct cases never hand off ---"
reset "$A"; echo queue >"$T/qmode"; tick; tick
check "no" "$(handoff "$A")" "held, never released: no handoff"
reset "$A"; echo queue >"$T/qmode"; tick; operator_release; tick; operator_reassert; tick
check "no" "$(handoff "$A")" "operator re-added loom:blocked (shared with merge-risk): no handoff"
reset "$A"; echo queue >"$T/qmode"; comment_as outsider "$forged"
tick; check "no" "$(handoff "$A")" "outsider-forged release marker: no handoff"
reset "$A"; echo queue >"$T/qmode"; comment_as outsider "$forged"; touch "$T/trust_down"
tick; check "no" "$(handoff "$A")" "forged release, trust filter down: no handoff"
reset "$A"; echo queue >"$T/qmode"; tick; operator_release; tick; push "$B"; touch "$T/trust_down"
tick; check "no" "$(handoff "$B")" "genuine release but trust filter down: deferred, no handoff"
reset "$A"; echo queue >"$T/qmode"; comment_as "$BOT" "As the notice said:
> $forged"
tick; check "no" "$(handoff "$A")" "quoted release marker: no handoff"
reset "$A"; tick; operator_release; tick
check "no" "$(handoff "$A")" "direct-mode repo: released PR is not handed off (operator merges)"
grep -q 'merge-pr.sh' "$T/comments.json"; check 0 "$?" "direct mode: release comment still points at merge-pr.sh"
reset "$A"; echo broken >"$T/qmode"; tick; operator_release; tick
check "no" "$(handoff "$A")" "mode unreadable: fails closed to direct, no handoff"
reset "$A"; echo queue >"$T/qmode"; tick PASS
check "no" "$(handoff "$A")" "PASS (no critical file): the tick emits no handoff"

echo
echo "Results: $((TESTS_RUN - TESTS_FAILED))/$TESTS_RUN passed, $TESTS_FAILED failed"
[[ $TESTS_FAILED -eq 0 ]]
