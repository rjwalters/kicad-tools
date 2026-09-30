#!/usr/bin/env bash
# lib/cargo-target-dir.sh — resolve, and safely reclaim, a worktree's Cargo
# target directory (issue #7239).
#
# ## Why this exists
#
# Cargo's build output is NOT always `<workspace>/target`. `CARGO_TARGET_DIR`
# or `build.target-dir` in any `config.toml` on the lookup path can redirect it
# anywhere — commonly to a large external volume. Loom's worktree lifecycle
# creates one such directory per worktree, but every removal path
# (`worktree.sh remove`, the daemon reaper) only ever removed the worktree
# directory itself, so a redirected target dir outlived its worktree forever.
# On one multi-agent host that accumulated tens of orphaned directories and
# hundreds of GB before anyone noticed.
#
# ## Three parts
#
#   1. `loom_resolve_cargo_target_dir <workspace_root>` — Cargo's resolution
#      order (env → `cargo metadata` → `<root>/target`). This is a LIBRARY
#      twin of the standalone `scripts/cargo-target-dir.sh` (which is part of
#      the Loom repo's own install path and is not shipped to consumer repos).
#      **The two must stay byte-identical in behavior**;
#      `defaults/scripts/tests/test-cargo-target-dir-reclaim.sh` asserts parity
#      between them on every branch of the resolution order, so drift fails CI
#      rather than silently changing what gets deleted.
#
#   2. `loom_reclaim_worktree_target_dir …` — the removal-time gate: reclaim a
#      resolved target dir ONLY when it is redirected outside the worktree, is
#      attributable to that worktree, is not shared with any other live
#      worktree, is not held open by a running process, and is not one of the
#      paths this pass must never touch.
#
#   3. `loom_is_per_worktree_target_dir` / `loom_read_worktree_target_dir_marker`
#      (issue #8458) — two thin wrappers over `loom-daemon cargo-target-dir`,
#      consulted by parts 1 and 2 so they recognise a target dir the CREATION
#      side (`loom-daemon cargo-target-dir provision`, called by `worktree.sh`)
#      gave this worktree under the otherwise-shared root and recorded in a
#      marker file INSIDE it. See "Per-worktree target dirs" below.
#
# ## The attribution rule (the one that keeps costing data)
#
# Only a redirect derived from the WORKTREE ITSELF can be evidence that a
# directory belongs to it. Three inputs look like evidence and are not:
#
#   * the remover's own `CARGO_TARGET_DIR` — read from this process's
#     environment, machine- or session-global by construction;
#   * a `build.target-dir` in `$CARGO_HOME/config.toml` or in any ancestor
#     `.cargo/config.toml` above the worktree — equally global, and resolving
#     identically for every path on the host;
#   * a DEGRADED resolution of some other live worktree — when `cargo metadata`
#     fails for a sibling, "it builds somewhere else" and "we could not find out
#     where it builds" are the same `<root>/target` string, and only the first
#     of those licenses a deletion.
#
# Each of the three deleted a machine-global shared cargo cache in review
# reproductions of this file. The first two are handled by
# `loom_cargo_target_dir_redirect_possible` (which consults only the worktree's
# own config files) plus gate 2f; the third by
# `loom_resolve_worktree_target_dir_checked` plus the exit-3 fail-closed path in
# `loom_target_dir_shared_with`.
#
# The Rust daemon has an equivalent (`loom-daemon/src/worktree_ops/cargo_target.rs`)
# for its own removal path; the two are deliberately parallel implementations
# of the same rules, each with its own tests, because the daemon runs against
# repos where this library is not installed.
#
# Everything here writes diagnostics to stderr only: the reclaim function
# emits ONE tab-separated record on stdout so callers (which may be in
# `--json` stdout-purity mode) decide how to render it.

# --------------------------------------------------------------------------
# Resolution
# --------------------------------------------------------------------------

# Resolve a possibly-relative Cargo path against a workspace root, without
# requiring it to exist (the target dir is created by the build itself).
_loom_ctd_absolutize() {
    local value="$1" root="$2"
    case "$value" in
        /*) printf '%s\n' "$value" ;;
        *)  printf '%s\n' "$root/$value" ;;
    esac
}

# loom_resolve_cargo_target_dir <workspace_root>
#
# Prints Cargo's actual target directory for <workspace_root>. Mirrors
# scripts/cargo-target-dir.sh exactly:
#   1. $CARGO_TARGET_DIR when set and non-empty (env beats config in Cargo).
#   2. `cargo metadata --format-version 1 --no-deps` (applies the full
#      config.toml hierarchy, including build.target-dir).
#   3. `<workspace_root>/target` — Cargo's default.
# Always exits 0 with a path on stdout; a resolution hiccup degrades to the
# historical hardcoded assumption rather than to a hard failure.
#
# `_loom_ctd_metadata_target_dir <root>` is the middle step, factored out so the
# reclaim path can tell "cargo says <root>/target" apart from "cargo could not
# answer" — a distinction `loom_resolve_cargo_target_dir` itself must NOT make,
# because it has to stay behaviorally identical to scripts/cargo-target-dir.sh.
# Prints the absolutized target_directory on stdout, exit 0. Exit 1 means cargo
# is missing, exited non-zero, or emitted no usable field.
_loom_ctd_metadata_target_dir() {
    local root="$1"
    command -v cargo >/dev/null 2>&1 || return 1

    local metadata resolved=""
    metadata="$(cd "$root" 2>/dev/null && cargo metadata --format-version 1 --no-deps 2>/dev/null)" || metadata=""
    [[ -n "$metadata" ]] || return 1

    if command -v jq >/dev/null 2>&1; then
        resolved="$(printf '%s' "$metadata" | jq -r '.target_directory // empty' 2>/dev/null)"
    else
        resolved="$(printf '%s' "$metadata" | sed -n 's/.*"target_directory":"\([^"]*\)".*/\1/p')"
    fi
    [[ -n "$resolved" && "$resolved" != "null" ]] || return 1

    _loom_ctd_absolutize "$resolved" "$root"
}

