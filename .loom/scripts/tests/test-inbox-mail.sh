#!/usr/bin/env bash
# Tests the inbox_mail helper in defaults/docs/inbox-mail.md (#10000) against
# stubbed curl/gh: unset env -> no-op (no curl, no gh), send/resolve payloads and
# the Authorization header, failure is non-fatal, the origin-derived key, the
# human-merge resolve path (resolve-merged), the hold file's loader with the doc
# missing, plus doc-lint for the shared rule and labels.yml.
set -u
ROOT=$(cd "$(dirname "$0")/../../.." && pwd)
T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
fails=0
ok() { echo "ok: $1"; }
bad() { echo "FAIL: $1"; fails=$((fails+1)); }
DOC="$ROOT/defaults/docs/inbox-mail.md"
HOLD="$ROOT/defaults/.claude/commands/loom/champion-critical-file-hold.md"

awk '/^```bash inbox-mail/{f=1;next} /^```/{f=0} f' "$DOC" >"$T/fn.sh"
[ -s "$T/fn.sh" ] || { echo "FAIL: no inbox-mail fence"; exit 1; }

mkdir "$T/bin"
# curl stub: logs the payload file AND the --config stdin (where the header lives).
cat >"$T/bin/curl" <<'STUB'
#!/usr/bin/env bash
echo called >>"$STUB_LOG"
for a in "$@"; do echo "argv: $a" >>"$STUB_LOG"; done
while [ $# -gt 0 ]; do [ "$1" = --data-binary ] && { cat "${2#@}" >>"$STUB_LOG"; echo >>"$STUB_LOG"; }; shift; done
sed 's/^/config: /' >>"$STUB_LOG"
printf '{}\n%s' "${STUB_CODE:-200}"; exit "${STUB_RC:-0}"
STUB
# gh stub: honours the `merged:>=DATE` search like the forge does; no such
# qualifier -> [] (so a query without the date filter finds nothing).
cat >"$T/bin/gh" <<'STUB'
#!/usr/bin/env bash
echo "gh $*" >>"$GH_LOG"
since=; while [ $# -gt 0 ]; do [ "$1" = --search ] && since=$(sed -n 's/.*merged:>=\([^ ]*\).*/\1/p' <<<"$2"); shift; done
[ -n "$since" ] || { echo '[]'; exit 0; }
jq --arg s "$since" '[.[] | select(.mergedAt >= $s)]' "$GH_FIXTURE" 2>/dev/null || cat "$GH_FIXTURE"
STUB
# loom-daemon stub (#10137): `forge inbox-config` prints $STUB_CFG's contents (paths only);
# unset/absent -> prints nothing (an older daemon without the subcommand).
cat >"$T/bin/loom-daemon" <<'STUB'
#!/usr/bin/env bash
echo "loom-daemon $*" >>"${DAEMON_LOG:-/dev/null}"
[ "$1 $2" = "forge inbox-config" ] && [ -n "${STUB_CFG:-}" ] && cat "$STUB_CFG"
exit 0
STUB
chmod +x "$T/bin/curl" "$T/bin/gh" "$T/bin/loom-daemon"
export PATH="$T/bin:$PATH" STUB_LOG="$T/log" GH_LOG="$T/ghlog" GH_FIXTURE="$T/prs.json"
# shellcheck disable=SC1091
. "$T/fn.sh"

now=$(date -u +%Y-%m-%dT%H:%M:%SZ); old=$(date -u -d '-4 days' +%Y-%m-%dT%H:%M:%SZ); held=$(date -u -d '-30 hours' +%Y-%m-%dT%H:%M:%SZ)
M='<!-- champion:critical-file-hold -->'
jq -n --arg now "$now" --arg old "$old" --arg held "$held" --arg m "$M" '[
  {number: 10, mergedAt: $held, comments: [{body: $m}]},
  {number: 11, mergedAt: $now, comments: [{body: ("## hold\n" + $m)}]},
  {number: 12, mergedAt: $now, comments: [{body: "unrelated"}]},
  {number: 13, mergedAt: $old, comments: [{body: $m}]}]' >"$GH_FIXTURE"

: >"$STUB_LOG"; : >"$GH_LOG"; unset LOOM_UI_INBOX_URL LOOM_UI_INGEST_KEY
out=$(inbox_mail send k1 "hello"); rc=$?
{ [ $rc -eq 0 ] && grep -q "not configured" <<<"$out" && [ ! -s "$STUB_LOG" ]; } && ok "unset env: no-op, no curl" || bad "unset env"
out=$(inbox_mail resolve k1); rc=$?
{ [ $rc -eq 0 ] && [ ! -s "$STUB_LOG" ]; } && ok "unset env resolve: no-op" || bad "unset env resolve"
inbox_mail on && bad "on: true while unconfigured" || ok "on: false while unconfigured"
inbox_mail resolve-merged crithold-pr "$M" >/dev/null
{ [ ! -s "$GH_LOG" ] && [ ! -s "$STUB_LOG" ]; } && ok "unset env resolve-merged: no gh read" || bad "unset env resolve-merged read the forge"

# Key: from the origin remote, not the checkout directory name.
git init -q "$T/issue-77" && git -C "$T/issue-77" remote add origin git@github.com:acme/widgets.git
[ "$(cd "$T/issue-77" && inbox_mail key crithold-pr 5)" = mail-widgets-crithold-pr-5 ] && ok "key from origin (ssh)" || bad "key ssh: $(cd "$T/issue-77" && inbox_mail key crithold-pr 5)"
git -C "$T/issue-77" remote set-url origin https://github.com/acme/widgets/
[ "$(cd "$T/issue-77" && inbox_mail key crithold-pr 5)" = mail-widgets-crithold-pr-5 ] && ok "key from origin (https)" || bad "key https"

export LOOM_UI_INBOX_URL=http://inbox.invalid LOOM_UI_INGEST_KEY=secret TO=@op:x
inbox_mail on && ok "on: true when configured" || bad "on: configured"
: >"$STUB_LOG"; inbox_mail send mail-loom-crithold-pr-1 "PR 1 needs a human merge" >/dev/null
grep -q '"key": "mail-loom-crithold-pr-1"' "$STUB_LOG" && grep -q 'needs a human merge' "$STUB_LOG" \
  && ok "send payload keyed" || bad "send payload"
grep -qx 'config: header = "Authorization: Bearer secret"' "$STUB_LOG" && ok "Authorization header via --config stdin" || bad "auth header"
grep -q '^argv: .*secret' "$STUB_LOG" && bad "ingest key on argv" || ok "ingest key not on argv"
: >"$STUB_LOG"; inbox_mail send mail-loom-crithold-pr-1 "PR 1 needs a human merge" >/dev/null
[ "$(grep -c called "$STUB_LOG")" = 1 ] && grep -q '"key": "mail-loom-crithold-pr-1"' "$STUB_LOG" && ok "re-send reuses same key" || bad "re-send key"
: >"$STUB_LOG"; inbox_mail resolve mail-loom-crithold-pr-1 >/dev/null
grep -q '"resolve": true' "$STUB_LOG" && grep -q '"key": "mail-loom-crithold-pr-1"' "$STUB_LOG" && ok "resolve sends resolve:true" || bad "resolve payload"
out=$(STUB_CODE=500 STUB_RC=22 inbox_mail send k2 body); rc=$?
{ [ $rc -eq 0 ] && grep -q FAILED <<<"$out"; } && ok "failure is non-fatal" || bad "failure handling"

# Human-merge path: a held PR merged outside Champion gets its mail resolved.
: >"$STUB_LOG"; : >"$GH_LOG"
(cd "$T/issue-77" && inbox_mail resolve-merged crithold-pr "$M" >/dev/null); rc=$?
grep -Eq 'pr list --state merged --limit 100 --search merged:>=[0-9]{4}-[0-9]{2}-[0-9]{2}T' "$GH_LOG" \
  && ok "resolve-merged: query filters on merge date (merged:>=)" || bad "resolve-merged query: $(cat "$GH_LOG")"
{ [ $rc -eq 0 ] && [ "$(grep -c called "$STUB_LOG")" = 2 ] \
  && grep -q '"key": "mail-widgets-crithold-pr-10"' "$STUB_LOG" && grep -q '"key": "mail-widgets-crithold-pr-11"' "$STUB_LOG" \
  && [ "$(grep -c '"resolve": true' "$STUB_LOG")" = 2 ]; } && ok "resolve-merged: held PRs #10 (30h) and #11 resolved; old/unmarked skipped" || bad "resolve-merged selection"
echo 'not json' >"$GH_FIXTURE"; (inbox_mail resolve-merged crithold-pr "$M" >/dev/null) && ok "resolve-merged: bad gh output non-fatal" || bad "resolve-merged rc"

# The hold file's loader, run where the doc is missing: defines a no-op, `on` false.
sed -n '/^_im=\$(awk/,/^type inbox_mail/p' "$HOLD" >"$T/loader.sh"
[ "$(wc -l <"$T/loader.sh")" = 3 ] && ok "hold file carries the 3-line loader" || bad "loader not found in hold file"
out=$(cd "$T" && bash -c '. ./loader.sh; inbox_mail send k b; echo "send=$?"; inbox_mail on; echo "on=$?"' 2>&1)
{ grep -q 'send=0' <<<"$out" && grep -q 'on=1' <<<"$out" && ! grep -q 'not found' <<<"$out"; } && ok "missing doc: no-op fallback defined" || bad "missing doc fallback: $out"
mkdir -p "$T/repo/.loom/docs" && cp "$DOC" "$T/repo/.loom/docs/"
out=$(cd "$T/repo" && bash -c '. ../loader.sh; type inbox_mail | grep -c resolve-merged') && ok "doc present: loader evals the fence" || bad "loader with doc"

grep -q 'Two ways to reach a human' "$ROOT/defaults/docs/label-state-machine.md" && ok "rule in label-state-machine.md" || bad "rule missing"
[ "$(grep -rl --exclude-dir=tests 'a call is a decision, a human task is a mail' "$ROOT/defaults" | wc -l)" = 1 ] && ok "rule text appears once" || bad "rule text count"
grep -q 'loom:operator-mechanical' "$ROOT/defaults/.github/labels.yml" && ok "operator-mechanical label kept" || bad "label removed"
grep -q 'inbox_mail resolve "\$CF_MAIL_KEY"' "$HOLD" && ok "hold resolves mail" || bad "hold resolve missing"
grep -q 'inbox_mail send' "$HOLD" && ok "hold sends mail" || bad "hold send missing"
grep -q 'inbox_mail on &&' "$HOLD" && ok "hold gates its forge read on inbox config" || bad "hold read ungated"
grep -qF 'inbox_mail resolve-merged crithold-pr "<!-- champion:critical-file-hold -->"' \
  "$ROOT/defaults/.claude/commands/loom/champion-pr-merge.md" && ok "Champion runs resolve-merged per pass" || bad "resolve-merged not wired"

# --- #10137: resolve the key file and inbox URL like the daemon does --------
unset LOOM_UI_INBOX_URL LOOM_UI_INGEST_KEY
printf 'TOPSECRETKEY\n' >"$T/ingest.key"
printf 'url=https://dashboard.example.com\nkey_file=%s\n' "$T/ingest.key" >"$T/cfg"
export STUB_CFG="$T/cfg" DAEMON_LOG="$T/dlog"
inbox_mail on && ok "on: true via daemon-resolved url + key file" || bad "on via daemon"
: >"$STUB_LOG"; out=$(inbox_mail send k10 "hi" 2>&1)
grep -qx 'argv: https://dashboard.example.com/api/inbox' "$STUB_LOG" && ok "fallback: URL derived from endpoint" || bad "fallback url: $(cat "$STUB_LOG")"
grep -qx 'config: header = "Authorization: Bearer TOPSECRETKEY"' "$STUB_LOG" && ok "fallback: header carries the key file's key" || bad "fallback header"
grep -q '^argv: .*TOPSECRETKEY' "$STUB_LOG" && bad "key on argv (fallback)" || ok "fallback: key not on argv"
grep -q TOPSECRETKEY <<<"$out" && bad "key echoed in output" || ok "key never in output"
grep -q TOPSECRETKEY "$DAEMON_LOG" && bad "key reached daemon argv" || ok "key never given to the daemon"
# Env vars win: the daemon is not consulted when both are set.
: >"$STUB_LOG"; : >"$DAEMON_LOG"
LOOM_UI_INBOX_URL=http://env.test LOOM_UI_INGEST_KEY=envkey inbox_mail send k11 "hi" >/dev/null
{ grep -qx 'argv: http://env.test/api/inbox' "$STUB_LOG" && grep -q 'Bearer envkey' "$STUB_LOG" && [ ! -s "$DAEMON_LOG" ]; } \
  && ok "env vars take precedence; daemon not consulted" || bad "env precedence"
# Only the URL in env: key still comes from the file.
: >"$STUB_LOG"; LOOM_UI_INBOX_URL=http://env.test inbox_mail send k12 "hi" >/dev/null
{ grep -qx 'argv: http://env.test/api/inbox' "$STUB_LOG" && grep -q 'Bearer TOPSECRETKEY' "$STUB_LOG"; } && ok "mixed: env URL + file key" || bad "mixed"
# Nothing resolves (daemon reports only missing=…): no curl, no-op.
printf 'missing=ingest key: x\n' >"$T/cfg"; : >"$STUB_LOG"
out=$(inbox_mail send k13 "hi"); { grep -q "not configured" <<<"$out" && [ ! -s "$STUB_LOG" ]; } && ok "unresolved: no-op" || bad "unresolved"
# Old daemon / no subcommand: today's env-only behavior.
unset STUB_CFG; inbox_mail on && bad "old daemon: on" || ok "old daemon: falls back to env-only (off)"

# mail-send.md Phase 2 block: delivers the inbox leg via file+endpoint fallback.
awk '/^```bash$/{n++; f=(n==1)} /^```$/{f=0} f&&!/^```bash$/' "$ROOT/defaults/.claude/commands/loom/mail-send.md" >"$T/send.sh"
[ -s "$T/send.sh" ] || bad "mail-send fence not found"
printf 'url=https://dashboard.example.com\nkey_file=%s\n' "$T/ingest.key" >"$T/cfg"; export STUB_CFG="$T/cfg"
printf '#!/usr/bin/env bash\necho "$$event"\n' | sed 's/\$\$/\\$/' >"$T/post.sh"; chmod +x "$T/post.sh"
: >"$STUB_LOG"
out=$(MATRIX_POST="$T/post.sh" TO=@op:x BODY="need a token" bash "$T/send.sh" 2>&1); rc=$?
{ [ $rc -eq 0 ] && grep -q '^loom-ui: ok' <<<"$out"; } && ok "mail-send: delivered via fallback" || bad "mail-send fallback rc=$rc: $out"
grep -qx 'argv: https://dashboard.example.com/api/inbox' "$STUB_LOG" && grep -q 'Bearer TOPSECRETKEY' "$STUB_LOG" && ok "mail-send: URL + header from fallback" || bad "mail-send url/header"
grep -q '^argv: .*TOPSECRETKEY' "$STUB_LOG" && bad "mail-send: key on argv" || ok "mail-send: key not on argv"
grep -q TOPSECRETKEY <<<"$out" && bad "mail-send: key in output" || ok "mail-send: key not in output"
# Env vars win over whatever the daemon resolves.
: >"$STUB_LOG"
out=$(LOOM_UI_INBOX_URL=http://env.test LOOM_UI_INGEST_KEY=envkey MATRIX_POST="$T/post.sh" TO=@op:x BODY=b bash "$T/send.sh" 2>&1); rc=$?
{ [ $rc -eq 0 ] && grep -qx 'argv: http://env.test/api/inbox' "$STUB_LOG" && grep -q 'Bearer envkey' "$STUB_LOG" && ! grep -q TOPSECRETKEY "$STUB_LOG"; } \
  && ok "mail-send: env vars take precedence" || bad "mail-send env precedence rc=$rc"
unset STUB_CFG
out=$(MATRIX_POST="$T/post.sh" TO=@op:x BODY=b bash "$T/send.sh" 2>&1); rc=$?
{ [ $rc -eq 2 ] && grep -q 'SEND NOT ATTEMPTED' <<<"$out" && grep -q 'ingest.key' <<<"$out"; } && ok "mail-send: names what is missing (exit 2)" || bad "mail-send missing rc=$rc"
# shellcheck disable=SC2088 # the literal "~/..." is the documented path text being matched, not a path to expand
grep -qF '~/.config/loom-ui/ingest.key' <<<"$out" && ok "mail-send: missing names the dashboard key file first" || bad "mail-send missing location: $out"

# Key-file tier order (#10137 builder caution) is resolved daemon-side (stubbed here);
# pin the documented order: dashboard tiers before every telemetry tier.
tiers=$(tr '\n' ' ' <"$DOC" | grep -o 'LOOM_UI_INGEST_KEY_FILE.*observability/ingest\.key' | head -1)
# shellcheck disable=SC2088 # the literal "~/..." is the documented path text being matched, not a path to expand
{ [ -n "$tiers" ] && grep -qF '~/.config/loom-ui/ingest.key' <<<"$tiers" && grep -qF 'LOOM_OBSERVABILITY_INGEST_KEY_FILE' <<<"$tiers"; } \
  && ok "doc: dashboard key tiers precede telemetry tiers" || bad "doc tier order"
grep -q 'https.*/ingest' "$DOC" && ok "doc: URL derived only from https /ingest" || bad "doc URL rule"


[ "$fails" -eq 0 ] && echo "ALL PASSED" || { echo "$fails failed"; exit 1; }
