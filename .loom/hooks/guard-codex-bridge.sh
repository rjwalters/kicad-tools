#!/usr/bin/env bash
# guard-codex-bridge.sh — Codex `pre_tool_use` → Loom guard bridge
# (issue #4495, epic #4489 Phase 6).
#
# ============================================================================
# WHAT THIS IS
# ============================================================================
#
# Loom's guards (`guard-destructive.sh`, `guard-worktree-paths.sh`,
# `guard-loom-workflow.sh`) are Claude Code `PreToolUse` hooks. Codex 0.146.0
# has its own hooks engine (`$CODEX_HOME/hooks.json`) with a `pre_tool_use`
# event whose payload is *similar to* but not identical to Claude's, and whose
# RESPONSE schema is materially narrower (see "Response encoding" below).
#
# This file is the adapter/normalization layer at the guard boundary:
#
#     Codex pre_tool_use JSON
#            │
#            ▼
#     [1] validate + classify tool          (this file)
#            │
#            ▼
#     [2] normalize → internal guard request (this file)
#            │  Claude-shaped: {tool_name, tool_input, cwd}
#            ▼
#     [3] SHARED policy — the existing guards, unmodified
#            guard-loom-workflow.sh   (workflow policy)
#            guard-destructive.sh     (catastrophic/destructive + Bash writes)
#            guard-worktree-paths.sh  (managed-worktree confinement)
#            │
#            ▼
#     [4] encode the outcome in Codex's pre_tool_use response schema
#
# There is NO second regex/policy table here. Step 3 executes the same scripts
# the Claude path executes, with the same inputs they already understand, so a
# policy change lands for both runtimes at once.
#
# ============================================================================
# RUNTIME DISCRIMINATION (explicit wrapper mode, never payload sniffing)
# ============================================================================
#
# The Codex and Claude `PreToolUse` payloads share field names
# (`hook_event_name`, `tool_name`, `tool_input`, `cwd`, `session_id`), so
# sniffing the payload to guess the runtime would be ambiguous and spoofable.
# Instead, *this file being the entry point IS the discriminator*: it is
# installed only as a Codex `hooks.json` `pre_tool_use` command by
# `provision-codex-hooks.sh`, and it accepts only
# `hook_event_name == "PreToolUse"`. Claude's settings.json never points here.
#
# ============================================================================
# TESTED CODEX SCHEMA (PINNED)
# ============================================================================
#
#   codex-cli 0.146.0 — verified 2026-07-31 against the schemas embedded in the
#   shipped binary (`pre-tool-use.command.input` / `pre-tool-use.command.output`).
#
#   INPUT (`pre-tool-use.command.input`), all required:
#     agent_id? agent_type? cwd hook_event_name("PreToolUse") model
#     permission_mode session_id tool_input tool_name tool_use_id
#     transcript_path turn_id
#
#   OUTPUT (`pre-tool-use.command.output`):
#     { "continue": bool, "decision": "approve"|"block", "reason": str,
#       "stopReason": str, "suppressOutput": bool, "systemMessage": str,
#       "hookSpecificOutput": {
#         "hookEventName": "PreToolUse",       (required)
#         "permissionDecision": "allow"|"deny"|"ask",
#         "permissionDecisionReason": str,
#         "additionalContext": str, "updatedInput": any } }
#
#   ...BUT the 0.146.0 engine rejects most of that surface. The binary carries
#   these exact refusals:
#
#     "PreToolUse hook returned unsupported permissionDecision:allow"
#     "PreToolUse hook returned unsupported permissionDecision:ask"
#     "PreToolUse hook returned unsupported decision:approve"
#     "PreToolUse hook returned unsupported continue:false"
#     "PreToolUse hook returned unsupported stopReason"
#     "PreToolUse hook returned unsupported suppressOutput"
#     "PreToolUse hook returned permissionDecision:deny without a non-empty
#      permissionDecisionReason"
#
#   Therefore, on 0.146.0 the ONLY two things a `pre_tool_use` hook may say are:
#
#     allow → emit NOTHING and exit 0
#     deny  → { "hookSpecificOutput": { "hookEventName": "PreToolUse",
#               "permissionDecision": "deny",
#               "permissionDecisionReason": "<non-empty>" } }
#
#   This is why the "headless ask → deny" rule in #4495 is not merely a policy
#   preference: `ask` is not expressible on this wire at all, and `codex exec`
#   is non-interactive, so there is no operator who could answer it. Every
#   Claude `ask` outcome is translated to `deny` with the original reason
#   preserved and prefixed so the model can see why.
#
# ============================================================================
# FAIL-CLOSED CONTRACT (the inverse of the Claude guards)
# ============================================================================
#
# The Claude guards fail OPEN: a broken guard must not wedge every tool call.
# That is the right default there because Claude Code's own permission system
# is still in play and a human eventually sees the session.
#
# This bridge fails CLOSED for anything it cannot prove safe:
#
#   - malformed / unparseable JSON                       → deny
#   - wrong or missing `hook_event_name`                  → deny
#   - a MUTATING tool whose payload yields no command/path→ deny
#   - an UNKNOWN tool name (not provably non-mutating)    → deny
#   - a sub-guard that exits non-zero or emits garbage    → deny
#
# It exits 0 in every case (a non-zero exit is a *hook failure* on Codex's wire,
# not a decision). The one thing it never does is emit "allow" for an input it
# did not fully understand.
#
# ============================================================================
# TOOL CLASSIFICATION
# ============================================================================
#
# See classify_tool() below. The three buckets are:
#
#   shell    — command execution. Normalized to a Claude `Bash` request and run
#              through guard-loom-workflow.sh then guard-destructive.sh (whose
#              generic guard also carries the #4178 Bash write-confinement).
#   patch    — native file mutation (apply_patch and friends). Every target
#              path is extracted and run through guard-worktree-paths.sh as a
#              Claude `Write` request.
#   readonly — provably non-mutating. Allowed.
#   mcp      — `mcp__*` / `mcp.*` tool calls. PASSED THROUGH, because Claude's
#              own guards do not match MCP tools either (their matchers are
#              `Bash` and `Edit|Write`). Denying them here would be *stricter*
#              than the Claude path, not parity, and would break the `mcp: yes`
#              capability. Recorded as a residual gap in
#              defaults/docs/guardrail-parity-codex.md.
#   unknown  — everything else. Denied (fail closed).
#
# ============================================================================
# USAGE
# ============================================================================
#
#   guard-codex-bridge.sh [--project-root <abs-path>] [--loom-hook-version N]
#
#   --project-root      the workspace this managed hook was provisioned for.
#                       Exported as LOOM_PROJECT_ROOT so guard-destructive.sh
#                       resolves the consuming repo the same way the
#                       machine-level Claude wiring does (#4262).
#   --loom-hook-version an ownership/version marker consumed by
#                       provision-codex-hooks.sh; ignored at runtime.
#
# Env:
#   LOOM_CODEX_BRIDGE_GUARD_DIR   override the directory the sibling guards are
#                                 read from (test seam).
#   LOOM_CODEX_BRIDGE_TRACE_FILE  append one JSONL record per decision (test
#                                 seam / operator debugging). Never contains
#                                 credentials — the same redaction the guards'
#                                 own decision log uses is NOT applied here, so
#                                 this is off unless explicitly pointed at a
#                                 file.

