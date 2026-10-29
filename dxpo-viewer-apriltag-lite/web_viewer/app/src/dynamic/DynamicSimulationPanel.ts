import * as THREE from "three";
import { TransformControls } from "three/addons/controls/TransformControls.js";
import type { OrbitControls } from "three/addons/controls/OrbitControls.js";
import {
  boxGeometryDimensions,
  composePose,
  createRuntimeDynamicBoxConfig,
  invertPose,
  MAX_RUNTIME_DYNAMIC_BOXES,
  normalizeQuaternion,
  type DynamicObjectConfig,
  type DynamicObjectRegistry,
  type FixedTagMap,
  type PoseTransform,
  type TransformStampedMessage,
} from "./DynamicTrackingContract";
import { RosbridgeTransformPublisher } from "./RosbridgeTransformPublisher";
import { requireViewerSimulationParent, type ViewerSimulationSessionGuard } from "./ViewerSimulationSession";

export interface DynamicSimulationPanelOptions {
  panel: HTMLElement;
  scene: THREE.Scene;
  camera: THREE.Camera;
  rendererElement: HTMLElement;
  controls: OrbitControls;
  scenePackageId: string | undefined;
  parentSessionId: string | undefined;
  fixedTagMap: FixedTagMap | undefined;
  registry: DynamicObjectRegistry | undefined;
}

export interface DynamicSimulationPanelHandle {
  dispose(): void;
}

interface SimulationObjectState {
  readonly config: DynamicObjectConfig;
  readonly cube: THREE.Group;
  /** Runtime-only simulator membership. The parent Viewer admits valid
   * `box_<positive integer>` IDs dynamically; a Scene Package entry is only
   * an optional visual/threshold override. */
  enabled: boolean;
  selectedFrame: string;
  relativePose: PoseTransform;
}

function createButton(label: string, onClick: () => void, disabled = false): HTMLButtonElement {
  const button = document.createElement("button");
  button.type = "button";
  button.textContent = label;
  button.disabled = disabled;
  button.addEventListener("click", onClick);
  return button;
}

function addNumberInput(parent: HTMLElement, label: string, value: number, onChange: (value: number) => void): HTMLInputElement {
  const field = document.createElement("label");
  field.className = "dynamic-simulator-field";
  const caption = document.createElement("span");
  caption.textContent = label;
  const input = document.createElement("input");
  input.type = "number";
  input.step = "0.01";
  input.value = value.toFixed(3);
  input.addEventListener("change", () => {
    const parsed = Number(input.value);
    if (Number.isFinite(parsed)) onChange(parsed);
  });
  field.append(caption, input);
  parent.appendChild(field);
  return input;
}

function createSimulationCube(config: DynamicObjectConfig): THREE.Group {
  const fallbackModel = createRuntimeDynamicBoxConfig(config.id).model;
  const dimensions = config.model.format === "cube"
    ? config.model.dimensions_m
    : fallbackModel.format === "cube"
      ? fallbackModel.dimensions_m
      : { width_m: 0.5, depth_m: 0.4, height_m: 0.3 };
  const [width, height, depth] = boxGeometryDimensions(dimensions);
  const geometry = new THREE.BoxGeometry(width, height, depth);
  const color = config.model.format === "cube" ? config.model.color_hex : "#35b6ff";
  const group = new THREE.Group();
  group.name = `Simulation publisher ${config.id}`;
  const mesh = new THREE.Mesh(geometry, new THREE.MeshBasicMaterial({
    color, transparent: true, opacity: 0.84, depthTest: false, depthWrite: false,
  }));
  const outline = new THREE.LineSegments(
    new THREE.EdgesGeometry(geometry),
    new THREE.LineBasicMaterial({ color: "#f4fbff", transparent: true, opacity: 0.92, depthTest: false }),
  );
  mesh.renderOrder = 10;
  outline.renderOrder = 11;
  group.add(mesh, outline);
  return group;
}

function poseFromObject(object: THREE.Object3D): PoseTransform {
  return {
    translation_m: [object.position.x, object.position.y, object.position.z],
    rotation_xyzw: [object.quaternion.x, object.quaternion.y, object.quaternion.z, object.quaternion.w],
  };
}

