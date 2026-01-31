#!/bin/bash

# verify-install.sh - Checksum manifest for Loom installation verification
#
# Generates and verifies a SHA-256 checksum manifest (.loom/manifest.json)
# to detect post-installation drift (modified or missing files).
#
# Usage:
#   verify-install.sh                    # Auto: verify if manifest exists, generate if not
#   verify-install.sh generate           # Generate manifest
#   verify-install.sh generate --quiet   # Generate without output
#   verify-install.sh verify             # Verify against manifest (human-readable)
#   verify-install.sh verify --json      # Verify (machine-readable JSON)
#   verify-install.sh verify --quiet     # Verify (exit code only)
#   verify-install.sh --help             # Show help

set -euo pipefail

# Colors for output (only when stdout is a TTY)
if [[ -t 1 ]]; then
    RED='\033[0;31m'
    GREEN='\033[0;32m'
    YELLOW='\033[1;33m'
    BLUE='\033[0;34m'
    BOLD='\033[1m'
    NC='\033[0m'
else
    RED=''
    GREEN=''
    YELLOW=''
    BLUE=''
    BOLD=''
    NC=''
fi

# Exit codes
EXIT_OK=0
EXIT_DRIFT=1
EXIT_BAD_ARGS=2
EXIT_NO_MANIFEST=3
EXIT_ENV_ERROR=4

MANIFEST_VERSION=1

# Find the repository root (works from worktrees and subdirectories)
find_repo_root() {
    local dir="$PWD"
    while [[ "$dir" != "/" ]]; do
        if [[ -d "$dir/.git" ]] || [[ -f "$dir/.git" ]]; then
            echo "$dir"
            return 0
        fi
        dir="$(dirname "$dir")"
    done
    echo "Error: Not in a git repository" >&2
    return 1
}

# Detect SHA-256 command (portable across macOS/Linux)
detect_sha_cmd() {
    if command -v shasum &>/dev/null; then
        echo "shasum -a 256"
    elif command -v sha256sum &>/dev/null; then
        echo "sha256sum"
    else
        echo "Error: No SHA-256 command found (need shasum or sha256sum)" >&2
        return 1
    fi
}

# Compute SHA-256 of a file, output just the hash
compute_sha256() {
    local file="$1"
    $SHA_CMD "$file" | awk '{print $1}'
}

