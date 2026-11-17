#!/usr/bin/env bash
# daemon-env-harvest.sh — shared harvesters for a live launchd plist's /
# systemd unit's LOOM_*/token EnvironmentVariables (#4581).
#
# Extracted from loom-daemon-update.sh's perform_relaunch / perform_systemd_
# relaunch (#4118/#4126), which already apply the harvest-and-preserve
# pattern on ITS equivalent refused-restart fallback: before re-rendering the
# plist/unit, read back the LIVE installed file's LOOM_*/token env and
# re-export it, so the re-render never silently narrows to whatever LOOM_*
# vars happen to be exported in the invoking shell. scripts/loom's
# `loom_cmd_restart()` bare-exec fallback had the identical shape but skipped
# this step entirely (the #4522/#4579 incident) — sharing this lib lets both
# call sites apply the same, single-sourced pattern instead of drifting.
#
# Source this file (do not exec). Defines two functions:
#
#   harvest_plist_env <plist>
#     Echoes "<key>\t<base64(value)>" lines (one per key, base64 so values
#     with spaces/newlines survive) restricted to exactly the keys
#     render_launchd_plist (loom-daemon-start.sh) itself forwards — LOOM_*,
#     GH_TOKEN, GITEA_TOKEN, FORGE_TOKEN — and EXCLUDING:
#       - PATH / HOME     (start.sh resolves PATH deterministically; round-
#                          tripping the plist's PATH here would re-introduce
#                          the non-deterministic-render bug it fixed, #4172)
#       - LOOM_DAEMON_SUPERVISOR (start.sh hardcodes it; re-exporting is
#                          pointless)
#     Returns 2 (with a stderr diagnostic) when the plist is missing or
#     unparseable (plutil/jq absent, or the plist fails to parse) — NEVER
#     silently returns an empty set, which would let a caller re-render into
#     a silently-narrowed, FLAGS-OFF-defaults autonomy config (#4011).
#
#   harvest_unit_env <unit_path>
#     Echoes "<key>\t<value>" lines (no base64 — a systemd `Environment=`
#     line is single-valued) with the identical key allowlist/exclusion,
#     mirroring render_systemd_unit (loom-daemon-start.sh). Returns 2 (with a
#     stderr diagnostic) when the unit file is missing — same "never return
#     an empty set silently" contract as harvest_plist_env.
#
# Callers decide what "harvest failed" means for them (loom-daemon-update.sh's
# perform_relaunch/perform_systemd_relaunch treat it as a hard abort; see
# scripts/loom's loom_cmd_restart() for the equivalent bare-exec-fallback use).
#
# Three more functions (#5081) close the OTHER half of the plist-env-change
# story: after a bootout+bootstrap, prove the freshly-rendered plist's
# EnvironmentVariables actually took effect, instead of trusting a successful
# `launchctl bootstrap` + a live pid alone (which says nothing about whether
# the job's REPORTED env matches what was just written):
#
#   extract_launchd_print_env <print_output>
#     Pure text parser (no `launchctl` call of its own — takes ALREADY-
#     CAPTURED `launchctl print <service>` output as its one argument, so it
#     is trivially unit-testable against canned text). Echoes "<key>\t<value>"
#     lines parsed out of the "environment = { KEY => value ... }" block
#     `launchctl print` reports for a loaded job — the merged env launchd
#     actually handed the running process, and the only authoritative source
#     for "did the new plist's env take effect" (the plist file itself only
#     says what SHOULD have been applied).
#
#   launchd_env_matches_plist <plist> <print_output>
#     Compares EVERY key in <plist>'s EnvironmentVariables dict (not just the
#     LOOM_*/token subset harvest_plist_env extracts — this checks the WHOLE
#     dict, including PATH/HOME/LOOM_DAEMON_SUPERVISOR) against
#     extract_launchd_print_env's parse of <print_output>. Returns 0 when
#     every key matches, 1 (with a stderr diagnostic naming the first
#     missing/mismatched key) otherwise, 2 when plutil/jq are unavailable or
#     the plist is unparseable — same contract as harvest_plist_env. Takes
#     the print output as a plain argument (not a live service name) so it,
#     too, is testable without a real `launchctl`.
#
#   verify_launchd_env_applied <service> <plist>
#     The live wrapper: runs `launchctl print <service>` itself and hands the
#     result to launchd_env_matches_plist. Returns 1 (with a stderr
#     diagnostic) when the job cannot even be printed (not loaded/running),
#     in addition to launchd_env_matches_plist's own return contract. This is
#     the function loom-daemon-start.sh's launchd path calls as its final
#     post-bootstrap post-condition.

