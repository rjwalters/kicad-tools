#!/usr/bin/env bash
# guard-worktree-paths.sh - PreToolUse hook to confine Edit/Write to worktree
#
# Blocks Edit and Write tool calls whose file_path resolves outside a
# builder's issue worktree. This prevents builders from escaping their
# worktree and modifying files in the main repository (see issue #2441,
# #4007).
#
# Two independent mechanisms, tried in order:
#
#   1. Env fast path (LOOM_WORKTREE_PATH): when set, only that exact
#      worktree is allowed. Set by tmux/manual sessions that pin one process
#      to one worktree. This is unchanged from the original hook.
#
#   2. Path-derived fallback (no LOOM_WORKTREE_PATH): a daemon-dispatched
#      sweep hosts multiple Task-subagent builders (different issues) in one
#      shared process env, so a single process-wide LOOM_WORKTREE_PATH
#      cannot work there (#3719) -- nothing sets it on that path and the
#      hook was structurally inert. Instead, derive the answer from the
#      target path itself: walk up from the resolved target looking for the
#      `.loom-managed` sentinel that `worktree.sh` writes at the root of
#      every worktree it creates (and never at the main repo root). If the
#      target is inside ANY managed worktree, allow it -- we cannot tell
#      which issue a given subagent owns (no ambient signal exists for
#      that), so this deliberately does not attempt cross-issue isolation.
#      A worktree git knows about but Loom did not create (a plain `git
#      worktree add`, so no sentinel) is recognized too, via `git worktree
#      list --porcelain` -- otherwise one nested under the main checkout
#      (`<main>/.claude/worktrees/x`) is misread as the main checkout itself
#      (#7415; see registered_worktree_roots() for the trust-boundary note).
#      What it DOES catch -- the actual failure mode in #2802 / #3513 /
#      #4007 -- is a write that resolves to the MAIN checkout (typically a
#      repo-relative path evaluated after a cwd reset) while worktree
#      isolation is in play for this repo/session (i.e. at least one
#      managed worktree currently exists). If no managed worktree has ever
#      been created, the hook fails open -- there is nothing to protect.
#
# Toggle: guards.worktreeIsolation (default true) / LOOM_GUARD_WORKTREE_ISOLATION
# env override, following the resolution order used by every other guard
# category in this repo (env > config > default; pattern documented at
# guard-destructive-generic.sh around the guards.sqlDdl toggle).
#
# Input (JSON on stdin):
#   { "tool_input": { "file_path": "/path/to/file", ... }, "cwd": "/cwd" }
#
# Output:
#   Exit 0 with no output = allow
#   Exit 0 with JSON { "hookSpecificOutput": { "permissionDecision": "deny", ... } } = block
#
# Contract: NEVER exits non-zero. Fails open on every unexpected condition
# (missing jq, unparseable input, no sentinel anywhere, resolver errors) so a
# broken guard cannot wedge all agent writes.

# Determine main repo root via git-common-dir (works from worktrees and subdirectories).
# `pwd -P` (physical, symlinks resolved) rather than plain `pwd` (#4495): the
# target path is canonicalized with symlinks resolved below, so the root it is
# compared against must be resolved the same way — otherwise a repo reached
# through a symlinked ancestor (the common case for `/tmp` on macOS, which is a
# symlink to `/private/tmp`) never string-matches and the deny silently misses.
MAIN_ROOT="$(cd "$(git rev-parse --git-common-dir 2>/dev/null)/.." 2>/dev/null && pwd -P)" || \
MAIN_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." 2>/dev/null && pwd -P 2>/dev/null || echo ".")"
# ...and the LOGICAL spelling of the same root (symlinks intact). Both are kept
# because the canonicalization of the TARGET path degrades to lexical
# normalization on a host without python3/GNU realpath (or an install that
# predates defaults/scripts/lib/canonical-path.sh). In that degraded mode a
# target reached through a symlinked ancestor still spells the logical root, and
# comparing against the physical root alone would silently stop denying. Both
# prefixes are checked in path_derived_allow(), so neither spelling escapes.
MAIN_ROOT_LOGICAL="$(cd "$(git rev-parse --git-common-dir 2>/dev/null)/.." 2>/dev/null && pwd)" || \
MAIN_ROOT_LOGICAL="$MAIN_ROOT"
[[ -n "$MAIN_ROOT_LOGICAL" ]] || MAIN_ROOT_LOGICAL="$MAIN_ROOT"
HOOK_ERROR_LOG="${MAIN_ROOT}/.loom/logs/hook-errors.log"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd 2>/dev/null || echo ".")"

