#!/usr/bin/env bash
# installed-file-guard.sh — repo-identity discriminator + installed-managed-path
# containment for the "do not edit installed Loom files in place" guard (#7995).
#
# Source this file (do not exec).
#
# ---------------------------------------------------------------------------
# WHY
# ---------------------------------------------------------------------------
# In a repo that is NOT Loom's own source tree, everything under
# `.loom/hooks|scripts|roles|docs|bin/` and `.claude/commands/loom/` is a
# resync-refreshed COPY of Loom's `defaults/`. An in-place edit there is
# reverted by the next `resync-installed.sh` run — or, if the file was added
# rather than edited, orphaned with no upstream counterpart — silently, after
# the PR merges. #7883 shipped that rule as prose in `builder.md`/`doctor.md`
# and `defaults/docs/repo-owned-files.md`; this library is the mechanical half
# it asked for (#7883 AC #4), consumed by `guard-worktree-paths.sh` (the
# Edit/Write matcher) and `guard-loom-workflow.sh` (the Bash matcher).
#
# Only two dispositions are valid for such a change, and the deny message names
# both: UPSTREAM it (a PR against `rjwalters/loom`'s matching
# `defaults/<relative path>`), or PIN it (`.loom/resync-ignore`).
#
# ---------------------------------------------------------------------------
# THE REPO-IDENTITY DISCRIMINATOR (the decision #7995 asked a Builder to make)
# ---------------------------------------------------------------------------
# A guard that cannot tell "I am a Loom consumer repo" from "I am
# rjwalters/loom itself" is worse than the prose-only status quo: it would deny
# Loom's own Builders every legitimate edit to `.loom/` in Loom's source tree.
# Nothing in a hook could make that distinction before this file existed
# (verified while curating #7883: zero matches for
# `install-metadata|is_loom_repo|rjwalters/loom|dogfood` across
# `defaults/hooks/guard-*.sh`).
#
# Three candidates were weighed (#7995's body):
#
#   1. A new `.loom/install-metadata.json` field (`"upstream_source": …`),
#      written at install time. REJECTED: it needs a migration story for every
#      already-installed repo, and until that migration completes the field is
#      absent everywhere — so the guard is inert on exactly the repos that have
#      been carrying installed copies the longest. It also adds a field whose
#      only consumer is this guard.
#   2. The forge remote (`rjwalters/loom`). REJECTED: wrong for forks (a fork
#      of Loom IS an upstream source tree and must stay permissive), wrong for
#      a locally-renamed remote, wrong for a mirror, and it reads git config
#      rather than the filesystem.
#   3. STRUCTURAL DETECTION — CHOSEN. `defaults/.claude/commands/loom/` exists
#      at the checkout root. That directory is where Loom's role prompts and
#      slash-command bodies live in Loom's own source tree, and it is NEVER
#      installed into a consumer repo (the installer ships those files to
#      `.claude/commands/loom/`, never under `defaults/`). No migration, no new
#      field, no network, no git call — one `[[ -d ]]`. A Loom FORK is
#      correctly classified as an upstream source tree, which is the outcome
#      the remote-based option gets wrong.
#
# It is an inference rather than a declaration, which is the honest cost of
# option 3. That cost is bounded by the fail-permissive rule below: the only
# thing a wrong inference can do here is fail to deny.
#
# ---------------------------------------------------------------------------
# FAIL PERMISSIVE — the non-negotiable direction
# ---------------------------------------------------------------------------
# `loom_repo_identity` is THREE-valued, not boolean. "Cannot determine"
# (`unknown`) and "Loom's own tree" (`upstream`) both resolve to NO DENIAL.
# A denial requires an affirmative `consumer` verdict, which requires BOTH
# (a) no `defaults/.claude/commands/loom/`, and (b) an affirmative "Loom is
# installed here" marker (`.loom/install-metadata.json`, `.loom/config.json`,
# or `.loom-project/project.json`). A missing, stale, or unreadable marker
# therefore never blocks a legitimate edit — it degrades to today's
# prose-only behavior.
#
# Every function here is best-effort and returns non-zero rather than erroring,
# so a consuming guard's fail-open contract is preserved even if this file is
# half-broken.

