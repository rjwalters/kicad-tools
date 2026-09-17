#!/usr/bin/env bash
# daemon-update-fixtures.sh — the fake-binary / fake-forge fixtures shared by
# test-loom-daemon-update.sh and its resolve-json sibling (#7977).
#
# Source this file (do not exec). Extracted so the two suites build identical
# fixtures from ONE definition: a fake `gh` that drifts from the real one in
# only one of them is a test that proves nothing, and that is exactly the class
# of bug #7977 hit (the stub emitted post-`--jq` scalars, indistinguishable
# from real `gh` only because every caller passed `--jq`).
#
# Required globals, set by the sourcing suite BEFORE sourcing:
#   LOOM_REPO_ROOT      repo root, for locating the real sources these fake
#   CLI_DIR             defaults/scripts/cli, for START_SCRIPT-adjacent copies
#   START_SCRIPT        the real loom-daemon-start.sh a fixture may copy
#   NEW_FAKE_BIN_SRC    the fake-daemon source a fixture provisions from
# `CARGO_TARGET_DIR` is read when set and is optional.

# sha256_of <path> — portable checksum in `.sha256`-file format
# (`<hex>  <basename>`), matching the release workflow's own
# `shasum -a 256`/`sha256sum` output.
sha256_of() {
    local path="$1" base
    base="$(basename "$path")"
    if command -v shasum >/dev/null 2>&1; then
        (cd "$(dirname "$path")" && shasum -a 256 "$base")
    else
        (cd "$(dirname "$path")" && sha256sum "$base")
    fi
}

# ---------- fixture builder ----------
# Sets up a fresh throwaway repo root at $1 with a real git HEAD, a stub
# loom-daemon crate, real start/stop scripts, a real copy of
# provision-daemon.sh (so the #4016 signing step is exercised, not silently
# skipped as "not found/sourceable"), and a minimal, machine-agnostic PATH
# (excludes ~/.local/bin and similar, so a real loom-daemon possibly
# installed on the dev machine can never leak into a test).
new_fixture() {
    local root="$1"
    mkdir -p "$root/.loom/logs" "$root/.loom/scripts/cli" "$root/.loom/scripts/lib" "$root/loom-daemon" "$root/scripts/install"
    cp "$CLI_DIR/loom-daemon-start.sh" "$root/.loom/scripts/cli/loom-daemon-start.sh"
    cp "$CLI_DIR/loom-daemon-stop.sh" "$root/.loom/scripts/cli/loom-daemon-stop.sh"
    chmod +x "$root/.loom/scripts/cli/"*.sh
    # The fixture start/stop scripts source ../lib/launchd-domain.sh for the
    # shared gui/<uid> ↦ user/<uid> resolver (#4130), so it must exist alongside
    # them in the throwaway tree — else a launchd-mode restart path would find no
    # resolve_launchd_domain. Mirrors the real defaults/scripts/lib layout.
    cp "$CLI_DIR/../lib/launchd-domain.sh" "$root/.loom/scripts/lib/launchd-domain.sh"
    # Same for lib/systemd-user.sh (#4268): the fixture start script's systemd
    # --user path (invoked via perform_systemd_relaunch's call to $START_SCRIPT,
    # #4260 sub-issue C) sources it relative to ITS OWN location, so it must exist
    # alongside the fixture copy too, not just in the real repo tree.
    cp "$CLI_DIR/../lib/systemd-user.sh" "$root/.loom/scripts/lib/systemd-user.sh"
    # Same for lib/bounded-run.sh (#4799): the fixture start script's
    # print_calibrate_hint() sources it relative to ITS OWN location to bound
    # its `calibrate` command substitution, so it must exist alongside the
    # fixture copy too.
    cp "$CLI_DIR/../lib/bounded-run.sh" "$root/.loom/scripts/lib/bounded-run.sh"
    # Same for lib/locate-daemon-bin.sh (#4875): the fixture start script
    # sources it relative to ITS OWN location to resolve the daemon binary
    # under a minimal PATH, so every fixture flow that execs the copied
    # loom-daemon-start.sh (restart, --relaunch, the full update run) needs it
    # in the throwaway tree. Without it those flows abort with
    # "locate-daemon-bin.sh not found at <fixture>/.loom/scripts/lib" before
    # reaching the behaviour under test.
    cp "$CLI_DIR/../lib/locate-daemon-bin.sh" "$root/.loom/scripts/lib/locate-daemon-bin.sh"
    cp "$LOOM_REPO_ROOT/scripts/install/provision-daemon.sh" "$root/scripts/install/provision-daemon.sh"
    cat > "$root/loom-daemon/Cargo.toml" <<'EOF'
[package]
name = "loom-daemon"
version = "0.0.0"
EOF
    ( cd "$root" && git init -q && git -c user.email=test@test -c user.name=test commit -q --allow-empty -m init )
}

