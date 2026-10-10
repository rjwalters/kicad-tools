#!/usr/bin/env bash
# test-skip-labels.sh — regression test for #8255's shared skip-label source.
#
# Covers `defaults/scripts/skip-labels.sh`, a thin stub over
# `loom-daemon skip-labels` (Rust, `loom-daemon/src/cli/skip_labels.rs`). It
# unions the fixed fleet-wide hard-exclusion list (`external`, #7528) with
# this workspace's configured `autonomous.workFinder.extraSkipLabels`
# (#6685) — the per-repo knob the daemon's work finder already reads but
# `curator.md`'s Priority-2 fallback query (and the analogous
# hand-maintained lists in `builder.md` / `guide.md`) never did.
#
# THE MOTIVATING CASE (#8255): 2AMLogic/2am uses a `journal` label on
# long-lived pass-by-pass status issues (2am#582) that are never work items.
# With `"autonomous": {"workFinder": {"extraSkipLabels": ["journal"]}}` in
# `.loom/config.json`, every Curator fallback pass still surfaced them as
# curation candidates because the fallback query only ever read
# `hard-exclusion-labels.sh`'s fixed list. This suite pins that a repo with
# `journal` configured never has it survive the `--jq-not` filter.
#
# What this suite pins:
#   - default behavior (no `extraSkipLabels` configured) is IDENTICAL to
#     `hard-exclusion-labels.sh`'s output (Acceptance Criterion 2);
#   - a repo with `extraSkipLabels: ["journal"]` gets `journal` folded into
#     every rendering mode, alongside the fleet-wide `external`
#     (Acceptance Criterion 1);
#   - `loom:building` can never be resolved, even if named in config —
#     mirrors the work finder's own defensive filter;
#   - the `--jq-not` fragment actually filters a `journal`-labelled issue out
#     of a realistic `gh issue list --json number,labels` payload;
#   - an unknown option is a usage error, not a silently-empty list;
#   - every find-work tier in `builder.md` (not just the fallback) applies the
#     fragment, and `builder.json`'s interval prompt restates none unfiltered
#     (#8911).
#
# Needs a BUILT `loom-daemon` (the subject is a stub over a Rust
# subcommand) — same shape as test-classify-dependency-block.sh, wired in
# the "Native Port Suites" CI job (#7952), not shell-suite-tests.
#
# Usage:
#   cargo build --package loom-daemon
#   bash defaults/scripts/tests/test-skip-labels.sh
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"

# shellcheck source=lib/require-daemon-bin.sh
source "$SCRIPT_DIR/lib/require-daemon-bin.sh"
loom_test_require_daemon_bin "$SCRIPTS_DIR" "skip-labels"

resolve_shipped_script() {
    local rel="$1"
    if [[ -f "$REPO_ROOT/.loom/scripts/$rel" ]]; then
        printf '%s\n' "$REPO_ROOT/.loom/scripts/$rel"
    else
        printf '%s\n' "$REPO_ROOT/defaults/scripts/$rel"
    fi
}

SUBJECT="$(resolve_shipped_script "skip-labels.sh")"
HARD_EXCLUSION="$(resolve_shipped_script "hard-exclusion-labels.sh")"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

passed=0
failed=0
pass() { echo -e "${GREEN}\xe2\x9c\x93${NC} $1"; passed=$((passed + 1)); }
fail() { echo -e "${RED}\xe2\x9c\x97${NC} $1"; failed=$((failed + 1)); }

# Exact whole-line membership in a newline-separated list, with no pipeline.
# `printf '%s\n' "$list" | grep -qx "$x"` would be the obvious spelling, but
# under this file's `set -o pipefail` that is the SIGPIPE class
# scripts/check-pipefail-early-exit.sh ratchets (#7790/#7789): `grep -q` closes
# the pipe the moment it matches, the producer takes SIGPIPE (141), and
# pipefail reports the whole pipeline as failed — intermittently, depending on
# output size vs. the pipe buffer. This is the pure-bash "exact membership in a
# list" idiom from that script's own HOW TO FIX A FINDING table, and it is
# bash-3.2-clean (the macOS leg of the Shell Syntax job).
has_line() {
    local list="$1" needle="$2"
    case $'\n'"$list"$'\n' in
        *$'\n'"$needle"$'\n'*) return 0 ;;
        *) return 1 ;;
    esac
}

