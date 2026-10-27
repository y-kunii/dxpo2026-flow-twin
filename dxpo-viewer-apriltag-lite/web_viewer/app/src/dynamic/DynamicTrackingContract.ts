export type Vec3 = [number, number, number];
export type QuaternionXYZW = [number, number, number, number];

/** Immutable Scene Package and ROS wire quaternions are x,y,z,w unit values.
 * Keep this shared with SceneLoader; values outside this tolerance are a
 * contract error and must not be silently normalized at an input boundary. */
export const QUATERNION_UNIT_NORM_TOLERANCE = 1e-6;

/**
 * Canonical cardboard dimensions.  The semantic names avoid the historical
 * ambiguity of a three-item array: Three.js BoxGeometry is constructed as
 * (width, height, depth), while this contract stores width, depth, height.
 */
export interface BoxDimensionsM {
  width_m: number;
  depth_m: number;
  height_m: number;
}

/** Physical face on which the tracking AprilTag is centred. */
export type BoxTagFace = "top" | "front" | "right" | "left" | "back" | "bottom";

export const BOX_TAG_FACES: readonly BoxTagFace[] = [
  "top",
  "front",
  "right",
  "left",
  "back",
  "bottom",
];

export type DynamicRejectionReason =
  | "missing_fixed_tag_map"
  | "invalid_fixed_tag_geometry"
  | "unknown_fixed_tag_frame"
  | "unknown_dynamic_object"
  | "dynamic_object_limit_exceeded"
  | "invalid_child_frame"
  | "invalid_message_shape"
  | "non_finite_translation"
  | "non_finite_quaternion"
  | "zero_quaternion"
  | "non_unit_quaternion"
  | "stale_timestamp"
  | "timestamp_regression"
  | "duplicate_timestamp"
  | "invalid_receive_time"
  | "translation_jump_exceeded"
  | "rotation_jump_exceeded";

export interface PoseTransform {
  translation_m: Vec3;
  rotation_xyzw: QuaternionXYZW;
}

export interface FixedTagMap {
  schema_version: "1.0.0";
  coordinate_frame: "gs_world";
  unit: "meter";
  rotation_order: "xyzw";
  /** Independent evidence for the AprilTag triangulation. The scale solved
   * from these corners is not accepted as verification of the same corners.
   * Omitted only by a runtime-only virtual anchor. Package annotations must
   * carry it and are rejected by validateDynamicAnnotations when absent. */
  metric_quality?: {
    status: "verified";
    quality_gate: {
      status: "verified";
      thresholds?: unknown;
    };
  };
  frames: Record<string, PoseTransform>;
}

export interface DynamicObjectConfig {
  id: string;
  /**
   * Optional camera-side documentation for a package-specific override.
   * The Viewer never derives a pose from this field: the ROS publisher has
   * already converted its physical tag pose to `T_fixedTag_boxTracking`.
   */
  box_tag_frame?: string;
  model: {
    format: "glb";
    uri: string;
    sha256: string;
  } | {
    format: "cube";
    dimensions_m: BoxDimensionsM;
    color_hex?: string;
  };
  box_tag_to_object: PoseTransform;
  object_to_render: PoseTransform;
  max_translation_jump_m: number;
  max_rotation_jump_deg: number;
}

/** The Metric Scene `gs_world` name denotes the package's formal meter world:
 * +Y up, X/Z horizontal.  `render_from_world` is identity for the current
 * Spark/Three.js Viewer, but remains explicit in pose composition. */
export const METRIC_WORLD_COORDINATE_FRAME = "gs_world";
export const IDENTITY_POSE: PoseTransform = {
  translation_m: [0, 0, 0],
  rotation_xyzw: [0, 0, 0, 1],
};

/**
 * Change the basis of a relative ROS pose into the Scene Package's Viewer
 * basis. `viewerFromRos` is T_viewer_ros and applies to points as
 * p_viewer = R_viewer_ros * p_ros + t_viewer_ros.  A relation between two ROS
 * frames must be conjugated, not merely pre-multiplied:
 * T_viewerParent_viewerChild = T_viewer_ros * T_rosParent_rosChild * inverse(T_viewer_ros).
 */
export function convertRosRelativePoseToViewer(
  rosParentToChild: PoseTransform,
  viewerFromRos: PoseTransform,
): PoseTransform {
  return composePose(composePose(viewerFromRos, rosParentToChild), invertPose(viewerFromRos));
}

/**
 * Encode a Viewer-basis relative pose on the ROS wire.  This is the exact
 * inverse of `convertRosRelativePoseToViewer` and is used only by adapters
 * that produce a pose in the package's Viewer/tag basis before publishing.
 * Keeping the inverse here prevents camera-side test tools from guessing or
 * duplicating the immutable Scene Package basis conversion.
 */
export function convertViewerRelativePoseToRos(
  viewerParentToChild: PoseTransform,
  viewerFromRos: PoseTransform,
): PoseTransform {
  const rosFromViewer = invertPose(viewerFromRos);
  return composePose(composePose(rosFromViewer, viewerParentToChild), viewerFromRos);
}

/** A resource bound, not a list of pre-registered physical boxes. */
export const MAX_RUNTIME_DYNAMIC_BOXES = 64;

export const DEFAULT_DYNAMIC_BOX_DIMENSIONS_M: BoxDimensionsM = {
  width_m: 0.5,
  depth_m: 0.4,
  height_m: 0.3,
};

/**
 * Dynamic objects are admitted by their ROS child frame identity.  Both
 * `box_01` and `box_1` are valid; zero is deliberately invalid so that a
 * malformed default cannot silently create an overlay.  The limit applies to
 * concurrent live objects, not to the numeric suffix.
 */
export function isRuntimeDynamicBoxId(value: string): boolean {
  return /^box_0*[1-9][0-9]*$/.test(value) && value.length <= 64;
}

function runtimeBoxColor(objectId: string): string {
  const palette = ["#45a9e8", "#d58a46", "#8fcf5b", "#b895ff", "#f09a9a", "#4fc9b7"];
  let hash = 0;
  for (const character of objectId) hash = (hash * 31 + character.charCodeAt(0)) >>> 0;
  return palette[hash % palette.length];
}

/**
 * Generic runtime fallback for a valid ROS `box_<positive integer>` child
 * frame that has no Scene Package visual override.  It intentionally carries
 * no physical AprilTag mapping: that mapping belongs exclusively to the
 * camera-side node and its per-box configuration.
 */