# Show help
show_help() {
    cat <<EOF
${BLUE}verify-install.sh - Loom installation checksum manifest${NC}

${BOLD}USAGE:${NC}
    verify-install.sh                    Auto: verify or generate
    verify-install.sh generate           Generate manifest
    verify-install.sh generate --quiet   Generate without output
    verify-install.sh verify             Verify against manifest
    verify-install.sh verify --json      Machine-readable output
    verify-install.sh verify --quiet     Exit code only
    verify-install.sh --help             Show this help

${BOLD}DESCRIPTION:${NC}
    Generates a SHA-256 checksum manifest (.loom/manifest.json) of all
    tracked Loom installation files. The manifest can then be used to
    detect post-installation drift (modified or missing files).

${BOLD}TRACKED FILE LOCATIONS:${NC}
    .loom/roles/*.md, *.json       Role definitions
    .loom/scripts/*, cli/*         Helper scripts
    .loom/docs/*                   Documentation
    .claude/commands/*.md          Slash commands
    .claude/agents/*.md            Agent definitions
    .claude/settings.json          Claude settings
    .github/labels.yml             Label definitions
    .github/ISSUE_TEMPLATE/*       Issue templates
    .github/workflows/*.yml        GitHub workflows
    CLAUDE.md, .loom/AGENTS.md     Top-level docs
    .loom/CLAUDE.md, .loom/README.md
    .codex/config.toml             Codex configuration
    loom                           CLI wrapper

${BOLD}NOT TRACKED (runtime/user files):${NC}
    .loom/config.json              Local terminal config
    .loom/daemon-state.json        Daemon runtime state
    .loom/progress/                Shepherd progress
    .loom/worktrees/               Git worktrees
    package.json                   Project package config

${BOLD}EXIT CODES:${NC}
    0    generate succeeded, or verify found no drift
    1    verify found modified or missing files
    2    Invalid arguments
    3    Manifest not found (verify mode)
    4    Environment error (not a git repo, no SHA command)

${BOLD}EXAMPLES:${NC}
    # After installation, generate a manifest
    ./.loom/scripts/verify-install.sh generate

    # Later, check for drift
    ./.loom/scripts/verify-install.sh verify

    # In CI, check exit code only
    ./.loom/scripts/verify-install.sh verify --quiet
    echo \$?  # 0 = clean, 1 = drift
EOF
}

# Collect all tracked file paths (relative to repo root)
# Outputs one path per line, sorted
collect_tracked_files() {
    local root="$1"
    local files=()

    # .loom/roles/*.md and *.json
    while IFS= read -r -d '' f; do
        files+=("${f#$root/}")
    done < <(find "$root/.loom/roles" -maxdepth 1 -type f \( -name "*.md" -o -name "*.json" \) -print0 2>/dev/null || true)

    # .loom/scripts/* (recursive, all files)
    while IFS= read -r -d '' f; do
        files+=("${f#$root/}")
    done < <(find "$root/.loom/scripts" -type f -print0 2>/dev/null || true)

    # .loom/docs/*
    while IFS= read -r -d '' f; do
        files+=("${f#$root/}")
    done < <(find "$root/.loom/docs" -type f -print0 2>/dev/null || true)

    # .claude/commands/*.md
    while IFS= read -r -d '' f; do
        files+=("${f#$root/}")
    done < <(find "$root/.claude/commands" -maxdepth 1 -type f -name "*.md" -print0 2>/dev/null || true)

    # .claude/agents/*.md
    while IFS= read -r -d '' f; do
        files+=("${f#$root/}")
    done < <(find "$root/.claude/agents" -maxdepth 1 -type f -name "*.md" -print0 2>/dev/null || true)

    # .claude/settings.json
    if [[ -f "$root/.claude/settings.json" ]]; then
        files+=(".claude/settings.json")
    fi

    # .github/labels.yml
    if [[ -f "$root/.github/labels.yml" ]]; then
        files+=(".github/labels.yml")
    fi

    # .github/ISSUE_TEMPLATE/*
    while IFS= read -r -d '' f; do
        files+=("${f#$root/}")
    done < <(find "$root/.github/ISSUE_TEMPLATE" -type f -print0 2>/dev/null || true)

    # .github/workflows/*.yml (only loom-managed ones)
    if [[ -f "$root/.github/workflows/label-external-issues.yml" ]]; then
        files+=(".github/workflows/label-external-issues.yml")
    fi

    # Top-level docs
    if [[ -f "$root/CLAUDE.md" ]]; then
        files+=("CLAUDE.md")
    fi
    if [[ -f "$root/.loom/AGENTS.md" ]]; then
        files+=(".loom/AGENTS.md")
    fi
    if [[ -f "$root/.loom/CLAUDE.md" ]]; then
        files+=(".loom/CLAUDE.md")
    fi
    if [[ -f "$root/.loom/README.md" ]]; then
        files+=(".loom/README.md")
    fi

    # .codex/config.toml
    if [[ -f "$root/.codex/config.toml" ]]; then
        files+=(".codex/config.toml")
    fi

    # CLI wrapper
    if [[ -f "$root/loom" ]]; then
        files+=("loom")
    fi

    # Output sorted, one per line
    printf '%s\n' "${files[@]}" | sort
}

# Generate manifest
cmd_generate() {
    local quiet=false
    if [[ "${1:-}" == "--quiet" ]]; then
        quiet=true
    fi

    local root
    root=$(find_repo_root) || exit $EXIT_ENV_ERROR

    local manifest_path="$root/.loom/manifest.json"
    local tmp_path="${manifest_path}.tmp"

    # Ensure .loom directory exists
    mkdir -p "$root/.loom"

    # Collect files
    local tracked_files
    tracked_files=$(collect_tracked_files "$root")

    if [[ -z "$tracked_files" ]]; then
        echo "Error: No tracked files found. Is Loom installed?" >&2
        exit $EXIT_ENV_ERROR
    fi

    local file_count=0
    local generated_at
    generated_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)

    # Detect loom version from CLAUDE.md if possible
    local loom_version=""
    local loom_commit=""
    if [[ -f "$root/CLAUDE.md" ]]; then
        loom_version=$(grep -o 'Loom Version.*: .*' "$root/CLAUDE.md" | head -1 | sed 's/.*: //' | sed 's/\*//g' | tr -d '[:space:]' || true)
        loom_commit=$(grep -o 'Loom Commit.*: .*' "$root/CLAUDE.md" | head -1 | sed 's/.*: //' | sed 's/\*//g' | tr -d '[:space:]' || true)
    fi

    # Build JSON using printf (no jq dependency for generate)
    {
        printf '{\n'
        printf '  "version": %d,\n' "$MANIFEST_VERSION"
        printf '  "generated_at": "%s",\n' "$generated_at"
        printf '  "loom_version": "%s",\n' "$loom_version"
        printf '  "loom_commit": "%s",\n' "$loom_commit"

        # First pass: count files
        while IFS= read -r rel_path; do
            [[ -z "$rel_path" ]] && continue
            file_count=$((file_count + 1))
        done <<< "$tracked_files"

        printf '  "file_count": %d,\n' "$file_count"
        printf '  "files": {\n'

        local first=true
        while IFS= read -r rel_path; do
            [[ -z "$rel_path" ]] && continue
            local full_path="$root/$rel_path"

            if [[ ! -f "$full_path" ]]; then
                continue
            fi

            local sha256
            sha256=$(compute_sha256 "$full_path")
            local size
            size=$(wc -c < "$full_path" | tr -d '[:space:]')

            if [[ "$first" == "true" ]]; then
                first=false
            else
                printf ',\n'
            fi

            # Escape any special chars in path for JSON
            local json_path
            json_path=$(printf '%s' "$rel_path" | sed 's/\\/\\\\/g; s/"/\\"/g')

            printf '    "%s": {\n' "$json_path"
            printf '      "sha256": "%s",\n' "$sha256"
            printf '      "size": %s\n' "$size"
            printf '    }'
        done <<< "$tracked_files"

        printf '\n  }\n'
        printf '}\n'
    } > "$tmp_path"

    # Atomic write
    mv "$tmp_path" "$manifest_path"

    if [[ "$quiet" != "true" ]]; then
        echo -e "${GREEN}Manifest generated: .loom/manifest.json${NC}"
        echo -e "  Files tracked: ${BOLD}$file_count${NC}"
        echo -e "  Generated at: $generated_at"
    fi

    exit $EXIT_OK
}

