#!/usr/bin/env bash
#
# scripts/lib/site-verify.sh — deployed-artifact verification library
# (Issue #5318).
#
# Background: the live kicad-tools.org site served a pre-repair Board05 PCB
# (41 arbitrary-angle B.Cu segments) for weeks after the source-level fix
# (#5045) merged to main. The site is deployed MANUALLY (see
# site/README.md "Deploying") — there is no CI auto-deploy — and nobody
# re-ran the deploy after the fix landed. A `wrangler pages deploy` that
# exits 0 is not proof the public bytes actually changed: CDN propagation
# lag, a partial upload, or simply "the operator forgot to redeploy" all
# look identical to success from the deploy pipeline's point of view.
#
# This library provides one thing: a way to fetch a board's publicly
# served `board.kicad_pcb` and compare its SHA-256 against a local,
# just-staged copy, so staleness is caught loudly instead of silently.
# It is shared by two callers:
#   - scripts/deploy-site.sh          — runs this right after every
#                                        production deploy, against
#                                        site/dist/boards (the site it just
#                                        built and pushed).
#   - scripts/verify-site-deployment.sh — a standalone drift check runnable
#                                        at any time, without deploying,
#                                        against a freshly staged
#                                        site/public/boards.
#
# Intentionally NOT `set -e`: this file is sourced into scripts that manage
# their own error handling (both callers use `set -euo pipefail`).

# sha256_of_file PATH -- portable digest helper for a file's raw bytes.
# Unlike hashing a shell string (mangles embedded NULs/binary), this always
# reads bytes straight off disk -- used for both the local staged file and
# the downloaded HTTP response (curl writes to a temp file, never a
# variable), so fetched bytes are never round-tripped through bash.
sha256_of_file() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$1" | awk '{print $1}'
  else
    return 1
  fi
}

# verify_deployed_pcbs BASE_URL STAGED_BOARDS_DIR
#
#   For every "<STAGED_BOARDS_DIR>/<slug>/board.kicad_pcb", fetches
#   "<BASE_URL>/boards/<slug>/board.kicad_pcb" and compares SHA-256 hashes.
#   Retries a few times with a short backoff (CDN propagation is not always
#   instantaneous) before declaring a mismatch. Prints one line per board
#   checked/mismatched to stderr/stdout and returns non-zero if ANY board's
#   publicly served bytes differ from the staged copy, or the fetch fails
#   outright after retries.
#
#   Returns 0 (with a warning, not an error) if STAGED_BOARDS_DIR contains
#   no "*/board.kicad_pcb" entries at all -- a fresh checkout with no board
#   output is not a verification failure.
#
#   Tuning (mainly for tests): VERIFY_RETRY_ATTEMPTS (default 3),
#   VERIFY_RETRY_SLEEP_SECONDS (default 5).
verify_deployed_pcbs() {
  local base_url="$1" staged_dir="$2"
  local attempts="${VERIFY_RETRY_ATTEMPTS:-3}"
  local sleep_s="${VERIFY_RETRY_SLEEP_SECONDS:-5}"
  local checked=0 mismatches=0
  local tmp
  tmp="$(mktemp)"

  shopt -s nullglob
  local pcb
  for pcb in "${staged_dir}"/*/board.kicad_pcb; do
    local slug url local_hash remote_hash attempt ok
    slug="$(basename "$(dirname "${pcb}")")"
    url="${base_url%/}/boards/${slug}/board.kicad_pcb"
    local_hash="$(sha256_of_file "${pcb}")"
    remote_hash=""
    ok=0

    attempt=1
    while [ "${attempt}" -le "${attempts}" ]; do
      if curl -fsSL --max-time 30 -o "${tmp}" "${url}" 2>/dev/null; then
        remote_hash="$(sha256_of_file "${tmp}")"
        if [ "${remote_hash}" = "${local_hash}" ]; then
          ok=1
          break
        fi
      fi
      [ "${attempt}" -lt "${attempts}" ] && sleep "${sleep_s}"
      attempt=$((attempt + 1))
    done

    checked=$((checked + 1))
    if [ "${ok}" -ne 1 ]; then
      mismatches=$((mismatches + 1))
      printf '[site-verify] MISMATCH board=%s url=%s local=%s remote=%s\n' \
        "${slug}" "${url}" "${local_hash}" "${remote_hash:-<fetch failed>}" >&2
    fi
  done
  rm -f "${tmp}"

  if [ "${checked}" -eq 0 ]; then
    printf '[site-verify] WARNING: no board.kicad_pcb found under %s/*/board.kicad_pcb; nothing to verify.\n' \
      "${staged_dir}" >&2
    return 0
  fi

  if [ "${mismatches}" -gt 0 ]; then
    printf '[site-verify] %d/%d deployed board PCB(s) do NOT match staged source (see issue #5318).\n' \
      "${mismatches}" "${checked}" >&2
    return 1
  fi

  printf '[site-verify] OK: %d deployed board PCB(s) match staged source (%s).\n' "${checked}" "${base_url}"
  return 0
}
