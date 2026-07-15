#!/usr/bin/env bash
# default-branch.sh — Resolve a repository's default branch name.
#
# Source this file (do not exec). Defines a single function:
#
#   loom_default_branch [remote] -> echoes the default branch name (e.g. "main"
#                                   or "master"); returns non-zero on failure.
#
# Motivation (#3549): the worktree helpers historically hardcoded the base
# branch as the literal string `main` / `origin/main`. On a repo whose default
# branch is `master` (or any non-`main` name) every git call that references
# `origin/main` fails with `fatal: invalid reference: origin/main`, aborting
# worktree creation. This helper centralizes offline-first default-branch
# detection so both worktree.sh and pr-worktree.sh work regardless of the
# repo's default branch name.
#
# Detection order (first match wins) — offline-first, HARD-FAIL:
#
#   1. LOOM_DEFAULT_BRANCH env var         — explicit escape hatch / test seam.
#   2. git symbolic-ref --short            — the offline, no-network source of
#      refs/remotes/<remote>/HEAD            truth. Present in normal clones;
#                                            `git remote set-head <remote> -a`
#                                            populates it.
#   3. git ls-remote --symref <remote> HEAD — network fallback when origin/HEAD
#                                            is unset locally (fresh clones
#                                            sometimes lack it).
#   4. Local probe                          — check refs/remotes/<remote>/main
#                                            then refs/remotes/<remote>/master,
#                                            pick whichever exists.
#   5. Hard error (return 1 + remediation)  — NEVER silently default to `main`.
#      A silent wrong default reintroduces the exact class of bug this helper
#      exists to fix (an empty or wrong branch on a master-default repo).
#
# Design notes:
#   - `git symbolic-ref` is preferred over `gh repo view --json defaultBranchRef`
#     because Loom is forge-agnostic (it supports Gitea too), needs no network,
#     and needs no `gh` auth. A forge-API tier could be added later but must not
#     be the primary detector.
#   - The helper runs git against the current working directory. Callers that
#     need a specific repo context should `cd` there (or run git -C) before
#     calling, or resolve the branch once at the top of the script while cwd is
#     the main workspace.
#   - No side effects at source time; pure function, mirrors lib/worktree-root.sh.

# loom_default_branch [remote]
#
# Echoes the resolved default branch name on stdout. Returns 0 on success,
# 1 when the branch cannot be determined (with a remediation hint on stderr).
loom_default_branch() {
    local remote="${1:-origin}"

    # 1. Env var override — highest priority (escape hatch + test seam).
    if [[ -n "${LOOM_DEFAULT_BRANCH:-}" ]]; then
        echo "$LOOM_DEFAULT_BRANCH"
        return 0
    fi

    # 2. Local symbolic ref for the remote's HEAD — offline, no network.
    #    Returns e.g. "origin/main"; strip the "<remote>/" prefix.
    local sref
    sref=$(git symbolic-ref --short "refs/remotes/$remote/HEAD" 2>/dev/null || true)
    if [[ -n "$sref" ]]; then
        echo "${sref#"$remote"/}"
        return 0
    fi

    # 3. Network fallback: ask the remote for its HEAD symref.
    #    Output line looks like: "ref: refs/heads/main\tHEAD"; strip the prefix.
    local lsref
    lsref=$(git ls-remote --symref "$remote" HEAD 2>/dev/null \
        | awk '/^ref:/ { print $2; exit }' || true)
    if [[ -n "$lsref" ]]; then
        echo "${lsref#refs/heads/}"
        return 0
    fi

    # 4. Local probe: prefer main, then master, whichever ref exists.
    local candidate
    for candidate in main master; do
        if git show-ref --verify --quiet "refs/remotes/$remote/$candidate" 2>/dev/null; then
            echo "$candidate"
            return 0
        fi
    done

    # 5. Hard fail — do NOT default to main.
    echo "loom_default_branch: could not determine the default branch for remote '$remote'." >&2
    echo "  Fix: run 'git remote set-head $remote -a' to populate refs/remotes/$remote/HEAD," >&2
    echo "  or set LOOM_DEFAULT_BRANCH to the branch name explicitly." >&2
    return 1
}