# Shared config-tier resolver (#4063 / Epic #3835 Phase 5, #4262). Source
# defaults/scripts/lib/config-resolver.sh so the two reads below (the
# guards.worktreeIsolation toggle and worktree.root) honor the full tier chain
# — including .loom-project/project.json — instead of a direct single-file jq
# read against legacy .loom/config.json only. At runtime SCRIPT_DIR is
# .loom/hooks/ (project-level wiring) or defaults/hooks/ (machine-level
# wiring, #4262); in both layouts .loom/scripts (a symlink to
# defaults/scripts) or defaults/scripts sits alongside, so ../scripts/lib
# resolves. Best-effort: a missing/unsourceable lib leaves loom_resolve_config
# undefined and the readers below fall back to their documented defaults.
if [[ -f "$SCRIPT_DIR/../scripts/lib/config-resolver.sh" ]]; then
    # shellcheck source=/dev/null
    source "$SCRIPT_DIR/../scripts/lib/config-resolver.sh" 2>/dev/null || true
fi

# Shared symlink-aware canonicalization (#4495). Replaces this hook's original
# `python3 os.path.normpath` call, which was purely LEXICAL: it collapsed `.`
# and `..` but left symlinks intact, so `<worktree>/link-to-main/CLAUDE.md`
# looked like it was inside the worktree while actually resolving into the main
# checkout. loom_canonical_path resolves every component that exists and
# normalizes the tail that does not, so a `Write` to a brand-new file still
# works. Best-effort source: a missing lib leaves loom_canonical_path undefined
# and the normalization below falls back to the historical normpath behavior,
# preserving this hook's fail-open contract.
if [[ -f "$SCRIPT_DIR/../scripts/lib/canonical-path.sh" ]]; then
    # shellcheck source=/dev/null
    source "$SCRIPT_DIR/../scripts/lib/canonical-path.sh" 2>/dev/null || true
fi

log_hook_error() {
    mkdir -p "$(dirname "$HOOK_ERROR_LOG")" 2>/dev/null || true
    echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] [guard-worktree-paths] $1" >> "$HOOK_ERROR_LOG" 2>/dev/null || true
}

# Top-level error trap: on ANY unexpected error, allow to prevent infinite retry loops
trap 'log_hook_error "Unexpected error on line ${LINENO}: ${BASH_COMMAND:-unknown} (exit=$?)"; exit 0' ERR

# =============================================================================
# Guard category toggle — guards.worktreeIsolation / LOOM_GUARD_WORKTREE_ISOLATION
#
# Default ON. Resolution order (highest precedence first):
#   1. LOOM_GUARD_WORKTREE_ISOLATION env var (0/false/no disables, 1/true/yes forces on)
#   2. Tiered config (Epic #3835) -> guards.worktreeIsolation, honoring
#      .loom-project/project.json over legacy .loom/config.json (default true
#      when absent from every tier)
#   3. Default: true (guard on)
#
# The config read is best-effort: any parse failure falls through to
# guard-ON and never trips the ERR trap or produces a non-zero exit.
#
# CARVE-OUT (#4241, same class as #4063), UPDATED for Epic #3835 Phase 5
# (#4262): worktree_isolation_guard_enabled() is called UNCONDITIONALLY as the
# very first thing this hook does, on EVERY Edit/Write PreToolUse -- there is
# no cheaper structural pre-check upstream of it (unlike
# guard-destructive-generic.sh's cold-path toggles, which only read config
# lazily after a specific pattern has already matched). loom_resolve_config
# soft-reads all tier files plus a merge on every call, so calling it directly
# from BOTH readers below would double the fork cost. Instead,
# resolved_config() (below) calls loom_resolve_config() at most ONCE per
# invocation and caches the merged JSON string; both readers then run one
# cheap in-memory `jq` filter against that cached string (no extra file I/O).
# This is exactly the mitigation this carve-out originally called for --
# "prefer caching a repo-scoped loom_resolve_config() result once per
# process" -- so `.loom-project/project.json` (and every other tier) now
# reaches both toggles without re-forking the merge per reader.
# =============================================================================
_WORKTREE_GUARD_CONFIG_CACHE=""
_WORKTREE_GUARD_CONFIG_DONE=""
resolved_config() {
    if [[ -z "$_WORKTREE_GUARD_CONFIG_DONE" ]]; then
        _WORKTREE_GUARD_CONFIG_DONE=1
        if [[ -n "$MAIN_ROOT" ]] && command -v loom_resolve_config &>/dev/null; then
            _WORKTREE_GUARD_CONFIG_CACHE=$(loom_resolve_config "$MAIN_ROOT" 2>/dev/null) || _WORKTREE_GUARD_CONFIG_CACHE='{}'
        fi
        [[ -n "$_WORKTREE_GUARD_CONFIG_CACHE" ]] || _WORKTREE_GUARD_CONFIG_CACHE='{}'
    fi
    printf '%s' "$_WORKTREE_GUARD_CONFIG_CACHE"
}

