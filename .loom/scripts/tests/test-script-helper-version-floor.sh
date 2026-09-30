#!/usr/bin/env bash
# test-script-helper-version-floor.sh — the marker-driven daemon-version
# preflight (#8385, follow-up to #8285), exercised through the two entry points
# that carry it: `loom_exec_script_helper` (lib/script-helper.sh) and
# `skip-labels.sh`'s standalone call. The preflight itself lives in
# lib/locate-daemon-bin.sh, beside the resolver whose answer it interrogates;
# this suite deliberately drives it from the OUTSIDE, through real stubs, so it
# pins the contract a stub depends on rather than the function's current home.
#
# THE INCIDENT THIS CLOSES. On 2026-09-19 `skip-labels.sh` — a thin stub whose
# only statement is an `exec` into `loom-daemon skip-labels` — met a host whose
# resolved binary was 0.19.179, predating the subcommand (landed in 0.19.186,
# commit 268777b7). The ONLY thing the operator saw was clap's own
#
#     error: unrecognized subcommand 'skip-labels'
#
# which names neither the version to roll to nor the command to roll with.
# #8285 gave merge-pr.sh an actionable refusal for exactly this class; every
# other stub still had the bare clap error. This suite pins the shared fix:
# the CALLING stub's own `# requires-daemon:` marker is read back, and the call
# refuses with the same four facts merge-pr.sh's hint carries
# (floor, what the binary actually is, the `--fetch` roll, the LOOM_DAEMON_BIN
# pin) BEFORE exec'ing into a binary that cannot serve the call.
#
# WHAT IS PINNED (in priority order — the first three are the whole contract):
#
#   1. INERTNESS. A stub that declares no marker, or declares `optional`, or
#      declares a floor for a DIFFERENT subcommand, behaves byte-identically to
#      today: the exec happens and clap's error is what surfaces. Adoption is
#      opt-in, one file at a time, which is the only reason this can land
#      across a family of eleven stubs at once.
#   2. EXIT-CODE DISCIPLINE. Several of these subcommands use non-zero codes as
#      DATA (`resolve-model --tier` exits 3 for "no mapping";
#      `detect-dependency-cycle` exits 1 for "cycle found"), which is why
#      LOOM_SCRIPT_HELPER_MISSING_RC exists. A version refusal MUST use that
#      reserved could-not-run code, never a code the caller reads as an answer.
#   3. FAIL OPEN, NOT CLOSED, WHEN THE VERSION IS UNREADABLE. This preflight's
#      job is to turn an unactionable error into an actionable one — it is NOT
#      a safety gate (unlike merge-pr.sh's fail-CLOSED guards, where an empty
#      answer would silently close an unfinished issue). If `--version` cannot
#      be parsed, refusing would invent a NEW failure mode on a host whose
#      binary is probably fine; exec'ing reproduces exactly today's behaviour,
#      and the subcommand itself still refuses correctly when truly absent.
#
#   …plus numeric (not lexicographic) semver comparison — 0.19.9 < 0.19.10 and
#   0.19.100 > 0.19.99 — and the two live adopters: skip-labels.sh's own floor,
#   and that it is still a Shape-A stub afterwards.
#
# HERMETIC: no real loom-daemon is needed or wanted. The whole subject is
# behaviour against a binary that does NOT have the subcommand, so every
# fixture binary is a shell script that prints a version and rejects anything
# else exactly as clap does.
#
# Usage:
#   bash defaults/scripts/tests/test-script-helper-version-floor.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$SCRIPTS_DIR/../.." && pwd)"
HELPER_LIB="$SCRIPTS_DIR/lib/script-helper.sh"
SKIP_LABELS="$SCRIPTS_DIR/skip-labels.sh"
CHECKER="$REPO_ROOT/scripts/check-daemon-subcommand-versions.sh"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

assert_contains() {
    local haystack="$1" needle="$2" msg="$3"
    TESTS_RUN=$((TESTS_RUN + 1))
    if grep -qF -- "$needle" <<<"$haystack"; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: $msg"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: $msg"
        echo "    expected substring: '$needle'"
        echo "    in: $haystack"
    fi
}

assert_not_contains() {
    local haystack="$1" needle="$2" msg="$3"
    TESTS_RUN=$((TESTS_RUN + 1))
    if grep -qF -- "$needle" <<<"$haystack"; then
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: $msg"
        echo "    unexpected substring: '$needle'"
        echo "    in: $haystack"
    else
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: $msg"
    fi
}

