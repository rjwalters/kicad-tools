/** Content-bound manufacturing evidence. Runs only at build time. */
import { createHash } from "node:crypto";
import { existsSync, readFileSync, readdirSync, realpathSync, statSync } from "node:fs";
import { isAbsolute, join, relative, resolve } from "node:path";
import type { KnownIssue, Readiness } from "./types.ts";

const KNOWN_ISSUE_KINDS = ["defect", "exception", "advisory"];
const KNOWN_ISSUE_KEYS = new Set(["issue", "kind", "summary", "recorded", "warnings", "errors"]);

type Shape = "pad" | "at" | "item";
const SHAPE_KEYS: Record<Shape, readonly string[]> = {
  pad: ["rule", "ref", "pad"], at: ["rule", "at"], item: ["rule", "item"],
};
const SORT_ORDER: Record<Shape, string> = { pad: "(rule, ref, pad)", at: "(rule, x, y)", item: "(rule, item)" };

type Identity = { rule: string; ref?: string; pad?: string; item?: string; at?: [number, number] };
type IdentityKey = [string, string, string, string, number, number, number];

/** Mirrors `identity_key` in `known_issues.py`: one key for sorting, duplicates and ownership. */
function identityKey(item: Identity): IdentityKey {
  const [x, y] = item.at ?? [0, 0];
  return [item.rule, item.ref ?? "", item.pad ?? "", item.item ?? "", item.at ? 1 : 0, x, y];
}

function keyLess(a: IdentityKey, b: IdentityKey): boolean {
  for (let i = 0; i < a.length; i++) if (a[i] !== b[i]) return a[i] < b[i];
  return false;
}

/** Mirrors `_check_identities` in `known_issues.py` (#7389, #7355): sorted, unique identities of the accepted shapes. */
function checkIdentities(
  value: unknown, field: "warnings" | "errors", shapes: Shape[], issue: number, owners: Map<string, number>,
): void {
  const bad = (why: string) => new Error(`invalid ${field} for known issue #${issue}: ${why}`);
  const expected = shapes.map(s => `{${SHAPE_KEYS[s].map(k => (k === "at" ? "at: [x_mm, y_mm]" : k)).join(", ")}}`).join(" or ");
  if (!Array.isArray(value) || value.length === 0) throw bad("expected a non-empty list");
  let previous: IdentityKey | null = null;
  for (const item of value) {
    const keys = item && typeof item === "object" && !Array.isArray(item) ? Object.keys(item) : [];
    const shape = shapes.find(s => keys.length === SHAPE_KEYS[s].length && SHAPE_KEYS[s].every(k => keys.includes(k)));
    if (!shape) throw bad(`an identity must be exactly ${expected}`);
    const record = item as Record<string, unknown>;
    if (typeof record.rule !== "string" || !/^[A-Za-z0-9_.-]+$/.test(record.rule)) throw bad(`invalid rule id ${String(record.rule)}`);
    let identity: Identity;
    let text: string;
    if (shape === "at") {
      const at = record.at;
      if (!Array.isArray(at) || at.length !== 2 || !at.every(c => typeof c === "number" && Number.isFinite(c))) {
        throw bad("at must be [x_mm, y_mm]");
      }
      const [x, y] = at as [number, number];
      if (Object.is(x, -0) || Object.is(y, -0) || Number(x.toFixed(2)) !== x || Number(y.toFixed(2)) !== y) {
        throw bad("at must be rounded to 0.01 mm");
      }
      identity = { rule: record.rule, at: [x, y] };
      text = `${record.rule} at ${x}/${y}`;
    } else if (shape === "item") {
      const part = record.item;
      if (typeof part !== "string" || !part || part !== part.trim()) throw bad("item must be non-empty, trimmed text");
      identity = { rule: record.rule, item: part };
      text = `${record.rule} on ${part}`;
    } else {
      for (const key of ["ref", "pad"]) {
        const part = record[key];
        if (typeof part !== "string" || !part || part !== part.trim()) throw bad(`${key} must be non-empty, trimmed text`);
      }
      identity = { rule: record.rule, ref: record.ref as string, pad: record.pad as string };
      text = `${record.rule} ${record.pad}`;
    }
    const key = identityKey(identity);
    const id = JSON.stringify(key);
    const owner = owners.get(id);
    if (owner !== undefined) {
      throw bad(owner === issue ? `${text} is listed more than once` : `${text} is listed by both #${owner} and #${issue}`);
    }
    if (previous && !keyLess(previous, key)) throw bad(`identities must be sorted by ${shapes.map(s => SORT_ORDER[s]).join(" then ")}`);
    previous = key;
    owners.set(id, issue);
  }
}

/** Mirrors `validate_known_issues` in `src/kicad_tools/cli/known_issues.py` (#7261). */
export function validateKnownIssues(value: unknown): KnownIssue[] {
  if (value === undefined || value === null) return [];
  if (!Array.isArray(value)) throw new Error("known_issues must be a list");
  let previous = 0;
  const owners = new Map<string, number>();
  for (const entry of value) {
    if (!entry || typeof entry !== "object" || Array.isArray(entry)) throw new Error("known issue must be an object");
    if (Object.keys(entry).some(key => !KNOWN_ISSUE_KEYS.has(key))) throw new Error("unknown known-issue field");
    const { issue, kind, summary, recorded, warnings, errors } = entry as Record<string, unknown>;
    if (typeof issue !== "number" || !Number.isInteger(issue) || issue < 1) throw new Error("invalid known issue number");
    if (issue <= previous) throw new Error("known_issues must be unique and sorted by issue number");
    previous = issue;
    if (!KNOWN_ISSUE_KINDS.includes(kind as string)) throw new Error(`invalid kind for known issue #${issue}`);
    if (typeof summary !== "string" || !summary.trim() || summary !== summary.trim()) throw new Error(`invalid summary for known issue #${issue}`);
    if (typeof recorded !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(recorded) || !Number.isFinite(Date.parse(recorded))) {
      throw new Error(`invalid recorded date for known issue #${issue}`);
    }
    if (warnings !== undefined) checkIdentities(warnings, "warnings", ["pad"], issue, owners);
    if (errors !== undefined) {
      if (kind !== "exception") throw new Error(`only an exception may carry errors (known issue #${issue})`);
      checkIdentities(errors, "errors", ["at", "item"], issue, owners);
    }
  }
  return value as KnownIssue[];
}

function canonical(entries: KnownIssue[]): string {
  return JSON.stringify(entries.map(e => ({ issue: e.issue, kind: e.kind, summary: e.summary, recorded: e.recorded,
    warnings: e.warnings === undefined ? null : e.warnings.map(w => ({ rule: w.rule, ref: w.ref, pad: w.pad })),
    errors: e.errors === undefined ? null : e.errors.map(x => ("at" in x ? { rule: x.rule, at: x.at } : { rule: x.rule, item: x.item })) })));
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
