/** Content-bound manufacturing evidence. Runs only at build time. */
import { createHash } from "node:crypto";
import { existsSync, readFileSync, readdirSync, realpathSync, statSync } from "node:fs";
import { isAbsolute, join, relative, resolve } from "node:path";
import type { KnownIssue, Readiness } from "./types.ts";

const KNOWN_ISSUE_KINDS = ["defect", "exception", "advisory"];
const KNOWN_ISSUE_KEYS = new Set(["issue", "kind", "summary", "recorded", "warnings"]);

/** Mirrors `validate_known_issues` in `src/kicad_tools/cli/known_issues.py` (#7261). */
export function validateKnownIssues(value: unknown): KnownIssue[] {
  if (value === undefined || value === null) return [];
  if (!Array.isArray(value)) throw new Error("known_issues must be a list");
  let previous = 0;
  for (const entry of value) {
    if (!entry || typeof entry !== "object" || Array.isArray(entry)) throw new Error("known issue must be an object");
    if (Object.keys(entry).some(key => !KNOWN_ISSUE_KEYS.has(key))) throw new Error("unknown known-issue field");
    const { issue, kind, summary, recorded, warnings } = entry as Record<string, unknown>;
    if (typeof issue !== "number" || !Number.isInteger(issue) || issue < 1) throw new Error("invalid known issue number");
    if (issue <= previous) throw new Error("known_issues must be unique and sorted by issue number");
    previous = issue;
    if (!KNOWN_ISSUE_KINDS.includes(kind as string)) throw new Error(`invalid kind for known issue #${issue}`);
    if (typeof summary !== "string" || !summary.trim() || summary !== summary.trim()) throw new Error(`invalid summary for known issue #${issue}`);
    if (typeof recorded !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(recorded) || !Number.isFinite(Date.parse(recorded))) {
      throw new Error(`invalid recorded date for known issue #${issue}`);
    }
    if (warnings !== undefined && (!warnings || typeof warnings !== "object" || Array.isArray(warnings)
      || Object.keys(warnings).length === 0
      || Object.values(warnings).some(n => typeof n !== "number" || !Number.isInteger(n) || n < 1))) {
      throw new Error(`invalid warnings for known issue #${issue}`);
    }
  }
  return value as KnownIssue[];
}

function canonical(entries: KnownIssue[]): string {
  return JSON.stringify(entries.map(e => ({ issue: e.issue, kind: e.kind, summary: e.summary, recorded: e.recorded,
    warnings: e.warnings === undefined ? null : Object.fromEntries(Object.entries(e.warnings).sort()) })));
}

/** Mirrors `_evidence_folders` in `src/kicad_tools/cli/board_readiness.py` (#7337). */
export function evidenceFolders(evidence: unknown, bound: string[]): Set<string> {
  const names = evidence && typeof evidence === "object" && !Array.isArray(evidence)
    ? Object.values(evidence).filter((v): v is string => typeof v === "string") : [];
  const folders = new Set<string>();
  for (const name of [...names, ...bound]) {
    const parts = name.split("/").filter(part => part !== "" && part !== ".");
    if (parts.includes("..") || isAbsolute(name)) continue;
    const depth = parts[0] === "output" ? 2 : 1;
    if (parts.length > depth) folders.add(parts.slice(0, depth).join("/"));
  }
  return folders.size ? folders : new Set(["output/readiness"]);
}