export function createRuntimeDynamicBoxConfig(objectId: string): DynamicObjectConfig {
  if (!isRuntimeDynamicBoxId(objectId)) {
    throw new Error(`Invalid runtime dynamic object ID: ${objectId}`);
  }
  return {
    id: objectId,
    model: {
      format: "cube",
      dimensions_m: { ...DEFAULT_DYNAMIC_BOX_DIMENSIONS_M },
      color_hex: runtimeBoxColor(objectId),
    },
    box_tag_to_object: IDENTITY_POSE,
    object_to_render: IDENTITY_POSE,
    max_translation_jump_m: 0.5,
    max_rotation_jump_deg: 45,
  };
}

export interface DynamicObjectRegistry {
  schema_version: "1.0.0";
  coordinate_frame: "gs_world";
  unit: "meter";
  rotation_order: "xyzw";
  objects: DynamicObjectConfig[];
}

export interface TransformStampedMessage {
  header: {
    stamp: { sec: number; nanosec: number };
    frame_id: string;
  };
  child_frame_id: string;
  transform: PoseTransform;
}

export interface ResolvedDynamicPose extends PoseTransform {
  object_id: string;
  fixed_tag_frame: string;
  source_stamp_ms: number;
}

export interface DynamicRejection {
  status: "rejected";
  reason: DynamicRejectionReason;
  detail: string;
}

export interface DynamicResolution {
  status: "accepted";
  pose: ResolvedDynamicPose;
}

export type DynamicTrackingStateName = "disconnected" | "waiting" | "tracking" | "stale" | "lost" | "invalid";

/** Runtime-only state, scoped by child_frame_id in the collection map.
 * `last_source_stamp_ms` is the ROS stamp ordering watermark and
 * `last_received_monotonic_ms` is the WebSocket freshness clock. They must
 * never be compared directly. */
export interface DynamicTrackingRuntimeState {
  connection_state: "disconnected" | "connecting" | "receiving" | "stale" | "rejected";
  tracking_state: DynamicTrackingStateName;
  last_pose?: ResolvedDynamicPose;
  last_source_stamp_ms?: number;
  last_received_monotonic_ms?: number;
  active_fixed_tag_frame?: string;
  dynamic_object_id?: string;
  accepted_message_count: number;
  rejected_message_count: number;
  latest_rejection_reason?: DynamicRejectionReason;
  source_mode: "rosbridge" | "debug_virtual_anchor";
  tracking_alignment: "verified_fixed_tag_map" | "uncalibrated";
}

/** Independent runtime state for every accepted `box_<N>` child frame.
 *
 * Scene Package entries may provide overrides, but the ROS stream can admit
 * new well-formed box IDs at runtime. Source stamps, jump rejection, and
 * freshness must therefore be scoped to the child frame/object, never shared
 * by the whole rosbridge subscription. */
export interface DynamicTrackingCollectionState {
  object_states: Record<string, DynamicTrackingRuntimeState>;
  /** A malformed or disallowed child-frame message has no safe object state
   * to update. Keep its diagnostic separately without changing tracked boxes. */
  latest_rejection?: DynamicRejection;
}

export interface DynamicTrackingMessageResult {
  state: DynamicTrackingRuntimeState;
  resolution?: DynamicResolution;
  rejection?: DynamicRejection;
}

export interface DynamicTrackingCollectionMessageResult {
  state: DynamicTrackingCollectionState;
  resolution?: DynamicResolution;
  rejection?: DynamicRejection;
}

export interface DynamicAnnotationValidation {
  status: "verified" | "rejected";
  fixedTagMap?: FixedTagMap;
  registry?: DynamicObjectRegistry;
  reason?: string;
}

/**
 * Runtime fallback for packages with no visual overrides.  This deliberately
 * has no objects: physical boxes are created only after an accepted ROS
 * TransformStamped arrives.  The default cube is provided by
 * `createRuntimeDynamicBoxConfig` at that point.
 */
export function createDefaultDynamicRegistry(): DynamicObjectRegistry {
  return {
    schema_version: "1.0.0",
    coordinate_frame: "gs_world",
    unit: "meter",
    rotation_order: "xyzw",
    objects: [],
  };
}

/**
 * Build T_gs_fixedTag from the verified AprilTag corner annotation already
 * carried by a Metric Scene.  Corner order is the detector's AprilTag order:
 * top-left, top-right, bottom-right, bottom-left when viewed from the tag
 * front.  The resulting basis is therefore +X right, +Y down and +Z into the
 * tag plane, expressed in gs_world.
 *
 * This is deliberately a pure, fail-closed helper.  It does not infer a tag
 * from the rendered splat: only the reconstruction's triangulated corners
 * can produce a map.
 */
