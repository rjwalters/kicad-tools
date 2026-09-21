#!/usr/bin/env bash
# heredoc-body-safety.sh — shared STATIC scan for the #7508 bash-3.2
# heredoc-in-command-substitution parser trap, used by the watchdog test
# suites (test-loom-daemon-watchdog.sh, test-loom-daemon-watchdog-dedup.sh).
#
# The trap (#7508): wrapping a heredoc in `$(...)` makes bash 3.2 (macOS's
# stock, pre-GPLv3 /bin/bash) mis-track quoting THROUGH the heredoc body while
# its lexer textually scans for the closing `)`. A bare apostrophe anywhere in
# the body, or a `#` sharing a physical line with a backtick, is then misread
# as opening a region it can never close — `bad substitution: no closing )` /
# `unexpected EOF`, and an empty body. `read -r -d '' var <<EOF` reads the
# heredoc with no `$(...)` wrapper at all and never enters that scan, so it is
# safe whatever punctuation the prose contains.
#
# Why this file exists (#7834): the scan started life inline in
# test-loom-daemon-watchdog.sh, where it sliced the body with an awk pattern
# that only matched openers ENDING in `<<EOF`. The #7508 fix had already moved
# escalate_peer_coordination_degraded() to `IFS= read -r -d '' body <<EOF ||
# true` — the line ends in `|| true`, the pattern never fired, the slice came
# back EMPTY, and both of that function's assertions passed VACUOUSLY. Two
# properties here are the structural fix for that whole class:
#
#   1. Opener detection is shape-agnostic (anything after `<<EOF` is fine), and
#   2. an empty slice is a HARD FAILURE, never a silent pass — so a future
#      opener shape this scan does not know about fails loudly instead of
#      quietly disabling the check.
#
# Why the prose rules are scoped by construction rather than applied to every
# body: the two trigger shapes are only dangerous inside the `$(...)` wrapper.
# Demanding trigger-free prose from a `read -d ''` body constrains ordinary
# alert text (every "`foo` (#1234)" reference is a backtick+`#` line) to dodge
# a bug that construction cannot hit, and a failure message claiming it
# "reintroduces the bash-3.2 parse trap" would simply be false. Instead a
# `read -d ''` body must prove its CONSTRUCTION, and a body that regresses to
# `$(cat <<EOF` is reclassified and gets the strict prose rules applied to it
# at that moment — which is exactly when they start to matter.
#
# Sourced, not executed. The caller provides pass()/fail().

# heredoc_func_body <file> <fn>
# Prints the source lines of function <fn> in <file> (its `NAME() {` line
# through the matching `^}` at column 0 — the brace style used throughout the
# scripts this scans).
heredoc_func_body() {
    awk -v fn="$2" '
        $0 ~ "^"fn"\\(\\)" { printing = 1 }
        printing { print }
        printing && /^}/ { exit }
    ' "$1"
}

# _heredoc_opener_lines <func-source>
# Prints every heredoc-opening line in <func-source>. Comment lines are skipped
# FIRST: the #7508 rationale comments quote `body="$(cat <<EOF ... EOF)"` in
# prose, and a scan that took one of those as its opener would slice the
# comment block instead of the body.
_heredoc_opener_lines() {
    printf '%s\n' "$1" | awk '
        /^[[:space:]]*#/ { next }
        /<<-?'"'"'?EOF'"'"'?([^[:alnum:]_]|$)/ { print }
    '
}

# _heredoc_prose <func-source>
# Prints the heredoc BODY lines (the issue-body prose, not the surrounding
# bash) of every heredoc in <func-source>.
_heredoc_prose() {
    printf '%s\n' "$1" | awk '
        !inside && /^[[:space:]]*#/ { next }
        !inside && /<<-?'"'"'?EOF'"'"'?([^[:alnum:]_]|$)/ { inside = 1; next }
        inside && /^[[:space:]]*EOF$/ { inside = 0; next }
        inside { print }
    '
}

