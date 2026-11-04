#!/usr/bin/env node
// squad-mcp.mjs — project MCP launcher for the Claude runtime.
//
// Installed verbatim by install.sh into the target repo's `.claude/hooks/`, and
// named by `.mcp.json`'s `mcpServers.squad.args`. It exists because of a
// three-way constraint:
//
//   * The MCP runtime lives in the squad source checkout, not in this repo
//     (install.sh never copies `dist/`).
//   * `.mcp.json` is a tracked file, so it must not carry a machine-specific
//     absolute path into a public repository.
//   * A path relative to the project working directory resolves beside a linked
//     git worktree, where no sibling squad checkout exists — so pointing
//     `.mcp.json` straight at the runtime meant the server could not be spawned
//     from a worktree at all, and a fleet running one agent per worktree got no
//     squad in any of them (#95).
//
// `.mcp.json` therefore names this launcher by a repository-relative path,
// which every worktree checkout of the repo also has, and passes the runtime's
// location in `SQUAD_RUNTIME` (relative to the repository root when the squad
// checkout is a sibling, absolute for any other layout — the same value the
// launcher used to hold in `args`). This file is byte-identical in every
// consumer repo on every machine, so it is safe to commit: everything
// machine-local stays in `.mcp.json`, where the installer's ownership receipts
// already manage it.
//
// Resolution is anchored to the primary clone via `git rev-parse
// --git-common-dir` — the same walk src/db.ts uses to keep every worktree of a
// repo in one room — rather than to the working directory.
//
// stdout belongs to the MCP stdio transport: diagnostics go to stderr only.
import { existsSync, readFileSync, statSync } from "node:fs";
import { execFileSync } from "node:child_process";
import { basename, dirname, isAbsolute, join, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

/** The checkout this launcher was installed into: <root>/.claude/hooks/<file>. */
const installedRoot = resolve(dirname(fileURLToPath(import.meta.url)), "../..");

/**
 * The primary clone's working tree for the linked worktree at `dir`, or null
 * when `dir` is not a linked worktree (or git cannot answer). Mirrors db.ts's
 * mainWorktreeRoot: a linked worktree's `.git` is a pointer file, and
 * `--git-common-dir` is shared by every worktree of a repo, so its parent is
 * the primary clone's root. A submodule's common dir
 * (`<super>/.git/modules/<name>`) and a bare repo's have no working tree at
 * that parent, so require the conventional `<root>/.git` shape first.
 */
function mainWorktreeRoot(dir) {
  try {
    if (!statSync(join(dir, ".git")).isFile()) return null;
  } catch {
    return null; // no .git at all (an unversioned target repo)
  }
  let commonDir;
  try {
    commonDir = execFileSync(
      "git",
      ["-C", dir, "rev-parse", "--path-format=absolute", "--git-common-dir"],
      { encoding: "utf8", stdio: ["ignore", "pipe", "ignore"] },
    ).trim();
  } catch {
    return null; // no git on PATH, or not a repo — fall back to this checkout
  }
  if (!commonDir || basename(commonDir) !== ".git") return null;
  const root = dirname(commonDir);
  return existsSync(root) ? root : null;
}

const primaryRoot = mainWorktreeRoot(installedRoot) ?? installedRoot;
// Primary clone first: a relative runtime path was recorded against the primary
// clone at install time, so that is where a worktree has to look. The
// worktree's own root stays a fallback, for a checkout that really does have a
// sibling squad source of its own.
const roots =
  primaryRoot === installedRoot ? [installedRoot] : [primaryRoot, installedRoot];

/**
 * `mcpServers.squad.env` from the first readable `.mcp.json` among the roots,
 * or {}. Some harnesses start the launcher bare, without the env block Claude
 * Code would pass; the value lives in `.mcp.json`, so read it from there.
 */
function mcpJsonEnv() {
  for (const root of [installedRoot, primaryRoot]) {
    try {
      const env = JSON.parse(readFileSync(join(root, ".mcp.json"), "utf8"))
        ?.mcpServers?.squad?.env;
      if (env && typeof env === "object") return env;
    } catch {
      // missing or malformed — try the next root
    }
  }
  return {};
}

// A real environment value always wins; .mcp.json only fills what is unset.
if (!process.env.SQUAD_RUNTIME || !process.env.SQUAD_DIR) {
  const fallback = mcpJsonEnv();
  for (const key of ["SQUAD_RUNTIME", "SQUAD_DIR"]) {
    if (!process.env[key] && typeof fallback[key] === "string" && fallback[key])
      process.env[key] = fallback[key];
  }
}

const spec = process.env.SQUAD_RUNTIME;
if (!spec) {
  console.error(
    "squad: SQUAD_RUNTIME is not set; this launcher needs the squad runtime path from .mcp.json",
  );
  console.error(
    "squad: rerun install.sh from the squad source checkout in this repository",
  );
  process.exit(1);
}
const candidates = isAbsolute(spec)
  ? [spec]
  : roots.map((root) => resolve(root, spec));
const runtime = candidates.find((path) => existsSync(path));
if (!runtime) {
  console.error(
    `squad: MCP runtime not found; looked for ${candidates.join(" and ")}`,
  );
  console.error(
    "squad: build the squad source checkout (CI=true pnpm install --frozen-lockfile && pnpm build), then rerun install.sh in this repository",
  );
  process.exit(1);
}

// SQUAD_DIR is installed as the repository-relative ".squad", which would
// otherwise resolve against the working directory — from a worktree that is a
// private, empty room instead of the shared one. Anchor it to the checkout that
// actually holds the room: the worktree's own .squad when it has one (the
// documented opt-in for a separate room), the primary clone's otherwise.
const room = process.env.SQUAD_DIR;
if (room && !isAbsolute(room))
  process.env.SQUAD_DIR =
    [installedRoot, primaryRoot]
      .map((root) => resolve(root, room))
      .find((path) => existsSync(path)) ?? resolve(primaryRoot, room);

await import(pathToFileURL(runtime).href);