# Deliberately NOT `set -e`: a non-zero from any probe must fall into the
# fail-closed path we choose, never abort the script with no output.
set -uo pipefail

LOOM_HOOK_VERSION_DEFAULT=1
CODEX_SCHEMA_PIN="0.146.0"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd 2>/dev/null || echo ".")"
GUARD_DIR="${LOOM_CODEX_BRIDGE_GUARD_DIR:-$SCRIPT_DIR}"

PROJECT_ROOT=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --project-root)
            PROJECT_ROOT="${2:-}"
            shift 2 || shift
            ;;
        --project-root=*)
            PROJECT_ROOT="${1#--project-root=}"
            shift
            ;;
        --loom-hook-version|--loom-hook-version=*)
            # Ownership marker only; consumed by the provisioner's parser.
            [[ "$1" == "--loom-hook-version" ]] && shift
            shift
            ;;
        *)
            shift
            ;;
    esac
done

if [[ -n "$PROJECT_ROOT" ]]; then
    export LOOM_PROJECT_ROOT="$PROJECT_ROOT"
fi

# Shared symlink-aware canonicalization (#4495). Best-effort source: if the lib
# is missing the bridge still runs, but path decisions degrade to lexical
# normalization inside guard-worktree-paths.sh itself.
if [[ -f "$GUARD_DIR/../scripts/lib/canonical-path.sh" ]]; then
    # shellcheck source=../scripts/lib/canonical-path.sh
    source "$GUARD_DIR/../scripts/lib/canonical-path.sh" 2>/dev/null || true
