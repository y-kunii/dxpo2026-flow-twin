#!/usr/bin/env node
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";


const appRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const targets = [
  "colmap",
  "apriltag-check",
  "spz-import",
  "scene-manager",
  "project-manager",
  "package-builder",
  "viewer",
  "field-memory",
  "site-insights",
  "frame-analysis",
];
const failures = [];

function filesBelow(root) {
  if (!fs.existsSync(root)) return [];
  return fs.readdirSync(root, { withFileTypes: true }).flatMap((entry) => {
    const absolute = path.join(root, entry.name);
    return entry.isDirectory() ? filesBelow(absolute) : [absolute];
  });
}

for (const target of targets) {
  const root = path.join(appRoot, "dist", "apps", target);
  const index = path.join(root, "index.html");
  if (!fs.existsSync(index)) {
    failures.push(`${target}:index_missing`);
    continue;
  }
  const assets = path.join(root, "assets");
  const scripts = fs.existsSync(assets)
    ? fs.readdirSync(assets).filter((name) => name.endsWith(".js"))
    : [];
  if (scripts.length === 0) failures.push(`${target}:javascript_missing`);
  // Every standalone application intentionally includes a lazy read-only
  // Viewer route so search/analysis records can open their Scene Package in
  // the same Web origin. Keep canonical data out of the build; bundle size is
  // not a reliable data-boundary check.
  const canonicalAssets = filesBelow(root).filter((name) =>
    /\.(spz|ply|glb|ksplat|sog|sogs|rad)$/i.test(name)
    || name.includes(`${path.sep}scene_package${path.sep}runtime${path.sep}`)
  );
  if (canonicalAssets.length > 0) {
    failures.push(`${target}:canonical_scene_data_leaked`);
  }
}

if (failures.length > 0) {
  console.error(`Standalone app smoke: FAILED\n${failures.map((item) => `- ${item}`).join("\n")}`);
  process.exit(1);
}
console.log(`Standalone app smoke: ${targets.length} app builds verified`);
