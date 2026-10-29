import {
  advanceDynamicTrackingCollectionState,
  advanceDynamicTrackingState,
  boxGeometryDimensions,
  composePose,
  createDefaultDynamicRegistry,
  createRuntimeDynamicBoxConfig,
  convertRosRelativePoseToViewer,
  createDynamicTrackingCollectionState,
  createDynamicTrackingRuntimeState,
  createVirtualTransformStamped,
  deriveFixedTagMapFromAprilTagCorners,
  distanceM,
  invertPose,
  isDynamicRejection,
  normalizeBoxDimensions,
  isRuntimeDynamicBoxId,
  processDynamicTrackingMessage,
  processDynamicTrackingCollectionMessage,
  selectVirtualFixedTag,
  transitionDynamicTrackingConnection,
  validateDynamicAnnotations,
  type DynamicObjectRegistry,
  type FixedTagMap,
  type PoseTransform,
} from "../src/dynamic/DynamicTrackingContract.js";
import { assessDynamicBoxCongestion } from "../src/dynamic/DynamicBoxCongestion.js";
import { validateImmutableCoordinateContract } from "../src/scene/SceneLoader.js";
import type { SceneManifest } from "../src/scene/SceneManifest.js";

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}

function assertEqual(actual: unknown, expected: unknown, message: string): void {
  assert(actual === expected, `${message}: ${String(actual)} !== ${String(expected)}`);
}

function assertNear(actual: number, expected: number, tolerance: number, message: string): void {
  assert(Math.abs(actual - expected) <= tolerance, `${message}: ${actual} != ${expected} (±${tolerance})`);
}

function assertVector(actual: number[], expected: number[], message: string, tolerance = 1e-9): void {
  assert(actual.length === expected.length && actual.every((value, index) => Math.abs(value - expected[index]) <= tolerance),
    `${message}: ${JSON.stringify(actual)} != ${JSON.stringify(expected)}`);
}

const identity: PoseTransform = {
  translation_m: [0, 0, 0],
  rotation_xyzw: [0, 0, 0, 1],
};
const yaw90: PoseTransform = {
  translation_m: [0, 0, 0],
  rotation_xyzw: [0, Math.SQRT1_2, 0, Math.SQRT1_2],
};
const viewerFromRos: PoseTransform = {
  translation_m: [0, 0, 0],
  // ROS (+X forward,+Y left,+Z up) -> Viewer (+X right,+Y up,+Z backward).
  rotation_xyzw: [-0.5, 0.5, 0.5, 0.5],
};

const v12CoordinateManifest = {
  schema_version: "1.2.0",
  project_id: "project_fixture",
  scene_package_id: "package_fixture",
  coordinate_system: {
    right_axis: "X", up_axis: "Y", forward_axis: "Z",
    ros: { handedness: "right", x: "forward", y: "left", z: "up" },
    viewer: { handedness: "right", x: "right", y: "up", z: "backward" },
    T_viewer_ros: { translation: [0, 0, 0], rotation_xyzw: [-0.5, 0.5, 0.5, 0.5] },
  },
  dynamic_overlay: { topic: "/box/relative_transform", freshness: { timeout_ms: 750, on_timeout: "hide", ordering: "ros_stamp" } },
} as unknown as SceneManifest;
assertEqual(validateImmutableCoordinateContract(v12CoordinateManifest), undefined, "Viewer accepts a non-500 valid package timeout and unit T_viewer_ros quaternion");
for (const invalidQuaternion of [[0, 0, 0, 0], [0, 0, 0, 2], [Number.NaN, 0, 0, 1], [Infinity, 0, 0, 1]]) {
  const candidate = structuredClone(v12CoordinateManifest);
  candidate.coordinate_system.T_viewer_ros!.rotation_xyzw = invalidQuaternion as [number, number, number, number];
  assert(Boolean(validateImmutableCoordinateContract(candidate)), "Viewer rejects an invalid T_viewer_ros quaternion");
}

const fixedTagMap: FixedTagMap = {
  schema_version: "1.0.0",
  coordinate_frame: "gs_world",
  unit: "meter",
  rotation_order: "xyzw",
  metric_quality: { status: "verified", quality_gate: { status: "verified" } },
  frames: {
    apriltag_tag25h9_0: identity,
    apriltag_tag25h9_1: { translation_m: [1, 0, 0], rotation_xyzw: [0, 0, 0, 1] },
  },
};

