#!/usr/bin/env bash
# spawn-codex.sh - Runtime adapter #2: the OpenAI Codex CLI worker runner.
#
# This is the Codex sibling of `spawn-claude.sh`, sitting behind the same
# `spawn-worker.sh` dispatch seam: `LOOM_RUNTIME=codex` (or `.loom/config.json`
# -> `runtimes.default = "codex"`) makes the dispatcher exec THIS script with
# every argument forwarded verbatim. It implements contract point 1 (Spawn) of
# `.loom/docs/runtime-adapters.md` against the Codex CLI's headless mode.
#
# Attribution: this adapter is a PORT of the Codex worker support built in the
# gpeyton/loom fork by Graham Peyton (fork PRs #15/#16/#20/#40), restructured to
# land behind upstream's runtime-adapter contract (epic #4167, Phase 2, issue
# #4468). It is a design port, not a cherry-pick: the auth chain is deliberately
# narrowed to `CODEX_HOME` profile passthrough (no provider-aware token pool —
# that is Phase 4), and the sandbox default is inverted from the fork's
# full-access posture (see "Sandbox mapping" below).
#
# Trust boundary: `defaults/docs/guardrail-parity-codex.md` is the required
# guardrail-parity document for this adapter (contract point 6). Read it before
# promoting Codex beyond tier-2 — Codex 0.146.0 DOES expose a `hooks.json`
# `pre_tool_use` event, but Loom does not wire into it yet (see gap 1 in that
# doc), so Loom's guard hooks do not fire for a Codex worker today.
#
# Production sandbox posture (issue #4478, decided 2026-07-31): read-only
# default, with Builder-role-only escalation to workspace-write (+
# LOOM_CODEX_NETWORK=1 for push access) — no fleet-wide danger-full-access.
# See guardrail-parity-codex.md § "Promotion gate" for the full decision and
# its relationship to the hooks/worktreeIsolation evidence gate above.
#
# ---------------------------------------------------------------------------
# Minimum supported Codex CLI version: 0.146.0
#
#   Every flag and config key below was verified against `codex exec --help` on
#   codex-cli 0.146.0 (2026-07-29). The Codex CLI surface churns between
#   releases; if a future Codex renames or removes any of these, bump this pin
#   and re-verify:
#     - `codex exec "<prompt>"`               non-interactive run
#     - `-m <model>` / `--model <model>`      model selection
#     - `-s <mode>` / `--sandbox <mode>`      read-only|workspace-write|danger-full-access
#     - `--skip-git-repo-check`               allow running outside a git repo
#     - `-c <key=value>`                      config override (TOML-parsed value)
#
#   Flags the fork's runner used that DO NOT EXIST on `codex exec` 0.146.0:
#     - `--full-auto`            (absent; `-s workspace-write` is the replacement)
#     - `-a/--ask-for-approval`  (top-level only; `codex exec` is always
#                                 non-interactive, so approvals never gate it)
#   `--dangerously-bypass-approvals-and-sandbox` still exists but this adapter
#   prefers `-s danger-full-access`, which is the same sandbox posture without
#   also waiving Codex's hook-trust prompt.
#
#   IMPORTANT — `-p` collision: on `codex exec`, `-p` means `--profile`, NOT
#   "prompt". Loom's runner-neutral convention is `-p "<prompt>"`, so this
#   script CONSUMES `-p`/`--prompt` and re-delivers the value as `codex exec`'s
#   positional PROMPT argument. `-p` is therefore never forwarded to codex. To
#   select a Codex config profile, pass the long form `--profile <name>`, which
#   passes through untouched.
# ---------------------------------------------------------------------------
#
# Live-CLI behaviors this adapter handles (observed on 0.146.0, issue #4468):
#
#   1. Stdin. `codex exec` reads stdin whenever stdin is not a TTY, printing
#      "Reading additional input from stdin..." and appending it as a `<stdin>`
#      block to the prompt. Under Loom dispatch stdin is a pipe nobody writes
#      to, so the child would block forever. This script ALWAYS redirects the
#      child's stdin from /dev/null — the prompt is delivered positionally, so
#      stdin has no job to do.
#   2. Git-repo trust check. `codex exec` refuses to run outside a git repo
#      ("Not inside a trusted directory and --skip-git-repo-check was not
#      specified.", exit 1). Worktree dispatch is fine (a worktree IS a git
#      dir), so `--skip-git-repo-check` is injected ONLY when the cwd is
#      genuinely not inside a work tree — never unconditionally, because the
#      check is a real guardrail for scratch-dir dispatch.
#   3. Stream split. `codex exec` writes ONLY the agent's final message to
#      stdout. Everything else — the banner (`model:`, `sandbox:`,
#      `session id: <uuid>`), the `user`/`codex` message blocks, and
#      `tokens used\n<N>` — goes to STDERR. (The issue's intel described these
#      as stdout; the live CLI disagrees, so this adapter reads them from
#      stderr.) To report the transcript join key without disturbing the
#      caller's stdout, the child's stderr is tee'd through a temp file while
#      stdout passes straight through untouched.
#   4. Transcript. The durable per-session JSONL lives at
#      `$CODEX_HOME/sessions/<YYYY>/<MM>/<DD>/rollout-<ts>-<session-id>.jsonl`.
#      The `session id:` banner line is the join key (contract point 2); this
#      script resolves and logs the concrete path when it can find it.
#
# Contract point 1 (Spawn) interface conformance:
#
#   | Facet                | Codex mapping                                      |
#   |----------------------|----------------------------------------------------|
#   | Args passthrough     | unknown args accumulate and are forwarded verbatim; |
#   |                      | `--` forwards the remainder                        |
#   | Prompt delivery      | `-p`/`--prompt` -> `codex exec "<prompt>"` positional |
#   | Model tier env       | `LOOM_MODEL` -> `-m <v>`; explicit `-m`/`--model` wins |
#   | Effort tier env      | `LOOM_EFFORT` -> `-c model_reasoning_effort=<v>`   |
#   |                      | (codex exec has no `--effort` flag)                |
#   | Missing-credential   | exit **78** (EX_CONFIG) when an EXPLICITLY requested |
#   |                      | Codex profile has no usable `auth.json`            |
#   | Runtime-missing      | exit **127** when `codex` is not on PATH (matches   |
#   |                      | spawn-claude.sh; 78 is reserved for config errors  |
#   |                      | and unknown-runtime dispatch)                      |
#   | Observability        | one `spawn-codex: model=<v>` line, one              |
#   |                      | `spawn-codex: sandbox=<mode> source=<where>` line, |
#   |                      | plus profile/session/transcript/tokens lines       |
#
# Auth (CODEX_HOME profile passthrough — NO token pool in this phase):
#   Precedence, highest first:
#     1. `LOOM_CODEX_HOME` — pins one profile directory explicitly.
#     2. A pre-set `CODEX_HOME` in the environment — honored verbatim.
#     3. `LOOM_CODEX_PROFILE` — a bare account name resolved under the profile
#        root (`LOOM_CODEX_PROFILE_ROOT`, default `~/.loom/codex-profiles`), so
#        `LOOM_CODEX_PROFILE=alice` -> `~/.loom/codex-profiles/alice`.
#     4. Ambient auth — nothing is set, and the Codex CLI resolves its own
#        default `~/.codex` login state (`codex login`).
#   Tiers 1-3 are an ASSIGNMENT, never a copy: the selected directory becomes
#   `CODEX_HOME` and Codex reads `$CODEX_HOME/auth.json` in place. Nothing under
#   `.loom/` ever holds a copy of `auth.json`.
#   An explicitly-requested profile (tiers 1-3) with no usable `auth.json`
#   (regular, non-empty, readable) exits **78** rather than silently degrading
#   to a different account — the contract's missing-credential facet. Tier 4 is
#   not a "request", so ambient auth never fails here; Codex reports its own
#   auth error (a `401 Unauthorized` stream error, classified TOKEN_EXPIRED).
#   Logging discipline: only ever the profile DIRECTORY NAME is logged — never
#   the full path's contents and never a byte of `auth.json`.
#
# Sandbox mapping (SAFETY-CRITICAL — read guardrail-parity-codex.md):
#   Codex's sandbox is the ONLY enforced guard for a Loom-spawned Codex worker
#   (Codex has no hook system, so `guard-destructive.sh` /
#   `guard-worktree-paths.sh` never run). This adapter therefore defaults to the
#   most restrictive mode and requires an explicit signal to widen it.
#   Precedence, highest first:
#     1. An explicit `-s`/`--sandbox` in the passthrough args.
#     2. `LOOM_CODEX_SANDBOX` (read-only|workspace-write|danger-full-access).
#     3. Loom's runner-neutral `--dangerously-skip-permissions` convention ->
#        `workspace-write` (writes confined to the workspace; NOT full access).
#     4. Default: `read-only`.
#   DELIBERATE DIVERGENCE FROM THE FORK: the fork maps skip-permissions to
#   `--dangerously-bypass-approvals-and-sandbox` (no sandbox at all), reasoning
#   that Claude already runs unattended with full tool access. Upstream declines
#   that mapping because Claude's unattended posture is backstopped by PreToolUse
#   guards that Codex cannot run — so "same flag, same posture" would be a
#   strictly weaker trust boundary, not parity. `workspace-write` is the closest
#   honest analogue of `guard-worktree-paths.sh`'s intent. Operators who want the
#   fork's posture opt in explicitly with
#   `LOOM_CODEX_SANDBOX=danger-full-access`.
#   Network: `workspace-write` blocks outbound network by default, so a Codex
#   worker cannot `git push` or call `gh`. `LOOM_CODEX_NETWORK=1` adds
#   `-c sandbox_workspace_write.network_access=true` (workspace-write only).
#
# Usage:
#   .loom/scripts/spawn-codex.sh -p "your prompt"
#   LOOM_RUNTIME=codex .loom/scripts/spawn-worker.sh -p "your prompt"
#   LOOM_CODEX_SANDBOX=workspace-write LOOM_CODEX_NETWORK=1 \
#       .loom/scripts/spawn-codex.sh -p "..." --dangerously-skip-permissions
#   .loom/scripts/spawn-codex.sh                 # interactive (bare `codex`)
#
# Env vars:
#   LOOM_MODEL           Model passed as `codex -m <value>`. Lowest priority: an
#                        explicit `-m`/`--model` in the passthrough args wins.
#                        When neither is set, NO model flag is emitted and the
#                        Codex CLI/profile default is preserved (Phase-2
#                        model-mapping minimalism — logical tier -> model ID
#                        resolution is epic #4167 Phase 4).
#   LOOM_CODEX_MODEL     Static per-adapter default model, used only when
#                        neither an explicit flag nor LOOM_MODEL is present.
#                        Unset by default (no `-m` emitted).
#   LOOM_CODEX_MODEL_CHECK  Set to 0 to disable the Claude-shaped-model refusal
#                        below (issue #5028). Default on (`1`).
#   LOOM_EFFORT          Reasoning effort, mapped to
#                        `-c model_reasoning_effort=<value>`. Skipped when an
#                        explicit `-c model_reasoning_effort=` override is
#                        already present in the passthrough args.
#   LOOM_CODEX_SANDBOX   read-only | workspace-write | danger-full-access.
#                        Overrides the skip-permissions mapping and the default.
#   LOOM_CODEX_NETWORK   When 1 and the effective sandbox is workspace-write,
#                        adds `-c sandbox_workspace_write.network_access=true`.
#   LOOM_CODEX_HOME      Pins one CODEX_HOME profile directory (auth tier 1).
#   CODEX_HOME           Honored verbatim if pre-set (auth tier 2).
#   LOOM_CODEX_PROFILE   Bare profile/account name resolved under the profile
#                        root (auth tier 3).
#   LOOM_CODEX_PROFILE_ROOT  Profile root for LOOM_CODEX_PROFILE. Default
#                        `~/.loom/codex-profiles`.
#   LOOM_SPAWN_NO_EXPORT If set, skip ALL auth resolution (mirrors
#                        spawn-claude.sh) — the caller already prepared the env.
#   LOOM_WORKSPACE       Override repo-root detection (config lookups).
#   LOOM_CODEX_NO_CAPTURE  If set, `exec codex` directly instead of running it
#                        as a child with stderr tee'd. Preserves the pid but
#                        forfeits session-id / transcript / token reporting.
#   LOOM_CODEX_NO_EXEC   Test/CI hook: print the argv this script WOULD run
#                        (prefixed `spawn-codex would-exec:`) and exit 0. Never
#                        touches the real CLI. Does not change production
#                        behavior.
#   LOOM_SWEEP_NICENESS / LOOM_SWEEP_NICE / LOOM_SWEEP_TASKPOLICY_CLASS
#                        Scheduling priority, applied by this runner exactly the
#                        way spawn-claude.sh applies it (issue #4233 — priority
#                        is a per-runner policy, never the dispatcher's).
#   LOOM_ROLE            The acting role (builder/doctor/judge/... or their
#                        development-worker/pr-fixer/sweep-lifecycle aliases),
#                        used ONLY by the managed-hook mutable-role preflight
#                        below. `loom-daemon` sets this for every admitted
#                        dispatch (sweep child or role-runner tick, issue
#                        #4768); an UNSET or unrecognized value is treated as
#                        read-only, NOT fail-closed — see that preflight's
#                        comments for why this is deliberate today.

