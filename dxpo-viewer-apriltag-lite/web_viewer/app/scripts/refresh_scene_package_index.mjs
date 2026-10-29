#!/usr/bin/env node
/**
 * Rebuild the generated App B package index from already-published runtime packages.
 *
 * The local API can list canonical Scene Packages, but a standalone Vite Viewer
 * must also work without that API. Runtime packages are created only by the App B
 * publish step (or copied into the read-only distribution after validation), so
 * this script never imports source SPZ files or creates a Scene Package. It only
 * exposes published packages that have a complete local SPZ asset.
 */
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const appRoot = process.env.SCENE_PACKAGE_APP_ROOT
  ? path.resolve(process.env.SCENE_PACKAGE_APP_ROOT)
  : path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const packageRoot = path.join(appRoot, "public", "scene_package");
const runtimeRoot = path.join(packageRoot, "runtime");
const indexPath = path.join(packageRoot, "distribution_index.json");
const ID = /^[A-Za-z0-9][A-Za-z0-9_.-]*$/;

// Application distributions contain a generator-validated, checksummed
// snapshot. Their public Scene Package tree is intentionally mounted
// read-only, so the normal development-time predev refresh must not rewrite
// the index. Source checkouts keep the existing refresh behavior.
if (process.env.SCENE_PACKAGE_INDEX_READ_ONLY === "1") {
  if (!fs.existsSync(indexPath) || !fs.statSync(indexPath).isFile()) {
    throw new Error("read-only Scene Package distribution index is missing");
  }
  console.log("Scene Package index: using read-only distribution snapshot");
  process.exit(0);
}

function readJson(file) {
  return JSON.parse(fs.readFileSync(file, "utf8"));
}

function isSafeRelativeUri(value) {
  return typeof value === "string" && value.length > 0 && !path.isAbsolute(value) && !value.split(/[\\/]/).includes("..");
}

function projectNameFromWorkspaceUri(value) {
  if (typeof value !== "string" || !value) return undefined;
  const parts = value.replaceAll("\\", "/").split("/").filter(Boolean);
  const index = parts.indexOf("projects");
  const projectName = index >= 0 ? parts[index + 1] : undefined;
  return projectName && ID.test(projectName) ? projectName : undefined;
}

function projectNameFromManifest(manifest, packageDirectory) {
  if (typeof manifest.project_id === "string" && ID.test(manifest.project_id)) return manifest.project_id;
  if (typeof manifest.project_name === "string" && ID.test(manifest.project_name)) return manifest.project_name;
  if (typeof manifest.provenance?.project_name === "string" && ID.test(manifest.provenance.project_name)) {
    return manifest.provenance.project_name;
  }
  // Legacy published packages predate the explicit project_name field. Their
  // bundled MRP is authoritative and lets the Viewer show the same App A
  // project name without mutating a user's generated Scene Package.
  const mrpManifest = path.join(packageDirectory, "metric_reconstruction", "manifest.json");
  try {
    return projectNameFromWorkspaceUri(readJson(mrpManifest)?.provenance?.source_workspace_uri);
  } catch {
    return undefined;
  }
}

function hasForbiddenV12ObjectTimeout(manifest, packageDirectory) {
  if (!["1.2.0", "1.3.0"].includes(manifest.schema_version)) return false;
  const dynamicObjectsUri = manifest.annotations?.dynamic_objects;
  if (dynamicObjectsUri === undefined) return false;
  if (!isSafeRelativeUri(dynamicObjectsUri)) return true;
  const dynamicObjectsPath = path.resolve(packageDirectory, dynamicObjectsUri);
  if (!dynamicObjectsPath.startsWith(`${packageDirectory}${path.sep}`) || !fs.existsSync(dynamicObjectsPath)) return true;
  try {
    const document = readJson(dynamicObjectsPath);
    return !Array.isArray(document?.objects) || document.objects.some(
      (object) => object && typeof object === "object" && Object.hasOwn(object, "timeout_ms"),
    );
  } catch {
    return true;
  }
}

