#!/usr/bin/env bash
# spawn-claude.sh - Token-rotating launcher for Claude Code.
#
# This script is a thin layer that:
#   1. Selects a Claude Code OAuth token from .loom/tokens/ via the native
#      `loom-daemon tokens select` CLI (issue #4228, epic #4081 Phase 2 — the
#      historical interpreted-language selector call has been retired from
#      this path entirely; see loom-daemon/src/tokens_pool/select.rs).
#   2. Exports CLAUDE_CODE_OAUTH_TOKEN.
#   3. exec's the underlying CLI (`claude` by default, or
#      `claude-wrapper.sh` if --use-wrapper is passed for retry behavior).
#
# It does NOT replace the existing 1700-LOC `.loom/scripts/claude-wrapper.sh`,
# which provides retry, backoff, auth-cache, and error classification.
# Use `claude-wrapper.sh` directly when you need that behavior; use this
# script when you want pure token rotation in front of either `claude` or
# the wrapper.
#
# Behavior on missing tokens:
#   Token selection resolves the effective pool (issue #3938): the per-repo
#   pool at `<repo>/.loom/tokens/` when it holds `*.token` files, else the
#   shared machine-level pool at `~/.loom/tokens/` (override
#   `LOOM_SHARED_TOKENS_DIR`; set it empty to disable the fallback). This lets a
#   consumer repo the daemon dispatches into — which has no pool of its own —
#   spawn against the shared pool instead of hard-failing. All pool STATE
#   (`.bad_tokens`/`.ranking`/`.allowlist`/`.failure_counts`) lives in whichever
#   pool was selected, so it is never forked per repo.
#   When NEITHER pool exists/has tokens (or all tokens are bad), this script
#   exits 78 (EX_CONFIG) with a message instructing the user to run
#   `loom-daemon tokens bootstrap` (or `--shared` for the machine-level pool).
#   It does NOT silently fall back to keychain.
#   (The recovery advice named the Python `loom-tokens` console script until epic
#   #4081 Phase 4, #4557, deleted the package that provided it; `loom-daemon
#   tokens` is the only implementation now.)
#
# Worktree handling:
#   When invoked from a git worktree, the script resolves the canonical repo
#   root via `git rev-parse --git-common-dir` and looks up `.loom/tokens/`
#   there — never in the worktree's path.
#
# Env vars (pool location):
#   LOOM_SHARED_TOKENS_DIR  Shared machine-level pool location. Non-empty path
#                           overrides the `~/.loom/tokens` default; an empty
#                           value disables the shared fallback (per-repo only).
#
# Usage:
#   .loom/scripts/spawn-claude.sh -p "your prompt"
#   .loom/scripts/spawn-claude.sh --use-wrapper --prompt "..." --log /tmp/log
#
# Env vars:
#   LOOM_WORKSPACE         Override repo root detection.
#   LOOM_SPAWN_NO_EXPORT   If set, skip selection (caller already exported a
#                          token). Useful for testing the dispatch path.
#   LOOM_DAEMON_BIN        Override the `loom-daemon` binary used for native
#                          token selection (see lib/locate-daemon-bin.sh for
#                          the full resolution order: this env var ->
#                          `loom-daemon` on PATH -> build-output-relative
#                          candidates under the repo).
#   LOOM_MODEL             Model to pass as `claude --model <value>` (issue
#                          #3477). Lowest-priority tier: an explicit `--model`
#                          in the passthrough args always wins. When neither
#                          is set, NO --model flag is emitted and the session/
#                          CLI default is preserved.
#   LOOM_EFFORT            Reasoning-effort level to pass as `claude --effort
#                          <value>` (issue #3705). Mirrors LOOM_MODEL: an
#                          explicit `--effort` in the passthrough args wins;
#                          when neither is set, NO --effort flag is emitted and
#                          the session/CLI default is preserved. This sets a
#                          session-default effort for the whole spawned child —
#                          the sweep escalation ladder's per-rung `@effort`
#                          cannot be threaded here because the in-session Task
#                          tool exposes no effort parameter (see sweep.md).
#   LOOM_SWEEP_NICENESS    Scheduling niceness applied to this process (and
#                          everything it execs/forks — claude, claude-wrapper,
#                          cargo, rustc, test binaries) via a `nice -n N exec`
#                          re-exec, mirroring build-gate.sh's niceness pattern
#                          (issue #4233). Precedence: this env var ->
#                          `autonomous.spawnNiceness` config -> default `10`.
#   LOOM_SWEEP_NICE=0      Disables the ENTIRE priority mechanism (nice AND
#                          taskpolicy below), restoring pre-#4233 behavior.
#   LOOM_SWEEP_TASKPOLICY_CLASS  Optional macOS `taskpolicy -c <class>`
#                          scheduling class applied in-place (e.g. "utility").
#                          Precedence: this env var ->
#                          `autonomous.spawnTaskpolicyClass` config -> unset
#                          (off by default). No-op on non-macOS or when
#                          `taskpolicy` is unavailable.

