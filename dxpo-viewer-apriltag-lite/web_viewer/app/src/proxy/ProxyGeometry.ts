import * as THREE from "three";
import type { CameraPreset } from "../scene/CameraPresets";

export interface ProxyObjectMetadata {
  id: string;
  semantic_class: "floor" | "wall" | "ceiling" | "large_obstacle";
  centroid: [number, number, number];
  normal: [number, number, number] | null;
  bounds_m: { min: [number, number, number]; max: [number, number, number] };
  support_count: number;
}

export interface ProxyMetadata {
  schema_version: string;
  unit: "meter";
  coordinate_system: { up_axis: "Y" };
  generation_parameters?: {
    surface_placement?: {
      horizontal_threshold: number;
      common_offset_m: number;
    };
    [key: string]: unknown;
  };
  objects?: ProxyObjectMetadata[];
  quality_metrics?: {
    dense_explanation_ratio: number;
    unexplained_point_ratio: number;
    surface_residual_m: { mean: number; median: number; max: number };
    valid_contact_count: number;
    invalid_penetration_count: number;
    overlapping_surface_count: number;
    wall_coverage_ratio: number;
    wall_height_coverage_ratio: number;
  };
}

export function createProxyPlacementPresets(metadata: ProxyMetadata): CameraPreset[] {
  const presets: CameraPreset[] = [];
  const objects = metadata.objects ?? [];
  for (const semanticClass of ["floor", "wall", "ceiling"] as const) {
    const candidates = objects.filter((item) => item.semantic_class === semanticClass);
    const item = candidates.sort((a, b) => b.support_count - a.support_count)[0];
    if (!item) continue;

    const center = new THREE.Vector3(...item.centroid);
    const normal = item.normal ? new THREE.Vector3(...item.normal).normalize() : new THREE.Vector3(0, 1, 0);
    let position: THREE.Vector3;
    if (semanticClass === "floor") {
      position = center.clone().add(new THREE.Vector3(0, 1.5, 0));
    } else if (semanticClass === "ceiling") {
      position = center.clone().add(new THREE.Vector3(0, -1.5, 0));
    } else {
      position = center.clone().sub(normal.multiplyScalar(1.5));
    }

    presets.push({
      id: `proxy_${semanticClass}`,
      name: `Proxy ${semanticClass} placement`,
      position_m: [position.x, position.y, position.z],
      look_at_m: [center.x, center.y, center.z],
      up_world: [0, 1, 0],
      vertical_fov_deg: 50,
      resolution: [1600, 1000]
    });
  }
  return presets;
}

export function checkProxyOcclusion(
  proxy: THREE.Object3D,
  origin: THREE.Vector3,
  target: THREE.Vector3
): boolean {
  return evaluateProxyVisibility(proxy, origin, target).status === "occluded";
}

export interface ProxyVisibilityResult {
  camera_id: string;
  target_id: string;
  status: "visible" | "occluded" | "unavailable";
  blocking_object_id: "surface_proxy" | null;
  hit_distance_m: number | null;
  target_distance_m: number;
}

export function evaluateProxyVisibility(
  proxy: THREE.Object3D,
  origin: THREE.Vector3,
  target: THREE.Vector3,
  cameraId = "viewer_camera",
  targetId = "viewer_target"
): ProxyVisibilityResult {
  const direction = target.clone().sub(origin);
  const distance = direction.length();
  if (!Number.isFinite(distance) || distance <= 0) {
    return {
      camera_id: cameraId,
      target_id: targetId,
      status: "unavailable",
      blocking_object_id: null,
      hit_distance_m: null,
      target_distance_m: Math.max(0, distance),
    };
  }
  direction.normalize();
  const raycaster = new THREE.Raycaster(origin, direction, 0.01, Math.max(0.01, distance - 0.01));
  const hit = raycaster.intersectObject(proxy, true)[0];
  return {
    camera_id: cameraId,
    target_id: targetId,
    status: hit ? "occluded" : "visible",
    blocking_object_id: hit ? "surface_proxy" : null,
    hit_distance_m: hit?.distance ?? null,
    target_distance_m: distance,
  };
}
