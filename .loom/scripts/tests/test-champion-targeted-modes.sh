#!/usr/bin/env bash
# test-champion-targeted-modes.sh - Champion's targeted and non-item modes
# (#11859): `/loom:champion --pr N`, `--issue N`, `--non-item`.
#
# THE GAP: the step dispatcher hands Champion one item per step, but
# champion.md had no argument handling: it always discovered its own queues.
# And a targeted merge could not pin the head it evaluated (merge-pr.sh read
# its own), so a push after evaluation was merged under a stale verdict.
#
# WHAT THIS SUITE DOES
#   1. BEHAVIOUR -- champion-pr-merge.md's Step 3 block is EXTRACTED and RUN
#      against stubs: with no EXPECTED_HEAD (batch) it is unchanged: the head
#      comes from its own read and merge-pr.sh gets no --expected-head. With
#      EXPECTED_HEAD set (--pr N) the evaluated SHA is the queue's
#      --approved-sha and merge-pr.sh's --expected-head, with no head re-read;
#      merge-pr.sh's exit 8 re-queues and is never "Merge failed".
#   2. WIRING -- champion.md routes the three arguments to
#      champion-targeted.md and leaves the no-argument batch pass in place;
#      champion-targeted.md defines each mode's outcomes, the --issue verdict
#      set lives in ONE table, and --non-item skips both item queues.
#
# Hermetic: file reads plus a mktemp -d dir with stub gh / git / loom-daemon /
# merge-pr.sh. No forge, no network.

# SC2034: MARGV / QARGV / VERDICTS are read inside `check ... eval '...'` strings.
# shellcheck disable=SC2034

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
pick_dir() { if [[ -d "$REPO_ROOT/$1" ]]; then echo "$REPO_ROOT/$1"; else echo "$REPO_ROOT/$2"; fi; }
ROLE_DIR="$(pick_dir defaults/.claude/commands/loom .claude/commands/loom)"
CHAMPION_MD="$ROLE_DIR/champion.md"
TARGETED_MD="$ROLE_DIR/champion-targeted.md"
PR_MERGE_MD="$ROLE_DIR/champion-pr-merge.md"

PASS=0; FAIL=0
ok()   { PASS=$((PASS+1)); echo "  PASS: $1"; }
fail() { FAIL=$((FAIL+1)); echo "  FAIL: $1"; }
check() { local d="$1"; shift; if "$@"; then ok "$d"; else fail "$d"; echo "----- output -----"; echo "${out:-}"; echo "------------------"; fi; }
has()   { [[ "$out" == *"$1"* ]]; }
hasnt() { [[ "$out" != *"$1"* ]]; }
doc_has()   { grep -qF -- "$2" "$1"; }
doc_lacks() { ! grep -qF -- "$2" "$1"; }

for f in "$CHAMPION_MD" "$TARGETED_MD" "$PR_MERGE_MD"; do
  [[ -f "$f" ]] || { echo "FAIL: missing $f"; exit 1; }
done

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/bin" "$TMP/.loom/scripts"

# ---------------------------------------------------------------------------
echo "--- 1. Step 3 merge block, batch vs --pr N ---"
awk '/^```bash$/{buf="";inb=1;next} /^```$/{if(inb&&buf~/merge-queue step/){printf "%s",buf;exit} inb=0} inb{buf=buf $0 "\n"}' "$PR_MERGE_MD" > "$TMP/step3.sh"
[[ -s "$TMP/step3.sh" ]] || { echo "FAIL: could not extract Step 3 block"; exit 1; }
bash -n "$TMP/step3.sh" || { echo "FAIL: Step 3 block is not valid bash"; exit 1; }

# git: only `rev-parse --git-common-dir` answers (where the --pr pin file lives).
mkdir -p "$TMP/gitdir"
cat > "$TMP/bin/git" <<S
#!/usr/bin/env bash
[ "\$*" = "rev-parse --git-common-dir" ] && echo "$TMP/gitdir"
exit 0
S
cat > "$TMP/bin/gh" <<S
#!/usr/bin/env bash
case "\$*" in
  *"--json headRefOid"*) echo HEAD_READ >> "$TMP/gh-calls"; echo "\${FAKE_HEAD:-1111111111111111111111111111111111111111}" ;;