# The installed managed prefixes, as one anchored ERE over an ABSOLUTE path.
# `.*` is greedy, so capture group 1 is the LONGEST possible prefix — i.e. the
# LAST occurrence of a managed prefix in the path. That matters for the nested
# case a Builder actually hits: in
# `<repo>/.loom/worktrees/issue-7/.loom/hooks/x.sh` the implied checkout root
# is the worktree (`<repo>/.loom/worktrees/issue-7`), not `<repo>`.
#
# Deriving the checkout root FROM THE TARGET PATH (rather than from the hook's
# cwd) is deliberate: it is pure string manipulation (zero forks), it is
# correct whether the write lands in the main checkout or in any worktree, and
# it does not depend on the acting session's cwd being inside the repo at all.
LOOM_IFW_MANAGED_RE='^(.*)/(\.loom/(hooks|scripts|roles|docs|bin)|\.claude/commands/loom)(/.*)?$'

# Cheap substring pre-check: true when $1 mentions any managed prefix at all.
# Consumers call this BEFORE any tokenizing/parsing work, so the overwhelming
# majority of tool calls pay nothing beyond a handful of bash pattern matches.
loom_installed_prefix_mentioned() {
    local text="${1:-}"
    [[ -n "$text" ]] || return 1
    case "$text" in
        *".loom/hooks"*|*".loom/scripts"*|*".loom/roles"*|*".loom/docs"*|*".loom/bin"*|*".claude/commands/loom"*)
            return 0 ;;
    esac
    return 1
}

# loom_repo_identity <checkout_root> -> echoes exactly one of:
#   upstream  — this checkout IS a Loom source tree (Loom's own repo, or a fork
#               of it). Never denied: editing `.loom/` here is legitimate, and
#               includes every resync.
#   consumer  — Loom is installed here AND this is not a Loom source tree.
#   unknown   — cannot determine. Treated exactly like `upstream` by callers.
#
# O(1): at most four filesystem stats, no forks, no network, no git.
loom_repo_identity() {
    local root="${1:-}"
    if [[ -z "$root" || ! -d "$root" ]]; then
        printf 'unknown'
        return 0
    fi
    # (1) Loom's own source tree (or a fork of it). Checked FIRST so a source
    #     tree — which also carries an installed `.loom/` for dogfooding — can
    #     never be misread as a consumer.
    if [[ -d "$root/defaults/.claude/commands/loom" ]]; then
        printf 'upstream'
        return 0
    fi
    # (2) An affirmative "Loom is installed in this repo" marker. Without one,
    #     a repo that merely happens to have a `.loom/docs/` directory of its
    #     own is NOT classified as a consumer.
    if [[ -f "$root/.loom/install-metadata.json" \
       || -f "$root/.loom/config.json" \
       || -f "$root/.loom-project/project.json" ]]; then
        printf 'consumer'
        return 0
    fi
    printf 'unknown'
}

