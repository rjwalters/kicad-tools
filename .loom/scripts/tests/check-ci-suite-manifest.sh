#!/usr/bin/env bash
# check-ci-suite-manifest.sh — enforce the wired/excluded partition invariant
# for defaults/scripts/tests/ (issue #4455).
#
# Invariant: every `test-*.sh` in this directory appears in EXACTLY ONE of
#   - ci-wired.txt    (run by run-ci-suites.sh in CI), or
#   - ci-excluded.txt (explicitly excluded, with a required one-line reason).
#
# This is the forcing function that keeps the coverage gap from regrowing: a
# newly added suite that is in NEITHER list fails this check, so a contributor
# must consciously wire it or exclude-it-with-a-reason. The `lib/` helper
# directory is intentionally ignored — only `test-*.sh` files are suites.
#
# Since #4769 the invariant also covers tests/hooks/ (repo root) — the
# Claude-path guard suites. Since #4451 it also covers defaults/hooks/tests/
# (the Repo Skills' own guard-hook suites — background-subagents,
# loom-workspace, worktree-paths, methodology-inject, skill-router). Since
# #5275 it also covers tests/install/ (installer/uninstaller unit suites) and
# tests/hermit/ (the Hermit role's stateless-ceremony heuristic regression
# guard) -- both were previously orphaned (no CI runner at all). Since #5278
# it also covers the top-level scripts/ directory (non-recursive — only
# `scripts/test-*.sh` itself, not subdirectories), whose suites are invoked
# directly as their own CI/build-gate steps rather than via run-ci-suites.sh;
# they are listed in ci-excluded.txt with a "wired elsewhere" reason (see that
# file) so run-ci-suites.sh doesn't double-run them. Entries for any external
# directory are qualified with their path relative to the repo root (e.g.
# `tests/hooks/test-guard-destructive.sh`,
# `defaults/hooks/tests/test-skill-router.sh`,
# `tests/install/test-forge-detect.sh`, `scripts/test-changelog.sh`) so they
# can't collide with a same-named suite in this directory.
#
# Exit 0 = invariant holds; exit 1 = violation (details printed to stderr).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
WIRED_MANIFEST="$SCRIPT_DIR/ci-wired.txt"
EXCLUDED_MANIFEST="$SCRIPT_DIR/ci-excluded.txt"
# Directories of suites outside defaults/scripts/tests/ that this manifest
# also governs, each relative to the repo root (#4769, #4451, #5275, #5278).
# The scan below is non-recursive (bash glob `test-*.sh` in each dir), so
# "scripts" only picks up top-level scripts/test-*.sh, not subdirectories.
EXTERNAL_TEST_DIRS=("tests/hooks" "defaults/hooks/tests" "tests/install" "tests/hermit" "scripts")

fail=0
err() { printf 'ERROR: %s\n' "$1" >&2; fail=1; }

for m in "$WIRED_MANIFEST" "$EXCLUDED_MANIFEST"; do
    [[ -f "$m" ]] || { err "missing manifest: $m"; }
done
[[ "$fail" -eq 0 ]] || exit 1

# Strip comments/blank lines; emit the first whitespace-delimited token (the
# filename) of each manifest entry.
manifest_names() { # <manifest-file>
    sed -E 's/#.*$//' "$1" | awk 'NF { print $1 }'
}

# --- Collect the three sets --------------------------------------------------
mapfile -t actual_local < <(cd "$SCRIPT_DIR" && for f in test-*.sh; do [[ -e "$f" ]] && printf '%s\n' "$f"; done)
actual_external=()
for rel_dir in "${EXTERNAL_TEST_DIRS[@]}"; do
    ext_dir="$REPO_ROOT/$rel_dir"
    [[ -d "$ext_dir" ]] || continue
    while IFS= read -r name; do
        [[ -n "$name" ]] || continue
        actual_external+=("$rel_dir/$name")
    done < <(cd "$ext_dir" && for f in test-*.sh; do [[ -e "$f" ]] && printf '%s\n' "$f"; done)
done
mapfile -t actual < <(printf '%s\n' "${actual_local[@]}" "${actual_external[@]}" | sort)
mapfile -t wired < <(manifest_names "$WIRED_MANIFEST" | sort)
mapfile -t excluded < <(manifest_names "$EXCLUDED_MANIFEST" | sort)

# --- Every excluded entry must carry a non-empty reason ----------------------
while IFS= read -r line; do
    stripped="$(printf '%s' "$line" | sed -E 's/#.*$//')"
    [[ -n "${stripped// /}" ]] || continue
    name="$(awk '{print $1}' <<<"$stripped")"
    reason="$(awk '{$1=""; sub(/^[[:space:]]+/, ""); print}' <<<"$stripped")"
    if [[ -z "${reason// /}" ]]; then
        err "excluded suite '$name' has no reason (format: '<suite.sh>  <reason>')"
    fi
done < "$EXCLUDED_MANIFEST"

# --- No duplicates within either manifest ------------------------------------
dup_wired="$(printf '%s\n' "${wired[@]}" | sort | uniq -d)"
[[ -z "$dup_wired" ]] || err "duplicate entries in ci-wired.txt: $(tr '\n' ' ' <<<"$dup_wired")"
dup_excluded="$(printf '%s\n' "${excluded[@]}" | sort | uniq -d)"
[[ -z "$dup_excluded" ]] || err "duplicate entries in ci-excluded.txt: $(tr '\n' ' ' <<<"$dup_excluded")"

# --- No suite may appear in BOTH manifests -----------------------------------
both="$(comm -12 <(printf '%s\n' "${wired[@]}") <(printf '%s\n' "${excluded[@]}"))"
[[ -z "$both" ]] || err "suite(s) listed in BOTH manifests: $(tr '\n' ' ' <<<"$both")"

# --- No manifest entry may reference a nonexistent file ----------------------
# A `/`-containing entry is a path relative to the repo root (tests/hooks/…,
# #4769); a bare filename is resolved against this directory as before.
listed="$(printf '%s\n' "${wired[@]}" "${excluded[@]}" | sort -u)"
while IFS= read -r name; do
    [[ -n "$name" ]] || continue
    if [[ "$name" == */* ]]; then
        suite_path="$REPO_ROOT/$name"
    else
        suite_path="$SCRIPT_DIR/$name"
    fi
    [[ -f "$suite_path" ]] || err "manifest references nonexistent suite: $name"
done <<<"$listed"

# --- Every actual test-*.sh must be listed in exactly one manifest -----------
unlisted="$(comm -23 <(printf '%s\n' "${actual[@]}") <(printf '%s\n' "$listed"))"
if [[ -n "$unlisted" ]]; then
    while IFS= read -r name; do
        [[ -n "$name" ]] || continue
        err "suite '$name' is in NEITHER ci-wired.txt nor ci-excluded.txt — wire it or exclude it with a reason"
    done <<<"$unlisted"
fi

if [[ "$fail" -eq 0 ]]; then
    printf 'OK: %d suites (%d wired, %d excluded) — every test-*.sh is accounted for.\n' \
        "${#actual[@]}" "${#wired[@]}" "${#excluded[@]}"
fi
exit "$fail"