# Writes a fake "release artifact" binary at $1 reporting version $2 / commit
# $3 on --version, otherwise behaving like write_fake_daemon (rejects unknown
# subcommands, loops forever on a normal run) — standing in for a downloaded
# `loom-daemon-<target>` asset. A parameterized-version sibling of
# write_fake_daemon (which hardcodes 0.15.0), needed so a fetched artifact can
# report a version NEWER than the installed daemon's.
write_fake_artifact_daemon() {
    local path="$1" version="$2" commit="$3"
    cat > "$path" <<EOF
#!/usr/bin/env bash
if [[ "\${1:-}" == "--version" ]]; then
    echo "loom-daemon ${version} (commit ${commit}, built 2026-08-03T00:00:00Z)"
    exit 0
fi
if [[ "\${1:-}" == "calibrate" ]]; then
    exit 1
fi
if [[ -n "\${1:-}" && "\${1:-}" != -* ]]; then
    echo "fake loom-daemon: unsupported subcommand: \$*" >&2
    exit 1
fi
while true; do sleep 1; done
EOF
    chmod +x "$path"
}

# Writes a fake `cargo` that, on `cargo build --release [--message-format=...]`
# (cwd = loom-daemon/), copies $NEW_FAKE_BIN_SRC into
# ${CARGO_TARGET_DIR:-target}/release/loom-daemon instead of compiling --
# honoring CARGO_TARGET_DIR (#6160) exactly like real cargo does, so tests can
# redirect the build output the same way a redirected host does. Tests export
# NEW_FAKE_BIN_SRC before invoking loom-daemon-update.sh. When a
# --message-format=json* flag is present (the invocation loom-daemon-update.sh
# actually uses since #6160), also emits the compiler-artifact/build-finished
# JSON messages loom-daemon-update.sh parses to locate the built executable --
# shaped like real `cargo build --message-format=json-render-diagnostics`
# output (a null-executable library artifact first, matching the real
# multi-target stream, then the bin target's own artifact with the real,
# possibly-redirected, absolute executable path). Also answers `cargo metadata
# --format-version 1 --no-deps` with a minimal object reporting the same
# (redirect-aware) target_directory, for the fallback path's own test coverage.
write_fake_cargo() {
    local path="$1"
    cat > "$path" <<'EOF'
#!/usr/bin/env bash
if [[ "${1:-}" == "build" ]]; then
    target_dir="${CARGO_TARGET_DIR:-target}"
    mkdir -p "$target_dir/release"
    cp "$NEW_FAKE_BIN_SRC" "$target_dir/release/loom-daemon"
    chmod +x "$target_dir/release/loom-daemon"
    abs_target_dir="$(cd "$target_dir" && pwd)"
    for arg in "$@"; do
        case "$arg" in
            --message-format=json*)
                printf '{"reason":"compiler-artifact","target":{"kind":["lib"],"name":"loom_daemon"},"executable":null}\n'
                printf '{"reason":"compiler-artifact","target":{"kind":["bin"],"name":"loom-daemon"},"executable":"%s/release/loom-daemon"}\n' "$abs_target_dir"
                printf '{"reason":"build-finished","success":true}\n'
                break
                ;;
        esac
    done
    echo "[fake cargo] build ok" >&2
    exit 0
fi
if [[ "${1:-}" == "metadata" ]]; then
    target_dir="${CARGO_TARGET_DIR:-target}"
    mkdir -p "$target_dir"
    abs_target_dir="$(cd "$target_dir" && pwd)"
    printf '{"target_directory":"%s"}\n' "$abs_target_dir"
    exit 0
