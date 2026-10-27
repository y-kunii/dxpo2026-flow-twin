import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const scriptsRoot = path.dirname(fileURLToPath(import.meta.url));
const refreshScript = path.join(scriptsRoot, "refresh_scene_package_index.mjs");
const temporaryRoot = fs.mkdtempSync(path.join(os.tmpdir(), "scene-package-index-"));
const appRoot = path.join(temporaryRoot, "app");
const runtimeRoot = path.join(appRoot, "public", "scene_package", "runtime");

function writeRuntimePackage({ id, packageTimeoutMs, objectTimeoutMs }) {
  const packageRoot = path.join(runtimeRoot, id);
  fs.mkdirSync(path.join(packageRoot, "splats"), { recursive: true });
  fs.writeFileSync(path.join(packageRoot, "splats", "fixture.spz"), "fixture");
  const annotations = { apriltag_corners_world: "annotations/apriltag_corners_world.json" };
  if (objectTimeoutMs !== undefined) {
    fs.mkdirSync(path.join(packageRoot, "annotations"), { recursive: true });
    fs.writeFileSync(path.join(packageRoot, "annotations", "dynamic_objects.json"), JSON.stringify({
      schema_version: "1.0.0",
      coordinate_frame: "gs_world",
      unit: "meter",
      rotation_order: "xyzw",
      objects: [{ id: "box_01", timeout_ms: objectTimeoutMs }],
    }));
    annotations.dynamic_objects = "annotations/dynamic_objects.json";
  }
  fs.writeFileSync(path.join(packageRoot, "scene_manifest.json"), JSON.stringify({
    schema_version: "1.2.0",
    project_id: "fixture_project",
    scene_id: "fixture_scene",
    scene_package_id: id,
    reconstruction_id: "fixture_reconstruction",
    unit: "meter",
    coordinate_system: {
      up_axis: "Y",
      T_viewer_ros: { translation: [0, 0, 0], rotation_xyzw: [-0.5, 0.5, 0.5, 0.5] },
    },
    dynamic_overlay: {
      topic: "/box/relative_transform",
      freshness: { timeout_ms: packageTimeoutMs, on_timeout: "hide", ordering: "ros_stamp" },
    },
    annotations,
    assets: { splats: [{ uri: "splats/fixture.spz" }] },
  }));
}

function refreshAndReadIndex() {
  execFileSync(process.execPath, [refreshScript], {
    env: { ...process.env, SCENE_PACKAGE_APP_ROOT: appRoot },
    stdio: "pipe",
  });
  return JSON.parse(fs.readFileSync(path.join(appRoot, "public", "scene_package", "distribution_index.json"), "utf8"));
}

try {
  writeRuntimePackage({ id: "valid-v12", packageTimeoutMs: 750 });
  let index = refreshAndReadIndex();
  assert.equal(index.packages.length, 1, "v1.2 package without object timeout must be indexed");
  assert.equal(index.packages[0].scene_package_id, "valid-v12");

  fs.rmSync(runtimeRoot, { recursive: true, force: true });
  writeRuntimePackage({ id: "invalid-v12", packageTimeoutMs: 500, objectTimeoutMs: 1_000 });
  index = refreshAndReadIndex();
  assert.equal(index.packages.length, 0, "v1.2 object timeout must be excluded from runtime index");

  const indexPath = path.join(appRoot, "public", "scene_package", "distribution_index.json");
  const distributionSnapshot = `${JSON.stringify({
    schema_version: "1.0.0",
    kind: "spatial_3dgs_viewer_distribution_index",
    packages: [{ scene_package_id: "checksummed-distribution-snapshot" }],
  }, null, 2)}\n`;
  fs.writeFileSync(indexPath, distributionSnapshot);
  execFileSync(process.execPath, [refreshScript], {
    env: {
      ...process.env,
      SCENE_PACKAGE_APP_ROOT: appRoot,
      SCENE_PACKAGE_INDEX_READ_ONLY: "1",
    },
    stdio: "pipe",
  });
  assert.equal(
    fs.readFileSync(indexPath, "utf8"),
    distributionSnapshot,
    "read-only application distribution must preserve its generated index",
  );
  console.log("Scene Package runtime index v1.2 timeout contract: OK");
} finally {
  fs.rmSync(temporaryRoot, { recursive: true, force: true });
}
