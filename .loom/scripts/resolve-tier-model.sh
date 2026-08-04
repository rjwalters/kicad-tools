#!/usr/bin/env bash
# resolve-tier-model.sh - Print the model an issue's work must run on (#4238).
#
# Turns the Curator's runtime-neutral complexity tier into a concrete model id,
# so the dispatch path does a LOOKUP instead of a judgement call. Reading a
# document and "resolving" a model in your head is how model selection silently
# drifts; this makes the resolution a command whose output is either used or
# visibly absent.
#
#   resolve-tier-model.sh <issue> [runtime] [repo]   # runtime defaults to claude
#
# Resolution:
#   1. Read `<!-- loom:complexity=... -->` from the issue body. Missing or
#      unrecognised => routine (the safe middle).
#   2. Look up sweep.tierModels[<runtime>][<tier>] in .loom/config.json. If that
#      has no entry, fall back to the tier's entry (if any) in the
#      sweep.optimization preset (`cost` | `speed` | `balanced`, default
#      `balanced`; env override LOOM_SWEEP_OPTIMIZATION, issue #4238 Phase B).
#      Either way the resolved logical tier is passed through resolve-model.sh
#      (logical tier -> current-generation ID). All three steps live in
#      loom-daemon/src/script_helpers/model_tiers.rs (--tier mode), so they are
#      covered by its Rust unit tests rather than duplicated here in inline
#      python.
#   3. No entry from either source (or a mapping that would resolve to `fable`)
#      => print nothing, exit 3, so the caller falls through to its normal
#      precedence chain (the tier-3 role default) instead of guessing a model.
#      An unconfigured repo (or one with sweep.optimization unset/"balanced")
#      therefore dispatches byte-identically to today.
#
# Prints ONLY the model id on stdout; diagnostics go to stderr.
set -uo pipefail

ISSUE="${1:-}"
RUNTIME="${2:-claude}"
[[ -n "$ISSUE" ]] || { echo "usage: resolve-tier-model.sh <issue> [runtime] [repo]" >&2; exit 2; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" || { echo "not a git repo" >&2; exit 2; }
CONFIG="$ROOT/.loom/config.json"

# Resolve the repo explicitly. A bare `gh issue view` targets the default remote,
# which is wrong wherever `origin` is not where the issues live (a fork checkout,
# most obviously) — it would read the same-numbered issue in another repository
# and hand back a confident model choice for someone else's work item.
REPO="${3:-${LOOM_REPO:-}}"
if [[ -z "$REPO" ]]; then
  # shellcheck source=/dev/null
  source "$ROOT/.loom/scripts/lib/forge-helpers.sh" 2>/dev/null || true
  if declare -F forge_get_repo_nwo >/dev/null; then
    REPO="$(forge_get_repo_nwo gh 2>/dev/null || true)"
  fi
fi
[[ -n "$REPO" ]] || { echo "could not determine repo; pass it explicitly or set LOOM_REPO" >&2; exit 2; }

# Fetch the issue body with a GraphQL->REST fallback (#4472). `gh issue view` is
# a GraphQL call; under quota exhaustion (routine at fleet scale, epic #4432) it
# fails and a swallowed failure parses as an empty tier -> `routine`, silently
# disabling cost/speed routing. REST draws on a separate quota, so try it before
# giving up. If BOTH fail we still fall through to `routine` (a non-breaking
# default for this non-blocking resolver — unlike require-complexity-marker.sh
# this script never blocks curation) but say so, so the degradation shows in logs.
if body="$(gh issue view "$ISSUE" -R "$REPO" --json body -q .body 2>/dev/null)"; then
  :
elif body="$(gh api "repos/$REPO/issues/$ISSUE" --jq .body 2>/dev/null)"; then
  :
else
  echo "$REPO#$ISSUE: could not fetch body (GraphQL+REST failed — likely API quota) -> routine" >&2
  body=""
fi
# Anchor to the canonical HTML-comment marker form (`<!-- loom:complexity=<tier>
# -->`) rather than a bare `loom:complexity=[a-z]*` substring, and take the LAST
# such match (#4840). A bare substring match also fires on prose that merely
# *discusses* the marker syntax — e.g. an issue about the complexity-marker
# feature itself quoting `` `<!-- loom:complexity=<tier> -->` `` as literal
# example text. There the `<` right after `=` matches zero `[a-z]` chars,
# producing an empty match that `head -1` picked over the real marker later in
# the body, silently resolving to `routine` with no error surfaced. Anchoring
# to the full `<!-- ... -->` comment form excludes that placeholder text (the
# literal `<` breaks the `-->` anchor), and `tail -1` picks the marker nearest
# the end of the body, matching where the marker is conventionally placed.
tier="$(printf '%s' "$body" | grep -oE '<!--[[:space:]]*loom:complexity=[a-z]*[[:space:]]*-->' | tail -1 | sed -E 's/.*complexity=([a-z]*).*/\1/')"
# Two distinct fall-through cases (#4448): an absent marker is the expected
# default for issues curated before the marker existed (or before it became
# mandatory) and stays a quiet info line; an out-of-vocabulary value (a
# curator paraphrasing the closed enum — `trivial`/`large`/`moderate` etc.)
# is a drift bug worth naming so it shows up in logs, not just silently
# routed to routine. Both still resolve to `routine` — no behavior change.
case "$tier" in
  mechanical|routine|complex) ;;
  "")
    echo "$REPO#$ISSUE: no complexity marker -> routine" >&2
    tier="routine"
    ;;
  *)
    echo "$REPO#$ISSUE: invalid complexity tier '$tier' -> routine" >&2
    tier="routine"
    ;;
esac

# --tier mode returns "" + exit 3 when the runtime/tier has no mapping.
if model="$("$SCRIPT_DIR/resolve-model.sh" \
              --tier "$tier" --runtime "$RUNTIME" --config "$CONFIG" 2>/dev/null)" \
   && [[ -n "$model" ]]; then
  echo "resolve-tier-model: repo=$REPO issue=$ISSUE runtime=$RUNTIME tier=$tier model=$model" >&2
  printf '%s\n' "$model"
  exit 0
fi

echo "no tierModels/optimization-preset entry for runtime=$RUNTIME tier=$tier — falling through to tier 3" >&2
exit 3
