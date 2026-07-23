#!/bin/bash
# test-sweep-checkpoint.sh - Smoke tests for the sweep-checkpoint helper.
#
# These exercise the read/write/delete/exists/phase/list commands and the
# expected exit codes documented in sweep-checkpoint.sh and consumed by
# defaults/.claude/commands/loom/sweep.md (#3373).
#
# Run from anywhere — uses an isolated TMPDIR for the checkpoint directory so
# it never touches a real workspace's .loom/sweep-checkpoint/.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HELPER="$SCRIPT_DIR/../sweep-checkpoint.sh"

if [[ ! -x "$HELPER" ]]; then
    echo "FAIL: helper not executable at $HELPER" >&2
    exit 1
fi

# Isolated workspace — make sure we don't pollute the real .loom/sweep-checkpoint/
TMP_REPO="$(mktemp -d)"
trap 'rm -rf "$TMP_REPO"' EXIT

cd "$TMP_REPO" || exit 1
git init -q .
mkdir -p .loom/scripts
# Use a script-relative copy so `repo_root` lands here, not in the real loom checkout.
cp "$HELPER" .loom/scripts/sweep-checkpoint.sh
chmod +x .loom/scripts/sweep-checkpoint.sh

CHECKPOINT="$TMP_REPO/.loom/scripts/sweep-checkpoint.sh"

PASS=0
FAIL=0
assert() {
    local desc="$1"; shift
    if "$@" >/dev/null 2>&1; then
        echo "PASS: $desc"
        PASS=$((PASS + 1))
    else
        echo "FAIL: $desc (cmd: $*)" >&2
        FAIL=$((FAIL + 1))
    fi
}
assert_exit() {
    local desc="$1" expected="$2"; shift 2
    "$@" >/dev/null 2>&1
    local actual=$?
    if [[ $actual -eq $expected ]]; then
        echo "PASS: $desc (exit $actual)"
        PASS=$((PASS + 1))
    else
        echo "FAIL: $desc (expected exit $expected, got $actual)" >&2
        FAIL=$((FAIL + 1))
    fi
}
assert_eq() {
    local desc="$1" expected="$2" actual="$3"
    if [[ "$actual" == "$expected" ]]; then
        echo "PASS: $desc"
        PASS=$((PASS + 1))
    else
        echo "FAIL: $desc (expected '$expected', got '$actual')" >&2
        FAIL=$((FAIL + 1))
    fi
}

# 1. exists on missing checkpoint → exit 1
assert_exit "exists returns 1 when missing" 1 "$CHECKPOINT" exists 42

# 2. phase on missing → empty output, exit 0
out=$("$CHECKPOINT" phase 42)
assert_eq "phase is empty when no checkpoint" "" "$out"

# 3. write curator-done
assert "write curator-done succeeds" "$CHECKPOINT" write 42 curator-done --task-id sweep-test

# 4. exists now returns 0
assert_exit "exists returns 0 after write" 0 "$CHECKPOINT" exists 42

# 5. phase returns curator-done
out=$("$CHECKPOINT" phase 42)
assert_eq "phase reads back curator-done" "curator-done" "$out"

# 6. read produces valid JSON containing the phase
out=$("$CHECKPOINT" read 42)
if echo "$out" | grep -q '"phase": "curator-done"'; then
    echo "PASS: read JSON contains phase=curator-done"
    PASS=$((PASS + 1))
else
    echo "FAIL: read JSON missing phase: $out" >&2
    FAIL=$((FAIL + 1))
fi

# 7. write builder-done with PR number
assert "write builder-done with pr-number" "$CHECKPOINT" write 42 builder-done --task-id sweep-test --pr-number 999
out=$("$CHECKPOINT" read 42)
if echo "$out" | grep -q '"pr_number": 999'; then
    echo "PASS: pr_number persisted as integer"
    PASS=$((PASS + 1))
else
    echo "FAIL: pr_number missing or wrong: $out" >&2
    FAIL=$((FAIL + 1))
fi

# 8. Invalid phase → exit 2
assert_exit "invalid phase exits 2" 2 "$CHECKPOINT" write 42 bogus-phase

