import type { JsonObject } from "./PreviewApiClient";
import {
  deriveFixedTagMapFromAprilTagCorners,
  isDynamicRejection,
  type PoseTransform,
} from "../dynamic/DynamicTrackingContract";
import { aprilTagCornerSets, type AprilTagWorldCorners, type SceneManifest } from "../scene/SceneManifest";
import { validateImmutableCoordinateContract } from "../scene/SceneLoader";
import type { CameraCalibration } from "./CameraCalibration";

export interface AprilTagFamilyContract {
  default_family: string;
  families: Array<{ value: string; label: string }>;
}

export interface TagPose {
  frame: string;
  translation_m: [number, number, number];
  rotation_quaternion_xyzw: [number, number, number, number];
  rotation_rpy_deg: [number, number, number];
  pose_error: number | null;
}

export interface TagDetection {
  family: string;
  tag_id: number;
  hamming: number;
  decision_margin: number;
  center_px: [number, number];
  corners_px: Array<[number, number]>;
  bbox_px: { x: number; y: number; width: number; height: number };
  pose_camera_tag: TagPose | null;
  tracking_state?: "detected" | "enhanced" | "optical_flow";
  tracking_age_frames?: number;
  tracking_feature_count?: number;
  tracking_inlier_count?: number;
  tracking_inlier_ratio?: number;
  tracking_seed_score?: number | null;
}

export interface TrackingDiagnostic {
  track_key: string;
  previous_misses: number;
  decoded_age_ms: number;
  outcome: "pending" | "tracked" | "rejected" | "reanchored_by_decoder";
  failure_stage: string | null;
  failure_reason: string | null;
  [key: string]: unknown;
}

export interface LiveFrameDiagnostics {
  schema_version: "apriltag_live_frame_diagnostics_v1";
  frame_sequence: number;
  timings_ms: Record<string, number>;
  direct_detection_count: number;
  enhanced_detection_count: number;
  optical_flow_detection_count: number;
  track_count_before: number;
  track_count_after: number;
  tracking: TrackingDiagnostic[];
  thresholds: Record<string, number>;
}

export interface LiveDetectionResponse {
  status: string;
  frame: { width: number; height: number };
  detection_count: number;
  detector_detection_count?: number;
  tracked_detection_count?: number;
  detections: TagDetection[];
  processing_ms: number;
  scanned_families: string[];
  diagnostics?: LiveFrameDiagnostics;
}

export interface SeenDetection {
  detection: TagDetection;
  targetDetection: TagDetection;
  cornerVelocityPxMs: Array<[number, number]>;
  measurementCapturedMs: number;
  measurementLatencyMs: number;
  lastSeenMs: number;
  current: boolean;
}

export interface ViewerBridgeTarget {
  scenePackageId: string;
  projectName: string;
  viewerUrl: string;
  viewerFromRos: PoseTransform;
  fixedTags: Array<{ frameId: string; family: string; tagId: number; physicalSideM: number; worldPose: PoseTransform }>;
}

export interface AprilTagLiveCheckOptions {
  integratedViewerMode?: boolean;
  onShowViewer?: () => void;
}

export interface IntegratedViewerSession {
  scenePackageId: string;
  rosbridgeUrl: string;
  fixedTagId: string;
  minimumMargin: string;
  family: string;
  tagSizeM: string;
  processingWidth: string;
  exposureProfile: string;
  intrinsicsMode: string;
  horizontalFov: string;
  fx: string;
  fy: string;
  cx: string;
  cy: string;
  calibration?: CameraCalibration;
  boxMappings: Array<{ tagId: number; childFrame: string }>;
}

export const INTEGRATED_VIEWER_SESSION_KEY = "spatial-3dgs.apriltag-integrated-viewer.v1";

export const OVERLAY_HOLD_MS = 1200;
export const SIDEBAR_RETENTION_MS = 2000;
export const TRACKING_ALPHA = 0.38;
export const VELOCITY_ALPHA = 0.58;
export const MAX_CORNER_SPEED_PX_MS = 2;
export const PREDICTION_TAU_MS = 260;
export const MAX_LATENCY_COMPENSATION_MS = 180;
export const AUTO_EXPOSURE_SETTLE_MS = 450;
export const MOTION_EXPOSURE_SAFE_FLOOR = 156.25;
export const DARK_FRAME_LUMA_THRESHOLD = 4;
export const DARK_FRAME_RECOVERY_COUNT = 3;
export const MAX_DIAGNOSTIC_FRAMES = 2400;

export function stringValue(value: unknown): string | undefined {
  return typeof value === "string" && value.trim() ? value.trim() : undefined;
}

export function scenePackageBaseUrl(scenePackageId: string): string {
  return `/scene_package/runtime/${encodeURIComponent(scenePackageId)}/`;
}

