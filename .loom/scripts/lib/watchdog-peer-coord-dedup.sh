#!/usr/bin/env bash
# watchdog-peer-coord-dedup.sh — #7664 same-issue dedup window for the
# watchdog's peer-coordination escalation. Sourced by
# cli/loom-daemon-watchdog.sh (never executed directly).
#
# Why a separate file: loom-daemon-watchdog.sh sits at the File Size Ratchet
# (#7711) threshold, and the ratchet's rule is "put the new code in a sibling
# module and leave a dispatch arm behind" rather than grow a frozen file. The
# two functions here ARE that new code; the watchdog keeps a one-line call
# site for each.
#
# Contract: both functions run inside the watchdog's own shell (sourced, not
# exec'd) and read the watchdog's globals PEER_COORD_SENTINEL,
# PEER_COORD_COOLDOWN_STATE and PEER_COORD_SUMMARY. If this file is missing on
# an installed host, the watchdog's `declare -F` guards fall through — a repeat
# degradation files a FRESH issue and a recovery stamps flap-count 1 — the same
# fail-open contract as every other failure on this path: a genuine
# degradation is never silently dropped.
#
# LOOM_WATCHDOG_PEER_COORD_DEDUP_WINDOW_SECS (documented in the watchdog's
# --help): default 86400 (24h) — deliberately longer than the default 21600s
# (6h) #7258 cooldown so it covers at least one full cooldown cycle of slack.
# A non-numeric override fails open to the default, like every other numeric
# knob in the watchdog.
PEER_COORD_DEDUP_WINDOW_SECS="${LOOM_WATCHDOG_PEER_COORD_DEDUP_WINDOW_SECS:-86400}"
[[ "$PEER_COORD_DEDUP_WINDOW_SECS" =~ ^[0-9]+$ ]] || PEER_COORD_DEDUP_WINDOW_SECS=86400

# peer_coord_dedup_comment <cooldown_ts> <issue_ref> <flap_count> <elapsed>
#
# Called by escalate_peer_coordination_degraded() once the #7258 cooldown has
# elapsed, with the fields `read` back from PEER_COORD_COOLDOWN_STATE
# (`<epoch> <issue-ref> <flap-count>`; an old bare-epoch file leaves the last
# two empty) and the seconds elapsed since that epoch.
#
# If the excursion is still inside PEER_COORD_DEDUP_WINDOW_SECS (strict `<`,
# same boundary convention as the cooldown: exactly AT the window is
# window-elapsed) AND there is a usable prior issue reference AND `gh` is on
# PATH: comments on that issue with a bumped flap count, reopens it, re-arms
# PEER_COORD_SENTINEL against it, writes the bumped count back into the
# cooldown-state file, sets PEER_COORD_ESCALATE_NOTE for the caller's log
# line, and returns 0. Every other outcome — outside the window, no reference,
# no `gh`, a failed `gh issue comment` — returns 1 so the caller files fresh.
peer_coord_dedup_comment() {
    local cooldown_ts="$1" cooldown_issue_ref="$2" flap_count="$3" elapsed="$4"
    (( elapsed >= 0 && elapsed < PEER_COORD_DEDUP_WINDOW_SECS )) || return 1
    [[ -n "$cooldown_issue_ref" ]] || return 1
    command -v gh >/dev/null 2>&1 || return 1
    local hostname_str dedup_body
    [[ "$flap_count" =~ ^[0-9]+$ ]] || flap_count=1
    flap_count=$(( flap_count + 1 ))
    hostname_str="$(hostname 2>/dev/null || echo unknown-host)"
    # #7508-style construction (read -d '' <<EOF, no $(...) wrapper) — see the
    # rationale on escalate_peer_coordination_degraded()'s own body in the
    # watchdog: wrapping a heredoc in `$(...)` trips a bash 3.2 parser bug.
    IFS= read -r -d '' dedup_body <<EOF || true
peer-claim coordination has gone DEGRADED again on \`$hostname_str\` (${PEER_COORD_SUMMARY:-see the daemon peer-claims report}).

This is flap #${flap_count} since this tracking issue was first filed, landing within the ${PEER_COORD_DEDUP_WINDOW_SECS}s dedup window (#7664) since the last recovery — commenting here instead of filing a fresh issue.

**Suspected cause** (unverified, per anvil#1270): \`advertised\` only moves at dispatch time, so a RAM/disk-throttled host with cap 0 never advertises and cannot reach the sustained-receive recovery threshold.

Filed automatically by the loom-daemon-watchdog.sh peer-coordination escalation (#6222, dedup by #7664).
EOF
    gh issue comment "$cooldown_issue_ref" --body "$dedup_body" >/dev/null 2>&1 || return 1
    gh issue reopen "$cooldown_issue_ref" >/dev/null 2>&1 || true
    mkdir -p "$(dirname "$PEER_COORD_SENTINEL")" 2>/dev/null || true
    printf '%s %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$cooldown_issue_ref" > "$PEER_COORD_SENTINEL" 2>/dev/null || true
    mkdir -p "$(dirname "$PEER_COORD_COOLDOWN_STATE")" 2>/dev/null || true
    printf '%s %s %s\n' "$cooldown_ts" "$cooldown_issue_ref" "$flap_count" > "$PEER_COORD_COOLDOWN_STATE" 2>/dev/null || true
    # Read by the watchdog's own report line after this returns (sourced,
    # same shell) — not unused.
    # shellcheck disable=SC2034
    PEER_COORD_ESCALATE_NOTE="Repeat flap #${flap_count} within the ${PEER_COORD_DEDUP_WINDOW_SECS}s dedup window (#7664) — commented on the existing tracking issue ${cooldown_issue_ref} instead of filing a new one."
    return 0
}

# peer_coord_carry_flap_count <issue_ref>
#
# Called by clear_peer_coordination_escalation() just before it stamps a fresh
# recovery epoch into PEER_COORD_COOLDOWN_STATE. Prints the flap count to carry
# forward: the count already stored for THIS SAME issue reference (so a
# dedup-comment escalation's bumped count survives the recovery and continues
# from there on the next in-window excursion, instead of resetting every
# recovery), or 1 when the stored reference is missing, corrupt, or belongs
# to a different issue (this episode's own original filing).
peer_coord_carry_flap_count() {
    local issue_ref="$1" prev_ref="" prev_flap=""
    if [[ -f "$PEER_COORD_COOLDOWN_STATE" ]]; then
        # First field (the prior epoch) is intentionally discarded — the
        # caller is about to stamp its OWN fresh epoch.
        read -r _ prev_ref prev_flap < "$PEER_COORD_COOLDOWN_STATE" 2>/dev/null || true
    fi
    if [[ "$prev_ref" == "$issue_ref" && "$prev_flap" =~ ^[0-9]+$ ]]; then
        echo "$prev_flap"
    else
        echo 1
    fi
}