fi

# ---------------------------------------------------------------------------
# Response encoding (Codex 0.146.0 wire — see the header)
# ---------------------------------------------------------------------------

trace() {
    [[ -n "${LOOM_CODEX_BRIDGE_TRACE_FILE:-}" ]] || return 0
    { printf '%s\n' "$1" >> "$LOOM_CODEX_BRIDGE_TRACE_FILE"; } 2>/dev/null || true
    return 0
}

# Set the instant a decision is committed to stdout, so the crash/termination
# safety net below never double-emits after a normal decision.
DECIDED=0

# allow: emit nothing. `permissionDecision:"allow"` is REJECTED by the 0.146.0
# engine, so silence is the only expressible allow.
emit_allow() {
    DECIDED=1
    trace "{\"decision\":\"allow\",\"tool\":\"${TOOL_NAME:-}\"}"
    exit 0
}

# deny: the only expressible blocking decision. A non-empty reason is REQUIRED
# by the engine; guard against ever emitting an empty one.
emit_deny() {
    local reason="$1"
    DECIDED=1
    [[ -n "$reason" ]] || reason="Blocked by Loom guard policy (no reason supplied)."
    trace "{\"decision\":\"deny\",\"tool\":\"${TOOL_NAME:-}\"}"
    if command -v jq >/dev/null 2>&1 && jq -n --arg reason "$reason" '{
        hookSpecificOutput: {
            hookEventName: "PreToolUse",
            permissionDecision: "deny",
            permissionDecisionReason: $reason
        }
    }' 2>/dev/null; then
        exit 0
    fi
    # jq unavailable or failed — hand-escape. Keeping this path alive matters:
    # the fail-closed branches below must be able to speak even when jq is the
    # thing that is missing.
    local escaped
    escaped=$(printf '%s' "$reason" \
        | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' -e 's/\t/\\t/g' \
        | awk 'NR>1{printf "\\n"} {printf "%s", $0}')
    printf '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"%s"}}\n' "$escaped"
    exit 0
}

FAIL_CLOSED_PREFIX="BLOCKED (Loom Codex guard bridge, fail-closed)"

fail_closed() {
    emit_deny "$FAIL_CLOSED_PREFIX: $1 Loom cannot prove this call is safe, so it is denied rather than allowed. (issue #4495; Codex hook schema pin ${CODEX_SCHEMA_PIN})"
}