# Verify against manifest
cmd_verify() {
    local output_mode="human"
    if [[ "${1:-}" == "--json" ]]; then
        output_mode="json"
    elif [[ "${1:-}" == "--quiet" ]]; then
        output_mode="quiet"
    fi

    local root
    root=$(find_repo_root) || exit $EXIT_ENV_ERROR

    local manifest_path="$root/.loom/manifest.json"

    if [[ ! -f "$manifest_path" ]]; then
        if [[ "$output_mode" == "json" ]]; then
            printf '{"status": "error", "error": "manifest_not_found"}\n'
        elif [[ "$output_mode" != "quiet" ]]; then
            echo -e "${RED}Error: Manifest not found at .loom/manifest.json${NC}" >&2
            echo "Run 'verify-install.sh generate' to create one." >&2
        fi
        exit $EXIT_NO_MANIFEST
    fi

    # Requires jq for parsing manifest
    if ! command -v jq &>/dev/null; then
        echo "Error: jq is required for verify mode" >&2
        exit $EXIT_ENV_ERROR
    fi

    # Read manifest metadata
    local manifest_generated_at
    manifest_generated_at=$(jq -r '.generated_at // ""' "$manifest_path")
    local manifest_loom_version
    manifest_loom_version=$(jq -r '.loom_version // ""' "$manifest_path")
    local total_tracked
    total_tracked=$(jq '.file_count // 0' "$manifest_path")

    # Iterate over manifest files, check each
    local ok_count=0
    local modified_files=()
    local missing_files=()

    # Get file paths from manifest
    local file_paths
    file_paths=$(jq -r '.files | keys[]' "$manifest_path")

    while IFS= read -r rel_path; do
        [[ -z "$rel_path" ]] && continue
        local full_path="$root/$rel_path"

        local expected_sha256
        expected_sha256=$(jq -r --arg p "$rel_path" '.files[$p].sha256' "$manifest_path")
        local expected_size
        expected_size=$(jq -r --arg p "$rel_path" '.files[$p].size' "$manifest_path")

        if [[ ! -f "$full_path" ]]; then
            missing_files+=("$rel_path|$expected_sha256|$expected_size")
            continue
        fi

        local actual_sha256
        actual_sha256=$(compute_sha256 "$full_path")
        local actual_size
        actual_size=$(wc -c < "$full_path" | tr -d '[:space:]')

        if [[ "$actual_sha256" != "$expected_sha256" ]]; then
            modified_files+=("$rel_path|$expected_sha256|$actual_sha256|$expected_size|$actual_size")
        else
            ok_count=$((ok_count + 1))
        fi
    done <<< "$file_paths"

    local modified_count=${#modified_files[@]}
    local missing_count=${#missing_files[@]}
    local status="ok"
    local exit_code=$EXIT_OK

    if [[ $modified_count -gt 0 ]] || [[ $missing_count -gt 0 ]]; then
        status="drift"
        exit_code=$EXIT_DRIFT
    fi

    # Output results
    case "$output_mode" in
        json)
            printf '{\n'
            printf '  "status": "%s",\n' "$status"
            printf '  "manifest_generated_at": "%s",\n' "$manifest_generated_at"
            printf '  "loom_version": "%s",\n' "$manifest_loom_version"
            printf '  "total_tracked": %d,\n' "$total_tracked"
            printf '  "ok": %d,\n' "$ok_count"

            # Modified files array
            printf '  "modified": ['
            if [[ $modified_count -gt 0 ]]; then
                local first=true
                for entry in "${modified_files[@]}"; do
                    IFS='|' read -r path exp_sha act_sha exp_size act_size <<< "$entry"
                    if [[ "$first" == "true" ]]; then
                        first=false
                        printf '\n'
                    else
                        printf ',\n'
                    fi
                    printf '    {\n'
                    printf '      "path": "%s",\n' "$path"
                    printf '      "expected_sha256": "%s",\n' "$exp_sha"
                    printf '      "actual_sha256": "%s",\n' "$act_sha"
                    printf '      "expected_size": %s,\n' "$exp_size"
                    printf '      "actual_size": %s\n' "$act_size"
                    printf '    }'
                done
                printf '\n  '
            fi
            printf '],\n'

            # Missing files array
            printf '  "missing": ['
            if [[ $missing_count -gt 0 ]]; then
                local first=true
                for entry in "${missing_files[@]}"; do
                    IFS='|' read -r path exp_sha exp_size <<< "$entry"
                    if [[ "$first" == "true" ]]; then
                        first=false
                        printf '\n'
                    else
                        printf ',\n'
                    fi
                    printf '    {\n'
                    printf '      "path": "%s",\n' "$path"
                    printf '      "expected_sha256": "%s",\n' "$exp_sha"
                    printf '      "expected_size": %s\n' "$exp_size"
                    printf '    }'
                done
                printf '\n  '
            fi
            printf ']\n'
            printf '}\n'
            ;;

        human)
            echo -e "${BOLD}Loom Installation Verification${NC}"
            echo "==============================="
            echo -e "Manifest: .loom/manifest.json"
            echo -e "Generated: $manifest_generated_at"
            echo -e "Files tracked: $total_tracked"
            echo ""
            echo "Results:"
            echo -e "  OK:        ${GREEN}$ok_count files match${NC}"

            if [[ $modified_count -gt 0 ]]; then
                echo -e "  MODIFIED:  ${YELLOW}$modified_count files changed${NC}"
            fi
            if [[ $missing_count -gt 0 ]]; then
                echo -e "  MISSING:   ${RED}$missing_count files removed${NC}"
            fi

            if [[ $modified_count -gt 0 ]]; then
                echo ""
                echo "Modified files:"
                local entry
                for entry in "${modified_files[@]}"; do
                    local path
                    IFS='|' read -r path _ _ _ _ <<< "$entry"
                    echo -e "  ${YELLOW}$path${NC} (sha256 mismatch)"
                done
            fi

            if [[ $missing_count -gt 0 ]]; then
                echo ""
                echo "Missing files:"
                local entry
                for entry in "${missing_files[@]}"; do
                    local path
                    IFS='|' read -r path _ _ <<< "$entry"
                    echo -e "  ${RED}$path${NC}"
                done
            fi

            echo ""
            if [[ "$status" == "ok" ]]; then
                echo -e "Status: ${GREEN}ALL FILES MATCH${NC}"
            else
                echo -e "Status: ${RED}DRIFT DETECTED${NC} ($modified_count modified, $missing_count missing)"
            fi
            ;;

        quiet)
            # No output, just exit code
            ;;
    esac

    exit $exit_code
}

# Main
main() {
    # Validate environment
    SHA_CMD=$(detect_sha_cmd) || exit $EXIT_ENV_ERROR

    # Parse command
    local command="${1:-}"

    case "$command" in
        generate)
            shift
            cmd_generate "${1:-}"
            ;;
        verify)
            shift
            cmd_verify "${1:-}"
            ;;
        --help|-h|help)
            show_help
            exit $EXIT_OK
            ;;
        "")
            # Auto mode: verify if manifest exists, generate if not
            local root
            root=$(find_repo_root) || exit $EXIT_ENV_ERROR
            if [[ -f "$root/.loom/manifest.json" ]]; then
                cmd_verify
            else
                cmd_generate
            fi
            ;;
        *)
            echo -e "${RED}Error: Unknown command '$command'${NC}" >&2
            echo "Run 'verify-install.sh --help' for usage" >&2
            exit $EXIT_BAD_ARGS
            ;;
    esac
}

main "$@"
