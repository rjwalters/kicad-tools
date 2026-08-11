#!/usr/bin/env bash
# worktree-root.sh — Resolve the base directory that holds Loom worktrees.
#
# Source this file (do not exec). Defines a single function:
#
#   loom_worktree_root <repo_root> -> echoes the absolute worktree base dir
#
# Resolution precedence (first match wins), all opt-in:
#
#   1. LOOM_WORKTREE_ROOT env var          — highest priority
#   2. .loom/config.json → worktree.root   — jq-guarded, same namespace as
#                                            worktree.linkPaths (#3534)
#   3. ${repo_root}/.loom/worktrees        — default, UNCHANGED behavior
#
# When an override (env var or config key) is set, the returned path is
# namespaced by repo basename so multiple workspaces can share one external
# volume without colliding:
#
#     ${override%/}/<repo-basename>
#
# Callers then append `issue-<N>` / `pr-<N>` as before. With neither override
# set, the function returns `${repo_root}/.loom/worktrees` verbatim — the
# result is byte-for-byte identical to the historical hardcoded path, so
# default installations (including the sandboxed macOS app, see ADR-0004) see
# zero behavior change.
#
# Design notes:
#   - The env-var branch imitates other Loom env overrides (e.g.
#     LOOM_WORKTREE_ALWAYS_INCLUDE) and always wins over config.
#   - The config read reuses the exact guard pattern worktree.sh uses for
#     worktree.linkPaths: only attempt jq when it exists AND the config file
#     is present, and fall through softly (missing jq / missing key / malformed
#     JSON → default) so a broken config never breaks worktree creation.
#   - A RELATIVE override is rejected with a stderr warning and the function
#     falls back to the default. An external worktree root must be absolute so
#     that cleanup/GC comparison sites (which resolve absolute paths) match.
#   - Repo namespacing uses `basename "$repo_root"`. Two repos whose basenames
#     collide under the same override root would share a namespace; that is a
#     documented v1 limitation (see the issue), not a bug this helper guards.
#   - This helper never creates directories; callers `mkdir -p` the parent as
#     needed (git worktree add creates only the leaf).
#
# Config resolution (#4062): the `worktree.root` key is read through the
# config-resolver tier chain (loom_config_get) instead of a hand-rolled
# single-tier `.loom/config.json` read, so an override in
# `.loom-project/project.json` / `.loom-local/local.json` is honored too.
# `worktree.root` is a plain string, so a single loom_config_get call is
# correct here (no array/object multi-field re-merge concern — see
# config-resolver.sh's docstring on non-scalar values).
#
# ${BASH_SOURCE[0]:-$0} (not bare ${BASH_SOURCE[0]}) -- the bash+zsh-portable
# self-path idiom from #3680: disk-headroom.sh sources this file, and
# disk-headroom.sh is itself sourced DIRECTLY into the invoking shell by
# sweep.md's Stage -1, which on macOS is often zsh. Under zsh, BASH_SOURCE is
# unset, so a bare ${BASH_SOURCE[0]} resolves to the shell's CWD instead of
# this lib dir and the source below fails (zsh sets $0 to the sourced file's
# own path, which recovers it).
_LOOM_WORKTREE_ROOT_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
# shellcheck source=./config-resolver.sh
source "$_LOOM_WORKTREE_ROOT_LIB_DIR/config-resolver.sh"

# loom_worktree_root <repo_root>
#
# Echoes the absolute worktree base directory. `repo_root` must be an absolute
# path to the main workspace (the parent of the git common dir).
loom_worktree_root() {
    local repo_root="$1"

    # 1. Env var override — highest priority.
    if [[ -n "${LOOM_WORKTREE_ROOT:-}" ]]; then
        if [[ "$LOOM_WORKTREE_ROOT" == /* ]]; then
            echo "${LOOM_WORKTREE_ROOT%/}/$(basename "$repo_root")"
            return 0
        fi
        echo "loom_worktree_root: LOOM_WORKTREE_ROOT must be an absolute path (got: '$LOOM_WORKTREE_ROOT'); falling back to default" >&2
        echo "$repo_root/.loom/worktrees"
        return 0
    fi

    # 2. Config key override — worktree.root, resolved through the config
    #    tier chain (legacy .loom/config.json, .loom-project/project.json,
    #    .loom-local/local.json, private defaults).
    local cfg_root
    cfg_root=$(loom_config_get "$repo_root" "worktree.root" "")
    if [[ -n "$cfg_root" ]]; then
        if [[ "$cfg_root" == /* ]]; then
            echo "${cfg_root%/}/$(basename "$repo_root")"
            return 0
        fi
        echo "loom_worktree_root: worktree.root in the resolved config must be an absolute path (got: '$cfg_root'); falling back to default" >&2
        echo "$repo_root/.loom/worktrees"
        return 0
    fi

    # 3. Default — unchanged historical behavior.
    echo "$repo_root/.loom/worktrees"
}