assert_eq() {
    local expected="$1" actual="$2" msg="$3"
    TESTS_RUN=$((TESTS_RUN + 1))
    if [[ "$expected" == "$actual" ]]; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: $msg"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: $msg"
        echo "    expected: '$expected'"
        echo "    actual:   '$actual'"
    fi
}

if [[ ! -f "$HELPER_LIB" ]]; then
    echo -e "${RED}FATAL${NC}: $HELPER_LIB not found" >&2
    exit 2
fi

WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

# --- Fixture binaries -------------------------------------------------------
# Each knows `--version` and rejects every subcommand the way clap does
# (exit 2, "error: unrecognized subcommand"). `widget` is served only by the
# binaries that are supposed to be new enough, and it exits 3 — a DATA code, so
# any wrapper that remapped the child's status would be caught here.
_fake_daemon() { # <path> <version-line-or-EMPTY> [serves-widget]
    local path="$1" version="$2" serves="${3:-no}"
    {
        echo '#!/usr/bin/env bash'
        echo 'if [[ "${1:-}" == "--version" ]]; then'
        if [[ -n "$version" ]]; then
            printf '  echo %q\n' "$version"
            echo '  exit 0'
        else
            echo '  exit 1'
        fi
        echo 'fi'
        if [[ "$serves" == "yes" ]]; then
            echo 'if [[ "${1:-}" == "widget" ]]; then'
            echo '  shift'
            echo '  echo "RAN widget $*"'
            echo '  exit 3'
            echo 'fi'
        fi
        echo 'echo "error: unrecognized subcommand '"'"'${1:-}'"'"'" >&2'
        echo 'exit 2'
    } >"$path"
    chmod +x "$path"
}

_fake_daemon "$WORKDIR/daemon-0.16.0"   "loom-daemon 0.16.0 (commit 0160000, built 2026-07-29T00:00:00Z)"
_fake_daemon "$WORKDIR/daemon-0.19.100" "loom-daemon 0.19.100 (commit deadbee, built 2026-09-01T00:00:00Z)"
_fake_daemon "$WORKDIR/daemon-0.19.200" "loom-daemon 0.19.200 (commit cafebab, built 2026-09-20T00:00:00Z)" yes
_fake_daemon "$WORKDIR/daemon-0.19.240" "loom-daemon 0.19.240 (commit f00ba12, built 2026-09-21T00:00:00Z)" yes
_fake_daemon "$WORKDIR/daemon-0.19.9"   "loom-daemon 0.19.9 (commit 0000009, built 2026-08-01T00:00:00Z)"
_fake_daemon "$WORKDIR/daemon-0.19.99"  "loom-daemon 0.19.99 (commit 0000099, built 2026-08-02T00:00:00Z)"
_fake_daemon "$WORKDIR/daemon-garbled"  "loom-daemon (a development build)"
_fake_daemon "$WORKDIR/daemon-mute"     ""

# --- Fixture stubs ----------------------------------------------------------
# Real stubs in miniature: `set -euo pipefail` like production, source the REAL
# lib, one `loom_exec_script_helper` call. The marker is written through a
# substitution for the same reason check-daemon-subcommand-versions.sh does it
# — a literal one here would make THIS file scan as a script with its own
# undeclared daemon dependency.
_stub() { # <path> <marker-body-or-EMPTY> [extra-line]
    local path="$1" marker="$2" extra="${3:-}"
    {
        echo '#!/usr/bin/env bash'
        [[ -n "$marker" ]] && echo "# @RD@: $marker"
        echo 'set -euo pipefail'
        printf 'source %q\n' "$HELPER_LIB"
        [[ -n "$extra" ]] && echo "$extra"
        echo 'loom_exec_script_helper widget "$@"'
    } | sed 's/@RD@/requires-daemon/' >"$path"
    chmod +x "$path"
}

_stub "$WORKDIR/declared.sh"      "widget >= 0.19.200   the floor under test"
_stub "$WORKDIR/undeclared.sh"    ""
_stub "$WORKDIR/optional.sh"      "widget optional   degrades to the fixed list"
_stub "$WORKDIR/other-sub.sh"     "gadget >= 0.19.200   a DIFFERENT subcommand"
_stub "$WORKDIR/declared-rc2.sh"  "widget >= 0.19.200   exit 2 is this entry point's could-not-run code" \
                                  'LOOM_SCRIPT_HELPER_MISSING_RC=2'
