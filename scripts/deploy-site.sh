#!/usr/bin/env bash
#
# deploy-site.sh — manual, one-command deploy of the kicad-tools.org demo
# gallery (Epic #3674, Phase 3) to Cloudflare Pages.
#
# This replaces the former CI auto-deploy workflow
# (.github/workflows/gallery-deploy.yml).  The team deploys MANUALLY via a
# locally authenticated `wrangler` (OAuth `wrangler login`) instead of GitHub
# Actions, so no CLOUDFLARE_API_TOKEN / CLOUDFLARE_ACCOUNT_ID secrets are
# needed (see operator issue #3686).
#
# Pipeline (mirrors the old workflow, kept linear so data/render/build are
# independent of the deploy step):
#   1. board.json  -- `kct board-metrics --all`           (non-fatal)
#   2. renders     -- `kct render boards/` per-board PNGs  (3D via xvfb-run)
#   3. site build  -- `npm --prefix site ci && run build`  (prebuild stages
#                     renders + manufacturing files into site/public/)
#   4. deploy      -- `wrangler pages deploy site/dist`
#
# Generated artifacts (board.json, renders, site/public/boards/, site/dist/)
# are all git-ignored and never committed.
#
# Custom domain (kicad-tools.org -> kicad-tools.pages.dev) is operator issue
# #3686 and is OUT OF SCOPE here: this script targets the default Pages
# hostname kicad-tools.pages.dev.
#
# Usage:
#   ./scripts/deploy-site.sh [--no-deploy] [--preview] [--no-3d] [--help]
#
#   --no-deploy   Run metrics + render + build only; skip the wrangler deploy.
#   --preview     Deploy to a non-main preview branch (omit `--branch main`;
#                 wrangler auto-names the preview by the current branch).
#   --no-3d       Skip 3D renders (skip xvfb-run; useful on machines without X).
#   --help        Show this help and exit.
#
# Prerequisites (the script warns clearly if any are missing):
#   uv        -- required (board metrics + renders)
#   node/npm  -- required (Astro build)
#   wrangler  -- required unless --no-deploy (run `wrangler login` once)
#   kicad-cli -- optional; if absent, renders are skipped and the site serves
#                placeholder thumbnails (warn-then-continue, not an abort)

set -euo pipefail

# --- Locate the repo root so the script works from any cwd -----------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# --- Logging helpers -------------------------------------------------------
info()  { printf '\033[0;34m[deploy]\033[0m %s\n' "$*"; }
warn()  { printf '\033[0;33m[deploy] WARNING:\033[0m %s\n' "$*" >&2; }
err()   { printf '\033[0;31m[deploy] ERROR:\033[0m %s\n' "$*" >&2; }

usage() {
  sed -n '2,/^set -euo pipefail/p' "${BASH_SOURCE[0]}" \
    | sed -e 's/^# \{0,1\}//' -e '/^set -euo pipefail/d'
}

# --- Parse flags -----------------------------------------------------------
NO_DEPLOY=0
PREVIEW=0
NO_3D=0

while [ $# -gt 0 ]; do
  case "$1" in
    --no-deploy) NO_DEPLOY=1 ;;
    --preview)   PREVIEW=1 ;;
    --no-3d)     NO_3D=1 ;;
    -h|--help)   usage; exit 0 ;;
    *)
      err "Unknown argument: $1"
      echo "Run '$0 --help' for usage." >&2
      exit 2
      ;;
  esac
  shift
done

cd "${REPO_ROOT}"

# --- Prerequisite checks ---------------------------------------------------
MISSING=0

if ! command -v uv >/dev/null 2>&1; then
  err "'uv' not found — required for board metrics + renders. Install: https://astral.sh/uv"
  MISSING=1
fi

if ! command -v npm >/dev/null 2>&1; then
  err "'npm' (Node.js) not found — required to build the Astro site. Install Node 22+."
  MISSING=1
fi

if [ "${NO_DEPLOY}" -eq 0 ]; then
  if ! command -v wrangler >/dev/null 2>&1 && ! command -v npx >/dev/null 2>&1; then
    err "'wrangler' (and 'npx') not found — required to deploy. Run 'wrangler login' once, or pass --no-deploy."
    MISSING=1
  fi