if [[ ! -f "$SUBJECT" ]]; then
    echo "ERROR: skip-labels.sh not found at $REPO_ROOT/.loom/scripts/ or $REPO_ROOT/defaults/scripts/" >&2
    exit 1
fi

echo "=== test-skip-labels.sh (#8255) ==="
echo "subject: $SUBJECT"
echo ""

# --- Fixture repos -----------------------------------------------------
WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

NO_CONFIG_REPO="$WORKDIR/no-config"
mkdir -p "$NO_CONFIG_REPO"

JOURNAL_REPO="$WORKDIR/journal-repo"
mkdir -p "$JOURNAL_REPO/.loom"
cat >"$JOURNAL_REPO/.loom/config.json" <<'EOF'
{
  "autonomous": {
    "workFinder": {
      "extraSkipLabels": ["journal"]
    }
  }
}
EOF

BUILDING_REPO="$WORKDIR/building-repo"
mkdir -p "$BUILDING_REPO/.loom"
cat >"$BUILDING_REPO/.loom/config.json" <<'EOF'
{
  "autonomous": {
    "workFinder": {
      "extraSkipLabels": ["journal", "loom:building"]
    }
  }
}
EOF

# --- 1. AC2: no config -> identical to hard-exclusion-labels.sh ---------
no_config_out="$(bash "$SUBJECT" --repo-root "$NO_CONFIG_REPO" 2>&1)"
hard_excl_out="$(bash "$HARD_EXCLUSION" 2>&1)"

if [[ "$no_config_out" == "$hard_excl_out" ]]; then
    pass "no extraSkipLabels configured: output matches hard-exclusion-labels.sh exactly (AC2)"
else
    fail "expected '$hard_excl_out', got '$no_config_out'"
fi

# --- 2. AC1: journal-configured repo folds it in ------------------------
journal_out="$(bash "$SUBJECT" --repo-root "$JOURNAL_REPO" 2>&1)"

if has_line "$journal_out" 'journal'; then
    pass "a repo with extraSkipLabels: [journal] includes journal (AC1, 2am#625)"
else
    fail "journal missing from: $journal_out"
fi

if has_line "$journal_out" 'external'; then
    pass "the fleet-wide external exclusion is still present alongside journal"
else
    fail "external missing from: $journal_out"
fi

# --- 3. loom:building can never be resolved, even if configured --------
building_out="$(bash "$SUBJECT" --repo-root "$BUILDING_REPO" 2>&1)"
if has_line "$building_out" 'journal' && ! has_line "$building_out" 'loom:building'; then
    pass "loom:building is never resolvable even when named in config"
else
    fail "unexpected output with loom:building configured: $building_out"
fi

# --- 4. Every rendering covers every label for the journal repo --------
json_out="$(bash "$SUBJECT" --repo-root "$JOURNAL_REPO" --json 2>&1)"
jqnot_out="$(bash "$SUBJECT" --repo-root "$JOURNAL_REPO" --jq-not 2>&1)"
search_out="$(bash "$SUBJECT" --repo-root "$JOURNAL_REPO" --search 2>&1)"

missing=""
while IFS= read -r label; do
    [[ -z "$label" ]] && continue
    [[ "$json_out" == *"\"$label\""* ]] || missing+=" json:$label"
    [[ "$jqnot_out" == *"contains([\"$label\"])"* ]] || missing+=" jq-not:$label"
    [[ "$search_out" == *"-label:\"$label\""* ]] || missing+=" search:$label"
done <<< "$journal_out"

if [[ -z "$missing" ]]; then
    pass "every label appears in --json, --jq-not and --search"
else
    fail "renderings omit:$missing"
fi

# --- 5. The --jq-not fragment actually filters a journal issue ---------
if command -v jq >/dev/null 2>&1; then
    payload='[
      {"number":1,"labels":[{"name":"loom:triage"}]},
      {"number":582,"labels":[{"name":"journal"}]}
    ]'
    kept="$(printf '%s' "$payload" | jq -r ".[] | select($jqnot_out) | .number" 2>&1)"
    jq_rc=$?
    if [[ "$jq_rc" -ne 0 ]]; then
        fail "--jq-not is not a valid jq expression: $jqnot_out ($kept)"
    elif [[ "$kept" == "1" ]]; then
        pass "--jq-not keeps the ordinary issue and drops the journal one (2am#625 case)"
    else
        fail "expected only #1 to survive the --jq-not filter, got: $kept"
    fi