# --- crash / termination safety net (#4495) --------------------------------
#
# Everything above is written to reach an explicit decision, but "the bridge
# itself dies" is its own fail-closed vector in #4495's acceptance criteria
# (hook timeout / crash / nonzero exit must not become an allow). On Codex's
# wire an allow IS silence, so a bridge that dies before printing anything is
# indistinguishable from an approval — precisely the failure mode this whole
# file exists to prevent.
#
# So: any exit path that has not already committed a decision emits a deny.
# `set -u` on an unbound variable, an unexpected `set -o pipefail` abort, an
# out-of-memory kill of a child, or a SIGTERM from Codex's hook timeout all
# land here. Written WITHOUT jq and without calling emit_deny (which would
# re-enter the trap), so it still speaks when the failure was jq itself.
#
# SIGKILL (Codex's escalation after a timeout grace period) is by definition
# untrappable; that residual is recorded in
# defaults/docs/guardrail-parity-codex.md and is a named reason the capability
# manifest stays `partial`.
bridge_safety_net() {
    local rc=$?
    [[ "$DECIDED" == "1" ]] && return 0
    DECIDED=1
    printf '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"%s"}}\n' \
        "$FAIL_CLOSED_PREFIX: the guard bridge terminated (status ${rc}) before reaching a decision, so the tool call is denied rather than silently allowed. On the Codex 0.146.0 wire an allow is expressed as NO output, which makes a crashed guard indistinguishable from an approval. Check the bridge and its sub-guards. (issue #4495)"
    # Force the exit-0 contract: a non-zero exit is a hook FAILURE on Codex's
    # wire (not a decision), and would discard the deny we just emitted.
    # `exit` inside an EXIT trap sets the final status without re-entering it.
    exit 0
}
trap bridge_safety_net EXIT
# A signal terminates the script; the EXIT trap then emits the deny. `exit 0`
# keeps the hook's exit-0 contract (a non-zero exit is a hook FAILURE on
# Codex's wire, not a decision).
trap 'exit 0' TERM INT HUP

# ---------------------------------------------------------------------------
# Read + validate the Codex event
# ---------------------------------------------------------------------------

TOOL_NAME=""
INPUT="$(cat 2>/dev/null)" || INPUT=""

if [[ -z "${INPUT//[[:space:]]/}" ]]; then
    fail_closed "the pre_tool_use payload was empty."
fi

if ! command -v jq >/dev/null 2>&1; then
    fail_closed "jq is not on PATH, so the pre_tool_use payload cannot be parsed."
fi

if ! printf '%s' "$INPUT" | jq -e . >/dev/null 2>&1; then
    fail_closed "the pre_tool_use payload is not valid JSON."
fi

EVENT_NAME="$(printf '%s' "$INPUT" | jq -r '.hook_event_name // empty' 2>/dev/null)" || EVENT_NAME=""
if [[ "$EVENT_NAME" != "PreToolUse" ]]; then
    fail_closed "unexpected hook_event_name '${EVENT_NAME:-<missing>}' on the pre_tool_use channel (expected 'PreToolUse')."
fi

TOOL_NAME="$(printf '%s' "$INPUT" | jq -r '.tool_name // empty' 2>/dev/null)" || TOOL_NAME=""
if [[ -z "$TOOL_NAME" ]]; then
    fail_closed "the pre_tool_use payload has no tool_name."
fi

EVENT_CWD="$(printf '%s' "$INPUT" | jq -r '.cwd // empty' 2>/dev/null)" || EVENT_CWD=""

# ---------------------------------------------------------------------------
# Tool classification
# ---------------------------------------------------------------------------
#
# Names verified against codex-cli 0.146.0's tool surface. `shell_command` is
# the current shell tool; `shell` / `local_shell` / `exec_command` /
# `unified_exec` are the other execution entry points the same binary can
# expose depending on feature flags and model family.
classify_tool() {
    local name="$1"
    case "$name" in
        # --- command execution ------------------------------------------------
        shell|shell_command|local_shell|exec_command|unified_exec|container.exec|Bash|bash)
            printf 'shell'
            ;;
        # --- native file mutation --------------------------------------------
        apply_patch|applyPatch|write_file|edit_file|create_file|str_replace_editor|Edit|Write|MultiEdit|NotebookEdit)
            printf 'patch'
            ;;
        # --- recognized mutation surface Loom cannot inspect ------------------
        # write_stdin feeds bytes to an ALREADY-RUNNING PTY session. The command
        # that owns the session was policed when it started, but the bytes
        # themselves are an un-analyzable second command channel (`sh` waiting on
        # stdin). Denied rather than waved through — see gap "write_stdin" in
        # defaults/docs/guardrail-parity-codex.md.
        write_stdin)
            printf 'opaque-mutating'
            ;;
        # --- provably non-mutating -------------------------------------------
        update_plan|view_image|read_file|read_mcp_resource|request_permissions|list_dir|glob|grep|web_search|Read|Glob|Grep|WebSearch|WebFetch|TodoWrite|Task)
            printf 'readonly'
            ;;
        # --- MCP passthrough (parity with Claude, see header) -----------------
        mcp__*|mcp.*)
            printf 'mcp'
            ;;
        *)
            printf 'unknown'
            ;;
    esac
}