fi

if [ "${MISSING}" -ne 0 ]; then
  err "Missing required prerequisites (see above). Aborting."
  exit 1
fi

# kicad-cli is OPTIONAL: if absent, skip renders and let the site fall back to
# placeholder-board.svg.  board-metrics does not need kicad-cli.
SKIP_RENDERS=0
if ! command -v kicad-cli >/dev/null 2>&1; then
  warn "'kicad-cli' not found — skipping board renders. The site will serve placeholder thumbnails."
  SKIP_RENDERS=1
fi

# --- Step 1: board.json metrics (non-fatal) --------------------------------
info "Step 1/4: generating board metrics (uv run kct board-metrics --all)"
# A metrics regression must not block the deploy; the site tolerates a board
# without board.json.
uv run kct board-metrics --all || warn "board-metrics returned non-zero; continuing (site tolerates missing board.json)."

# --- Step 2: renders -------------------------------------------------------
if [ "${SKIP_RENDERS}" -eq 1 ]; then
  info "Step 2/4: skipping renders (kicad-cli absent)."
else
  info "Step 2/4: rendering board images (uv run kct render boards/)"
  rendered=0

  # Prefer 3D (2D + 3D) unless --no-3d.  The 3D pass (kicad-cli pcb render)
  # needs a display, so wrap it in xvfb-run when available to give it a virtual
  # framebuffer on headless machines.
  if [ "${NO_3D}" -eq 0 ]; then
    if command -v xvfb-run >/dev/null 2>&1; then
      if xvfb-run --auto-servernum --server-args="-screen 0 1280x1024x24" \
           uv run kct render boards/; then
        info "Renders complete (2D + 3D, under xvfb)."
        rendered=1
      else
        warn "3D render failed under xvfb; falling back to 2D-only."
      fi
    else
      # No xvfb: try 3D directly (works if a real display is present); on
      # failure we fall through to the 2D-only path below.
      if uv run kct render boards/; then
        info "Renders complete (2D + 3D)."
        rendered=1
      else
        warn "3D render failed (no xvfb / no display); falling back to 2D-only."
      fi
    fi
  fi

  # 2D-only path: requested via --no-3d, or used as the fallback when 3D failed.
  if [ "${rendered}" -eq 0 ]; then
    info "Rendering 2D-only (uv run kct render boards/ --no-3d)"
    if uv run kct render boards/ --no-3d; then
      info "2D renders complete."
    else
      warn "Render step failed; continuing with existing renders (placeholders will be served)."
    fi
  fi
fi

# --- Step 3: build the Astro site ------------------------------------------
# `prebuild` (site/scripts/copy-renders.mjs) runs automatically before `build`
# and stages renders + manufacturing files from boards/<id>/output/ into
# site/public/.  No manual copy step needed.
info "Step 3/4: building the Astro site (npm --prefix site ci && run build)"
npm --prefix site ci
npm --prefix site run build
info "Site built to site/dist/."

# --- Step 4: deploy --------------------------------------------------------
if [ "${NO_DEPLOY}" -eq 1 ]; then
  info "Step 4/4: --no-deploy set; skipping Cloudflare Pages deploy."
  info "Done. Build is in site/dist/ (not deployed)."
  exit 0
fi

# Prefer a directly installed wrangler; fall back to npx.
if command -v wrangler >/dev/null 2>&1; then
  WRANGLER=(wrangler)
else
  WRANGLER=(npx wrangler)
fi

if [ "${PREVIEW}" -eq 1 ]; then
  info "Step 4/4: deploying PREVIEW to Cloudflare Pages (wrangler auto-names the branch)."
  # Omit --branch so wrangler auto-names the preview by the current branch.
  "${WRANGLER[@]}" pages deploy site/dist --project-name kicad-tools
else
  info "Step 4/4: deploying PRODUCTION (branch main) to Cloudflare Pages."
  "${WRANGLER[@]}" pages deploy site/dist --project-name kicad-tools --branch main
fi

info "Deploy complete. The deployed URL is printed above by wrangler."
