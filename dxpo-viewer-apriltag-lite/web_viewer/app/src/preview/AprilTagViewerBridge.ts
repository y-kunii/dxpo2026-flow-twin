import {
  composePose,
  convertRosRelativePoseToViewer,
  convertViewerRelativePoseToRos,
  createVirtualTransformStamped,
  DEFAULT_DYNAMIC_BOX_DIMENSIONS_M,
  BOX_TAG_FACES,
  isDynamicRejection,
  isRuntimeDynamicBoxId,
  type BoxDimensionsM,
  type BoxTagFace,
  type PoseTransform,
  type TransformStampedMessage,
} from "../dynamic/DynamicTrackingContract";

export type { BoxTagFace } from "../dynamic/DynamicTrackingContract";

export interface AprilTagBridgeDetection {
  family: string;
  tag_id: number;
  hamming: number;
  decision_margin: number;
  tracking_state?: "detected" | "enhanced" | "optical_flow";
  pose_camera_tag: {
    translation_m: [number, number, number];
    rotation_quaternion_xyzw: [number, number, number, number];
  } | null;
}

export interface BoxTagMapping {
  tag_id: number;
  child_frame_id: string;
  face: BoxTagFace;
  dimensions_m: BoxDimensionsM;
}

export interface AprilTagViewerBridgeConfiguration {
  family: "tag25h9";
  fixed_tag_id: number;
  /** All package-verified anchors. `fixed_tag_id` remains the preferred anchor. */
  fixed_tags?: Array<{ tag_id: number; frame_id: string; world_pose: PoseTransform }>;
  boxes: BoxTagMapping[];
  minimum_decision_margin: number;
  /** Immutable T_viewer_ros read from the selected v1.2 Scene Package. */
  viewer_from_ros: PoseTransform;
}

export interface BridgeMessageBatch {
  messages: TransformStampedMessage[];
  detail: string;
  anchor?: {
    tag_id: number;
    frame_id: string;
    switched: boolean;
    reason: string;
    candidate_streak: number;
  };
}

const HALF_SQRT_TWO = Math.SQRT1_2;
const DETECTOR_TAG_FROM_STANDARD_TAG: PoseTransform = {
  translation_m: [0, 0, 0],
  rotation_xyzw: [1, 0, 0, 0],
};
const BOX_FACES = new Set<BoxTagFace>(BOX_TAG_FACES);

function finitePositive(value: number, label: string): number {
  if (!Number.isFinite(value) || value <= 0) throw new Error(`${label}は正の有限値にしてください。`);
  return value;
}

export function validateBoxTagMappings(mappings: BoxTagMapping[]): BoxTagMapping[] {
  if (mappings.length === 0) throw new Error("箱Tag対応を1件以上入力してください。");
  const tagIds = new Set<number>();
  const childFrames = new Set<string>();
  return mappings.map((mapping) => {
    const tagId = mapping.tag_id;
    const childFrame = mapping.child_frame_id;
    const face = mapping.face;
    if (!Number.isInteger(tagId) || tagId < 20) throw new Error(`箱Tag IDは20以上です: ${tagId}`);
    if (!isRuntimeDynamicBoxId(childFrame)) throw new Error(`child frameはbox_01形式にしてください: ${childFrame}`);
    if (!BOX_FACES.has(face)) throw new Error(`未対応の貼付面です: ${face}`);
    if (tagIds.has(tagId)) throw new Error(`箱Tag IDが重複しています: ${tagId}`);
    if (childFrames.has(childFrame)) throw new Error(`child frameが重複しています: ${childFrame}`);
    tagIds.add(tagId);
    childFrames.add(childFrame);
    return {
      tag_id: tagId,
      child_frame_id: childFrame,
      face,
      dimensions_m: {
        width_m: finitePositive(mapping.dimensions_m.width_m, `${childFrame} width`),
        depth_m: finitePositive(mapping.dimensions_m.depth_m, `${childFrame} depth`),
        height_m: finitePositive(mapping.dimensions_m.height_m, `${childFrame} height`),
      },
    };
  });
}

