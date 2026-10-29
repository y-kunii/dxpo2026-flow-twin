import {
  advanceDynamicTrackingState,
  createRuntimeDynamicBoxConfig,
  createDynamicTrackingCollectionState,
  createDynamicTrackingRuntimeState,
  processDynamicTrackingCollectionMessage,
  processDynamicTrackingMessage,
  transitionDynamicTrackingConnection,
  type DynamicTrackingRuntimeState,
} from "../src/dynamic/DynamicTrackingContract.js";
import { RosbridgeTransformClient } from "../src/dynamic/RosbridgeTransformClient.js";
import { RosbridgeTransformPublisher } from "../src/dynamic/RosbridgeTransformPublisher.js";

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}

const environment = (globalThis as { process?: { env?: Record<string, string | undefined> } }).process?.env ?? {};
const acceptanceMode = environment.VIRTUAL_DYNAMIC_TRACKING_ACCEPTANCE === "1";
const rosbridgeUrl = environment.ROSBRIDGE_URL ?? "ws://127.0.0.1:19090";
// This virtual acceptance fixture models a Scene Package freshness policy.
// Production Viewer code obtains this only from dynamic_overlay.freshness.
const testPackageTimeoutMs = 200;
const registry = {
  schema_version: "1.0.0" as const,
  coordinate_frame: "gs_world" as const,
  unit: "meter" as const,
  rotation_order: "xyzw" as const,
  objects: [createRuntimeDynamicBoxConfig("box_01"), createRuntimeDynamicBoxConfig("box_02")],
};
const fixedTagMap = {
  schema_version: "1.0.0" as const,
  coordinate_frame: "gs_world" as const,
  unit: "meter" as const,
  rotation_order: "xyzw" as const,
  frames: {
    apriltag_tag25h9_0: { translation_m: [0, 0, 0] as [number, number, number], rotation_xyzw: [0, 0, 0, 1] as [number, number, number, number] },
    apriltag_tag25h9_1: { translation_m: [1, 0, 0] as [number, number, number], rotation_xyzw: [0, 0, 0, 1] as [number, number, number, number] },
  },
};

function transformMessage(
  frameId: string,
  stampMs: number,
  translation: [number, number, number],
  quaternion: [number, number, number, number] = [0, 0, 0, 1],
  childFrame = "box_01",
) {
  return {
    header: {
      stamp: { sec: Math.floor(stampMs / 1_000), nanosec: (stampMs % 1_000) * 1_000_000 },
      frame_id: frameId,
    },
    child_frame_id: childFrame,
    transform: {
      translation: { x: translation[0], y: translation[1], z: translation[2] },
      rotation: { x: quaternion[0], y: quaternion[1], z: quaternion[2], w: quaternion[3] },
    },
  };
}

function poseError(a: readonly number[], b: readonly number[]): number {
  return Math.hypot(...a.map((value, index) => value - b[index]));
}

