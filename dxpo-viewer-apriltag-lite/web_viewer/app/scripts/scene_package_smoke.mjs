import fs from "node:fs";
import path from "node:path";
import { createHash } from "node:crypto";
import { fileURLToPath } from "node:url";

const appRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const publicationRoot = path.join(appRoot, "public", "scene_package");
const runtimeRoot = path.join(publicationRoot, "runtime");
const indexPath = path.join(publicationRoot, "distribution_index.json");
const supportedSchemas = new Set(["1.0.0", "1.1.0", "1.2.0", "1.3.0"]);

function readJson(file) {
  return JSON.parse(fs.readFileSync(file, "utf8"));
}

function resolveAsset(packageRoot, uri, label) {
  if (typeof uri !== "string" || uri.startsWith("/") || uri.split("/").includes("..")) {
    throw new Error(`invalid ${label} URI`);
  }
  const file = path.join(packageRoot, uri);
  if (!fs.statSync(file).isFile()) throw new Error(`missing ${label}: ${uri}`);
  return file;
}

async function verifySplat(file, expectedSha256, label) {
  const handle = fs.openSync(file, "r");
  const header = Buffer.alloc(64);
  let length;
  try {
    length = fs.readSync(handle, header, 0, header.length, 0);
  } finally {
    fs.closeSync(handle);
  }
  if (header.subarray(0, length).toString("utf8").startsWith("version https://git-lfs.github.com/spec/v1")) {
    throw new Error(`${label}: Git LFS pointer found; install Git LFS and run git lfs pull`);
  }
  if (typeof expectedSha256 !== "string" || expectedSha256.length === 0) return;
  const hash = createHash("sha256");
  for await (const chunk of fs.createReadStream(file)) hash.update(chunk);
  if (hash.digest("hex") !== expectedSha256.toLowerCase()) {
    throw new Error(`${label}: SHA-256 mismatch`);
  }
}

const index = fs.existsSync(indexPath) && fs.statSync(indexPath).isFile()
  ? readJson(indexPath)
  : { packages: [] };
const packages = Array.isArray(index.packages) ? index.packages : [];
if (packages.length === 0) {
  // A source checkout intentionally carries no generated runtime Package.
  // The adjacent refresh_scene_package_index_test.mjs supplies the synthetic
  // contract fixture; this script verifies real publications when present.
  console.log(JSON.stringify({ status: "not_available", package_count: 0 }, null, 2));
  process.exit(0);
}

const results = [];
for (const entry of packages) {
  const packageId = entry?.scene_package_id;
  if (typeof packageId !== "string" || !/^[A-Za-z0-9][A-Za-z0-9_.-]*$/.test(packageId)) {
    throw new Error("runtime index contains an invalid Scene Package ID");
  }
  const packageRoot = path.join(runtimeRoot, packageId);
  const manifest = readJson(path.join(packageRoot, "scene_manifest.json"));
  if (
    manifest.scene_package_id !== packageId ||
    !supportedSchemas.has(manifest.schema_version) ||
    manifest.unit !== "meter" ||
    manifest.coordinate_system?.up_axis !== "Y"
  ) {
    throw new Error(`${packageId}: manifest contract is not a supported meter/+Y package`);
  }
  if (!manifest.assets?.splats?.length) throw new Error(`${packageId}: manifest has no splat asset`);
  for (const [assetIndex, asset] of manifest.assets.splats.entries()) {
    const label = `splat_${assetIndex}`;
    const file = resolveAsset(packageRoot, asset.uri, label);
    await verifySplat(file, asset.sha256, label);
  }

  let proxySummary = null;
  const proxy = manifest.assets.proxy_mesh;
  if (proxy) {
    const glb = resolveAsset(packageRoot, proxy.uri, "proxy_glb");
    const metadata = readJson(resolveAsset(packageRoot, proxy.metadata_uri, "proxy_metadata"));
    const quality = readJson(resolveAsset(packageRoot, proxy.quality_uri, "proxy_quality"));
    if (quality.status !== "verified" || quality.proxy_kind !== "surface_mesh") {
      throw new Error(`${packageId}: proxy quality is not verified`);
    }
    if (metadata.mesh?.vertex_count !== quality.vertex_count || metadata.mesh?.triangle_count !== quality.triangle_count) {
      throw new Error(`${packageId}: proxy metadata and quality counts differ`);
    }
    if (fs.readFileSync(glb).subarray(0, 4).toString("ascii") !== "glTF") {
      throw new Error(`${packageId}: proxy GLB header is invalid`);
    }
    proxySummary = {
      vertex_count: quality.vertex_count,
      triangle_count: quality.triangle_count,
      glb_bytes: fs.statSync(glb).size,
    };
  } else if (manifest.capabilities?.measurement || manifest.capabilities?.surface_placement) {
    throw new Error(`${packageId}: geometry capability is enabled without a proxy asset`);
  }

  results.push({
    scene_package_id: packageId,
    scene_id: manifest.scene_id,
    splat_count: manifest.assets.splats.length,
    proxy: proxySummary,
  });
}

console.log(JSON.stringify({ status: "verified", package_count: results.length, packages: results }, null, 2));