_stub "$WORKDIR/floor-19-10.sh"   "widget >= 0.19.10   numeric, not lexicographic"
_stub "$WORKDIR/floor-19-99.sh"   "widget >= 0.19.99   numeric, not lexicographic"

# Guard against the substitution silently producing a non-marker (which would
# turn most assertions below into vacuous passes).
assert_eq "# requires-daemon: widget >= 0.19.200   the floor under test" \
    "$(sed -n '2p' "$WORKDIR/declared.sh")" \
    "the fixture substitution really produces a requires-daemon marker line"

# run_stub <stub> <bin-env-name> <bin> -- sets OUT (stdout+stderr) and RC.
#
# Deliberately NOT `OUT="$(run_stub …)"`: a function that assigns RC inside a
# command substitution assigns it in that SUBSHELL, so every exit-code
# assertion here would silently read the parent's stale 0 — a whole class of
# vacuous passes. Assigning both in the caller's own shell is the fix.
OUT=""
RC=0
run_stub() {
    local stub="$1" var="$2" bin="$3"
    OUT="$(env "$var=$bin" "$stub" --flag 2>&1)"; RC=$?
}

echo ""
echo "=== test-script-helper-version-floor.sh (#8385) ==="
echo ""
echo "A declared floor against a STALE binary refuses, actionably…"

run_stub "$WORKDIR/declared.sh" LOOM_DAEMON_BIN "$WORKDIR/daemon-0.19.100"
assert_eq "1" "$RC" "refuses with LOOM_SCRIPT_HELPER_MISSING_RC's default (1), not clap's 2"
assert_contains "$OUT" "0.19.200" "names the declared FLOOR"
assert_contains "$OUT" "0.19.100" "names what the resolved binary actually reports"
assert_contains "$OUT" "widget" "names the subcommand that cannot be served"
assert_contains "$OUT" "cli/loom-daemon-update.sh --fetch" "names the artifact-first roll command for THIS host"
assert_contains "$OUT" "LOOM_DAEMON_BIN" "names the LOOM_DAEMON_BIN pin as the fallback"
assert_contains "$OUT" "cargo build --release -p loom-daemon" "names the source-build escape hatch"
assert_contains "$OUT" "requires-daemon: widget >= 0.19.200" "quotes the marker it read, so the floor has one visible source"
assert_not_contains "$OUT" "unrecognized subcommand" \
    "the bare clap error is REPLACED, not merely prefixed (that error is what #8385 exists to remove)"

echo ""
echo "…and the same refusal rides the LOOM_DAEMON_SELF_BIN seam…"

run_stub "$WORKDIR/declared.sh" LOOM_DAEMON_SELF_BIN "$WORKDIR/daemon-0.19.100"
assert_eq "1" "$RC" "the LOOM_DAEMON_SELF_BIN fast path is preflighted too (#8134's seam is not a bypass)"
assert_contains "$OUT" "0.19.200" "…and names the floor there as well"

echo ""
echo "A binary AT or ABOVE the floor execs normally (>=, not >)…"

run_stub "$WORKDIR/declared.sh" LOOM_DAEMON_BIN "$WORKDIR/daemon-0.19.200"
assert_eq "3" "$RC" "exactly-at-the-floor passes, and the child's DATA exit code (3) reaches the caller unmodified"
assert_contains "$OUT" "RAN widget --flag" "arguments are forwarded unchanged"
assert_not_contains "$OUT" "requires-daemon" "no refusal text leaks onto a healthy call"

run_stub "$WORKDIR/declared.sh" LOOM_DAEMON_BIN "$WORKDIR/daemon-0.19.240"
assert_eq "3" "$RC" "a binary above the floor passes"

echo ""
echo "Inertness: a stub that declares nothing behaves EXACTLY as before…"

run_stub "$WORKDIR/undeclared.sh" LOOM_DAEMON_BIN "$WORKDIR/daemon-0.19.100"
assert_eq "2" "$RC" "no marker => the exec happens and clap's own exit code surfaces"
assert_contains "$OUT" "unrecognized subcommand" "no marker => today's bare clap error, unchanged"

