import type {
  AprilTagWorldCorners,
  CoordinateTransform,
  DynamicOverlayConfiguration,
  ProxyQuality,
  SceneManifest,
  SplatAsset,
  WorldAlignment
} from "./SceneManifest";
import type { CameraPresetFile } from "./CameraPresets";
import type { ProxyMetadata } from "../proxy/ProxyGeometry";
import type { SceneObjectsDocument } from "./SceneObjects";
import {
  createDefaultDynamicRegistry,
  QUATERNION_UNIT_NORM_TOLERANCE,
  validateDynamicAnnotations,
  type DynamicObjectRegistry,
  type FixedTagMap,
} from "../dynamic/DynamicTrackingContract";

export interface LoadedScene {
  manifest: SceneManifest;
  baseUrl: string;
  primarySplat: SplatAsset;
  primarySplatUrl: string;
  cameraPresets: CameraPresetFile;
  worldAlignment: WorldAlignment;
  aprilTagWorldCorners: AprilTagWorldCorners;
  sceneObjects: SceneObjectsDocument;
  proxyMeshUrl?: string;
  proxyMetadata?: ProxyMetadata;
  proxyQuality?: ProxyQuality;
  fixedTagMap?: FixedTagMap;
  dynamicObjectRegistry?: DynamicObjectRegistry;
  dynamicTrackingError?: string;
  viewerFromRos?: CoordinateTransform;
  dynamicOverlay?: DynamicOverlayConfiguration;
}

function finiteTuple(value: unknown, size: number): value is number[] {
  return Array.isArray(value) && value.length === size && value.every((item) => typeof item === "number" && Number.isFinite(item));
}

export function validateImmutableCoordinateContract(manifest: SceneManifest): string | undefined {
  if (manifest.schema_version !== "1.2.0" && manifest.schema_version !== "1.3.0") return undefined;
  if (!manifest.project_id || !manifest.scene_package_id) return "Immutable Scene Package IDs are missing.";
  const coordinate = manifest.coordinate_system;
  if (coordinate.ros?.handedness !== "right" || coordinate.viewer?.handedness !== "right" ||
      !finiteTuple(coordinate.T_viewer_ros?.translation, 3) || !finiteTuple(coordinate.T_viewer_ros?.rotation_xyzw, 4)) {
    return "Scene Package coordinate_system.T_viewer_ros is invalid.";
  }
  const quaternionNorm = Math.hypot(...coordinate.T_viewer_ros.rotation_xyzw);
  if (quaternionNorm <= 1e-12) {
    return "Scene Package coordinate_system.T_viewer_ros has a zero quaternion.";
  }
  if (Math.abs(quaternionNorm - 1) > QUATERNION_UNIT_NORM_TOLERANCE) {
    return `Scene Package coordinate_system.T_viewer_ros quaternion must be normalized within ${QUATERNION_UNIT_NORM_TOLERANCE}.`;
  }
  const overlay = manifest.dynamic_overlay;
  if (!overlay || overlay.topic !== "/box/relative_transform" || overlay.freshness.ordering !== "ros_stamp" ||
      overlay.freshness.on_timeout !== "hide" || !Number.isFinite(overlay.freshness.timeout_ms) || overlay.freshness.timeout_ms <= 0) {
    return "Scene Package dynamic_overlay policy is invalid.";
  }
  return undefined;
}

