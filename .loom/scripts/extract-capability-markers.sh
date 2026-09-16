#!/usr/bin/env bash
# extract-capability-markers.sh - reference parser for the
# `<!-- loom:capability=<name> -->` convention (#6892), mirroring
# require-complexity-marker.sh's `<!-- loom:complexity=<tier> -->` anchoring
# so both sides of the fleet parse the same marker identically.
#
# This marker is meaningful ONLY alongside the `loom:operator-mechanical`
# sub-kind label (see defaults/docs/label-state-machine.md, "Capability-
# declaration convention"). This script does not look at labels at all --
# callers decide whether the marker applies to the item they read it from.
#
# Reads an issue/PR body from stdin, extracts every
# `<!-- loom:capability=<value> -->` marker, validates each value against the
# closed vocabulary below (kept in sync with the convention doc's table), and
# prints the deduplicated, sorted list of VALID capabilities to stdout, one
# per line. Multiple markers are ANDed by convention (a caller enforcing
# "does this worker satisfy every declared capability" needs the full set,
# not just one) -- unlike the complexity marker, which has exactly one valid
# value per item, this script collects every match rather than taking the
# last.
#
# Anchoring rationale (identical to require-complexity-marker.sh, #4840): a
# bare substring match would also fire on prose that merely *quotes* the
# marker syntax as literal example text (this convention doc does exactly
# that). Anchoring to the full `<!-- ... -->` comment form excludes that
# prose, because a placeholder like `<name>` breaks the `-->` anchor.
#
# Usage:
#   extract-capability-markers.sh < body.txt
#   printf '%s' "$body" | extract-capability-markers.sh
#
# Exit codes:
#   0 - one or more valid capability markers found (printed to stdout)
#   1 - no capability markers found at all (not an error -- most items,
#       including most loom:operator-mechanical items, declare none)
#   2 - at least one marker was found but failed closed-vocabulary
#       validation; the offending value(s) are named on stderr. Per the
#       fail-closed rule, an unrecognized value is a hard signal, not a
#       silently-dropped one -- any co-occurring VALID markers still print to
#       stdout (a caller may want to know what WAS understood), but the
#       nonzero exit means the overall declaration could not be fully
#       resolved and must not be treated as fully satisfied.
set -uo pipefail

# Closed vocabulary (#6892). Extend by adding a literal value here AND a row
# in defaults/docs/label-state-machine.md's convention table -- keep both in
# sync; this is the enforcement side, that doc is the human-readable side.
# `cloud-profile:` is a family (colon-parameterized: `cloud-profile:<name>`),
# matched by prefix rather than as a literal.
KNOWN_LITERALS=(host-sudo forge-admin-token tailnet-access)
KNOWN_PREFIXES=(cloud-profile:)

body="$(cat)"

# Value grammar: lowercase alnum, `:`/`_`/`-` separators (the colon supports
# the `cloud-profile:<name>` family) -- same character-class discipline as
# the complexity marker's `[a-z]*`, generalized for parameterized values.
# `mapfile` is bash 4+; macOS ships 3.2 (#7751). The `|| true` means a
# no-match grep yields no output, so empty lines are skipped -- the
# `${#raw_matches[@]} -eq 0` check below is what distinguishes "no markers".
raw_matches=()
while IFS= read -r _m; do
  [[ -n "$_m" ]] || continue
  raw_matches+=("$_m")
done < <(printf '%s' "$body" \
  | grep -oE '<!--[[:space:]]*loom:capability=[a-z0-9][a-z0-9:_-]*[[:space:]]*-->' \
  || true)

if [[ "${#raw_matches[@]}" -eq 0 ]]; then
  exit 1
fi

is_known() {
  local value="$1" lit prefix
  for lit in "${KNOWN_LITERALS[@]}"; do
    [[ "$value" == "$lit" ]] && return 0
  done
  for prefix in "${KNOWN_PREFIXES[@]}"; do
    if [[ "$value" == "$prefix"* && "${#value}" -gt "${#prefix}" ]]; then
      return 0
    fi
  done
  return 1
}

# Indexed array, not `declare -A` (bash 4+; #7751). This was a pure SET whose
# only consumer printed its keys through `sort`, so appending every valid value
# and deduping with `sort -u` on output is byte-identical -- no membership test
# needed, and one fewer moving part than the associative form.
seen_valid=()
unknown=()
for m in "${raw_matches[@]}"; do
  # Anchored on the same `[[:space:]]*-->` terminator the recognition regex
  # above matched (#6914) -- `-` is both a legal value character and the head
  # of the closing delimiter, so an unanchored greedy capture (the pre-#6914
  # form: `s/.*capability=([a-z0-9][a-z0-9:_-]*).*/\1/`) could cross into the
  # delimiter's own dashes on a no-space marker like
  # `<!--loom:capability=tailnet-access-->`, yielding `tailnet-access--`
  # instead of `tailnet-access`. Anchoring the capture on `-->` (optionally
  # preceded by whitespace) forces the regex engine to backtrack the capture
  # to stop exactly where recognition already decided the value ends, so the
  # two steps agree.
  value="$(sed -E 's/.*capability=([a-z0-9][a-z0-9:_-]*)[[:space:]]*-->.*/\1/' <<<"$m")"
  if is_known "$value"; then
    seen_valid+=("$value")
  else
    unknown+=("$value")
  fi
done

if [[ "${#seen_valid[@]}" -gt 0 ]]; then
  printf '%s\n' "${seen_valid[@]}" | sort -u
fi

if [[ "${#unknown[@]}" -gt 0 ]]; then
  # Dedupe unknown values for a clean message without changing exit semantics.
  unknown_sorted="$(printf '%s\n' "${unknown[@]}" | sort -u | tr '\n' ',' | sed 's/,$//')"
  printf 'UNKNOWN capability value(s), failing closed: %s\n' "$unknown_sorted" >&2
  exit 2
fi

exit 0