loom_resolve_cargo_target_dir() {
    local root="$1"

    if [[ -n "${CARGO_TARGET_DIR:-}" ]]; then
        _loom_ctd_absolutize "$CARGO_TARGET_DIR" "$root"
        return 0
    fi

    local resolved
    if resolved="$(_loom_ctd_metadata_target_dir "$root")"; then
        printf '%s\n' "$resolved"
        return 0
    fi

    printf '%s\n' "$root/target"
}

# loom_cargo_target_dir_redirect_possible <workspace_root>
#
# Cheap pre-check: is a redirect even conceivable *for this worktree*? Returns
# 0 (yes) when the root carries a Cargo manifest AND either CARGO_TARGET_DIR is
# set or a `.cargo/config.toml` INSIDE the root mentions `target-dir`. Returns
# 1 (no) otherwise — in which case this pass treats the target dir as
# `<workspace_root>/target` and the caller can skip the `cargo metadata`
# subprocess entirely.
#
# This keeps the common (unredirected) host at zero added cost per removal:
# a handful of small file reads instead of a cargo invocation.
#
# ## Why the candidates stop at the worktree boundary
#
# This is an ATTRIBUTION question ("is this directory the worktree's own?"),
# not a faithful reimplementation of Cargo's config lookup. A `target-dir` in
# `$CARGO_HOME/config.toml` — or in any ancestor `.cargo/config.toml` above the
# worktree — is exactly as machine- or session-global as the CARGO_TARGET_DIR
# env var: it resolves identically for every path on the host, so it can never
# establish that a directory belongs to the one worktree being removed. Before
# this restriction, a `$CARGO_HOME` redirect made a worktree resolve straight to
# the machine-global cache (which the sharing scan then failed to see any
# referent for, because every manifest-less tree is skipped and the primary
# checkout resolves through cargo, which can fail), and the shared cache was
# `rm -rf`'d. The per-worktree redirect this pass exists to reclaim can only
# come from a config INSIDE the worktree, so nothing reclaimable is lost.
loom_cargo_target_dir_redirect_possible() {
    local root="$1"

    # No manifest ⇒ nothing here ever built with cargo ⇒ nothing to redirect,
    # and in particular an ambient CARGO_TARGET_DIR is NOT evidence that this
    # tree owns the directory it names. This test comes FIRST, before the env
    # short-circuit, so the worktree being removed is judged by exactly the
    # same manifest rule `loom_target_dir_shared_with` already applies to every
    # OTHER worktree. When it came second, a manifest-less worktree resolved to
    # the machine-global env path while every sibling was skipped as a referent
    # — so nothing looked shared and the shared cache was deleted.
    [[ -f "$root/Cargo.toml" ]] || return 1

    # A Loom-provisioned per-worktree redirect (#8458). Read from a file INSIDE
    # the worktree, so — exactly like the `.cargo/config.toml` case below — it is
    # worktree-derived attribution evidence, not a machine-global source. The
    # negated form is deliberate: `[[ -f … ]] && return 0` would leave this
    # function returning 1 from a FAILED test under `set -e` on the ordinary
    # (no-marker) host, which is the caller's "no redirect" answer by accident
    # rather than by construction.
    [[ ! -f "$root/.loom-cargo-target-dir" ]] || return 0

    # An ambient CARGO_TARGET_DIR still makes a redirect *possible* (Cargo
    # honors it), so resolution must not skip it — but the reclaim step refuses
    # to delete a path that is only ever that env value. See gate 2f.
    [[ -n "${CARGO_TARGET_DIR:-}" ]] && return 0

    # ONLY the worktree's own config files. Ancestors and $CARGO_HOME are
    # machine-global sources; see the header comment above.
    local f
    for f in "$root/.cargo/config.toml" "$root/.cargo/config"; do
        [[ -f "$f" ]] || continue
        grep -qE '^[[:space:]]*target-dir[[:space:]]*=' "$f" 2>/dev/null && return 0
    done
    return 1
}

