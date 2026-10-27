import * as THREE from "three";

export interface FrameAnalysisCameraRecord {
  frame_analysis_id: string;
  scene_package_id: string;
  pose: {
    position: { x: number; y: number; z: number };
    rotation: { x: number; y: number; z: number; w: number };
  };
  projection?: { vertical_fov_deg?: number };
}

export interface FrameAnalysisCameraControls {
  enabled: boolean;
  target: THREE.Vector3;
  update(): void;
}

export function requestedFrameAnalysisId(query: URLSearchParams): string | undefined {
  const value = query.get("frame_analysis");
  return value && /^[A-Za-z0-9][A-Za-z0-9_.-]*$/.test(value) ? value : undefined;
}

/** Restore a registered COLMAP optical-camera pose in Viewer world. */
export function applyFrameAnalysisCameraRecord(
  frame: FrameAnalysisCameraRecord,
  expectedFrameAnalysisId: string,
  expectedScenePackageId: string,
  camera: THREE.PerspectiveCamera,
  controls: FrameAnalysisCameraControls,
  afterApply?: () => void,
): void {
  if (frame.frame_analysis_id !== expectedFrameAnalysisId) {
    throw new Error("frame_analysis_identity_mismatch");
  }
  if (frame.scene_package_id !== expectedScenePackageId) {
    throw new Error("frame_analysis_scene_package_mismatch");
  }

  const positionValues = [frame.pose.position.x, frame.pose.position.y, frame.pose.position.z];
  const rotationValues = [
    frame.pose.rotation.x,
    frame.pose.rotation.y,
    frame.pose.rotation.z,
    frame.pose.rotation.w,
  ];
  if (![...positionValues, ...rotationValues].every(Number.isFinite)) {
    throw new Error("frame_analysis_pose_non_finite");
  }
  const opticalRotation = new THREE.Quaternion(...rotationValues);
  const norm = opticalRotation.length();
  if (norm === 0 || Math.abs(norm - 1) > 1e-6) {
    throw new Error("frame_analysis_pose_quaternion_not_unit");
  }

  // Stored axes: +X right, +Y down, +Z forward. THREE looks along -Z/+Y up.
  const forward = new THREE.Vector3(0, 0, 1).applyQuaternion(opticalRotation);
  const up = new THREE.Vector3(0, -1, 0).applyQuaternion(opticalRotation);
  const position = new THREE.Vector3(...positionValues);
  controls.enabled = true;
  camera.position.copy(position);
  camera.up.copy(up);
  controls.target.copy(position).add(forward);
  const verticalFov = frame.projection?.vertical_fov_deg;
  if (typeof verticalFov === "number"
    && Number.isFinite(verticalFov)
    && verticalFov > 0
    && verticalFov < 180) {
    camera.fov = verticalFov;
    camera.updateProjectionMatrix();
  }
  camera.lookAt(controls.target);
  controls.update();
  camera.updateMatrixWorld(true);
  afterApply?.();
}

export async function restoreFrameAnalysisCameraPose(
  frameAnalysisId: string,
  scenePackageId: string,
  camera: THREE.PerspectiveCamera,
  controls: FrameAnalysisCameraControls,
  afterApply?: () => void,
): Promise<FrameAnalysisCameraRecord> {
  const response = await fetch(`/api/frame-analysis/frames/${encodeURIComponent(frameAnalysisId)}`);
  if (!response.ok) throw new Error(`frame_analysis_fetch_failed:${response.status}`);
  const frame = await response.json() as FrameAnalysisCameraRecord;
  applyFrameAnalysisCameraRecord(
    frame,
    frameAnalysisId,
    scenePackageId,
    camera,
    controls,
    afterApply,
  );
  return frame;
}