run_stub "$WORKDIR/optional.sh" LOOM_DAEMON_BIN "$WORKDIR/daemon-0.19.100"
assert_eq "2" "$RC" "an 'optional' marker declares graceful degradation, so it must NOT hard-refuse"
assert_contains "$OUT" "unrecognized subcommand" "…and leaves the call to the stub's own probe/fallback"

run_stub "$WORKDIR/other-sub.sh" LOOM_DAEMON_BIN "$WORKDIR/daemon-0.19.100"
assert_eq "2" "$RC" "a floor declared for a DIFFERENT subcommand never gates this one"

OUT="$(env LOOM_SKIP_DAEMON_VERSION_PREFLIGHT=1 LOOM_DAEMON_BIN="$WORKDIR/daemon-0.19.100" \
        "$WORKDIR/declared.sh" --flag 2>&1)"; RC=$?
assert_eq "2" "$RC" "LOOM_SKIP_DAEMON_VERSION_PREFLIGHT=1 restores the pre-#8385 path wholesale"

echo ""
echo "Exit-code discipline: the refusal uses the entry point's reserved code…"

run_stub "$WORKDIR/declared-rc2.sh" LOOM_DAEMON_BIN "$WORKDIR/daemon-0.19.100"
assert_eq "2" "$RC" "LOOM_SCRIPT_HELPER_MISSING_RC=2 is honored — never a code the caller reads as an answer"
assert_contains "$OUT" "0.19.200" "…and the message is the same actionable one"

echo ""
echo "Version comparison is numeric, not lexicographic…"

run_stub "$WORKDIR/floor-19-10.sh" LOOM_DAEMON_BIN "$WORKDIR/daemon-0.19.9"
assert_eq "1" "$RC" "0.19.9 < 0.19.10 (a string compare would wrongly pass this)"

run_stub "$WORKDIR/floor-19-99.sh" LOOM_DAEMON_BIN "$WORKDIR/daemon-0.19.100"
assert_eq "2" "$RC" "0.19.100 > 0.19.99 (a string compare would wrongly refuse this)"

echo ""
echo "Fail OPEN when the version cannot be read — never a NEW failure mode…"

run_stub "$WORKDIR/declared.sh" LOOM_DAEMON_BIN "$WORKDIR/daemon-garbled"
assert_eq "2" "$RC" "an unparseable --version proceeds to the exec (today's behaviour), it does not invent a refusal"
assert_contains "$OUT" "unrecognized subcommand" "…so the subcommand's own error is still what surfaces"

run_stub "$WORKDIR/declared.sh" LOOM_DAEMON_BIN "$WORKDIR/daemon-mute"
assert_eq "2" "$RC" "a --version that exits non-zero proceeds to the exec too"

echo ""
echo "The no-binary-at-all path is untouched by the preflight…"

mkdir -p "$WORKDIR/empty-path" "$WORKDIR/empty-home"
OUT="$(env PATH="$WORKDIR/empty-path:/usr/bin:/bin" HOME="$WORKDIR/empty-home" \
        LOOM_DAEMON_BIN="" LOOM_DAEMON_SELF_BIN="" \
        "$WORKDIR/declared.sh" --flag 2>&1)"; RC=$?
assert_eq "1" "$RC" "no resolvable binary still exits LOOM_SCRIPT_HELPER_MISSING_RC (the #4275 contract)"
assert_contains "$OUT" "loom-daemon not found" "…with the original not-found message, not a version refusal"

echo ""
echo "Live adopter: skip-labels.sh — the stub that motivated #8285's follow-up…"