export function deriveFixedTagMapFromAprilTagCorners(
  value: unknown,
  metricQualityVerified = false,
): FixedTagMap | DynamicRejection {
  if (!metricQualityVerified) {
    return {
      status: "rejected",
      reason: "invalid_fixed_tag_geometry",
      detail: "AprilTag metric quality is not independently verified.",
    };
  }
  if (isRecord(value) && Array.isArray(value.tags) && value.tags.length > 0) {
    const frames: FixedTagMap["frames"] = {};
    for (const tag of value.tags) {
      const derived = deriveFixedTagMapFromAprilTagCorners(tag, true);
      if (isDynamicRejection(derived)) return derived;
      for (const [frameId, pose] of Object.entries(derived.frames)) {
        if (frames[frameId]) {
          return {
            status: "rejected",
            reason: "invalid_fixed_tag_geometry",
            detail: `Duplicate fixed AprilTag frame: ${frameId}`,
          };
        }
        frames[frameId] = pose;
      }
    }
    if (Object.keys(frames).length === 0) {
      return {
        status: "rejected",
        reason: "invalid_fixed_tag_geometry",
        detail: "No fixed AprilTag corners are available.",
      };
    }
    return {
      schema_version: "1.0.0",
      coordinate_frame: "gs_world",
      unit: "meter",
      rotation_order: "xyzw",
      metric_quality: {
        status: "verified",
        quality_gate: { status: "verified" },
      },
      frames,
    };
  }
  if (!isRecord(value) || typeof value.family !== "string" || !value.family ||
      !/^[A-Za-z0-9_-]+$/.test(value.family) || !Number.isInteger(value.id) || (value.id as number) < 0 ||
      !Array.isArray(value.corners_world_m) || value.corners_world_m.length !== 4) {
    return {
      status: "rejected",
      reason: "invalid_fixed_tag_geometry",
      detail: "AprilTag family, id, or four corners are invalid.",
    };
  }

  const corners = new Map<number, Vec3>();
  for (const candidate of value.corners_world_m) {
    if (!isRecord(candidate) || !Number.isInteger(candidate.corner_index) ||
        !isFiniteArray(candidate.xyz_world_m, 3) || corners.has(candidate.corner_index as number)) {
      return {
        status: "rejected",
        reason: "invalid_fixed_tag_geometry",
        detail: "AprilTag corners must be unique finite world points.",
      };
    }
    corners.set(candidate.corner_index as number, [...candidate.xyz_world_m] as Vec3);
  }
  if (![0, 1, 2, 3].every((index) => corners.has(index))) {
    return {
      status: "rejected",
      reason: "invalid_fixed_tag_geometry",
      detail: "AprilTag corner indices must be 0 through 3.",
    };
  }

  const p0 = corners.get(0)!;
  const p1 = corners.get(1)!;
  const p2 = corners.get(2)!;
  const p3 = corners.get(3)!;
  const subtract = (left: Vec3, right: Vec3): Vec3 => [
    left[0] - right[0], left[1] - right[1], left[2] - right[2],
  ];
  const add = (left: Vec3, right: Vec3): Vec3 => [
    left[0] + right[0], left[1] + right[1], left[2] + right[2],
  ];
  const scale = (vector: Vec3, factor: number): Vec3 => [
    vector[0] * factor, vector[1] * factor, vector[2] * factor,
  ];
  const cross = (left: Vec3, right: Vec3): Vec3 => [
    left[1] * right[2] - left[2] * right[1],
    left[2] * right[0] - left[0] * right[2],
    left[0] * right[1] - left[1] * right[0],
  ];
  const normalize = (vector: Vec3): Vec3 | undefined => {
    const magnitude = Math.hypot(...vector);
    return Number.isFinite(magnitude) && magnitude > 1e-9 ? scale(vector, 1 / magnitude) : undefined;
  };

  const xAxis = normalize(scale(add(subtract(p1, p0), subtract(p2, p3)), 0.5));
  const yApproximation = normalize(scale(add(subtract(p3, p0), subtract(p2, p1)), 0.5));
  const zAxis = xAxis && yApproximation ? normalize(cross(xAxis, yApproximation)) : undefined;
  const yAxis = zAxis && xAxis ? normalize(cross(zAxis, xAxis)) : undefined;
  if (!xAxis || !yAxis || !zAxis) {
    return {
      status: "rejected",
      reason: "invalid_fixed_tag_geometry",
      detail: "AprilTag corners are degenerate or collinear.",
    };
  }

  // The axes are the columns of tag-to-gs_world rotation.
  const m00 = xAxis[0], m01 = yAxis[0], m02 = zAxis[0];
  const m10 = xAxis[1], m11 = yAxis[1], m12 = zAxis[1];
  const m20 = xAxis[2], m21 = yAxis[2], m22 = zAxis[2];
  const trace = m00 + m11 + m22;
  let quaternion: QuaternionXYZW;
  if (trace > 0) {
    const s = 0.5 / Math.sqrt(trace + 1);
    quaternion = [(m21 - m12) * s, (m02 - m20) * s, (m10 - m01) * s, 0.25 / s];
  } else if (m00 > m11 && m00 > m22) {
    const s = 2 * Math.sqrt(1 + m00 - m11 - m22);
    quaternion = [0.25 * s, (m01 + m10) / s, (m02 + m20) / s, (m21 - m12) / s];
  } else if (m11 > m22) {
    const s = 2 * Math.sqrt(1 + m11 - m00 - m22);
    quaternion = [(m01 + m10) / s, 0.25 * s, (m12 + m21) / s, (m02 - m20) / s];
  } else {
    const s = 2 * Math.sqrt(1 + m22 - m00 - m11);
    quaternion = [(m02 + m20) / s, (m12 + m21) / s, 0.25 * s, (m10 - m01) / s];
  }
  const normalized = normalizeQuaternion(quaternion);
  if (isDynamicRejection(normalized)) {
    return {
      status: "rejected",
      reason: "invalid_fixed_tag_geometry",
      detail: "AprilTag basis did not produce a finite quaternion.",
    };
  }
  const center = scale(add(add(p0, p1), add(p2, p3)), 0.25);
  const frameId = `apriltag_${value.family}_${value.id}`;
  return {
    schema_version: "1.0.0",
    coordinate_frame: "gs_world",
    unit: "meter",
    rotation_order: "xyzw",
    metric_quality: {
      status: "verified",
      quality_gate: { status: "verified" },
    },
    frames: {
      [frameId]: {
        translation_m: center,
        rotation_xyzw: normalized,
      },
    },
  };
}

export function isDynamicRejection(value: unknown): value is DynamicRejection {
  return Boolean(value && typeof value === "object" && (value as { status?: unknown }).status === "rejected");
}

function isFiniteArray(value: unknown, length: number): value is number[] {
  return Array.isArray(value) && value.length === length && value.every((item) => typeof item === "number" && Number.isFinite(item));
}

function isPositiveFinite(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value) && value > 0;
}

/** Normalize the only legacy representation at the Scene Package boundary.
 * Old packages used `[width, depth, height]`; all verified runtime objects
 * leave this function with named dimensions. */
export function normalizeBoxDimensions(value: unknown): BoxDimensionsM | undefined {
  if (isRecord(value) && isPositiveFinite(value.width_m) && isPositiveFinite(value.depth_m) && isPositiveFinite(value.height_m)) {
    return { width_m: value.width_m, depth_m: value.depth_m, height_m: value.height_m };
  }
  if (isFiniteArray(value, 3) && value.every((dimension) => dimension > 0)) {
    return { width_m: value[0], depth_m: value[1], height_m: value[2] };
  }
  return undefined;
}

/** Exact Three.js BoxGeometry argument order: width, height, depth. */
export function boxGeometryDimensions(dimensions: BoxDimensionsM): Vec3 {
  return [dimensions.width_m, dimensions.height_m, dimensions.depth_m];
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value && typeof value === "object" && !Array.isArray(value));
}

function hasPackageHeader(value: unknown): boolean {
  return isRecord(value) && value.schema_version === "1.0.0" &&
    value.coordinate_frame === "gs_world" && value.unit === "meter" &&
    value.rotation_order === "xyzw";
}

