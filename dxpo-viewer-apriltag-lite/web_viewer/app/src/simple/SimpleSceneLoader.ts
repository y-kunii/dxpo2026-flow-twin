import type { CameraPresetFile } from "../scene/CameraPresets";
import {
  type AprilTagWorldCorners,
  type SceneManifest,
  type SplatAsset,
  type WorldAlignment,
} from "../scene/SceneManifest";
import { validateImmutableCoordinateContract } from "../scene/SceneLoader";

export interface SimpleLoadedScene {
  manifest: SceneManifest;
  primarySplat: SplatAsset;
  primarySplatUrl: string;
  cameraPresets: CameraPresetFile;
  worldAlignment: WorldAlignment;
  aprilTagWorldCorners: AprilTagWorldCorners;
}

async function fetchJson<T>(url: string, label: string): Promise<T> {
  const response = await fetch(url);
  if (!response.ok) throw new Error(`${label}を読み込めません: ${url}`);
  return await response.json() as T;
}

function relativeAssetUrl(baseUrl: string, uri: string): string {
  return new URL(uri, window.location.origin + baseUrl).pathname;
}

/**
 * Load only the immutable assets needed by the simple Viewer.
 *
 * Proxy geometry, scene objects, people, dynamic objects and ROS annotations
 * are deliberately not requested here.  This keeps the simple Viewer a
 * separate, read-only application rather than a hidden mode of the full
 * Viewer.
 */
export async function loadSimpleSceneManifest(manifestUrl: string): Promise<SimpleLoadedScene> {
  const manifest = await fetchJson<SceneManifest>(manifestUrl, "Scene manifest");
  if (!["1.1.0", "1.2.0", "1.3.0"].includes(manifest.schema_version)) {
    throw new Error(`未対応のScene Package schemaです: ${String(manifest.schema_version)}`);
  }
  const coordinateError = validateImmutableCoordinateContract(manifest);
  if (coordinateError) throw new Error(coordinateError);
  if (!manifest.assets?.splats?.length) {
    throw new Error("Scene Packageに3DGS assetがありません。");
  }

  const baseUrl = manifestUrl.slice(0, manifestUrl.lastIndexOf("/") + 1);
  const primarySplat = manifest.assets.splats[0];
  const [cameraPresets, worldAlignment, aprilTagWorldCorners] = await Promise.all([
    fetchJson<CameraPresetFile>(
      relativeAssetUrl(baseUrl, manifest.configs.camera_presets),
      "カメラ設定",
    ),
    fetchJson<WorldAlignment>(
      relativeAssetUrl(baseUrl, manifest.configs.world_alignment),
      "world alignment",
    ),
    fetchJson<AprilTagWorldCorners>(
      relativeAssetUrl(baseUrl, manifest.annotations.apriltag_corners_world),
      "AprilTag annotation",
    ),
  ]);

  return {
    manifest,
    primarySplat,
    primarySplatUrl: relativeAssetUrl(baseUrl, primarySplat.uri),
    cameraPresets,
    worldAlignment,
    aprilTagWorldCorners,
  };
}