else
    echo "note: jq not installed — skipping the fragment-validity check" >&2
fi

# --- 6. An unknown option must NOT yield a silently-empty list ---------
bad_out="$(bash "$SUBJECT" --nope 2>&1)"
bad_rc=$?
if [[ "$bad_rc" -ne 0 ]]; then
    pass "an unknown option is a usage error (rc=$bad_rc), never an empty list"
else
    fail "expected a non-zero exit for an unknown option, got 0: $bad_out"
fi

help_out="$(bash "$SUBJECT" --help 2>&1)"
help_rc=$?
if [[ "$help_rc" -eq 0 ]]; then
    pass "--help exits 0"
else
    fail "expected --help to exit 0, got $help_rc: $help_out"
fi

# --- 7. #8911: EVERY builder.md find-work tier applies the skip set -----
# #8255 wired the filter into the fallback tier only, so an issue carrying a
# configured skip label AND a higher-priority label (the star, loom:curated)
# was still offered — from the HIGHEST tier. Each tier's fenced block is run
# verbatim here (cwd = fixture repo, the real skip-labels.sh behind the
# `./.loom/scripts/` path the block names, a fake `gh` that ANDs `--label`
# like the real one), so the prompt's own quoting is what is under test.
BUILDER_MD="$REPO_ROOT/defaults/.claude/commands/loom/builder.md"
BUILDER_JSON="$REPO_ROOT/defaults/roles/builder.json"

# The first ```bash block under "**Step <n>".
step_block() {
    awk -v s="**Step $1" 'index($0, s) == 1 { f = 1; next }
        f && /^```bash/ { b = 1; next }
        b && /^```/ { exit }
        b { print }' "$BUILDER_MD"
}

# Structural drift guard: every Step that lists claimable issues, including a
# future fourth tier, resolves and uses the fragment, with no small pre-filter cap.
queried=0
while IFS= read -r n; do
    block="$(step_block "$n")"
    [[ "$block" == *"gh issue list"* ]] || continue
    queried=$((queried + 1))
    # shellcheck disable=SC2016  # literal prompt text, not an expansion
    if [[ "$block" == *'skip-labels.sh --jq-not'* && "$block" == *'select('*'$EXCL'* \
        && "$block" == *'--limit=500'* ]]; then
        pass "builder.md Step $n filters on skip-labels.sh --jq-not with --limit=500 (#8911)"
    else
        fail "builder.md Step $n is a claim query without the \$EXCL skip filter / --limit=500"
    fi
done < <(sed -n 's/^\*\*Step \([0-9][0-9]*\).*/\1/p' "$BUILDER_MD")
if [[ "$queried" -ge 3 ]]; then
    pass "found $queried find-work tier queries in builder.md"
else
    fail "expected at least 3 find-work tier queries in builder.md, found $queried"
fi

if command -v jq >/dev/null 2>&1; then
    FAKE_BIN="$WORKDIR/fake-bin"
    mkdir -p "$FAKE_BIN"
    cat >"$FAKE_BIN/gh" <<'EOF'