worktree_isolation_guard_enabled() {
    local enabled=true
    if command -v jq &>/dev/null; then
        # jq // is alternative-on-null, not default-on-missing, so use
        # if/then/else to treat only an explicit `false` as disabled (a
        # missing guards.worktreeIsolation key stays on).
        local raw
        raw=$(resolved_config | jq -r 'if .guards.worktreeIsolation == false then "false" else "true" end' 2>/dev/null) || raw=true
        [[ -n "$raw" ]] && enabled="$raw"
    fi
    case "${LOOM_GUARD_WORKTREE_ISOLATION:-}" in
        0|false|no)  enabled=false ;;
        1|true|yes)  enabled=true ;;
    esac
    [[ "$enabled" == "true" ]]
}

if ! worktree_isolation_guard_enabled; then
    exit 0
fi

# --------------------------------------------------------------------------
# Helpers for the path-derived fallback (mechanism 2 above)
# --------------------------------------------------------------------------

# Walk up from $1 (a path that may or may not exist) looking for a
# `.loom-managed` sentinel. Prints the sentinel's directory and returns 0 if
# found; returns 1 if none is found by the time we reach filesystem root.
# Pure string manipulation (no subprocess forks, no `cd`) so it never errors
# on a path whose parent directories don't exist yet (new file via Write).
walk_up_for_sentinel() {
    local dir="$1"
    if [[ ! -d "$dir" ]]; then
        dir="${dir%/*}"
        [[ -z "$dir" ]] && dir="/"
    fi
    local i=0
    while [[ $i -lt 64 ]]; do
        if [[ -f "$dir/.loom-managed" ]]; then
            printf '%s' "$dir"
            return 0
        fi
        [[ "$dir" == "/" ]] && break
        dir="${dir%/*}"
        [[ -z "$dir" ]] && dir="/"
        i=$((i + 1))
    done
    return 1
}