/** Parse the legacy `20=box_01@top` representation with explicit fallback dimensions. */
export function parseBoxTagMappings(
  value: string,
  fallbackDimensions: BoxDimensionsM = DEFAULT_DYNAMIC_BOX_DIMENSIONS_M,
): BoxTagMapping[] {
  const entries = value.split(/[,;\n]+/).map((item) => item.trim()).filter(Boolean);
  return validateBoxTagMappings(entries.map((entry) => {
    const match = /^(\d+)\s*=\s*(box_0*[1-9]\d*)(?:\s*@\s*(top|front|right|left|back|bottom))?$/.exec(entry);
    if (!match) throw new Error(`箱Tag対応が不正です: ${entry}`);
    return {
      tag_id: Number(match[1]),
      child_frame_id: match[2],
      face: (match[3] ?? "top") as BoxTagFace,
      dimensions_m: { ...fallbackDimensions },
    };
  }));
}

/**
 * Return T_boxTag_boxTracking for a centered tag on one face of the canonical
 * cardboard box.  This is the exact direction defined by the camera adapter
 * contract: points in boxTracking are mapped into the physical boxTag frame.
 */
export function boxTrackingInTagPose(face: BoxTagFace, dimensions: BoxDimensionsM): PoseTransform {
  const width = finitePositive(dimensions.width_m, "box width");
  const depth = finitePositive(dimensions.depth_m, "box depth");
  const height = finitePositive(dimensions.height_m, "box height");
  switch (face) {
    case "top":
      return { translation_m: [0, 0, -height / 2], rotation_xyzw: [HALF_SQRT_TWO, 0, 0, HALF_SQRT_TWO] };
    case "bottom":
      return { translation_m: [0, 0, -height / 2], rotation_xyzw: [-HALF_SQRT_TWO, 0, 0, HALF_SQRT_TWO] };
    case "front":
      return { translation_m: [0, 0, -depth / 2], rotation_xyzw: [0, 0, 0, 1] };
    case "back":
      return { translation_m: [0, 0, -depth / 2], rotation_xyzw: [0, 1, 0, 0] };
    case "right":
      return { translation_m: [0, 0, -width / 2], rotation_xyzw: [0, -HALF_SQRT_TWO, 0, HALF_SQRT_TWO] };
    case "left":
      return { translation_m: [0, 0, -width / 2], rotation_xyzw: [0, HALF_SQRT_TWO, 0, HALF_SQRT_TWO] };
  }
}

export function epochMillisecondsToRosStamp(epochMs: number): { sec: number; nanosec: number } {
  if (!Number.isFinite(epochMs) || epochMs < 0) throw new Error("画像取得時刻が不正です。");
  const sec = Math.floor(epochMs / 1000);
  const nanosec = Math.floor((epochMs - sec * 1000) * 1_000_000);
  return { sec, nanosec: Math.max(0, Math.min(999_999_999, nanosec)) };
}

export function acceptedPoseDetection(
  detections: AprilTagBridgeDetection[],
  family: string,
  tagId: number,
  minimumDecisionMargin: number,
): AprilTagBridgeDetection | undefined {
  return detections
    .filter((item) => item.family === family && item.tag_id === tagId)
    .filter((item) => item.tracking_state !== "optical_flow")
    .filter((item) => item.hamming === 0 && item.decision_margin >= minimumDecisionMargin && item.pose_camera_tag !== null)
    .sort((left, right) => right.decision_margin - left.decision_margin)[0];
}