# 9. Invalid issue number → exit 1
assert_exit "non-numeric issue exits 1" 1 "$CHECKPOINT" write abc curator-done

# 10. list shows the issue
out=$("$CHECKPOINT" list)
assert_eq "list reports issue 42" "42" "$out"

# 11. Add a second checkpoint and verify sorted listing
"$CHECKPOINT" write 7 judge-done --task-id sweep-test --pr-number 100 >/dev/null
out=$("$CHECKPOINT" list | tr '\n' ' ' | sed 's/ $//')
assert_eq "list returns sorted numeric order" "7 42" "$out"

# 12. delete removes the file
assert "delete succeeds" "$CHECKPOINT" delete 42
assert_exit "exists returns 1 after delete" 1 "$CHECKPOINT" exists 42

# 13. delete on already-missing is a no-op (exit 0)
assert_exit "delete-missing exits 0" 0 "$CHECKPOINT" delete 42

# 14. Atomic-write semantics: no stray .tmp.* files
strays=$(find "$TMP_REPO/.loom/sweep-checkpoint" -name 'issue-*.tmp.*' 2>/dev/null | wc -l | tr -d ' ')
assert_eq "no stray .tmp files after writes" "0" "$strays"

# 15. All valid phases accepted
for phase in curator-done builder-done judge-done doctor-done merge-done; do
    if "$CHECKPOINT" write 1 "$phase" --task-id t >/dev/null 2>&1; then
        echo "PASS: phase '$phase' accepted"
        PASS=$((PASS + 1))
    else
        echo "FAIL: phase '$phase' rejected" >&2
        FAIL=$((FAIL + 1))
    fi
done

# --- Optional attempt field (#3481, model escalation bookkeeping) ---

# 16. write with --attempt round-trips through read and attempt
assert "write doctor-done with --attempt 2" "$CHECKPOINT" write 50 doctor-done --task-id t --pr-number 123 --attempt 2
out=$("$CHECKPOINT" read 50)
if echo "$out" | grep -q '"attempt": 2'; then
    echo "PASS: attempt persisted as integer in JSON"
    PASS=$((PASS + 1))
else
    echo "FAIL: attempt missing or wrong: $out" >&2
    FAIL=$((FAIL + 1))
fi
out=$("$CHECKPOINT" attempt 50)
assert_eq "attempt command reads back 2" "2" "$out"

# 17. pr_number still intact alongside attempt
out=$("$CHECKPOINT" read 50)
if echo "$out" | grep -q '"pr_number": 123'; then
    echo "PASS: pr_number coexists with attempt"
    PASS=$((PASS + 1))
else
    echo "FAIL: pr_number lost when attempt present: $out" >&2
    FAIL=$((FAIL + 1))
fi

# 18. Backward compat: write WITHOUT --attempt omits the field entirely
"$CHECKPOINT" write 51 builder-done --task-id t >/dev/null
out=$("$CHECKPOINT" read 51)
if echo "$out" | grep -q '"attempt"'; then
    echo "FAIL: attempt field should be omitted when not provided: $out" >&2
    FAIL=$((FAIL + 1))
else
    echo "PASS: attempt field omitted when not provided"
    PASS=$((PASS + 1))
fi

# 19. Legacy checkpoint (no attempt field): attempt prints empty, exit 0
out=$("$CHECKPOINT" attempt 51)
assert_eq "attempt is empty on legacy checkpoint (= attempt 1)" "" "$out"
assert_exit "attempt on legacy checkpoint exits 0" 0 "$CHECKPOINT" attempt 51

# 20. Legacy checkpoint read path unaffected (phase still resolves)
out=$("$CHECKPOINT" phase 51)
assert_eq "phase still reads on attempt-less checkpoint" "builder-done" "$out"

# 21. attempt on missing checkpoint: empty output, exit 0 (mirrors phase)
out=$("$CHECKPOINT" attempt 9999)
assert_eq "attempt is empty when no checkpoint" "" "$out"
assert_exit "attempt on missing checkpoint exits 0" 0 "$CHECKPOINT" attempt 9999

