#!/usr/bin/env bash
# blame-issue.sh - Which issue/PR produced this line? (#4338, codecast borrow item 1)
#
# codecast's `cast blame` answers "which agent session wrote this line" by
# joining git blame against transcript session records -- a database Loom does
# not have. Loom does not need one: every Builder/Doctor commit is created in a
# labeled issue's worktree, its PR is merged (merge commit, the Loom default
# since #9105; squash-merged repos use a `(#PR)` subject suffix instead), and
# the PR body carries `Closes #N`. That is already an in-band, durable join key
# from a line of code back to the issue that produced it. This script is a
# small READ-ONLY reporting wrapper around `git blame` / `git log -S` that
# walks that chain for a human.
#
# What it does, per hunk (a contiguous run of lines attributed to one commit):
#   1. `git blame --porcelain` to find the commit that last touched each line.
#   2. Resolve that commit to a PR number -- first offline, by matching the
#      merge-commit subject ("Merge[d]? pull request #1234 from ...", the Loom
#      default since #9105) or a squash subject's trailing "(#1234)" suffix;
#      falls back to the GitHub REST "commit -> associated pulls" endpoint for
#      any commit carrying neither (direct-to-main commits, non-PR merges).
#   3. Resolve the PR to its closing issue number(s) via
#      `closingIssuesReferences` (GitHub's own computed field), falling back to
#      regexing `Closes/Fixes/Resolves/Part of #N` out of the PR body.
#   4. Best-effort role: if `loom:changes-requested` was EVER applied to the PR
#      (per its label timeline), the PR went through a Doctor cycle at least
#      once -- reported as `builder+doctor` (mixed; attribution is at the PR
#      level by design, never per commit). Otherwise
#      `builder`. `unknown` if the PR can't be resolved.
#
# Read-only: only `git blame`/`git log` (local) and `gh api`/`gh pr view` GET
# calls. No forge writes, no new state on disk, no daemon involvement.
#
# Usage:
#   blame-issue.sh <path>                       # whole-file blame, one row per hunk
#   blame-issue.sh <path> -L START,END           # restrict to a line range (git-blame -L syntax)
#   blame-issue.sh --pattern STRING <path>       # `git log --follow -S<pattern>` mode instead of blame
#   blame-issue.sh --no-role <path>              # skip the role lookup (fewer gh calls, faster)
#   blame-issue.sh --format json <path>          # newline-delimited JSON instead of the table
#   blame-issue.sh --help
#
# Output (table format, tab-separated, greppable):
#   PATH  LINES  COMMIT  PR  ISSUES  ROLE  SUBJECT
#
# Exit codes:
#   0 - completed (rows printed; a row with unresolved PR/issue/role is not a
#       failure -- best-effort fields are marked "-"/"unknown")
#   2 - usage error (bad args, path not found, not in a git repo)

set -euo pipefail

SCRIPT_NAME="$(basename "$0")"

usage() {
    sed -n '2,42p' "$0" | sed 's/^# \{0,1\}//'
}

# ---- Argument parsing ------------------------------------------------------
PATTERN=""
LINE_RANGE=""
DO_ROLE=1
FORMAT="table"
TARGET_PATH=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        -h|--help)
            usage
            exit 0
            ;;
        --pattern)
            [[ -n "${2:-}" ]] || { echo "$SCRIPT_NAME: --pattern requires an argument" >&2; exit 2; }
            PATTERN="$2"
            shift 2
            ;;
        -L)
            [[ -n "${2:-}" ]] || { echo "$SCRIPT_NAME: -L requires an argument (START,END)" >&2; exit 2; }
            LINE_RANGE="$2"
            shift 2
            ;;
        --no-role)
            DO_ROLE=0
            shift
            ;;
        --format)
            [[ -n "${2:-}" ]] || { echo "$SCRIPT_NAME: --format requires an argument (table|json)" >&2; exit 2; }
            FORMAT="$2"
            shift 2
            ;;
        -*)
            echo "$SCRIPT_NAME: unknown option: $1" >&2
            echo "Run with --help for usage." >&2
            exit 2
            ;;
        *)
            if [[ -n "$TARGET_PATH" ]]; then
                echo "$SCRIPT_NAME: unexpected extra argument: $1" >&2
                exit 2
            fi
            TARGET_PATH="$1"
            shift
            ;;
    esac
done

if [[ -z "$TARGET_PATH" ]]; then
    echo "$SCRIPT_NAME: missing <path>" >&2
    echo "Run with --help for usage." >&2
    exit 2
fi

if [[ "$FORMAT" != "table" && "$FORMAT" != "json" ]]; then
    echo "$SCRIPT_NAME: --format must be 'table' or 'json' (got '$FORMAT')" >&2
    exit 2
fi

# ---- Resolve repo root + gh availability -----------------------------------
REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" || {
    echo "$SCRIPT_NAME: not inside a git repository" >&2
    exit 2
}