# heredoc_body_verdict <file> <fn>
# Prints `<construction>|<flags>|<prose-line-count>`:
#   construction: none        no heredoc opener found in the function
#                 cmdsubst    at least one opener is wrapped in `$(...)`
#                 read-d      every opener uses `read ... -d '' ... <<`
#                 unknown     an opener shape this scan cannot classify
#   flags:        `-`, or a comma-separated subset of
#                 {apostrophe,hash-backtick} found in the prose
#   count:        number of prose lines scanned (0 ⇒ the scan found nothing,
#                 which callers MUST treat as a failure, not a pass)
heredoc_body_verdict() {
    local body openers prose construction flags count
    body="$(heredoc_func_body "$1" "$2")"
    openers="$(_heredoc_opener_lines "$body")"
    prose="$(_heredoc_prose "$body")"

    if [[ -z "$openers" ]]; then
        construction="none"
    elif printf '%s\n' "$openers" | grep -q '[$]('; then   # literal `$(`, bracketed so it reads as text to humans and linters alike
        construction="cmdsubst"
    elif [[ "$(printf '%s\n' "$openers" | grep -Ec "read ([-a-zA-Z]+ )*-d ''")" == "$(printf '%s\n' "$openers" | grep -c '')" ]]; then
        construction="read-d"
    else
        construction="unknown"
    fi

    flags=""
    if printf '%s\n' "$prose" | grep -q "'"; then
        flags="apostrophe"
    fi
    if printf '%s\n' "$prose" | grep '`' | grep -q '#'; then
        flags="${flags:+$flags,}hash-backtick"
    fi
    [[ -n "$flags" ]] || flags="-"

    if [[ -z "$prose" ]]; then
        count=0
    else
        count="$(printf '%s\n' "$prose" | grep -c '')"
    fi

    printf '%s|%s|%s\n' "$construction" "$flags" "$count"
}

# check_heredoc_body_safety <file> <fn> <label>
# Asserts (via the caller's pass()/fail()) that <fn>'s heredoc body was
# actually located, that its construction is one this scan understands, and —
# for the `$(...)`-wrapped construction only — that the prose carries neither
# bash-3.2 trigger shape.
check_heredoc_body_safety() {
    local file="$1" fn="$2" label="$3" verdict construction flags count
    verdict="$(heredoc_body_verdict "$file" "$fn")"
    construction="${verdict%%|*}"
    count="${verdict##*|}"
    flags="${verdict#*|}"
    flags="${flags%|*}"

    # Anti-vacuity guard (#7834): without this, an opener shape the scan does
    # not recognise leaves `count` at 0 and every assertion below passes
    # without ever having read a byte of the body.
    if [[ "$count" -gt 0 ]]; then
        pass "#7508 static: $label heredoc body located by the scan ($count prose lines)"
    else
        fail "#7508 static: $label heredoc body was NOT located -- the opener shape changed and this scan would pass vacuously (#7834)"
        return 1
    fi

    case "$construction" in
        read-d)
            pass "#7508 static: $label builds its body via read -d '' -- no \$(...) wrapper, so the bash-3.2 lexer trap cannot fire regardless of prose"
            return 0
            ;;
        cmdsubst)
            pass "#7508 static: $label builds its body via \$(...)-wrapped heredoc -- the bash-3.2 trigger-shape rules apply and are enforced below"
            ;;
        *)
            fail "#7508 static: $label uses a heredoc construction this scan cannot classify ($(_heredoc_opener_lines "$(heredoc_func_body "$file" "$fn")" | tr '\n' ';')) -- enforcing the strict prose rules as a fallback"
            ;;
    esac

    if [[ "$flags" != *apostrophe* ]]; then
        pass "#7508 static: $label heredoc body has no bare apostrophe"
    else
        fail "#7508 static: $label heredoc body contains a bare apostrophe -- reintroduces the bash-3.2 parse trap ($(_heredoc_prose "$(heredoc_func_body "$file" "$fn")" | grep -n "'"))"
    fi
    if [[ "$flags" != *hash-backtick* ]]; then
        pass "#7508 static: $label heredoc body has no backtick+# sharing a line"
    else
        fail "#7508 static: $label heredoc body has a backtick and # on the same line -- reintroduces the bash-3.2 parse trap ($(_heredoc_prose "$(heredoc_func_body "$file" "$fn")" | grep '`' | grep -n '#'))"
    fi
}