const registry: DynamicObjectRegistry = {
  schema_version: "1.0.0",
  coordinate_frame: "gs_world",
  unit: "meter",
  rotation_order: "xyzw",
  objects: [{
    id: "box_01",
    box_tag_frame: "apriltag_tag25h9_20",
    model: { uri: "assets/dynamic/box_01.glb", format: "glb", sha256: "0".repeat(64) },
    // T_boxTag_boxTracking is already applied by the virtual camera-side
    // generator.  This term documents the wire transform's semantic source.
    box_tag_to_object: identity,
    // T_boxTracking_boxModel: an explicit +X render-frame offset proves it is
    // composed after T_world_boxTracking.
    object_to_render: { translation_m: [0.1, 0, 0], rotation_xyzw: [0, 0, 0, 1] },
    max_translation_jump_m: 0.5,
    max_rotation_jump_deg: 45,
  }],
};

const objectTimeoutDocument = {
  ...registry,
  objects: [{ ...registry.objects[0], timeout_ms: 1_000 }],
};
const rejectedV12ObjectTimeout = validateDynamicAnnotations(
  fixedTagMap,
  objectTimeoutDocument,
  "1.2.0",
);
assert(rejectedV12ObjectTimeout.status === "rejected" && rejectedV12ObjectTimeout.reason?.includes("timeout_ms"),
  "v1.2 dynamic object timeout is rejected instead of retained");
const legacyObjectTimeout = validateDynamicAnnotations(fixedTagMap, objectTimeoutDocument, "1.1.0");
assert(legacyObjectTimeout.status === "verified", "v1.1 dynamic object timeout remains readable for compatibility");

function message(
  frameId = "apriltag_tag25h9_0",
  stampMs = 1_000,
  translation: [number, number, number] = [0, 0, 0],
  rotation: [number, number, number, number] = [0, 0, 0, 1],
  childFrame = "box_01",
) {
  return {
    header: {
      stamp: { sec: Math.floor(stampMs / 1_000), nanosec: (stampMs % 1_000) * 1_000_000 },
      frame_id: frameId,
    },
    child_frame_id: childFrame,
    transform: { translation_m: translation, rotation_xyzw: rotation },
  };
}

function process(state = createDynamicTrackingRuntimeState(), value: unknown = message(), receiveMs = 0) {
  return processDynamicTrackingMessage(value, fixedTagMap, registry, state, receiveMs);
}

assertEqual(assessDynamicBoxCongestion(0).severity, "normal", "zero boxes are normal");
assertEqual(assessDynamicBoxCongestion(1).severity, "normal", "one box is normal");
assertEqual(assessDynamicBoxCongestion(2).severity, "caution", "two boxes require caution");
assertEqual(assessDynamicBoxCongestion(3).severity, "warning", "three boxes require warning");
assertEqual(assessDynamicBoxCongestion(4).severity, "alert", "four boxes require alert");
assertEqual(assessDynamicBoxCongestion(9).severity, "alert", "more than four boxes remain alert");

// Named dimensions are canonical. Legacy [width, depth, height] is accepted
// only at the read boundary, and Three.js receives [width, height, depth].
const namedDimensions = normalizeBoxDimensions({ width_m: 0.5, depth_m: 0.4, height_m: 0.3 });
assert(Boolean(namedDimensions), "named cardboard dimensions normalize");
assertVector(boxGeometryDimensions(namedDimensions!), [0.5, 0.3, 0.4], "BoxGeometry width/height/depth");
const legacyDimensions = normalizeBoxDimensions([0.5, 0.4, 0.3]);
assert(Boolean(legacyDimensions), "legacy dimensions normalize at read boundary");
assertVector(boxGeometryDimensions(legacyDimensions!), [0.5, 0.3, 0.4], "legacy width/depth/height order");
const defaultRegistry = createDefaultDynamicRegistry();
assertEqual(defaultRegistry.objects.length, 0, "default registry does not pre-register physical boxes");
assert(isRuntimeDynamicBoxId("box_01") && isRuntimeDynamicBoxId("box_37"), "positive box IDs are admitted at runtime");
assert(!isRuntimeDynamicBoxId("box_0") && !isRuntimeDynamicBoxId("apriltag_tag25h9_20"), "non-box child IDs stay fail-closed");
const defaultCube = createRuntimeDynamicBoxConfig("box_37").model;
assert(defaultCube.format === "cube", "runtime dynamic object uses a cube fallback");
if (defaultCube.format === "cube") {
  assertVector([defaultCube.dimensions_m.width_m, defaultCube.dimensions_m.depth_m, defaultCube.dimensions_m.height_m], [0.5, 0.4, 0.3], "120-size cardboard dimensions");
assertNear(defaultCube.dimensions_m.height_m / 2, 0.15, 1e-9, "cube center lies 0.15m above its base");
}
assertNear(distanceM([0, 0, 0], [0.3, 0.4, 0]), 0.5, 1e-9,
  "fixed-tag to box distance uses the Metric Scene meter world");