function nowStamp(): TransformStampedMessage["header"]["stamp"] {
  const milliseconds = Date.now();
  return { sec: Math.floor(milliseconds / 1_000), nanosec: (milliseconds % 1_000) * 1_000_000 };
}

/** A second Viewer instance that acts as a metric, virtual camera-side test
 * rig. It publishes independent runtime `box_<N>` streams over the same
 * rosbridge topic. The parent Viewer remains the SPZ authority. */
export function mountDynamicSimulationPanel(options: DynamicSimulationPanelOptions): DynamicSimulationPanelHandle {
  const { panel, scene, camera, rendererElement, controls, scenePackageId, parentSessionId, fixedTagMap, registry } = options;
  panel.replaceChildren();
  const title = document.createElement("strong");
  title.textContent = "ROS実験シミュレータ";
  panel.appendChild(title);
  const explanation = document.createElement("div");
  explanation.className = "dynamic-summary";
  explanation.textContent = "親Viewerで選択済みのScene Package/SPZだけを使います。箱は送信時に動的IDとして生成され、追加・削除できます。";
  panel.appendChild(explanation);

  const availability = document.createElement("div");
  availability.className = "dynamic-notice";
  panel.appendChild(availability);
  if (!scenePackageId || !fixedTagMap) {
    availability.textContent = "利用不可: 検証済み固定タグを持つScene Packageから起動してください。";
    return { dispose: () => undefined };
  }

  const fixedFrames = Object.keys(fixedTagMap.frames);
  const effectiveRegistry: DynamicObjectRegistry = registry ?? {
    schema_version: "1.0.0",
    coordinate_frame: "gs_world",
    unit: "meter",
    rotation_order: "xyzw",
    objects: [],
  };
  const states = new Map<string, SimulationObjectState>();
  const configFor = (id: string): DynamicObjectConfig =>
    effectiveRegistry.objects.find((candidate) => candidate.id === id) ?? createRuntimeDynamicBoxConfig(id);
  const addSimulationState = (config: DynamicObjectConfig, index: number, enabled: boolean): SimulationObjectState => {
    const cube = createSimulationCube(config);
    cube.visible = enabled;
    scene.add(cube);
    const state: SimulationObjectState = {
      config,
      cube,
      enabled,
      selectedFrame: fixedFrames[0],
      relativePose: { translation_m: [index * 0.65, 0.2, -0.5], rotation_xyzw: [0, 0, 0, 1] },
    };
    states.set(config.id, state);
    return state;
  };
  // Two deliberately visible test publishers make the Docker/ROS experiment
  // immediately observable. They are not a Scene Package object registry;
  // more boxes are allocated dynamically with the same `box_<N>` contract.
  addSimulationState(configFor("box_01"), 0, true);
  addSimulationState(configFor("box_02"), 1, true);

  let selectedObjectId = "box_01";
  const selectedState = (): SimulationObjectState => states.get(selectedObjectId)!;
  const applyRelativePose = (state: SimulationObjectState): void => {
    const fixedPose = fixedTagMap.frames[state.selectedFrame];
    const renderPose = composePose(composePose(fixedPose, state.relativePose), state.config.object_to_render);
    state.cube.position.set(...renderPose.translation_m);
    state.cube.quaternion.set(...renderPose.rotation_xyzw);
  };
  for (const state of states.values()) applyRelativePose(state);

  const transformControls = new TransformControls(camera, rendererElement);
  transformControls.attach(selectedState().cube);
  transformControls.setMode("translate");
  scene.add(transformControls.getHelper());

  const objectSelect = document.createElement("select");
  objectSelect.className = "dynamic-simulator-select";
  objectSelect.ariaLabel = "simulation box";
  const refreshObjectOptions = (): void => {
    objectSelect.replaceChildren();
    for (const state of states.values()) {
      const option = document.createElement("option");
      option.value = state.config.id;
      option.textContent = state.enabled ? state.config.id : `${state.config.id}（未追加）`;
      objectSelect.appendChild(option);
    }
    objectSelect.value = selectedObjectId;
  };
  refreshObjectOptions();
  const objectField = document.createElement("label");
  objectField.className = "dynamic-simulator-field";
  const objectCaption = document.createElement("span");
  objectCaption.textContent = "編集する箱";
  objectField.append(objectCaption, objectSelect);
  panel.appendChild(objectField);

  const fixedFrameSelect = document.createElement("select");
  fixedFrameSelect.className = "dynamic-simulator-select";
  for (const frame of fixedFrames) {
    const option = document.createElement("option");
    option.value = frame;
    option.textContent = frame;
    fixedFrameSelect.appendChild(option);
  }
  const fixedField = document.createElement("label");
  fixedField.className = "dynamic-simulator-field";
  const fixedCaption = document.createElement("span");
  fixedCaption.textContent = "固定タグ";
  fixedField.append(fixedCaption, fixedFrameSelect);
  panel.appendChild(fixedField);

  const fields = document.createElement("div");
  fields.className = "dynamic-simulator-fields";
  panel.appendChild(fields);
  const syncFields = (): void => {
    const state = selectedState();
    fixedFrameSelect.value = state.selectedFrame;
    xInput.value = state.relativePose.translation_m[0].toFixed(3);
    yInput.value = state.relativePose.translation_m[1].toFixed(3);
    zInput.value = state.relativePose.translation_m[2].toFixed(3);
    const euler = new THREE.Euler().setFromQuaternion(new THREE.Quaternion(...state.relativePose.rotation_xyzw), "XYZ");
    rollInput.value = THREE.MathUtils.radToDeg(euler.x).toFixed(1);
    pitchInput.value = THREE.MathUtils.radToDeg(euler.y).toFixed(1);
    yawInput.value = THREE.MathUtils.radToDeg(euler.z).toFixed(1);
  };
  const setTranslation = (index: 0 | 1 | 2, value: number): void => {
    const state = selectedState();
    state.relativePose = {
      ...state.relativePose,
      translation_m: state.relativePose.translation_m.map((item, current) => current === index ? value : item) as [number, number, number],
    };
    applyRelativePose(state);
  };
  const setEulerDegrees = (index: 0 | 1 | 2, value: number): void => {
    const state = selectedState();
    const euler = new THREE.Euler().setFromQuaternion(new THREE.Quaternion(...state.relativePose.rotation_xyzw), "XYZ");
    const angles = [euler.x, euler.y, euler.z];
    angles[index] = THREE.MathUtils.degToRad(value);
    const quaternion = new THREE.Quaternion().setFromEuler(new THREE.Euler(angles[0], angles[1], angles[2], "XYZ"));
    const normalized = normalizeQuaternion([quaternion.x, quaternion.y, quaternion.z, quaternion.w]);
    if ("status" in normalized) return;
    state.relativePose = { ...state.relativePose, rotation_xyzw: normalized };
    applyRelativePose(state);
  };
  const xInput = addNumberInput(fields, "Tag基準 X (m)", selectedState().relativePose.translation_m[0], (value) => setTranslation(0, value));
  const yInput = addNumberInput(fields, "Tag基準 Y (m)", selectedState().relativePose.translation_m[1], (value) => setTranslation(1, value));
  const zInput = addNumberInput(fields, "Tag基準 Z (m)", selectedState().relativePose.translation_m[2], (value) => setTranslation(2, value));
  const rollInput = addNumberInput(fields, "回転 X (deg)", 0, (value) => setEulerDegrees(0, value));
  const pitchInput = addNumberInput(fields, "回転 Y (deg)", 0, (value) => setEulerDegrees(1, value));
  const yawInput = addNumberInput(fields, "回転 Z (deg)", 0, (value) => setEulerDegrees(2, value));
  syncFields();

  const modeButton = createButton("操作: 移動", () => {
    const rotating = transformControls.mode === "rotate";
    transformControls.setMode(rotating ? "translate" : "rotate");
    modeButton.textContent = rotating ? "操作: 移動" : "操作: 回転";
  });
  panel.appendChild(modeButton);

  const urlInput = document.createElement("input");
  urlInput.type = "url";
  urlInput.value = "ws://127.0.0.1:9090";
  urlInput.ariaLabel = "rosbridge URL";
  panel.appendChild(urlInput);
  const startButton = createButton("ROS送信を開始", () => undefined, true);
  const stopButton = createButton("送信を停止", () => undefined, true);
  panel.append(startButton, stopButton);
  const runtime = document.createElement("div");
  runtime.className = "dynamic-summary-rows";
  panel.appendChild(runtime);

  let parentAvailable = false;
  let publishing = false;
  let publishTimer: number | undefined;
  const activeStates = (): SimulationObjectState[] => [...states.values()].filter((state) => state.enabled);
  const addBoxButton = createButton("箱を追加", () => undefined);
  const removeBoxButton = createButton("選択した箱を削除", () => undefined);
  panel.append(addBoxButton, removeBoxButton);

  const boxStatusToggle = createButton("箱の状況カードを隠す", () => undefined);
  const boxStatusCard = document.createElement("section");
  boxStatusCard.className = "dynamic-box-status-card";
  const boxStatusTitle = document.createElement("strong");
  const boxStatusRows = document.createElement("div");
  boxStatusRows.className = "dynamic-summary-rows";
  const boxStatusHint = document.createElement("div");
  boxStatusHint.className = "dynamic-summary";
  boxStatusCard.append(boxStatusTitle, boxStatusRows, boxStatusHint);
  panel.append(boxStatusToggle, boxStatusCard);
  let boxStatusVisible = true;

  const renderBoxStatusCard = (): void => {
    const active = activeStates();
    boxStatusTitle.textContent = `箱の状況: ${active.length}箱を送信対象に設定`;
    boxStatusRows.replaceChildren();
    for (const state of states.values()) {
      const row = document.createElement("div");
      row.className = "dynamic-summary-row";
      const label = document.createElement("span");
      label.textContent = state.config.id;
      const value = document.createElement("span");
      value.textContent = state.enabled
        ? `${publishing ? "送信中" : "準備済み"} / ROS child=${state.config.id}`
        : `削除済み / ROS child=${state.config.id}`;
      row.append(label, value);
      boxStatusRows.appendChild(row);
    }
    boxStatusHint.textContent = active.length === 0
      ? "箱を追加すると、次の box_<番号> をROSへ送信できます。"
      : `追加・削除はこの実験シミュレータ内だけの操作です。親Viewerは受信した box_<番号> を動的に表示します（同時上限 ${MAX_RUNTIME_DYNAMIC_BOXES}）。`;
  };
  const updateStartAvailability = (): void => {
    startButton.disabled = !parentAvailable || publishing || activeStates().length === 0;
    stopButton.disabled = !publishing;
  };
  const syncSelectedBox = (): void => {
    const state = selectedState();
    if (state.enabled) transformControls.attach(state.cube);
    else transformControls.detach();
    modeButton.disabled = !state.enabled;
    removeBoxButton.disabled = !state.enabled;
    syncFields();
  };
  const selectBox = (id: string): void => {
    selectedObjectId = id;
    refreshObjectOptions();
    syncSelectedBox();
  };
  addBoxButton.onclick = () => {
    const next = [...states.values()].find((state) => !state.enabled);
    if (next) {
      next.enabled = true;
      next.cube.visible = true;
      selectBox(next.config.id);
      renderBoxStatusCard();
      updateStartAvailability();
      runtime.textContent = `${next.config.id}を実験に再追加しました。`;
      return;
    }
    if (states.size >= MAX_RUNTIME_DYNAMIC_BOXES) {
      runtime.textContent = `同時に扱える箱は${MAX_RUNTIME_DYNAMIC_BOXES}個までです。`;
      return;
    }
    const suffixes = [...states.keys()].map((id) => Number(id.slice("box_".length))).filter(Number.isFinite);
    const nextId = `box_${String(Math.max(0, ...suffixes) + 1).padStart(2, "0")}`;
    const created = addSimulationState(configFor(nextId), states.size, true);
    applyRelativePose(created);
    selectBox(created.config.id);
    renderBoxStatusCard();
    updateStartAvailability();
    runtime.textContent = `${created.config.id}を実験に追加しました。`;
  };
  removeBoxButton.onclick = () => {
    const state = selectedState();
    if (!state.enabled) return;
    state.enabled = false;
    state.cube.visible = false;
    const remaining = activeStates()[0] ?? [...states.values()].find((candidate) => candidate.config.id !== state.config.id);
    if (remaining) selectBox(remaining.config.id);
    else transformControls.detach();
    if (publishing && activeStates().length === 0) stopPublishing("すべての箱を削除したため、ROS送信を停止しました。");
    renderBoxStatusCard();
    updateStartAvailability();
    runtime.textContent = `${state.config.id}をこの実験から削除しました。`;
  };
  boxStatusToggle.onclick = () => {
    boxStatusVisible = !boxStatusVisible;
    boxStatusCard.hidden = !boxStatusVisible;
    boxStatusToggle.textContent = boxStatusVisible ? "箱の状況カードを隠す" : "箱の状況カードを表示";
  };

  const publisher = new RosbridgeTransformPublisher({
    onStatus: (status, detail) => {
      runtime.textContent = `rosbridge: ${status}${detail ? ` (${detail})` : ""}`;
    },
  });
  const message = (state: SimulationObjectState): TransformStampedMessage => ({
    header: { stamp: nowStamp(), frame_id: state.selectedFrame },
    child_frame_id: state.config.id,
    transform: state.relativePose,
  });
  const stopPublishing = (reason?: string): void => {
    publishing = false;
    if (publishTimer !== undefined) {
      window.clearInterval(publishTimer);
      publishTimer = undefined;
    }
    publisher.disconnect();
    updateStartAvailability();
    renderBoxStatusCard();
    if (reason) runtime.textContent = reason;
  };
  startButton.onclick = () => {
    const active = activeStates();
    if (!parentAvailable || active.length === 0) return;
    publisher.connect(urlInput.value.trim());
    publishing = true;
    updateStartAvailability();
    publishTimer = window.setInterval(() => {
      for (const state of activeStates()) publisher.publish(message(state));
    }, 50);
    runtime.textContent = `rosbridge接続待ち。接続後、${active.length}箱を各20 Hzで T_fixedTag_box として送信します。`;
    renderBoxStatusCard();
  };
  stopButton.onclick = () => stopPublishing("送信を停止しました。");
  transformControls.addEventListener("dragging-changed", (event: { value?: unknown }) => {
    controls.enabled = event.value !== true;
  });
  transformControls.addEventListener("objectChange", () => {
    const state = selectedState();
    const fixedPose = fixedTagMap.frames[state.selectedFrame];
    state.relativePose = composePose(
      composePose(invertPose(fixedPose), poseFromObject(state.cube)),
      invertPose(state.config.object_to_render),
    );
    syncFields();
  });
  objectSelect.addEventListener("change", () => {
    selectBox(objectSelect.value);
  });
  fixedFrameSelect.addEventListener("change", () => {
    const state = selectedState();
    // Preserve this box's render-world pose while changing its tag reference.
    const worldPose = poseFromObject(state.cube);
    state.selectedFrame = fixedFrameSelect.value;
    state.relativePose = composePose(
      composePose(invertPose(fixedTagMap.frames[state.selectedFrame]), worldPose),
      invertPose(state.config.object_to_render),
    );
    syncFields();
  });
  const guard: ViewerSimulationSessionGuard = requireViewerSimulationParent(
    scenePackageId,
    parentSessionId,
    (available, detail, parentInfo) => {
      parentAvailable = available && Boolean(parentInfo?.fixedTagFrames.length) && parentInfo?.acceptsRuntimeDynamicBoxes === true;
      availability.textContent = parentAvailable ? detail : `${detail} 送信不可。`;
      if (!parentAvailable && publishing) stopPublishing("親Viewerの確認が失われたため、ROS送信を停止しました。");
      if (!publishing) updateStartAvailability();
    },
  );
  syncSelectedBox();
  renderBoxStatusCard();
  updateStartAvailability();
  return {
    dispose: () => {
      guard.dispose();
      stopPublishing();
      transformControls.detach();
      scene.remove(transformControls.getHelper());
      transformControls.dispose();
      for (const state of states.values()) {
        scene.remove(state.cube);
        state.cube.traverse((child) => {
          if (child instanceof THREE.Mesh) {
            child.geometry.dispose();
            (Array.isArray(child.material) ? child.material : [child.material]).forEach((material) => material.dispose());
          }
        });
      }
      states.clear();
    },
  };
}