function normalizedPose(value: unknown): PoseTransform | undefined {
  const pose = normalizePose(value);
  return isDynamicRejection(pose) ? undefined : pose;
}

/** Runtime fail-closed validation for optional dynamic annotations.
 *
 * Python validates packages before publication, but the Viewer must not trust
 * a hand-edited manifest or a stale generated file. Invalid annotations are
 * rejected without affecting static SPZ/Proxy loading.
 */
export function validateDynamicAnnotations(
  fixedValue: unknown | undefined,
  registryValue: unknown,
  packageSchemaVersion?: string,
): DynamicAnnotationValidation {
  const frames: Record<string, PoseTransform> = {};
  if (fixedValue !== undefined) {
    if (!isRecord(fixedValue) || !hasPackageHeader(fixedValue) || !isRecord(fixedValue.frames)) {
      return { status: "rejected", reason: "fixed_tag_map header or frames are invalid" };
    }
    const fixed = fixedValue as Record<string, unknown>;
    const metricQuality = fixed.metric_quality;
    if (!isRecord(metricQuality) || metricQuality.status !== "verified" ||
        !isRecord(metricQuality.quality_gate) || metricQuality.quality_gate.status !== "verified") {
      return { status: "rejected", reason: "fixed_tag_map metric quality is not independently verified" };
    }
    for (const [frame, value] of Object.entries(fixed.frames as Record<string, unknown>)) {
      if (!frame || !normalizedPose(value)) {
        return { status: "rejected", reason: `invalid fixed tag frame: ${frame}` };
      }
      frames[frame] = normalizedPose(value)!;
    }
    if (!Object.keys(frames).length) {
      return { status: "rejected", reason: "fixed_tag_map has no frames" };
    }
  }

  if (!hasPackageHeader(registryValue) || !isRecord(registryValue) || !Array.isArray(registryValue.objects)) {
    return { status: "rejected", reason: "dynamic_objects header or objects are invalid" };
  }
  const registry = registryValue as Record<string, unknown>;
  const objects: DynamicObjectConfig[] = [];
  const ids = new Set<string>();
  const immutableTimeoutContract = packageSchemaVersion === "1.2.0" || packageSchemaVersion === "1.3.0";
  for (const candidate of registry.objects as unknown[]) {
    const hasObjectTimeout = isRecord(candidate) && Object.prototype.hasOwnProperty.call(candidate, "timeout_ms");
    if (immutableTimeoutContract && hasObjectTimeout) {
      return { status: "rejected", reason: `dynamic object timeout_ms is not allowed in immutable packages: ${String(candidate.id)}` };
    }
    if (!isRecord(candidate) || typeof candidate.id !== "string" || !isRuntimeDynamicBoxId(candidate.id) || ids.has(candidate.id) ||
        (candidate.box_tag_frame !== undefined && (typeof candidate.box_tag_frame !== "string" || !candidate.box_tag_frame)) ||
        (candidate.model !== undefined && (!isRecord(candidate.model) ||
          (candidate.model.format === "glb" && (typeof candidate.model.uri !== "string" || !candidate.model.uri ||
            typeof candidate.model.sha256 !== "string" || !/^[a-fA-F0-9]{64}$/.test(candidate.model.sha256))) ||
          (candidate.model.format === "cube" && !normalizeBoxDimensions(candidate.model.dimensions_m)) ||
          (candidate.model.format !== "glb" && candidate.model.format !== "cube"))) ||
        (candidate.box_tag_to_object !== undefined && !normalizedPose(candidate.box_tag_to_object)) ||
        (candidate.object_to_render !== undefined && !normalizedPose(candidate.object_to_render)) ||
        (!immutableTimeoutContract && candidate.timeout_ms !== undefined &&
          (typeof candidate.timeout_ms !== "number" || !Number.isFinite(candidate.timeout_ms) || candidate.timeout_ms <= 0)) ||
        (candidate.max_translation_jump_m !== undefined && (typeof candidate.max_translation_jump_m !== "number" || !Number.isFinite(candidate.max_translation_jump_m) || candidate.max_translation_jump_m <= 0)) ||
        (candidate.max_rotation_jump_deg !== undefined && (typeof candidate.max_rotation_jump_deg !== "number" || !Number.isFinite(candidate.max_rotation_jump_deg) || candidate.max_rotation_jump_deg <= 0))) {
      return { status: "rejected", reason: `invalid dynamic object: ${String(isRecord(candidate) ? candidate.id : "unknown")}` };
    }
    if (typeof candidate.box_tag_frame === "string" && Object.keys(frames).length && frames[candidate.box_tag_frame]) {
      return { status: "rejected", reason: `dynamic object frame conflicts with fixed tag frame: ${candidate.box_tag_frame}` };
    }
    const fallback = createRuntimeDynamicBoxConfig(candidate.id);
    const model = candidate.model as Record<string, unknown> | undefined;
    ids.add(candidate.id);
    objects.push({
      id: candidate.id,
      ...(typeof candidate.box_tag_frame === "string" ? { box_tag_frame: candidate.box_tag_frame } : {}),
      model: model
        ? {
          ...(model.format === "glb"
          ? { format: "glb" as const, uri: model.uri as string, sha256: (model.sha256 as string).toLowerCase() }
          : {
              format: "cube" as const,
              dimensions_m: normalizeBoxDimensions(model.dimensions_m)!,
              ...(typeof model.color_hex === "string" ? { color_hex: model.color_hex } : {}),
            }),
        }
        : fallback.model,
      box_tag_to_object: candidate.box_tag_to_object === undefined
        ? fallback.box_tag_to_object
        : normalizedPose(candidate.box_tag_to_object)!,
      object_to_render: candidate.object_to_render === undefined
        ? fallback.object_to_render
        : normalizedPose(candidate.object_to_render)!,
      max_translation_jump_m: typeof candidate.max_translation_jump_m === "number"
        ? candidate.max_translation_jump_m
        : fallback.max_translation_jump_m,
      max_rotation_jump_deg: typeof candidate.max_rotation_jump_deg === "number"
        ? candidate.max_rotation_jump_deg
        : fallback.max_rotation_jump_deg,
    });
  }
  return {
    status: "verified",
    ...(Object.keys(frames).length ? {
      fixedTagMap: {
        schema_version: "1.0.0" as const,
        coordinate_frame: "gs_world" as const,
        unit: "meter" as const,
        rotation_order: "xyzw" as const,
        metric_quality: {
          status: "verified" as const,
          quality_gate: { status: "verified" as const },
        },
        frames,
      },
    } : {}),
    registry: {
      schema_version: "1.0.0",
      coordinate_frame: "gs_world",
      unit: "meter",
      rotation_order: "xyzw",
      objects,
    },
  };
}