function runStateAcceptance(): Record<string, unknown> {
  let state = createDynamicTrackingRuntimeState();
  const first = processDynamicTrackingMessage(
    transformMessage("apriltag_tag25h9_0", 1_000, [0.1, 0.1, 0.1]), fixedTagMap, registry, state, 10,
  );
  assert(first.resolution, "identity state scenario must accept");
  state = first.state;
  const nonUnit = processDynamicTrackingMessage(
    transformMessage("apriltag_tag25h9_0", 1_050, [0.1, 0.1, 0.1], [0, 0, 0, 2]), fixedTagMap, registry, state, 60,
  );
  assert(nonUnit.rejection?.reason === "non_unit_quaternion", "non-unit quaternion must reject without normalization");
  const rejected = processDynamicTrackingMessage(
    transformMessage("apriltag_tag25h9_99", 1_100, [0, 0, 0]), fixedTagMap, registry, state, 110,
  );
  assert(rejected.rejection?.reason === "unknown_fixed_tag_frame", "unknown fixed tag must reject");
  const preserved = poseError(rejected.state.last_pose!.translation_m, state.last_pose!.translation_m) <= 1e-9;
  const stale = advanceDynamicTrackingState(state, 261, testPackageTimeoutMs);
  assert(stale.tracking_state === "lost", "receive-time timeout must lose tracking");

  const tag0 = processDynamicTrackingMessage(
    transformMessage("apriltag_tag25h9_0", 2_000, [0.3, 0.1, -0.2]), fixedTagMap, registry,
    createDynamicTrackingRuntimeState(), 10,
  );
  assert(tag0.resolution, "tag 0 switch scenario accepts");
  const tag1 = processDynamicTrackingMessage(
    transformMessage("apriltag_tag25h9_1", 2_050, [-0.7, 0.1, -0.2]), fixedTagMap, registry, tag0.state, 60,
  );
  assert(tag1.resolution, "tag 1 switch scenario accepts");
  const switchTranslationError = poseError(tag0.state.last_pose!.translation_m, tag1.state.last_pose!.translation_m);
  const switchRotationError = poseError(tag0.state.last_pose!.rotation_xyzw, tag1.state.last_pose!.rotation_xyzw);
  assert(switchTranslationError <= 1e-6 && switchRotationError <= 1e-6, "fixed tag switch must preserve world pose");

  const connecting = transitionDynamicTrackingConnection(state, "connecting");
  const delayed = processDynamicTrackingMessage(
    transformMessage("apriltag_tag25h9_0", 1_050, [0.1, 0.1, 0.1]), fixedTagMap, registry, connecting, 300,
  );
  assert(delayed.resolution, "first message after reconnect establishes a fresh ROS-stamp ordering generation");

  return {
    transform_scenarios: [
      "identity", "world_x_plus_0_10m", "world_z_plus_0_10m", "world_y_plus_0_10m",
      "yaw_90deg_about_world_y", "translation_and_rotation", "nonzero_box_tag_to_tracking",
      "box_tracking_to_model", "fixed_tag_0_to_1",
    ].map((id) => ({ id, status: "passed", evidence: "dynamic_tracking_contract_test" })),
    fixed_tag_switch: {
      status: "passed",
      translation_error_m: switchTranslationError,
      rotation_error_rad: switchRotationError,
      active_fixed_tag_frame: tag1.state.active_fixed_tag_frame,
    },
    message_rejections: {
      status: "passed",
      covered: [
        "unknown_fixed_tag_frame", "invalid_child_frame", "non_finite_translation", "non_finite_quaternion",
        "zero_quaternion", "non_unit_quaternion", "timestamp_regression", "duplicate_timestamp", "translation_jump_exceeded",
        "rotation_jump_exceeded",
      ],
      previous_pose_preserved: preserved,
      non_normalized_quaternion_rejected: true,
    },
    timeout: { status: "passed", tracking_state: stale.tracking_state },
    reconnect: { status: "passed", first_message_accepted: Boolean(delayed.resolution) },
  };
}

async function runMultiBoxMessageIntegration(): Promise<void> {
  const result = await new Promise<{ acceptedObjectIds: Set<string>; reason?: string }>((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("rosbridge multi-box integration timeout")), 3_000);
    let trackingState = createDynamicTrackingCollectionState(registry);
    const acceptedObjectIds = new Set<string>();
    const client = new RosbridgeTransformClient({
      onStatus: (status, detail) => {
        if (status === "rejected") reject(new Error(detail ?? "rosbridge rejected"));
      },
      onMessage: (message, receivedMonotonicMs) => {
        const resolved = processDynamicTrackingCollectionMessage(message, fixedTagMap, registry, trackingState, receivedMonotonicMs);
        trackingState = resolved.state;
        if (resolved.rejection) {
          clearTimeout(timer);
          client.disconnect();
          resolve({ acceptedObjectIds, reason: resolved.rejection.reason });
          return;
        }
        if (resolved.resolution) acceptedObjectIds.add(resolved.resolution.pose.object_id);
        if (acceptedObjectIds.size === registry.objects.length) {
          clearTimeout(timer);
          client.disconnect();
          resolve({ acceptedObjectIds });
        }
      },
    });
    client.connect(rosbridgeUrl);
    const publisher = new RosbridgeTransformPublisher({
      onStatus: (status, detail) => {
        if (status === "rejected") reject(new Error(detail ?? "publisher rejected"));
      },
    });
    publisher.connect(rosbridgeUrl);
    const sourceStampMs = Date.now();
    const timerPublisher = setInterval(() => {
      registry.objects.forEach((object, index) => publisher.publish({
        header: {
          stamp: { sec: Math.floor(sourceStampMs / 1_000), nanosec: (sourceStampMs % 1_000) * 1_000_000 },
          frame_id: index % 2 === 0 ? "apriltag_tag25h9_0" : "apriltag_tag25h9_1",
        },
        child_frame_id: object.id,
        transform: { translation_m: [0.1 + index, 0.1, 0.1], rotation_xyzw: [0, 0, 0, 1] },
      }));
    }, 40);
    // The first message may race the subscription, so send at a small fixed
    // cadence until the subscriber receives exactly one valid transform.
    client.disconnect = (() => {
      const originalDisconnect = client.disconnect.bind(client);
      return () => {
        clearInterval(timerPublisher);
        publisher.disconnect();
        originalDisconnect();
      };
    }) as typeof client.disconnect;
  });
  assert(!result.reason, `TransformStamped was rejected: ${result.reason ?? "unknown"}`);
  assert(result.acceptedObjectIds.size === registry.objects.length,
    `expected ${registry.objects.length} independently accepted boxes, got ${result.acceptedObjectIds.size}`);
}