function buildForFixedTag(
  detections: AprilTagBridgeDetection[],
  configuration: AprilTagViewerBridgeConfiguration,
  sourceEpochMs: number,
  fixedTagId: number,
): BridgeMessageBatch {
  if (configuration.family !== "tag25h9") throw new Error("Viewer連携の箱Tag familyはtag25h9です。");
  if (!Number.isInteger(fixedTagId) || fixedTagId < 0 || fixedTagId > 9) {
    throw new Error("固定Tag IDは0～9です。");
  }
  if (!Number.isFinite(configuration.minimum_decision_margin) || configuration.minimum_decision_margin < 0) {
    throw new Error("decision margin閾値は0以上の有限値にしてください。");
  }
  const fixed = acceptedPoseDetection(
    detections,
    configuration.family,
    fixedTagId,
    configuration.minimum_decision_margin,
  );
  if (!fixed?.pose_camera_tag) {
    return { messages: [], detail: `固定Tag ID ${fixedTagId}の同一フレーム確定値を待っています。` };
  }
  const sourceStamp = epochMillisecondsToRosStamp(sourceEpochMs);
  const fixedFrame = configuration.fixed_tags?.find((item) => item.tag_id === fixedTagId)?.frame_id
    ?? `apriltag_${configuration.family}_${fixedTagId}`;
  const messages: TransformStampedMessage[] = [];
  for (const mapping of configuration.boxes) {
    const box = acceptedPoseDetection(
      detections,
      configuration.family,
      mapping.tag_id,
      configuration.minimum_decision_margin,
    );
    if (!box?.pose_camera_tag) continue;
    // pupil-apriltags reports +X right, +Y down and +Z into the printed
    // plane. Normalize both detections to the repository's physical tag
    // convention (+X right, +Y up, +Z toward the camera) before computing
    // their relative pose. Applying the adapter only to the box offset leaves
    // detector-space Y/Z motion inverted.
    const cameraFixedStandardTag = composePose(
      {
        translation_m: fixed.pose_camera_tag.translation_m,
        rotation_xyzw: fixed.pose_camera_tag.rotation_quaternion_xyzw,
      },
      DETECTOR_TAG_FROM_STANDARD_TAG,
    );
    const cameraBoxStandardTag = composePose(
      {
        translation_m: box.pose_camera_tag.translation_m,
        rotation_xyzw: box.pose_camera_tag.rotation_quaternion_xyzw,
      },
      DETECTOR_TAG_FROM_STANDARD_TAG,
    );
    const standardTagGenerated = createVirtualTransformStamped({
      camera_fixed_tag: cameraFixedStandardTag,
      camera_box_tag: cameraBoxStandardTag,
      box_tag_to_box_tracking: boxTrackingInTagPose(mapping.face, mapping.dimensions_m),
      source_stamp: sourceStamp,
      fixed_tag_frame: fixedFrame,
      box_child_frame: mapping.child_frame_id,
    });
    if (isDynamicRejection(standardTagGenerated)) {
      throw new Error(`${mapping.child_frame_id}: ${standardTagGenerated.detail}`);
    }
    messages.push({
      ...standardTagGenerated,
      transform: convertViewerRelativePoseToRos(
        standardTagGenerated.transform,
        configuration.viewer_from_ros,
      ),
    });
  }
  return {
    messages,
    detail: messages.length > 0
      ? `${fixedFrame}基準で${messages.length}箱を生成しました。`
      : "登録済み箱Tagの同一フレーム確定値を待っています。",
  };
}

function quaternionDistanceDeg(left: readonly number[], right: readonly number[]): number {
  const dot = Math.min(1, Math.abs(left.reduce((sum, value, index) => sum + value * right[index], 0)));
  return 2 * Math.acos(dot) * 180 / Math.PI;
}

/** Stateful selector for package-verified fixed tags. A replacement anchor is
 * required in three consecutive decoded frames and must preserve the last
 * object pose in world coordinates before it becomes active. */
export class AprilTagViewerBridgeRuntime {
  private activeTagId: number | undefined;
  private candidateTagId: number | undefined;
  private candidateStreak = 0;
  private readonly lastWorldByChild = new Map<string, PoseTransform>();

  reset(): void {
    this.activeTagId = undefined;
    this.candidateTagId = undefined;
    this.candidateStreak = 0;
    this.lastWorldByChild.clear();
  }