# Resolve the base directory that holds managed worktrees, honoring the same
# override precedence as `defaults/scripts/lib/worktree-root.sh`
# (LOOM_WORKTREE_ROOT env > .loom/config.json worktree.root > default). Kept
# as a small inline duplicate rather than sourcing the library, so this
# guard's fail-open contract does not depend on another script's behavior.
#
# UPDATED for Epic #3835 Phase 5 (#4262): this function is only reached on the
# rare deny-candidate path (target resolves inside the main checkout and
# outside every worktree, see path_derived_allow() above), so per-call fork
# cost was never the concern here -- the original CARVE-OUT (#4241) instead
# worried about this guard's fail-open contract depending on
# config-resolver.sh's own behavior. That concern is addressed the same way
# as the toggle above: resolved_config() calls loom_resolve_config() (if
# sourced) at most once per invocation and soft-fails to '{}' on any error,
# so a resolver failure still degrades to the pre-#4262 "no config, fall back
# to the default" behavior rather than propagating an error. This lets
# `.loom-project/project.json -> worktree.root` (Epic #3835) reach this guard
# without a second, independent read path to keep in lockstep.
resolve_worktree_base() {
    if [[ -n "${LOOM_WORKTREE_ROOT:-}" && "${LOOM_WORKTREE_ROOT}" == /* ]]; then
        printf '%s' "${LOOM_WORKTREE_ROOT%/}/$(basename "$MAIN_ROOT")"
        return 0
    fi
    if [[ -n "$MAIN_ROOT" ]] && command -v jq &>/dev/null; then
        local cfg_root
        cfg_root=$(resolved_config | jq -r '.worktree.root? // empty' 2>/dev/null) || cfg_root=""
        if [[ -n "$cfg_root" && "$cfg_root" == /* ]]; then
            printf '%s' "${cfg_root%/}/$(basename "$MAIN_ROOT")"
            return 0
        fi
    fi
    printf '%s' "${MAIN_ROOT}/.loom/worktrees"
}

# --------------------------------------------------------------------------
# Registered-but-unmanaged git worktrees (#7415)
#
# `.loom-managed` (written only by worktree.sh) is this guard's signal for
# "this is a worktree, not the main checkout". A worktree created with a plain
# `git worktree add` never carries one -- harmless while it sits OUTSIDE the
# main checkout (the under_main test in path_derived_allow() already lets it
# through), but a worktree nested UNDER the main checkout
# (`<main>/.claude/worktrees/x`, a layout some repos document) matches the
# main-root prefix test and is denied, even though git itself considers it a
# separate working tree sharing nothing with the main checkout's index or
# tracked files.
#
# So: consult `git worktree list --porcelain` and treat a target inside any
# registered worktree OTHER than the main one as "not the main checkout". The
# main worktree entry is excluded in both spellings, so this can never become
# an allow for the checkout this guard protects.
#
# TRUST BOUNDARY (#4245): this widens recognition from "worktrees Loom
# created" to "worktrees git knows about" -- a stray or deliberate `git
# worktree add` elsewhere in the repo now gains the same treatment. Accepted
# knowingly: the sentinel was never an authentication boundary (the deny
# message itself says the check "cannot verify it belongs to the acting
# session", and a `touch .loom-managed` already forges it), while the false
# positive it caused blocks a documented, legitimate workflow. The main
# checkout's own working tree -- what #4007/#4178 protect -- stays denied.
#
# Resolved lazily and cached: only reached on the deny-candidate path, so the
# `git` call never runs for an ordinary in-worktree write, and the parse forks
# nothing per entry (a `cd … && pwd -P` per worktree would be ~25 extra forks
# in a normal Loom checkout, inside a PreToolUse hook).
_REGISTERED_WT_ROOTS=""
_REGISTERED_WT_ROOTS_DONE=""
registered_worktree_roots() {
    local line wt
    if [[ -z "$_REGISTERED_WT_ROOTS_DONE" ]]; then
        _REGISTERED_WT_ROOTS_DONE=1
        if [[ -n "$MAIN_ROOT" && -d "$MAIN_ROOT" ]]; then
            while IFS= read -r line; do
                [[ "$line" == "worktree "* ]] || continue
                wt="${line#worktree }"
                [[ "$wt" == /* ]] || continue
                wt="${wt%/}"
                # Skip the MAIN worktree entry in either spelling.
                if [[ "$wt" == "$MAIN_ROOT" || "$wt" == "$MAIN_ROOT_LOGICAL" ]]; then
                    continue
                fi
                _REGISTERED_WT_ROOTS+="${wt}"$'\n'
                # ...plus the ALTERNATE root spelling, for the same reason
                # MAIN_ROOT_LOGICAL exists: git records exactly one spelling of
                # a nested worktree's path, while the canonicalized target can
                # degrade to a lexical form that keeps a symlinked ancestor
                # intact. Only a worktree nested under the main root is
                # reachable here (path_derived_allow's under_main test already
                # let everything else through), so swapping just the root
                # prefix covers both spellings without a fork per entry.
                if [[ -n "$MAIN_ROOT_LOGICAL" && "$MAIN_ROOT_LOGICAL" != "$MAIN_ROOT" ]]; then
                    case "$wt" in
                        "$MAIN_ROOT"/*)
                            _REGISTERED_WT_ROOTS+="${MAIN_ROOT_LOGICAL}/${wt#"$MAIN_ROOT"/}"$'\n' ;;
                        "$MAIN_ROOT_LOGICAL"/*)
                            _REGISTERED_WT_ROOTS+="${MAIN_ROOT}/${wt#"$MAIN_ROOT_LOGICAL"/}"$'\n' ;;
                    esac
                fi
            done < <(git -C "$MAIN_ROOT" worktree list --porcelain 2>/dev/null || true)
        fi
    fi
    printf '%s' "$_REGISTERED_WT_ROOTS"
}

# True if $1 (an absolute, canonicalized path) sits inside a registered
# worktree that is not the main checkout.
in_registered_worktree() {
    local target="$1" root roots
    [[ -n "$target" ]] || return 1
    roots=$(registered_worktree_roots)
    [[ -n "$roots" ]] || return 1
    while IFS= read -r root; do
        [[ -n "$root" ]] || continue
        if [[ "$target" == "$root" || "$target" == "$root"/* ]]; then
            return 0
        fi
    done <<< "$roots"
    return 1
}

# The worktree location to point a denied write at. Names the ACTUALLY
# configured worktree root (LOOM_WORKTREE_ROOT env > worktree.root config >
# in-repo default) rather than hardcoding `.loom/worktrees/issue-<N>`, which is
# wrong for any repo that relocates its worktree root (#7415).
worktree_hint() {
    local base
    base="$(resolve_worktree_base 2>/dev/null)" || base=""
    if [[ -n "$base" ]]; then
        printf '%s/issue-<N>' "$base"
    else
        printf '.loom/worktrees/issue-<N>'
    fi
}

# True if at least one managed worktree currently exists under $1
# (`<base>/<name>/.loom-managed`, depth 2 -- matches worktree.sh's layout).
any_managed_worktree_exists() {
    local base="$1"
    [[ -n "$base" && -d "$base" ]] || return 1
    local hit
    hit=$(find "$base" -mindepth 2 -maxdepth 2 -name '.loom-managed' -print -quit 2>/dev/null) || hit=""
    [[ -n "$hit" ]]
}

# Decide allow(0)/deny(1) for a resolved target path when no
# LOOM_WORKTREE_PATH fast path is set. See the mechanism-2 comment above for
# the rationale.
path_derived_allow() {
    local target="$1"

    # (a) Already inside some managed worktree -> allow.
    if walk_up_for_sentinel "$target" >/dev/null; then
        return 0
    fi

    # Not under any worktree. If it's also not under the main checkout (in
    # EITHER its physical or its logical spelling — see MAIN_ROOT_LOGICAL
    # above), there's nothing this guard protects (e.g. /tmp scratch files)
    # -> allow.
    local target_slash="${target%/}/"
    local main_slash="${MAIN_ROOT%/}/" alt_slash="${MAIN_ROOT_LOGICAL%/}/"
    local under_main=false
    if [[ -n "$MAIN_ROOT" && ( "$target_slash" == "$main_slash"* || "$target" == "$MAIN_ROOT" ) ]]; then
        under_main=true
    fi
    if [[ -n "$MAIN_ROOT_LOGICAL" && ( "$target_slash" == "$alt_slash"* || "$target" == "$MAIN_ROOT_LOGICAL" ) ]]; then
        under_main=true
    fi
    if [[ "$under_main" != true ]]; then
        return 0
    fi

    # (c) Inside a git-registered worktree nested under the main checkout
    # (created by a plain `git worktree add`, so carrying no `.loom-managed`
    # sentinel) -> allow. git treats that directory as a separate working
    # tree; it is not the main checkout. See registered_worktree_roots()'s
    # doc comment for the trust-boundary trade-off this accepts (#7415).
    if in_registered_worktree "$target"; then
        return 0
    fi

    # Target resolves inside the main checkout and outside every worktree.
    # Deny only if worktree isolation is actually in play for this
    # repo/session (a managed worktree exists somewhere); otherwise fail
    # open (a repo/session that has never created a worktree is unaffected).
    local base
    base="$(resolve_worktree_base)"
    if any_managed_worktree_exists "$base"; then
        return 1
    fi
    return 0
}

# Emit a deny decision and exit 0 (the hook itself never exits non-zero).
emit_deny() {
    local reason="$1"
    log_hook_error "Denied: $reason"
    if jq -n --arg reason "$reason" '{
        hookSpecificOutput: {
            permissionDecision: "deny",
            permissionDecisionReason: $reason
        }
    }' 2>/dev/null; then
        exit 0
    fi
    # jq failed — emit raw JSON as fallback
    local escaped
    escaped=$(echo "$reason" | sed 's/\\/\\\\/g; s/"/\\"/g; s/\t/\\t/g')
    echo "{\"hookSpecificOutput\":{\"permissionDecision\":\"deny\",\"permissionDecisionReason\":\"${escaped}\"}}"
    exit 0
}

# --------------------------------------------------------------------------
# Fast path: LOOM_WORKTREE_PATH (tmux/manual sessions)
# --------------------------------------------------------------------------
WORKTREE_PATH="${LOOM_WORKTREE_PATH:-}"

# Read stdin (needed by both mechanisms below)
INPUT=$(cat 2>/dev/null) || INPUT=""

# Verify jq is available
if ! command -v jq &>/dev/null; then
    log_hook_error "jq not found in PATH — allowing (cannot parse input)"
    exit 0
fi

# Extract file_path from tool input
FILE_PATH=$(echo "$INPUT" | jq -r '.tool_input.file_path // empty' 2>/dev/null) || FILE_PATH=""

if [[ -z "$FILE_PATH" ]]; then
    # No file_path in input (shouldn't happen for Edit/Write) — allow
    exit 0
fi

# Resolve the file path to absolute (handle relative paths via cwd)
if [[ "$FILE_PATH" != /* ]]; then
    CWD=$(echo "$INPUT" | jq -r '.cwd // empty' 2>/dev/null) || CWD=""
    if [[ -n "$CWD" ]]; then
        FILE_PATH="${CWD}/${FILE_PATH}"
    fi
fi

# Canonicalize the path: resolve symlinks in every component that EXISTS, and
# normalize `.`/`..` in the tail that does not (so a Write to a brand-new file
# still resolves). loom_canonical_path (defaults/scripts/lib/canonical-path.sh,
# #4495) owns the resolution chain — python3 realpath, GNU `realpath -m`, then a
# pure-bash lexical fallback that reproduces the pre-#4495 normpath behavior.
# If the lib could not be sourced, fall back to the original inline normpath so
# this hook's fail-open contract does not depend on it.
if declare -F loom_canonical_path >/dev/null 2>&1; then
    NORM_PATH=$(loom_canonical_path "$FILE_PATH" "${CWD:-}" 2>/dev/null) || NORM_PATH="$FILE_PATH"
    [[ -n "$NORM_PATH" ]] || NORM_PATH="$FILE_PATH"
else
    NORM_PATH=$(printf '%s' "$FILE_PATH" | python3 -c "import os,sys; print(os.path.normpath(sys.stdin.read()))" 2>/dev/null) || NORM_PATH="$FILE_PATH"
fi

if [[ -n "$WORKTREE_PATH" ]]; then
    # Normalize worktree path (resolve symlinks, remove trailing slash)
    WORKTREE_REAL=$(cd "$WORKTREE_PATH" 2>/dev/null && pwd -P 2>/dev/null) || WORKTREE_REAL="$WORKTREE_PATH"
    WORKTREE_REAL="${WORKTREE_REAL%/}"

    # Check if the normalized path starts with the worktree path
    if [[ "$NORM_PATH/" == "$WORKTREE_REAL/"* ]] || [[ "$NORM_PATH/" == "$WORKTREE_PATH/"* ]]; then
        # Path is within the worktree — allow
        exit 0
    fi

    emit_deny "BLOCKED: Edit/Write path '${NORM_PATH}' is outside worktree '${WORKTREE_PATH}'. Use paths within the worktree directory."
fi

# --------------------------------------------------------------------------
# Path-derived fallback: no LOOM_WORKTREE_PATH (daemon-dispatched sweep path)
# --------------------------------------------------------------------------
if path_derived_allow "$NORM_PATH"; then
    exit 0
fi

emit_deny "BLOCKED: Edit/Write path '${NORM_PATH}' resolves to the main repository checkout ('${MAIN_ROOT}'), but a Loom-managed worktree exists elsewhere in this repository (this check cannot verify it belongs to the acting session — see #4245). Builders must write inside their issue worktree ($(worktree_hint)), never the main checkout. Do NOT retry this write via Bash redirection/tee/sed -i/cp/mv -- that is also confined (guard-destructive-generic.sh, #4178) and denied for the same reason. cd into your issue worktree and write there instead. Not a Builder and need to write here directly? Set guards.worktreeIsolation:false in .loom/config.json for the session -- an inline 'LOOM_GUARD_WORKTREE_ISOLATION=0 <command>' prefix does NOT work (this hook runs as a separate process). (#4007)"