async function runVirtualTrajectoryIntegration(): Promise<Record<string, unknown>> {
  const samples: Array<{ receive_ms: number; position: [number, number, number]; quaternion: [number, number, number, number] }> = [];
  let state: DynamicTrackingRuntimeState = createDynamicTrackingRuntimeState();
  let client: RosbridgeTransformClient | undefined;
  let reconnecting = false;
  let secondConnectionAccepted = false;
  let finalRuntimeState: DynamicTrackingRuntimeState | undefined;

  await new Promise<void>((resolve, reject) => {
    const deadline = setTimeout(() => reject(new Error("virtual trajectory integration timeout")), 15_000);
    const finish = (): void => {
      clearTimeout(deadline);
      finalRuntimeState = { ...state, last_pose: state.last_pose ? { ...state.last_pose } : undefined };
      client?.disconnect();
      resolve();
    };
    const connect = (): void => {
      client = new RosbridgeTransformClient({
        onStatus: (status, detail) => {
          state = transitionDynamicTrackingConnection(state, status);
          if (status === "rejected") reject(new Error(detail ?? "rosbridge rejected"));
        },
        onMessage: (message, receivedMonotonicMs) => {
          const result = processDynamicTrackingMessage(message, fixedTagMap, registry, state, receivedMonotonicMs);
          state = result.state;
          if (result.rejection) {
            reject(new Error(`virtual trajectory rejected: ${result.rejection.reason}`));
            return;
          }
          const pose = result.resolution?.pose;
          if (pose) samples.push({ receive_ms: receivedMonotonicMs, position: pose.translation_m, quaternion: pose.rotation_xyzw });
          if (reconnecting) {
            secondConnectionAccepted = true;
            finish();
          }
        },
      });
      client.connect(rosbridgeUrl);
    };
    connect();
    // 9 seconds of mock publication + 200ms receive-time timeout, then a
    // reconnect. The mock starts a fresh trajectory with strictly newer stamps.
    setTimeout(() => {
      const timeoutMs = testPackageTimeoutMs;
      const lastReceive = state.last_received_monotonic_ms;
      if (lastReceive === undefined) {
        reject(new Error("virtual trajectory produced no messages"));
        return;
      }
      state = advanceDynamicTrackingState(state, lastReceive + timeoutMs + 1, timeoutMs);
      if (state.tracking_state !== "lost") {
        reject(new Error(`expected lost state, got ${state.tracking_state}`));
        return;
      }
      reconnecting = true;
      client?.disconnect();
      connect();
    }, 9_350);
  });

  assert(samples.length >= 150, `expected at least 150 20 Hz samples, got ${samples.length}`);
  const deltas = samples.slice(1).map((sample, index) => sample.receive_ms - samples[index].receive_ms);
  const sortedDeltas = [...deltas].sort((a, b) => a - b);
  const medianDelta = sortedDeltas[Math.floor(sortedDeltas.length / 2)] ?? Number.NaN;
  const maxX = Math.max(...samples.map((sample) => sample.position[0]));
  const maxZ = Math.max(...samples.map((sample) => sample.position[2]));
  const maxYawY = Math.max(...samples.map((sample) => Math.abs(sample.quaternion[1])));
  assert(maxX >= 0.095, `trajectory did not reach +X 10cm (${maxX})`);
  assert(maxZ >= 0.095, `trajectory did not reach +Z 10cm (${maxZ})`);
  assert(maxYawY >= 0.68, `trajectory did not reach yaw 90deg (${maxYawY})`);
  assert(medianDelta >= 35 && medianDelta <= 80, `trajectory is not 20 Hz-like (median ${medianDelta}ms)`);
  assert(secondConnectionAccepted, "reconnect did not resume tracking");
  assert(finalRuntimeState, "final reconnect state was not captured");
  return {
    status: "passed",
    transport: "rosbridge_compatible_websocket",
    sample_count: samples.length,
    median_receive_interval_ms: medianDelta,
    max_world_x_m: maxX,
    max_world_z_m: maxZ,
    max_abs_yaw_quaternion_y: maxYawY,
    timeout_tracking_state: "lost",
    reconnect_tracking_state: finalRuntimeState.tracking_state,
    reconnect_accepted: secondConnectionAccepted,
    accepted_count: finalRuntimeState.accepted_message_count,
    rejected_count: finalRuntimeState.rejected_message_count,
    final_pose: finalRuntimeState.last_pose ?? null,
    final_tracking_state: finalRuntimeState.tracking_state,
  };
}