# check_heredoc_scan_selftest
# Negative control (#7834): drives the scan over synthetic fixtures whose
# outcomes are known, so a regression that makes the scan blind again — the
# exact failure this file was extracted to fix — is itself caught. Without
# this, "the assertions pass" and "the assertions never ran" look identical.
# pass()/fail() are shadowed inside a subshell to capture what the check WOULD
# report without polluting the caller's counters.
check_heredoc_scan_selftest() {
    local fixture out
    fixture="$(mktemp "${TMPDIR:-/tmp}/heredoc-scan-fixture.XXXXXX")"
    cat > "$fixture" <<'FIXTURE'
read_d_with_triggers() {
    # A comment quoting body="$(cat <<EOF ... EOF)" must not be mistaken
    # for the opener.
    IFS= read -r -d '' body <<EOF || true
This host's peer-claim path (Safehouse, #6157)
See \`advertised\` (#1270) for the suspected cause.
EOF
}

cmdsubst_with_triggers() {
    body="$(cat <<EOF
This host's peer-claim path (Safehouse, #6157)
See \`advertised\` (#1270) for the suspected cause.
EOF
)"
}

cmdsubst_clean() {
    body="$(cat <<EOF
This host has a peer-claim path (Safehouse, see 6157)
See the advertised counter for the suspected cause.
EOF
)"
}

no_heredoc_at_all() {
    body="nothing to scan here"
}
FIXTURE

    _selftest_verdict() { # <fn> <expected> <label>
        local got
        got="$(heredoc_body_verdict "$fixture" "$1")"
        if [[ "$got" == "$2" ]]; then
            pass "#7834 self-test: $3"
        else
            fail "#7834 self-test: $3 (expected '$2', got '$got')"
        fi
    }
    # The load-bearing one: a `read -d '' ... <<EOF || true` opener must be
    # SEEN (count 2, both triggers detected) -- pre-#7834 this came back
    # `none|-|0` and every downstream assertion passed vacuously.
    _selftest_verdict read_d_with_triggers "read-d|apostrophe,hash-backtick|2" \
        "a read -d '' <<EOF || true opener is detected and its body scanned"
    _selftest_verdict cmdsubst_with_triggers "cmdsubst|apostrophe,hash-backtick|2" \
        "a \$(cat <<EOF) opener is detected and its trigger shapes flagged"
    _selftest_verdict cmdsubst_clean "cmdsubst|-|2" \
        "a trigger-free \$(cat <<EOF) body reports no flags"
    _selftest_verdict no_heredoc_at_all "none|-|0" \
        "a function with no heredoc reports an empty scan"
    unset -f _selftest_verdict

    # Negative control proper: the strict prose rules must actually FAIL on a
    # vulnerable body, and the vacuity guard must actually FAIL on an empty
    # scan. pass/fail are redefined only inside these subshells.
    out="$(pass() { echo "PASS: $1"; }; fail() { echo "FAIL: $1"; }
        check_heredoc_body_safety "$fixture" cmdsubst_with_triggers "fixture")"
    if [[ "$(grep -c '^FAIL: ' <<< "$out")" == "2" ]] \
        && grep -q 'bare apostrophe' <<< "$out" && grep -q 'backtick and # on the same line' <<< "$out"; then
        pass "#7834 self-test: both strict prose assertions FAIL on a vulnerable \$(cat <<EOF) body"
    else
        fail "#7834 self-test: expected two prose failures on a vulnerable body, got: $(tr '\n' ';' <<< "$out")"
    fi

    out="$(pass() { echo "PASS: $1"; }; fail() { echo "FAIL: $1"; }
        check_heredoc_body_safety "$fixture" no_heredoc_at_all "fixture")"
    if grep -q '^FAIL: .*NOT located' <<< "$out"; then
        pass "#7834 self-test: an empty scan FAILS loudly instead of passing vacuously"
    else
        fail "#7834 self-test: expected a vacuity failure on an unscannable body, got: $(tr '\n' ';' <<< "$out")"
    fi

    out="$(pass() { echo "PASS: $1"; }; fail() { echo "FAIL: $1"; }
        check_heredoc_body_safety "$fixture" read_d_with_triggers "fixture")"
    if [[ "$(grep -c '^FAIL: ' <<< "$out")" == "0" ]] && grep -q 'read -d' <<< "$out"; then
        pass "#7834 self-test: a read -d '' body with trigger-shaped prose is scanned, and passes on construction"
    else
        fail "#7834 self-test: expected a read -d '' body to pass on construction, got: $(tr '\n' ';' <<< "$out")"
    fi

    rm -f "$fixture"
}