set -euo pipefail

# --- Logging helpers (match loom convention) ---
RED='\033[0;31m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log_info() { echo -e "${BLUE}[$(date -u '+%Y-%m-%dT%H:%M:%SZ')]${NC} $*" >&2; }
log_warn() { echo -e "${YELLOW}[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] WARN${NC} $*" >&2; }
log_error() { echo -e "${RED}[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] ERROR${NC} $*" >&2; }

# --- Repo root resolution (handles worktrees) ---
# If LOOM_WORKSPACE is set, trust it. Otherwise:
#   1. Try `git rev-parse --git-common-dir` to find the canonical .git dir
#      (works inside main checkouts and worktrees alike).
#   2. The parent of `.git` (or of the common dir if it's not literally .git)
#      is the canonical repo root.
#   3. Fallback: walk up from the script's directory.
_resolve_workspace() {
    if [[ -n "${LOOM_WORKSPACE:-}" ]]; then
        printf '%s\n' "$LOOM_WORKSPACE"
        return
    fi

    local git_common_dir
    if git_common_dir="$(git rev-parse --git-common-dir 2>/dev/null)"; then
        # `git rev-parse --git-common-dir` may return a relative path inside
        # a worktree — convert to absolute, then take parent.
        if [[ ! "$git_common_dir" = /* ]]; then
            git_common_dir="$(cd "$git_common_dir" && pwd)"
        fi
        # If common-dir basename is `.git`, parent is repo root.
        # Otherwise (linked worktree case), it's the literal main `.git/`
        # directory — its parent is still the canonical main checkout.
        printf '%s\n' "$(dirname "$git_common_dir")"
        return
    fi

    # Fallback: relative to this script
    cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd
}

WORKSPACE="$(_resolve_workspace)"

_script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# --- Sweep/role-runner spawn scheduling priority (issue #4233) ---
#
# Issue #4231 (host meltdown 2026-07-27→28: 6-way sweep fan-out, load 118/28
# cores, WindowServer watchdog-killed 5×, loom-daemon crashed) diagnosed
# STARVATION, not raw load, as the actual failure mode: sweep worker processes
# — and every build they spawn (cargo, rustc, test binaries) — run at default
# niceness (0), so under fan-out they compete head-to-head with WindowServer,
# VNC, and other system daemons for CPU.
#
# #3985/#4020/#4073/#4084 (see build-gate.sh:32-82) already ran this
# experiment from the OTHER side — nicing the build GATE up so it could never
# starve sweeps. #4020 found the extreme gate=19 starved the gate itself into
# UNEVALUATED and settled on gate=5 (a mild, measured, unprivileged-achievable
# gap above sweeps' nice-0 default); its comment (build-gate.sh:63-70)
# explicitly flagged the untried alternative — nicing sweep children UP in the
# spawn path — as deliberately not done at the time. This block does that, so
# the gap between gate and sweep niceness is now real in both directions.
#
# Applied here — the outermost point in the daemon's actual dispatch path.
# `loom-daemon` invokes THIS script directly for both sweep dispatch
# (SweepRegistryConfig::resolve_spawn_bin in
# loom-daemon/src/sweep_registry.rs) and scheduled role-runner dispatch
# (role_runner.rs) — both unattended/background paths; MOM's interactive
# terminals run `claude` directly and never go through this script, so no
# separate "interactive opt-out" is needed. spawn-worker.sh (a not-yet-wired
# future multi-runtime seam, see its own header) execs into this script for
# the "claude" runtime and is therefore covered transparently; it applies no
# priority adjustment of its own so there is no double-apply risk.
#
# Mechanism: a `nice -n N exec "$0" "$@"` re-exec — mirroring build-gate.sh's
# `LOOM_BUILD_GATE_NICED` sentinel pattern — applied before ANY child process
# (token selection, `claude`, `claude-wrapper.sh`). Because `exec` preserves
# the PID and nice/taskpolicy attributes survive exec, the whole subsequent
# process tree (this script → claude/claude-wrapper → any cargo/rustc it
# forks) inherits the adjustment with no separate handling required
# downstream. The build gate has its own independent niceness policy
# (build-gate.sh) and is never invoked through this script, so there is no
# overlap with it either.
#
# Precedence (env > config > default), the tier order used throughout loom:
#   LOOM_SWEEP_NICENESS         > autonomous.spawnNiceness         > 10
#   LOOM_SWEEP_TASKPOLICY_CLASS > autonomous.spawnTaskpolicyClass  > (unset)
#
# LOOM_SWEEP_NICE=0 disables the ENTIRE mechanism (nice AND taskpolicy),
# restoring byte-identical pre-#4233 behavior (nice 0, no taskpolicy class).
#
# Default is nice ONLY, at a moderate value (10) — deliberately above the
# gate's 5, so sweeps now yield to the gate under contention, addressing the
# #4084 CPU-contention premise as a structural side effect. `taskpolicy` is
# opt-in and unset by default: on macOS, `-c utility` is the recommended class
# if enabled — deliberately NOT `-b`/background (DARWIN_BG), which also
# throttles disk I/O and network aggressively enough to risk *causing* the
# very timeouts this issue is trying to prevent.
if [[ -z "${LOOM_SWEEP_NICED:-}" && "${LOOM_SWEEP_NICE:-1}" != "0" ]]; then
    _sweep_niceness="${LOOM_SWEEP_NICENESS:-}"
    _sweep_taskpolicy_class="${LOOM_SWEEP_TASKPOLICY_CLASS:-}"
    _sweep_config_lib="${_script_dir}/lib/config-resolver.sh"
    if [[ ( -z "$_sweep_niceness" || -z "$_sweep_taskpolicy_class" ) \
          && -f "$_sweep_config_lib" ]]; then
        # shellcheck source=./lib/config-resolver.sh
        source "$_sweep_config_lib"
        [[ -z "$_sweep_niceness" ]] \
            && _sweep_niceness="$(loom_config_get "$WORKSPACE" "autonomous.spawnNiceness" "")"
        [[ -z "$_sweep_taskpolicy_class" ]] \
            && _sweep_taskpolicy_class="$(loom_config_get "$WORKSPACE" "autonomous.spawnTaskpolicyClass" "")"
    fi
    : "${_sweep_niceness:=10}"

    # taskpolicy sets the scheduling class on THIS running process (no
    # re-exec needed) and, like nice, survives the exec below since exec
    # preserves the PID. Best-effort: a failure here never blocks the spawn.
    if [[ -n "$_sweep_taskpolicy_class" ]] && command -v taskpolicy >/dev/null 2>&1; then
        if taskpolicy -c "$_sweep_taskpolicy_class" -p $$ >/dev/null 2>&1; then
            log_info "spawn-claude: applied taskpolicy -c $_sweep_taskpolicy_class (issue #4233)"
        else
            log_warn "spawn-claude: taskpolicy -c $_sweep_taskpolicy_class failed (non-fatal, continuing at default policy class)"
        fi
    fi

    if [[ "$_sweep_niceness" != "0" ]] && command -v nice >/dev/null 2>&1; then
        export LOOM_SWEEP_NICED=1
        log_info "spawn-claude: re-exec at nice -n $_sweep_niceness (issue #4233; LOOM_SWEEP_NICE=0 to disable)"
        exec nice -n "$_sweep_niceness" "$0" "$@"
    fi
fi

# --- Locate the loom-daemon binary (token selection, issue #4228) ---
# Resolution precedence (see lib/locate-daemon-bin.sh): $LOOM_DAEMON_BIN ->
# `loom-daemon` on PATH -> build-output-relative candidates under $WORKSPACE.
# shellcheck source=lib/locate-daemon-bin.sh
source "${_script_dir}/lib/locate-daemon-bin.sh"

# --- Daemon self-claim marker visibility (Issue #3823, observability #3967) ---
# `loom-daemon`'s `SweepRegistry::spawn_child` exports
# `LOOM_SWEEP_CLAIM_OWNED=<issue>` into a daemon-dispatched child so its
# `/loom:sweep` pre-flight recognises the daemon's own pre-dispatch
# `loom:issue -> loom:building` label flip as ITS OWN claim rather than
# self-skipping the issue as "another worker is in flight" (the #3967
# incident). Log the marker's presence/value here — unconditionally, on
# every spawn, whether daemon-dispatched or not — so a future occurrence is
# diagnosable straight from `.loom/logs/sweep-issue-<N>.log` without needing
# to reproduce the dispatch: this line, plus the `spawn-claude:` prefix, is
# already captured by `spawn_child`'s per-sweep log redirection, so any gap
# between the Rust `Command::env()` call and what this script's own
# environment actually sees is now directly observable instead of inferred.
log_info "spawn-claude: LOOM_SWEEP_CLAIM_OWNED=${LOOM_SWEEP_CLAIM_OWNED:-unset}"

# --- Argument parsing ---
USE_WRAPPER=false
PASSTHROUGH_ARGS=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --use-wrapper)
            USE_WRAPPER=true
            shift
            ;;
        --help|-h)
            sed -n '2,/^set -euo/p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' \
                | head -n -1
            exit 0
            ;;
        --)
            shift
            PASSTHROUGH_ARGS+=("$@")
            break
            ;;
        *)
            PASSTHROUGH_ARGS+=("$1")
            shift
            ;;
    esac
done

# --- Model selection (issue #3477, Phase 1; observability #3482, Phase 3a) ---
# Precedence: explicit `--model` in the passthrough args > LOOM_MODEL env >
# nothing (session/CLI default — no --model flag emitted at all).
#
# Observability (#3482): exactly ONE structured `spawn-claude: model=<value>`
# line is emitted on every spawn, covering all three precedence cases —
# including `model=default` when nothing is configured. The line is
# stderr-only and changes NO spawn behavior; downstream log scrapers key on
# the `model=` token.
_explicit_model=""
_has_model_arg=false
_prev_was_model_flag=false
for _arg in ${PASSTHROUGH_ARGS[@]+"${PASSTHROUGH_ARGS[@]}"}; do
    if [[ "$_prev_was_model_flag" == "true" ]]; then
        _explicit_model="$_arg"
        _prev_was_model_flag=false
        continue
    fi
    case "$_arg" in
        --model)
            _has_model_arg=true
            _prev_was_model_flag=true
            ;;
        --model=*)
            _has_model_arg=true
            _explicit_model="${_arg#--model=}"
            ;;
    esac
done

if [[ "$_has_model_arg" == "true" ]]; then
    if [[ -n "${LOOM_MODEL:-}" ]]; then
        log_info "spawn-claude: explicit --model in args wins over LOOM_MODEL='$LOOM_MODEL'"
    fi
    log_info "spawn-claude: model=${_explicit_model:-default} (from --model arg)"
elif [[ -n "${LOOM_MODEL:-}" ]]; then
    PASSTHROUGH_ARGS+=(--model "$LOOM_MODEL")
    log_info "spawn-claude: model=$LOOM_MODEL (from LOOM_MODEL)"
else
    log_info "spawn-claude: model=default"
fi

# --- Effort selection (issue #3705) ---
# Mirrors the model block above for the `claude --effort <level>` session knob.
# Precedence: explicit `--effort` in the passthrough args > LOOM_EFFORT env >
# nothing (session/CLI default — no --effort flag emitted at all).
#
# This is the ONLY per-call reasoning-effort surface available in this
# environment: the `claude` CLI exposes `--effort`, but the in-session Task
# tool (the sweep's per-role subagent dispatch, one level deep) exposes NO
# effort parameter. So spawn-claude/daemon can set a *session-default* effort
# for a whole `/loom:sweep` child; the escalation ladder's per-rung `@effort`
# still degrades to the bare model at Task-tool dispatch time (see
# `sweep.md` → "Effort graceful degradation").
#
# Observability: exactly ONE structured `spawn-claude: effort=<value>` line
# is emitted only when an effort is actually resolved (explicit arg or env).
# When nothing is configured, NO effort line is emitted and NO --effort flag
# is appended — byte-for-byte identical to pre-#3705 behavior.
_explicit_effort=""
_has_effort_arg=false
_prev_was_effort_flag=false
for _arg in ${PASSTHROUGH_ARGS[@]+"${PASSTHROUGH_ARGS[@]}"}; do
    if [[ "$_prev_was_effort_flag" == "true" ]]; then
        _explicit_effort="$_arg"
        _prev_was_effort_flag=false
        continue
    fi
    case "$_arg" in
        --effort)
            _has_effort_arg=true
            _prev_was_effort_flag=true
            ;;
        --effort=*)
            _has_effort_arg=true
            _explicit_effort="${_arg#--effort=}"
            ;;
    esac
done

if [[ "$_has_effort_arg" == "true" ]]; then
    if [[ -n "${LOOM_EFFORT:-}" ]]; then
        log_info "spawn-claude: explicit --effort in args wins over LOOM_EFFORT='$LOOM_EFFORT'"
    fi
    log_info "spawn-claude: effort=${_explicit_effort:-default} (from --effort arg)"
elif [[ -n "${LOOM_EFFORT:-}" ]]; then
    PASSTHROUGH_ARGS+=(--effort "$LOOM_EFFORT")
    log_info "spawn-claude: effort=$LOOM_EFFORT (from LOOM_EFFORT)"
fi

# --- Token selection ---
if [[ -z "${LOOM_SPAWN_NO_EXPORT:-}" && -z "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]]; then
    _daemon_bin="$(loom_locate_daemon_bin "$WORKSPACE")"
    if [[ -z "$_daemon_bin" ]] || ! "$_daemon_bin" tokens select --help >/dev/null 2>&1; then
        log_error "No loom-daemon binary supporting 'tokens select' was found."
        log_error "(\$LOOM_DAEMON_BIN -> 'loom-daemon' on PATH -> build-output-relative"
        log_error "candidates under the repo all came up empty or stale.)"
        log_error "Build or start one, then retry:"
        log_error "  ./.loom/scripts/cli/loom-daemon-start.sh"
        log_error "  cargo build --release -p loom-daemon"
        exit 78  # EX_CONFIG
    fi

    # --- Binary provenance at the selection site (issue #4643) ---
    # The binary that decides token selection is resolved HERE, independently
    # of any running daemon ($LOOM_DAEMON_BIN -> PATH -> build-output
    # candidates), so a long-running daemon can hand a sweep child a binary
    # older than the last merged selection fix. That staleness was the leading
    # hypothesis for the 2026-07-30 incident (13h-old exhaustion entries
    # appearing to block every spawn); refuting it took out-of-band forensics
    # on the host's installed binaries, because NOTHING in the sweep log
    # recorded which binary ran. Log path + version on every spawn so the next
    # occurrence is answerable straight from `.loom/logs/sweep-issue-<N>.log`.
    # Best-effort: a binary too old to support `--version` still logs its path.
    _daemon_version="$("$_daemon_bin" --version 2>/dev/null | head -1)"
    log_info "spawn-claude: token-selection binary: $_daemon_bin (${_daemon_version:-version unknown})"

    _select_args=(tokens select --workspace "$WORKSPACE" --export)
    # Pre-flight: auto-unpin if every allowlisted account has hit the
    # consecutive-failure threshold (default 5). Without this, an
    # operator-set pin can trap the spawner once all pinned accounts
    # exhaust their weekly quota. Empty-pool guard: we never silently
    # clear .bad_tokens — if that file blocks every account, the user
    # must intervene (e.g. `loom-daemon tokens unblock <name>`). Capability-probed
    # (issue #4228) so a daemon binary mid-roll that predates `--auto-unpin`
    # degrades to plain selection instead of a hard argument-parse failure.
    if "$_daemon_bin" tokens select --help 2>&1 | grep -q -- '--auto-unpin'; then
        _select_args+=(--auto-unpin)
    fi

    # Capture stdout (shell-evalable export lines) and stderr (errors /
    # advisories, e.g. a firing "[auto-unpin] ..." line) separately so log
    # output never contaminates what we're about to `eval`.
    _selection_stderr_file="$(mktemp)"
    _selection_output=""
    if ! _selection_output="$("$_daemon_bin" "${_select_args[@]}" 2>"$_selection_stderr_file")"; then
        log_error "Token selection failed:"
        cat "$_selection_stderr_file" >&2 || true
        rm -f "$_selection_stderr_file"
        log_error "Run '$_daemon_bin tokens bootstrap' to populate <repo>/.loom/tokens/,"
        log_error "or add '--shared' for the machine-level pool"
        log_error "(~/.loom/tokens, override LOOM_SHARED_TOKENS_DIR) that consumer"
        log_error "repos fall back to. Use '$_daemon_bin tokens unblock <name>' if"
        log_error ".bad_tokens is the cause."
        log_error "Spawn-claude refuses to auto-clear .bad_tokens — that's"
        log_error "intentional: an empty pool indicates a real auth problem."
        log_error "Set CLAUDE_CODE_OAUTH_TOKEN explicitly to bypass selection."
        exit 78  # EX_CONFIG
    fi
    # Surface any advisory stderr (e.g. the auto-unpin line) without treating
    # it as a failure — mirrors the historical Python pre-flight's `|| true`.
    cat "$_selection_stderr_file" >&2 || true
    rm -f "$_selection_stderr_file"

    # `--export` emits `export CLAUDE_CODE_OAUTH_TOKEN=...` and
    # `export LOOM_TOKEN_NAME=...` (both consumed by downstream processes,
    # including a claude-wrapper.sh invoked via --use-wrapper) plus a local
    # (non-exported) `LOOM_TOKEN_MODE=...` used only for the log line below.
    # Tokens are base64/hex-like and never contain a single quote in
    # practice — see `loom-daemon tokens select`'s own doc comment.
    eval "$_selection_output"

    if [[ -z "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]]; then
        log_error "Token selection returned an empty key for account '${LOOM_TOKEN_NAME:-unknown}'."
        exit 78
    fi

    log_info "spawn-claude: using OAuth account '${LOOM_TOKEN_NAME:-unknown}' (mode=${LOOM_TOKEN_MODE:-unknown})"
    echo "# LOOM_ACCOUNT name=${LOOM_TOKEN_NAME:-unknown}" >&2
    unset LOOM_TOKEN_MODE
fi

# --- Print-mode background-task wait ceiling (issue #3943) ---
# When the daemon spawns a sweep child as a headless `claude -p "/loom:sweep N"`
# session, the Claude Code harness (print mode) terminates still-running
# background tasks — the sweep's dispatched Builder/Judge subagents — after a
# 600s ceiling and exits the session. Any role phase taking >10 minutes is
# killed mid-build, causing loom:building<->loom:issue label ping-pong. Disable
# the ceiling (0 = no cap) so long-running phases run to completion. This is
# harmless for interactive use (no background-task reaping there). Use the
# `:=` default-assignment idiom so an operator override — e.g. a non-zero
# ceiling exported in the environment — is preserved.
: "${CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS:=0}"
export CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS

# --- Optional safehouse MCP server injection (issue #3999) ---
# When the `safehouse` config block is enabled and a socket + launch command
# resolve, inject a session-scoped MCP config that adds the `safehouse` stdio
# server alongside `loom`, with a PER-WORKER persona so two concurrent workers
# in the same workspace get distinct `SAFEHOUSE_PERSONA` values. The persona is
# drawn from a bounded, operator-pre-registered pool (`safehouse.workerPersonas`)
# round-robined by this worker's issue slot (`LOOM_SWEEP_CLAIM_OWNED`), because
# safehoused's allowlist is a static boot-time TOML array with no runtime/prefix
# registration — literal per-issue names cannot be minted at dispatch time.
#
# Delivery shape: a session-scoped `--mcp-config <file>` (not a rewrite of the
# workspace `.mcp.json`). Concurrent sweeps SHARE the workspace root, so a
# per-worker persona cannot live in the shared file; a session-scoped config is
# the only correct per-worker surface. The file lists `loom` FIRST so it is
# self-contained even if the session's cwd has no project `.mcp.json`.
#
# Degradation contract (mirrors #3997): disabled/absent ⇒ byte-for-byte no-op
# (no --mcp-config appended). Enabled but the launch command is missing, or no
# socket resolves ⇒ log a warning and SKIP injection — the `loom` MCP server is
# never affected and the worker always starts. Only the socket path (no token or
# key) is ever written into the injected config.
_mcp_config_lib="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/mcp-config.sh"
if [[ -f "$_mcp_config_lib" ]]; then
    # shellcheck source=./lib/mcp-config.sh
    source "$_mcp_config_lib"

    # Respect an explicit operator-supplied --mcp-config (do not clobber).
    _has_mcp_config_arg=false
    for _arg in ${PASSTHROUGH_ARGS[@]+"${PASSTHROUGH_ARGS[@]}"}; do
        case "$_arg" in
            --mcp-config | --mcp-config=*) _has_mcp_config_arg=true; break ;;
        esac
    done

    if [[ "$_has_mcp_config_arg" == "false" \
        && "$(loom_mcp_safehouse_enabled "$WORKSPACE")" == "true" ]]; then
        _sh_socket="$(loom_mcp_safehouse_socket "$WORKSPACE")"
        _sh_command="$(loom_mcp_safehouse_command "$WORKSPACE")"
        _sh_pool="$(loom_mcp_worker_personas "$WORKSPACE")"
        _sh_fallback="$(loom_mcp_safehouse_persona_fallback "$WORKSPACE")"
        _sh_persona="$(loom_mcp_pick_persona \
            "${LOOM_SWEEP_CLAIM_OWNED:-}" "$_sh_pool" "$_sh_fallback")"

        # Export persona + socket into the SESSION process environment — not
        # only into the injected MCP config file's server env — so Bash-tool
        # helpers can reach safehoused. Lifecycle role subagents (loom-builder /
        # loom-judge / loom-doctor) pin their tool allowlists to Read/Glob/Grep/
        # Bash(/Write/Edit) with no MCP tools, so the injected `safehouse_send`
        # tool is invisible to them; `fleet-send.sh` (Bash) is their path to the
        # room and it resolves the socket/persona from these env vars (#4199).
        # Only the socket path is exported (no token or key). Guard on a
        # resolvable socket — with none there is nothing to connect to, and this
        # is independent of whether the safehouse-mcp launch command is present.
        if [[ -n "$_sh_socket" ]]; then
            export SAFEHOUSE_PERSONA="$_sh_persona"
            export SAFEHOUSED_SOCKET="$_sh_socket"
        fi

        if [[ -z "$_sh_socket" ]]; then
            log_warn "spawn-claude: safehouse enabled but no socket resolves (safehouse.socket / \$LOOM_SAFEHOUSE_SOCKET / \$SAFEHOUSED_SOCKET); skipping safehouse MCP injection (loom MCP unaffected)."
        elif ! command -v "$_sh_command" >/dev/null 2>&1; then
            log_warn "spawn-claude: safehouse launch command '$_sh_command' not found in PATH; skipping safehouse MCP injection (loom MCP unaffected)."
        else
            if [[ ! -S "$_sh_socket" && ! -e "$_sh_socket" ]]; then
                log_warn "spawn-claude: safehouse socket '$_sh_socket' not present at spawn; injecting anyway (best-effort — safehouse-mcp connects lazily and never blocks the worker)."
            fi
            _mcp_session_config="$(mktemp -t loom-mcp-config.XXXXXX 2>/dev/null || mktemp)"
            if loom_mcp_emit_config "$WORKSPACE" "$_sh_socket" "$_sh_persona" "$_sh_command" >"$_mcp_session_config" 2>/dev/null; then
                PASSTHROUGH_ARGS+=(--mcp-config "$_mcp_session_config")
                log_info "spawn-claude: injected safehouse MCP server (persona=$_sh_persona, socket=$_sh_socket, config=$_mcp_session_config)"
            else
                rm -f "$_mcp_session_config"
                log_warn "spawn-claude: failed to generate safehouse MCP config; skipping injection (loom MCP unaffected)."
            fi
        fi
    fi
fi

# --- Dispatch ---
if [[ "$USE_WRAPPER" == "true" ]]; then
    _wrapper="${WORKSPACE}/.loom/scripts/claude-wrapper.sh"
    if [[ ! -x "$_wrapper" ]]; then
        log_error "Cannot find executable claude-wrapper.sh at $_wrapper"
        exit 1
    fi
    exec "$_wrapper" "${PASSTHROUGH_ARGS[@]}"
fi

# Default: exec the `claude` CLI directly.
if ! command -v claude >/dev/null 2>&1; then
    log_error "'claude' command not found in PATH."
    log_error "Install Claude Code or pass --use-wrapper to invoke claude-wrapper.sh."
    exit 127
fi
echo "# LOOM_CLI_START runtime=claude" >&2
exec claude "${PASSTHROUGH_ARGS[@]}"