export function normalizeQuaternion(value: unknown): QuaternionXYZW | DynamicRejection {
  if (!isFiniteArray(value, 4)) {
    return { status: "rejected", reason: "non_finite_quaternion", detail: "Quaternion must contain four finite numbers." };
  }
  const norm = Math.hypot(...value);
  if (norm <= 1e-12) {
    return { status: "rejected", reason: "zero_quaternion", detail: "Quaternion norm must be greater than zero." };
  }
  if (Math.abs(norm - 1) > QUATERNION_UNIT_NORM_TOLERANCE) {
    return {
      status: "rejected",
      reason: "non_unit_quaternion",
      detail: `Quaternion must be normalized within ${QUATERNION_UNIT_NORM_TOLERANCE}.`,
    };
  }
  return [value[0], value[1], value[2], value[3]];
}

/** Normalize values calculated locally from already validated rotations.
 * This is deliberately not used at package or ROS message boundaries. */
function normalizeComputedQuaternion(value: QuaternionXYZW): QuaternionXYZW {
  const norm = Math.hypot(...value);
  if (!Number.isFinite(norm) || norm <= 1e-12) {
    throw new Error("Computed quaternion is non-finite or zero.");
  }
  return [value[0] / norm, value[1] / norm, value[2] / norm, value[3] / norm];
}

function normalizePose(value: unknown): PoseTransform | DynamicRejection {
  if (!value || typeof value !== "object") {
    return { status: "rejected", reason: "invalid_message_shape", detail: "Pose must be an object." };
  }
  const candidate = value as Partial<PoseTransform>;
  if (!isFiniteArray(candidate.translation_m, 3)) {
    return { status: "rejected", reason: "non_finite_translation", detail: "Translation must contain three finite numbers." };
  }
  const rotation = normalizeQuaternion(candidate.rotation_xyzw);
  if (isDynamicRejection(rotation)) return rotation;
  return { translation_m: [...candidate.translation_m] as Vec3, rotation_xyzw: rotation };
}

/** Convert a native ROS `geometry_msgs/msg/Transform` into the Viewer-internal
 * meter / xyzw representation. Real ROS publishers never need to know the
 * internal array form retained for package and virtual-test helpers. */
function normalizeRosTransform(value: unknown): PoseTransform | DynamicRejection {
  if (isRecord(value) && ("translation_m" in value || "rotation_xyzw" in value)) return normalizePose(value);
  if (!isRecord(value) || !isRecord(value.translation) || !isRecord(value.rotation)) {
    return { status: "rejected", reason: "invalid_message_shape", detail: "Transform must use ROS translation and rotation objects." };
  }
  const translation = value.translation;
  const rotation = value.rotation;
  return normalizePose({
    translation_m: [translation.x, translation.y, translation.z],
    rotation_xyzw: [rotation.x, rotation.y, rotation.z, rotation.w],
  });
}

function normalizeRosTime(value: unknown): { sec: number; nanosec: number } | undefined {
  if (!isRecord(value)) return undefined;
  // ROS 2 uses sec/nanosec. Accept the old ROS 1 rosbridge aliases only at
  // the receive boundary so mixed test infrastructure cannot alter the
  // canonical ROS 2 publisher contract.
  const sec = typeof value.sec === "number" ? value.sec : value.secs;
  const nanosec = typeof value.nanosec === "number" ? value.nanosec : value.nsecs;
  if (typeof sec !== "number" || typeof nanosec !== "number" ||
      !Number.isInteger(sec) || sec < 0 || !Number.isInteger(nanosec) || nanosec < 0 || nanosec >= 1_000_000_000) return undefined;
  return { sec, nanosec };
}

function multiplyQuaternion(a: QuaternionXYZW, b: QuaternionXYZW): QuaternionXYZW {
  const [ax, ay, az, aw] = a;
  const [bx, by, bz, bw] = b;
  return [
    aw * bx + ax * bw + ay * bz - az * by,
    aw * by - ax * bz + ay * bw + az * bx,
    aw * bz + ax * by - ay * bx + az * bw,
    aw * bw - ax * bx - ay * by - az * bz,
  ];
}

function rotateVector(rotation: QuaternionXYZW, vector: Vec3): Vec3 {
  const [x, y, z, w] = rotation;
  const [vx, vy, vz] = vector;
  const tx = 2 * (y * vz - z * vy);
  const ty = 2 * (z * vx - x * vz);
  const tz = 2 * (x * vy - y * vx);
  return [vx + w * tx + (y * tz - z * ty), vy + w * ty + (z * tx - x * tz), vz + w * tz + (x * ty - y * tx)];
}

export function composePose(first: PoseTransform, second: PoseTransform): PoseTransform {
  const firstRotation = normalizeQuaternion(first.rotation_xyzw);
  const secondRotation = normalizeQuaternion(second.rotation_xyzw);
  if (isDynamicRejection(firstRotation)) throw new Error(firstRotation.detail);
  if (isDynamicRejection(secondRotation)) throw new Error(secondRotation.detail);
  const rotation = normalizeComputedQuaternion(multiplyQuaternion(firstRotation, secondRotation));
  const translated = rotateVector(firstRotation, second.translation_m);
  return {
    translation_m: [
      first.translation_m[0] + translated[0],
      first.translation_m[1] + translated[1],
      first.translation_m[2] + translated[2],
    ],
    rotation_xyzw: rotation,
  };
}

/** Return `inverse(T_A_B)`, preserving the explicit xyzw convention. */
export function invertPose(pose: PoseTransform): PoseTransform {
  const rotation = normalizeQuaternion(pose.rotation_xyzw);
  if (isDynamicRejection(rotation)) throw new Error(rotation.detail);
  const inverseRotation: QuaternionXYZW = [-rotation[0], -rotation[1], -rotation[2], rotation[3]];
  const translation = rotateVector(inverseRotation, [
    -pose.translation_m[0],
    -pose.translation_m[1],
    -pose.translation_m[2],
  ]);
  return { translation_m: translation, rotation_xyzw: inverseRotation };
}