HAVE_GH=0
if command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1; then
    HAVE_GH=1
else
    echo "$SCRIPT_NAME: WARNING: gh CLI not available/authenticated -- PR/issue/role columns will be '-'/'unknown'" >&2
fi

# git blame/log need a path relative to the repo (or CWD); resolve to a path
# git accepts, working whether invoked from repo root or a subdirectory, and
# whether the caller passed a path through a per-file symlink (e.g. the
# installed .loom/scripts/<name>.sh -> defaults/scripts/<name>.sh layout) --
# git blame refuses a path outside the repo, so resolve through the symlink to
# the tracked file's real location before handing it to git.
if [[ ! -e "$TARGET_PATH" ]]; then
    echo "$SCRIPT_NAME: path not found: $TARGET_PATH" >&2
    exit 2
fi
REAL_PATH="$(cd "$(dirname "$TARGET_PATH")" 2>/dev/null && pwd -P)/$(basename "$TARGET_PATH")"
GIT_PATH="${REAL_PATH#"$REPO_ROOT"/}"
if [[ "$GIT_PATH" == "$REAL_PATH" ]]; then
    echo "$SCRIPT_NAME: path is not inside repo $REPO_ROOT: $TARGET_PATH" >&2
    exit 2
fi

# ---- Small caches (plain arrays -- macOS ships bash 3.2, no associative arrays) ---
CACHE_SHA=()
CACHE_PR=()
CACHE_ISSUES=()
CACHE_ROLE=()

cache_lookup() {
    # cache_lookup <sha> -> sets CACHED_PR/CACHED_ISSUES/CACHED_ROLE, returns 0 if found
    local sha="$1" i
    for i in "${!CACHE_SHA[@]}"; do
        if [[ "${CACHE_SHA[$i]}" == "$sha" ]]; then
            CACHED_PR="${CACHE_PR[$i]}"
            CACHED_ISSUES="${CACHE_ISSUES[$i]}"
            CACHED_ROLE="${CACHE_ROLE[$i]}"
            return 0
        fi
    done
    return 1
}

cache_store() {
    local sha="$1" pr="$2" issues="$3" role="$4"
    CACHE_SHA+=("$sha")
    CACHE_PR+=("$pr")
    CACHE_ISSUES+=("$issues")
    CACHE_ROLE+=("$role")
}

