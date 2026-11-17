#!/usr/bin/env bash
# check-git-identity.sh - Detect corrupted / stacked local git identity config (#4369).
#
# Background: a now-deleted Tauri-era code path (`terminal-actions.ts`,
# `setupWorktreeForAgent()`, removed in d61acab0 / #3353) pushed two shell
# lines into an agent's terminal to set a per-role git identity:
#   git config user.email "${gitIdentity.email}"
#   echo "✓ Git identity configured: ${gitIdentity.name} <${gitIdentity.email}>"
# A lost newline between the two lines glued the second line's first token
# onto the first line's value, producing corrupted local-scope emails like
# `loom-reviewer@users.noreply.github.comecho`. Because `git config user.email`
# REPLACES rather than appends, repeated writes/`--add` improvisation could
# also stack multiple values for the same key. Both symptoms persist in
# long-lived checkouts that predate the code's removal, even though nothing
# on `main` writes `user.email`/`user.name` anymore (see #4369).
#
# This script is a read-only detector: it never mutates git config. It flags
# two independent conditions in the LOCAL (repo + per-worktree) config scope:
#   1. CORRUPTION: a user.email (or user.name) value matching the glued-token
#      pattern (a GitHub noreply email immediately followed by extra
#      characters with no separator, e.g. "...github.comecho"). This is the
#      garbled-commit-author failure mode and is always a hard problem.
#   2. MULTI-VALUE: more than one value stored locally for user.email or
#      user.name (via `--get-all`), with no corrupted value among them. This
#      is ambiguous but not necessarily broken (`git config user.<field> <v>`
#      will still refuse to *set* while multiple values exist, but reads that
#      pick a single value may still work) — treated as a lower-severity
#      warning.
#
#   3. FIXTURE IDENTITY: the EFFECTIVE user.email (all scopes: system, global,
#      local, worktree) is a known test-fixture address (`ci@loom.test`, any
#      `@loom.local`, or an address under the reserved test domains `.test`,
#      `.invalid`, `example.com`). Fleet commits have been authored as
#      `Loom CI <ci@loom.test>` because such a value leaked into host git
#      config (#11332). Reported as a warning (exit 1) naming the scope that
#      supplied the value (`git config --show-origin --get user.email`).
#
#   4. MISSING IDENTITY: no explicit author identity at any scope or in the
#      environment, so git would invent `user@hostname` (#13088: a fleet PR
#      was authored as `Ubuntu <user@hostname>`). Probed
#      mechanically with `git -c user.useConfigOnly=true var GIT_AUTHOR_IDENT`,
#      which fails unless name and email are both explicit; `GIT_AUTHOR_*` env
#      identities count as explicit. Reported as exit 4 with remediation.
#
# Conditions 1 and 2 read LOCAL (+ per-worktree, when `extensions.worktreeConfig`
# is enabled) scope only — never global/system — so a normal global identity
# with no repo-local override never false-positives. `git config --get-all`
# WITHOUT `--local`/`--worktree` also surfaces global/system values, which
# would incorrectly flag perfectly ordinary setups.
#
# Usage:
#   ./.loom/scripts/check-git-identity.sh          # check the current repo
#   ./.loom/scripts/check-git-identity.sh --help   # show usage
#
# Exit codes:
#   0 - Clean: no corrupted value and at most one local value per field.
#   1 - WARNING: multiple local values for user.email and/or user.name (none
#       matching the corruption pattern), and/or the effective user.email is a
#       fixture address (condition 3). Ambiguous, not necessarily broken.
#   2 - Usage error or not inside a git repository.
#   3 - CORRUPTION detected: a value matches the glued-token pattern. This is
#       the failure mode that ships garbled commit authors (see PR #4303).
#   4 - MISSING identity (condition 4): no explicit user.name/user.email, so a
#       commit would be authored under git's `user@hostname` fallback.
#
# Remediation (never applied automatically — this script only detects and
# prints the command; auto-remediation is a judgment call, see #4369):
#   Preferred (most repos have a working global identity to fall back to):
#     git config --unset-all user.email && git config --unset-all user.name
#   Alternative (no global identity, or you want to keep a specific value):
#     git config --replace-all user.email <value-to-keep>
#     git config --replace-all user.name  <value-to-keep>
#   Fixture identity: remove it from the scope named in the warning
#     git config --global --unset user.email   (or --local/--system), or set a
#     real identity with `git config --global user.email <you>`.

set -uo pipefail  # no -e: git config "not found" exits are expected control flow

EXIT_OK=0
EXIT_MULTI_VALUE=1
EXIT_USAGE=2
EXIT_CORRUPTED=3
EXIT_MISSING=4

# Anchored to the specific corruption vector (a GitHub noreply email with
# extra characters glued directly after ".com", no separator) rather than a
# generic "\.com[a-z]+$" — the generic form would false-positive on ordinary
# addresses with unusual-but-valid TLDs (e.g. "user@company.comm").
CORRUPTION_REGEX='users\.noreply\.github\.com[a-zA-Z0-9]+$'

# Known fixture addresses (#11332): ci@loom.test, anything @loom.local, and
# the reserved test domains (RFC 2606): *.test, *.invalid, example.com.
FIXTURE_REGEX='(^ci@loom\.test$|@loom\.local$|\.test$|\.invalid$|@([A-Za-z0-9-]+\.)*example\.com$)'

usage() {
    sed -n '2,76p' "$0" | sed 's/^# \{0,1\}//'
}

case "${1:-}" in
    -h|--help)
        usage
        exit "$EXIT_OK"
        ;;
    "")
        ;;
    *)
        echo "check-git-identity.sh: unknown argument: $1" >&2
        echo "Run with --help for usage." >&2
        exit "$EXIT_USAGE"
        ;;