const convertedRosPose = convertRosRelativePoseToViewer({
  translation_m: [1, 0, 0], rotation_xyzw: [0, 0, 0, 1],
}, viewerFromRos);
assertVector(convertedRosPose.translation_m, [0, 0, -1], "T_viewer_ros converts ROS forward to Viewer backward");
const yawedBasis = composePose(yaw90, { translation_m: [1, 0, 0], rotation_xyzw: [0, 0, 0, 1] });
assertVector(yawedBasis.translation_m, [0, 0, -1], "90 degree Y rotation maps the +X basis to -Z without scaling translation");
const emptyOverrides = validateDynamicAnnotations(fixedTagMap, createDefaultDynamicRegistry());
assert(emptyOverrides.status === "verified" && emptyOverrides.registry?.objects.length === 0,
  "an empty dynamic annotation registry is a valid override set");

// Pure camera-side generator: inverse(T_camera_fixed) * T_camera_box * T_boxTag_boxTracking.
const virtual = createVirtualTransformStamped({
  camera_fixed_tag: { translation_m: [1, 0, 0], rotation_xyzw: [0, 0, 0, 1] },
  camera_box_tag: { translation_m: [1.3, 0.2, 0], rotation_xyzw: [0, 0, 0, 1] },
  box_tag_to_box_tracking: { translation_m: [0, 0.1, 0], rotation_xyzw: [0, 0, 0, 1] },
  source_stamp: { sec: 1, nanosec: 0 },
  fixed_tag_frame: "apriltag_tag25h9_0",
  box_child_frame: "box_01",
});
assert(!isDynamicRejection(virtual), "virtual camera-side generator emits TransformStamped");
if (!isDynamicRejection(virtual)) {
  assertVector(virtual.transform.translation_m, [0.3, 0.3, 0], "camera-side transform composition");
  assertNear(Math.hypot(...virtual.transform.rotation_xyzw), 1, 1e-9, "generator quaternion normalized");
}

const inverse = invertPose({ translation_m: [1, -2, 3], rotation_xyzw: [0, 0, 0, 1] });
const inverseIdentity = composePose({ translation_m: [1, -2, 3], rotation_xyzw: [0, 0, 0, 1] }, inverse);
assertVector(inverseIdentity.translation_m, [0, 0, 0], "pose inverse translation");
assertVector(inverseIdentity.rotation_xyzw, [0, 0, 0, 1], "pose inverse rotation");

// Candidate choice is deterministic and its only hysteresis is a quality/area tie.
const selected = selectVirtualFixedTag([
  { frame_id: "apriltag_tag25h9_1", hamming: 0, decision_margin: 20, image_area_px2: 100 },
  { frame_id: "apriltag_tag25h9_0", hamming: 0, decision_margin: 20, image_area_px2: 100 },
], 10, "apriltag_tag25h9_1");
assert(!isDynamicRejection(selected) && selected.frame_id === "apriltag_tag25h9_1", "virtual fixed-tag hysteresis chooses previous quality tie");
const areaWinner = selectVirtualFixedTag([
  { frame_id: "apriltag_tag25h9_1", hamming: 0, decision_margin: 20, image_area_px2: 100 },
  { frame_id: "apriltag_tag25h9_0", hamming: 0, decision_margin: 20, image_area_px2: 101 },
], 10, "apriltag_tag25h9_1");
assert(!isDynamicRejection(areaWinner) && areaWinner.frame_id === "apriltag_tag25h9_0", "larger image area wins before hysteresis");

