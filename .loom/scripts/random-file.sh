#!/usr/bin/env bash
#
# random-file.sh - Get a random file from the workspace
#
# This script provides standalone random file selection for use without the MCP server.
# It respects .gitignore and supports include/exclude patterns.
#
# Usage:
#   ./random-file.sh                                    # Random file from workspace
#   ./random-file.sh --include "src/**/*.ts"            # Only TypeScript files in src/
#   ./random-file.sh --exclude "**/*.test.ts"           # Exclude test files
#   ./random-file.sh --include "src/**/*.ts" --exclude "**/*.test.ts"
#
# Options:
#   --include PATTERN   Glob pattern to include (can be used multiple times)
#   --exclude PATTERN   Glob pattern to exclude (can be used multiple times)
#   --help              Show this help message
#   --debug             Show debug output
#
# Examples:
#   ./random-file.sh --include "src/**/*.ts" --include "src/**/*.tsx"
#   ./random-file.sh --exclude "**/*.test.ts" --exclude "**/*.spec.ts"
#   ./random-file.sh --include "defaults/roles/*.md"
#

set -eo pipefail

# Get script directory and workspace root
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKSPACE_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

# Configuration
DEBUG="${DEBUG:-false}"
INCLUDE_PATTERNS=()
EXCLUDE_PATTERNS=()

# Default exclude patterns (match MCP implementation)
DEFAULT_EXCLUDES=(
    "node_modules"
    ".git"
    "dist"
    "build"
    "target"
    ".loom/worktrees"
    "*.log"
    "package-lock.json"
    "pnpm-lock.yaml"
    "yarn.lock"
    "Cargo.lock"
)

# Parse command line arguments
parse_args() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --include)
                if [[ -z "${2:-}" ]]; then
                    echo "Error: --include requires a pattern argument" >&2
                    exit 1
                fi
                INCLUDE_PATTERNS+=("$2")
                shift 2
                ;;
            --exclude)
                if [[ -z "${2:-}" ]]; then
                    echo "Error: --exclude requires a pattern argument" >&2
                    exit 1
                fi
                EXCLUDE_PATTERNS+=("$2")
                shift 2
                ;;
            --debug)
                DEBUG=true
                shift
                ;;
            --help|-h)
                show_help
                exit 0
                ;;
            *)
                echo "Error: Unknown option: $1" >&2
                show_help >&2
                exit 1
                ;;
        esac
    done
}

show_help() {
    cat << 'EOF'
random-file.sh - Get a random file from the workspace

Usage:
  ./random-file.sh [OPTIONS]

Options:
  --include PATTERN   Glob pattern to include (can be used multiple times)
  --exclude PATTERN   Glob pattern to exclude (can be used multiple times)
  --debug             Show debug output
  --help              Show this help message

Examples:
  ./random-file.sh                                    # Random file from workspace
  ./random-file.sh --include "src/**/*.ts"            # Only TypeScript files in src/
  ./random-file.sh --exclude "**/*.test.ts"           # Exclude test files
  ./random-file.sh --include "src/**/*.ts" --exclude "**/*.test.ts"

Default exclusions:
  - node_modules/, .git/, dist/, build/, target/
  - .loom/worktrees/
  - *.log, package-lock.json, pnpm-lock.yaml, yarn.lock, Cargo.lock
  - Files matching .gitignore patterns

The script always respects .gitignore if present in the workspace root.
EOF
}

debug() {
    if [[ "$DEBUG" == "true" ]]; then
        echo "[DEBUG] $*" >&2
    fi
}

