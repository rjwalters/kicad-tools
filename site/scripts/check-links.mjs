/**
 * Post-build verification: crawl `site/dist/` for internal references and
 * report external report/doc destinations, so the manual "check internal
 * links/resources, fragment targets and external report/doc destinations"
 * step in `site/README.md` has a repeatable, scriptable first pass instead
 * of relying solely on manual browsing.
 *
 * This intentionally does NOT replace the manual review: it cannot see
 * rendered layout, contrast, focus order or KiCanvas viewer behavior. It
 * only verifies that:
 *
 *   1. Every internal `href`/`src` in the built HTML resolves to a file
 *      that actually exists in `dist/` (a broken relative link or a stale
 *      asset path fails loudly instead of silently 404ing in production).
 *   2. Every in-page `#fragment` target referenced by an internal link
 *      resolves to an element with a matching `id` on the same page.
 *   3. External `http(s)://` destinations are listed for the reviewer to
 *      spot-check (report/doc links, GitHub issue references, etc.);
 *      network reachability is checked only with `--external`, since CI/
 *      sandboxed environments may not have outbound network access.
 *
 * Usage (from `site/`, after `npm run build`):
 *   node scripts/check-links.mjs               # internal + fragment checks only
 *   node scripts/check-links.mjs --external     # also HEAD-check external URLs
 *   node scripts/check-links.mjs --dist=dist    # override the dist directory
 */
import { readFileSync, existsSync, readdirSync, statSync } from "node:fs";
import { join, resolve } from "node:path";

const args = process.argv.slice(2);
const checkExternal = args.includes("--external");
const distArg = args.find((a) => a.startsWith("--dist="));
const DIST = resolve(distArg ? distArg.split("=")[1] : "dist");

if (!existsSync(DIST)) {
  console.error(`error: dist directory not found at ${DIST} -- run "npm run build" first.`);
  process.exit(1);
}

function walkHtml(dir, out = []) {
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    const st = statSync(full);
    if (st.isDirectory()) walkHtml(full, out);
    else if (entry.endsWith(".html")) out.push(full);
  }
  return out;
}

const HREF_RE = /(?:href|src)="([^"]+)"/g;
const ID_RE = /\sid="([^"]+)"/g;

const htmlFiles = walkHtml(DIST);
const pageIds = new Map(); // page path (relative to DIST) -> Set of element ids
const internalRefs = []; // { fromPage, url }
const externalUrls = new Set();

for (const file of htmlFiles) {
  const content = readFileSync(file, "utf8");
  const relPage = "/" + file.slice(DIST.length + 1).replace(/index\.html$/, "");
  const ids = new Set();
  let m;
  while ((m = ID_RE.exec(content))) ids.add(m[1]);
  pageIds.set(relPage, ids);

  while ((m = HREF_RE.exec(content))) {
    const url = m[1];
    if (url.startsWith("http://") || url.startsWith("https://")) {
      externalUrls.add(url);
    } else if (url.startsWith("/") || url.startsWith("./") || url.startsWith("../") || url.startsWith("#")) {
      internalRefs.push({ fromPage: relPage, url });
    }
  }
}

function resolveDistPath(pathPart) {
  let clean = pathPart.split("?")[0];
  if (clean === "") return DIST + "/index.html"; // same-page fragment only
  if (clean.endsWith("/")) clean += "index.html";
  else if (!clean.includes(".")) clean += "/index.html";
  return join(DIST, clean);
}

const missingTargets = [];
const missingFragments = [];

for (const { fromPage, url } of internalRefs) {
  const [pathPart, fragment] = url.split("#");
  const isSamePage = url.startsWith("#");
  const targetFile = isSamePage ? join(DIST, fromPage.replace(/\/$/, ""), "index.html") : resolveDistPath(pathPart);

  if (!isSamePage && !existsSync(targetFile)) {
    missingTargets.push({ fromPage, url });
    continue;
  }

  if (fragment) {
    const targetPage = isSamePage
      ? fromPage
      : "/" + targetFile.slice(DIST.length + 1).replace(/index\.html$/, "");
    const ids = pageIds.get(targetPage);
    if (ids && !ids.has(fragment)) {
      missingFragments.push({ fromPage, url, targetPage, fragment });
    }
  }
}

console.log(`Scanned ${htmlFiles.length} built page(s) under ${DIST}`);
console.log(`Internal references checked: ${internalRefs.length}`);
console.log(`External destinations found: ${externalUrls.size}`);

let failed = false;

if (missingTargets.length) {
  failed = true;
  console.error(`\nBroken internal link target(s): ${missingTargets.length}`);
  for (const t of missingTargets) console.error(`  ${t.fromPage} -> ${t.url}`);
} else {
  console.log("No broken internal link targets.");
}

if (missingFragments.length) {
  failed = true;
  console.error(`\nUnresolved fragment target(s): ${missingFragments.length}`);
  for (const f of missingFragments) {
    console.error(`  ${f.fromPage} -> ${f.url} (no id="${f.fragment}" on ${f.targetPage})`);
  }
} else {
  console.log("All fragment targets resolve.");
}

if (checkExternal) {
  console.log(`\nChecking ${externalUrls.size} external URL(s) (HEAD, falling back to GET)...`);
  const results = await Promise.all(
    [...externalUrls].map(async (url) => {
      try {
        let res = await fetch(url, { method: "HEAD", redirect: "follow" });
        if (res.status >= 400) res = await fetch(url, { method: "GET", redirect: "follow" });
        return { url, status: res.status, ok: res.ok };
      } catch (err) {
        return { url, status: null, ok: false, error: String(err) };
      }
    }),
  );
  for (const r of results) {
    console.log(`  ${r.ok ? "OK " : "FAIL"} ${r.status ?? "ERR"}  ${r.url}${r.error ? `  (${r.error})` : ""}`);
    if (!r.ok) failed = true;
  }
} else {
  console.log("\nExternal URLs found (not network-checked; pass --external to verify):");
  for (const url of externalUrls) console.log(`  - ${url}`);
}

if (failed) {
  console.error("\ncheck-links: FAILED");
  process.exit(1);
}
console.log("\ncheck-links: OK");
