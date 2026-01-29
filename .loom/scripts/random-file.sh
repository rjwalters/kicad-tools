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

# Build the find command exclude patterns
build_exclude_args() {
    local args=""

    # Add default excludes
    for pattern in "${DEFAULT_EXCLUDES[@]}"; do
        # Handle directory patterns
        if [[ "$pattern" != *.* ]]; then
            args+=" -path '*/$pattern/*' -o -path '*/$pattern' -o"
        else
            # Handle file patterns
            args+=" -name '$pattern' -o"
        fi
    done

    # Add user-specified excludes
    for pattern in "${EXCLUDE_PATTERNS[@]}"; do
        # Convert glob pattern to find pattern
        # **/ at start means anywhere in path
        local find_pattern
        find_pattern=$(convert_glob_to_find "$pattern")
        args+=" $find_pattern -o"
    done

    # Remove trailing " -o" and wrap in parentheses
    args="${args% -o}"
    echo "$args"
}

# Convert a glob pattern to a find -path/-name pattern
convert_glob_to_find() {
    local pattern="$1"

    # Handle **/*.ext patterns (anywhere with extension)
    if [[ "$pattern" == "**/"* ]]; then
        local rest="${pattern#**/}"
        if [[ "$rest" == *"*"* ]]; then
            # It's a wildcard pattern like **/*.test.ts
            echo "-path '*/$rest' -o -name '${rest#*/}'"
        else
            # It's a specific name like **/foo.ts
            echo "-name '$rest'"
        fi
    elif [[ "$pattern" == *"**"* ]]; then
        # Pattern contains ** somewhere
        echo "-path '*${pattern//\*\*/\*}'"
    else
        # Simple pattern
        echo "-path '*/$pattern'"
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
    local fd_args=("--type" "f" "--hidden" "--no-ignore-vcs")

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

    # Get files and filter by gitignore
    local files
    files=$(fd "${fd_args[@]}" . 2>/dev/null | filter_by_gitignore)
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
    else
        # Find all files
        files=$(find . -type f 2>/dev/null | sed 's|^\./||')
    fi

    # Apply exclusions
    files=$(echo "$files" | apply_exclusions | filter_by_gitignore)
    echo "$files"
}

# Find files matching a glob pattern
find_with_glob() {
    local pattern="$1"

    # Enable extended globbing
    shopt -s globstar nullglob 2>/dev/null || true

    # Try to match the pattern
    local matches=()
    # shellcheck disable=SC2086
    if [[ "$pattern" == *"**"* ]]; then
        # Pattern uses ** for recursive matching
        eval "matches=($pattern)" 2>/dev/null || true
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

# Filter files by .gitignore
filter_by_gitignore() {
    local input
    input=$(cat)

    local gitignore="$WORKSPACE_ROOT/.gitignore"

    if [[ ! -f "$gitignore" ]]; then
        echo "$input"
        return
    fi

    # Read gitignore patterns (skip comments and empty lines)
    local patterns=()
    while IFS= read -r line || [[ -n "$line" ]]; do
        # Skip empty lines and comments
        [[ -z "$line" || "$line" == \#* ]] && continue
        # Trim whitespace
        line="${line#"${line%%[![:space:]]*}"}"
        line="${line%"${line##*[![:space:]]}"}"
        [[ -n "$line" ]] && patterns+=("$line")
    done < "$gitignore"

    if [[ ${#patterns[@]} -eq 0 ]]; then
        echo "$input"
        return
    fi

    # Build regex from gitignore patterns
    local exclude_regex=""
    for pattern in "${patterns[@]}"; do
        # Handle negation patterns (!)
        [[ "$pattern" == !* ]] && continue

        # Convert gitignore pattern to regex
        local regex
        regex=$(gitignore_to_regex "$pattern")
        [[ -n "$regex" ]] && exclude_regex+="|$regex"
    done

    exclude_regex="${exclude_regex#|}"

    if [[ -n "$exclude_regex" ]]; then
        debug "Gitignore regex: $exclude_regex"
        echo "$input" | grep -v -E "$exclude_regex" || true
    else
        echo "$input"
    fi
}

# Convert gitignore pattern to regex
gitignore_to_regex() {
    local pattern="$1"

    # Handle directory-only patterns (ending with /)
    if [[ "$pattern" == */ ]]; then
        pattern="${pattern%/}"
        local regex
        regex=$(glob_to_regex "$pattern")
        echo "/$regex(/|$)"
        return
    fi

    # Handle patterns starting with /
    if [[ "$pattern" == /* ]]; then
        pattern="${pattern#/}"
        local regex
        regex=$(glob_to_regex "$pattern")
        echo "^$regex"
        return
    fi

    # Handle patterns containing /
    if [[ "$pattern" == */* ]]; then
        local regex
        regex=$(glob_to_regex "$pattern")
        echo "(^|/)$regex"
        return
    fi

    # Simple pattern - match anywhere
    local regex
    regex=$(glob_to_regex "$pattern")
    echo "(^|/)$regex(/|$)"
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