set -euo pipefail

# --- Logging helpers (match spawn-claude.sh / spawn-worker.sh convention) ---
RED='\033[0;31m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log_info() { echo -e "${BLUE}[$(date -u '+%Y-%m-%dT%H:%M:%SZ')]${NC} $*" >&2; }
log_warn() { echo -e "${YELLOW}[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] WARN${NC} $*" >&2; }
log_error() { echo -e "${RED}[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] ERROR${NC} $*" >&2; }

_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -f "${_SCRIPT_DIR}/lib/locate-daemon-bin.sh" ]]; then
    # shellcheck source=./lib/locate-daemon-bin.sh
    source "${_SCRIPT_DIR}/lib/locate-daemon-bin.sh"
fi

# --- Repo root resolution (handles worktrees; mirrors spawn-claude.sh) ---
_resolve_workspace() {
    if [[ -n "${LOOM_WORKSPACE:-}" ]]; then
        printf '%s\n' "$LOOM_WORKSPACE"
        return
    fi

    local git_common_dir
    if git_common_dir="$(git rev-parse --git-common-dir 2>/dev/null)"; then
        if [[ ! "$git_common_dir" = /* ]]; then
            git_common_dir="$(cd "$git_common_dir" && pwd)"
        fi
        printf '%s\n' "$(dirname "$git_common_dir")"
        return
    fi

    cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd
}

WORKSPACE="$(_resolve_workspace)"

# --- Sweep/role-runner scheduling priority (issue #4233) ---
# Applied HERE, in the runner, not in spawn-worker.sh — the dispatcher only
# `exec`s, which preserves the pid, so a runner-level re-exec still covers every
# process the dispatch would otherwise have spawned, with no double-apply. Same
# `LOOM_SWEEP_NICED` sentinel and same precedence chain spawn-claude.sh uses
# (env > config > default 10); `LOOM_SWEEP_NICE=0` disables the mechanism.
if [[ -z "${LOOM_SWEEP_NICED:-}" && "${LOOM_SWEEP_NICE:-1}" != "0" ]]; then
    _sweep_niceness="${LOOM_SWEEP_NICENESS:-}"
    _sweep_taskpolicy_class="${LOOM_SWEEP_TASKPOLICY_CLASS:-}"
    _sweep_config_lib="${_SCRIPT_DIR}/lib/config-resolver.sh"
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

    if [[ -n "$_sweep_taskpolicy_class" ]] && command -v taskpolicy >/dev/null 2>&1; then
        if taskpolicy -c "$_sweep_taskpolicy_class" -p $$ >/dev/null 2>&1; then
            log_info "spawn-codex: applied taskpolicy -c $_sweep_taskpolicy_class (issue #4233)"
        else
            log_warn "spawn-codex: taskpolicy -c $_sweep_taskpolicy_class failed (non-fatal, continuing at default policy class)"
        fi
    fi

    if [[ "$_sweep_niceness" != "0" ]] && command -v nice >/dev/null 2>&1; then
        export LOOM_SWEEP_NICED=1
        log_info "spawn-codex: re-exec at nice -n $_sweep_niceness (issue #4233; LOOM_SWEEP_NICE=0 to disable)"
        exec nice -n "$_sweep_niceness" "$0" "$@"
    fi
fi

# --- Daemon self-claim marker visibility (mirrors spawn-claude.sh) ---
log_info "spawn-codex: LOOM_SWEEP_CLAIM_OWNED=${LOOM_SWEEP_CLAIM_OWNED:-unset}"

# --- Argument parsing ---
# Buckets:
#   PROMPT            prompt text extracted from -p/--prompt (exec mode)
#   PASSTHROUGH_ARGS  everything else, forwarded to codex verbatim
PROMPT=""
HAS_PROMPT=false
SKIP_PERMISSIONS=false
HAS_MODEL_ARG=false
EXPLICIT_MODEL=""
HAS_SANDBOX_ARG=false
EXPLICIT_SANDBOX=""
HAS_EFFORT_OVERRIDE=false
GENERIC_EFFORT=""
HAS_SKIP_GIT_CHECK_ARG=false
PASSTHROUGH_ARGS=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        -p|--prompt)
            if [[ $# -lt 2 ]]; then
                log_error "$1 requires a value"
                exit 78  # EX_CONFIG
            fi
            PROMPT="$2"
            HAS_PROMPT=true
            shift 2
            ;;
        -p=*)
            PROMPT="${1#-p=}"
            HAS_PROMPT=true
            shift
            ;;
        --prompt=*)
            PROMPT="${1#--prompt=}"
            HAS_PROMPT=true
            shift
            ;;
        --dangerously-skip-permissions)
            # Loom's runner-neutral skip-permissions convention. Consumed here
            # (codex does not understand this Claude-specific flag) and mapped
            # to a Codex sandbox mode below.
            SKIP_PERMISSIONS=true
            shift
            ;;
        --effort)
            if [[ $# -lt 2 ]]; then
                log_error "$1 requires a value"
                exit 78
            fi
            GENERIC_EFFORT="$2"
            shift 2
            ;;
        --effort=*)
            GENERIC_EFFORT="${1#--effort=}"
            shift
            ;;
        --use-wrapper)
            # Generic daemon retry convention. Codex already owns retry
            # behavior; consume the convention instead of forwarding it.
            shift
            ;;
        -m|--model)
            if [[ $# -lt 2 ]]; then
                log_error "$1 requires a value"
                exit 78  # EX_CONFIG
            fi
            HAS_MODEL_ARG=true
            EXPLICIT_MODEL="$2"
            PASSTHROUGH_ARGS+=("$1" "$2")
            shift 2
            ;;
        -m=*)
            HAS_MODEL_ARG=true
            EXPLICIT_MODEL="${1#-m=}"
            PASSTHROUGH_ARGS+=("$1")
            shift
            ;;
        --model=*)
            HAS_MODEL_ARG=true
            EXPLICIT_MODEL="${1#--model=}"
            PASSTHROUGH_ARGS+=("$1")
            shift
            ;;
        -s|--sandbox)
            if [[ $# -lt 2 ]]; then
                log_error "$1 requires a value"
                exit 78  # EX_CONFIG
            fi
            HAS_SANDBOX_ARG=true
            EXPLICIT_SANDBOX="$2"
            PASSTHROUGH_ARGS+=("$1" "$2")
            shift 2
            ;;
        -s=*)
            HAS_SANDBOX_ARG=true
            EXPLICIT_SANDBOX="${1#-s=}"
            PASSTHROUGH_ARGS+=("$1")
            shift
            ;;
        --sandbox=*)
            HAS_SANDBOX_ARG=true
            EXPLICIT_SANDBOX="${1#--sandbox=}"
            PASSTHROUGH_ARGS+=("$1")
            shift
            ;;
        --dangerously-bypass-approvals-and-sandbox)
            # Codex's own no-sandbox flag. Treated as an explicit sandbox
            # decision so this script never ALSO injects `-s <mode>` (which
            # Codex would reject as conflicting).
            HAS_SANDBOX_ARG=true
            EXPLICIT_SANDBOX="danger-full-access"
            PASSTHROUGH_ARGS+=("$1")
            shift
            ;;
        --skip-git-repo-check)
            HAS_SKIP_GIT_CHECK_ARG=true
            PASSTHROUGH_ARGS+=("$1")
            shift
            ;;
        -c|--config)
            PASSTHROUGH_ARGS+=("$1")
            if [[ $# -ge 2 ]]; then
                case "$2" in
                    model_reasoning_effort=*) HAS_EFFORT_OVERRIDE=true ;;
                esac
                PASSTHROUGH_ARGS+=("$2")
                shift 2
            else
                shift
            fi
            ;;
        -c=*|--config=*)
            case "$1" in
                *model_reasoning_effort=*) HAS_EFFORT_OVERRIDE=true ;;
            esac
            PASSTHROUGH_ARGS+=("$1")
            shift
            ;;
        --help|-h)
            # macOS-safe: `sed '$d'` (delete last line) instead of the GNU-only
            # `head -n -1`, matching spawn-worker.sh's help renderer.
            sed -n '2,/^set -euo/p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' \
                | sed '$d'
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

# --- Model selection (mirrors spawn-claude.sh's #3477 precedence) ---
# Precedence: explicit -m/--model > LOOM_MODEL > LOOM_CODEX_MODEL (the adapter's
# static default, unset by default) > nothing (Codex CLI/profile default).
# Exactly one structured `spawn-codex: model=<value>` line per spawn.
#
# Phase-2 minimalism (issue #4468): there is NO logical-tier -> model-ID table
# here. Loom's logical tiers (`opus`, `sonnet`, `fable`) are Claude names; the
# `sweep.modelAliases`/`sweep.tierModels` indirection that would map them onto
# OpenAI model IDs is epic #4167 Phase 4. Until then LOOM_MODEL is forwarded
# verbatim and the adapter's static default is "whatever the profile already
# chose", which is the operator's own selection.
CODEX_DEFAULT_MODEL="${LOOM_CODEX_MODEL:-}"
if [[ "$HAS_MODEL_ARG" == "true" ]]; then
    if [[ -n "${LOOM_MODEL:-}" ]]; then
        log_info "spawn-codex: explicit -m/--model in args wins over LOOM_MODEL='$LOOM_MODEL'"
    fi
    log_info "spawn-codex: model=${EXPLICIT_MODEL:-default} (from -m/--model arg)"
elif [[ -n "${LOOM_MODEL:-}" ]]; then
    PASSTHROUGH_ARGS+=(-m "$LOOM_MODEL")
    log_info "spawn-codex: model=$LOOM_MODEL (from LOOM_MODEL)"
elif [[ -n "$CODEX_DEFAULT_MODEL" ]]; then
    PASSTHROUGH_ARGS+=(-m "$CODEX_DEFAULT_MODEL")
    log_info "spawn-codex: model=$CODEX_DEFAULT_MODEL (from LOOM_CODEX_MODEL adapter default)"
else
    log_info "spawn-codex: model=default"
fi

# --- Claude-shaped model refusal (issue #5028, follow-up to #5001 AC2/AC3) ---
# The daemon-native role runner independently refuses this same conflict
# (`loom-daemon/src/sweep_registry/model.rs::model_runtime_mismatch`) before
# ever shelling out, but any OTHER caller that pins a model onto this runtime
# (sweep dispatch, a hand-run `LOOM_RUNTIME=codex`) reaches this adapter
# directly with no daemon preflight in front of it. Loom's logical Claude
# tiers/aliases (`opus`, `opusplan`, `sonnet`, `haiku`, `fable`) and any
# `claude*`-prefixed pinned ID are never valid on Codex's wire — the CLI 400s
# on them. Catching it here, before any auth/dispatch work, means a
# misconfigured caller fails fast and names the fix instead of burning an
# entire session on a doomed spawn. Escape hatch: LOOM_CODEX_MODEL_CHECK=0
# (e.g. if a future Codex model is genuinely named something like
# "sonnet-mini").
EFFECTIVE_MODEL=""
if [[ "$HAS_MODEL_ARG" == "true" ]]; then
    EFFECTIVE_MODEL="$EXPLICIT_MODEL"
elif [[ -n "${LOOM_MODEL:-}" ]]; then
    EFFECTIVE_MODEL="$LOOM_MODEL"
elif [[ -n "$CODEX_DEFAULT_MODEL" ]]; then
    EFFECTIVE_MODEL="$CODEX_DEFAULT_MODEL"
fi
if [[ -n "$EFFECTIVE_MODEL" && "${LOOM_CODEX_MODEL_CHECK:-1}" != "0" ]]; then
    _model_base="${EFFECTIVE_MODEL%%@*}"
    _model_key="$(printf '%s' "$_model_base" | tr '[:upper:]' '[:lower:]')"
    case "$_model_key" in
        opus | opusplan | sonnet | haiku | fable | claude*)
            log_error "spawn-codex: refusing Claude-shaped model '$EFFECTIVE_MODEL' on the Codex runtime (#5028)."
            log_error "This model/runtime combination is guaranteed to fail on the wire (HTTP 400)."
            log_error "Fix one of:"
            log_error "  - set autonomous.roleRunner.roleModels.<role> to a Codex-valid model in .loom/config.json"
            log_error "  - set LOOM_MODEL / LOOM_CODEX_MODEL to a Codex-valid model for this invocation"
            log_error "  - point this role/runtime binding back at Claude (unset runtimes.roles.<role> / LOOM_RUNTIME_<ROLE>)"
            log_error "Escape hatch: LOOM_CODEX_MODEL_CHECK=0 (only if this really is a valid Codex model name)."
            exit 78 # EX_CONFIG
            ;;
    esac
fi

# --- Effort selection ---
# `codex exec` has no `--effort` flag; the equivalent knob is the
# `model_reasoning_effort` config key. Mirrors the model precedence: an explicit
# `-c model_reasoning_effort=` in the passthrough args wins, then LOOM_EFFORT,
# then nothing (no override emitted, CLI default preserved).
if [[ "$HAS_EFFORT_OVERRIDE" == "true" ]]; then
    if [[ -n "${LOOM_EFFORT:-}" ]]; then
        log_info "spawn-codex: explicit -c model_reasoning_effort= wins over LOOM_EFFORT='$LOOM_EFFORT'"
    fi
elif [[ -n "$GENERIC_EFFORT" ]]; then
    PASSTHROUGH_ARGS+=(-c "model_reasoning_effort=$GENERIC_EFFORT")
    log_info "spawn-codex: effort=$GENERIC_EFFORT (from --effort)"
elif [[ -n "${LOOM_EFFORT:-}" ]]; then
    PASSTHROUGH_ARGS+=(-c "model_reasoning_effort=$LOOM_EFFORT")
    log_info "spawn-codex: effort=$LOOM_EFFORT (from LOOM_EFFORT)"
fi

# --- Sandbox mapping (SAFETY-CRITICAL; see header + guardrail-parity-codex.md) ---
VALID_SANDBOX_MODES="read-only workspace-write danger-full-access"
SANDBOX_MODE=""
SANDBOX_SOURCE=""
if [[ "$HAS_SANDBOX_ARG" == "true" ]]; then
    SANDBOX_MODE="${EXPLICIT_SANDBOX:-unknown}"
    SANDBOX_SOURCE="explicit-arg"
    if [[ -n "${LOOM_CODEX_SANDBOX:-}" ]]; then
        log_info "spawn-codex: explicit sandbox arg wins over LOOM_CODEX_SANDBOX='$LOOM_CODEX_SANDBOX'"
    fi
elif [[ -n "${LOOM_CODEX_SANDBOX:-}" ]]; then
    SANDBOX_MODE="$LOOM_CODEX_SANDBOX"
    SANDBOX_SOURCE="LOOM_CODEX_SANDBOX"
    if [[ " $VALID_SANDBOX_MODES " != *" $SANDBOX_MODE "* ]]; then
        log_error "Invalid LOOM_CODEX_SANDBOX='$SANDBOX_MODE'."
        log_error "Valid modes: $VALID_SANDBOX_MODES."
        exit 78  # EX_CONFIG
    fi
    PASSTHROUGH_ARGS+=(-s "$SANDBOX_MODE")
elif [[ "$SKIP_PERMISSIONS" == "true" ]]; then
    # Loom's skip-permissions convention maps to workspace-write, NOT full
    # access — see the header's "DELIBERATE DIVERGENCE FROM THE FORK".
    SANDBOX_MODE="workspace-write"
    SANDBOX_SOURCE="loom-skip-permissions-convention"
    PASSTHROUGH_ARGS+=(-s "$SANDBOX_MODE")
else
    SANDBOX_MODE="read-only"
    SANDBOX_SOURCE="adapter-default"
    PASSTHROUGH_ARGS+=(-s "$SANDBOX_MODE")
fi
log_info "spawn-codex: sandbox=$SANDBOX_MODE source=$SANDBOX_SOURCE"

# Outbound network inside a workspace-write sandbox is OFF in Codex by default,
# which blocks `git push` / `gh` for a Builder-equivalent worker. Opt in
# explicitly; a no-op (with a warning) under any other sandbox mode.
if [[ "${LOOM_CODEX_NETWORK:-}" == "1" ]]; then
    if [[ "$SANDBOX_MODE" == "workspace-write" ]]; then
        PASSTHROUGH_ARGS+=(-c "sandbox_workspace_write.network_access=true")
        log_info "spawn-codex: network=enabled (workspace-write + LOOM_CODEX_NETWORK=1)"
    else
        log_warn "spawn-codex: LOOM_CODEX_NETWORK=1 has no effect under sandbox=$SANDBOX_MODE (the network_access key is read only for workspace-write)"
    fi
fi

# --- Git-repo trust check (live-CLI behavior 2) ---
# `codex exec` refuses to run outside a git work tree. Worktrees ARE work trees,
# so dispatch into `.loom/worktrees/issue-N` needs nothing. Inject
# `--skip-git-repo-check` ONLY when the cwd is genuinely not inside one, so the
# guardrail keeps applying wherever it can.
if [[ "$HAS_SKIP_GIT_CHECK_ARG" != "true" ]]; then
    if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        log_info "spawn-codex: cwd is inside a git work tree — leaving Codex's trusted-directory check enabled"
    else
        PASSTHROUGH_ARGS+=(--skip-git-repo-check)
        log_warn "spawn-codex: cwd '$PWD' is not inside a git work tree — injecting --skip-git-repo-check (Codex would otherwise refuse to start)"
    fi
fi

# --- Auth: CODEX_HOME profile passthrough (see header) ---
CODEX_PROFILE_NAME=""
if [[ -n "${LOOM_SPAWN_NO_EXPORT:-}" ]]; then
    log_info "spawn-codex: LOOM_SPAWN_NO_EXPORT set — skipping CODEX_HOME resolution"
else
    # A managed headless dispatch with no explicit pin uses the provider-aware
    # selector. This fails closed when every profile is disabled, cooling down,
    # or awaiting reauthentication; it never falls back to ambient ~/.codex.
    if [[ "$HAS_PROMPT" == "true" && -z "${LOOM_CODEX_NO_EXEC:-}" \
          && -z "${LOOM_CODEX_HOME:-}" \
          && -z "${CODEX_HOME:-}" && -z "${LOOM_CODEX_PROFILE:-}" ]]; then
        if ! declare -F loom_locate_daemon_bin >/dev/null 2>&1; then
            log_error "Provider-aware account selection support is not installed."
            exit 78
        fi
        _daemon_bin="$(loom_locate_daemon_bin "$WORKSPACE")"
        if [[ -z "$_daemon_bin" ]] \
            || ! "$_daemon_bin" tokens select --help 2>&1 | grep -q -- '--provider'; then
            log_error "No loom-daemon binary supporting provider-aware account selection was found."
            exit 78
        fi
        _selection_stderr_file="$(mktemp)"
        _selection_output=""
        if ! _selection_output="$("$_daemon_bin" tokens select --provider codex \
            --workspace "$WORKSPACE" --export 2>"$_selection_stderr_file")"; then
            log_error "Codex account selection failed:"
            cat "$_selection_stderr_file" >&2 || true
            rm -f "$_selection_stderr_file"
            exit 78
        fi
        cat "$_selection_stderr_file" >&2 || true
        rm -f "$_selection_stderr_file"
        eval "$_selection_output"
    fi
    _requested_home=""
    _requested_source=""
    if [[ -n "${LOOM_CODEX_HOME:-}" ]]; then
        _requested_home="${LOOM_CODEX_HOME%/}"
        _requested_source="LOOM_CODEX_HOME"
    elif [[ -n "${CODEX_HOME:-}" ]]; then
        _requested_home="${CODEX_HOME%/}"
        _requested_source="CODEX_HOME"
    elif [[ -n "${LOOM_CODEX_PROFILE:-}" ]]; then
        _profile_root="${LOOM_CODEX_PROFILE_ROOT:-$HOME/.loom/codex-profiles}"
        _requested_home="${_profile_root%/}/${LOOM_CODEX_PROFILE}"
        _requested_source="LOOM_CODEX_PROFILE"
    fi

    if [[ -n "$_requested_home" ]]; then
        _auth_candidate="${_requested_home}/auth.json"
        if [[ -f "$_auth_candidate" && -r "$_auth_candidate" && -s "$_auth_candidate" ]]; then
            export CODEX_HOME="$_requested_home"
            CODEX_PROFILE_NAME="${LOOM_ACCOUNT_NAME:-$(basename "$_requested_home")}"
            # Directory NAME only — never the path contents, never auth.json.
            log_info "spawn-codex: using Codex profile '$CODEX_PROFILE_NAME' (source=$_requested_source)"
            echo "# LOOM_ACCOUNT name=$CODEX_PROFILE_NAME" >&2
        else
            log_error "Codex profile requested via $_requested_source has no usable auth.json."
            log_error "Expected a regular, non-empty, readable file at <profile>/auth.json."
            log_error "Provision the profile with:"
            log_error "  CODEX_HOME=$_requested_home codex login"
            log_error "Or unset $_requested_source to fall back to the Codex CLI's own"
            log_error "default login state (~/.codex)."
            exit 78  # EX_CONFIG
        fi
    else
        log_info "spawn-codex: no Codex profile requested — using the Codex CLI's ambient login state (~/.codex)"
    fi
fi

# --- Managed hook readiness / trust preflight (issue #4495) ---
#
# Loom's guard intent is enforced for Codex through a managed `pre_tool_use`
# hook installed into the SELECTED profile's CODEX_HOME (see
# provision-codex-hooks.sh and defaults/hooks/guard-codex-bridge.sh). That hook
# is the only mechanism that gives a Codex worker managed-worktree confinement,
# destructive-command blocking, and Loom workflow interception.
#
# Roles are therefore split by whether they mutate:
#
#   MUTABLE roles (builder, doctor) MUST prove the managed hook is installed at
#   the expected version, pinned, readable, points at THIS workspace's bridge,
#   and that the profile has established Codex hook trust. Any failure exits 78
#   BEFORE the CLI starts. `--dangerously-bypass-hook-trust` is never passed —
#   #4495's scope guards forbid it, and waiving trust would defeat the very
#   boundary this preflight exists to prove.
#
#   READ-ONLY roles keep the existing conservative sandbox fallback, but the
#   audit line states explicitly that hook parity was unavailable. They are
#   never reported as Builder-capable; capability truth lives in
#   defaults/runtimes/codex.json, which stays `partial` until the evidence gate
#   in #4495 is satisfied.
#
# The audit line names the profile DIRECTORY NAME and the readiness verdict
# only — never a profile path's contents and never a byte of auth.json.
LOOM_CODEX_MUTABLE_ROLES="builder doctor"
_hook_role="$(printf '%s' "${LOOM_ROLE:-}" | tr '[:upper:]_' '[:lower:]-')"
case "$_hook_role" in
    development-worker) _hook_role="builder" ;;
    pr-fixer)           _hook_role="doctor" ;;
    # A full `/loom:sweep` dispatch is modelled daemon-side as one
    # "sweep-lifecycle" launch, admitted against Builder's (strongest
    # lifecycle) capability requirements (see loom-daemon's
    # runtime_admission.rs module doc) — it runs the Builder/Doctor phases
    # in-process, so it needs the same mutable-role hook-trust preflight
    # `builder`/`doctor` get. `loom-daemon` sets `LOOM_ROLE=sweep-lifecycle`
    # for every daemon-dispatched sweep child (issue #4768).
    sweep-lifecycle)   _hook_role="builder" ;;
esac

_hook_role_is_mutable=false
if [[ -n "$_hook_role" && " $LOOM_CODEX_MUTABLE_ROLES " == *" $_hook_role "* ]]; then
    _hook_role_is_mutable=true
fi

_hook_provisioner="${_SCRIPT_DIR}/provision-codex-hooks.sh"
_hook_status="unknown"
_hook_reason=""

if [[ ! -x "$_hook_provisioner" && ! -r "$_hook_provisioner" ]]; then
    _hook_status="unavailable"
    _hook_reason="provision-codex-hooks.sh is not installed next to this adapter"
elif [[ -z "${CODEX_HOME:-}" ]]; then
    # Ambient auth (tier 4): Loom never provisions into the operator's own
    # ~/.codex, so there is no managed hook to verify.
    _hook_status="unavailable"
    _hook_reason="ambient Codex login state (no Loom-managed profile selected)"
else
    _hook_verify_out=""
    if _hook_verify_out="$(bash "$_hook_provisioner" verify \
            --codex-home "$CODEX_HOME" --workspace "$WORKSPACE" --json 2>/dev/null)"; then
        _hook_status="ready"
    else
        _hook_status="not-ready"
    fi
    if [[ -n "$_hook_verify_out" ]] && command -v jq >/dev/null 2>&1; then
        _hook_reason="$(printf '%s' "$_hook_verify_out" | jq -r '.reason // empty' 2>/dev/null)" || _hook_reason=""
    fi
fi

log_info "spawn-codex: hooks=$_hook_status role=${_hook_role:-unset} mutable=$_hook_role_is_mutable trust-bypass=never${_hook_reason:+ reason=\"$_hook_reason\"}"

if [[ "$_hook_role_is_mutable" == "true" && "$_hook_status" != "ready" ]]; then
    log_error "Role '$_hook_role' mutates the repository, but Loom's managed Codex pre_tool_use hook is not ready (status=$_hook_status)."
    [[ -n "$_hook_reason" ]] && log_error "  reason: $_hook_reason"
    log_error "Without it a Codex worker runs with NO managed-worktree confinement,"
    log_error "NO destructive-command blocking, and NO Loom workflow interception."
    log_error "Provision and trust the profile, then retry:"
    log_error "  .loom/scripts/provision-codex-hooks.sh install --all-profiles --workspace $WORKSPACE"
    log_error "  CODEX_HOME=<profile> codex     # accept the hook-trust prompt once per profile"
    log_error "  .loom/scripts/provision-codex-hooks.sh verify --all-profiles --workspace $WORKSPACE --json"
    log_error "Loom will not pass --dangerously-bypass-hook-trust (issue #4495)."
    exit 78  # EX_CONFIG
fi

if [[ "$_hook_role_is_mutable" != "true" && "$_hook_status" != "ready" ]]; then
    log_warn "spawn-codex: hook parity unavailable — this session gets ONLY the Codex sandbox (${SANDBOX_MODE}) as a boundary. Read-only roles may proceed; this session is NOT Builder-capable."
fi

# --- Assemble the codex invocation ---
# Non-interactive (prompt present): `codex exec [flags] "<prompt>"`.
# Interactive (no prompt):          `codex [flags]`.
CODEX_ARGS=()
if [[ "$HAS_PROMPT" == "true" ]]; then
    CODEX_ARGS+=(exec)
fi
CODEX_ARGS+=(${PASSTHROUGH_ARGS[@]+"${PASSTHROUGH_ARGS[@]}"})
if [[ "$HAS_PROMPT" == "true" ]]; then
    CODEX_ARGS+=("$PROMPT")
fi

# --- Test/CI hook: surface the resolved argv without touching the real CLI ---
# Checked BEFORE the binary check so the mocked test can assert argv assembly on
# a host with no `codex` installed at all.
if [[ -n "${LOOM_CODEX_NO_EXEC:-}" ]]; then
    echo "# LOOM_CLI_START runtime=codex" >&2
    echo "spawn-codex would-exec: codex ${CODEX_ARGS[*]}"
    exit 0
fi

# --- Binary check ---
# Exit 127 (not 78): 78/EX_CONFIG is reserved for configuration errors,
# including spawn-worker.sh's unknown-runtime dispatch failure. A missing
# runtime binary is the contract's "Runtime-missing" facet, which
# spawn-claude.sh answers with 127.
if ! command -v codex >/dev/null 2>&1; then
    log_error "'codex' command not found in PATH."
    log_error "Install the OpenAI Codex CLI (>= 0.146.0), e.g.:"
    log_error "  npm install -g @openai/codex     # or: brew install codex"
    exit 127
fi

# --- Dispatch ---
# Interactive runs (no prompt) and an explicit LOOM_CODEX_NO_CAPTURE keep a
# plain pid-preserving `exec`. Note stdin is NOT redirected in the interactive
# case — an operator at the keyboard needs it.
if [[ "$HAS_PROMPT" != "true" || -n "${LOOM_CODEX_NO_CAPTURE:-}" ]]; then
    echo "# LOOM_CLI_START runtime=codex" >&2
    exec codex ${CODEX_ARGS[@]+"${CODEX_ARGS[@]}"}
fi

# Headless run. Two live-CLI behaviors force a child (not `exec`) here:
#   - stdin must be /dev/null so `codex exec` does not block reading a pipe
#     nobody writes to (behavior 1);
#   - the `session id:` / `tokens used` lines land on STDERR (behavior 3), so
#     they must be tee'd to be observable while the caller's stdout — the agent's
#     final message, and nothing else — passes through byte-for-byte.
# fd juggling: `2>&1 1>&3` sends codex's STDERR into the pipe (to tee) and its
# STDOUT to fd 3, which the outer `3>&1` has already pointed at this script's
# real stdout. PIPESTATUS[0] recovers codex's own exit code, so the contract's
# exit-code passthrough is preserved despite not using `exec`.
_stderr_file="$(mktemp -t loom-spawn-codex.XXXXXX 2>/dev/null || mktemp)"
# shellcheck disable=SC2064  # expand $_stderr_file now, at trap-install time.
trap "rm -f '$_stderr_file'" EXIT

set +e
echo "# LOOM_CLI_START runtime=codex" >&2
{ codex ${CODEX_ARGS[@]+"${CODEX_ARGS[@]}"} </dev/null 2>&1 1>&3 3>&- \
    | tee "$_stderr_file" >&2; } 3>&1
_exit_code=${PIPESTATUS[0]}
set -e

# --- Session / transcript / cost reporting (contract points 1, 2 and 4) ---
# `session id: <uuid>` is the transcript join key. The durable per-session JSONL
# is `$CODEX_HOME/sessions/<YYYY>/<MM>/<DD>/rollout-<ts>-<session-id>.jsonl`;
# resolve the concrete path when the session dir is reachable, and fall back to
# reporting just the id when it is not (e.g. `--ephemeral`, or ambient auth
# whose CODEX_HOME this script never set).
_session_id="$(
    grep -aoE 'session id:[[:space:]]*[0-9a-fA-F-]{8,}' "$_stderr_file" 2>/dev/null \
    | head -1 | sed -E 's/.*session id:[[:space:]]*//' || true
)"
if [[ -n "$_session_id" ]]; then
    log_info "spawn-codex: session=$_session_id"
    _sessions_root="${CODEX_HOME:-$HOME/.codex}/sessions"
    _transcript=""
    if [[ -d "$_sessions_root" ]]; then
        _transcript="$(
            find "$_sessions_root" -type f -name "*${_session_id}*.jsonl" 2>/dev/null \
            | head -1 || true
        )"
    fi
    if [[ -n "$_transcript" ]]; then
        log_info "spawn-codex: transcript=$_transcript"
    else
        log_info "spawn-codex: transcript=unresolved (session $_session_id; searched $_sessions_root)"
    fi
else
    log_info "spawn-codex: transcript=unresolved (no 'session id:' line in codex output)"
fi

# `tokens used` is followed by the count on the NEXT line. This is an aggregate
# per-session signal, not the per-turn usage the Claude transcript carries — the
# durable JSONL above is the higher-fidelity source once mapped (Phase 4).
_tokens_used="$(
    grep -aA1 '^tokens used' "$_stderr_file" 2>/dev/null \
    | tail -1 | tr -d ' ,' | grep -aE '^[0-9]+$' || true
)"
if [[ -n "$_tokens_used" ]]; then
    log_info "spawn-codex: tokens_used=$_tokens_used"