// Tracking result applies the renderer model transform after the Metric world pose.
let state = createDynamicTrackingRuntimeState();
let result = process(state, message("apriltag_tag25h9_0", 1_000, [0, 0, 0]), 10);
assert(result.resolution?.status === "accepted", "identity tracking message accepted");
state = result.state;
assertVector(state.last_pose!.translation_m, [0.1, 0, 0], "T_world_boxModel applies object-to-render offset");
assertEqual(state.tracking_state, "tracking", "accepted state is tracking");
const modelAxisRegistry: DynamicObjectRegistry = {
  ...registry,
  objects: [{ ...registry.objects[0], object_to_render: yaw90, max_rotation_jump_deg: 180 }],
};
const modelAxis = processDynamicTrackingMessage(
  message("apriltag_tag25h9_0", 1_000, [0, 0, 0]), fixedTagMap, modelAxisRegistry,
  createDynamicTrackingRuntimeState(), 10,
);
assert(modelAxis.resolution?.status === "accepted", "non-identity box tracking to model transform accepted");
assertNear(Math.abs(modelAxis.state.last_pose!.rotation_xyzw[1]), Math.SQRT1_2, 1e-9,
  "T_boxTracking_boxModel rotates the render model around +Y");

result = process(state, message("apriltag_tag25h9_0", 1_050, [0.1, 0, 0]), 60);
assert(result.resolution?.status === "accepted", "world +X 10cm accepted");
state = result.state;
assertVector(state.last_pose!.translation_m, [0.2, 0, 0], "world +X 10cm");
result = process(state, message("apriltag_tag25h9_0", 1_100, [0.1, 0, 0.1]), 110);
assert(result.resolution?.status === "accepted", "world +Z 10cm accepted");
state = result.state;
assertVector(state.last_pose!.translation_m, [0.2, 0, 0.1], "world +Z 10cm");
result = process(state, message("apriltag_tag25h9_0", 1_150, [0.1, 0.1, 0.1]), 160);
assert(result.resolution?.status === "accepted", "world +Y 10cm accepted");
state = result.state;
assertVector(state.last_pose!.translation_m, [0.2, 0.1, 0.1], "world +Y 10cm");
const yawResult = process(createDynamicTrackingRuntimeState(), message("apriltag_tag25h9_0", 1_200, [0.1, 0.1, 0.1], yaw90.rotation_xyzw), 210);
assert(yawResult.resolution?.status === "accepted", "world +Y yaw 90deg accepted");
assertNear(Math.abs(yawResult.state.last_pose!.rotation_xyzw[1]), Math.SQRT1_2, 1e-9, "yaw uses +Y axis and xyzw order");

// A tag 0 -> tag 1 handoff must preserve the reconstructed world pose.
const switchRegistry: DynamicObjectRegistry = { ...registry, objects: [{ ...registry.objects[0], object_to_render: identity, max_rotation_jump_deg: 180 }] };
let switchState = createDynamicTrackingRuntimeState();
const tag0WorldBox = processDynamicTrackingMessage(message("apriltag_tag25h9_0", 2_000, [0.3, 0.1, -0.2]), fixedTagMap, switchRegistry, switchState, 10);
assert(tag0WorldBox.resolution?.status === "accepted", "tag 0 fixed-map message accepted");
switchState = tag0WorldBox.state;
const tag1WorldBox = processDynamicTrackingMessage(message("apriltag_tag25h9_1", 2_050, [-0.7, 0.1, -0.2]), fixedTagMap, switchRegistry, switchState, 60);
assert(tag1WorldBox.resolution?.status === "accepted", "tag 1 fixed-map message accepted");
assertVector(tag0WorldBox.state.last_pose!.translation_m, tag1WorldBox.state.last_pose!.translation_m, "fixed tag switch preserves world translation", 1e-6);
assertVector(tag0WorldBox.state.last_pose!.rotation_xyzw, tag1WorldBox.state.last_pose!.rotation_xyzw, "fixed tag switch preserves world rotation", 1e-6);
assertEqual(tag1WorldBox.state.active_fixed_tag_frame, "apriltag_tag25h9_1", "active tag records tag switch");