fi
echo "[fake cargo] unsupported subcommand: $*" >&2
exit 1
EOF
    chmod +x "$path"
}

# Writes a fake `gh` at $1 for the artifact-fetch tests (Epic #4990 Phase 3,
# #5020). Understands exactly the invocations loom-daemon-update.sh's
# fetch_resolve_latest() / fetch_and_verify_artifact() make:
#   gh release view --json tagName  -R <slug> --jq '.tagName'         -> $2
#   gh release view --json assets   -R <slug> --jq '.assets[].name'   -> `ls $3`
#   gh release download <tag> -R <slug> -p <name> [-p <name> ...] -D <dir> --clobber
#       -> copies each matching file from $3 into <dir>; exits 1 if NONE of
#          the -p patterns matched anything under $3 (mirrors real gh's
#          "no assets match" failure for a required download).
write_fake_gh() {
    local path="$1" tag="$2" assets_dir="$3"
    cat > "$path" <<FAKEGH
#!/usr/bin/env bash
ASSETS_DIR="$assets_dir"
TAG_VAL="$tag"
FAKEGH
    cat >> "$path" <<'FAKEGH'
# Real `gh --json <fields>` emits an OBJECT, and `--jq` then filters it. This
# stub used to skip to the post-`--jq` scalar, indistinguishable only because
# every caller passed `--jq`. #7810 PR 5 has one that does not (the #7922
# pattern: a bare scalar cannot distinguish "absent" from "empty"), so the stub
# now does what gh does -- emit the object, filter only when asked.
if [[ "${1:-}" == "release" && "${2:-}" == "view" ]]; then
    shift 2
    fields=""; jqx=""
    while [[ $# -gt 0 ]]; do
        case "$1" in --json) fields="$2"; shift 2 ;; --jq|-q) jqx="$2"; shift 2 ;; *) shift ;; esac
    done
    case "$fields" in
        tagName) obj="$(jq -n --arg t "$TAG_VAL" '{tagName:$t}')" ;;
        assets)  obj="$(ls "$ASSETS_DIR" 2>/dev/null | jq -R -s -c 'split("\n")|map(select(length>0)|{name:.})|{assets:.}')" ;;
        # --resolve-json (#7609) asks for the release's publish timestamp so
        # the daemon can surface `artifact_available.published_at`.
        publishedAt) obj="$(jq -n '{publishedAt:"2026-09-13T12:00:00Z"}')" ;;
        *) exit 1 ;;
    esac
    [[ -n "$jqx" ]] && { printf '%s' "$obj" | jq -r "$jqx"; exit 0; }; printf '%s\n' "$obj"; exit 0
fi
if [[ "${1:-}" == "release" && "${2:-}" == "download" ]]; then
    shift 2
    shift # drop the <tag> positional arg
    dest="."
    patterns=()
    while [[ $# -gt 0 ]]; do
        case "$1" in
            -p) patterns+=("$2"); shift 2 ;;
            -D) dest="$2"; shift 2 ;;
            -R) shift 2 ;;
            --clobber) shift ;;
            *) shift ;;
        esac
    done
    mkdir -p "$dest"
    copied=0
    for pat in "${patterns[@]}"; do
        for f in "$ASSETS_DIR"/$pat; do
            [[ -e "$f" ]] || continue
            cp "$f" "$dest/"
            copied=1
        done
    done
    [[ "$copied" -eq 1 ]] && exit 0 || exit 1
fi
echo "fake gh: unsupported invocation: $*" >&2
exit 1
FAKEGH
    chmod +x "$path"
}

# Writes a fake `gh` at $1 whose every `release view` fails — standing in for
# the "GitHub API unreachable / rate-limited / unauthenticated" case that must
# SOFTLY fall back to the local source build (AC4), never hard-fail.
write_fake_gh_unreachable() {
    local path="$1"
    cat > "$path" <<'FAKEGH'
#!/usr/bin/env bash
echo "gh: failed to fetch release: dial tcp: lookup api.github.com: no such host" >&2
exit 1
FAKEGH
    chmod +x "$path"
}
