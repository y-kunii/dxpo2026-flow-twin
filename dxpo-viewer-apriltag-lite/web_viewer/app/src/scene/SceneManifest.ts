export type AxisName = "X" | "Y" | "Z";

import type { FixedTagMap, DynamicObjectRegistry } from "../dynamic/DynamicTrackingContract";

export interface SceneManifest {
  schema_version: string;
  /** Immutable App A workspace identifier. Required by immutable v1.2+ Packages. */
  project_id?: string;
  scene_id: string;
  /** Immutable snapshot identifier. Required by immutable v1.2+ Packages. */
  scene_package_id?: string;
  unit: "meter";
  project_name?: string;
  coordinate_system: {
    right_axis: AxisName;
    up_axis: AxisName;
    forward_axis: AxisName;
    /** REP-103 world/body axes used by the external ROS adapter. */
    ros?: AxisContract;
    /** Three.js/Spark scene axes. +Z points backward from the default view. */
    viewer?: AxisContract;
    /** p_viewer = R_viewer_ros * p_ros + t_viewer_ros. */
    T_viewer_ros?: CoordinateTransform;
  };
  /** Package-owned runtime policy. It is not persisted in the runtime copy. */
  dynamic_overlay?: DynamicOverlayConfiguration;
  /** Portable application dataset. Required by self-contained v1.3 Packages. */
  dataset?: {
    manifest_uri: string;
    manifest_sha256: string;
  };
  assets: {
    splats: SplatAsset[];
    proxy_mesh: ProxyMeshAsset | null;
    human_models: HumanModelAsset[];
  };
  configs: {
    world_alignment: string;
    camera_presets: string;
    lens_presets: string;
    mount_candidates: string;
  };
  annotations: {
    measurements: string;
    visibility_results: string;
    apriltag_corners_world: string;
    scene_objects?: string;
    fixed_tag_map?: string;
    dynamic_objects?: string;
  };
  licenses: {
    notice: string;
  };
  provenance?: {
    decoder_coordinate_contract?: {
      id?: string;
      rotation_quaternion_xyzw?: [number, number, number, number];
      annotation_source?: string;
    };
    metric_tracking?: {
      status: "verified" | "unverified";
      reason?: string | null;
    };
  };
}

export interface AxisContract {
  handedness: "right";
  x: string;
  y: string;
  z: string;
}

export interface CoordinateTransform {
  translation: [number, number, number];
  rotation_xyzw: [number, number, number, number];
}

export interface DynamicOverlayConfiguration {
  topic: "/box/relative_transform";
  freshness: {
    timeout_ms: number;
    on_timeout: "hide";
    ordering: "ros_stamp";
  };
}

export type { FixedTagMap, DynamicObjectRegistry };

export interface SplatAsset {
  id: string;
  role: "overview" | "detail";
  uri: string;
  lod: boolean;
  paged: boolean;
  source_format?: "spz" | "ply" | "rad";
  runtime_format?: "spz" | "ply" | "rad";
  coordinate_stage?: string;
  sha256?: string;
  metadata_uri?: string;
}

export interface ProxyMeshAsset {
  uri: string;
  purpose: "measurement_and_occlusion";
  coordinate_space: "world";
  metadata_uri: string;
  quality_uri: string;
}

export interface ProxyQuality {
  status: "verified" | "unverified" | "failed";
  proxy_kind?: "semantic" | "surface_mesh";
  capabilities?: {
    measurement: boolean;
    surface_placement: boolean;
    fov: boolean;
    occlusion: boolean;
    semantic_object_query: boolean;
  };
  vertex_count?: number;
  triangle_count?: number;
  finite_vertex_count?: number;
  nan_vertex_count?: number;
  inf_vertex_count?: number;
  degenerate_triangle_count?: number;
  connected_component_count?: number;
  largest_component_ratio?: number;
  mesh_coverage_ratio?: number;
  bounding_box_dimension_ratio?: number[] | null;
  sampled_dense_to_mesh_distance?: { sample_count: number; threshold_m: number };
  mean_surface_distance_m?: number | null;
  median_surface_distance_m?: number | null;
  max_surface_distance_m?: number | null;
  surface_area_m2?: number;
  glb_write_result?: { status: "verified" | "failed"; byte_length: number };
  viewer_load_result?: "verified" | "failed" | "not_evaluated" | { status: "verified" | "failed" | "not_evaluated"; reason?: string };
  failure_reasons?: string[];
  warnings?: string[];
  provenance?: { semantic_proxy_status?: string; [key: string]: unknown };
  proxy_quality?: {
    detected_counts: Record<string, number>;
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

export interface HumanModelAsset {
  id: string;
  uri: string;
  height_m: number;
}

export interface WorldAlignment {
  unit: "meter";
  status: "verified" | "unverified";
  generated_to_world: {
    scale: number;
    rotation_quaternion_xyzw: [number, number, number, number];
    translation_m: [number, number, number];
  };
}

export interface AprilTagWorldCornerSet {
  family: string;
  id: number;
  physical_side_m: number;
  corners_world_m: Array<{
    corner_index: number;
    xyz_world_m: [number, number, number];
    observation_count: number;
    mean_ray_distance_colmap: number;
  }>;
  sides: Array<{ corners: [number, number]; distance_m: number }>;
  diagonals: Array<{ corners: [number, number]; distance_m: number }>;
}

/**
 * v1.2 can carry several independent fixed AprilTag anchors.  The legacy
 * top-level set remains the primary compatibility view for v1.0/v1.1
 * packages; new consumers use ``tags`` when it is present.
 */
export interface AprilTagWorldCorners extends AprilTagWorldCornerSet {
  schema_version: string;
  tags?: AprilTagWorldCornerSet[];
}

export function aprilTagCornerSets(value: AprilTagWorldCorners): AprilTagWorldCornerSet[] {
  return Array.isArray(value.tags) && value.tags.length > 0 ? value.tags : [value];
}