export interface VirtualFixedTagCandidate {
  frame_id: string;
  hamming: number;
  decision_margin: number;
  image_area_px2: number;
}

/** Deterministic virtual counterpart of the camera-side fixed-tag selection.
 * It only consumes supplied synthetic observations; it performs no image
 * detection and has no ROS dependency. */
export function selectVirtualFixedTag(
  candidates: readonly VirtualFixedTagCandidate[],
  minimumDecisionMargin: number,
  previousFrameId?: string,
): VirtualFixedTagCandidate | DynamicRejection {
  const eligible = candidates.filter((candidate) =>
    typeof candidate.frame_id === "string" && candidate.frame_id.length > 0 &&
    Number.isInteger(candidate.hamming) && candidate.hamming === 0 &&
    Number.isFinite(candidate.decision_margin) && candidate.decision_margin >= minimumDecisionMargin &&
    Number.isFinite(candidate.image_area_px2) && candidate.image_area_px2 > 0,
  );
  if (!eligible.length) {
    return { status: "rejected", reason: "unknown_fixed_tag_frame", detail: "No virtual fixed-tag candidate met the quality gate." };
  }
  return [...eligible].sort((left, right) => {
    if (left.image_area_px2 !== right.image_area_px2) return right.image_area_px2 - left.image_area_px2;
    // Keep the previously-used valid frame when the quality and visible area
    // are otherwise tied.  This is hysteresis, not pose interpolation.
    const leftPrevious = left.frame_id === previousFrameId ? 1 : 0;
    const rightPrevious = right.frame_id === previousFrameId ? 1 : 0;
    if (leftPrevious !== rightPrevious) return rightPrevious - leftPrevious;
    return left.frame_id.localeCompare(right.frame_id);
  })[0];
}

export interface VirtualCameraSideGeneratorInput {
  camera_fixed_tag: PoseTransform;
  camera_box_tag: PoseTransform;
  box_tag_to_box_tracking: PoseTransform;
  source_stamp: { sec: number; nanosec: number };
  fixed_tag_frame: string;
  box_child_frame: string;
}

/**
 * Compute the same camera-side relation used by the real integration:
 *
 * `T_fixedTag_boxTracking = inverse(T_camera_fixedTag) ×
 *                            T_camera_boxTag ×
 *                            T_boxTag_boxTracking`
 *
 * It emits the Viewer wire contract without importing ROS 2.
 */
export function createVirtualTransformStamped(
  input: VirtualCameraSideGeneratorInput,
): TransformStampedMessage | DynamicRejection {
  if (typeof input.fixed_tag_frame !== "string" || !input.fixed_tag_frame ||
      typeof input.box_child_frame !== "string" || !input.box_child_frame ||
      !Number.isInteger(input.source_stamp.sec) || input.source_stamp.sec < 0 ||
      !Number.isInteger(input.source_stamp.nanosec) || input.source_stamp.nanosec < 0 || input.source_stamp.nanosec >= 1_000_000_000) {
    return { status: "rejected", reason: "invalid_message_shape", detail: "Virtual generator frame or timestamp is invalid." };
  }
  const cameraFixed = normalizePose(input.camera_fixed_tag);
  const cameraBox = normalizePose(input.camera_box_tag);
  const tagToBox = normalizePose(input.box_tag_to_box_tracking);
  if (isDynamicRejection(cameraFixed)) return cameraFixed;
  if (isDynamicRejection(cameraBox)) return cameraBox;
  if (isDynamicRejection(tagToBox)) return tagToBox;
  return {
    header: { stamp: { sec: input.source_stamp.sec, nanosec: input.source_stamp.nanosec }, frame_id: input.fixed_tag_frame },
    child_frame_id: input.box_child_frame,
    transform: composePose(composePose(invertPose(cameraFixed), cameraBox), tagToBox),
  };
}

/** Euclidean distance in the Metric Scene's meter world. */
export function distanceM(a: Vec3, b: Vec3): number {
  return Math.hypot(a[0] - b[0], a[1] - b[1], a[2] - b[2]);
}

function rotationDistanceDeg(a: QuaternionXYZW, b: QuaternionXYZW): number {
  const dot = Math.min(1, Math.abs(a[0] * b[0] + a[1] * b[1] + a[2] * b[2] + a[3] * b[3]));
  return (2 * Math.acos(dot) * 180) / Math.PI;
}

export function parseTransformStamped(value: unknown): TransformStampedMessage | DynamicRejection {
  if (!value || typeof value !== "object") {
    return { status: "rejected", reason: "invalid_message_shape", detail: "TransformStamped must be an object." };
  }
  const candidate = value as Partial<TransformStampedMessage>;
  const header = candidate.header as Partial<TransformStampedMessage["header"]> | undefined;
  const stamp = normalizeRosTime(header?.stamp);
  if (!header || typeof header.frame_id !== "string" || !header.frame_id || !stamp ||
      typeof candidate.child_frame_id !== "string" || !candidate.child_frame_id) {
    return { status: "rejected", reason: "invalid_message_shape", detail: "Missing TransformStamped header or child frame." };
  }
  const transform = normalizeRosTransform(candidate.transform);
  if (isDynamicRejection(transform)) return transform;
  return {
    header: {
      stamp,
      frame_id: header.frame_id,
    },
    child_frame_id: candidate.child_frame_id,
    transform,
  };
}

export function sourceStampMs(stamp: { sec: number; nanosec: number }): number | undefined {
  if (!Number.isInteger(stamp.sec) || stamp.sec < 0 || !Number.isInteger(stamp.nanosec) ||
      stamp.nanosec < 0 || stamp.nanosec >= 1_000_000_000) return undefined;
  const value = stamp.sec * 1000 + stamp.nanosec / 1_000_000;
  return Number.isFinite(value) ? value : undefined;
}

export function createDynamicTrackingRuntimeState(
  sourceMode: DynamicTrackingRuntimeState["source_mode"] = "rosbridge",
  trackingAlignment: DynamicTrackingRuntimeState["tracking_alignment"] = "verified_fixed_tag_map",
): DynamicTrackingRuntimeState {
  return {
    connection_state: "disconnected",
    tracking_state: "disconnected",
    accepted_message_count: 0,
    rejected_message_count: 0,
    source_mode: sourceMode,
    tracking_alignment: trackingAlignment,
  };
}

