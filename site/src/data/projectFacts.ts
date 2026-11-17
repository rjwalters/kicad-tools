/**
 * Project facts the homepage states, read at build time from the files that
 * define them so the copy cannot drift from the package (issue #8101).
 *
 * Every parser throws on input it does not recognise: a build that cannot
 * establish a fact fails visibly instead of publishing a stale or empty one.
 */
import { readdirSync, readFileSync } from "node:fs";
import { resolve } from "node:path";
import { PROJECT_FACT_SOURCES } from "./projectFactSources.ts";

const repoRoot = resolve(process.cwd(), "..");
const read = (rel: string): string => readFileSync(resolve(repoRoot, rel), "utf8");

function projectTable(toml: string): string {
  const table = toml.split(/^\[project\][ \t]*$/m)[1]?.split(/^\[/m)[0];
  if (!table) throw new Error("pyproject.toml has no [project] table");
  return table;
}

/** `requires-python = ">=3.10"` -> `"3.10"`. */
export function parsePythonMinimum(toml: string): string {
  const spec = projectTable(toml).match(/^requires-python\s*=\s*["']>=\s*(\d+\.\d+)["']\s*$/m)?.[1];
  if (!spec) throw new Error("pyproject.toml must declare requires-python as \">=X.Y\"");
  return spec;
}

/** Keys of `[project.optional-dependencies]`, in file order. */
export function parseExtras(toml: string): string[] {
  const table = toml.split(/^\[project\.optional-dependencies\][ \t]*$/m)[1]?.split(/^\[/m)[0];
  const extras = table ? [...table.matchAll(/^([A-Za-z0-9_-]+)\s*=/gm)].map((m) => m[1]) : [];
  if (extras.length === 0) throw new Error("pyproject.toml declares no optional-dependencies");
  return extras;
}

/** `HARNESSES: tuple[str, ...] = ("claude-code", "codex", "opencode")`. */
export function parseHarnesses(python: string): string[] {
  const tuple = python.match(/^HARNESSES\s*:[^=\n]*=\s*\(([^)]*)\)/m)?.[1];
  const names = tuple ? [...tuple.matchAll(/["']([a-z0-9-]+)["']/g)].map((m) => m[1]) : [];
  if (names.length === 0) throw new Error("agent_surfaces.py must declare a HARNESSES tuple");
  return names;
}

/** Tool names passed to module-level `register_tool(name=...)` calls. */
export function parseMcpTools(python: string): string[] {
  const names = [...python.matchAll(/^register_tool\(\s*name="([a-z0-9_]+)"/gm)].map((m) => m[1]);
  if (names.length === 0) throw new Error("MCP registry declares no tools");
  if (new Set(names).size !== names.length) throw new Error("MCP registry declares a tool twice");
  return names;
}

export interface Skill {
  name: string;
  invocation: string;
  summary: string;
}

/** Front matter of one packaged skill; `summary` is the description's first sentence. */
export function parseSkill(markdown: string): Skill {
  const front = markdown.match(/^---\n([\s\S]*?)\n---/)?.[1];
  const field = (key: string) => front?.match(new RegExp(`^${key}:\\s*(.+)$`, "m"))?.[1].trim();
  const name = field("name");
  const invocation = field("invocation");
  const description = field("description");
  if (!name || !invocation || !description) {
    throw new Error("skill front matter must declare name, invocation and description");
  }
  const summary = (description.match(/^.*?[.!?](?=\s|$)/)?.[0] ?? description).replace(/\s+—\s*$/, "");
  return { name, invocation, summary };
}

const [PYPROJECT, SURFACES, MCP_REGISTRY, SKILLS_DIR] = PROJECT_FACT_SOURCES;

function loadSkills(): Skill[] {
  const files = readdirSync(resolve(repoRoot, SKILLS_DIR))
    .filter((f) => f.endsWith(".md") && f !== "README.md")
    .sort();
  if (files.length === 0) throw new Error(`no packaged skills under ${SKILLS_DIR}`);
  return files.map((f) => parseSkill(read(`${SKILLS_DIR}/${f}`)));
}

const pyproject = read(PYPROJECT);

export const pythonMinimum = parsePythonMinimum(pyproject);
export const extras = parseExtras(pyproject);
export const harnesses = parseHarnesses(read(SURFACES));
export const mcpTools = parseMcpTools(read(MCP_REGISTRY));
export const skills = loadSkills();

/** Extras the quick start names; the build fails if pyproject drops one. */
export const QUICK_START_EXTRAS = ["mcp", "native", "all"] as const;
for (const extra of QUICK_START_EXTRAS) {
  if (!extras.includes(extra)) throw new Error(`pyproject.toml no longer declares the "${extra}" extra`);
}

/** Display names for the harness ids the installer accepts. */
const HARNESS_LABELS: Record<string, string> = {
  "claude-code": "Claude Code",
  codex: "Codex CLI",
  opencode: "opencode",
};
export const harnessLabel = (id: string): string => HARNESS_LABELS[id] ?? id;