# loom_installed_managed_split <absolute_path>
#
# When the path is under an installed managed prefix, sets:
#   LOOM_IFW_ROOT      the implied checkout root
#   LOOM_IFW_REL       the report-relative path `.loom/resync-ignore` uses
#                      ("hooks/foo.sh", "commands/loom/builder.md", …)
#   LOOM_IFW_REPO_REL  the repo-relative path (".loom/hooks/foo.sh", …)
# and returns 0. Returns 1 otherwise.
loom_installed_managed_split() {
    local path="${1:-}"
    [[ -n "$path" && "$path" == /* ]] || return 1
    [[ "$path" =~ $LOOM_IFW_MANAGED_RE ]] || return 1
    local root="${BASH_REMATCH[1]}" prefix="${BASH_REMATCH[2]}" tail="${BASH_REMATCH[4]}"
    [[ -n "$root" ]] || root="/"
    LOOM_IFW_ROOT="$root"
    LOOM_IFW_REPO_REL="${prefix}${tail}"
    case "$prefix" in
        ".claude/commands/loom") LOOM_IFW_REL="commands/loom${tail}" ;;
        *)                       LOOM_IFW_REL="${prefix#.loom/}${tail}" ;;
    esac
    return 0
}

# loom_installed_path_pinned <checkout_root> <report_rel> <repo_rel>
#
# True when the path is declared repo-owned in `<root>/.loom/resync-ignore`.
# A pinned file is NOT overwritten by a resync, so editing it in place is
# legitimate and must not be denied — the pin IS one of the two sanctioned
# dispositions, and a guard that blocked it after the repo declared it would be
# denying the remedy it recommends.
#
# Matching mirrors `resync-installed.sh`'s is_ignored(): exact string, no
# globs, `#` comments and blank lines ignored, and both the ".loom/"-relative
# spelling and the natural repo-relative spelling (optionally "./"-prefixed)
# accepted (#6515).
loom_installed_path_pinned() {
    local root="${1:-}" rel="${2:-}" repo_rel="${3:-}"
    local ignore_file="$root/.loom/resync-ignore"
    [[ -n "$rel" && -f "$ignore_file" ]] || return 1
    local line
    while IFS= read -r line || [[ -n "$line" ]]; do
        line="${line%%#*}"
        line="${line#"${line%%[![:space:]]*}"}"
        line="${line%"${line##*[![:space:]]}"}"
        [[ -z "$line" ]] && continue
        line="${line#./}"
        [[ "$line" == "$rel" || "$line" == "$repo_rel" ]] && return 0
        [[ "${line#.loom/}" == "$rel" ]] && return 0
    done < "$ignore_file" 2>/dev/null
    return 1
}

# loom_installed_write_denied <absolute_path>
#
# The single decision function. Returns 0 (deny) only when ALL of:
#   - the path resolves under an installed managed prefix,
#   - the implied checkout root classifies as `consumer`, and
#   - the path is not pinned in that root's `.loom/resync-ignore`.
# On a 0 return, LOOM_IFW_ROOT / LOOM_IFW_REL / LOOM_IFW_REPO_REL are set for
# the deny message. Every other case returns 1 (permissive).
loom_installed_write_denied() {
    local path="${1:-}"
    loom_installed_managed_split "$path" || return 1
    [[ "$(loom_repo_identity "$LOOM_IFW_ROOT")" == "consumer" ]] || return 1
    loom_installed_path_pinned "$LOOM_IFW_ROOT" "$LOOM_IFW_REL" "$LOOM_IFW_REPO_REL" && return 1
    return 0
}

# loom_installed_guard_enabled <checkout_root>
#
# Category toggle `guards.installedFileWrites` / LOOM_GUARD_INSTALLED_FILE_WRITES.
# Default ON. Resolution order (highest precedence first), matching every other
# guard category in this repo:
#   1. LOOM_GUARD_INSTALLED_FILE_WRITES (0/false/no disables, 1/true/yes forces on)
#   2. tiered config -> guards.installedFileWrites (default true when absent)
#   3. default true
# The config read is best-effort and requires `loom_config_get`
# (config-resolver.sh) to have been sourced by the caller; without it the
# default stands.
loom_installed_guard_enabled() {
    local root="${1:-}" enabled=true raw
    if [[ -n "$root" ]] && command -v loom_config_get >/dev/null 2>&1; then
        raw=$(loom_config_get "$root" "guards.installedFileWrites" "true" 2>/dev/null) || raw=true
        [[ "$raw" == "false" ]] && enabled=false
    fi
    case "${LOOM_GUARD_INSTALLED_FILE_WRITES:-}" in
        0|false|no)  enabled=false ;;
        1|true|yes)  enabled=true ;;
    esac
    [[ "$enabled" == "true" ]]
}

# loom_installed_deny_reason <absolute_path> <tool_hint>
#
# The deny message. Names BOTH sanctioned dispositions (upstream / pin), the
# concrete `defaults/` path to upstream against, and the category toggle — with
# the same "an inline env prefix does NOT reach this hook" warning the sibling
# guards carry (#6110), because the hook runs as a separate process. Must be
# called after loom_installed_write_denied() has set LOOM_IFW_*.
loom_installed_deny_reason() {
    local path="${1:-}" tool="${2:-Write}"
    printf '%s' "BLOCKED: ${tool} to '${path}' edits an INSTALLED Loom file in place. This repository is a Loom consumer install ('${LOOM_IFW_ROOT}' has no defaults/.claude/commands/loom/, so it is not Loom's own source tree), where .loom/hooks|scripts|roles|docs|bin/ and .claude/commands/loom/ are resync-refreshed copies of Loom's defaults/. An edit here is reverted by the next 'resync-installed.sh' -- or, if you ADDED the file, orphaned with no upstream counterpart -- silently, after your PR merges. Two dispositions are valid; pick one. (1) UPSTREAM it: open a PR against rjwalters/loom's 'defaults/${LOOM_IFW_REL}'; the fix returns here via the normal 'chore: resync installed Loom surfaces' commit. (2) PIN it: add '${LOOM_IFW_REL}' to this repo's .loom/resync-ignore (with a comment saying why, and which upstream PR retires the pin if it is temporary), then re-run the edit. See .loom/docs/repo-owned-files.md. Do NOT retry this write through the other tool -- both the Edit/Write matcher and the Bash write idioms (>, >>, tee, sed -i, cp/mv) are confined the same way. Not a Builder and need to write here directly? Set guards.installedFileWrites:false in .loom/config.json for the session -- an inline 'LOOM_GUARD_INSTALLED_FILE_WRITES=0 <command>' prefix does NOT work (this hook runs as a separate process). (#7995)"
}

# loom_ifw_normalize_abs <path> [cwd]
#
# Make $1 absolute against $2 and collapse `.` / `..` / duplicate slashes
# LEXICALLY (no filesystem access, no forks, no symlink resolution, no glob
# expansion). Echoes nothing and returns 1 when the path cannot be made
# absolute.
#
# Lexical-only is sufficient here and deliberately NOT upgraded to
# loom_canonical_path: the managed-prefix match is a pure string test, so a
# path reached through a symlinked ancestor simply keeps its logical spelling
# and still matches. The failure direction of any residual mismatch is a missed
# deny (permissive), never a false one.
loom_ifw_normalize_abs() {
    local path="${1:-}" cwd="${2:-}"
    [[ -n "$path" ]] || return 1
    if [[ "$path" != /* ]]; then
        [[ -n "$cwd" && "$cwd" == /* ]] || return 1
        path="${cwd%/}/$path"
    fi
    local rest="${path#/}" seg out=''
    while [[ -n "$rest" ]]; do
        seg="${rest%%/*}"
        if [[ "$rest" == */* ]]; then rest="${rest#*/}"; else rest=''; fi
        case "$seg" in
            ''|'.') continue ;;
            '..')   out="${out%/*}" ;;
            *)      out="$out/$seg" ;;
        esac
    done
    printf '%s' "${out:-/}"
}