export async function loadViewerBridgeTarget(item: JsonObject): Promise<ViewerBridgeTarget | undefined> {
  const scenePackageId = stringValue(item.scene_package_id);
  const viewerUrl = stringValue(item.viewer_url);
  if (!scenePackageId || !viewerUrl || item.status !== "verified" || item.published !== true) return undefined;
  const baseUrl = scenePackageBaseUrl(scenePackageId);
  const manifestResponse = await fetch(`${baseUrl}scene_manifest.json`, { cache: "no-store" });
  if (!manifestResponse.ok) return undefined;
  const manifest = await manifestResponse.json() as SceneManifest;
  if (!["1.2.0", "1.3.0"].includes(manifest.schema_version) || validateImmutableCoordinateContract(manifest)) return undefined;
  const coordinate = manifest.coordinate_system.T_viewer_ros;
  const cornersReference = manifest.annotations.apriltag_corners_world;
  if (!coordinate || !cornersReference) return undefined;
  const cornersUrl = new URL(cornersReference, window.location.origin + baseUrl).pathname;
  const cornersResponse = await fetch(cornersUrl, { cache: "no-store" });
  if (!cornersResponse.ok) return undefined;
  const corners = await cornersResponse.json() as AprilTagWorldCorners;
  const fixedTagMap = deriveFixedTagMapFromAprilTagCorners(corners, true);
  if (isDynamicRejection(fixedTagMap)) return undefined;
  const fixedTags = aprilTagCornerSets(corners).map((tag) => ({
    frameId: `apriltag_${tag.family}_${tag.id}`,
    family: tag.family,
    tagId: tag.id,
    physicalSideM: tag.physical_side_m,
    worldPose: fixedTagMap.frames[`apriltag_${tag.family}_${tag.id}`],
  })).filter((tag) => Number.isFinite(tag.physicalSideM) && tag.physicalSideM > 0 && Boolean(tag.worldPose));
  if (fixedTags.length === 0) return undefined;
  return {
    scenePackageId,
    projectName: stringValue(item.project_name) ?? stringValue(manifest.project_name) ?? scenePackageId,
    viewerUrl,
    viewerFromRos: {
      translation_m: [...coordinate.translation],
      rotation_xyzw: [...coordinate.rotation_xyzw],
    },
    fixedTags,
  };
}

export async function loadViewerBridgeTargets(packages: JsonObject[]): Promise<ViewerBridgeTarget[]> {
  const loaded = await Promise.all(packages.map(async (item) => {
    try {
      return await loadViewerBridgeTarget(item);
    } catch {
      return undefined;
    }
  }));
  return loaded.filter((target): target is ViewerBridgeTarget => Boolean(target)).sort((left, right) =>
    right.projectName.localeCompare(left.projectName, "ja") || right.scenePackageId.localeCompare(left.scenePackageId, "ja")
  );
}

export function numberInput(wrapper: HTMLElement): HTMLInputElement {
  const input = wrapper.querySelector<HTMLInputElement>("input");
  if (!input) throw new Error("apriltag_live_input_missing");
  return input;
}

export function selectInput(wrapper: HTMLElement): HTMLSelectElement {
  const select = wrapper.querySelector<HTMLSelectElement>("select");
  if (!select) throw new Error("apriltag_live_select_missing");
  return select;
}

export function finiteNumber(input: HTMLInputElement, label: string, positive = false): number {
  const value = Number(input.value);
  if (!Number.isFinite(value) || (positive && value <= 0)) throw new Error(`${label}を正しく入力してください。`);
  return value;
}

export function formatVector(values: readonly number[], digits: number, unit = ""): string {
  return values.map((value) => `${value.toFixed(digits)}${unit}`).join(" / ");
}

export function blend(previous: number, current: number): number {
  const alpha = Math.min(0.9, TRACKING_ALPHA + Math.abs(current - previous) / 45);
  return previous + (current - previous) * alpha;
}

export function blendAngle(previous: number, current: number): number {
  const delta = ((current - previous + 540) % 360) - 180;
  return previous + delta * TRACKING_ALPHA;
}

export function blendQuaternion(previous: readonly number[], current: readonly number[]): [number, number, number, number] {
  const dot = previous.reduce((sum, value, index) => sum + value * current[index], 0);
  const aligned = dot < 0 ? current.map((value) => -value) : [...current];
  const mixed = previous.map((value, index) => blend(value, aligned[index]));
  const norm = Math.hypot(...mixed);
  if (!Number.isFinite(norm) || norm <= 1e-12) return [...current] as [number, number, number, number];
  return mixed.map((value) => value / norm) as [number, number, number, number];
}

