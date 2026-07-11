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

    # 2. Config key override — .loom/config.json → worktree.root.
    #    Same jq guard pattern as worktree.linkPaths (worktree.sh).
    local config_file="$repo_root/.loom/config.json"
    if command -v jq >/dev/null 2>&1 && [[ -f "$config_file" ]]; then
        local cfg_root
        cfg_root=$(jq -r '.worktree.root? // empty' "$config_file" 2>/dev/null)
        if [[ -n "$cfg_root" ]]; then
            if [[ "$cfg_root" == /* ]]; then
                echo "${cfg_root%/}/$(basename "$repo_root")"
                return 0
            fi
            echo "loom_worktree_root: worktree.root in .loom/config.json must be an absolute path (got: '$cfg_root'); falling back to default" >&2
            echo "$repo_root/.loom/worktrees"
            return 0
        fi
    fi

    # 3. Default — unchanged historical behavior.
    echo "$repo_root/.loom/worktrees"
}