if [[ -f "$SKIP_LABELS" ]]; then
    DECLARED_MIN="$(sed -n '/^[[:space:]]*#[[:space:]]*requires-daemon:[[:space:]]*skip-labels[[:space:]][[:space:]]*>=/{s/^.*>=[[:space:]]*\([0-9][0-9.]*\).*$/\1/p;q;}' "$SKIP_LABELS")"
    assert_eq "0.19.186" "$DECLARED_MIN" \
        "skip-labels.sh declares the version the subcommand actually landed in (268777b7 merged at VERSION 0.19.185; the bump after it was 0.19.186)"

    OUT="$(env LOOM_DAEMON_SELF_BIN="$WORKDIR/daemon-0.19.100" "$SKIP_LABELS" --lines 2>&1)"; RC=$?
    assert_eq "1" "$RC" "a stale binary makes skip-labels.sh refuse rather than emit an empty/garbled label list"
    assert_contains "$OUT" "0.19.186" "the refusal names the floor (was: 'error: unrecognized subcommand skip-labels')"
    assert_contains "$OUT" "cli/loom-daemon-update.sh --fetch" "the refusal names the roll command"
    assert_contains "$OUT" "LOOM_DAEMON_BIN" "the refusal names the pin fallback"
    assert_not_contains "$OUT" "unrecognized subcommand" "the bare clap error is gone from the motivating case itself"

    # Shape-A stub cap (scripts/check-shell-allowlist.sh): under 40 code lines
    # AND the last code line is `exec`. Asserted here as well as in that gate so
    # the coupling is visible from the file that depends on it.
    CODE_LINES="$(grep -cvE '^[[:space:]]*(#|$)' "$SKIP_LABELS")"
    LAST_CODE="$(grep -vE '^[[:space:]]*(#|$)' "$SKIP_LABELS" | tail -1 | sed 's/^[[:space:]]*//')"
    TESTS_RUN=$((TESTS_RUN + 1))
    if [[ "$CODE_LINES" -lt 40 ]] && [[ "$LAST_CODE" == exec\ * || "$LAST_CODE" == "exec" ]]; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: skip-labels.sh keeps its 'stub' allowlist category ($CODE_LINES code lines, ends in exec)"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: skip-labels.sh no longer satisfies the Shape-A cap ($CODE_LINES code lines, last: '$LAST_CODE')"
    fi
else
    echo "  SKIP: $SKIP_LABELS not present"
fi

echo ""
echo "The declared floors are sane, and the CI gate agrees they are declared…"

