/**
 * Build-time project facts for the homepage quick start and capability cards.
 *
 * Everything drift-prone (Python requirement, install extras, agent skills,
 * MCP tool names) is read from the repo's own source files so the page cannot
 * silently fall behind them. Every parser throws on a missing or reformatted
 * source rather than returning an empty list.
 */
import { readFileSync, readdirSync } from "node:fs";
import { resolve } from "node:path";

/** Skill files under `agent_skills/kct/` that are not themselves skills. */
const NON_SKILL_FILES = new Set(["README.md", "help.md"]);

/** Extras surfaced on the page: the user-facing installs, in this order. */
const FEATURED_EXTRAS = ["mcp", "parts", "placement", "native", "models", "all"] as const;

export interface ProjectFacts {
  requiresPython: string;
  extras: string[];
  skills: string[];
  mcpTools: string[];
  partsTools: string[];
}

function projectTable(toml: string): string {
  const table = toml.split(/^\[project\][ \t]*$/m)[1]?.split(/^\[/m)[0];
  if (!table) throw new Error("pyproject.toml must declare a [project] table");
  return table;
}

export function parseRequiresPython(toml: string): string {
  const spec = projectTable(toml).match(/^requires-python\s*=\s*["']([^"']+)["']\s*$/m)?.[1];
  if (!spec) throw new Error("pyproject.toml must declare project.requires-python");
  return spec;
}

/** Names of `[project.optional-dependencies]` extras, in file order. */
export function parseExtras(toml: string): string[] {
  const body = toml
    .split(/^\[project\.optional-dependencies\][ \t]*$/m)[1]
    ?.split(/^\[/m)[0];
  if (!body) throw new Error("pyproject.toml must declare [project.optional-dependencies]");
  const names = [...body.matchAll(/^([A-Za-z0-9_-]+)\s*=\s*\[/gm)].map((m) => m[1]);
  if (names.length === 0) throw new Error("no optional-dependency extras found");
  return names;
}

export function featuredExtras(all: string[]): string[] {
  const missing = FEATURED_EXTRAS.filter((e) => !all.includes(e));
  if (missing.length > 0) {
    throw new Error(`pyproject.toml no longer declares extras: ${missing.join(", ")}`);
  }
  return [...FEATURED_EXTRAS];
}

/** Skill names from the `name:` frontmatter of each skill file. */
export function parseSkills(files: Record<string, string>): string[] {
  const skills: string[] = [];
  for (const [file, text] of Object.entries(files)) {
    if (NON_SKILL_FILES.has(file) || !file.endsWith(".md")) continue;
    const name = text.match(/^---\n(?:[^\n]*\n)*?name:\s*([A-Za-z0-9_-]+)\s*\n/)?.[1];
    if (!name) throw new Error(`skill file ${file} has no name: frontmatter`);
    skills.push(name);
  }
  if (skills.length === 0) throw new Error("no agent skills found");
  return skills.sort();
}

/** MCP tool names registered in `mcp/tools/registry.py` via `name="..."`. */
export function parseMcpTools(py: string): string[] {
  const names = [...py.matchAll(/^\s+name="([a-z][a-z0-9_]*)",\s*$/gm)].map((m) => m[1]);
  if (names.length === 0) throw new Error("no MCP tools found in registry.py");
  return [...new Set(names)];
}

export function loadProjectFacts(root = resolve(process.cwd(), "..")): ProjectFacts {
  const toml = readFileSync(resolve(root, "pyproject.toml"), "utf8");
  const skillDir = resolve(root, "src/kicad_tools/agent_skills/kct");
  const skillFiles = Object.fromEntries(
    readdirSync(skillDir)
      .filter((f) => f.endsWith(".md"))
      .map((f) => [f, readFileSync(resolve(skillDir, f), "utf8")]),
  );
  const mcpTools = parseMcpTools(
    readFileSync(resolve(root, "src/kicad_tools/mcp/tools/registry.py"), "utf8"),
  );
  const partsTools = mcpTools.filter((t) => t.startsWith("parts_"));
  if (partsTools.length === 0) throw new Error("registry.py exposes no parts_* MCP tools");
  return {
    requiresPython: parseRequiresPython(toml),
    extras: featuredExtras(parseExtras(toml)),
    skills: parseSkills(skillFiles),
    mcpTools,
    partsTools,
  };
}

export const projectFacts = loadProjectFacts();