export function createDynamicTrackingCollectionState(
  _registry: DynamicObjectRegistry,
): DynamicTrackingCollectionState {
  return {
    // Registry entries are optional visual overrides, not live objects.  Do
    // not create invisible "configured boxes" before the ROS stream proves
    // that a physical box is currently detected.
    object_states: {},
  };
}

function rejectedTrackingResult(
  previous: DynamicTrackingRuntimeState,
  rejection: DynamicRejection,
): DynamicTrackingMessageResult {
  // Keep the previously verified pose in state for diagnostics, but never
  // overwrite it with an invalid value. The caller may render it only until
  // `advanceDynamicTrackingState` reaches the receive-time timeout.
  return {
    rejection,
    state: {
      ...previous,
      connection_state: "rejected",
      tracking_state: "invalid",
      rejected_message_count: previous.rejected_message_count + 1,
      latest_rejection_reason: rejection.reason,
    },
  };
}

/** Pure state transition for connection events. A reconnect resets each
 * child-frame ordering generation because ROS clocks may restart. */
export function transitionDynamicTrackingConnection(
  previous: DynamicTrackingRuntimeState,
  connection: DynamicTrackingRuntimeState["connection_state"],
): DynamicTrackingRuntimeState {
  if (connection === "connecting") {
    return {
      ...previous,
      connection_state: "connecting",
      tracking_state: "waiting",
      last_pose: undefined,
      last_source_stamp_ms: undefined,
      last_received_monotonic_ms: undefined,
      active_fixed_tag_frame: undefined,
      dynamic_object_id: undefined,
      latest_rejection_reason: undefined,
    };
  }
  if (connection === "disconnected") {
    return {
      ...previous,
      connection_state: "disconnected",
      tracking_state: "disconnected",
      last_pose: undefined,
      last_source_stamp_ms: undefined,
      last_received_monotonic_ms: undefined,
      active_fixed_tag_frame: undefined,
      dynamic_object_id: undefined,
    };
  }
  return { ...previous, connection_state: connection };
}

export function transitionDynamicTrackingCollectionConnection(
  previous: DynamicTrackingCollectionState,
  connection: DynamicTrackingRuntimeState["connection_state"],
): DynamicTrackingCollectionState {
  return {
    ...previous,
    object_states: Object.fromEntries(Object.entries(previous.object_states).map(([objectId, state]) => [
      objectId,
      transitionDynamicTrackingConnection(state, connection),
    ])),
    ...(connection === "connecting" || connection === "disconnected" ? { latest_rejection: undefined } : {}),
  };
}

/**
 * Scene Package entries are optional visual/threshold overrides.  They do
 * not authorise a fixed set of physical boxes: any well-formed `box_<N>` is
 * represented by the generic 120-size cube when no override exists.
 */
export function resolveDynamicObjectConfig(
  registry: DynamicObjectRegistry,
  objectId: string,
): DynamicObjectConfig | DynamicRejection {
  const configured = registry.objects.find((candidate) => candidate.id === objectId);
  if (configured) return configured;
  if (registry.objects.some((candidate) => candidate.box_tag_frame === objectId)) {
    return { status: "rejected", reason: "invalid_child_frame", detail: objectId };
  }
  if (!isRuntimeDynamicBoxId(objectId)) {
    return { status: "rejected", reason: "unknown_dynamic_object", detail: objectId };
  }
  return createRuntimeDynamicBoxConfig(objectId);
}

export function resolveDynamicPose(
  messageValue: unknown,
  fixedTagMap: FixedTagMap | undefined,
  registry: DynamicObjectRegistry,
  previousPose: ResolvedDynamicPose | undefined,
  renderFromWorld: PoseTransform = IDENTITY_POSE,
  viewerFromRos: PoseTransform = IDENTITY_POSE,
): DynamicResolution | DynamicRejection {
  if (!fixedTagMap) return { status: "rejected", reason: "missing_fixed_tag_map", detail: "No fixed tag map is available." };
  const message = parseTransformStamped(messageValue);
  if (isDynamicRejection(message)) return message;
  const fixedTagPose = fixedTagMap.frames[message.header.frame_id];
  if (!fixedTagPose) return { status: "rejected", reason: "unknown_fixed_tag_frame", detail: message.header.frame_id };
  const object = resolveDynamicObjectConfig(registry, message.child_frame_id);
  if (isDynamicRejection(object)) return object;
  const stampMs = sourceStampMs(message.header.stamp);
  if (stampMs === undefined) return { status: "rejected", reason: "invalid_message_shape", detail: "TransformStamped stamp is invalid." };
  const viewerRelativePose = convertRosRelativePoseToViewer(message.transform, viewerFromRos);
  const gsBox = composePose(fixedTagPose, viewerRelativePose);
  if (previousPose && distanceM(previousPose.translation_m, gsBox.translation_m) > object.max_translation_jump_m) {
    return { status: "rejected", reason: "translation_jump_exceeded", detail: "Translation changed beyond the configured jump limit." };
  }
  if (previousPose && rotationDistanceDeg(previousPose.rotation_xyzw, gsBox.rotation_xyzw) > object.max_rotation_jump_deg) {
    return { status: "rejected", reason: "rotation_jump_exceeded", detail: "Rotation changed beyond the configured jump limit." };
  }
  // `gs_world` is the Metric Scene's formal world coordinate frame (+Y up).
  // The current renderer has T_render_world = identity, but retain this term
  // so a future renderer conversion cannot be applied implicitly.
  const renderBox = composePose(composePose(renderFromWorld, gsBox), object.object_to_render);
  return {
    status: "accepted",
    pose: {
      object_id: object.id,
      fixed_tag_frame: message.header.frame_id,
      source_stamp_ms: stampMs,
      translation_m: renderBox.translation_m,
      rotation_xyzw: renderBox.rotation_xyzw,
    },
  };
}

/** Apply one received TransformStamped. Source stamps only establish message
 * ordering. Freshness is intentionally based on `receivedMonotonicMs`, not
 * ROS epoch time or the browser wall clock. */