esac

if ! git rev-parse --git-dir >/dev/null 2>&1; then
    echo "check-git-identity.sh: not inside a git repository" >&2
    exit "$EXIT_USAGE"
fi

# get_values <field> -> unique local-scope values for that config key, one
# per line, in first-seen order. Combines --local and --worktree so a value
# set only in a per-worktree config (extensions.worktreeConfig=true) is not
# missed; when that extension is off, --worktree reads the same file as
# --local, so the de-dup keeps the merged output equivalent to plain --local.
get_values() {
    local field="$1"
    { git config --local --get-all "$field" 2>/dev/null || true
      git config --worktree --get-all "$field" 2>/dev/null || true; } \
      | awk '!seen[$0]++'
}

has_global_fallback() {
    [[ -n "$(git config --global --get user.email 2>/dev/null || true)" ]]
}

print_remediation() {
    local field="$1"
    echo "" >&2
    echo "  Remediation:" >&2
    echo "    Preferred (falls back to your global identity):" >&2
    echo "      git config --unset-all user.email && git config --unset-all user.name" >&2
    echo "    Alternative (keep one specific value, e.g. no global identity is set):" >&2
    echo "      git config --replace-all $field <value-to-keep>" >&2
    if ! has_global_fallback; then
        echo "" >&2
        echo "    NOTE: no global user.email is configured on this host — the" >&2
        echo "    'unset-all' path would leave commits failing with 'Author identity" >&2
        echo "    unknown' until you set one (git config --global user.email <you>)," >&2
        echo "    or use the --replace-all fallback above instead." >&2
    fi
}

corrupted=0
multi_value=0

# Bash 3.2 compatible (macOS ships /bin/bash 3.2 - no mapfile/readarray):
# gather values into a newline-joined string rather than an array.
for field in user.email user.name; do
    values_raw=$(get_values "$field")
    [[ -z "$values_raw" ]] && continue
    count=$(printf '%s\n' "$values_raw" | grep -c '')

    field_corrupted=0
    while IFS= read -r v; do
        [[ -z "$v" ]] && continue
        if [[ "$v" =~ $CORRUPTION_REGEX ]]; then
            field_corrupted=1
            corrupted=1
        fi
    done <<< "$values_raw"

    if [[ "$field_corrupted" -eq 1 ]]; then
        echo "ERROR: check-git-identity.sh: corrupted $field value detected." >&2
        echo "       A stored value looks like a glued-together shell command output" >&2
        echo "       (a GitHub noreply email with extra characters appended directly" >&2
        echo "       after '.com', no separator) — the #4369 failure mode that ships" >&2
        echo "       garbled commit authors." >&2
        echo "" >&2
        echo "  Stored $field value(s):" >&2
        while IFS= read -r v; do [[ -n "$v" ]] && echo "    - $v" >&2; done <<< "$values_raw"
        print_remediation "$field"
    elif [[ "$count" -gt 1 ]]; then
        multi_value=1
        echo "WARNING: check-git-identity.sh: multiple local $field values ($count) detected." >&2
        echo "         'git config $field <value>' will refuse to overwrite until this" >&2
        echo "         is resolved ('cannot overwrite multiple values')." >&2
        echo "" >&2
        echo "  Stored $field value(s):" >&2
        while IFS= read -r v; do [[ -n "$v" ]] && echo "    - $v" >&2; done <<< "$values_raw"
        print_remediation "$field"
    fi
done

# Effective-scope fixture identity check (#11332). Read-only: report only.
effective_email=$(git config --get user.email 2>/dev/null || true)
if [[ -n "$effective_email" && "$effective_email" =~ $FIXTURE_REGEX ]]; then
    multi_value=1
    origin=$(git config --show-origin --get user.email 2>/dev/null || true)
    echo "WARNING: check-git-identity.sh: effective user.email is a test-fixture address." >&2
    echo "         Commits made here would be authored as <$effective_email>." >&2
    echo "" >&2
    echo "  Supplied by: ${origin:-unknown}" >&2
    echo "" >&2
    echo "  Remediation (not applied automatically; use the scope named above):" >&2
    echo "    git config --global --unset user.email   # or --local / --system" >&2
    echo "    git config --global user.email <your-real-address>" >&2
fi

# Missing explicit identity (#13088). useConfigOnly makes git refuse to guess
# user@hostname, so a non-zero probe means the identity is not explicit.
missing=0
if ! git -c user.useConfigOnly=true var GIT_AUTHOR_IDENT >/dev/null 2>&1; then
    missing=1
    echo "ERROR: check-git-identity.sh: no explicit git identity (user.name / user.email)." >&2
    echo "       A commit here would be authored under git's auto-generated" >&2
    echo "       user@hostname fallback (#13088) and fail DCO / attribution checks." >&2
    echo "" >&2
    echo "  Remediation (not applied automatically):" >&2
    echo "    git config --global user.name  \"<your name>\"" >&2
    echo "    git config --global user.email <your-address>" >&2
    echo "    or export GIT_AUTHOR_NAME/GIT_AUTHOR_EMAIL (and GIT_COMMITTER_*)." >&2
fi

if [[ "$corrupted" -eq 1 ]]; then
    exit "$EXIT_CORRUPTED"
elif [[ "$missing" -eq 1 ]]; then
    exit "$EXIT_MISSING"
elif [[ "$multi_value" -eq 1 ]]; then
    exit "$EXIT_MULTI_VALUE"
fi

echo "check-git-identity.sh: local git identity is clean (no corruption, no stacked values)"
exit "$EXIT_OK"