// Interleaved boxes have fully independent source stamps, jump checks and
// receive-time expiry. In particular, a far-away box_02 is not a jump for
// box_01, and both may legitimately use the same ROS timestamp.
const multiRegistry: DynamicObjectRegistry = {
  ...switchRegistry,
  objects: [
    { ...switchRegistry.objects[0], id: "box_01" },
    {
      ...switchRegistry.objects[0],
      id: "box_02",
      box_tag_frame: "apriltag_tag25h9_21",
      model: { format: "cube", dimensions_m: { width_m: 0.5, depth_m: 0.4, height_m: 0.3 }, color_hex: "#4fa8ff" },
    },
  ],
};
let multiState = createDynamicTrackingCollectionState(multiRegistry);
const firstBox = processDynamicTrackingCollectionMessage(
  message("apriltag_tag25h9_0", 3_000, [0.1, 0, 0], [0, 0, 0, 1], "box_01"),
  fixedTagMap, multiRegistry, multiState, 10,
);
assert(firstBox.resolution?.status === "accepted", "first simultaneous box accepted");
multiState = firstBox.state;
const secondBox = processDynamicTrackingCollectionMessage(
  message("apriltag_tag25h9_1", 3_000, [2.1, 0, 0], [0, 0, 0, 1], "box_02"),
  fixedTagMap, multiRegistry, multiState, 20,
);
assert(secondBox.resolution?.status === "accepted", "second simultaneous box accepts matching timestamp independently");
multiState = secondBox.state;
assertVector(multiState.object_states.box_01.last_pose!.translation_m, [0.1, 0, 0], "box_01 pose retained");
assertVector(multiState.object_states.box_02.last_pose!.translation_m, [3.1, 0, 0], "box_02 uses its own fixed tag and pose");
const firstBoxUpdate = processDynamicTrackingCollectionMessage(
  message("apriltag_tag25h9_0", 3_050, [0.2, 0, 0], [0, 0, 0, 1], "box_01"),
  fixedTagMap, multiRegistry, multiState, 30,
);
assert(firstBoxUpdate.resolution?.status === "accepted", "box_01 jump check ignores box_02 pose");
multiState = firstBoxUpdate.state;
const runtimeBox = processDynamicTrackingCollectionMessage(
  message("apriltag_tag25h9_0", 3_100, [0, 0, 0], [0, 0, 0, 1], "box_99"),
  fixedTagMap, multiRegistry, multiState, 40,
);
assert(runtimeBox.resolution?.status === "accepted", "well-formed unknown box ID is dynamically admitted");
assertVector(runtimeBox.state.object_states.box_01.last_pose!.translation_m, [0.2, 0, 0], "runtime box never changes box_01");
assertVector(runtimeBox.state.object_states.box_02.last_pose!.translation_m, [3.1, 0, 0], "runtime box never changes box_02");
assertEqual(runtimeBox.state.object_states.box_99.tracking_state, "tracking", "runtime box has independent tracking state");
const malformedRuntimeBox = processDynamicTrackingCollectionMessage(
  message("apriltag_tag25h9_0", 3_101, [0, 0, 0], [0, 0, 0, 1], "box_tag_20"),
  fixedTagMap, multiRegistry, runtimeBox.state, 41,
);
assert(malformedRuntimeBox.rejection?.reason === "unknown_dynamic_object", "malformed dynamic ID rejected fail-closed");
let capacityState = createDynamicTrackingCollectionState(createDefaultDynamicRegistry());
for (let index = 1; index <= 64; index += 1) {
  const accepted = processDynamicTrackingCollectionMessage(
    message("apriltag_tag25h9_0", 4_000 + index, [index / 100, 0, 0], [0, 0, 0, 1], `box_${index}`),
    fixedTagMap, createDefaultDynamicRegistry(), capacityState, index,
  );
  assert(accepted.resolution?.status === "accepted", `runtime box ${index} accepted within live-object limit`);
  capacityState = accepted.state;
}
const overCapacity = processDynamicTrackingCollectionMessage(
  message("apriltag_tag25h9_0", 4_100, [0, 0, 0], [0, 0, 0, 1], "box_65"),
  fixedTagMap, createDefaultDynamicRegistry(), capacityState, 65,
);
assert(overCapacity.rejection?.reason === "dynamic_object_limit_exceeded", "runtime live-object capacity is fail-closed");
const multiExpired = advanceDynamicTrackingCollectionState(multiState, multiRegistry, 135, 100);
assertEqual(multiExpired.object_states.box_02.tracking_state, "lost", "box_02 uses the package timeout");
assertEqual(multiExpired.object_states.box_01.tracking_state, "lost", "box_01 uses the same package timeout");