# loom_resolve_worktree_target_dir_checked <worktree_path>
#
# Resolve <worktree_path>'s target dir AND report whether the answer is
# trustworthy. Prints the resolved path on stdout in every case.
#
# Exit status:
#   0  the answer is definite (no redirect is configured, or the redirect was
#      read successfully)
#   2  a redirect IS configured for this tree but could not be read (`cargo
#      metadata` is missing, exited non-zero — a mid-edit manifest, a
#      conflicted merge — or emitted no target_directory). stdout carries the
#      degraded `<worktree_path>/target` fallback.
#
# Callers that are about to DELETE something must fail closed on 2. Silently
# degrading to `<root>/target` is how a sibling stops looking like a sharer of
# the dir we are about to remove: "this tree builds somewhere else" and "we
# could not find out where this tree builds" must not look alike, the same rule
# `loom_target_dir_shared_with` already applies to `git worktree list`.
loom_resolve_worktree_target_dir_checked() {
    local worktree_path="$1"

    # #8458: a Loom-provisioned per-worktree redirect wins outright, ahead of
    # even the env var. It is the strongest available *per-worktree* statement:
    # written into the worktree by `loom-daemon cargo-target-dir provision`, and the
    # same value the spawn path exports as CARGO_TARGET_DIR for that worktree's
    # builds — so in the normal case the two agree and the order is moot. When
    # they disagree (an operator exported a private dir of their own over the
    # top), preferring the marker reclaims only the directory Loom itself
    # provisioned and leaves the operator's alone: the safe direction, and the
    # only one of the two that is attributable to this worktree at all.
    local marker; marker="$(loom_read_worktree_target_dir_marker "$worktree_path" || true)"
    [[ -z "$marker" ]] || { printf '%s\n' "$marker"; return 0; }

    if ! loom_cargo_target_dir_redirect_possible "$worktree_path"; then
        printf '%s\n' "$worktree_path/target"
        return 0
    fi

    # Env beats config in Cargo, and needs no subprocess: a definite answer.
    if [[ -n "${CARGO_TARGET_DIR:-}" ]]; then
        _loom_ctd_absolutize "$CARGO_TARGET_DIR" "$worktree_path"
        return 0
    fi

    local resolved
    if resolved="$(_loom_ctd_metadata_target_dir "$worktree_path")"; then
        printf '%s\n' "$resolved"
        return 0
    fi

    printf '%s\n' "$worktree_path/target"
    return 2
}

# loom_resolve_worktree_target_dir <worktree_path>
#
# The removal-path entry point: resolve <worktree_path>'s target dir, skipping
# the expensive branch when no redirect is possible. MUST be called while the
# worktree still exists on disk — `cargo metadata` needs its manifest.
#
# Always exits 0. For the worktree BEING REMOVED a degraded answer is the safe
# direction (`<worktree>/target` is `inside` ⇒ a no-op, i.e. a missed reclaim),
# so the status is deliberately dropped here; the sharing scan, where a degraded
# answer would instead license a deletion, uses the `_checked` form above.
loom_resolve_worktree_target_dir() {
    loom_resolve_worktree_target_dir_checked "$1" || true
}

# --------------------------------------------------------------------------
# Reclaim gates
# --------------------------------------------------------------------------

# Best-effort physical path (symlinks resolved) for comparison purposes. A
# non-existent path is returned unchanged rather than dropped: containment
# checks still need something to compare.
_loom_ctd_realpath() {
    local p="$1"
    if [[ -d "$p" ]]; then
        (cd "$p" 2>/dev/null && pwd -P) || printf '%s\n' "$p"
    else
        printf '%s\n' "$p"
    fi
}

