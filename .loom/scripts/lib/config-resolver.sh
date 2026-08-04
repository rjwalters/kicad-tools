#!/usr/bin/env bash
# config-resolver.sh — single resolver over the config tier chain (#4039,
# Epic #3835 Phase 2).
#
# Source this file (do not exec). Defines:
#
#   loom_resolve_config <repo_root>
#       Echoes the merged effective config JSON (compact, one line) for
#       repo_root, deep-merging the tier chain lowest to highest precedence.
#
#   loom_config_get <repo_root> <dotted.path> [default]
#       Echoes the value at a dotted key path (e.g.
#       "autonomous.workFinder.enabled") within the resolved effective
#       config, or `default` (empty string if omitted) when absent.
#
# Tier precedence (lowest -> highest; a higher tier overrides a lower one,
# key-by-key, via a recursive JSON merge — this is exactly jq's `*`
# operator, so the merge below IS the spec, not an approximation of it):
#
#   1. Private/shared defaults — $LOOM_CONFIG_DEFAULTS_FILE, else
#      ~/.local/share/loom/config/defaults.json. A no-op tier on every host
#      until Epic #3835 Phase 3 ships (the file doesn't exist yet anywhere).
#   2. Legacy    <repo_root>/.loom/config.json         — today's status quo.
#   3. Tracked   <repo_root>/.loom-project/project.json — new in #4039.
#   4. Ignored   <repo_root>/.loom-local/local.json     — new in #4039.
#
# A missing/malformed file at ANY tier soft-fails to `{}` for that tier (it
# contributes nothing to the merge) — this never aborts the calling script.
# See docs/design/config-resolution-tiers.md for the full design and the
# `.loom-project/project.json` schema.
#
# Requires `jq`. When jq is unavailable, resolution soft-fails to '{}' (the
# same "all tiers absent" result callers already treat as fine) rather than
# erroring the caller.
#
# Mirrors loom-daemon/src/config_resolver.rs key-for-key so a shared fixture
# tree resolves identically from both languages (conformance test, fixture at
# defaults/scripts/tests/fixtures/config_resolver/). A third implementation,
# loom_tools/common/config_resolver.py, was part of this contract until #4970
# retired the Python `loom-tools` package.

LOOM_CONFIG_LEGACY_REL=".loom/config.json"
LOOM_CONFIG_PROJECT_REL=".loom-project/project.json"
LOOM_CONFIG_LOCAL_REL=".loom-local/local.json"
_LOOM_CONFIG_DEFAULTS_DEFAULT_REL=".local/share/loom/config/defaults.json"

# _loom_config_private_defaults_path -> echoes the resolved private-defaults
# path honoring $LOOM_CONFIG_DEFAULTS_FILE. Echoes nothing when the env var
# is explicitly set to empty (tier disabled) or when $HOME is unset. The
# echoed path may not exist on disk — the caller's soft-read handles that.
_loom_config_private_defaults_path() {
    if [[ -n "${LOOM_CONFIG_DEFAULTS_FILE+set}" ]]; then
        # Explicitly set (possibly empty) -- empty disables the tier.
        echo -n "$LOOM_CONFIG_DEFAULTS_FILE"
        return 0
    fi
    if [[ -n "${HOME:-}" ]]; then
        echo "$HOME/$_LOOM_CONFIG_DEFAULTS_DEFAULT_REL"
    fi
}

# _loom_config_soft_read <path> -> echoes the file's JSON when it parses as
# a JSON object; echoes '{}' for a missing path, an unreadable file,
# malformed JSON, or a non-object top level. Never fails the caller.
_loom_config_soft_read() {
    local path="$1"
    if [[ -z "$path" ]] || [[ ! -f "$path" ]] || ! command -v jq >/dev/null 2>&1; then
        echo '{}'
        return 0
    fi
    local parsed
    if ! parsed=$(jq -c 'if type == "object" then . else {} end' "$path" 2>/dev/null); then
        echo '{}'
        return 0
    fi
    echo "$parsed"
}

# loom_resolve_config <repo_root>
#
# Echoes the merged effective config JSON (compact, one line) by
# deep-merging the tier chain low -> high precedence.
loom_resolve_config() {
    local repo_root="$1"

    if ! command -v jq >/dev/null 2>&1; then
        echo '{}'
        return 0
    fi

    local defaults_path
    defaults_path=$(_loom_config_private_defaults_path)

    local tier_defaults tier_legacy tier_project tier_local
    tier_defaults=$(_loom_config_soft_read "$defaults_path")
    tier_legacy=$(_loom_config_soft_read "$repo_root/$LOOM_CONFIG_LEGACY_REL")
    tier_project=$(_loom_config_soft_read "$repo_root/$LOOM_CONFIG_PROJECT_REL")
    tier_local=$(_loom_config_soft_read "$repo_root/$LOOM_CONFIG_LOCAL_REL")

    jq -c -n \
        --argjson a "$tier_defaults" \
        --argjson b "$tier_legacy" \
        --argjson c "$tier_project" \
        --argjson d "$tier_local" \
        '$a * $b * $c * $d'
}

# loom_config_get <repo_root> <dotted.path> [default]
#
# Resolve a single value from the effective merged config by dotted key
# path (e.g. "autonomous.workFinder.enabled"). Echoes `default` (empty
# string if omitted) when the path is absent, non-scalar, or jq is missing.
loom_config_get() {
    local repo_root="$1"
    local dotted="$2"
    local default_val="${3:-}"

    if ! command -v jq >/dev/null 2>&1; then
        echo "$default_val"
        return 0
    fi

    local effective
    effective=$(loom_resolve_config "$repo_root")

    # `try ... catch empty` guards against `dotted` indexing through a
    # non-object at some segment (e.g. "a.b" where "a" is a scalar) --
    # getpath() would otherwise raise and jq would exit non-zero, which
    # (via the `value=$(...)` assignment below) would trip a caller's
    # `set -e` and abort the whole script. Soft-fail to `default_val`
    # instead, exactly like a missing path.
    local value
    value=$(echo "$effective" | jq -r --arg dotted "$dotted" '
        ($dotted | split(".")) as $path
        | try getpath($path) catch null
        | if . == null then empty else . end
    ' 2>/dev/null)

    if [[ -z "$value" ]]; then
        echo "$default_val"
    else
        echo "$value"
    fi
}