export async function loadSceneManifest(manifestUrl: string): Promise<LoadedScene> {
  const response = await fetch(manifestUrl);

  if (!response.ok) {
    throw new Error(`Failed to load scene manifest: ${manifestUrl}`);
  }

  const manifest = (await response.json()) as SceneManifest;

  if (!["1.1.0", "1.2.0", "1.3.0"].includes(manifest.schema_version)) {
    throw new Error(`Unsupported Scene Package schema: ${String(manifest.schema_version)}`);
  }
  const immutableContractError = validateImmutableCoordinateContract(manifest);
  if (immutableContractError) throw new Error(immutableContractError);

  if (!manifest.assets?.splats?.length) {
    throw new Error("Scene manifest does not contain splat assets.");
  }

  const baseUrl = manifestUrl.slice(0, manifestUrl.lastIndexOf("/") + 1);
  const primarySplat = manifest.assets.splats[0];
  const primarySplatUrl = new URL(primarySplat.uri, window.location.origin + baseUrl).pathname;

  const cameraPresetsUrl = new URL(
    manifest.configs.camera_presets,
    window.location.origin + baseUrl
  ).pathname;

  const cameraPresetsResponse = await fetch(cameraPresetsUrl);

  if (!cameraPresetsResponse.ok) {
    throw new Error(`Failed to load camera presets: ${cameraPresetsUrl}`);
  }

  const cameraPresets = (await cameraPresetsResponse.json()) as CameraPresetFile;

  const worldAlignmentUrl = new URL(
    manifest.configs.world_alignment,
    window.location.origin + baseUrl
  ).pathname;
  const worldAlignmentResponse = await fetch(worldAlignmentUrl);

  if (!worldAlignmentResponse.ok) {
    throw new Error(`Failed to load world alignment: ${worldAlignmentUrl}`);
  }

  const worldAlignment = (await worldAlignmentResponse.json()) as WorldAlignment;

  const aprilTagCornersUrl = new URL(
    manifest.annotations.apriltag_corners_world,
    window.location.origin + baseUrl
  ).pathname;
  const aprilTagCornersResponse = await fetch(aprilTagCornersUrl);

  if (!aprilTagCornersResponse.ok) {
    throw new Error(`Failed to load AprilTag world corners: ${aprilTagCornersUrl}`);
  }

  const aprilTagWorldCorners =
    (await aprilTagCornersResponse.json()) as AprilTagWorldCorners;

  let sceneObjects: SceneObjectsDocument = {
    schema_version: "1.0.0",
    scene_id: manifest.scene_id,
    coordinate_space: "world",
    unit: "meter",
    up_axis: "+Y",
    objects: []
  };
  if (manifest.annotations.scene_objects) {
    const sceneObjectsUrl = new URL(
      manifest.annotations.scene_objects,
      window.location.origin + baseUrl
    ).pathname;
    const sceneObjectsResponse = await fetch(sceneObjectsUrl);
    if (!sceneObjectsResponse.ok) {
      throw new Error(`Failed to load scene objects: ${sceneObjectsUrl}`);
    }
    sceneObjects = (await sceneObjectsResponse.json()) as SceneObjectsDocument;
  }

  let proxyMeshUrl: string | undefined;
  let proxyMetadata: ProxyMetadata | undefined;
  let proxyQuality: ProxyQuality | undefined;
  if (manifest.assets.proxy_mesh) {
    const proxy = manifest.assets.proxy_mesh;
    const proxyUrl = new URL(proxy.uri, window.location.origin + baseUrl).pathname;
    const metadataUrl = new URL(proxy.metadata_uri, window.location.origin + baseUrl).pathname;
    const qualityUrl = new URL(proxy.quality_uri, window.location.origin + baseUrl).pathname;
    const [metadataResponse, qualityResponse] = await Promise.all([
      fetch(metadataUrl),
      fetch(qualityUrl)
    ]);
    if (metadataResponse.ok && qualityResponse.ok) {
      const candidateQuality = (await qualityResponse.json()) as ProxyQuality;
      if (candidateQuality.status === "verified") {
        proxyMeshUrl = proxyUrl;
        proxyMetadata = (await metadataResponse.json()) as ProxyMetadata;
        proxyQuality = candidateQuality;
      }
    }
  }

  let fixedTagMap: FixedTagMap | undefined;
  let dynamicObjectRegistry: DynamicObjectRegistry | undefined;
  let dynamicTrackingError: string | undefined;
  const viewerFromRos = manifest.coordinate_system.T_viewer_ros;
  const dynamicOverlay = manifest.dynamic_overlay;
  if (!viewerFromRos || !dynamicOverlay) {
    // v1.1 remains display-compatible, but dynamic ROS tracking is not
    // guessed without the explicit v1.2 coordinate/runtime contract.
    dynamicTrackingError = "Dynamic overlay disabled: Scene Package lacks T_viewer_ros or dynamic_overlay.";
  }
  const fixedTagMapReference = manifest.annotations.fixed_tag_map;
  const dynamicObjectsReference = manifest.annotations.dynamic_objects;
  if (!dynamicTrackingError && (fixedTagMapReference || dynamicObjectsReference)) {
    const fixedTagMapUrl = fixedTagMapReference
      ? new URL(fixedTagMapReference, window.location.origin + baseUrl).pathname
      : undefined;
    const dynamicObjectsUrl = dynamicObjectsReference
      ? new URL(dynamicObjectsReference, window.location.origin + baseUrl).pathname
      : undefined;
    try {
      const dynamicResponse = dynamicObjectsUrl ? await fetch(dynamicObjectsUrl) : undefined;
      const fixedResponse = fixedTagMapUrl ? await fetch(fixedTagMapUrl) : undefined;
      if ((dynamicResponse && !dynamicResponse.ok) || (fixedResponse && !fixedResponse.ok)) {
        dynamicTrackingError = `Dynamic annotations unavailable (${fixedResponse?.status ?? "virtual"}/${dynamicResponse?.status ?? "default"})`;
      } else {
        const validation = validateDynamicAnnotations(
          fixedResponse ? await fixedResponse.json() : undefined,
          dynamicResponse ? await dynamicResponse.json() : createDefaultDynamicRegistry(),
          manifest.schema_version,
        );
        if (validation.status === "verified") {
          fixedTagMap = validation.fixedTagMap;
          dynamicObjectRegistry = validation.registry;
        } else {
          dynamicTrackingError = validation.reason ?? "Dynamic annotations failed validation";
        }
      }
    } catch (error) {
      dynamicTrackingError = error instanceof Error ? error.message : String(error);
    }
  }

  return {
    manifest,
    baseUrl,
    primarySplat,
    primarySplatUrl,
    cameraPresets,
    worldAlignment,
    aprilTagWorldCorners,
    sceneObjects,
    proxyMeshUrl,
    proxyMetadata,
    proxyQuality,
    fixedTagMap,
    dynamicObjectRegistry,
    dynamicTrackingError,
    viewerFromRos,
    dynamicOverlay,
  };
}