function validPublishedPackage(directoryName) {
  if (!ID.test(directoryName)) return undefined;
  const packageDirectory = path.join(runtimeRoot, directoryName);
  const manifestPath = path.join(packageDirectory, "scene_manifest.json");
  if (!fs.existsSync(manifestPath)) return undefined;

  let manifest;
  try {
    manifest = readJson(manifestPath);
  } catch {
    return undefined;
  }
  if (!manifest || typeof manifest !== "object" || manifest.scene_package_id !== directoryName) return undefined;
  if (!["1.1.0", "1.2.0", "1.3.0"].includes(manifest.schema_version)) return undefined;
  if (!ID.test(String(manifest.scene_id ?? "")) || !ID.test(String(manifest.reconstruction_id ?? ""))) return undefined;
  if (manifest.unit !== "meter" || manifest.coordinate_system?.up_axis !== "Y") return undefined;
  if (["1.2.0", "1.3.0"].includes(manifest.schema_version)) {
    if (!ID.test(String(manifest.project_id ?? ""))) return undefined;
    const overlay = manifest.dynamic_overlay;
    const viewerFromRos = manifest.coordinate_system?.T_viewer_ros;
    const timeoutMs = overlay?.freshness?.timeout_ms;
    if (overlay?.topic !== "/box/relative_transform" || typeof timeoutMs !== "number" || !Number.isFinite(timeoutMs) || timeoutMs <= 0
      || overlay?.freshness?.on_timeout !== "hide" || overlay?.freshness?.ordering !== "ros_stamp") return undefined;
    if (!Array.isArray(viewerFromRos?.translation) || viewerFromRos.translation.length !== 3
      || !Array.isArray(viewerFromRos?.rotation_xyzw) || viewerFromRos.rotation_xyzw.length !== 4) return undefined;
    if (hasForbiddenV12ObjectTimeout(manifest, packageDirectory)) return undefined;
  }

  const splat = Array.isArray(manifest.assets?.splats) ? manifest.assets.splats[0] : undefined;
  if (!splat || !isSafeRelativeUri(splat.uri)) return undefined;
  const splatPath = path.resolve(packageDirectory, splat.uri);
  if (!splatPath.startsWith(`${packageDirectory}${path.sep}`) || !fs.existsSync(splatPath) || !fs.statSync(splatPath).isFile()) {
    return undefined;
  }

  return {
    managed: true,
    published: true,
    // Runtime packages are created only after the App B publish/validation gate.
    // This index must not be used to turn an arbitrary raw SPZ into a package.
    status: "verified",
    scene_package_id: directoryName,
    project_id: typeof manifest.project_id === "string" ? manifest.project_id : projectNameFromManifest(manifest, packageDirectory),
    project_name: projectNameFromManifest(manifest, packageDirectory),
    scene_id: manifest.scene_id,
    reconstruction_id: manifest.reconstruction_id,
    viewer_url: `/?scene_package=${encodeURIComponent(directoryName)}`,
    spz: { filename: path.basename(splat.uri) },
    capabilities: manifest.capabilities ?? {},
  };
}

const packages = fs.existsSync(runtimeRoot)
  ? fs.readdirSync(runtimeRoot, { withFileTypes: true })
    .filter((entry) => entry.isDirectory())
    .map((entry) => validPublishedPackage(entry.name))
    .filter(Boolean)
    .sort((left, right) => left.scene_package_id.localeCompare(right.scene_package_id))
  : [];

fs.mkdirSync(packageRoot, { recursive: true });
const temporaryPath = `${indexPath}.tmp-${process.pid}`;
fs.writeFileSync(temporaryPath, `${JSON.stringify({
  kind: "spatial_3dgs_viewer_runtime_index",
  schema_version: "1.0.0",
  packages,
}, null, 2)}\n`, "utf8");
fs.renameSync(temporaryPath, indexPath);
console.log(`Scene Package index: ${packages.length} published package(s)`);