# Get list of files matching criteria
get_matching_files() {
    cd "$WORKSPACE_ROOT"

    # Build the find command
    local include_args=""
    if [[ ${#INCLUDE_PATTERNS[@]} -gt 0 ]]; then
        # Build include patterns for find
        for pattern in "${INCLUDE_PATTERNS[@]}"; do
            # Convert glob to find pattern
            if [[ "$pattern" == *"**"* ]]; then
                # Pattern with ** - use path matching
                local converted="${pattern//\*\*/\*}"
                include_args+=" -path './$converted' -o"
            else
                include_args+=" -path './$pattern' -o"
            fi
        done
        include_args="${include_args% -o}"
    fi

    debug "Include args: $include_args"

    # Use fd if available (faster), otherwise fall back to find + grep
    if command -v fd &>/dev/null; then
        get_files_with_fd
    else
        get_files_with_find
    fi
}

# Use fd for fast file finding (if available)
get_files_with_fd() {
    # Deliberately no --no-ignore-vcs: fd's native gitignore handling (which
    # correctly supports top-level directory patterns and `!`-negation) is
    # left enabled, rather than disabled-then-reimplemented by a hand-rolled
    # parser (#6537).
    local fd_args=("--type" "f" "--hidden")

    # Add include patterns
    if [[ ${#INCLUDE_PATTERNS[@]} -gt 0 ]]; then
        # For fd, we need to use -e for extensions or -g for globs
        for pattern in "${INCLUDE_PATTERNS[@]}"; do
            fd_args+=("-g" "$pattern")
        done
    fi

    # Add exclude patterns
    for pattern in "${DEFAULT_EXCLUDES[@]}"; do
        fd_args+=("-E" "$pattern")
    done

    for pattern in "${EXCLUDE_PATTERNS[@]}"; do
        fd_args+=("-E" "$pattern")
    done

    debug "Running: fd ${fd_args[*]}"

    # fd already respects .gitignore natively (see fd_args above), so no
    # further gitignore filtering is needed here.
    local files
    files=$(fd "${fd_args[@]}" . 2>/dev/null)
    echo "$files"
}

# Use find as fallback
get_files_with_find() {
    local files=""

    # If we have include patterns, search for those specifically
    if [[ ${#INCLUDE_PATTERNS[@]} -gt 0 ]]; then
        for pattern in "${INCLUDE_PATTERNS[@]}"; do
            # Use bash globbing for patterns
            local found
            found=$(find_with_glob "$pattern")
            if [[ -n "$found" ]]; then
                files+="$found"$'\n'
            fi
        done

        # Bash globbing does not consult .gitignore at all, so it must still
        # be filtered explicitly (via git's own machinery, see
        # filter_by_gitignore below — not a hand-rolled parser).
        files=$(echo "$files" | apply_exclusions | filter_by_gitignore)
    else
        # No include patterns: let git enumerate tracked + untracked files,
        # which respects .gitignore (including top-level directory patterns
        # and `!`-negation) correctly and natively (#6537). Falls back to a
        # plain `find` only when WORKSPACE_ROOT is not inside a git repo.
        if command -v git &>/dev/null && git -C "$WORKSPACE_ROOT" rev-parse --is-inside-work-tree &>/dev/null; then
            files=$(git -C "$WORKSPACE_ROOT" ls-files --cached --others --exclude-standard)
        else
            files=$(find . -type f 2>/dev/null | sed 's|^\./||')
        fi

        # git ls-files --exclude-standard already applied .gitignore; only the
        # script's own DEFAULT_EXCLUDES / --exclude patterns remain.
        files=$(echo "$files" | apply_exclusions)
    fi

    echo "$files"
}

# Find files matching a glob pattern
find_with_glob() {
    local pattern="$1"

    shopt -s nullglob 2>/dev/null || true

    local matches=()
    # shellcheck disable=SC2086
    if [[ "$pattern" == *"**"* ]]; then
        # `**` needs globstar, which is bash 4.0+. On stock macOS bash 3.2
        # `shopt -s globstar` FAILS and `**` then behaves as a single-level
        # `*` -- so the glob silently returns the wrong file set rather than
        # erroring (#7802 Class 1). Verified: on a tree with 4 matching files
        # at depths 0-3, the 3.2 glob returned 1.
        #
        # `find` recurses identically on both interpreters, so use it for the
        # recursive shape and keep the plain glob for everything else.
        local _gs_root="${pattern%%/\*\**}"   # text before the first /**
        local _gs_name="${pattern##*/}"         # trailing component, e.g. *.rs
        [[ "$_gs_root" == "$pattern" ]] && _gs_root="."
        [[ -z "$_gs_root" ]] && _gs_root="."
        while IFS= read -r _gs_hit; do
            [[ -n "$_gs_hit" ]] && matches+=("$_gs_hit")
        done < <(find "$_gs_root" -type f -name "$_gs_name" 2>/dev/null || true)
    else
        eval "matches=($pattern)" 2>/dev/null || true
    fi

    # Print matches that are files
    for match in "${matches[@]}"; do
        if [[ -f "$match" ]]; then
            echo "${match#./}"
        fi
    done
}

# Apply exclusion patterns
apply_exclusions() {
    local input
    input=$(cat)

    # Build grep exclusion pattern
    local exclude_regex=""

    for pattern in "${DEFAULT_EXCLUDES[@]}"; do
        # Handle different pattern types
        if [[ "$pattern" == *.* ]]; then
            # File extension or specific file
            local escaped
            escaped=$(printf '%s' "$pattern" | sed 's/[.[\*^$()+?{|]/\\&/g')
            escaped="${escaped//\\\*/.*}"  # Convert \* back to .*
            exclude_regex+="|$escaped$"
        else
            # Directory name
            exclude_regex+="|/$pattern/|^$pattern/"
        fi
    done

    for pattern in "${EXCLUDE_PATTERNS[@]}"; do
        # Convert glob to regex
        local regex
        regex=$(glob_to_regex "$pattern")
        exclude_regex+="|$regex"
    done

    # Remove leading |
    exclude_regex="${exclude_regex#|}"

    if [[ -n "$exclude_regex" ]]; then
        debug "Exclude regex: $exclude_regex"
        echo "$input" | grep -v -E "$exclude_regex" || true
    else
        echo "$input"
    fi
}

# Convert glob pattern to regex
glob_to_regex() {
    local pattern="$1"
    # Escape special regex characters except * and ?
    local regex
    regex=$(printf '%s' "$pattern" | sed 's/[.[\^$()+{|]/\\&/g')
    # Convert glob wildcards to regex
    regex="${regex//\*\*/.*}"      # ** -> .* (any path)
    regex="${regex//\*/[^/]*}"     # * -> [^/]* (any chars except /)
    regex="${regex//\?/.}"         # ? -> . (any single char)
    echo "$regex"
}

# Filter a newline-delimited file list by .gitignore, using git's own
# gitignore engine (`git check-ignore`) rather than a hand-rolled
# glob-to-regex parser. This correctly handles top-level directory patterns
# (e.g. `build/`) and `!`-negation re-inclusion lines, which the previous
# regex-based implementation mishandled (#6537).
#
# Only needed for file lists gathered via bash globbing (see
# get_files_with_find's --include path), which does not consult .gitignore at
# all. The fd path and the git-ls-files path already apply .gitignore
# natively upstream of this function.
filter_by_gitignore() {
    local input
    input=$(cat)

    [[ -z "$input" ]] && return 0

    if ! command -v git &>/dev/null || ! git -C "$WORKSPACE_ROOT" rev-parse --is-inside-work-tree &>/dev/null; then
        echo "$input"
        return
    fi

    # `git check-ignore --stdin` prints the subset of input paths that are
    # ignored (silently dropping the rest), so a plain diff against the
    # original list yields exactly the non-ignored files.
    local ignored
    ignored=$(printf '%s\n' "$input" | git -C "$WORKSPACE_ROOT" check-ignore --stdin 2>/dev/null || true)

    if [[ -z "$ignored" ]]; then
        echo "$input"
        return
    fi

    debug "Gitignore-excluded (git check-ignore): $(printf '%s' "$ignored" | tr '\n' ' ')"
    printf '%s\n' "$input" | grep -v -F -x -f <(printf '%s\n' "$ignored") || true
}

# Pick a random file from the list
pick_random() {
    local files=()
    while IFS= read -r line; do
        [[ -n "$line" ]] && files+=("$line")
    done

    if [[ ${#files[@]} -eq 0 ]]; then
        echo "No files found matching the criteria" >&2
        exit 1
    fi

    debug "Found ${#files[@]} matching files"

    # Pick random index
    local index=$((RANDOM % ${#files[@]}))
    local selected="${files[$index]}"

    # Return absolute path
    echo "$WORKSPACE_ROOT/$selected"
}

# Main
main() {
    parse_args "$@"

    debug "Workspace: $WORKSPACE_ROOT"
    debug "Include patterns: ${INCLUDE_PATTERNS[*]:-<all>}"
    debug "Exclude patterns: ${EXCLUDE_PATTERNS[*]:-<none>}"

    get_matching_files | pick_random
}

main "$@"