# _loom_ctd_machine_global_target_dirs <worktree_path>
#
# Every target-dir value that comes from a MACHINE- OR SESSION-GLOBAL source
# rather than from the worktree itself, one per line as:
#
#     <absolute value>\t<human description of where it came from>
#
# Two such sources exist:
#   * the remover's own CARGO_TARGET_DIR environment variable, and
#   * `build.target-dir` in a `.cargo/config.toml` OUTSIDE the worktree — any
#     ancestor directory, or $CARGO_HOME.
#
# Both resolve identically for every path on the host, so neither can ever be
# evidence that a directory belongs to the one worktree being removed. Used by
# gate 2f. Unlike the resolver, this reads the config files directly: it runs
# AFTER the worktree is off disk, when `cargo metadata` is no longer possible,
# and the files it reads all live outside the worktree so they are still there.
_loom_ctd_machine_global_target_dirs() {
    local worktree_path="$1"

    if [[ -n "${CARGO_TARGET_DIR:-}" ]]; then
        printf '%s\t%s\n' \
            "$(_loom_ctd_absolutize "${CARGO_TARGET_DIR%/}" "$worktree_path")" \
            "the ambient CARGO_TARGET_DIR"
    fi

    local candidates=()
    # Ancestors ABOVE the worktree only — a config inside the worktree is the
    # one legitimate form of per-worktree attribution and must not appear here.
    local dir
    dir="$(dirname "$worktree_path")"
    while [[ -n "$dir" && "$dir" != "/" && "$dir" != "." ]]; do
        candidates+=("$dir/.cargo/config.toml" "$dir/.cargo/config")
        dir="$(dirname "$dir")"
    done
    candidates+=("/.cargo/config.toml" "/.cargo/config")
    local cargo_home="${CARGO_HOME:-${HOME:-}/.cargo}"
    [[ -n "${CARGO_HOME:-}" || -n "${HOME:-}" ]] && candidates+=("$cargo_home/config.toml" "$cargo_home/config")

    local f value base
    for f in "${candidates[@]}"; do
        [[ -f "$f" ]] || continue
        # TOML strings only (`target-dir = "…"` / `'…'`); a value Cargo would
        # reject is not a value we need to compare against.
        value="$(sed -n 's/^[[:space:]]*target-dir[[:space:]]*=[[:space:]]*["'"'"']\([^"'"'"']*\)["'"'"'].*/\1/p' "$f" 2>/dev/null | head -1)"
        [[ -n "$value" ]] || continue
        # Cargo resolves a relative config path against the directory holding
        # the `.cargo` directory. Getting this wrong only costs a refusal we
        # would not otherwise have made, never a deletion.
        base="$(dirname "$(dirname "$f")")"
        printf '%s\t%s\n' "$(_loom_ctd_absolutize "${value%/}" "$base")" "the target-dir in $f"
    done
}

# loom_dir_size_human <dir> — human-readable size, or "unknown".
loom_dir_size_human() {
    local dir="$1" size
    size="$(du -sh "$dir" 2>/dev/null | awk '{print $1}')" || size=""
    printf '%s\n' "${size:-unknown}"
}