TOOL_CLASS="$(classify_tool "$TOOL_NAME")"

case "$TOOL_CLASS" in
    readonly|mcp)
        emit_allow
        ;;
    opaque-mutating)
        emit_deny "$FAIL_CLOSED_PREFIX: tool '$TOOL_NAME' writes to a live process's stdin, which Loom's guards cannot inspect for destructive content or write targets. Run the command as a fresh shell call instead so it can be policed. (issue #4495)"
        ;;
    unknown)
        fail_closed "tool '$TOOL_NAME' is not in Loom's Codex parity table, so it cannot be classified as non-mutating."
        ;;
esac

# ---------------------------------------------------------------------------
# Sub-guard invocation — SHARED policy, Claude-shaped input
# ---------------------------------------------------------------------------
#
# run_guard <guard-file> <claude-shaped-json>
#
# Prints the guard's decision as "<decision>\t<reason>" where <decision> is one
# of allow|deny|ask|broken. NEVER interpolates any payload value into a shell
# command: the JSON is fed on stdin.
run_guard() {
    local guard="$1" payload="$2"
    if [[ ! -r "$GUARD_DIR/$guard" ]]; then
        # A missing guard is a provisioning failure, not an allow.
        printf 'broken\tguard %s is missing or unreadable at %s' "$guard" "$GUARD_DIR"
        return 0
    fi

    # Run the guard FROM the request's cwd. guard-worktree-paths.sh resolves the
    # main checkout with `git rev-parse --git-common-dir`, and
    # guard-loom-workflow.sh resolves REPO_ROOT from the payload's cwd — both
    # need to see the same repository the tool call is about, not whatever
    # directory the Codex process happens to have been started in.
    local out rc=0
    out="$(cd "${GUARD_CWD:-$PWD}" 2>/dev/null || true; printf '%s' "$payload" | bash "$GUARD_DIR/$guard" 2>/dev/null)" || rc=$?
    if [[ "$rc" -ne 0 ]]; then
        printf 'broken\tguard %s exited %s (the Loom guards are contractually exit-0)' "$guard" "$rc"
        return 0
    fi

    if [[ -z "${out//[[:space:]]/}" ]]; then
        printf 'allow\t'
        return 0
    fi

    local decision reason
    decision="$(printf '%s' "$out" | jq -r '.hookSpecificOutput.permissionDecision // empty' 2>/dev/null)" || decision=""
    reason="$(printf '%s' "$out" | jq -r '.hookSpecificOutput.permissionDecisionReason // empty' 2>/dev/null)" || reason=""

    case "$decision" in
        deny|ask)
            printf '%s\t%s' "$decision" "$reason"
            ;;
        allow|"")
            # Non-empty output that is not a deny/ask decision. If it did not
            # parse as JSON at all, treat it as a broken guard rather than an
            # allow.
            if printf '%s' "$out" | jq -e . >/dev/null 2>&1; then
                printf 'allow\t'
            else
                printf 'broken\tguard %s emitted non-JSON output' "$guard"
            fi
            ;;
        *)
            printf 'broken\tguard %s emitted unknown permissionDecision %s' "$guard" "$decision"
            ;;
    esac
}

# Apply a guard result, translating Codex-inexpressible outcomes.
#   deny   → deny verbatim
#   ask    → deny, reason preserved and labelled (headless: nobody to ask)
#   broken → deny (fail closed)
apply_result() {
    local result="$1" context="$2"
    local decision="${result%%$'\t'*}"
    local reason="${result#*$'\t'}"
    [[ "$reason" == "$result" ]] && reason=""

    case "$decision" in
        allow)
            return 0
            ;;
        deny)
            emit_deny "$reason"
            ;;
        ask)
            emit_deny "BLOCKED (Loom: 'ask' is not answerable in a headless Codex session, so it is denied): $reason"
            ;;
        broken|*)
            fail_closed "$context: ${reason:-the guard produced no usable decision}."
            ;;
    esac
}