export function processDynamicTrackingMessage(
  messageValue: unknown,
  fixedTagMap: FixedTagMap | undefined,
  registry: DynamicObjectRegistry,
  previous: DynamicTrackingRuntimeState,
  receivedMonotonicMs: number,
  renderFromWorld: PoseTransform = IDENTITY_POSE,
  viewerFromRos: PoseTransform = IDENTITY_POSE,
): DynamicTrackingMessageResult {
  if (!Number.isFinite(receivedMonotonicMs) || receivedMonotonicMs < 0) {
    return rejectedTrackingResult(previous, {
      status: "rejected",
      reason: "invalid_receive_time",
      detail: "WebSocket receive time must be a finite monotonic value.",
    });
  }
  const parsed = parseTransformStamped(messageValue);
  if (isDynamicRejection(parsed)) return rejectedTrackingResult(previous, parsed);
  const stampMs = sourceStampMs(parsed.header.stamp);
  if (stampMs === undefined) {
    return rejectedTrackingResult(previous, {
      status: "rejected",
      reason: "invalid_message_shape",
      detail: "TransformStamped source stamp is invalid.",
    });
  }
  if (previous.last_source_stamp_ms !== undefined) {
    if (stampMs < previous.last_source_stamp_ms) {
      return rejectedTrackingResult(previous, {
        status: "rejected",
        reason: "timestamp_regression",
        detail: "TransformStamped source stamp regressed.",
      });
    }
    if (stampMs === previous.last_source_stamp_ms) {
      return rejectedTrackingResult(previous, {
        status: "rejected",
        reason: "duplicate_timestamp",
        detail: "TransformStamped source stamp is a duplicate.",
      });
    }
  }
  const resolution = resolveDynamicPose(parsed, fixedTagMap, registry, previous.last_pose, renderFromWorld, viewerFromRos);
  if (isDynamicRejection(resolution)) return rejectedTrackingResult(previous, resolution);
  return {
    resolution,
    state: {
      ...previous,
      connection_state: "receiving",
      tracking_state: "tracking",
      last_pose: resolution.pose,
      last_source_stamp_ms: resolution.pose.source_stamp_ms,
      last_received_monotonic_ms: receivedMonotonicMs,
      active_fixed_tag_frame: resolution.pose.fixed_tag_frame,
      dynamic_object_id: resolution.pose.object_id,
      accepted_message_count: previous.accepted_message_count + 1,
      latest_rejection_reason: undefined,
    },
  };
}

/** Route one ROS TransformStamped to its declared dynamic object.
 *
 * Each object owns its timestamp watermark, pose and timeout. An invalid
 * packet or an unknown child frame is fail-closed for that packet only: it
 * cannot make a previously verified, different box disappear or inherit a
 * jump comparison from another box. */
export function processDynamicTrackingCollectionMessage(
  messageValue: unknown,
  fixedTagMap: FixedTagMap | undefined,
  registry: DynamicObjectRegistry,
  previous: DynamicTrackingCollectionState,
  receivedMonotonicMs: number,
  renderFromWorld: PoseTransform = IDENTITY_POSE,
  viewerFromRos: PoseTransform = IDENTITY_POSE,
): DynamicTrackingCollectionMessageResult {
  const parsed = parseTransformStamped(messageValue);
  if (isDynamicRejection(parsed)) {
    return {
      rejection: parsed,
      state: { ...previous, latest_rejection: parsed },
    };
  }
  const object = resolveDynamicObjectConfig(registry, parsed.child_frame_id);
  if (isDynamicRejection(object)) {
    const rejection = object;
    return {
      rejection,
      state: { ...previous, latest_rejection: rejection },
    };
  }
  const isNewRuntimeObject = previous.object_states[object.id] === undefined;
  if (isNewRuntimeObject && Object.keys(previous.object_states).length >= MAX_RUNTIME_DYNAMIC_BOXES) {
    const rejection: DynamicRejection = {
      status: "rejected",
      reason: "dynamic_object_limit_exceeded",
      detail: `At most ${MAX_RUNTIME_DYNAMIC_BOXES} live dynamic box IDs are allowed.`,
    };
    return { rejection, state: { ...previous, latest_rejection: rejection } };
  }
  const objectState = previous.object_states[object.id] ?? createDynamicTrackingRuntimeState(
    "rosbridge",
    fixedTagMap ? "verified_fixed_tag_map" : "uncalibrated",
  );
  const result = processDynamicTrackingMessage(
    parsed,
    fixedTagMap,
    registry,
    objectState,
    receivedMonotonicMs,
    renderFromWorld,
    viewerFromRos,
  );
  return {
    resolution: result.resolution,
    rejection: result.rejection,
    state: {
      ...previous,
      object_states: { ...previous.object_states, [object.id]: result.state },
      ...(result.rejection ? { latest_rejection: result.rejection } : { latest_rejection: undefined }),
    },
  };
}

/** Advance receive-time expiry without inspecting or comparing ROS epochs. */
export function advanceDynamicTrackingState(
  previous: DynamicTrackingRuntimeState,
  nowMonotonicMs: number,
  timeoutMs: number,
): DynamicTrackingRuntimeState {
  if (!Number.isFinite(nowMonotonicMs) || !Number.isFinite(timeoutMs) || timeoutMs <= 0 ||
      previous.last_received_monotonic_ms === undefined) return previous;
  const ageMs = nowMonotonicMs - previous.last_received_monotonic_ms;
  if (ageMs > timeoutMs) {
    return { ...previous, connection_state: "stale", tracking_state: "lost" };
  }
  if (ageMs > timeoutMs * 0.75 && previous.tracking_state === "tracking") {
    return { ...previous, connection_state: "stale", tracking_state: "stale" };
  }
  return previous;
}

/** Advance the receive-time state of every object independently. */
export function advanceDynamicTrackingCollectionState(
  previous: DynamicTrackingCollectionState,
  registry: DynamicObjectRegistry,
  nowMonotonicMs: number,
  packageTimeoutMs: number,
): DynamicTrackingCollectionState {
  return {
    ...previous,
    object_states: Object.fromEntries(Object.keys(previous.object_states).map((objectId) => {
      const object = resolveDynamicObjectConfig(registry, objectId);
      const state = previous.object_states[objectId] ?? createDynamicTrackingRuntimeState();
      // Object IDs admitted through `process…Message` are valid. Preserve a
      // prior state rather than throwing if a hand-edited registry later
      // becomes inconsistent; the next incoming message will be rejected.
      if (isDynamicRejection(object)) return [objectId, state];
      // Expiry is one immutable policy per Scene Package.  Do not fall back
      // to per-object metadata, query parameters, or a hard-coded default.
      return [objectId, advanceDynamicTrackingState(state, nowMonotonicMs, packageTimeoutMs)];
    })),
  };
}