# loom_target_dir_holders <dir>
#
# PIDs of live processes whose cwd or executable image is inside <dir> — the
# "never unlink a running program's files" gate the daemon applies to the
# primary checkout's own artifacts. Fail-open (prints nothing) when neither
# /proc nor lsof can answer; the other gates are the primary protection.
loom_target_dir_holders() {
    local dir="$1"
    local self=$$ parent=${PPID:-0}
    local pids=""

    if [[ -d /proc ]]; then
        local link pid target
        for link in /proc/[0-9]*; do
            pid="${link#/proc/}"
            [[ "$pid" =~ ^[0-9]+$ ]] || continue
            [[ "$pid" == "$self" || "$pid" == "$parent" ]] && continue
            local probe
            for probe in cwd exe; do
                target="$(readlink "$link/$probe" 2>/dev/null)" || continue
                [[ -n "$target" ]] || continue
                if [[ "$target" == "$dir" || "$target" == "$dir"/* ]]; then
                    pids+="$pid"$'\n'
                    break
                fi
            done
        done
    elif command -v lsof >/dev/null 2>&1; then
        local pid
        while read -r pid; do
            [[ "$pid" =~ ^[0-9]+$ ]] || continue
            [[ "$pid" == "$self" || "$pid" == "$parent" ]] && continue
            pids+="$pid"$'\n'
        done < <(lsof -t +d "$dir" 2>/dev/null || true)
    fi

    printf '%s' "$pids"
}

# loom_target_dir_shared_with <repo_root> <worktree_path> <resolved>
#
# Prints the path of another LIVE worktree (or the primary checkout) that
# resolves to the same target dir, or nothing when the dir is exclusive to
# <worktree_path>. Registered worktrees come from `git worktree list`, so this
# sees user-provisioned worktrees too, not just `.loom/worktrees/*`.
#
# Containment counts as sharing in BOTH directions: a sibling whose target dir
# is a parent of ours (the host-optimize convention of a single shared
# `target-dir` for the whole machine) must never be unlinked, and neither must
# a parent whose subtree another worktree is building into.
#
# Exit status: 0 = answered (empty stdout means "exclusive"), 2 = the question
# could not be answered at all (`git worktree list` failed: no git, not a repo,
# I/O error), 3 = a live worktree has a redirect configured that could not be
# READ (`cargo metadata` failed for it), so whether it shares this dir is
# unknown. The caller MUST fail closed on both — an empty worktree list and a
# failed enumeration are indistinguishable on stdout, and a sibling that
# silently degraded to `<root>/target` is indistinguishable from one that
# genuinely builds elsewhere. Treating either as "nobody else uses it" is
# precisely how a sibling's cache gets deleted mid-build.
#
# ## The one exception: a Loom-provisioned per-worktree dir (#8458)
#
# When `resolved` carries the per-worktree SHAPE for `worktree_path` —
# `<root>/wt/<that worktree's own directory name>`, which only
# `loom-daemon cargo-target-dir provision` ever writes — two of the rules above are
# deliberately relaxed, because both of them otherwise veto EVERY reclaim of
# such a dir and the feature can never free a byte:
#
#   * **Containment stops counting; only an exact match does.** The whole point
#     of `<root>/wt/<name>` is that it lives *under* the otherwise-shared root,
#     so the primary checkout (which resolves to `<root>` itself via the host's
#     `~/.cargo/config.toml`) is always a containing "sharer". Deleting
#     `<root>/wt/<name>` cannot harm a tree that builds into `<root>`: cargo
#     writes `debug/`, `release/`, `CACHEDIR.TAG` … directly under its own target
#     dir and never into a `wt/` subtree.
#   * **Other worktrees are resolved with the ambient CARGO_TARGET_DIR ignored.**
#     An absolute env value resolves identically for EVERY path on the host, so
#     when the remover's own environment holds this worktree's per-worktree value
#     (which is exactly what the spawn path exports), every sibling — and the
#     primary checkout — "resolves" to it and looks like a sharer. That is an
#     artifact of the env var, not evidence about the sibling. The per-worktree
#     shape is itself the proof the value is worktree-specific, so the siblings
#     are judged on what THEY carry: their own marker, or their own in-worktree
#     `.cargo/config.toml`.
#
# Neither relaxation is reachable for a path without that shape, so the
# machine-global shared cache this function exists to protect is untouched by it.
loom_target_dir_shared_with() {
    local repo_root="$1" worktree_path="$2" resolved="$3"
    local resolved_real
    resolved_real="$(_loom_ctd_realpath "$resolved")"
    local worktree_real
    worktree_real="$(_loom_ctd_realpath "$worktree_path")"

    # `sib_ctd` is the CARGO_TARGET_DIR every sibling is resolved under: the
    # ambient value normally, and EMPTY once `resolved` is attributable — see
    # "Other worktrees are resolved with the ambient CARGO_TARGET_DIR ignored"
    # above. Deciding it once here keeps the loop body a single resolve call.
    local attributed=false sib_ctd="${CARGO_TARGET_DIR:-}"
    if loom_is_per_worktree_target_dir "$worktree_path" "$resolved" "$resolved_real"; then attributed=true sib_ctd=""; fi

    local listing
    listing="$(git -C "$repo_root" worktree list --porcelain 2>/dev/null)" || return 2

    local other other_real other_target other_target_real
    while read -r other; do
        [[ -n "$other" ]] || continue
        [[ -d "$other" ]] || continue
        other_real="$(_loom_ctd_realpath "$other")"
        [[ "$other_real" == "$worktree_real" ]] && continue
        # A tree with no manifest never builds with cargo, so it cannot be
        # depending on this target dir — skip it. This matters beyond
        # performance: an ambient absolute CARGO_TARGET_DIR resolves the SAME
        # for every path, so counting manifest-less trees would report every
        # redirected dir as "shared" and reclaim nothing, ever.
        [[ -f "$other/Cargo.toml" ]] || continue
        # FAIL CLOSED on a degraded answer. `loom_resolve_worktree_target_dir`
        # silently falls back to `<root>/target` when a configured redirect
        # cannot be read — one transient `cargo metadata` failure (a mid-edit
        # Cargo.toml, a conflicted merge) would otherwise stop this sibling
        # from counting as a sharer of the very directory we are about to
        # delete. Same rule as the `git worktree list` failure above.
        other_target="$(CARGO_TARGET_DIR="$sib_ctd" loom_resolve_worktree_target_dir_checked "$other")" || return 3
        other_target_real="$(_loom_ctd_realpath "$other_target")"
        # An EXACT match always counts. CONTAINMENT counts only when `resolved`
        # is NOT attributable — see "The one exception" above.
        if [[ "$other_target_real" == "$resolved_real" ]] ||
           { [[ "$attributed" != true ]] && [[ "$resolved_real" == "$other_target_real"/* ||
                "$other_target_real" == "$resolved_real"/* ]]; }; then
            printf '%s\n' "$other"
            return 0
        fi
    done < <(printf '%s\n' "$listing" | awk '/^worktree /{print substr($0, 10)}')

    return 0
}

# loom_reclaim_worktree_target_dir <repo_root> <worktree_path> <resolved> <dry_run>
#
# Decide, and (unless dry_run is "true") act. Emits exactly one
# tab-separated record on stdout:
#
#   <status>\t<path>\t<detail>
#
# status:
#   inside          resolved to the worktree itself — removed with it, no-op
#   absent          nothing on disk at the resolved path
#   refused         a path this pass must never delete (detail = why)
#   shared          another live worktree resolves here (detail = that worktree)
#   protected       live process(es) hold it (detail = pids)
#   would-reclaim   dry run; detail = human size
#   reclaimed       removed; detail = human size
#   failed          removal attempted and failed (detail = error)
_loom_ctd_record() { printf '%s\t%s\t%s\n' "$1" "$2" "$3"; }

loom_reclaim_worktree_target_dir() {
    local repo_root="$1" worktree_path="$2" resolved="$3" dry_run="${4:-false}"

    if [[ -z "$resolved" ]]; then
        _loom_ctd_record "absent" "" "target dir could not be resolved"
        return 0
    fi

    # Normalize away a trailing slash so every comparison below is exact.
    resolved="${resolved%/}"

    local worktree_real repo_real resolved_real
    worktree_real="$(_loom_ctd_realpath "$worktree_path")"
    repo_real="$(_loom_ctd_realpath "$repo_root")"
    resolved_real="$(_loom_ctd_realpath "$resolved")"

    # 1. The default, in-worktree location: it goes away with the worktree.
    if [[ "$resolved_real" == "$worktree_real" || "$resolved_real" == "$worktree_real"/* ]]; then
        _loom_ctd_record "inside" "$resolved" "inside the worktree — removed with it"
        return 0
    fi

    # 2. Paths that must never be deleted by this pass, however they resolved.
    local depth
    depth="$(printf '%s' "${resolved_real#/}" | awk -F/ '{print NF}')"
    if [[ "$resolved_real" == "/" || "${depth:-0}" -lt 2 ]]; then
        _loom_ctd_record "refused" "$resolved" "suspiciously shallow path"
        return 0
    fi
    if [[ -n "${HOME:-}" && "$resolved_real" == "$(_loom_ctd_realpath "$HOME")" ]]; then
        _loom_ctd_record "refused" "$resolved" "resolves to \$HOME"
        return 0
    fi
    if [[ "$resolved_real" == "$repo_real" || "$repo_real" == "$resolved_real"/* ]]; then
        _loom_ctd_record "refused" "$resolved" "contains the repository itself"
        return 0
    fi
    if [[ "$resolved_real" == "$repo_real/target" ]]; then
        # The primary checkout's own build cache. It is regenerable, but it
        # belongs to the deep-clean pass (which gates on disk pressure and the
        # machine build slot), never to a single worktree's removal.
        _loom_ctd_record "refused" "$resolved" "the primary checkout's own target/"
        return 0
    fi

    # 2f. The resolved path is nothing but a MACHINE-GLOBAL redirect value: the
    #     remover's own ambient CARGO_TARGET_DIR, or a `build.target-dir`
    #     declared by a `.cargo/config.toml` outside the worktree ($CARGO_HOME
    #     or an ancestor directory). Neither is read from anything belonging to
    #     the worktree, and both resolve identically for every path on the host,
    #     so neither can establish that this directory is exclusive to the
    #     worktree being removed — while the sharing scan below deliberately
    #     skips manifest-less trees, leaving a shared cache with no visible
    #     referent at all.
    #
    #     This costs the feature nothing: the per-worktree redirect this pass
    #     exists to reclaim comes from `build.target-dir` in a `.cargo/config.toml`
    #     INSIDE the worktree, whose value is per-worktree by construction and so
    #     does not equal any of these. A genuinely per-worktree CARGO_TARGET_DIR
    #     exported into the remover's environment merely gets reported instead of
    #     deleted — the safe direction.
    #
    #     Backstop, not the primary defense: `loom_cargo_target_dir_redirect_possible`
    #     already declines to resolve THROUGH an out-of-worktree config at all.
    #     This gate additionally catches a worktree-local config that names the
    #     same directory a machine-global one does.
    #
    #     #8458 EXEMPTION: a value carrying the Loom per-worktree SHAPE —
    #     `<root>/wt/<the removed worktree's own directory name>` — is
    #     per-worktree BY CONSTRUCTION, whichever variable happens to be holding
    #     it. The shape is checked against `$worktree_path`'s own basename, so
    #     matching it is itself the attribution: a machine-global shared root
    #     (`/big/cargo-target`) can never satisfy it, and the only way an ambient
    #     CARGO_TARGET_DIR can is by naming exactly the directory the spawn path
    #     provisioned for this worktree. Without this, the per-worktree scheme
    #     would be refused on every removal made from inside the very sweep that
    #     owns the worktree — which is every removal that matters.
    #
    #     Structural rather than marker-reading on purpose: this gate runs AFTER
    #     the worktree is off disk, so the in-worktree marker that produced the
    #     value is already gone and cannot be consulted here.
    local mg_value mg_source
    while IFS=$'\t' read -r mg_value mg_source; do
        [[ -n "$mg_value" ]] || continue
        if [[ "$resolved_real" == "$(_loom_ctd_realpath "$mg_value")" ]]; then
            if loom_is_per_worktree_target_dir "$worktree_path" "$resolved" "$resolved_real"; then break; fi
            _loom_ctd_record "refused" "$resolved" \
                "$mg_source is machine-global, not exclusive to this worktree"
            return 0
        fi
    done < <(_loom_ctd_machine_global_target_dirs "$worktree_path")

    # 3. Nothing there (never built, or already reclaimed).
    if [[ ! -d "$resolved_real" ]]; then
        _loom_ctd_record "absent" "$resolved" "no directory at the resolved path"
        return 0
    fi

    # 4. Shared with a still-live worktree (the host-optimize single-shared-
    #    target-dir convention). Deleting it would destroy a sibling's cache
    #    mid-build. Exit 2 means the question was unanswerable — fail closed.
    # `|| shared_rc=$?` rather than a bare assignment plus `$?`: the non-zero
    # fail-closed statuses must reach the checks below intact even when a
    # caller (worktree.sh, merge-pr.sh) runs under `set -e`.
    local sharer="" shared_rc=0
    sharer="$(loom_target_dir_shared_with "$repo_root" "$worktree_path" "$resolved_real")" || shared_rc=$?
    if [[ "$shared_rc" -eq 2 ]]; then
        _loom_ctd_record "refused" "$resolved" \
            "could not enumerate live worktrees (git worktree list failed)"
        return 0
    fi
    if [[ "$shared_rc" -eq 3 ]]; then
        _loom_ctd_record "refused" "$resolved" \
            "could not resolve a live worktree's target dir (cargo metadata failed)"
        return 0
    fi
    if [[ -n "$sharer" ]]; then
        _loom_ctd_record "shared" "$resolved" "$sharer"
        return 0
    fi

    # 5. A running process is using it. Checked under dry_run too: a preview
    #    that claims it "would remove" a live build's output is a preview an
    #    operator would act on.
    local holders
    holders="$(loom_target_dir_holders "$resolved_real")"
    if [[ -n "$holders" ]]; then
        _loom_ctd_record "protected" "$resolved" "pid(s) $(printf '%s' "$holders" | tr '\n' ' ' | sed 's/ $//')"
        return 0
    fi

    local size
    size="$(loom_dir_size_human "$resolved_real")"
    if [[ "$dry_run" == true ]]; then
        _loom_ctd_record "would-reclaim" "$resolved" "$size"
        return 0
    fi

    local err
    if err="$(rm -rf "$resolved_real" 2>&1)"; then
        _loom_ctd_record "reclaimed" "$resolved" "$size"
    else
        _loom_ctd_record "failed" "$resolved" "${err:-rm failed}"
    fi
    return 0
}

# There is deliberately NO shell renderer for a `status<TAB>path<TAB>detail`
# record here any more (#9153). Both removal surfaces that had one are gone:
# `worktree.sh remove`'s `_rm_report_target_dir` went with the verb in #8471
# (#8195 slice 3), and `merge-pr.sh`'s `_mp_report_target_dir_reclaim` went with
# its reclaim call site, which is now `loom-daemon cargo-target-dir reclaim` —
# emitting the already-rendered line as `LEVEL<TAB>message` from
# `TargetDirOutcome::report_record` (`cargo_target/report.rs`). One record
# grammar, one renderer, and it is the Rust one that `loom-daemon clean` and the
# reaper already printed through. Anything reading the records this file's
# `loom_reclaim_worktree_target_dir` still returns (its own test suite) renders
# them itself.


# --------------------------------------------------------------------------
# Per-worktree target dirs — the REMOVAL-side predicates (issue #8458)
# --------------------------------------------------------------------------
#
# ## The scheme, in one paragraph
#
# Cargo keys a *workspace* crate's artifacts (and its incremental session) by
# the crate's ABSOLUTE SOURCE PATH, so two worktrees never share workspace-crate
# build output even inside one shared target dir. The sharing buys nothing for
# the crates Loom rebuilds, and costs unbounded growth (#8453: 460 GB on one
# host) plus WRONG TEST RESULTS — cargo "uplifts" the final binary to one
# un-hashed `<target>/debug/loom-daemon` that whichever worktree built last
# overwrites, and integration tests execute that path. Giving each worktree
# `<root>/wt/<worktree name>` fixes both.
#
# ## Why only two thin wrappers live here
#
# Every bit of #8458's LOGIC is in the daemon — the opt-in, the derivation, the
# `mkdir`, the marker write (`loom-daemon cargo-target-dir provision|path`), AND
# the two predicates below (`… is-attributable`, `… marker`). That is the
# language policy (`.loom/docs/shell-language-policy.md`): `defaults/scripts/`
# is the shell budget's `contract` (portable) pool, whose growth the ratchet
# refuses with no `Shell-Budget-Growth:` override.
#
# What could NOT move is the *call sites*. `merge-pr.sh` and `worktree.sh
# remove` already resolve a target dir in bash, and marker-first resolution has
# to happen INSIDE that existing resolver rather than beside it — a second
# resolution path is exactly what this library exists to prevent. Delegating the
# implementation keeps the single resolver and deletes the duplicate rules: the
# functions below are now two-line calls into
# `loom-daemon/src/worktree_ops/cargo_target/per_worktree.rs`, which is the one
# place the shape rule and the marker grammar are written.
#
# Degradation is the `requires-daemon: cargo-target-dir optional` contract the
# callers already declare: no resolvable daemon ⇒ both predicates answer "no" ⇒
# no marker is ever read and neither attribution relaxation is ever reached ⇒
# exactly the pre-#8458 behaviour. That is safe by construction, because a host
# with no daemon also never *provisioned* a per-worktree dir to reclaim.
#
# ## Why a marker file and not `.cargo/config.toml`
#
# #7239's attribution rule is that ONLY a redirect derived from the worktree
# itself can prove a directory belongs to it, and it names `build.target-dir` in
# a `.cargo/config.toml` INSIDE the worktree as the way that happens. That
# vehicle is unavailable in a repo that TRACKS `.cargo/config.toml` (Loom itself
# does — it carries the CI-mirroring clippy `rustflags`): the redirect would
# leave every worktree with a modified tracked file, one careless `git add -A`
# from being committed. The marker is inside the worktree, so it is
# worktree-derived evidence in exactly the sense #7239 requires, and it is not a
# Cargo config, so it touches no tracked state. Cargo learns the redirect from
# `CARGO_TARGET_DIR`, exported by the spawn path.
#
# ## The shape IS the attribution
#
# `<root>/wt/<worktree name>` is checked structurally against the basename of
# the worktree being asked about. That predicate is what licenses the two
# relaxations documented at `loom_target_dir_shared_with` and gate 2f, and it is
# deliberately un-widenable: a machine-global root (`/big/cargo-target`,
# `$CARGO_HOME`-configured or env-exported) cannot satisfy it, and a corrupted or
# hand-written marker can only ever name `<something>/wt/<this worktree's own
# name>` — never a parent, never a sibling's directory, never the shared root.

# The marker file itself is `.loom-cargo-target-dir` inside the worktree —
# gitignored via the loom-managed block (loom-daemon/src/init/post_init.rs) and
# filtered out of the dirty-worktree guards in worktree.sh / merge-pr.sh, like
# every other marker in its family. Its name is spelled out at the two places
# that need it rather than held in a shell variable: the grammar lives in
# `per_worktree::MARKER_FILE`, and a second shell-side "source of truth" for a
# constant is how the two drift.

# _loom_ctd_daemon <verb> [arg...] — run `loom-daemon cargo-target-dir <verb>`,
# or exit 1 when no daemon binary resolves (the `optional` half of the callers'
# `requires-daemon: cargo-target-dir optional` declaration: both predicates then
# answer "no", which is the pre-#8458 behaviour).
#
# `loom_locate_daemon_bin` comes from the sibling `lib/locate-daemon-bin.sh`,
# which every consumer of this library already sources. It is called through a
# `|| true` command substitution with stderr discarded, so a partial checkout
# where it is NOT defined degrades to "no daemon" rather than erroring — the
# same failure direction as a host with no binary. Quiet, because this resolves
# once per predicate call inside loops and the #4997 resolution trace would
# otherwise dominate a removal's stderr.
#
# The result is memoised per shell. Calls made from inside a command
# substitution (`loom_read_worktree_target_dir_marker` is one) cannot write the
# memo back to the parent, so they re-resolve; that is a PATH/`-x` probe, not a
# build, and it is not in the per-sibling hot path.
_loom_ctd_daemon() {
    [[ -n "${_LOOM_CTD_DAEMON_BIN+x}" ]] || _LOOM_CTD_DAEMON_BIN="$(LOOM_LOCATE_DAEMON_BIN_QUIET=1 loom_locate_daemon_bin "$PWD" 2>/dev/null || true)"
    [[ -n "$_LOOM_CTD_DAEMON_BIN" ]] && "$_LOOM_CTD_DAEMON_BIN" cargo-target-dir "$@" 2>/dev/null
}

# loom_is_per_worktree_target_dir <worktree_path> <candidate> [<candidate>...]
#   Exit 0 when ANY <candidate> has the Loom per-worktree shape FOR
#   <worktree_path>. Purely structural — no disk access — so it is still
#   answerable after the worktree has been removed, which is what gate 2f needs.
#   Several candidates are accepted because every caller asks about a path and
#   its `realpath` together; one subprocess answers both. The rule itself is
#   `per_worktree::is_attributable`, not a copy of it.
#
# loom_read_worktree_target_dir_marker <worktree_path>
#   Print the target dir recorded for this worktree, or exit 1 when there is no
#   usable marker. MUST be called while the worktree is still on disk. Every
#   failure mode — absent, empty, a value without the per-worktree shape, a tree
#   with no Cargo manifest — exits 1, so a corrupt marker degrades to "no
#   per-worktree redirect" (pre-#8458 behavior) rather than to a path this
#   library would then act on. That validation chain is
#   `per_worktree::marker_value`, not a copy of it.
#
# Both are one line because there is nothing left for them to do: the whole
# predicate is the Rust it names. They keep their own names because
# `.loom/hooks/post-worktree.sh` overrides exactly these two on a partial
# checkout that has no lib at all.
loom_is_per_worktree_target_dir() { _loom_ctd_daemon is-attributable "$@"; }
loom_read_worktree_target_dir_marker() { _loom_ctd_daemon marker "$1"; }