# Build a Claude-shaped guard request without ever interpolating untrusted
# values into a command line.
claude_bash_request() {
    local command="$1" cwd="$2"
    jq -nc --arg command "$command" --arg cwd "$cwd" \
        '{tool_name: "Bash", tool_input: {command: $command}, cwd: $cwd}' 2>/dev/null
}

claude_write_request() {
    local path="$1" cwd="$2"
    jq -nc --arg path "$path" --arg cwd "$cwd" \
        '{tool_name: "Write", tool_input: {file_path: $path}, cwd: $cwd}' 2>/dev/null
}

# ---------------------------------------------------------------------------
# SHELL normalization
# ---------------------------------------------------------------------------
#
# Codex exposes several execution payload shapes. Extract a single command
# STRING for the guards' pattern matching:
#
#   shell / local_shell : {"command": ["bash","-lc","<cmd>"], "workdir": "..."}
#                         {"action": {"type":"exec","command":[...], ...}}
#   shell_command       : {"cmd": "<cmd>" | [...], "workdir": "..."}
#   exec_command        : {"cmd": "<cmd>", "shell": "...", "login": bool}
#   unified_exec        : {"input": ["<cmd>", ...]}
#
# When argv looks like `<shell> -lc <script>` (or -c/-ic), the SCRIPT is the
# command the guards must see; otherwise the argv is joined with spaces, which
# errs toward matching more patterns (deny-leaning), never fewer.
extract_shell_command() {
    printf '%s' "$INPUT" | jq -r '
        def argv_to_command:
            if type == "string" then .
            elif type == "array" then
                (map(tostring)) as $a
                | if ($a | length) >= 3
                     and ($a[0] | test("(^|/)(ba|z|k|da)?sh$"))
                     and ($a[1] | test("^-[a-z]*c$"))
                  then ($a[2:] | join(" "))
                  else ($a | join(" "))
                  end
            else empty end;
        .tool_input as $t
        | [ ($t.command? | select(. != null) | argv_to_command),
            ($t.cmd? | select(. != null) | argv_to_command),
            ($t.action?.command? | select(. != null) | argv_to_command),
            ($t.input? | select(. != null) | argv_to_command),
            ($t.script? | select(. != null) | argv_to_command)
          ] | map(select(. != null and . != "")) | .[0] // empty
    ' 2>/dev/null
}