fi

# Stable, bounded terminal feedback for the daemon. The classifier remains the
# single source of truth; this adapter only packages its result with the
# provider/account attribution already selected for this child. Raw output is
# neither included in this record nor persisted by the health layer.
_classifier_lib="${_SCRIPT_DIR}/lib/classify-error.sh"
if [[ -f "$_classifier_lib" ]]; then
    # shellcheck source=./lib/classify-error.sh
    source "$_classifier_lib"
    _classifier_input="$(tail -c 65536 "$_stderr_file" 2>/dev/null || true)"
    _terminal_category="$(classify_error "$_classifier_input" "$_exit_code" codex)"
    _terminal_account="${LOOM_ACCOUNT_NAME:-${CODEX_PROFILE_NAME:-unknown}}"
    if [[ ! "$_terminal_account" =~ ^[A-Za-z0-9._-]+$ ]]; then
        _terminal_account="unknown"
    fi
    case "$_terminal_category" in
        SUCCESS|TOKEN_EXPIRED|TOKEN_EXHAUSTED|RECOVERABLE|TIMEOUT|FATAL|CWD_DELETED|MODEL_REFUSAL|SESSION_LIMIT)
            printf '# LOOM_TERMINAL_RESULT v=1 provider=codex account=%s category=%s exit_code=%s\n' \
                "$_terminal_account" "$_terminal_category" "$_exit_code" >&2
            ;;
        *)
            log_warn "spawn-codex: classifier returned an invalid category; terminal feedback omitted"
            ;;
    esac
fi

exit "$_exit_code"