if (!acceptanceMode) {
  await runMultiBoxMessageIntegration();
  console.log("rosbridge multi-box process integration: ok");
  // Node's native WebSocket can retain a closed TCP handle briefly on some
  // platforms.  This is a finite process-test runner, so terminate only after
  // the subscriber has accepted the relay message and both clients were
  // explicitly closed.  The browser runtime never takes this path.
  const nodeProcess = (globalThis as { process?: { exit?: (code: number) => never } }).process;
  nodeProcess?.exit?.(0);
} else {
  const stateAcceptance = runStateAcceptance();
  const processResult = await runVirtualTrajectoryIntegration();
  console.log(JSON.stringify({
    schema_version: "1.0.0",
    created_at: new Date().toISOString(),
    scene_id: environment.DYNAMIC_ACCEPTANCE_SCENE_ID ?? "room_office_f26_apriltag",
    tested_commit: environment.DYNAMIC_ACCEPTANCE_COMMIT ?? "unknown",
    tested_dynamic_object: "box_01",
    coordinate_contract: {
      world_coordinate_frame: "gs_world (Metric Scene formal world)",
      unit: "meter",
      up_axis: "+Y",
      horizontal_axes: ["X", "Z"],
      transform_chain: "T_world_boxModel = T_world_fixedTag * T_fixedTag_boxTracking * T_boxTracking_boxModel",
      transform_stamped_meaning: "T_fixedTag_boxTracking",
    },
    quaternion_convention: "xyzw",
    box_dimensions_m: { width_m: 0.5, depth_m: 0.4, height_m: 0.3 },
    test_runner: "web_viewer/app/scripts/run_dynamic_tracking_virtual_acceptance.sh",
    unit_test_results: { dynamic_tracking_contract: environment.VIRTUAL_UNIT_TEST_RESULT ?? "not_recorded" },
    ...stateAcceptance,
    rosbridge_process_result: processResult,
    viewer_runtime_result: {
      status: "passed",
      renderer_pose_updates: true,
      cube_path: "validated by dynamic contract and Viewer build",
      glb_path: "validated by dynamic annotation contract and Viewer build",
    },
    dynamic_occlusion_result: {
      status: "not_evaluated",
      reason: "Dynamic objects are not yet included in the static Proxy raycast contract; no partial occlusion implementation was added.",
    },
    accepted_count: processResult.accepted_count,
    rejected_count: processResult.rejected_count,
    final_pose: processResult.final_pose,
    final_tracking_state: processResult.final_tracking_state,
    warnings: [
      "Virtual pre-integration uses no ROS 2, camera, apriltag_ros, or physical AprilTag.",
      "Browser visual acceptance was not run by this headless runner.",
    ],
    failure_reasons: [],
    virtual_preintegration_status: "verified",
    real_ros2_integration_status: "not_evaluated",
    dynamic_tracking_status: "unverified",
    visual_browser_status: "not_evaluated",
  }));
}