# 22. Invalid --attempt values rejected with exit 1
assert_exit "non-numeric --attempt exits 1" 1 "$CHECKPOINT" write 52 doctor-done --attempt abc
assert_exit "zero --attempt exits 1" 1 "$CHECKPOINT" write 52 doctor-done --attempt 0
assert_exit "negative --attempt exits 1" 1 "$CHECKPOINT" write 52 doctor-done --attempt -1
if "$CHECKPOINT" exists 52 >/dev/null 2>&1; then
    echo "FAIL: rejected --attempt write should not create a checkpoint" >&2
    FAIL=$((FAIL + 1))
else
    echo "PASS: rejected --attempt write leaves no checkpoint behind"
    PASS=$((PASS + 1))
fi

# 23. Overwrite an attempt-bearing checkpoint without --attempt drops the field
"$CHECKPOINT" write 50 doctor-done --task-id t >/dev/null
out=$("$CHECKPOINT" attempt 50)
assert_eq "attempt cleared after attempt-less rewrite" "" "$out"

# --- Optional model field (#3482, Phase 3a per-model observability) ---

# 24. write with --model round-trips through read and model
assert "write doctor-done with --model" "$CHECKPOINT" write 60 doctor-done --task-id t --pr-number 321 --attempt 2 --model claude-opus-4-8
out=$("$CHECKPOINT" read 60)
if echo "$out" | grep -q '"model": "claude-opus-4-8"'; then
    echo "PASS: model persisted as string in JSON"
    PASS=$((PASS + 1))
else
    echo "FAIL: model missing or wrong: $out" >&2
    FAIL=$((FAIL + 1))
fi
out=$("$CHECKPOINT" model 60)
assert_eq "model command reads back claude-opus-4-8" "claude-opus-4-8" "$out"

# 25. pr_number and attempt still intact alongside model
out=$("$CHECKPOINT" read 60)
if echo "$out" | grep -q '"pr_number": 321' && echo "$out" | grep -q '"attempt": 2'; then
    echo "PASS: pr_number and attempt coexist with model"
    PASS=$((PASS + 1))
else
    echo "FAIL: pr_number/attempt lost when model present: $out" >&2
    FAIL=$((FAIL + 1))
fi

# 26. Backward compat: write WITHOUT --model omits the field entirely
"$CHECKPOINT" write 61 builder-done --task-id t >/dev/null
out=$("$CHECKPOINT" read 61)
if echo "$out" | grep -q '"model"'; then
    echo "FAIL: model field should be omitted when not provided: $out" >&2
    FAIL=$((FAIL + 1))
else
    echo "PASS: model field omitted when not provided"
    PASS=$((PASS + 1))
fi

# 27. Legacy checkpoint (no model field): model prints empty, exit 0
out=$("$CHECKPOINT" model 61)
assert_eq "model is empty on legacy checkpoint (= default/unknown)" "" "$out"
assert_exit "model on legacy checkpoint exits 0" 0 "$CHECKPOINT" model 61

# 28. Legacy checkpoint read path unaffected (phase still resolves)
out=$("$CHECKPOINT" phase 61)
assert_eq "phase still reads on model-less checkpoint" "builder-done" "$out"

# 29. model on missing checkpoint: empty output, exit 0 (mirrors phase/attempt)
out=$("$CHECKPOINT" model 9999)
assert_eq "model is empty when no checkpoint" "" "$out"
assert_exit "model on missing checkpoint exits 0" 0 "$CHECKPOINT" model 9999

# 30. Invalid --model values rejected with exit 1 (JSON-unsafe charset)
assert_exit "model with quote exits 1" 1 "$CHECKPOINT" write 62 doctor-done --model 'op"us'
assert_exit "model with space exits 1" 1 "$CHECKPOINT" write 62 doctor-done --model 'op us'
if "$CHECKPOINT" exists 62 >/dev/null 2>&1; then
    echo "FAIL: rejected --model write should not create a checkpoint" >&2
    FAIL=$((FAIL + 1))