// Rejections preserve the last accepted pose. Message and package boundary
// quaternions must already be normalized; they are never silently repaired.
const beforeReject = state.last_pose!;
const rejectionCases: Array<{ id: string; value: unknown; reason: string }> = [
  { id: "unknown_fixed_tag", value: message("apriltag_tag25h9_99", 1_250), reason: "unknown_fixed_tag_frame" },
  { id: "wrong_child", value: message("apriltag_tag25h9_0", 1_250, [0, 0, 0], [0, 0, 0, 1], "apriltag_tag25h9_20"), reason: "invalid_child_frame" },
  { id: "translation_nan", value: message("apriltag_tag25h9_0", 1_250, [Number.NaN, 0, 0]), reason: "non_finite_translation" },
  { id: "translation_inf", value: message("apriltag_tag25h9_0", 1_250, [Infinity, 0, 0]), reason: "non_finite_translation" },
  { id: "quaternion_nan", value: message("apriltag_tag25h9_0", 1_250, [0.1, 0.1, 0.1], [Number.NaN, 0, 0, 1]), reason: "non_finite_quaternion" },
  { id: "quaternion_infinity", value: message("apriltag_tag25h9_0", 1_250, [0.1, 0.1, 0.1], [Infinity, 0, 0, 1]), reason: "non_finite_quaternion" },
  { id: "quaternion_zero", value: message("apriltag_tag25h9_0", 1_250, [0.1, 0.1, 0.1], [0, 0, 0, 0]), reason: "zero_quaternion" },
  { id: "quaternion_non_unit", value: message("apriltag_tag25h9_0", 1_250, [0.1, 0.1, 0.1], [0, 0, 0, 2]), reason: "non_unit_quaternion" },
  { id: "timestamp_regression", value: message("apriltag_tag25h9_0", 1_100), reason: "timestamp_regression" },
  { id: "large_timestamp_regression", value: message("apriltag_tag25h9_0", 1), reason: "timestamp_regression" },
  { id: "duplicate_timestamp", value: message("apriltag_tag25h9_0", 1_150), reason: "duplicate_timestamp" },
  { id: "translation_jump", value: message("apriltag_tag25h9_0", 1_250, [2, 0, 0]), reason: "translation_jump_exceeded" },
  { id: "rotation_jump", value: message("apriltag_tag25h9_0", 1_250, [0.1, 0.1, 0.1], [0, 1, 0, 0]), reason: "rotation_jump_exceeded" },
];
for (const testCase of rejectionCases) {
  const rejected = process(state, testCase.value, 220);
  assert(rejected.rejection?.reason === testCase.reason, `${testCase.id} rejection reason`);
  assertVector(rejected.state.last_pose!.translation_m, beforeReject.translation_m, `${testCase.id} preserves previous pose`);
}
result = process(state, message("apriltag_tag25h9_0", 1_250, [0.1, 0.1, 0.1], [0, 0, 0, 1]), 260);
assert(result.resolution?.status === "accepted", "unit quaternion remains accepted");
state = result.state;

// Source stamps only order messages. Receive monotonic time drives stale/lost.
let stale = advanceDynamicTrackingState(state, 411, 200);
assertEqual(stale.tracking_state, "stale", "receive age approaches timeout");
stale = advanceDynamicTrackingState(stale, 461, 200);
assertEqual(stale.tracking_state, "lost", "receive timeout loses tracking");
assert(Boolean(stale.last_pose), "timeout retains diagnostic pose in state while renderer hides it");