harvest_plist_env() {
    local plist="$1"
    if [[ ! -f "$plist" ]]; then
        echo "Cannot harvest launchd env: plist not found at $plist" >&2
        return 2
    fi
    if ! command -v plutil >/dev/null 2>&1 || ! command -v jq >/dev/null 2>&1; then
        echo "Cannot harvest launchd env: plutil and jq are both required on the macOS launchd path." >&2
        return 2
    fi
    local json
    json=$(plutil -convert json -o - "$plist" 2>/dev/null) || {
        echo "Cannot harvest launchd env: plist at $plist is not parseable by plutil." >&2
        return 2
    }
    printf '%s' "$json" | jq -r '
        .EnvironmentVariables // {}
        | to_entries[]
        | select(.key | test("^(LOOM_[A-Za-z0-9_]*|GH_TOKEN|GITEA_TOKEN|FORGE_TOKEN)$"))
        | select(.key != "LOOM_DAEMON_SUPERVISOR")
        | .key + "\t" + (.value | @base64)
    ' 2>/dev/null || {
        echo "Cannot harvest launchd env: failed to extract EnvironmentVariables from $plist." >&2
        return 2
    }
}

harvest_unit_env() {
    local unit_path="$1"
    if [[ ! -f "$unit_path" ]]; then
        echo "Cannot harvest systemd unit env: unit file not found at $unit_path" >&2
        return 2
    fi
    local line rest key value
    while IFS= read -r line; do
        [[ "$line" == Environment=* ]] || continue
        rest="${line#Environment=}"
        key="${rest%%=*}"
        value="${rest#*=}"
        case "$key" in
            LOOM_DAEMON_SUPERVISOR) continue ;;
            LOOM_*|GH_TOKEN|GITEA_TOKEN|FORGE_TOKEN) printf '%s\t%s\n' "$key" "$value" ;;
            *) continue ;;
        esac
    done < "$unit_path"
}

extract_launchd_print_env() {
    local print_output="$1"
    printf '%s\n' "$print_output" | awk '
        /^[[:space:]]*environment = \{/ { in_env=1; next }
        in_env && /^[[:space:]]*\}[[:space:]]*$/ { in_env=0; next }
        in_env {
            line=$0
            sub(/^[[:space:]]+/, "", line)
            idx = index(line, " => ")
            if (idx > 0) {
                key = substr(line, 1, idx - 1)
                val = substr(line, idx + 4)
                print key "\t" val
            }
        }
    '
}

launchd_env_matches_plist() {
    local plist="$1" print_output="$2"
    if [[ ! -f "$plist" ]]; then
        echo "Cannot verify launchd env: plist not found at $plist" >&2
        return 2
    fi
    if ! command -v plutil >/dev/null 2>&1 || ! command -v jq >/dev/null 2>&1; then
        echo "Cannot verify launchd env: plutil and jq are both required on the macOS launchd path." >&2
        return 2
    fi
    local expected_json
    expected_json=$(plutil -convert json -o - "$plist" 2>/dev/null) || {
        echo "Cannot verify launchd env: plist at $plist is not parseable by plutil." >&2
        return 2
    }
    local actual_env
    actual_env="$(extract_launchd_print_env "$print_output")"
    local key expected_value actual_value mismatches=0
    while IFS= read -r key; do
        [[ -z "$key" ]] && continue
        expected_value=$(printf '%s' "$expected_json" | jq -r --arg k "$key" '.EnvironmentVariables[$k] // ""')
        if ! actual_value=$(printf '%s\n' "$actual_env" | awk -F'\t' -v k="$key" '$1 == k { print $2; found=1; exit } END { if (!found) exit 1 }'); then
            echo "launchd env verification: key '${key}' is in the plist but missing from the running job's reported environment." >&2
            mismatches=$((mismatches + 1))
            continue
        fi
        if [[ "$actual_value" != "$expected_value" ]]; then
            echo "launchd env verification: key '${key}' expected [${expected_value}] but the running job reports [${actual_value}]." >&2
            mismatches=$((mismatches + 1))
        fi
    done < <(printf '%s' "$expected_json" | jq -r '.EnvironmentVariables // {} | keys[]')
    [[ "$mismatches" -eq 0 ]]
}

verify_launchd_env_applied() {
    local service="$1" plist="$2"
    local print_output
    print_output=$(launchctl print "$service" 2>/dev/null) || {
        echo "Cannot verify launchd env: 'launchctl print ${service}' failed (job not loaded/running)." >&2
        return 1
    }
    launchd_env_matches_plist "$plist" "$print_output"
}