#!/usr/bin/env bash
want='[]'; prog='.'
while [[ $# -gt 0 ]]; do
    case "$1" in
        --label=*) want="$(jq -c --arg l "${1#--label=}" '. + [$l]' <<<"$want")" ;;
        --jq) prog="$2"; shift ;;
    esac
    shift
done
jq -c --argjson want "$want" 'map(select(($want - [.labels[].name]) == []))' "$GH_FIXTURE" \
    | jq -r "$prog"
EOF
    chmod +x "$FAKE_BIN/gh"
    for repo in "$NO_CONFIG_REPO" "$JOURNAL_REPO"; do
        mkdir -p "$repo/.loom/scripts"
        printf '#!/usr/bin/env bash\nexec bash "%s" "$@"\n' "$SUBJECT" >"$repo/.loom/scripts/skip-labels.sh"
        chmod +x "$repo/.loom/scripts/skip-labels.sh"
    done

    # Fixture: per tier label T, issue <i>1 = T, <i>2 = T + journal,
    # <i>3 = T + external. The tier-1 labels are read out of the prompt's own
    # `for L in` list so this is not one more hand-synced level list.
    levels="$(step_block 1 | sed -n 's/^for L in \(.*\); do$/\1/p')"
    export GH_FIXTURE="$WORKDIR/issues.json"
    i=0; rows=""; star_plain=""; star_all=""
    for t in $levels loom:curated ""; do
        i=$((i + 1))
        rows+="{\"n\":$i,\"t\":\"$t\"}"
        if [[ "$t" == loom:curated ]]; then cur=$i
        elif [[ -n "$t" ]]; then star_plain+="${i}1 "; star_all+="${i}1 ${i}2 "
        else bare=$i; fi
    done
    jq -sc '[.[] | .n as $n | .t as $t | (["", "journal", "external"] | to_entries[]) |
        {number: ($n * 10 + .key + 1), title: "t",
         labels: (["loom:issue", $t, .value] | map(select(. != "") | {name: .}))}]' \
        <<<"$rows" >"$GH_FIXTURE"

    # run_tier <step> <repo> -> surviving issue numbers, space-joined, sorted.
    run_tier() {
        (cd "$2" && PATH="$FAKE_BIN:$PATH" bash -c "$(step_block "$1")" 2>&1) \
            | sed 's/^#\([0-9]*\): t$/\1/' | sort -n | tr '\n' ' '
    }
    expect_tier() {
        local got; got="$(run_tier "$1" "$2")"
        if [[ "$got" == "$3" ]]; then pass "$4"; else fail "$4 — expected '$3', got '$got'"; fi
    }
    sorted() { printf '%s\n' "$@" | sort -n | tr '\n' ' '; }

    if [[ "$(wc -w <<<"$levels")" -ge 3 ]]; then
        pass "Step 1 iterates $(wc -w <<<"$levels" | tr -d ' ') priority-level labels"
    else
        fail "could not read Step 1's priority-level list from builder.md: '$levels'"
    fi
    # shellcheck disable=SC2086  # deliberate word-splitting of number lists
    {
        # Configured repo: the skip-labeled twin never survives, in ANY tier.
        expect_tier 1 "$JOURNAL_REPO" "$(sorted $star_plain)" \
            "tier 1: a starred issue with a configured skip label is dropped, at every level (AC2)"
        expect_tier 2 "$JOURNAL_REPO" "$(sorted ${cur}1)" \
            "tier 2: a curated issue with a configured skip label is dropped (AC3)"
        expect_tier 3 "$JOURNAL_REPO" "$(sorted $star_plain ${bare}1)" \
            "tier 3: uncurated kept, skip-labeled dropped, curated excluded (AC4)"
        # Unconfigured repo: unchanged except that `external` drops out.
        expect_tier 1 "$NO_CONFIG_REPO" "$(sorted $star_all)" \
            "tier 1, no extraSkipLabels: only the external issue is dropped (AC5)"
        expect_tier 2 "$NO_CONFIG_REPO" "$(sorted ${cur}1 ${cur}2)" \
            "tier 2, no extraSkipLabels: only the external issue is dropped (AC5)"
        expect_tier 3 "$NO_CONFIG_REPO" "$(sorted $star_all ${bare}1 ${bare}2)" \
            "tier 3, no extraSkipLabels: only the external issue is dropped (AC5)"
    }

    # AC6: the interval reminder must not restate a tier query without the filter.
    interval="$(jq -r '.defaultIntervalPrompt' "$BUILDER_JSON")"
    if [[ "$interval" != *"gh issue list"* || "$interval" == *"skip-labels.sh"* ]] \
        && [[ "$interval" != *'--label="loom:issue"'* ]]; then
        pass "builder.json's interval prompt carries no unfiltered tier query (AC6)"
    else
        fail "builder.json's defaultIntervalPrompt restates an unfiltered gh issue list query"
    fi
else
    echo "note: jq not installed — skipping the #8911 tier checks" >&2
fi

echo ""
echo "=== Results: $passed passed, $failed failed ==="
if [[ "$failed" -gt 0 ]]; then
    exit 1
fi
exit 0
