#!/usr/bin/env bash
# test-provision-codex-hooks.sh — managed Codex hook installation / trust /
# removal tests (issue #4495, epic #4489 Phase 6).
#
# Hermetic: operates ONLY on temporary CODEX_HOME profiles. No Codex CLI, no
# network, no credentials. A recognizable fake credential is seeded into every
# fixture profile and asserted absent from every artifact the script produces.
#
# Coverage:
#   1. install into an absent / empty / existing hooks.json
#   2. unrelated user hooks (other events AND other PreToolUse handlers) are
#      preserved byte-for-byte
#   3. repeated install is idempotent (exactly one Loom entry, never two)
#   4. upgrade replaces only Loom's entry
#   5. atomic write + restrictive mode (0600); a malformed operator file is
#      never overwritten
#   6. verify: not-installed / untrusted / stale / wrong-version / missing
#      bridge / ready — each with the right exit code and JSON verdict
#   7. per-profile isolation: trusting profile A does not make profile B ready
#   8. remove deletes ONLY Loom's entry and leaves credentials + user config
#   9. credential hygiene: auth.json is never read, copied, or echoed
#   10. trust-baseline diff (issue #5005): a NEW trusted_hash appearing after
#       install reports trustSignal=baseline-diff; an idempotent reinstall of
#       unchanged content never resets an already-established trust; a
#       content-changing reinstall DOES require a fresh trust decision; a
#       legacy receipt (no baseline field) falls back to trustSignal=
#       legacy-coarse rather than losing readiness on a Loom upgrade, and the
#       next install migrates it by grandfathering existing trust
#
# Usage: ./defaults/scripts/tests/test-provision-codex-hooks.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
PROVISION="$REPO_ROOT/defaults/scripts/provision-codex-hooks.sh"
BRIDGE="$REPO_ROOT/defaults/hooks/guard-codex-bridge.sh"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'
PASS=0
FAIL=0
TOTAL=0
pass() { PASS=$((PASS + 1)); TOTAL=$((TOTAL + 1)); printf "${GREEN}PASS${NC} %s\n" "$1"; }
fail() { FAIL=$((FAIL + 1)); TOTAL=$((TOTAL + 1)); printf "${RED}FAIL${NC} %s\n" "$1"; }

command -v jq >/dev/null 2>&1 || { echo "jq is required for this suite"; exit 1; }

TMPROOT="$(mktemp -d)"
trap 'rm -rf "$TMPROOT"' EXIT
WORKSPACE="$TMPROOT/workspace"
mkdir -p "$WORKSPACE"

FAKE_TOKEN="sk-loom-FAKE-CODEX-CREDENTIAL-4495"

new_profile() {
    local name="$1"
    local dir="$TMPROOT/profiles/$name"
    mkdir -p "$dir"
    chmod 700 "$dir"
    printf '{"OPENAI_API_KEY":"%s","tokens":{"refresh_token":"%s"}}\n' "$FAKE_TOKEN" "$FAKE_TOKEN" > "$dir/auth.json"
    chmod 600 "$dir/auth.json"
    printf '%s' "$dir"
}

trust_profile() {
    # Simulate the operator having accepted Codex's hook-trust prompt once.
    printf 'hooks.state."loom-managed".trusted_hash = "deadbeefcafe"\n' > "$1/config.toml"
}

run_provision() {
    local out exit_code=0
    out="$(bash "$PROVISION" "$@" 2>/dev/null)" || exit_code=$?
    printf '%s|%s' "$exit_code" "$out"
}

