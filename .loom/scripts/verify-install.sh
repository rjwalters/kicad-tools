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
EXIT_SCHEMA_OUTDATED=5

# Manifest schema version.
#   v1: whole-file hash of every tracked path; file list derived from a
#       hard-coded directory walk (captured sibling-installer files).
#   v2 (issue #3600): file list derived from .loom/install-metadata.json
#       "installed_files" (Loom's ownership boundary); the top-level CLAUDE.md
#       entry is hashed over its <!-- BEGIN/END LOOM ORCHESTRATION --> region
#       only (recorded as "region": "loom-block"), so sibling edits outside
#       the Loom block no longer report drift.
# cmd_verify refuses to compare a manifest whose version != MANIFEST_VERSION
# and instructs the caller to regenerate, rather than reporting false drift.
MANIFEST_VERSION=2

# Loom section markers in the top-level CLAUDE.md (mirrors
# loom-daemon/src/init/scaffolding.rs LOOM_SECTION_START / LOOM_SECTION_END).
LOOM_SECTION_START='<!-- BEGIN LOOM ORCHESTRATION -->'
LOOM_SECTION_END='<!-- END LOOM ORCHESTRATION -->'

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
    Loom-owned installation files. The manifest can then be used to
    detect post-installation drift (modified or missing files).