  build(
    detections: AprilTagBridgeDetection[],
    configuration: AprilTagViewerBridgeConfiguration,
    sourceEpochMs: number,
  ): BridgeMessageBatch {
    const fallback = [{
      tag_id: configuration.fixed_tag_id,
      frame_id: `apriltag_${configuration.family}_${configuration.fixed_tag_id}`,
      world_pose: { translation_m: [0, 0, 0], rotation_xyzw: [0, 0, 0, 1] } as PoseTransform,
    }];
    const candidates = (configuration.fixed_tags?.length ? configuration.fixed_tags : fallback)
      .map((anchor) => ({
        anchor,
        detection: acceptedPoseDetection(detections, configuration.family, anchor.tag_id, configuration.minimum_decision_margin),
      }))
      .filter((item): item is typeof item & { detection: AprilTagBridgeDetection } => Boolean(item.detection?.pose_camera_tag))
      .sort((left, right) => right.detection.decision_margin - left.detection.decision_margin);
    const active = candidates.find((item) => item.anchor.tag_id === this.activeTagId);
    const preferred = candidates.find((item) => item.anchor.tag_id === configuration.fixed_tag_id);
    const best = candidates[0];
    let selected = active;
    let reason = "active_anchor_retained";
    if (this.activeTagId === undefined && preferred) {
      selected = preferred;
      reason = "preferred_anchor_initial";
    } else if (this.activeTagId === undefined && best) {
      selected = best;
      reason = "best_anchor_initial";
    } else if (best && (!active || (best.anchor.tag_id !== active.anchor.tag_id && best.detection.decision_margin >= active.detection.decision_margin + 10))) {
      if (this.candidateTagId === best.anchor.tag_id) this.candidateStreak += 1;
      else {
        this.candidateTagId = best.anchor.tag_id;
        this.candidateStreak = 1;
      }
      if (this.candidateStreak < 3) {
        return {
          messages: [],
          detail: `固定Tag ${best.anchor.frame_id}の切替確認中 ${this.candidateStreak}/3`,
          anchor: { tag_id: best.anchor.tag_id, frame_id: best.anchor.frame_id, switched: false, reason: "candidate_confirmation", candidate_streak: this.candidateStreak },
        };
      }
      selected = best;
      reason = active ? "better_quality_anchor" : "active_anchor_occluded";
    } else if (!active) {
      this.candidateTagId = undefined;
      this.candidateStreak = 0;
      return { messages: [], detail: "登録済み固定Tagの確定値を待っています。" };
    }
    if (!selected) return { messages: [], detail: "登録済み固定Tagの確定値を待っています。" };
    const batch = buildForFixedTag(detections, configuration, sourceEpochMs, selected.anchor.tag_id);
    const switched = this.activeTagId !== undefined && selected.anchor.tag_id !== this.activeTagId;
    if (switched && batch.messages.length > 0 && this.lastWorldByChild.size > 0) {
      for (const message of batch.messages) {
        const relativeViewer = convertRosRelativePoseToViewer(message.transform, configuration.viewer_from_ros);
        const world = composePose(selected.anchor.world_pose, relativeViewer);
        const previous = this.lastWorldByChild.get(message.child_frame_id);
        if (!previous) continue;
        const distance = Math.hypot(...world.translation_m.map((value, index) => value - previous.translation_m[index]));
        const rotation = quaternionDistanceDeg(world.rotation_xyzw, previous.rotation_xyzw);
        if (distance > 0.5 || rotation > 45) {
          return {
            messages: [],
            detail: `固定Tag切替を拒否しました（world連続性: ${distance.toFixed(3)} m / ${rotation.toFixed(1)}°）`,
            anchor: { tag_id: selected.anchor.tag_id, frame_id: selected.anchor.frame_id, switched: false, reason: "world_continuity_rejected", candidate_streak: this.candidateStreak },
          };
        }
      }
    }
    if (batch.messages.length > 0) {
      this.activeTagId = selected.anchor.tag_id;
      this.candidateTagId = undefined;
      this.candidateStreak = 0;
      for (const message of batch.messages) {
        const relativeViewer = convertRosRelativePoseToViewer(message.transform, configuration.viewer_from_ros);
        this.lastWorldByChild.set(message.child_frame_id, composePose(selected.anchor.world_pose, relativeViewer));
      }
    }
    batch.anchor = {
      tag_id: selected.anchor.tag_id,
      frame_id: selected.anchor.frame_id,
      switched,
      reason,
      candidate_streak: this.candidateStreak,
    };
    return batch;
  }
}

export function buildViewerBridgeMessages(
  detections: AprilTagBridgeDetection[],
  configuration: AprilTagViewerBridgeConfiguration,
  sourceEpochMs: number,
): BridgeMessageBatch {
  return buildForFixedTag(detections, configuration, sourceEpochMs, configuration.fixed_tag_id);
}