esac
exit 0
S
cat > "$TMP/bin/loom-daemon" <<S
#!/usr/bin/env bash
case "\$*" in
  "forge merge-queue step "*) printf '%s\n' "\$*" >> "$TMP/queue-argv"; echo LOOM-MERGE-QUEUE-DIRECT; exit 0 ;;
esac
exit 99
S
cat > "$TMP/.loom/scripts/merge-pr.sh" <<S
#!/usr/bin/env bash
printf '%s\n' "\$*" > "$TMP/merge-argv"
exit \${MERGE_STUB_RC:-0}
S
chmod +x "$TMP"/bin/* "$TMP/.loom/scripts/merge-pr.sh"

EVAL_SHA=2222222222222222222222222222222222222222
run_step3() { # [EXPECTED_HEAD]
  rm -f "$TMP/gh-calls" "$TMP/queue-argv" "$TMP/merge-argv"
  if [[ -n "${1:-}" ]]; then
    out=$( cd "$TMP" && EXPECTED_HEAD="$1" PATH="$TMP/bin:$PATH" bash "$TMP/step3.sh" 42 2>&1 )
  else
    out=$( cd "$TMP" && env -u EXPECTED_HEAD PATH="$TMP/bin:$PATH" bash "$TMP/step3.sh" 42 2>&1 )
  fi
  MARGV="$(cat "$TMP/merge-argv" 2>/dev/null)"; QARGV="$(cat "$TMP/queue-argv" 2>/dev/null)"
}

run_step3
check "batch: merge-pr.sh argv is exactly today's" eval '[[ "$MARGV" == "42 --auto --redate-stale-checks" ]]'
check "batch: the head comes from Step 3's own read" eval '[[ -s "$TMP/gh-calls" ]] && [[ "$QARGV" == *"--approved-sha 1111111111111111111111111111111111111111"* ]]'

run_step3 "$EVAL_SHA"
check "--pr N: merge-pr.sh gets --expected-head <evaluated sha>" eval '[[ "$MARGV" == "42 --auto --redate-stale-checks --expected-head $EVAL_SHA" ]]'
check "--pr N: the queue authorizes the evaluated sha, not a fresh read" eval '[[ "$QARGV" == *"--approved-sha $EVAL_SHA"* ]] && [[ ! -s "$TMP/gh-calls" ]]'
check "--pr N, merged: outcome rc=0" has "CHAMPION-MERGE-OUTCOME pr=42 rc=0"

export MERGE_STUB_RC=8
run_step3 "$EVAL_SHA"
check "exit 8 (head moved after evaluation) re-queues, never 'Merge failed'" \
  eval 'has "not merged this pass (exit 8)" && hasnt "Merge failed" && has "CHAMPION-MERGE-OUTCOME pr=42 rc=8"'
export MERGE_STUB_RC=1
run_step3 "$EVAL_SHA"
check "exit 1 is still a failure under --pr N" eval 'has "Merge failed for PR #42"'
unset MERGE_STUB_RC

echo "--- 1b. the --pr pin file: fail closed when the pin does not reach Step 3 ---"
PIN_FILE="$TMP/gitdir/loom-champion-pin-pr-42"
printf '%s\n' "$EVAL_SHA" > "$PIN_FILE"
run_step3
check "pin file + NO EXPECTED_HEAD (the env var was lost between calls): no merge, no queue call" \
  eval '[[ ! -e "$TMP/merge-argv" && ! -e "$TMP/queue-argv" ]]'
check "pin file + no EXPECTED_HEAD: CHAMPION-PIN-MISSING names the SHA to write, outcome rc=9 (re-queued, #11861)" \
  eval 'has "CHAMPION-PIN-MISSING pr=42: set EXPECTED_HEAD=$EVAL_SHA" && has "CHAMPION-MERGE-OUTCOME pr=42 rc=9"'
check "a lost pin is a re-queue, never 'Merge failed' (#11861)" \
  eval 'has "not merged this pass (exit 9)" && hasnt "Merge failed"'
run_step3 1111111111111111111111111111111111111111
check "pin file + a DIFFERENT EXPECTED_HEAD: no merge" eval '[[ ! -e "$TMP/merge-argv" ]] && has CHAMPION-PIN-MISSING'
run_step3 "$EVAL_SHA"
check "pin file + the literal pinned SHA: merges pinned" \
  eval '[[ "$MARGV" == "42 --auto --redate-stale-checks --expected-head $EVAL_SHA" ]] && hasnt CHAMPION-PIN-MISSING'
touch -t 202001010000 "$PIN_FILE"
run_step3
check "a pin file over 60 minutes old (a crashed step) no longer blocks the batch merge" \
  eval '[[ "$MARGV" == "42 --auto --redate-stale-checks" ]] && hasnt CHAMPION-PIN-MISSING'
# #11902: the pin records its step's agent process ($PPID); while that runs, a
# step longer than 60 minutes keeps its backstop (up to 24h).
sleep 600 & LIVE_PID=$!
( exit 0 ) & DEAD_PID=$!; wait "$DEAD_PID"
two_hours_ago() { touch -t "$(date -r $(( $(date +%s) - 7200 )) +%Y%m%d%H%M 2>/dev/null || date -d @$(( $(date +%s) - 7200 )) +%Y%m%d%H%M)" "$PIN_FILE"; }
printf '%s %s\n' "$EVAL_SHA" "$LIVE_PID" > "$PIN_FILE"; two_hours_ago
run_step3
check "a 2h-old pin whose step process still runs (a long --pr step) still refuses an unpinned merge" \
  eval '[[ ! -e "$TMP/merge-argv" ]] && has "CHAMPION-PIN-MISSING pr=42: set EXPECTED_HEAD=$EVAL_SHA"'
run_step3 "$EVAL_SHA"
check "the long step's literal pin still merges pinned" \
  eval '[[ "$MARGV" == "42 --auto --redate-stale-checks --expected-head $EVAL_SHA" ]]'
printf '%s %s\n' "$EVAL_SHA" "$DEAD_PID" > "$PIN_FILE"; two_hours_ago
run_step3
check "a 2h-old pin whose step process is gone (a crashed step) does not block the batch merge" \
  eval '[[ "$MARGV" == "42 --auto --redate-stale-checks" ]] && hasnt CHAMPION-PIN-MISSING'
printf '%s %s\n' "$EVAL_SHA" "$LIVE_PID" > "$PIN_FILE"; touch -t 202001010000 "$PIN_FILE"
run_step3
check "a live process cannot hold a pin past 24h (pid reuse)" \
  eval '[[ "$MARGV" == "42 --auto --redate-stale-checks" ]] && hasnt CHAMPION-PIN-MISSING'
printf '%s %s\n' "$EVAL_SHA" "$LIVE_PID" > "$PIN_FILE"
run_step3
check "a fresh pin with a recorded pid reads the SHA alone" \
  eval 'has "CHAMPION-PIN-MISSING pr=42: set EXPECTED_HEAD=$EVAL_SHA in this block"'
kill "$LIVE_PID" 2>/dev/null; wait "$LIVE_PID" 2>/dev/null
rm -f "$PIN_FILE"

# ---------------------------------------------------------------------------
echo "--- 2. champion.md routing ---"
check "champion.md reads \$ARGUMENTS" doc_has "$CHAMPION_MD" '**Arguments**: `$ARGUMENTS`'
check "the three arguments route to champion-targeted.md" \
  doc_has "$CHAMPION_MD" '`--pr N`, `--issue N` or `--non-item`: follow `champion-targeted.md` instead of "Finding Work"'
check "no arguments: the batch pass, unchanged" doc_has "$CHAMPION_MD" 'No arguments: the batch pass below, unchanged.'
check "champion-targeted.md is load-gated (not charged to every Champion session)" \
  doc_has "$CHAMPION_MD" '| `champion-targeted.md` | `--pr`, `--issue`, `--non-item` modes | `$ARGUMENTS` given |'
for p in "Priority 1: Safe PRs Ready to Auto-Merge" "Priority 2: Quality Issues Ready to Promote" \
         "Priority 3: Architect/Hermit/Auditor Proposals" "Priority 4: Epic Proposals" \
         "Priority 5: Doctor-Cycle-Capped PRs" "Rollout Check Pass"; do
  check "batch pass keeps: $p" doc_has "$CHAMPION_MD" "$p"
done

echo "--- 3. champion-targeted.md modes ---"
check "every mode ends with one CHAMPION-STEP line" \
  doc_has "$TARGETED_MD" '`CHAMPION-STEP mode=<pr|issue|non-item> item=<N|-> outcome=<outcome>`'
check "an unknown or doubled argument refuses and touches nothing" \
  doc_has "$TARGETED_MD" 'Error: /loom:champion takes --pr N, --issue N or --non-item'
# --pr N
check "--pr: writes the pin file only for an eligible PR" doc_has "$TARGETED_MD" '[ -n "$EVALUATED_SHA" ] && printf'
check "--pr: the pin records the step's agent process (#11902)" doc_has "$TARGETED_MD" '"$EVALUATED_SHA" "$PPID" > "$PIN_FILE"'
check "--pr: Step 3 must carry the SHA literally (env does not survive tool calls)" \
  doc_has "$TARGETED_MD" 'first line must be `EXPECTED_HEAD=<that SHA, written out literally>`'
check "--pr: a lost pin is outcome requeued, never merged" doc_has "$TARGETED_MD" '9 (`CHAMPION-PIN-MISSING`): not merged, still approved, no "Merge Failed"'
check "--pr: the pin file is removed at the end of every step, by a literal command" doc_has "$TARGETED_MD" '`rm -f "$(git rev-parse --git-common-dir)/loom-champion-pin-pr-N"`'
check "batch Step 3 never reads a pin it was not given: the guard is a no-op without a pin file" \
  doc_has "$PR_MERGE_MD" 'if [ -n "$PIN" ] && [ "${EXPECTED_HEAD:-}" != "$PIN" ]; then MERGE_RC=9'
check "--pr: full criteria, sticky holds, merge-risk axes, health pass" \
  doc_has "$TARGETED_MD" 'all 6 Safety Criteria with the merge-risk'
check "--pr: exit 8 is outcome head-moved, not a failure" \
  doc_has "$TARGETED_MD" '| `head-moved` | `merge-pr.sh` exit **8**'
check "--pr: never re-reads and retries inside the step" doc_has "$TARGETED_MD" '**Never re-read the head and retry the merge**'
# --issue N
check "--issue: claims with loom:evaluating via the promo per-issue order" \
  doc_has "$TARGETED_MD" 'the Claim with `loom:evaluating`'
check "--issue: the verdict set is one table, the single place to change it" \
  doc_has "$TARGETED_MD" '**This table is the only list of `--issue` outcomes.**'
VERDICTS="$(awk '/^### Promotion verdict set/{on=1;next} on&&/^## /{exit} on&&/^\| `/{print $2}' "$TARGETED_MD" | tr -d '`' | tr '\n' ' ')"
check "--issue: the four terminal verdicts plus closed and not-eligible, no deferred/skipped (#11860)" \
  eval '[[ "$VERDICTS" == "promoted revise blocked operator closed not-eligible " ]]'
check "--issue: revise sends the issue to Curator as loom:triage without loom:curated (#11860)" \
  doc_has "$TARGETED_MD" '`loom:curated` and the claim removed, `loom:triage` added'
check "--issue: a blocked issue is re-evaluated only once its blocker has closed (#11860)" \
  doc_has "$TARGETED_MD" 'or a merged PR, remove `loom:blocked` and go on; else outcome `blocked`'
# --non-item
NONITEM="$(awk '/^## `--non-item`/{on=1} on' "$TARGETED_MD")"
out="$NONITEM"
check "--non-item: names the role-runner knob that launches it" has 'autonomous.roleRunner.championMode: "non-item"'
for q in "Pass 0b, Pass 0c" "Priority 3: architect, hermit and auditor proposals" "Priority 4: epic proposals" \
         "Priority 5: Doctor-cycle-capped PRs" "Rollout Check Pass" "Held-PR Census and Per-PR Digest"; do
  check "--non-item runs: $q" has "$q"
done
check "--non-item skips Priority 1 (loom:pr merges) and Priority 2 (curated)" \
  has '**Skipped:** Priority 1 (`pr-queue --role champion`, every merge, the Held-PR'
check "--non-item: never claims, evaluates or writes a verdict on a curated issue; writes nothing on a loom:pr PR" \
  has 'It never
claims, evaluates or writes a verdict on one, and it writes nothing on a
`loom:pr` PR'
check "--non-item: the census line is still mandatory" has '`Merge-risk holds:` line is still mandatory'

echo "Results: $PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]]