extract_workdir() {
    printf '%s' "$INPUT" | jq -r '
        .tool_input as $t
        | [ ($t.workdir? // empty), ($t.cwd? // empty), ($t.action?.workdir? // empty) ]
          | map(select(type == "string" and . != "")) | .[0] // empty
    ' 2>/dev/null
}

# ---------------------------------------------------------------------------
# PATCH normalization
# ---------------------------------------------------------------------------
#
# apply_patch carries a `*** Begin Patch` envelope (0.146.0 grammar):
#     *** Add File: <path>
#     *** Update File: <path>       [ *** Move to: <path> ]
#     *** Delete File: <path>
# Both the freeform (`{"input": "..."}`) and JSON (`{"command":["apply_patch",
# "<patch>"]}`) call shapes are handled, plus the plain file_path/path shape the
# non-patch editors use.
extract_patch_text() {
    printf '%s' "$INPUT" | jq -r '
        .tool_input as $t
        | [ ($t.input? | select(type == "string")),
            ($t.patch? | select(type == "string")),
            ($t.command? | select(type == "array")
                         | map(select(type == "string"))
                         | map(select(test("\\*\\*\\* Begin Patch")))
                         | .[0]),
            ($t.command? | select(type == "string")
                         | select(test("\\*\\*\\* Begin Patch"))),
            ($t.input? | select(type == "array")
                       | map(select(type == "string"))
                       | map(select(test("\\*\\*\\* Begin Patch")))
                       | .[0])
          ] | map(select(. != null and . != "")) | .[0] // empty
    ' 2>/dev/null
}

extract_direct_paths() {
    printf '%s' "$INPUT" | jq -r '
        .tool_input as $t
        | [ ($t.file_path? // empty), ($t.path? // empty),
            ($t.target_file? // empty), ($t.filename? // empty) ]
          | map(select(type == "string" and . != "")) | .[]
    ' 2>/dev/null
}

# Parse `*** Add|Update|Delete File:` and `*** Move to:` headers out of a patch
# envelope. Pure awk on stdin — the patch text is never interpolated.
patch_target_paths() {
    printf '%s' "$1" | awk '
        /^\*\*\* (Add|Update|Delete) File: / {
            sub(/^\*\*\* (Add|Update|Delete) File: /, "");
            if (length($0) > 0) print $0;
            next
        }
        /^\*\*\* Move to: / {
            sub(/^\*\*\* Move to: /, "");
            if (length($0) > 0) print $0;
            next
        }
    ' 2>/dev/null
}

# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

GUARD_CWD="$EVENT_CWD"

if [[ "$TOOL_CLASS" == "shell" ]]; then
    COMMAND="$(extract_shell_command)"
    if [[ -z "${COMMAND//[[:space:]]/}" ]]; then
        fail_closed "tool '$TOOL_NAME' is a command-execution tool but no command could be extracted from its tool_input."
    fi

    WORKDIR="$(extract_workdir)"
    if [[ -n "$WORKDIR" ]]; then
        if [[ "$WORKDIR" == /* ]]; then
            GUARD_CWD="$WORKDIR"
        elif [[ -n "$EVENT_CWD" ]]; then
            GUARD_CWD="${EVENT_CWD%/}/$WORKDIR"
        fi
        if declare -F loom_canonical_path >/dev/null 2>&1; then
            GUARD_CWD="$(loom_canonical_path "$GUARD_CWD" "$EVENT_CWD")"
        fi
    fi

    REQUEST="$(claude_bash_request "$COMMAND" "$GUARD_CWD")"
    if [[ -z "$REQUEST" ]]; then
        fail_closed "the normalized guard request for tool '$TOOL_NAME' could not be encoded."
    fi

    # Loom workflow policy first (its denials are the most actionable), then the
    # destructive dispatcher (which also carries #4178 Bash write-confinement).
    apply_result "$(run_guard guard-loom-workflow.sh "$REQUEST")" "guard-loom-workflow.sh"
    apply_result "$(run_guard guard-destructive.sh "$REQUEST")" "guard-destructive.sh"
    emit_allow
fi

if [[ "$TOOL_CLASS" == "patch" ]]; then
    TARGETS=()
    PATCH_TEXT="$(extract_patch_text)"
    if [[ -n "$PATCH_TEXT" ]]; then
        while IFS= read -r p; do
            [[ -n "$p" ]] && TARGETS+=("$p")
        done < <(patch_target_paths "$PATCH_TEXT")
    fi
    while IFS= read -r p; do
        [[ -n "$p" ]] && TARGETS+=("$p")
    done < <(extract_direct_paths)

    if [[ ${#TARGETS[@]} -eq 0 ]]; then
        fail_closed "tool '$TOOL_NAME' mutates files but no target path could be extracted from its tool_input."
    fi

    for target in "${TARGETS[@]}"; do
        canonical="$target"
        if declare -F loom_canonical_path >/dev/null 2>&1; then
            canonical="$(loom_canonical_path "$target" "$GUARD_CWD")"
        fi
        [[ -n "$canonical" ]] || fail_closed "target path '$target' could not be canonicalized."
        REQUEST="$(claude_write_request "$canonical" "$GUARD_CWD")"
        if [[ -z "$REQUEST" ]]; then
            fail_closed "the normalized guard request for target '$target' could not be encoded."
        fi
        apply_result "$(run_guard guard-worktree-paths.sh "$REQUEST")" "guard-worktree-paths.sh"
    done
    emit_allow
fi

# Unreachable: every class is handled above. Fail closed if it ever is reached.
fail_closed "tool '$TOOL_NAME' reached the end of the bridge without a decision."