export function loadReadiness(boardPath: string): Readiness {
  try {
    const data = JSON.parse(readFileSync(join(boardPath, "output/readiness.json"), "utf8"));
    if (!data || data.schema_version !== 1 || !["assembly", "pcb_only"].includes(data.mode)
      || !["ready", "blocked", "unverified"].includes(data.status)
      || typeof data.checked_at !== "string" || !Number.isFinite(Date.parse(data.checked_at))
      || !Array.isArray(data.blockers) || !data.blockers.every((b: unknown) => typeof b === "string")
      || !data.inputs || typeof data.inputs !== "object" || Array.isArray(data.inputs)
      || Object.keys(data.inputs).length === 0) throw new Error("invalid readiness report");
    const checks = data.checks ?? [];
    const metrics = data.metrics === undefined ? {} : data.metrics;
    if (!metrics || typeof metrics !== "object" || Array.isArray(metrics)) throw new Error("invalid current metrics");
    for (const name of ["drc_violations", "lvs_mismatches", "nets_routed_pct"]) {
      const value = metrics[name];
      if (value !== undefined && (typeof value !== "number" || !Number.isFinite(value) || value < 0
        || (name !== "nets_routed_pct" && !Number.isInteger(value))
        || (name === "nets_routed_pct" && value > 100))) throw new Error(`invalid current metric: ${name}`);
    }
    if (metrics.lvs_clean !== undefined && typeof metrics.lvs_clean !== "boolean") throw new Error("invalid current metric: lvs_clean");
    if (!Array.isArray(checks) || checks.some((c: Record<string, unknown>) => !c
      || typeof c.name !== "string" || !["passed", "failed", "not_run"].includes(c.status as string)
      || (c.detail !== undefined && typeof c.detail !== "string"))) throw new Error("invalid checks");
    const root = realpathSync(boardPath);
    for (const [name, expected] of Object.entries(data.inputs)) {
      if (isAbsolute(name) || name.split("/").includes("..") || name.includes("\\")) {
        throw new Error("input path must be board-relative");
      }
      const target = realpathSync(resolve(root, name));
      const rel = relative(root, target);
      if (rel === ".." || rel.startsWith("../") || isAbsolute(rel)) throw new Error("input path leaves board directory");
      const actual = createHash("sha256").update(readFileSync(target)).digest("hex");
      if (actual !== expected) throw new Error(`Evidence is stale: ${name} changed`);
    }
    for (const name of readdirSync(join(root, "output"))) {
      if (statSync(join(root, "output", name)).isFile()
        && (/\.(kicad_pro|kicad_dru)$/.test(name) || name.endsWith("net_class_map.json") || name === "fab_profile.json")
        && !("output/" + name in data.inputs)) throw new Error("Evidence omits current project or rule files");
    }
    if (data.manufacturer !== undefined && (typeof data.manufacturer !== "string" || !data.manufacturer)) {
      throw new Error("invalid manufacturer");
    }
    // #7261: the list must equal its hashed known-issues.json evidence (already hash-checked above).
    const knownIssues = validateKnownIssues(data.known_issues);
    const bound = Object.keys(data.inputs).filter(n => n.split("/").pop() === "known-issues.json");
    // #7323: a known-issues.json in the evidence folder that inputs does not list was unbound and dropped.
    // #7337: only the record's own evidence folder(s) count; --verify's output/readiness-verification/
    // and the committed output/readiness/ never orphan each other.
    const orphan = [...evidenceFolders(data.evidence, bound)].flatMap(folder => {
      const dir = join(root, folder);
      if (!existsSync(dir) || !statSync(dir).isDirectory()) return [];
      return (readdirSync(dir, { recursive: true }) as string[])
        .map(name => `${folder}/${String(name).replaceAll("\\", "/")}`)
        .filter(name => name.split("/").pop() === "known-issues.json"
          && statSync(join(root, name)).isFile() && !(name in data.inputs));
    }).sort()[0];
    if (orphan) throw new Error(`${orphan} is not listed in inputs (orphaned known-issues evidence)`);
    if (bound.length > 1) throw new Error("known_issues is bound to more than one evidence file");
    if (bound.length === 0 && knownIssues.length) throw new Error("known_issues is not bound to hashed evidence");
    if (bound.length === 1) {
      const recorded = JSON.parse(readFileSync(resolve(root, bound[0]), "utf8"));
      if (canonical(validateKnownIssues(recorded?.known_issues)) !== canonical(knownIssues)) {
        throw new Error("known_issues differs from its hashed evidence");
      }
    }
    if (data.status === "ready" && knownIssues.some(e => e.kind === "defect")) {
      throw new Error("Ready conflicts with a known defect");
    }
    if (data.status === "ready") {
      if (!["kct_check", "native_drc", "artifacts", "bom"].every(name =>
        checks.some((c: { name: string }) => c.name === name))) {
        throw new Error("Ready requires kct_check, native_drc, artifacts and bom checks");
      }
      const names = Object.keys(data.inputs);
      if (!names.some(n => n.endsWith(".kicad_pcb")) || !names.some(n => n.endsWith(".kicad_sch"))
        || !("output/manufacturing/manifest.json" in data.inputs)) {
        throw new Error("Ready requires PCB, schematic and manufacturing evidence");
      }
      if (data.blockers.length || checks.some((c: { status: string }) => c.status !== "passed")) {
        throw new Error("Ready conflicts with incomplete or failed checks");
      }
    }
    if (data.status === "unverified") delete data.metrics;
    return data as Readiness;
  } catch (error) {
    const detail = error instanceof Error && !("code" in error)
      ? error.message : "Readiness report or checked files are missing or unreadable.";
    return { status: "unverified", blockers: [`Readiness needs verification: ${detail}`] };
  }
}