# A floor ABOVE this repo's VERSION is a typo or a copy-paste, and would make
# every host refuse forever — the same sanity rule
# check-daemon-subcommand-versions.sh applies, asserted here too because this
# suite is what a consumer repo runs when the gate script is not shipped.
REPO_VERSION="$(tr -d '[:space:]' <"$REPO_ROOT/VERSION" 2>/dev/null || echo "")"
_min="${DECLARED_MIN:-}"
if [[ -n "$_min" ]]; then
    TESTS_RUN=$((TESTS_RUN + 1))
    # `sort -V` over a here-string, then first line by parameter expansion: no
    # pipe, so a pipefail + early-exit-consumer SIGPIPE (#7790) is impossible.
    _sorted="$(sort -V <<<"$_min"$'\n'"$REPO_VERSION")"
    if [[ "$_min" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] \
       && { [[ -z "$REPO_VERSION" ]] || [[ "${_sorted%%$'\n'*}" == "$_min" ]]; }; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: skip-labels.sh's floor '$_min' is semver and <= this repo's VERSION ($REPO_VERSION)"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: skip-labels.sh's floor '$_min' is not semver, or is above VERSION ($REPO_VERSION)"
    fi
fi

if [[ -f "$CHECKER" && -f "$SKIP_LABELS" ]]; then
    : >"$WORKDIR/empty-baseline.txt"
    GATE_OUT="$(bash "$CHECKER" --baseline "$WORKDIR/empty-baseline.txt" "$SKIP_LABELS" 2>&1)"; GATE_RC=$?
    assert_eq "0" "$GATE_RC" \
      "check-daemon-subcommand-versions.sh passes skip-labels.sh with an EMPTY baseline (declared, no longer grandfathered)"
    [[ "$GATE_RC" -eq 0 ]] || echo "    gate said: $GATE_OUT"
else
    echo "  SKIP: $CHECKER not present (consumer repo)"
fi

echo ""
echo "Live adopters: the remaining eleven loom_exec_script_helper stubs (#8484)…"

# One block per newly-adopting stub, mirroring the skip-labels.sh block above.
# Four things are pinned per stub, and all four are properties of the STUB, not
# of the preflight function (which the fixture tests above already cover):
#
#   1. the floor the file DECLARES is the one established from history — read
#      back with the same sed grammar the preflight itself uses, so a marker
#      that stops parsing fails here rather than silently going inert;
#   2. that floor is semver and not above this repo's VERSION (a floor above it
#      is a typo that would make every host refuse forever);
#   3. against a binary BELOW the floor the stub really refuses, with the four
#      actionable facts and WITHOUT clap's bare "unrecognized subcommand" —
#      exercised through the real file, not a fixture copy of it;
#   4. the refusal exits the code that entry point reserves for "could not run"
#      — 2 where the subcommand uses 1 as DATA, 1 where it does not. This is
#      the assertion that would catch a future `LOOM_SCRIPT_HELPER_MISSING_RC`
#      deletion, which is otherwise invisible until a caller misreads a
#      refusal as a verdict.
#
# …plus that check-daemon-subcommand-versions.sh now passes the file with an
# EMPTY baseline, i.e. the declaration really did replace the grandfather row.
: >"$WORKDIR/empty-baseline.txt"

# assert_adopter <basename> <subcommand> <floor> <stale-bin> <want-rc> <gate-baseline> [args…]
assert_adopter() {
    local name="$1" sub="$2" want_floor="$3" bin="$4" want_rc="$5" gate_baseline="$6"
    shift 6
    local path="$SCRIPTS_DIR/$name" got_floor sorted gate_out gate_rc

    if [[ ! -f "$path" ]]; then
        echo "  SKIP: $path not present"
        return 0
    fi

    got_floor="$(sed -n "/^[[:space:]]*#[[:space:]]*requires-daemon:[[:space:]]*${sub}[[:space:]][[:space:]]*>=/{s/^.*>=[[:space:]]*\([0-9][0-9.]*\).*\$/\1/p;q;}" "$path")"
    assert_eq "$want_floor" "$got_floor" "$name declares '$sub >= $want_floor' (the version the subcommand actually landed in)"

    TESTS_RUN=$((TESTS_RUN + 1))
    sorted="$(sort -V <<<"$want_floor"$'\n'"$REPO_VERSION")"
    if [[ "$want_floor" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] \
       && { [[ -z "$REPO_VERSION" ]] || [[ "${sorted%%$'\n'*}" == "$want_floor" ]]; }; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: $name's floor '$want_floor' is semver and <= VERSION ($REPO_VERSION)"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: $name's floor '$want_floor' is not semver, or is above VERSION ($REPO_VERSION)"
    fi

    OUT="$(env LOOM_DAEMON_SELF_BIN="$bin" "$path" "$@" 2>&1)"; RC=$?
    assert_eq "$want_rc" "$RC" "$name refuses on its reserved could-not-run code ($want_rc), never one the caller reads as an answer"
    assert_contains "$OUT" "$want_floor" "$name's refusal names the floor"
    assert_contains "$OUT" "cli/loom-daemon-update.sh --fetch" "$name's refusal names the roll command"
    assert_contains "$OUT" "LOOM_DAEMON_BIN" "$name's refusal names the pin fallback"
    assert_not_contains "$OUT" "unrecognized subcommand" "$name no longer surfaces the bare clap error"

    if [[ -f "$CHECKER" ]]; then
        gate_out="$(bash "$CHECKER" --baseline "$gate_baseline" "$path" 2>&1)"; gate_rc=$?
        assert_eq "0" "$gate_rc" "check-daemon-subcommand-versions.sh passes $name against a baseline with no '$sub' row (declared, no longer grandfathered)"
        [[ "$gate_rc" -eq 0 ]] || echo "    gate said: $gate_out"
    fi
}

# agent-metrics.sh keeps a SECOND, still-grandfathered pair (`stats`), so its
# gate baseline carries that row and only that row — an empty one would fail
# for a dependency #8484 deliberately did not declare.
printf '%s\t%s\n' "$SCRIPTS_DIR/agent-metrics.sh" "stats" >"$WORKDIR/agent-metrics-baseline.txt"

# Champion's dependency-classification family — one commit, one floor
# (91ebce3ea, #7952/#7953, merged at VERSION 0.19.98 → first shipped 0.19.99).
# All three already reserved 2, because 1 is a finding here.
assert_adopter detect-dependency-cycle.sh   detect-dependency-cycle   0.19.99 \
    "$WORKDIR/daemon-0.19.9" 2 "$WORKDIR/empty-baseline.txt" --issue 1
assert_adopter classify-dependency-block.sh classify-dependency-block 0.19.99 \
    "$WORKDIR/daemon-0.19.9" 2 "$WORKDIR/empty-baseline.txt" --issue 1
assert_adopter detect-startable-subset.sh   detect-startable-subset   0.19.99 \
    "$WORKDIR/daemon-0.19.9" 2 "$WORKDIR/empty-baseline.txt" --issue 1

# Curator's re-check fingerprints (71d1fbae3, #7961/#7969, merged at VERSION
# 0.19.103 → first shipped 0.19.104). Driven from 0.19.100, which is ABOVE the
# previous family's floor and below this one — so a lexicographic comparison
# would get this pair exactly backwards.
assert_adopter dep-recheck-fingerprint.sh   dep-recheck-fingerprint   0.19.104 \
    "$WORKDIR/daemon-0.19.100" 2 "$WORKDIR/empty-baseline.txt" decide --hash deadbeef

# The #4275/#4552 script-helper family — one commit (3f147e8f0), one floor.
# It landed AFTER v0.16.0 was tagged, so 0.17.0 (3e5a9439e) is the first
# version that shipped any of them, and 0.16.0 is the stale binary.
assert_adopter check-usage.sh       usage            0.17.0 \
    "$WORKDIR/daemon-0.16.0" 1 "$WORKDIR/empty-baseline.txt" --status
assert_adopter checkpoint.sh        checkpoint       0.17.0 \
    "$WORKDIR/daemon-0.16.0" 1 "$WORKDIR/empty-baseline.txt" stages
assert_adopter resolve-model.sh     resolve-model    0.17.0 \
    "$WORKDIR/daemon-0.16.0" 1 "$WORKDIR/empty-baseline.txt" opus
assert_adopter strip-ansi.sh        strip-ansi       0.17.0 \
    "$WORKDIR/daemon-0.16.0" 1 "$WORKDIR/empty-baseline.txt" --file /dev/null
assert_adopter sweep-experiment.sh  sweep-experiment 0.17.0 \
    "$WORKDIR/daemon-0.16.0" 1 "$WORKDIR/empty-baseline.txt" resolve-mode

# validate-phase.sh is the one stub in that family whose exit 1 is an ANSWER
# ("contract failed"), so #8484 set LOOM_SCRIPT_HELPER_MISSING_RC=2 on it at
# the same time. want-rc 2 is therefore the point of this line, not a detail.
assert_adopter validate-phase.sh    validate-phase   0.17.0 \
    "$WORKDIR/daemon-0.16.0" 2 "$WORKDIR/empty-baseline.txt" builder 1

# agent-metrics.sh declares `sweep-experiment` for its --model-experiment
# branch only; the `stats` pair below it stays grandfathered.
assert_adopter agent-metrics.sh     sweep-experiment 0.17.0 \
    "$WORKDIR/daemon-0.16.0" 1 "$WORKDIR/agent-metrics-baseline.txt" --model-experiment

echo ""
echo "…and a binary AT each floor still execs, so the floors are not merely 'always refuse'…"

# The mirror image of every block above: without this, a floor typo'd far into
# the future would pass all of them (it refuses, actionably, every time) and
# the suite would be measuring nothing but the refusal path.
OUT="$(env LOOM_DAEMON_SELF_BIN="$WORKDIR/daemon-0.19.240" "$SCRIPTS_DIR/resolve-model.sh" opus 2>&1)"; RC=$?
assert_not_contains "$OUT" "requires-daemon" "a binary above the 0.17.0 floor is not refused by resolve-model.sh"
assert_contains "$OUT" "unrecognized subcommand" "…it reaches the exec, where the fixture binary's own error is what surfaces"

OUT="$(env LOOM_DAEMON_SELF_BIN="$WORKDIR/daemon-0.19.240" "$SCRIPTS_DIR/dep-recheck-fingerprint.sh" decide --hash deadbeef 2>&1)"; RC=$?
assert_not_contains "$OUT" "requires-daemon" "a binary above the 0.19.104 floor is not refused by dep-recheck-fingerprint.sh"

echo ""
echo "────────────────────────────────"
if [[ "$TESTS_FAILED" -eq 0 ]]; then
    echo -e "${GREEN}Results: $TESTS_PASSED/$TESTS_RUN passed, 0 failed${NC}"
    exit 0
fi
echo -e "${RED}Results: $TESTS_PASSED/$TESTS_RUN passed, $TESTS_FAILED failed${NC}"
exit 1