loom_entries() {
    jq '[ (.hooks?.PreToolUse? // []) | .[]? | (.hooks? // []) | .[]?
          | select((.command? // "") | contains("guard-codex-bridge.sh")) ] | length' \
        "$1/hooks.json" 2>/dev/null
}

echo "=== install: absent hooks.json ==="
P1="$(new_profile alice)"
r="$(run_provision install --codex-home "$P1" --workspace "$WORKSPACE" --bridge "$BRIDGE")"
[[ "${r%%|*}" == "0" ]] && pass "install into a profile with no hooks.json exits 0" || fail "install into a profile with no hooks.json exits 0 (got ${r%%|*})"
[[ -f "$P1/hooks.json" ]] && pass "hooks.json created" || fail "hooks.json created"
[[ "$(loom_entries "$P1")" == "1" ]] && pass "exactly one Loom entry" || fail "exactly one Loom entry (got $(loom_entries "$P1"))"
mode="$(ls -l "$P1/hooks.json" | cut -c1-10)"
[[ "$mode" == "-rw-------" ]] && pass "hooks.json mode is 0600 (got $mode)" || fail "hooks.json mode is 0600 (got $mode)"
jq -e '.hooks.PreToolUse[0].hooks[0].type == "command"' "$P1/hooks.json" >/dev/null 2>&1 \
    && pass "handler type is 'command'" || fail "handler type is 'command'"
jq -re '.hooks.PreToolUse[0].hooks[0].command' "$P1/hooks.json" 2>/dev/null | grep -q -- '--loom-hook-version 1' \
    && pass "entry carries the Loom ownership/version marker" || fail "entry carries the Loom ownership/version marker"
jq -re '.hooks.PreToolUse[0].hooks[0].command' "$P1/hooks.json" 2>/dev/null | grep -q -- "--project-root $WORKSPACE" \
    && pass "entry pins the provisioned workspace" || fail "entry pins the provisioned workspace"
[[ -f "$P1/loom-codex-hooks.json" ]] && pass "receipt written" || fail "receipt written"

echo
echo "=== install: preserves an operator's existing hooks ==="
P2="$(new_profile bob)"
cat > "$P2/hooks.json" <<'EOF'
{
  "hooks": {
    "PreToolUse": [
      { "matcher": "shell", "hooks": [ { "type": "command", "command": "/opt/operator/audit.sh", "timeout": 5 } ] }
    ],
    "SessionStart": [
      { "hooks": [ { "type": "command", "command": "/opt/operator/greet.sh" } ] }
    ]
  },
  "unknownTopLevelKey": { "keep": "me" }
}
EOF
r="$(run_provision install --codex-home "$P2" --workspace "$WORKSPACE" --bridge "$BRIDGE")"
[[ "${r%%|*}" == "0" ]] && pass "install alongside operator hooks exits 0" || fail "install alongside operator hooks exits 0"
jq -e '[.hooks.PreToolUse[] | .hooks[] | .command] | index("/opt/operator/audit.sh") != null' "$P2/hooks.json" >/dev/null 2>&1 \
    && pass "operator's PreToolUse handler preserved" || fail "operator's PreToolUse handler preserved"
jq -e '.hooks.SessionStart[0].hooks[0].command == "/opt/operator/greet.sh"' "$P2/hooks.json" >/dev/null 2>&1 \
    && pass "operator's SessionStart event preserved" || fail "operator's SessionStart event preserved"
jq -e '.unknownTopLevelKey.keep == "me"' "$P2/hooks.json" >/dev/null 2>&1 \
    && pass "unknown top-level keys preserved" || fail "unknown top-level keys preserved"
[[ "$(loom_entries "$P2")" == "1" ]] && pass "exactly one Loom entry alongside operator hooks" || fail "exactly one Loom entry alongside operator hooks"

echo
echo "=== install: idempotent + upgrade ==="
run_provision install --codex-home "$P2" --workspace "$WORKSPACE" --bridge "$BRIDGE" >/dev/null
run_provision install --codex-home "$P2" --workspace "$WORKSPACE" --bridge "$BRIDGE" >/dev/null
[[ "$(loom_entries "$P2")" == "1" ]] && pass "repeated install never duplicates the Loom entry" || fail "repeated install never duplicates the Loom entry (got $(loom_entries "$P2"))"
jq -e '[.hooks.PreToolUse[] | .hooks[] | .command] | index("/opt/operator/audit.sh") != null' "$P2/hooks.json" >/dev/null 2>&1 \
    && pass "operator handler survives repeated installs" || fail "operator handler survives repeated installs"
run_provision install --codex-home "$P2" --workspace "$WORKSPACE" --bridge "$BRIDGE" --timeout 90 >/dev/null
jq -e '[.hooks.PreToolUse[] | .hooks[] | select(.command | contains("guard-codex-bridge.sh")) | .timeout] == [90]' "$P2/hooks.json" >/dev/null 2>&1 \
    && pass "upgrade updates only Loom's entry (timeout changed)" || fail "upgrade updates only Loom's entry (timeout changed)"

echo
echo "=== install: refuses to clobber a malformed operator file ==="
P3="$(new_profile carol)"
printf '{ this is not json\n' > "$P3/hooks.json"
before="$(cat "$P3/hooks.json")"
r="$(run_provision install --codex-home "$P3" --workspace "$WORKSPACE" --bridge "$BRIDGE")"
[[ "${r%%|*}" == "78" ]] && pass "malformed hooks.json -> exit 78" || fail "malformed hooks.json -> exit 78 (got ${r%%|*})"
[[ "$(cat "$P3/hooks.json")" == "$before" ]] && pass "malformed hooks.json left untouched" || fail "malformed hooks.json left untouched"

echo
echo "=== install: atomic-write failure leaves the prior file intact ==="
# The temp file is created INSIDE CODEX_HOME so the rename is same-filesystem.
# Make that directory unwritable and the mktemp must fail — the operator's
# existing hooks.json must survive byte-for-byte and the exit must be 78, never
# a half-written file. (Skipped when running as root, which ignores the mode.)
P3B="$(new_profile carla)"
run_provision install --codex-home "$P3B" --workspace "$WORKSPACE" --bridge "$BRIDGE" >/dev/null
before_atomic="$(cat "$P3B/hooks.json")"
if [[ "$(id -u)" == "0" ]]; then
    pass "atomic-write failure preserves the prior file (skipped: running as root)"
else
    chmod 500 "$P3B"
    r="$(run_provision install --codex-home "$P3B" --workspace "$WORKSPACE" --bridge "$BRIDGE" --timeout 99)"
    chmod 700 "$P3B"
    [[ "${r%%|*}" == "78" ]] && pass "unwritable profile dir -> exit 78" || fail "unwritable profile dir -> exit 78 (got ${r%%|*})"
    [[ "$(cat "$P3B/hooks.json")" == "$before_atomic" ]] \
        && pass "atomic-write failure leaves hooks.json byte-identical" \
        || fail "atomic-write failure leaves hooks.json byte-identical"
    [[ -z "$(find "$P3B" -name 'hooks.json.loom-tmp.*' 2>/dev/null)" ]] \
        && pass "no temp file is left behind by a failed atomic write" \
        || fail "no temp file is left behind by a failed atomic write"
fi

echo
echo "=== install: missing bridge / missing profile ==="
P4="$(new_profile dave)"
r="$(run_provision install --codex-home "$P4" --workspace "$WORKSPACE" --bridge "$TMPROOT/nope.sh")"
[[ "${r%%|*}" == "78" ]] && pass "unreadable bridge -> exit 78" || fail "unreadable bridge -> exit 78 (got ${r%%|*})"
r="$(run_provision install --codex-home "$TMPROOT/profiles/does-not-exist" --workspace "$WORKSPACE" --bridge "$BRIDGE")"
[[ "${r%%|*}" == "78" ]] && pass "missing CODEX_HOME -> exit 78" || fail "missing CODEX_HOME -> exit 78 (got ${r%%|*})"

echo
echo "=== verify ==="
verify_json() {
    bash "$PROVISION" verify --codex-home "$1" --workspace "$WORKSPACE" --bridge "$BRIDGE" --json 2>/dev/null
}
verify_code() {
    bash "$PROVISION" verify --codex-home "$1" --workspace "$WORKSPACE" --bridge "$BRIDGE" >/dev/null 2>&1
    printf '%s' "$?"
}

# (a) installed but NOT trusted -> not ready
[[ "$(verify_code "$P1")" == "78" ]] && pass "installed + untrusted -> exit 78" || fail "installed + untrusted -> exit 78"
v="$(verify_json "$P1")"
jq -e '.installed == true and .trusted == false and .ready == false' <<<"$v" >/dev/null 2>&1 \
    && pass "verify JSON reports installed=true trusted=false ready=false" || fail "verify JSON reports installed=true trusted=false ready=false (got $v)"

# (b) trusted -> ready
trust_profile "$P1"
[[ "$(verify_code "$P1")" == "0" ]] && pass "installed + trusted -> exit 0 (ready)" || fail "installed + trusted -> exit 0 (ready)"
jq -e '.ready == true and .stale == false' <<<"$(verify_json "$P1")" >/dev/null 2>&1 \
    && pass "verify JSON reports ready=true stale=false" || fail "verify JSON reports ready=true stale=false"

# (c) staleness: hand-edit the installed entry so it no longer matches the receipt
cp "$P1/hooks.json" "$P1/hooks.json.bak"
jq '(.hooks.PreToolUse[].hooks[] | select(.command | contains("guard-codex-bridge.sh")) | .command) |= (. + " --tampered")' \
    "$P1/hooks.json" > "$P1/hooks.json.tmp" && mv "$P1/hooks.json.tmp" "$P1/hooks.json"
[[ "$(verify_code "$P1")" == "78" ]] && pass "tampered entry -> stale -> exit 78" || fail "tampered entry -> stale -> exit 78"
jq -e '.stale == true and .ready == false' <<<"$(verify_json "$P1")" >/dev/null 2>&1 \
    && pass "verify JSON reports stale=true" || fail "verify JSON reports stale=true"
mv "$P1/hooks.json.bak" "$P1/hooks.json"

# (d) version drift: an entry from a different managed-hook version is stale
cp "$P1/hooks.json" "$P1/hooks.json.bak"
jq '(.hooks.PreToolUse[].hooks[] | select(.command | contains("guard-codex-bridge.sh")) | .command) |= (sub("--loom-hook-version 1"; "--loom-hook-version 0"))' \
    "$P1/hooks.json" > "$P1/hooks.json.tmp" && mv "$P1/hooks.json.tmp" "$P1/hooks.json"
[[ "$(verify_code "$P1")" == "78" ]] && pass "wrong managed-hook version -> exit 78" || fail "wrong managed-hook version -> exit 78"
mv "$P1/hooks.json.bak" "$P1/hooks.json"

# (e) receipt missing -> unpinned -> not ready
mv "$P1/loom-codex-hooks.json" "$P1/receipt.bak"
[[ "$(verify_code "$P1")" == "78" ]] && pass "missing receipt -> unpinned -> exit 78" || fail "missing receipt -> unpinned -> exit 78"
mv "$P1/receipt.bak" "$P1/loom-codex-hooks.json"
[[ "$(verify_code "$P1")" == "0" ]] && pass "restored receipt -> ready again" || fail "restored receipt -> ready again"

# (f) unreadable bridge -> not ready even when everything else is fine
code=0
bash "$PROVISION" verify --codex-home "$P1" --workspace "$WORKSPACE" --bridge "$TMPROOT/nope.sh" >/dev/null 2>&1 || code=$?
[[ "$code" == "78" ]] && pass "missing bridge file -> exit 78" || fail "missing bridge file -> exit 78 (got $code)"

# (g) not installed at all
P5="$(new_profile erin)"
trust_profile "$P5"
[[ "$(verify_code "$P5")" == "78" ]] && pass "not installed -> exit 78 even when the profile is trusted" || fail "not installed -> exit 78 even when the profile is trusted"

echo
echo "=== trust baseline: a NEW trust decision vs. a stale existing one (issue #5005) ==="
P8="$(new_profile grace2)"
run_provision install --codex-home "$P8" --workspace "$WORKSPACE" --bridge "$BRIDGE" >/dev/null
trust_profile "$P8"
[[ "$(verify_code "$P8")" == "0" ]] && pass "trust-baseline: fresh install + trust -> ready" || fail "trust-baseline: fresh install + trust -> ready"
jq -e '.trustSignal == "baseline-diff"' <<<"$(verify_json "$P8")" >/dev/null 2>&1 \
    && pass "trust-baseline: verify JSON reports trustSignal=baseline-diff for a freshly trusted profile" \
    || fail "trust-baseline: verify JSON reports trustSignal=baseline-diff (got $(verify_json "$P8"))"

# An idempotent reinstall of UNCHANGED content must never erase the operator's
# earlier trust decision by resetting the baseline to "whatever is trusted
# right now" (which would already include that same decision, silently
# un-counting it).
run_provision install --codex-home "$P8" --workspace "$WORKSPACE" --bridge "$BRIDGE" >/dev/null
[[ "$(verify_code "$P8")" == "0" ]] && pass "trust-baseline: idempotent reinstall of unchanged content stays ready" || fail "trust-baseline: idempotent reinstall of unchanged content stays ready"

# A genuine content change (here: a different --workspace, which changes the
# managed command's project-root and therefore the pinned entry) resets the
# baseline: the OLD trusted_hash alone no longer satisfies readiness — a FRESH
# trust decision is required for the new content.
WORKSPACE2="$TMPROOT/workspace2"
mkdir -p "$WORKSPACE2"
run_provision install --codex-home "$P8" --workspace "$WORKSPACE2" --bridge "$BRIDGE" >/dev/null
[[ "$(verify_code "$P8")" == "78" ]] \
    && pass "trust-baseline: a content-changing reinstall requires a fresh trust decision" \
    || fail "trust-baseline: a content-changing reinstall requires a fresh trust decision"
jq -e '.trustSignal == "baseline-diff-no-new-trust"' <<<"$(verify_json "$P8")" >/dev/null 2>&1 \
    && pass "trust-baseline: verify JSON reports trustSignal=baseline-diff-no-new-trust after a content change" \
    || fail "trust-baseline: verify JSON reports trustSignal=baseline-diff-no-new-trust (got $(verify_json "$P8"))"
# Re-trusting (a NEW hooks.state entry, simulating the operator accepting the
# prompt again for the changed content) restores readiness.
printf 'hooks.state."loom-managed".trusted_hash = "deadbeefcafe"\nhooks.state."loom-managed-2".trusted_hash = "freshtrust2"\n' > "$P8/config.toml"
[[ "$(verify_code "$P8")" == "0" ]] \
    && pass "trust-baseline: a fresh trust decision after a content change restores readiness" \
    || fail "trust-baseline: a fresh trust decision after a content change restores readiness"

echo
echo "=== trust baseline: legacy-receipt migration (pre-#5005 profiles are not punished) ==="
P10="$(new_profile henry)"
run_provision install --codex-home "$P10" --workspace "$WORKSPACE" --bridge "$BRIDGE" >/dev/null
trust_profile "$P10"
# Simulate a receipt written by a Loom version that predates trust-baseline
# tracking: strip the field a real pre-#5005 install would never have written.
jq 'del(.loomManagedHook.trustBaselineHashes)' "$P10/loom-codex-hooks.json" > "$P10/loom-codex-hooks.json.tmp" \
    && mv "$P10/loom-codex-hooks.json.tmp" "$P10/loom-codex-hooks.json"
[[ "$(verify_code "$P10")" == "0" ]] \
    && pass "trust-baseline: a legacy receipt (no baseline field) does not lose readiness" \
    || fail "trust-baseline: a legacy receipt (no baseline field) does not lose readiness"
jq -e '.trustSignal == "legacy-coarse"' <<<"$(verify_json "$P10")" >/dev/null 2>&1 \
    && pass "trust-baseline: verify JSON reports trustSignal=legacy-coarse for a pre-#5005 receipt" \
    || fail "trust-baseline: verify JSON reports trustSignal=legacy-coarse (got $(verify_json "$P10"))"

# The NEXT install migrates the receipt (grandfathering existing trust rather
# than resetting the baseline to "current", which would erase it) — readiness
# must survive the migration.
run_provision install --codex-home "$P10" --workspace "$WORKSPACE" --bridge "$BRIDGE" >/dev/null
[[ "$(verify_code "$P10")" == "0" ]] \
    && pass "trust-baseline: migrating a legacy receipt on reinstall preserves readiness" \
    || fail "trust-baseline: migrating a legacy receipt on reinstall preserves readiness"
jq -e '.loomManagedHook.trustBaselineHashes == []' "$P10/loom-codex-hooks.json" >/dev/null 2>&1 \
    && pass "trust-baseline: migration grandfathers existing trust with an empty baseline" \
    || fail "trust-baseline: migration grandfathers existing trust with an empty baseline (got $(cat "$P10/loom-codex-hooks.json"))"

echo
echo "=== per-profile isolation ==="
# P1 is trusted and ready; a brand-new profile must NOT inherit that verdict.
P6="$(new_profile frank)"
run_provision install --codex-home "$P6" --workspace "$WORKSPACE" --bridge "$BRIDGE" >/dev/null
[[ "$(verify_code "$P6")" == "78" ]] && pass "a second profile does not inherit profile A's trust" || fail "a second profile does not inherit profile A's trust"
jq -e --arg p "$(basename "$P6")" '.profile == $p' <<<"$(verify_json "$P6")" >/dev/null 2>&1 \
    && pass "verify reports the profile DIRECTORY NAME only" || fail "verify reports the profile DIRECTORY NAME only"

echo
echo "=== --all-profiles fan-out over the pooled profile root ==="
POOL="$TMPROOT/pool"
mkdir -p "$POOL/one" "$POOL/two with spaces"
r="$(run_provision install --all-profiles --profile-root "$POOL" --workspace "$WORKSPACE" --bridge "$BRIDGE")"
[[ "${r%%|*}" == "0" ]] && pass "--all-profiles install exits 0" || fail "--all-profiles install exits 0 (got ${r%%|*})"
[[ "$(loom_entries "$POOL/one")" == "1" ]] && pass "--all-profiles installed into profile 'one'" || fail "--all-profiles installed into profile 'one'"
[[ "$(loom_entries "$POOL/two with spaces")" == "1" ]] \
    && pass "--all-profiles installed into a profile name containing spaces" \
    || fail "--all-profiles installed into a profile name containing spaces"

# verify: the WORST per-profile result wins, so one untrusted profile fails the
# whole fan-out — the property that makes this usable as a pool-wide gate.
all_verify() {
    bash "$PROVISION" verify --all-profiles --profile-root "$POOL" \
        --workspace "$WORKSPACE" --bridge "$BRIDGE" --json 2>/dev/null
    printf '\037%s' "$?"
}
out="$(all_verify)"
[[ "${out##*$'\037'}" == "78" ]] && pass "--all-profiles verify is 78 while any profile is untrusted" \
    || fail "--all-profiles verify is 78 while any profile is untrusted (got ${out##*$'\037'})"
trust_profile "$POOL/one"
out="$(all_verify)"
[[ "${out##*$'\037'}" == "78" ]] && pass "--all-profiles verify still 78 with only ONE profile trusted" \
    || fail "--all-profiles verify still 78 with only ONE profile trusted (got ${out##*$'\037'})"
trust_profile "$POOL/two with spaces"
out="$(all_verify)"
[[ "${out##*$'\037'}" == "0" ]] && pass "--all-profiles verify is 0 once every profile is ready" \
    || fail "--all-profiles verify is 0 once every profile is ready (got ${out##*$'\037'})"
[[ "$(printf '%s' "${out%$'\037'*}" | grep -c '"ready":true')" == "2" ]] \
    && pass "--all-profiles --json emits one JSONL record per profile" \
    || fail "--all-profiles --json emits one JSONL record per profile"
if printf '%s' "$out" | grep -q "$FAKE_TOKEN"; then
    fail "--all-profiles output leaked a credential"
else
    pass "--all-profiles output leaks no credential material"
fi
r="$(run_provision verify --all-profiles --profile-root "$TMPROOT/no-such-root")"
[[ "${r%%|*}" == "78" ]] && pass "--all-profiles with a missing root -> exit 78" || fail "--all-profiles with a missing root -> exit 78 (got ${r%%|*})"
mkdir -p "$TMPROOT/empty-pool"
r="$(run_provision verify --all-profiles --profile-root "$TMPROOT/empty-pool")"
[[ "${r%%|*}" == "78" ]] && pass "--all-profiles with an empty root -> exit 78" || fail "--all-profiles with an empty root -> exit 78 (got ${r%%|*})"

echo
echo "=== remove ==="
r="$(run_provision remove --codex-home "$P2")"
[[ "${r%%|*}" == "0" ]] && pass "remove exits 0" || fail "remove exits 0"
[[ "$(loom_entries "$P2")" == "0" ]] && pass "Loom entry removed" || fail "Loom entry removed"
jq -e '[.hooks.PreToolUse[] | .hooks[] | .command] | index("/opt/operator/audit.sh") != null' "$P2/hooks.json" >/dev/null 2>&1 \
    && pass "operator's PreToolUse handler survives removal" || fail "operator's PreToolUse handler survives removal"
jq -e '.hooks.SessionStart[0].hooks[0].command == "/opt/operator/greet.sh"' "$P2/hooks.json" >/dev/null 2>&1 \
    && pass "operator's SessionStart event survives removal" || fail "operator's SessionStart event survives removal"
jq -e '.unknownTopLevelKey.keep == "me"' "$P2/hooks.json" >/dev/null 2>&1 \
    && pass "unknown top-level keys survive removal" || fail "unknown top-level keys survive removal"
[[ -f "$P2/auth.json" ]] && pass "auth.json survives removal" || fail "auth.json survives removal"
[[ ! -f "$P2/loom-codex-hooks.json" ]] && pass "receipt removed" || fail "receipt removed"

# Removing Loom's entry when it was the ONLY PreToolUse entry cleans up the
# now-empty structures rather than leaving `{"hooks":{"PreToolUse":[]}}` behind.
P7="$(new_profile grace)"
run_provision install --codex-home "$P7" --workspace "$WORKSPACE" --bridge "$BRIDGE" >/dev/null
run_provision remove --codex-home "$P7" >/dev/null
jq -e 'has("hooks") | not' "$P7/hooks.json" >/dev/null 2>&1 \
    && pass "removal cleans up empty hooks structures" || fail "removal cleans up empty hooks structures ($(cat "$P7/hooks.json"))"
r="$(run_provision remove --codex-home "$(new_profile heidi)")"
[[ "${r%%|*}" == "0" ]] && pass "remove on a profile with no hooks.json is a no-op success" || fail "remove on a profile with no hooks.json is a no-op success"

echo
echo "=== credential hygiene ==="
leak=0
for f in "$P1/hooks.json" "$P1/loom-codex-hooks.json" "$P2/hooks.json"; do
    [[ -f "$f" ]] || continue
    if grep -q "$FAKE_TOKEN" "$f" 2>/dev/null; then
        leak=1
        fail "credential leaked into $(basename "$f")"
    fi
done
[[ "$leak" == "0" ]] && pass "no credential material in hooks.json or the receipt"
all_out="$(bash "$PROVISION" install --codex-home "$P1" --workspace "$WORKSPACE" --bridge "$BRIDGE" 2>&1; \
           bash "$PROVISION" verify --codex-home "$P1" --workspace "$WORKSPACE" --bridge "$BRIDGE" --json 2>&1)"
if grep -q "$FAKE_TOKEN" <<<"$all_out"; then
    fail "credential leaked into install/verify output"
else
    pass "no credential material in install/verify output"
fi
if grep -nE '(^|[^-A-Za-z])--dangerously-bypass-hook-trust' "$REPO_ROOT/defaults/scripts/spawn-codex.sh" \
    | grep -vqE '#|log_(error|warn|info)'; then
    fail "spawn-codex.sh passes --dangerously-bypass-hook-trust"
else
    pass "spawn-codex.sh never passes --dangerously-bypass-hook-trust"
fi
# The flag has a config-key equivalent (`-c bypass_hook_trust=true`) in the
# 0.146.0 binary, so asserting only on the flag spelling would leave the same
# waiver reachable through the other door.
if grep -nE 'bypass_hook_trust' "$REPO_ROOT/defaults/scripts/spawn-codex.sh" "$PROVISION" \
    | grep -vqE '#|log_(error|warn|info)'; then
    fail "a Loom script sets the bypass_hook_trust config key"
else
    pass "no Loom script sets the bypass_hook_trust config key"
fi

echo
echo "=== $PASS/$TOTAL passed ==="
[[ "$FAIL" -eq 0 ]]