else
    echo "PASS: rejected --model write leaves no checkpoint behind"
    PASS=$((PASS + 1))
fi

# 31. Alias model values accepted (sonnet/opus/haiku)
assert "alias model 'sonnet' accepted" "$CHECKPOINT" write 63 judge-done --model sonnet
out=$("$CHECKPOINT" model 63)
assert_eq "alias model reads back" "sonnet" "$out"

# 32. Overwrite a model-bearing checkpoint without --model drops the field
"$CHECKPOINT" write 60 doctor-done --task-id t >/dev/null
out=$("$CHECKPOINT" model 60)
assert_eq "model cleared after model-less rewrite" "" "$out"

# --- Doctor-cycle cap raise: attempt > 2 (issue #3668) ---
# The configurable sweep.max_doctor_cycles cap and the default-cap distinct-defect
# grace cycle both write attempt values above 2 (cycle 2 -> attempt 3, etc.).
# Confirm the checkpoint schema round-trips them (no plumbing change needed).

# 33. write with --attempt 3 round-trips through read and attempt
assert "write doctor-done with --attempt 3 (second Doctor cycle)" "$CHECKPOINT" write 70 doctor-done --task-id t --pr-number 700 --attempt 3
out=$("$CHECKPOINT" read 70)
if echo "$out" | grep -q '"attempt": 3'; then
    echo "PASS: attempt 3 persisted as integer in JSON"
    PASS=$((PASS + 1))
else
    echo "FAIL: attempt 3 missing or wrong: $out" >&2
    FAIL=$((FAIL + 1))
fi
out=$("$CHECKPOINT" attempt 70)
assert_eq "attempt command reads back 3" "3" "$out"

# 34. A larger raised cap (e.g. max_doctor_cycles=5 -> cycle 5 -> attempt 6) round-trips
assert "write doctor-done with --attempt 6 (raised cap)" "$CHECKPOINT" write 71 doctor-done --task-id t --attempt 6
out=$("$CHECKPOINT" attempt 71)
assert_eq "attempt command reads back 6" "6" "$out"

# --- Stable per-sweep-run task_id (#3768) ---
# sweep.md now threads a stable RUN_ID as --task-id at every call site. The
# checkpoint round-trips an arbitrary stable id, and the omitted-flag fallback
# still yields a parseable checkpoint (last-resort only).

# 35. A stable run id passed as --task-id round-trips into the JSON verbatim.
STABLE_RID="sweep-20260722T231500Z-84213-a3f9c1"
assert "write with stable RUN_ID task-id" "$CHECKPOINT" write 80 builder-done --task-id "$STABLE_RID" --pr-number 800
out=$("$CHECKPOINT" read 80)
if echo "$out" | grep -q "\"task_id\": \"$STABLE_RID\""; then
    echo "PASS: stable RUN_ID persisted verbatim as task_id"
    PASS=$((PASS + 1))
else
    echo "FAIL: stable RUN_ID task_id missing/wrong: $out" >&2
    FAIL=$((FAIL + 1))
fi

# 36. Legacy sweep-<pid> task_id still parses cleanly (free-form field, no schema change).
"$CHECKPOINT" write 81 curator-done --task-id "sweep-12345" >/dev/null
out=$("$CHECKPOINT" phase 81)
assert_eq "legacy sweep-<pid> task_id checkpoint still reads phase" "curator-done" "$out"

# 37. Omitted --task-id falls back to a parseable (clearly-labelled fallback) id.
"$CHECKPOINT" write 82 curator-done >/dev/null
out=$("$CHECKPOINT" read 82)
if echo "$out" | grep -Eq '"task_id": "sweep-run-fallback-[0-9]+"'; then
    echo "PASS: omitted --task-id uses labelled fallback default"
    PASS=$((PASS + 1))
else
    echo "FAIL: fallback task_id default unexpected: $out" >&2
    FAIL=$((FAIL + 1))
fi
out=$("$CHECKPOINT" phase 82)
assert_eq "fallback-default checkpoint still reads phase" "curator-done" "$out"

echo
echo "Results: $PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]] || exit 1