// A reconnect starts a fresh ordering generation because a ROS clock may
// reset. The receive-time freshness clock is cleared at the same boundary.
let reconnected = transitionDynamicTrackingConnection(state, "connecting");
assertEqual(reconnected.tracking_state, "waiting", "connecting enters waiting state");
assert(!reconnected.last_pose, "connecting clears prior pose");
const oldAfterReconnect = process(reconnected, message("apriltag_tag25h9_0", 1_250, [0.1, 0.1, 0.1]), 500);
assert(oldAfterReconnect.resolution?.status === "accepted", "first message after reconnect establishes new ordering generation");
const oldWithinSession = process(oldAfterReconnect.state, message("apriltag_tag25h9_0", 1, [0.1, 0.1, 0.1]), 550);
assertEqual(oldWithinSession.rejection?.reason, "timestamp_regression", "large ROS stamp regression is rejected within one connection");
const resetAfterReconnect = transitionDynamicTrackingConnection(oldAfterReconnect.state, "connecting");
const firstAfterExplicitReset = process(resetAfterReconnect, message("apriltag_tag25h9_0", 1, [0.1, 0.1, 0.1]), 600);
assert(firstAfterExplicitReset.resolution?.status === "accepted", "reconnect explicitly starts a new ordering generation");

const annotations = validateDynamicAnnotations(fixedTagMap, registry);
assert(annotations.status === "verified", "valid GLB dynamic annotations verify");
const cubeAnnotations = validateDynamicAnnotations(undefined, {
  ...registry,
  objects: [{ ...registry.objects[0], model: { format: "cube", dimensions_m: { width_m: 0.5, depth_m: 0.4, height_m: 0.3 } } }],
});
assert(cubeAnnotations.status === "verified", "named cube dynamic annotations verify");
const legacyCubeAnnotations = validateDynamicAnnotations(undefined, {
  ...registry,
  objects: [{ ...registry.objects[0], model: { format: "cube", dimensions_m: [0.5, 0.4, 0.3] } }],
});
assert(legacyCubeAnnotations.status === "verified", "legacy cube dimensions are normalized once at load");

const derivedMap = deriveFixedTagMapFromAprilTagCorners({
  family: "tag25h9",
  id: 0,
  corners_world_m: [
    { corner_index: 0, xyz_world_m: [0, 0, 0] },
    { corner_index: 1, xyz_world_m: [1, 0, 0] },
    { corner_index: 2, xyz_world_m: [1, 1, 0] },
    { corner_index: 3, xyz_world_m: [0, 1, 0] },
  ],
}, true);
assert(!isDynamicRejection(derivedMap), "verified AprilTag corners derive fixed tag map");
const multiTagDerivedMap = deriveFixedTagMapFromAprilTagCorners({
  tags: [
    {
      family: "tag25h9", id: 0,
      corners_world_m: [
        { corner_index: 0, xyz_world_m: [0, 0, 0] },
        { corner_index: 1, xyz_world_m: [1, 0, 0] },
        { corner_index: 2, xyz_world_m: [1, 1, 0] },
        { corner_index: 3, xyz_world_m: [0, 1, 0] },
      ],
    },
    {
      family: "tag25h9", id: 1,
      corners_world_m: [
        { corner_index: 0, xyz_world_m: [2, 0, 0] },
        { corner_index: 1, xyz_world_m: [3, 0, 0] },
        { corner_index: 2, xyz_world_m: [3, 1, 0] },
        { corner_index: 3, xyz_world_m: [2, 1, 0] },
      ],
    },
  ],
}, true);
assert(!isDynamicRejection(multiTagDerivedMap) && Object.keys(multiTagDerivedMap.frames).length === 2,
  "multiple verified AprilTags derive one fixed frame per anchor");
const degenerateMap = deriveFixedTagMapFromAprilTagCorners({
  family: "tag25h9", id: 0,
  corners_world_m: [0, 1, 2, 3].map((corner_index) => ({ corner_index, xyz_world_m: [corner_index, 0, 0] })),
});
assert(isDynamicRejection(degenerateMap) && degenerateMap.reason === "invalid_fixed_tag_geometry", "degenerate tag geometry rejected");

console.log("dynamic tracking contract tests: ok");
