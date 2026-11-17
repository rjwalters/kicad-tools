#!/usr/bin/env bash
#
# verify-site-deployment.sh — standalone drift check: does the live
# kicad-tools.org / kicad-tools.pages.dev site serve the SAME board PCB
# bytes as the CURRENT repository source, right now?
#
# Issue #5318: the live site served a pre-repair Board05 PCB (41
# arbitrary-angle B.Cu segments) for weeks after the source-level fix
# (#5045) merged to main. The site is deployed manually (see
# site/README.md "Deploying"), and the exact deployment history was not
# established. Nothing ever re-checked the deployed bytes against main, so
# the staleness went unnoticed until a user reported it.
#
# This script closes that detection gap WITHOUT requiring a deploy, a
# Cloudflare login, or even `npm ci` / an Astro build: it re-stages
# every published board asset (PCB, renders, manufacturing downloads incl.
# kicad_project.zip) from current source using the exact same staging logic
# the real build uses (site/scripts/copy-renders.mjs, run standalone --
# see its own header for why: no external deps needed for the copy step),
# then fetches the publicly served copy of each asset and compares SHA-256
# hashes via scripts/lib/site-verify.sh.
#
# Run this any time -- after landing a routing fix, before trusting a
# stale-looking bug report, or periodically as a drift check -- to answer
# "is the live site actually showing what's on main?" with measured
# evidence instead of an assumption.
#
# Usage:
#   ./scripts/verify-site-deployment.sh [--base-url URL] [--help]
#
#   --base-url URL   Public site base URL to check against.
#                     Default: $KCT_SITE_BASE_URL or
#                     https://kicad-tools.pages.dev
#   --help            Show this help and exit.
#
# Exit codes: 0 = every checked board asset's deployed bytes match current
# source (or there was nothing to check); 1 = at least one mismatch.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
# shellcheck source=lib/site-verify.sh
source "${SCRIPT_DIR}/lib/site-verify.sh"

usage() {
  sed -n '2,/^set -euo pipefail/p' "${BASH_SOURCE[0]}" \
    | sed -e 's/^# \{0,1\}//' -e '/^set -euo pipefail/d'
}

BASE_URL="${KCT_SITE_BASE_URL:-https://kicad-tools.pages.dev}"

while [ $# -gt 0 ]; do
  case "$1" in
    --base-url)
      BASE_URL="$2"
      shift 2
      ;;
    --base-url=*)
      BASE_URL="${1#*=}"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      echo "Run '$0 --help' for usage." >&2
      exit 2
      ;;
  esac
done

cd "${REPO_ROOT}"

if ! command -v node >/dev/null 2>&1; then
  echo "[verify-site-deployment] 'node' not found — required to stage current-source board PCBs." >&2
  exit 1
fi
if ! command -v curl >/dev/null 2>&1; then
  echo "[verify-site-deployment] 'curl' not found — required to fetch the deployed PCBs." >&2
  exit 1
fi

echo "[verify-site-deployment] staging current-source board assets (site/scripts/copy-renders.mjs)..."
node "${REPO_ROOT}/site/scripts/copy-renders.mjs"

STAGED_DIR="${REPO_ROOT}/site/public/boards"
if [ ! -d "${STAGED_DIR}" ]; then
  echo "[verify-site-deployment] no boards staged under ${STAGED_DIR}; nothing to verify (fresh checkout with no board output?)." >&2
  exit 0
fi

echo "[verify-site-deployment] comparing staged source against ${BASE_URL} ..."
verify_deployed_assets "${BASE_URL}" "${STAGED_DIR}"