${BOLD}TRACKED FILE SET:${NC}
    The file list is derived from .loom/install-metadata.json
    "installed_files" (Loom's ownership boundary) intersected with the
    files actually present on disk. Files a sibling installer added
    (e.g. .claude/agents/<other-tool>-*.md) are never tracked. When the
    metadata file is absent (pre-#3450 installs), a legacy directory
    walk is used instead, with a stderr warning.

    The top-level CLAUDE.md is hashed over its
    ${LOOM_SECTION_START} ... ${LOOM_SECTION_END}
    region only, so sibling edits outside the Loom block do not report
    drift. All other files are hashed whole.

${BOLD}NOT TRACKED (runtime/user/merge-target files):${NC}
    .loom/config.json              Local terminal config
    .loom/daemon-state.json        Daemon runtime state
    .loom/manifest.json            This manifest
    .loom/install-metadata.json    Install ownership record
    .loom/progress/                Shepherd progress
    .loom/worktrees/               Git worktrees
    .gitignore                     Consumer-merged ignore rules
    package.json                   Project package config

${BOLD}EXIT CODES:${NC}
    0    generate succeeded, or verify found no drift
    1    verify found modified or missing files
    2    Invalid arguments
    3    Manifest not found (verify mode)
    4    Environment error (not a git repo, no SHA command)
    5    Manifest schema outdated (verify mode) — run generate

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

# Return "loom-block" for the top-level CLAUDE.md (region-scoped hashing),
# empty string for every other path (whole-file hashing). Only the repo-root
# CLAUDE.md is a merge target; .loom/CLAUDE.md is fully Loom-owned.
region_for_path() {
    if [[ "$1" == "CLAUDE.md" ]]; then
        echo "loom-block"
    else
        echo ""
    fi
}

# Runtime / user-mutable / merge-target files that Loom installs (so they
# appear in install-metadata.json) but must NOT be checksum-tracked: verifying
# them would report drift on every legitimate consumer edit. Returns 0 (match)
# for paths to exclude from the tracked set.
is_untracked_runtime_file() {
    case "$1" in
        .loom/config.json) return 0 ;;
        .loom/daemon-state.json) return 0 ;;
        .loom/manifest.json) return 0 ;;
        .loom/install-metadata.json) return 0 ;;
        .loom/progress/*) return 0 ;;
        .loom/worktrees/*) return 0 ;;
        .gitignore) return 0 ;;
        package.json) return 0 ;;
        *) return 1 ;;
    esac
}

# Parse the "installed_files" JSON array from install-metadata.json into
# newline-delimited target-relative paths. Deliberately jq-free (awk) so
# `generate` has no hard jq dependency — the awk split pattern is ported from
# scripts/install/manifest.sh::_emit_loom_ownership_set (Loom-shipped paths
# never contain commas, so a naive comma split is safe).
parse_installed_files() {
    local metadata_path="$1"
    awk '
        { buf = buf $0 "\n" }
        END {
            idx = index(buf, "\"installed_files\"")
            if (idx == 0) { exit }
            rest = substr(buf, idx)
            lb = index(rest, "[")
            if (lb == 0) { exit }
            rest = substr(rest, lb + 1)
            rb = index(rest, "]")
            if (rb == 0) { exit }
            arr = substr(rest, 1, rb - 1)
            n = split(arr, items, ",")
            for (i = 1; i <= n; i++) {
                entry = items[i]
                sub(/^[[:space:]]+/, "", entry)
                sub(/[[:space:]]+$/, "", entry)
                sub(/^"/, "", entry)
                sub(/"$/, "", entry)
                if (entry != "") print entry
            }
        }
    ' "$metadata_path"
}

# Legacy fallback: hard-coded directory walk (pre-#3450 installs, or when
# install-metadata.json is absent/empty). This is the historical behavior and
# intentionally still captures .claude/agents/*.md by glob — it is only reached
# when Loom's ownership manifest is unavailable.
collect_tracked_files_walk() {
    local root="$1"
    local files=()

    # .loom/roles/*.md and *.json
    while IFS= read -r -d '' f; do
        files+=("${f#"$root"/}")
    done < <(find "$root/.loom/roles" -maxdepth 1 -type f \( -name "*.md" -o -name "*.json" \) -print0 2>/dev/null || true)

    # .loom/scripts/* (recursive, all files)
    while IFS= read -r -d '' f; do
        files+=("${f#"$root"/}")
    done < <(find "$root/.loom/scripts" -type f -print0 2>/dev/null || true)

    # .loom/docs/*
    while IFS= read -r -d '' f; do
        files+=("${f#"$root"/}")
    done < <(find "$root/.loom/docs" -type f -print0 2>/dev/null || true)

    # .claude/commands/loom/*.md
    while IFS= read -r -d '' f; do
        files+=("${f#"$root"/}")
    done < <(find "$root/.claude/commands/loom" -maxdepth 1 -type f -name "*.md" -print0 2>/dev/null || true)

    # .claude/agents/*.md
    while IFS= read -r -d '' f; do
        files+=("${f#"$root"/}")
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
        files+=("${f#"$root"/}")
    done < <(find "$root/.github/ISSUE_TEMPLATE" -type f -print0 2>/dev/null || true)

    # .github/workflows/ - no workflows installed by default
    # (label-external-issues.yml moved to optional in #3098)

    # Top-level docs
    if [[ -f "$root/CLAUDE.md" ]]; then
        files+=("CLAUDE.md")
    fi
    if [[ -f "$root/.loom/CLAUDE.md" ]]; then
        files+=(".loom/CLAUDE.md")
    fi
    if [[ -f "$root/.loom/README.md" ]]; then
        files+=(".loom/README.md")
    fi

    # CLI wrapper
    if [[ -f "$root/loom" ]]; then
        files+=("loom")
    fi

    # Output sorted, one per line
    [[ ${#files[@]} -eq 0 ]] && return 0
    printf '%s\n' "${files[@]}" | sort -u
}

# Collect all tracked file paths (relative to repo root), one per line, sorted.
#
# Primary path (issue #3600): derive the list from .loom/install-metadata.json
# "installed_files" (Loom's ownership boundary), intersected with files present
# on disk and minus the runtime/merge-target denylist. This means files a
# sibling installer added (e.g. Anvil's .claude/agents/anvil-*.md) never enter
# the manifest. Falls back to the legacy directory walk (with a warning) when
# the metadata is absent or yields no usable entries.
collect_tracked_files() {
    local root="$1"
    local metadata_path="$root/.loom/install-metadata.json"

    local installed=""
    if [[ -f "$metadata_path" ]]; then
        installed=$(parse_installed_files "$metadata_path")
    fi

    if [[ -n "$installed" ]]; then
        local files=()
        local rel
        while IFS= read -r rel; do
            [[ -z "$rel" ]] && continue
            is_untracked_runtime_file "$rel" && continue
            [[ -f "$root/$rel" ]] || continue
            files+=("$rel")
        done <<< "$installed"

        if [[ ${#files[@]} -gt 0 ]]; then
            printf '%s\n' "${files[@]}" | sort -u
            return 0
        fi
    fi

    # Fallback: metadata missing or empty (pre-#3450 install).
    echo "Warning: .loom/install-metadata.json missing or has no installed_files;" \
         "falling back to legacy directory walk (pre-#3450 install?)." \
         "Sibling-installer files may be captured; re-run install to refresh metadata." >&2
    collect_tracked_files_walk "$root"
}

# Emit the hashable bytes for an entry to stdout, honoring region rules.
# For region "loom-block", emit only the CLAUDE.md Loom marker region
# (inclusive). For whole-file entries, cat the file verbatim.
emit_hashable_content() {
    local full_path="$1"
    local region="$2"
    if [[ "$region" == "loom-block" ]]; then
        sed -n "/${LOOM_SECTION_START}/,/${LOOM_SECTION_END}/p" "$full_path"
    else
        cat "$full_path"
    fi
}

# Compute "<sha256> <byte-size>" for an entry, honoring the region rule.
# Whole-file entries hash the raw file bytes (identical to `shasum`); region
# entries hash the extracted marker block. Both generate and verify call this,
# so the two sides always agree.
compute_entry_digest() {
    local full_path="$1"
    local region="$2"
    local sha size
    if [[ "$region" == "loom-block" ]]; then
        local block
        block=$(emit_hashable_content "$full_path" "$region")
        sha=$(printf '%s' "$block" | $SHA_CMD | awk '{print $1}')
        size=$(printf '%s' "$block" | wc -c | tr -d '[:space:]')
    else
        sha=$(compute_sha256 "$full_path")
        size=$(wc -c < "$full_path" | tr -d '[:space:]')
    fi
    # Trailing newline is required: callers use `read`, which returns non-zero
    # on EOF-without-newline and would abort under `set -e`.
    printf '%s %s\n' "$sha" "$size"
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

    # Detect loom version from CLAUDE.md and commit from install-metadata.json
    local loom_version=""
    local loom_commit=""
    if [[ -f "$root/CLAUDE.md" ]]; then
        loom_version=$(grep -o 'Loom Version.*: .*' "$root/CLAUDE.md" | head -1 | sed 's/.*: //' | sed 's/\*//g' | tr -d '[:space:]' || true)
    fi
    if [[ -f "$root/.loom/install-metadata.json" ]]; then
        loom_commit=$(grep -o '"loom_commit": "[^"]*"' "$root/.loom/install-metadata.json" | head -1 | sed 's/.*: "//; s/"//' || true)
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

            local region
            region=$(region_for_path "$rel_path")
            local sha256 size
            read -r sha256 size < <(compute_entry_digest "$full_path" "$region")

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
            printf '      "size": %s' "$size"
            if [[ -n "$region" ]]; then
                printf ',\n      "region": "%s"\n' "$region"
            else
                printf '\n'
            fi
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

    # Schema-version gate (issue #3600). A manifest written by an older
    # verify-install.sh uses a different file-set / hashing contract (v1:
    # directory walk + whole-file CLAUDE.md hash). Comparing against it would
    # report false drift, so refuse and instruct the caller to regenerate.
    local manifest_schema
    manifest_schema=$(jq -r '.version // 0' "$manifest_path")
    if [[ "$manifest_schema" != "$MANIFEST_VERSION" ]]; then
        if [[ "$output_mode" == "json" ]]; then
            printf '{"status": "error", "error": "manifest_schema_outdated", "manifest_version": %s, "expected_version": %s}\n' \
                "${manifest_schema:-0}" "$MANIFEST_VERSION"
        elif [[ "$output_mode" != "quiet" ]]; then
            echo -e "${YELLOW}manifest schema outdated (found v${manifest_schema}, expected v${MANIFEST_VERSION}) — run 'verify-install.sh generate'${NC}" >&2
        fi
        exit $EXIT_SCHEMA_OUTDATED
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
        # Per-entry region marker (issue #3600). Absent → whole-file hashing.
        local region
        region=$(jq -r --arg p "$rel_path" '.files[$p].region // ""' "$manifest_path")

        if [[ ! -f "$full_path" ]]; then
            missing_files+=("$rel_path|$expected_sha256|$expected_size")
            continue
        fi

        # A region-scoped entry whose Loom marker block is now absent is real
        # drift (the block was removed), even though the file still exists.
        if [[ "$region" == "loom-block" ]] \
            && ! grep -q "$LOOM_SECTION_START" "$full_path" 2>/dev/null; then
            modified_files+=("$rel_path|$expected_sha256|missing-loom-markers|$expected_size|0")
            continue
        fi

        local actual_sha256 actual_size
        read -r actual_sha256 actual_size < <(compute_entry_digest "$full_path" "$region")

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
            # Auto mode: verify if manifest exists, generate if not.
            # Self-heal: an outdated-schema manifest is regenerated rather than
            # verified, giving consumers a hands-free upgrade path (issue #3600).
            local root
            root=$(find_repo_root) || exit $EXIT_ENV_ERROR
            if [[ -f "$root/.loom/manifest.json" ]]; then
                # Lightweight, jq-free schema read so auto mode stays robust.
                local auto_schema
                auto_schema=$(grep -o '"version"[[:space:]]*:[[:space:]]*[0-9]\{1,\}' \
                    "$root/.loom/manifest.json" | grep -o '[0-9]\{1,\}$' | head -1 || true)
                if [[ "$auto_schema" != "$MANIFEST_VERSION" ]]; then
                    cmd_generate
                else
                    cmd_verify
                fi
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
