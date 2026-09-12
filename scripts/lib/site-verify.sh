#!/usr/bin/env bash
#
# scripts/lib/site-verify.sh — deployed-artifact verification library
# (Issue #5318).
#
# Background: the live kicad-tools.org site served a pre-repair Board05 PCB
# (41 arbitrary-angle B.Cu segments) for weeks after the source-level fix
# (#5045) merged to main. The site is deployed MANUALLY (see
# site/README.md "Deploying") — there is no CI auto-deploy — and the exact
# deployment history was not established. A `wrangler pages deploy` that
# exits 0 is not proof the public bytes actually changed: CDN propagation
# lag, a partial upload, or a stale re-publish all look identical to success
# from the deploy pipeline's point of view. The follow-up audit on #5318
# additionally confirmed the SAME stale bytes are also served through
# manufacturing/kicad_project.zip, not just the interactive-viewer PCB, so a
# fix that only checked board.kicad_pcb would leave the downloadable project
# silently stale.
#
# This library provides a way to fetch every publicly served file staged
# under a board's directory (PCB, renders, manufacturing downloads) and
# compare its SHA-256 against the local, just-staged copy, so staleness in
# ANY published asset is caught loudly instead of silently. It is shared by
# two callers:
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
# their own error handling (both callers use `set -euo pipefail`). Both
# `sha256_of_file` and `verify_deployed_assets` below check every command's
# exit status explicitly rather than relying on inherited `set -e`/`pipefail`
# — this matters because a caller invoking `verify_deployed_assets` as
# `if ! verify_deployed_assets ...` runs it with errexit suppressed for the
# entire dynamic call (a documented bash behavior for the condition of an
# `if`), so a silently-swallowed failure inside this library would not abort
# anything on its own.

# sha256_of_file PATH -- portable digest helper for a file's raw bytes.
# Unlike hashing a shell string (mangles embedded NULs/binary), this always
# reads bytes straight off disk -- used for both the local staged file and
# the downloaded HTTP response (curl writes to a temp file, never a
# variable), so fetched bytes are never round-tripped through bash.
#
# Fails loudly (returns 1, prints nothing to stdout) if the file is missing,
# unreadable, or neither sha256sum nor shasum is available -- callers MUST
# check the return status and reject an empty result rather than treating
# "both sides came back empty" as a match.
sha256_of_file() {
  local path="$1" raw
  [ -r "${path}" ] || return 1
  if command -v sha256sum >/dev/null 2>&1; then
    raw="$(sha256sum "${path}")" || return 1
  elif command -v shasum >/dev/null 2>&1; then
    raw="$(shasum -a 256 "${path}")" || return 1
  else
    return 1
  fi
  printf '%s\n' "${raw}" | awk '{print $1}'
}

# verify_deployed_assets BASE_URL STAGED_BOARDS_DIR
#
#   For every file staged under "<STAGED_BOARDS_DIR>/<slug>/..." (the PCB,
#   renders, and manufacturing downloads that site/scripts/copy-renders.mjs
#   stages), fetches "<BASE_URL>/boards/<slug>/<relative-path>" and compares
#   SHA-256 hashes. Retries a few times with a short backoff (CDN propagation
#   is not always instantaneous) before declaring a mismatch. Prints one line
#   per asset checked/mismatched to stderr/stdout and returns non-zero if ANY
#   asset's publicly served bytes differ from the staged copy, the fetch
#   fails outright after retries, or the LOCAL hash cannot be computed (a
#   local read/hashing failure is treated as a mismatch, never a silent
#   match against an equally-failed remote hash).
#
#   Returns 0 (with a warning, not an error) if STAGED_BOARDS_DIR contains no
#   board directories at all -- a fresh checkout with no board output is not
#   a verification failure.
#
#   Tuning (mainly for tests): VERIFY_RETRY_ATTEMPTS (default 3),
#   VERIFY_RETRY_SLEEP_SECONDS (default 5).
verify_deployed_assets() {
  local base_url="$1" staged_dir="$2"
  local attempts="${VERIFY_RETRY_ATTEMPTS:-3}"
  local sleep_s="${VERIFY_RETRY_SLEEP_SECONDS:-5}"
  local checked=0 mismatches=0
  local tmp
  tmp="$(mktemp)"

  shopt -s nullglob
  local board_dir
  for board_dir in "${staged_dir}"/*/; do
    [ -d "${board_dir}" ] || continue
    local slug
    slug="$(basename "${board_dir%/}")"

    local file
    while IFS= read -r -d '' file; do
      local rel_path url local_hash remote_hash attempt ok
      rel_path="${file#"${board_dir}"}"
      url="${base_url%/}/boards/${slug}/${rel_path}"

      if ! local_hash="$(sha256_of_file "${file}")" || [ -z "${local_hash}" ]; then
        checked=$((checked + 1))
        mismatches=$((mismatches + 1))
        printf '[site-verify] MISMATCH board=%s asset=%s url=%s error=local hash computation failed\n' \
          "${slug}" "${rel_path}" "${url}" >&2
        continue
      fi

      remote_hash=""
      ok=0
      attempt=1
      while [ "${attempt}" -le "${attempts}" ]; do
        if curl -fsSL --max-time 30 -o "${tmp}" "${url}" 2>/dev/null; then
          if remote_hash="$(sha256_of_file "${tmp}")" && [ -n "${remote_hash}" ]; then
            if [ "${remote_hash}" = "${local_hash}" ]; then
              ok=1
              break
            fi
          else
            remote_hash=""
          fi
        fi
        [ "${attempt}" -lt "${attempts}" ] && sleep "${sleep_s}"
        attempt=$((attempt + 1))
      done

      checked=$((checked + 1))
      if [ "${ok}" -ne 1 ]; then
        mismatches=$((mismatches + 1))
        printf '[site-verify] MISMATCH board=%s asset=%s url=%s local=%s remote=%s\n' \
          "${slug}" "${rel_path}" "${url}" "${local_hash}" "${remote_hash:-<fetch failed>}" >&2
      fi
    done < <(find "${board_dir}" -type f -print0)
  done
  rm -f "${tmp}"

  if [ "${checked}" -eq 0 ]; then
    printf '[site-verify] WARNING: no staged assets found under %s/*/; nothing to verify.\n' \
      "${staged_dir}" >&2
    return 0
  fi

  if [ "${mismatches}" -gt 0 ]; then
    printf '[site-verify] %d/%d deployed board asset(s) do NOT match staged source (see issue #5318).\n' \
      "${mismatches}" "${checked}" >&2
    return 1
  fi

  printf '[site-verify] OK: %d deployed board asset(s) match staged source (%s).\n' "${checked}" "${base_url}"
  return 0
}