# loom_bash_write_targets <command_string>
#
# Emit, one per line, every argument of $1 that a recognized Bash WRITE idiom
# would write to: `>`/`>>` (and `2>`, `&>`, `>|`) redirection, `tee`, `sed -i`,
# and the destination of `cp`/`mv`/`install`/`rsync`. That is exactly the idiom
# set `guard-destructive-generic.sh`'s write-confinement category already
# handles (#4178).
#
# It is a quote-aware TOKENIZER, not a shell evaluator, and it is deliberately
# narrow. Known, ACCEPTED limitations — every one of them fails toward
# PERMISSIVE (a missed deny, never a false one):
#   - an unexpanded `$VAR` in a target is left as-is and will not match a
#     managed prefix;
#   - a `cd` earlier on the same command line is not tracked, so a relative
#     target is resolved against the tool call's own cwd;
#   - `dd of=`, `python -c`, and writes performed inside an invoked script are
#     not recognized at all.
# The Edit/Write matcher (guard-worktree-paths.sh) is the primary surface; this
# closes the "denied on Edit/Write, retry via Bash" fallback, which is the same
# threat model #4178 documents.
loom_bash_write_targets() {
    local s="${1:-}"
    # `n` is assigned in its OWN statement: bash expands every word of a
    # `local` command before performing any of its assignments, so an
    # `n=${#s}` sharing the statement that assigns `s` would read the OLD `s`.
    local n=${#s}
    local i=0 c q='' tok='' started=0
    local -a toks=()
    while (( i < n )); do
        c="${s:i:1}"
        if [[ -n "$q" ]]; then
            if [[ "$c" == "$q" ]]; then q=''; else tok+="$c"; fi
            started=1; (( i++ )); continue
        fi
        if [[ "$c" == "\\" ]]; then
            (( i++ )); tok+="${s:i:1}"; started=1; (( i++ )); continue
        fi
        case "$c" in
            "'"|'"')
                q="$c"; started=1 ;;
            ' '|$'\t')
                if (( started )); then toks+=("$tok"); tok=''; started=0; fi ;;
            $'\n'|';'|'&'|'|'|'('|')'|'{'|'}')
                if (( started )); then toks+=("$tok"); tok=''; started=0; fi
                toks+=($'\001SEP') ;;
            '<')
                if (( started )); then toks+=("$tok"); tok=''; started=0; fi
                toks+=($'\001IN') ;;
            '>')
                if (( started )); then toks+=("$tok"); tok=''; started=0; fi
                while [[ "${s:i+1:1}" == '>' || "${s:i+1:1}" == '|' ]]; do (( i++ )); done
                toks+=($'\001OUT') ;;
            *)
                tok+="$c"; started=1 ;;
        esac
        (( i++ ))
    done
    (( started )) && toks+=("$tok")

    local t cmd='' base='' pending='' sed_inplace=0 a
    local -a args=()
    for t in ${toks[@]+"${toks[@]}"}; do
        case "$t" in
            $'\001SEP')
                # End of a simple command: emit its idiom-specific targets.
                case "$base" in
                    tee) for a in ${args[@]+"${args[@]}"}; do printf '%s\n' "$a"; done ;;
                    sed) if (( sed_inplace )); then
                             for a in ${args[@]+"${args[@]}"}; do printf '%s\n' "$a"; done
                         fi ;;
                    cp|mv|install|rsync)
                        if [[ ${#args[@]} -ge 2 ]]; then printf '%s\n' "${args[$((${#args[@]} - 1))]}"; fi ;;
                esac
                cmd=''; base=''; args=(); sed_inplace=0; pending=''
                continue ;;
            $'\001OUT') pending=out; continue ;;
            $'\001IN')  pending=in;  continue ;;
        esac
        if [[ "$pending" == out ]]; then printf '%s\n' "$t"; pending=''; continue; fi
        if [[ "$pending" == in ]];  then pending=''; continue; fi
        if [[ -z "$cmd" ]]; then
            # Skip leading `VAR=value` env assignments and the usual wrappers.
            if [[ "$t" == *=* && "$t" != /* && "$t" != -* ]]; then continue; fi
            case "${t##*/}" in
                sudo|env|command|nohup|time|xargs) continue ;;
            esac
            cmd="$t"; base="${t##*/}"
            continue
        fi
        case "$t" in
            -i|-i.*|--in-place|--in-place=*) [[ "$base" == sed ]] && sed_inplace=1 ;;
        esac
        [[ "$t" == -* ]] && continue
        args+=("$t")
    done
    # Flush the final simple command (no trailing separator).
    case "$base" in
        tee) for a in ${args[@]+"${args[@]}"}; do printf '%s\n' "$a"; done ;;
        sed) if (( sed_inplace )); then
                 for a in ${args[@]+"${args[@]}"}; do printf '%s\n' "$a"; done
             fi ;;
        cp|mv|install|rsync)
            if [[ ${#args[@]} -ge 2 ]]; then printf '%s\n' "${args[$((${#args[@]} - 1))]}"; fi ;;
    esac
    return 0
}