# resolve_pr_from_subject <subject> -> PR number on stdout, or empty
resolve_pr_from_subject() {
    local subject="$1"
    # Merge-commit subjects: "Merge[d] pull request #N from ..." (GitHub says
    # "Merge", Gitea says "Merged"); squash subjects end in "(#N)". Number is
    # capture group 1 in both patterns. Both alternatives are anchored -- the
    # merge form at `^` so a subject that merely quotes one (`Revert "Merge
    # pull request #1234 from ..."`) falls through to the /pulls endpoint
    # instead of resolving to the quoted PR, mirroring the `$` anchor the
    # squash form already carries.
    if [[ "$subject" =~ ^Merged?\ pull\ request\ #([0-9]+)\ from || "$subject" =~ \(#([0-9]+)\)[[:space:]]*$ ]]; then
        printf '%s' "${BASH_REMATCH[1]}"
    fi
}

# resolve_pr_from_gh <sha> -> PR number on stdout, or empty. Fallback for
# commits whose subject has no trailing "(#N)" (direct-to-main, non-squash).
resolve_pr_from_gh() {
    local sha="$1"
    [[ "$HAVE_GH" == 1 ]] || return 0
    gh api "repos/{owner}/{repo}/commits/$sha/pulls" --jq '.[0].number // empty' 2>/dev/null || true
}

# resolve_issues <pr> -> comma-separated issue numbers on stdout (may be empty)
resolve_issues() {
    local pr="$1" issues=""
    [[ "$HAVE_GH" == 1 && -n "$pr" ]] || { printf ''; return; }
    issues="$(gh pr view "$pr" --json closingIssuesReferences \
        -q '[.closingIssuesReferences[].number] | join(",")' 2>/dev/null || true)"
    if [[ -z "$issues" ]]; then
        # Fallback: regex the PR body for Closes/Fixes/Resolves/Part of #N.
        local body
        body="$(gh pr view "$pr" --json body -q '.body // ""' 2>/dev/null || true)"
        issues="$(printf '%s' "$body" \
            | grep -Eio '(clos(e|es|ed)|fix(es|ed)?|resolv(e|es|ed)|part of)[[:space:]]+#[0-9]+' \
            | grep -Eo '[0-9]+' \
            | awk '!seen[$0]++' \
            | paste -sd, - 2>/dev/null || true)"
    fi
    printf '%s' "$issues"
}

# resolve_role <pr> -> "builder" | "builder+doctor" | "unknown"
resolve_role() {
    local pr="$1"
    [[ "$DO_ROLE" == 1 && "$HAVE_GH" == 1 && -n "$pr" ]] || { printf 'unknown'; return; }
    local labels
    labels="$(gh api "repos/{owner}/{repo}/issues/$pr/timeline" --paginate \
        -q '.[] | select(.event=="labeled") | .label.name' 2>/dev/null || true)"
    if [[ -z "$labels" ]]; then
        printf 'unknown'
    elif printf '%s\n' "$labels" | grep -qx 'loom:changes-requested'; then
        printf 'builder+doctor'
    else
        printf 'builder'
    fi
}

# resolve_commit <sha> -> populates RESOLVED_PR / RESOLVED_ISSUES / RESOLVED_ROLE
# / RESOLVED_SUBJECT, using the cache when possible.
resolve_commit() {
    local sha="$1" subject pr issues role
    subject="$(git -C "$REPO_ROOT" log -1 --format=%s "$sha" 2>/dev/null || echo "")"
    RESOLVED_SUBJECT="$subject"

    if cache_lookup "$sha"; then
        RESOLVED_PR="$CACHED_PR"
        RESOLVED_ISSUES="$CACHED_ISSUES"
        RESOLVED_ROLE="$CACHED_ROLE"
        return
    fi

    pr="$(resolve_pr_from_subject "$subject")"
    if [[ -z "$pr" ]]; then
        pr="$(resolve_pr_from_gh "$sha")"
    fi

    issues=""
    role="unknown"
    if [[ -n "$pr" ]]; then
        issues="$(resolve_issues "$pr")"
        role="$(resolve_role "$pr")"
    fi

    RESOLVED_PR="${pr:--}"
    RESOLVED_ISSUES="${issues:--}"
    RESOLVED_ROLE="$role"
    cache_store "$sha" "$RESOLVED_PR" "$RESOLVED_ISSUES" "$RESOLVED_ROLE"
}

emit_row() {
    local lines="$1" sha="$2" pr="$3" issues="$4" role="$5" subject="$6"
    local short_sha="${sha:0:9}"
    if [[ "$FORMAT" == "json" ]]; then
        # Minimal hand-rolled JSON (no jq dependency for output); subject is
        # escaped for backslashes/quotes only, adequate for commit subjects.
        local esc_subject="${subject//\\/\\\\}"
        esc_subject="${esc_subject//\"/\\\"}"
        printf '{"path":"%s","lines":"%s","commit":"%s","pr":"%s","issues":"%s","role":"%s","subject":"%s"}\n' \
            "$GIT_PATH" "$lines" "$sha" "$pr" "$issues" "$role" "$esc_subject"
    else
        printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
            "$GIT_PATH" "$lines" "$short_sha" "$pr" "$issues" "$role" "$subject"
    fi
}

if [[ "$FORMAT" == "table" ]]; then
    printf 'PATH\tLINES\tCOMMIT\tPR\tISSUES\tROLE\tSUBJECT\n'
fi

if [[ -n "$PATTERN" ]]; then
    # ---- Pattern mode: git log --follow -S<pattern> ------------------------
    while IFS= read -r sha; do
        [[ -n "$sha" ]] || continue
        resolve_commit "$sha"
        emit_row "-" "$sha" "$RESOLVED_PR" "$RESOLVED_ISSUES" "$RESOLVED_ROLE" "$RESOLVED_SUBJECT"
    done < <(git -C "$REPO_ROOT" log --follow --format=%H -S"$PATTERN" -- "$GIT_PATH" 2>/dev/null)
else
    # ---- Blame mode: one row per contiguous hunk ---------------------------
    blame_args=(--porcelain -e)
    if [[ -n "$LINE_RANGE" ]]; then
        blame_args+=(-L "$LINE_RANGE")
    fi
    while IFS= read -r header; do
        # Only lines with exactly 4 fields (sha, orig-line, final-line, group-size)
        # mark the START of a new hunk in porcelain output; that group-size is
        # the count of consecutive lines this commit owns starting at final-line.
        sha="${header%% *}"
        rest="${header#* }"
        final_line="${rest#* }"
        final_line="${final_line%% *}"
        group_size="${rest##* }"
        end_line=$(( final_line + group_size - 1 ))
        line_range="$final_line"
        [[ "$group_size" -gt 1 ]] && line_range="${final_line}-${end_line}"

        resolve_commit "$sha"
        emit_row "$line_range" "$sha" "$RESOLVED_PR" "$RESOLVED_ISSUES" "$RESOLVED_ROLE" "$RESOLVED_SUBJECT"
    done < <(git -C "$REPO_ROOT" blame "${blame_args[@]}" -- "$GIT_PATH" 2>/dev/null \
        | grep -E '^[0-9a-f]{40} [0-9]+ [0-9]+ [0-9]+$')
fi