export function smoothDetection(previous: TagDetection, current: TagDetection): TagDetection {
  const previousPose = previous.pose_camera_tag;
  const currentPose = current.pose_camera_tag;
  const pose = previousPose && currentPose ? {
    ...currentPose,
    translation_m: currentPose.translation_m.map((value, index) => blend(previousPose.translation_m[index], value)) as [number, number, number],
    rotation_quaternion_xyzw: blendQuaternion(previousPose.rotation_quaternion_xyzw, currentPose.rotation_quaternion_xyzw),
    rotation_rpy_deg: currentPose.rotation_rpy_deg.map((value, index) => blendAngle(previousPose.rotation_rpy_deg[index], value)) as [number, number, number],
  } : currentPose;
  return {
    ...current,
    center_px: current.center_px.map((value, index) => blend(previous.center_px[index], value)) as [number, number],
    corners_px: current.corners_px.map((point, index) => [
      blend(previous.corners_px[index]?.[0] ?? point[0], point[0]),
      blend(previous.corners_px[index]?.[1] ?? point[1], point[1]),
    ]),
    bbox_px: {
      x: blend(previous.bbox_px.x, current.bbox_px.x),
      y: blend(previous.bbox_px.y, current.bbox_px.y),
      width: blend(previous.bbox_px.width, current.bbox_px.width),
      height: blend(previous.bbox_px.height, current.bbox_px.height),
    },
    pose_camera_tag: pose,
  };
}

export function boundedVelocity(previous: number, measured: number): number {
  const smoothed = previous + (measured - previous) * VELOCITY_ALPHA;
  return Math.max(-MAX_CORNER_SPEED_PX_MS, Math.min(MAX_CORNER_SPEED_PX_MS, smoothed));
}

export function predictDetection(item: SeenDetection, now: number): TagDetection {
  const renderAgeMs = Math.max(0, now - item.lastSeenMs);
  // The detector response describes the frame captured before JPEG encoding
  // and the API round trip. Account for that source-frame age so the overlay
  // does not start interpolating from an already stale position. Keep the
  // horizon bounded because a stalled request must not extrapolate forever.
  const ageMs = Math.min(
    MAX_LATENCY_COMPENSATION_MS,
    item.measurementLatencyMs + renderAgeMs,
  );
  // Constant velocity is useful between detector responses, but an
  // exponentially bounded horizon prevents a stale velocity from flying the
  // overlay off-screen when the tag suddenly stops or becomes occluded.
  const predictionMs = PREDICTION_TAU_MS * (1 - Math.exp(-ageMs / PREDICTION_TAU_MS));
  const corners = item.targetDetection.corners_px.map((point, index) => [
    point[0] + (item.cornerVelocityPxMs[index]?.[0] ?? 0) * predictionMs,
    point[1] + (item.cornerVelocityPxMs[index]?.[1] ?? 0) * predictionMs,
  ] as [number, number]);
  const xs = corners.map((point) => point[0]);
  const ys = corners.map((point) => point[1]);
  const minimumX = Math.min(...xs);
  const maximumX = Math.max(...xs);
  const minimumY = Math.min(...ys);
  const maximumY = Math.max(...ys);
  return {
    ...item.targetDetection,
    center_px: [xs.reduce((sum, value) => sum + value, 0) / 4, ys.reduce((sum, value) => sum + value, 0) / 4],
    corners_px: corners,
    bbox_px: { x: minimumX, y: minimumY, width: maximumX - minimumX, height: maximumY - minimumY },
  };
}

export const CAMERA_FRAME_JPEG_QUALITY = 0.95;

export function canvasDataUrlBlob(canvas: HTMLCanvasElement): Blob {
  const dataUrl = canvas.toDataURL("image/jpeg", CAMERA_FRAME_JPEG_QUALITY);
  const separator = dataUrl.indexOf(",");
  if (separator < 0) throw new Error("camera_frame_encode_failed");
  const metadata = dataUrl.slice(0, separator);
  const encoded = dataUrl.slice(separator + 1);
  const binary = atob(encoded);
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) bytes[index] = binary.charCodeAt(index);
  return new Blob([bytes], { type: metadata.match(/^data:([^;]+)/)?.[1] ?? "image/jpeg" });
}

export function canvasBlob(canvas: HTMLCanvasElement): Promise<Blob> {
  // Chromium can defer the callback of canvas.toBlob() to roughly one second
  // after a document has remained hidden. That exceeds the Scene Package's
  // 500 ms freshness timeout even when detection itself is fast. Keep the
  // normal non-blocking encoder while visible, and use the synchronous canvas
  // encoder only for the background sender path.
  if (document.visibilityState === "hidden") {
    try {
      return Promise.resolve(canvasDataUrlBlob(canvas));
    } catch (error) {
      return Promise.reject(error);
    }
  }
  return new Promise((resolve, reject) => {
    canvas.toBlob(
      (blob) => blob ? resolve(blob) : reject(new Error("camera_frame_encode_failed")),
      "image/jpeg",
      CAMERA_FRAME_JPEG_QUALITY,
    );
  });
}

export function meanCanvasLuminance(source: HTMLCanvasElement, probe: HTMLCanvasElement): number {
  probe.width = 32;
  probe.height = 18;
  const context = probe.getContext("2d", { alpha: false, willReadFrequently: true });
  if (!context) return 255;
  context.drawImage(source, 0, 0, probe.width, probe.height);
  const pixels = context.getImageData(0, 0, probe.width, probe.height).data;
  let total = 0;
  for (let index = 0; index < pixels.length; index += 4) {
    total += pixels[index] * 0.2126 + pixels[index + 1] * 0.7152 + pixels[index + 2] * 0.0722;
  }
  return total / (pixels.length / 4);
}
