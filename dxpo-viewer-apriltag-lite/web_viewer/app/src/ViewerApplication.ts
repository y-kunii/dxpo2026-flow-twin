import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { GLTFLoader } from "three/addons/loaders/GLTFLoader.js";
import { loadSceneManifest } from "./scene/SceneLoader";
import { aprilTagCornerSets, type DynamicOverlayConfiguration } from "./scene/SceneManifest";
import type { CameraPreset } from "./scene/CameraPresets";
import { createCameraFrustumObject, type CameraFrustumObject } from "./camera/CameraFrustum";
import { createFirstPersonNavigation } from "./camera/FirstPersonNavigation";
import { mountFirstPersonNavigationSettings } from "./camera/FirstPersonNavigationSettings";
import { createRendererInteractionQuality } from "./camera/RendererInteractionQuality";
import {
  createAprilTagDebugLayer,
  type AprilTagDebugLayer
} from "./measurement/AprilTagDebug";
import { createProxyInteractionLayer, type ProxyInteractionLayer } from "./proxy/ProxyInteraction";
import { createProxyPlacementPresets, evaluateProxyVisibility } from "./proxy/ProxyGeometry";
import { createDynamicObjectLayer, type DynamicObjectLayer } from "./dynamic/DynamicObjectLayer";
import { refreshDynamicObjectSelect } from "./dynamic/DynamicObjectSelector";
import {
  exportRuntimeDynamicBoxProfile,
  importRuntimeDynamicBoxProfile,
  loadRuntimeDynamicBoxProfile,
  runtimeDynamicBoxProfileKey,
  saveRuntimeDynamicBoxProfile,
} from "./dynamic/RuntimeDynamicBoxProfile";
import { mountDynamicCongestionOverlay } from "./dynamic/DynamicCongestionOverlay";
import {
  createDynamicFixedTagDistanceOverlay,
  type DynamicFixedTagDistanceOverlay,
} from "./dynamic/DynamicFixedTagDistanceOverlay";
import {
  advanceDynamicTrackingCollectionState,
  createDynamicTrackingCollectionState,
  deriveFixedTagMapFromAprilTagCorners,
  processDynamicTrackingCollectionMessage,
  transitionDynamicTrackingCollectionConnection,
  isDynamicRejection,
  createDefaultDynamicRegistry,
  DEFAULT_DYNAMIC_BOX_DIMENSIONS_M,
  resolveDynamicObjectConfig,
  distanceM,
  type DynamicObjectRegistry,
  type DynamicTrackingCollectionState,
  type FixedTagMap,
  type BoxTagFace,
  type PoseTransform
} from "./dynamic/DynamicTrackingContract";
import {
  RosbridgeTransformClient,
  type RosbridgeConnectionStatus
} from "./dynamic/RosbridgeTransformClient";
import { mountDynamicSimulationPanel, type DynamicSimulationPanelHandle } from "./dynamic/DynamicSimulationPanel";
import {
  clearDynamicTrackingDiagnostics,
  dynamicTrackingDiagnosticSnapshot,
  recordDynamicTrackingDiagnostic,
  startDynamicTrackingDiagnostics,
  stopDynamicTrackingDiagnostics,
  subscribeDynamicTrackingDiagnostics,
} from "./dynamic/DynamicTrackingDiagnostics";
import { startViewerSimulationSession, type ViewerSimulationSessionInfo } from "./dynamic/ViewerSimulationSession";
import {
  cameraForward,
  cloneSceneObject,
  createSceneObjectVisual,
  DEFAULT_PERSON_HEIGHT_M,
  emptySceneObjects,
  makeCurrentViewAnchor,
  makePersonObject,
  personTargetPoint,
  pickSceneObject,
  sceneObjectToCameraPreset,
  setCameraForward,
  setPersonYaw,
  type SceneObject,
  type SceneObjectsDocument,
  type SurfaceType
} from "./scene/SceneObjects";
import { frameAnalysisReturnUrl } from "./preview/FrameAnalysisNavigation";
import {
  requestedFrameAnalysisId as parseRequestedFrameAnalysisId,
  restoreFrameAnalysisCameraPose,
} from "./camera/FrameAnalysisCameraPose";
import { mountViewerChrome } from "./ui/ViewerChrome";

// 例:
// const SPLAT_URL = "/assets/splats/office_room_f26_2-lod.rad";
// const SPLAT_URL = "/assets/splats/office_room_f26_2-lod.rad";
// const SPLAT_URL = "/assets/splats/office_room_f26_2-lod.rad";

const app = document.querySelector<HTMLDivElement>("#app");

if (!app) {
  throw new Error("#app element not found");
}

const query = new URLSearchParams(window.location.search);

// App B is opened only for an explicit published Scene Package. It does not
// interpret a legacy static scene, raw SPZ, or MRP as a Viewer input.
const requestedScenePackageId = query.get("scene_package");
const runtimeScenePackageId = requestedScenePackageId && /^[A-Za-z0-9][A-Za-z0-9_.-]*$/.test(requestedScenePackageId)
  ? requestedScenePackageId
  : undefined;
const requestedFrameAnalysisId = parseRequestedFrameAnalysisId(query);

if (requestedFrameAnalysisId) {
  const returnLink = document.createElement("a");
  returnLink.className = "frame-analysis-return-link";
  returnLink.href = frameAnalysisReturnUrl();
  returnLink.textContent = "← 解析DBへ戻る";
  returnLink.title = "登録画像AI解析の検索画面へ戻る";
  returnLink.setAttribute("aria-label", "解析DBへ戻る");
  document.body.appendChild(returnLink);
}
const requestedPreview = query.get("preview");
const requestedFocus = (() => {
  const raw = query.get("focus");
  if (!raw) return undefined;
  const values = raw.split(",").map(Number);
  return values.length === 3 && values.every(Number.isFinite)
    ? new THREE.Vector3(values[0], values[1], values[2])
    : undefined;
})();
const dynamicSimulatorRequested = query.get("dynamic_simulator") === "1";
const dynamicSimulatorParentSessionId = query.get("viewer_session") ?? undefined;
const SPARK_SPZ_COORDINATE_CONTRACT = "spark_spz_decoder_rx180";
const VERIFIED_GAUSSIAN_COORDINATE_CONTRACTS = new Set([
  SPARK_SPZ_COORDINATE_CONTRACT,
  "spark_gaussian_decoder_rx180",
  "viewer_native_identity",
]);

if (!runtimeScenePackageId && !requestedPreview && __SPATIAL_APP_TARGET__ !== "suite") {
  window.location.replace("/?preview=packages");
}

if (__SPATIAL_APP_TARGET__ === "suite") {
  if (!runtimeScenePackageId && !requestedPreview) {
    void import("./apps/AppLauncher").then(({ mountApplicationLauncher }) => {
      mountApplicationLauncher(document.body);
    });
  } else {
    void import("./preview/ApplicationPreview").then(({ mountApplicationPreview }) => {
      mountApplicationPreview(document.body);
    });
  }
}

// The package landing page must run without a checked-in fixture. This keeps a
// read-only Viewer distribution self-contained: it only ships selected runtime
// packages and never tries to load the development sample behind the landing UI.
if (runtimeScenePackageId) {
  // runtime is a generated, read-only publication copy. The canonical source
  // is data/scene_repository/packages/scenes/<scene_package_id>; no static fixture is a fallback.
  const sceneManifestUrl = `/scene_package/runtime/${encodeURIComponent(runtimeScenePackageId)}/scene_manifest.json`;

const scene = new THREE.Scene();
scene.background = new THREE.Color(0x111111);

const camera = new THREE.PerspectiveCamera(
  60,
  window.innerWidth / window.innerHeight,
  0.01,
  1000
);

camera.position.set(0, 1.5, 4);

const renderer = new THREE.WebGLRenderer({
  antialias: false,
  alpha: false,
  powerPreference: "high-performance"
});

renderer.setPixelRatio(1);
renderer.setSize(window.innerWidth, window.innerHeight);
app.appendChild(renderer.domElement);

// Keep full-screen rendering within a predictable GPU budget. The controller
// owns the timer/state symmetry so camera presets, resize, and disposal cannot
// leave the Viewer permanently in an interaction quality mode.
const interactionQuality = createRendererInteractionQuality(renderer);
window.addEventListener("beforeunload", () => interactionQuality.dispose(), { once: true });

function createControls(): OrbitControls {
  const nextControls = new OrbitControls(camera, renderer.domElement);
  // OrbitControls remains as the shared target/preset state holder. Direct
  // mouse navigation is handled by FirstPersonNavigation so rotations happen
  // around the camera itself instead of around a remote orbit target.
  nextControls.enableDamping = false;
  nextControls.enableRotate = false;
  nextControls.enablePan = false;
  nextControls.enableZoom = false;
  renderer.domElement.addEventListener("contextmenu", (event) => event.preventDefault());
  return nextControls;
}

const controls = createControls();
controls.target.set(0, 1, 0);
controls.update();

const navigationClock = new THREE.Clock();
const firstPersonNavigation = createFirstPersonNavigation(camera, controls, renderer.domElement, {
  onInteractionStart: () => interactionQuality.begin(),
  onInteractionEnd: () => interactionQuality.end(),
});


const congestionOverlay = mountDynamicCongestionOverlay();
const navigationPanel = mountFirstPersonNavigationSettings(firstPersonNavigation);
const { status, cameraPanel, proxyPanel, objectPanel, dynamicPanel } = mountViewerChrome(navigationPanel);

const cameraFrustumObjects = new Map<string, CameraFrustumObject>();
let cameraGuidesVisible = false;
let aprilTagDebugVisible = false;
let aprilTagDebugLayer: AprilTagDebugLayer | undefined;
let proxyInteractionLayer: ProxyInteractionLayer | undefined;
let proxyObject: THREE.Object3D | undefined;
let dynamicObjectLayer: DynamicObjectLayer | undefined;
let dynamicFixedTagDistanceOverlay: DynamicFixedTagDistanceOverlay | undefined;
let dynamicTrackingClient: RosbridgeTransformClient | undefined;
let dynamicStaleTimer: number | undefined;
let dynamicRuntimeRenderFrame: number | undefined;
let dynamicDiagnosticUnsubscribe: (() => void) | undefined;
let dynamicSimulationPanelHandle: DynamicSimulationPanelHandle | undefined;
let viewerSimulationSession: { info: ViewerSimulationSessionInfo; dispose(): void } | undefined;
let runtimeBoxProfileStorageListener: ((event: StorageEvent) => void) | undefined;
let proxyTarget = new THREE.Vector3();
let proxyCenter = new THREE.Vector3();
let sceneCameraPresets: CameraPreset[] = [];
let surfacePlacementSettings: { horizontal_threshold: number; common_offset_m: number } | undefined;
let surfacePlacementMode = false;
let surfacePlacementButton: HTMLButtonElement | undefined;

/** WebSocket receive times are intentionally monotonic.  They are never
 * compared with a ROS epoch timestamp. */
function monotonicNowMs(): number {
  return typeof performance !== "undefined" && Number.isFinite(performance.now())
    ? performance.now()
    : Date.now();
}
let pointerGestureMoved = false;
let pointerGestureStart: { x: number; y: number } | undefined;
let sceneObjectsDocument: SceneObjectsDocument = emptySceneObjects("unknown");
let selectedObjectId: string | null = null;
let surfaceViewPreviousProxyVisible = false;
let objectVisibilityResult: ReturnType<typeof evaluateProxyVisibility> | null = null;
const sceneObjectsRoot = new THREE.Group();
sceneObjectsRoot.name = "Persistent scene objects";
scene.add(sceneObjectsRoot);

function clearCameraFrustums(): void {
  for (const frustum of cameraFrustumObjects.values()) {
    scene.remove(frustum.group);
  }

  cameraFrustumObjects.clear();
}

function renderCameraFrustums(presets: CameraPreset[]): void {
  clearCameraFrustums();

  for (const preset of presets) {
    if (preset.id.startsWith("apriltag_")) {
      continue;
    }
    const frustum = createCameraFrustumObject(preset);
    frustum.group.visible = cameraGuidesVisible;
    cameraFrustumObjects.set(preset.id, frustum);
    scene.add(frustum.group);
  }
}

function objectCameraPresets(): CameraPreset[] {
  return sceneObjectsDocument.objects
    .filter((object) => object.enabled && object.type === "camera" && object.camera?.frustum_visible)
    .map((object) => sceneObjectToCameraPreset(object))
    .filter((preset): preset is CameraPreset => Boolean(preset));
}

function renderAllCameraFrustums(): void {
  renderCameraFrustums([...sceneCameraPresets, ...objectCameraPresets()]);
}

function updateCameraGuidesButton(): void {
  const toggle = cameraPanel.querySelector<HTMLButtonElement>("button[data-camera-guides-toggle]");
  if (!toggle) return;
  toggle.textContent = `カメラガイド: ${cameraGuidesVisible ? "表示" : "非表示"}`;
  toggle.setAttribute("aria-pressed", String(cameraGuidesVisible));
  toggle.classList.toggle("is-active", cameraGuidesVisible);
}

function updateAprilTagDebugButton(): void {
  const toggle = cameraPanel.querySelector<HTMLButtonElement>("button[data-apriltag-debug-toggle]");
  if (!toggle) return;
  toggle.textContent = `AprilTagデバッグ: ${aprilTagDebugVisible ? "表示" : "非表示"}`;
  toggle.setAttribute("aria-pressed", String(aprilTagDebugVisible));
  toggle.classList.toggle("is-active", aprilTagDebugVisible);
}

function setAprilTagDebugVisible(visible: boolean): void {
  aprilTagDebugVisible = visible;
  aprilTagDebugLayer?.setVisible(visible);
  updateAprilTagDebugButton();
}

function setCameraGuidesVisible(visible: boolean): void {
  cameraGuidesVisible = visible;
  for (const frustum of cameraFrustumObjects.values()) {
    frustum.group.visible = visible;
  }
  for (const visual of sceneObjectsRoot.children) {
    if (visual.name.startsWith("scene-camera-")) {
      visual.visible = visible;
    }
  }
  updateCameraGuidesButton();
}

function replaceSceneObjectVisuals(): void {
  while (sceneObjectsRoot.children.length) {
    sceneObjectsRoot.remove(sceneObjectsRoot.children[0]);
  }
  for (const object of sceneObjectsDocument.objects) {
    const visual = createSceneObjectVisual(object, object.id === selectedObjectId);
    if (object.type === "camera") visual.visible = object.enabled && cameraGuidesVisible;
    sceneObjectsRoot.add(visual);
  }
}

function selectedSceneObject(): SceneObject | null {
  return sceneObjectsDocument.objects.find((object) => object.id === selectedObjectId) ?? null;
}

function sceneObjectTypeLabel(object: SceneObject): string {
  return object.type === "camera" ? "ビューアンカー" : "人物アンカー";
}

function updateObjectAndRender(): void {
  replaceSceneObjectVisuals();
  renderAllCameraFrustums();
  renderObjectPanel();
}

function setSelectedObject(id: string | null): void {
  selectedObjectId = id;
  updateObjectAndRender();
}

function objectUpdated(object: SceneObject): void {
  object.updated_at = new Date().toISOString();
  updateObjectAndRender();
}

function inputField(
  parent: HTMLElement,
  label: string,
  value: string,
  onChange: (value: string) => void,
  type = "text",
  step?: string
): void {
  const wrapper = document.createElement("label");
  wrapper.className = "object-field";
  wrapper.textContent = label;
  const input = document.createElement("input");
  input.type = type;
  input.value = value;
  if (step) input.step = step;
  input.addEventListener("change", () => onChange(input.value));
  wrapper.appendChild(input);
  parent.appendChild(wrapper);
}

function cameraYawPitch(object: SceneObject): [number, number] {
  const forward = cameraForward(object);
  return [
    THREE.MathUtils.radToDeg(Math.atan2(forward.x, -forward.z)),
    THREE.MathUtils.radToDeg(Math.asin(THREE.MathUtils.clamp(forward.y, -1, 1)))
  ];
}

function cameraForwardFromYawPitch(yawDeg: number, pitchDeg: number): THREE.Vector3 {
  const yaw = THREE.MathUtils.degToRad(yawDeg);
  const pitch = THREE.MathUtils.degToRad(THREE.MathUtils.clamp(pitchDeg, -89, 89));
  const horizontal = Math.cos(pitch);
  return new THREE.Vector3(Math.sin(yaw) * horizontal, Math.sin(pitch), -Math.cos(yaw) * horizontal).normalize();
}

function addSceneObject(object: SceneObject): void {
  sceneObjectsDocument.objects.push(object);
  selectedObjectId = object.id;
  updateObjectAndRender();
}

function addCurrentViewAnchor(): void {
  const forward = camera.getWorldDirection(new THREE.Vector3()).normalize();
  const object = makeCurrentViewAnchor(camera.position.clone(), forward, camera.fov);
  object.name = `View anchor ${object.id.slice(-4)}`;
  addSceneObject(object);
  status.textContent = "現在の視点をビューアンカーとして保存しました";
}

function addCurrentViewPerson(): void {
  const feet = camera.position.clone().addScaledVector(new THREE.Vector3(0, 1, 0), -DEFAULT_PERSON_HEIGHT_M * 0.88);
  const object = makePersonObject(feet);
  if (object.person) object.person.target_type = "head";
  const forward = camera.getWorldDirection(new THREE.Vector3());
  forward.y = 0;
  if (forward.lengthSq() > 1e-8) {
    const yawDeg = THREE.MathUtils.radToDeg(Math.atan2(forward.x, -forward.z));
    setPersonYaw(object, yawDeg);
  }
  object.name = `Person anchor ${object.id.slice(-4)}`;
  addSceneObject(object);
  status.textContent = "現在の視点に頭部を合わせて人物を追加しました";
}

function runPersonVisibilityCheck(cameraId: string, personId: string): void {
  const cameraObject = sceneObjectsDocument.objects.find((object) => object.id === cameraId && object.type === "camera");
  const personObject = sceneObjectsDocument.objects.find((object) => object.id === personId && object.type === "person");
  if (!cameraObject || !personObject || !proxyObject || !cameraObject.camera || !personTargetPoint(personObject)) {
    objectVisibilityResult = {
      camera_id: cameraId,
      target_id: personId,
      status: "unavailable",
      blocking_object_id: null,
      hit_distance_m: null,
      target_distance_m: 0
    };
  } else {
    const wasVisible = proxyObject.visible;
    proxyObject.visible = true;
    objectVisibilityResult = evaluateProxyVisibility(
      proxyObject,
      new THREE.Vector3(...cameraObject.position),
      personTargetPoint(personObject)!,
      cameraId,
      personId
    );
    proxyObject.visible = wasVisible;
  }
  status.textContent = objectVisibilityResult.status === "unavailable"
    ? "検証済みProxyがないため解析できません"
    : `${objectVisibilityResult.status}: ${objectVisibilityResult.blocking_object_id ?? "遮蔽物なし"}`;
  renderObjectPanel();
}

function renderObjectPanel(): void {
  objectPanel.innerHTML = "";
  const title = document.createElement("strong");
  title.textContent = "シーンアンカー";
  objectPanel.appendChild(title);
  const hint = document.createElement("div");
  hint.className = "object-hint";
  hint.textContent = proxyObject
    ? "面からの表示は一時的です。アンカーは現在の視点を保存します。"
    : "検証済みProxyがないため面からの表示は無効です。現在の視点は保存できます。";
  objectPanel.appendChild(hint);

  const actions = document.createElement("div");
  actions.className = "object-actions";
  const addCamera = document.createElement("button");
  addCamera.type = "button";
  addCamera.textContent = "現在の視点を保存";
  addCamera.addEventListener("click", addCurrentViewAnchor);
  actions.appendChild(addCamera);
  const addPerson = document.createElement("button");
  addPerson.type = "button";
  addPerson.textContent = "現在の視点に人物を追加";
  addPerson.addEventListener("click", addCurrentViewPerson);
  actions.appendChild(addPerson);
  objectPanel.appendChild(actions);
  const immutableHint = document.createElement("div");
  immutableHint.className = "object-hint";
  immutableHint.textContent = "このScene Packageは不変です。ここでのアンカー編集は表示中のみで、保存するには3D再構成（COLMAP）から新しいScene Packageを生成してください。";
  objectPanel.appendChild(immutableHint);

  const list = document.createElement("div");
  list.className = "object-list";
  for (const object of sceneObjectsDocument.objects) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = `${sceneObjectTypeLabel(object)}: ${object.name}`;
    button.classList.toggle("is-active", object.id === selectedObjectId);
    button.addEventListener("click", () => setSelectedObject(object.id));
    list.appendChild(button);
  }
  objectPanel.appendChild(list);

  const selected = selectedSceneObject();
  if (!selected) {
    const empty = document.createElement("div");
    empty.className = "object-hint";
    empty.textContent = "編集するオブジェクトを選択してください";
    objectPanel.appendChild(empty);
  } else {
    const inspector = document.createElement("div");
    inspector.className = "object-inspector";
    const heading = document.createElement("strong");
    heading.textContent = `${sceneObjectTypeLabel(selected)}: ${selected.name}`;
    inspector.appendChild(heading);
    const transformMode = document.createElement("label");
    transformMode.className = "object-field";
    transformMode.textContent = "変形モード";
    const transformSelect = document.createElement("select");
    for (const value of ["translate", "rotate"] as const) {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = value;
      transformSelect.appendChild(option);
    }
    transformMode.appendChild(transformSelect);
    inspector.appendChild(transformMode);
    inputField(inspector, "名前", selected.name, (value) => { selected.name = value || selected.name; objectUpdated(selected); });
    for (const [index, axis] of ["X", "Y", "Z"].entries()) {
      inputField(inspector, `位置 ${axis}`, String(selected.position[index]), (value) => {
        const parsed = Number(value);
        if (Number.isFinite(parsed)) { selected.position[index] = parsed; objectUpdated(selected); }
      }, "number", "0.001");
    }
    const enabled = document.createElement("label");
    enabled.className = "object-check";
    const enabledInput = document.createElement("input");
    enabledInput.type = "checkbox";
    enabledInput.checked = selected.enabled;
    enabledInput.addEventListener("change", () => { selected.enabled = enabledInput.checked; objectUpdated(selected); });
    enabled.append(enabledInput, document.createTextNode(" 有効"));
    inspector.appendChild(enabled);

    if (selected.type === "camera" && selected.camera) {
      const [yaw, pitch] = cameraYawPitch(selected);
      inputField(inspector, "面からの距離 (m)", String(selected.camera.surface_offset_m), (value) => {
        const parsed = Number(value);
        if (!Number.isFinite(parsed) || parsed <= 0) return;
        const normal = new THREE.Vector3(...selected.camera!.mount_surface_normal).normalize();
        const mountPoint = new THREE.Vector3(...selected.position).sub(normal.clone().multiplyScalar(selected.camera!.surface_offset_m));
        selected.camera!.surface_offset_m = parsed;
        const updatedPosition = mountPoint.add(normal.multiplyScalar(parsed));
        selected.position = [updatedPosition.x, updatedPosition.y, updatedPosition.z];
        objectUpdated(selected);
      }, "number", "0.01");
      inputField(inspector, "ヨー (度)", String(yaw), (value) => {
        const forward = cameraForwardFromYawPitch(Number(value), cameraYawPitch(selected)[1]);
        if (Number.isFinite(forward.x)) { setCameraForward(selected, forward); objectUpdated(selected); }
      }, "number", "1");
      inputField(inspector, "ピッチ (度)", String(pitch), (value) => {
        const forward = cameraForwardFromYawPitch(cameraYawPitch(selected)[0], Number(value));
        if (Number.isFinite(forward.x)) { setCameraForward(selected, forward); objectUpdated(selected); }
      }, "number", "1");
      inputField(inspector, "垂直FOV (度)", String(selected.camera.vertical_fov_deg), (value) => {
        const parsed = Number(value);
        if (Number.isFinite(parsed) && parsed > 1 && parsed < 179) { selected.camera!.vertical_fov_deg = parsed; objectUpdated(selected); }
      }, "number", "1");
      const frustum = document.createElement("label");
      frustum.className = "object-check";
      const frustumInput = document.createElement("input");
      frustumInput.type = "checkbox";
      frustumInput.checked = selected.camera.frustum_visible;
      frustumInput.addEventListener("change", () => { selected.camera!.frustum_visible = frustumInput.checked; objectUpdated(selected); });
      frustum.append(frustumInput, document.createTextNode(" 視錐台を表示"));
      inspector.appendChild(frustum);
    }
    if (selected.type === "person" && selected.person) {
      inputField(inspector, "身長 (m)", String(selected.person.height_m), (value) => {
        const parsed = Number(value);
        if (Number.isFinite(parsed) && parsed > 0) { selected.person!.height_m = parsed; objectUpdated(selected); }
      }, "number", "0.01");
      inputField(inspector, "ヨー (度)", String(selected.person.yaw_deg), (value) => {
        const parsed = Number(value);
        if (Number.isFinite(parsed)) { setPersonYaw(selected, parsed); objectUpdated(selected); }
      }, "number", "1");
      const target = document.createElement("label");
      target.className = "object-field";
      target.textContent = "可視性の対象";
      const targetSelect = document.createElement("select");
      for (const value of ["torso", "head"] as const) {
        const option = document.createElement("option");
        option.value = value;
        option.textContent = value;
        option.selected = selected.person.target_type === value;
        targetSelect.appendChild(option);
      }
      targetSelect.addEventListener("change", () => { selected.person!.target_type = targetSelect.value as "torso" | "head"; objectUpdated(selected); });
      target.appendChild(targetSelect);
      inspector.appendChild(target);
    }
    const objectActions = document.createElement("div");
    objectActions.className = "object-actions";
    if (selected.type === "camera") {
      const goToView = document.createElement("button");
      goToView.type = "button";
      goToView.textContent = "この視点へ移動";
      goToView.addEventListener("click", () => {
        const preset = sceneObjectToCameraPreset(selected);
        if (!preset) return;
        applyCameraPreset(preset);
        status.textContent = `${selected.name}の視点へ移動しました`;
      });
      objectActions.appendChild(goToView);
    }
    const duplicate = document.createElement("button");
    duplicate.type = "button";
    duplicate.textContent = "複製";
    duplicate.addEventListener("click", () => addSceneObject(cloneSceneObject(selected)));
    objectActions.appendChild(duplicate);
    const remove = document.createElement("button");
    remove.type = "button";
    remove.textContent = "削除";
    remove.addEventListener("click", () => {
      sceneObjectsDocument.objects = sceneObjectsDocument.objects.filter((object) => object.id !== selected.id);
      selectedObjectId = null;
      updateObjectAndRender();
    });
    objectActions.appendChild(remove);
    const clear = document.createElement("button");
    clear.type = "button";
    clear.textContent = "選択を解除";
    clear.addEventListener("click", () => setSelectedObject(null));
    objectActions.appendChild(clear);
    inspector.appendChild(objectActions);
    objectPanel.appendChild(inspector);
  }

  const analysis = document.createElement("div");
  analysis.className = "object-analysis";
  const analysisTitle = document.createElement("strong");
  analysisTitle.textContent = "可視性解析";
  analysis.appendChild(analysisTitle);
  const cameras = sceneObjectsDocument.objects.filter((object) => object.type === "camera");
  const people = sceneObjectsDocument.objects.filter((object) => object.type === "person");
  if (!proxyObject) {
    const unavailable = document.createElement("div");
    unavailable.className = "object-hint";
    unavailable.textContent = "品質検証済みProxyがないため解析を無効化しています。";
    analysis.appendChild(unavailable);
  } else if (cameras.length === 0 || people.length === 0) {
    const unavailable = document.createElement("div");
    unavailable.className = "object-hint";
    unavailable.textContent = "解析にはカメラアンカーと人物アンカーをそれぞれ1件以上追加してください。";
    analysis.appendChild(unavailable);
  } else {
    const cameraSelect = document.createElement("select");
    const personSelect = document.createElement("select");
    for (const object of cameras) {
      const option = document.createElement("option"); option.value = object.id; option.textContent = object.name; cameraSelect.appendChild(option);
    }
    for (const object of people) {
      const option = document.createElement("option"); option.value = object.id; option.textContent = object.name; personSelect.appendChild(option);
    }
    const analyze = document.createElement("button");
    analyze.type = "button";
    analyze.textContent = "可視性を確認";
    analyze.addEventListener("click", () => runPersonVisibilityCheck(cameraSelect.value, personSelect.value));
    analysis.append(cameraSelect, personSelect, analyze);
  }
  const result = document.createElement("div");
  result.className = "object-hint";
  result.textContent = objectVisibilityResult
    ? `結果: ${objectVisibilityResult.status} / 遮蔽物=${objectVisibilityResult.blocking_object_id ?? "なし"} / ヒット=${objectVisibilityResult.hit_distance_m?.toFixed(3) ?? "-"} m / 対象=${objectVisibilityResult.target_distance_m.toFixed(3)} m`
    : "解析は検証済みProxyのみを使用します。";
  analysis.appendChild(result);
  objectPanel.appendChild(analysis);
}

function setActiveCameraPreset(presetId: string): void {
  for (const frustum of cameraFrustumObjects.values()) {
    frustum.setActive(frustum.id === presetId);
  }

  const buttons = cameraPanel.querySelectorAll<HTMLButtonElement>("button[data-camera-id]");

  for (const button of buttons) {
    button.classList.toggle("is-active", button.dataset.cameraId === presetId);
  }
}

function applyCameraPreset(preset: CameraPreset): void {
  const [px, py, pz] = preset.position_m;
  const [tx, ty, tz] = preset.look_at_m;
  const [ux, uy, uz] = preset.up_world ?? [0, 1, 0];

  const target = new THREE.Vector3(tx, ty, tz);
  const presetUp = new THREE.Vector3(ux, uy, uz).normalize();
  const isAprilTagInspection = preset.id.startsWith("apriltag_");

  interactionQuality.reset();

  // AprilTag検証中はOrbitControlsに姿勢を再計算させない。
  controls.enabled = false;
  controls.target.copy(target);

  camera.up.copy(presetUp);
  camera.position.set(px, py, pz);
  camera.fov = preset.vertical_fov_deg;
  camera.updateProjectionMatrix();
  camera.lookAt(target);
  camera.updateMatrixWorld(true);

  // 通常表示へ戻る場合だけ、Y-up前提のOrbitControlsを再開する。
  if (!isAprilTagInspection) {
    controls.enabled = true;
    controls.update();
    camera.updateMatrixWorld(true);
  }

  setActiveCameraPreset(preset.id);
}

async function applyRequestedFrameAnalysisPose(): Promise<boolean> {
  if (!requestedFrameAnalysisId || !runtimeScenePackageId) return false;
  // Activation determines which run contributes to ordinary search results;
  // it does not invalidate the verified pose of a persisted historical or
  // partially completed record. Frame Analysis exposes those saved records
  // for inspection, so their Viewer links must remain usable as well.
  await restoreFrameAnalysisCameraPose(
    requestedFrameAnalysisId,
    runtimeScenePackageId,
    camera,
    controls,
    () => firstPersonNavigation.syncFromCamera(),
  );
  return true;
}

  function applyRequestedViewerFocus(): void {
    if (!requestedFocus) return;
    const offset = camera.position.clone().sub(controls.target);
    if (!offset.lengthSq() || !Number.isFinite(offset.lengthSq())) offset.set(0, 1.5, 4);
    controls.enabled = true;
    controls.target.copy(requestedFocus);
  camera.position.copy(requestedFocus).add(offset);
  camera.lookAt(requestedFocus);
  controls.update();
  camera.updateMatrixWorld(true);
  firstPersonNavigation.syncFromCamera();
}

function renderCameraPresetButtons(presets: CameraPreset[]): void {
  const title = document.createElement("strong");
  title.textContent = "カメラ";

  cameraPanel.innerHTML = "";
  cameraPanel.appendChild(title);

  const guidesToggle = document.createElement("button");
  guidesToggle.type = "button";
  guidesToggle.dataset.cameraGuidesToggle = "true";
  guidesToggle.addEventListener("click", () => setCameraGuidesVisible(!cameraGuidesVisible));
  cameraPanel.appendChild(guidesToggle);
  updateCameraGuidesButton();

  const debugToggle = document.createElement("button");
  debugToggle.type = "button";
  debugToggle.dataset.apriltagDebugToggle = "true";
  debugToggle.addEventListener("click", () => setAprilTagDebugVisible(!aprilTagDebugVisible));
  cameraPanel.appendChild(debugToggle);
  updateAprilTagDebugButton();

  if (presets.length === 0) {
    const empty = document.createElement("div");
    empty.className = "camera-panel-empty";
    empty.textContent = "カメラプリセットなし";
    cameraPanel.appendChild(empty);
    return;
  }

  for (const preset of presets) {
    const button = document.createElement("button");
    button.type = "button";
    button.dataset.cameraId = preset.id;
    button.textContent = preset.name;
    button.addEventListener("click", () => applyCameraPreset(preset));
    cameraPanel.appendChild(button);
  }
}

function setSurfacePlacementMode(enabled: boolean): void {
  if (enabled && !proxyObject) {
    status.textContent = "Surface view disabled: verified Proxy is unavailable";
    return;
  }
  if (enabled && !surfacePlacementMode) {
    surfaceViewPreviousProxyVisible = Boolean(proxyObject?.visible);
    proxyInteractionLayer?.setVisible(true);
  } else if (!enabled && !surfaceViewPreviousProxyVisible) {
    proxyInteractionLayer?.setVisible(false);
  }
  surfacePlacementMode = enabled;
  if (surfacePlacementButton) {
    surfacePlacementButton.textContent = enabled ? "面から表示: 面を選択" : "面から表示";
    surfacePlacementButton.classList.toggle("is-active", enabled);
  }
  status.textContent = enabled
    ? "Proxy面を選択すると、その面から表示します（カメラanchorは作成しません）"
    : "計測モード: Proxy上の2点を選択";
  renderObjectPanel();
}

function surfaceCameraPlacement(
  point: THREE.Vector3,
  normal: THREE.Vector3,
  trajectoryMedianY: number
): { semantic: SurfaceType; inward: THREE.Vector3; position: THREE.Vector3 } | null {
  if (!surfacePlacementSettings) {
    return null;
  }
  const upThreshold = surfacePlacementSettings.horizontal_threshold;
  const horizontal = Math.abs(normal.y) >= upThreshold;
  const semantic = horizontal ? (point.y <= trajectoryMedianY ? "floor" : "ceiling") : "wall";
  const inward = normal.clone();
  if (inward.dot(proxyCenter.clone().sub(point)) < 0) inward.negate();
  const position = point.clone().addScaledVector(inward, surfacePlacementSettings.common_offset_m);
  return { semantic, inward, position };
}

function viewFromSurface(point: THREE.Vector3, normal: THREE.Vector3, trajectoryMedianY: number): void {
  const placement = surfaceCameraPlacement(point, normal, trajectoryMedianY);
  if (!placement || !surfacePlacementSettings) {
    status.textContent = "Surface view disabled: configuration missing";
    return;
  }
  const preset: CameraPreset = {
    id: `surface_view_${Date.now()}`,
    name: `View from ${placement.semantic}`,
    position_m: [placement.position.x, placement.position.y, placement.position.z],
    look_at_m: [point.x, point.y, point.z],
    up_world: [0, 1, 0],
    vertical_fov_deg: 50,
    resolution: [1600, 1000]
  };
  applyCameraPreset(preset);
  setSurfacePlacementMode(false);
  status.textContent = `Viewing from ${placement.semantic} surface`;
}

function mountDynamicTrackingPanel(
  fixedTagMap: FixedTagMap | undefined,
  registry: DynamicObjectRegistry | undefined,
  loadError: string | undefined,
  dynamicOverlay: DynamicOverlayConfiguration | undefined,
  viewerFromRos: PoseTransform | undefined,
  fixedTagMapSource?: "package" | "derived",
  openSimulator?: () => void,
): void {
  dynamicTrackingClient?.disconnect();
  dynamicTrackingClient = undefined;
  dynamicDiagnosticUnsubscribe?.();
  dynamicDiagnosticUnsubscribe = undefined;
  if (dynamicStaleTimer !== undefined) {
    window.clearInterval(dynamicStaleTimer);
    dynamicStaleTimer = undefined;
  }
  if (dynamicRuntimeRenderFrame !== undefined) {
    window.cancelAnimationFrame(dynamicRuntimeRenderFrame);
    dynamicRuntimeRenderFrame = undefined;
  }
  dynamicObjectLayer?.hideAll();
  dynamicFixedTagDistanceOverlay?.dispose();
  if (dynamicFixedTagDistanceOverlay) {
    scene.remove(dynamicFixedTagDistanceOverlay.group);
    dynamicFixedTagDistanceOverlay = undefined;
  }
  dynamicPanel.innerHTML = "";

  const title = document.createElement("strong");
  title.textContent = "動的追跡";
  dynamicPanel.appendChild(title);
  const summary = document.createElement("div");
  summary.className = "dynamic-summary";
  dynamicPanel.appendChild(summary);

  if (!registry) {
    summary.textContent = loadError ? `利用不可: ${loadError}` : "このScene Packageでは未設定です";
    return;
  }
  if (!dynamicOverlay || !viewerFromRos) {
    summary.textContent = "利用不可: Scene Packageの動的座標または鮮度契約がありません";
    return;
  }
  if (!dynamicObjectLayer) {
    summary.textContent = "利用不可: 動的モデルの検証または読み込みに失敗しました";
    return;
  }
  dynamicFixedTagDistanceOverlay = createDynamicFixedTagDistanceOverlay();
  scene.add(dynamicFixedTagDistanceOverlay.group);

  const objectLabel = document.createElement("div");
  const runtimeBoxProfile = runtimeScenePackageId
    ? loadRuntimeDynamicBoxProfile(runtimeScenePackageId)
    : undefined;
  objectLabel.textContent = registry.objects.length
    ? `追跡対象: ROSで受信した box_<番号> を動的に表示します。設定済み上書き: ${registry.objects.map((object) =>
        object.model.format === "cube"
          ? `${object.id} (cube W${object.model.dimensions_m.width_m} × D${object.model.dimensions_m.depth_m} × H${object.model.dimensions_m.height_m}m)`
          : `${object.id} (GLB)`
      ).join(", ")}`
    : runtimeBoxProfile?.boxes.length
      ? `追跡対象: ${runtimeBoxProfile.boxes.map((box) => `${box.id} (W${box.dimensions_m.width_m} × D${box.dimensions_m.depth_m} × H${box.dimensions_m.height_m}m)`).join(", ")}（AprilTagカメラ確認の実行時設定）`
    : "追跡対象: ROSで受信した box_<番号> を動的に表示します（既定: 120サイズ cube W0.5 × D0.4 × H0.3m）。";
  dynamicPanel.appendChild(objectLabel);
  let activeFixedTagMap = fixedTagMap;
  let trackingRuntime: DynamicTrackingCollectionState = createDynamicTrackingCollectionState(
    registry,
  );
  let virtualFrameInput: HTMLInputElement | undefined;
  const connect = document.createElement("button");
  if (!activeFixedTagMap) {
    const virtualHint = document.createElement("div");
    virtualHint.textContent = "固定タグマップがありません。現在の視点を実行時専用の仮想タグとして登録できます。";
    dynamicPanel.appendChild(virtualHint);
    virtualFrameInput = document.createElement("input");
    virtualFrameInput.type = "text";
    virtualFrameInput.value = "apriltag_tag25h9_0";
    virtualFrameInput.ariaLabel = "virtual fixed tag frame";
    dynamicPanel.appendChild(virtualFrameInput);
    const virtualButton = document.createElement("button");
    virtualButton.type = "button";
    virtualButton.textContent = "現在の視点を仮想固定タグにする";
    virtualButton.addEventListener("click", () => {
      const frame = virtualFrameInput?.value.trim() || "apriltag_tag25h9_0";
      const viewQuaternion = new THREE.Quaternion();
      camera.getWorldQuaternion(viewQuaternion);
      activeFixedTagMap = {
        schema_version: "1.0.0",
        coordinate_frame: "gs_world",
        unit: "meter",
        rotation_order: "xyzw",
        frames: {
          [frame]: {
            translation_m: [controls.target.x, controls.target.y, controls.target.z],
            rotation_xyzw: [viewQuaternion.x, viewQuaternion.y, viewQuaternion.z, viewQuaternion.w],
          },
        },
      };
      trackingRuntime = createDynamicTrackingCollectionState(registry);
      summary.textContent = `Debug virtual anchor active: ${frame} — runtime-only, uncalibrated, and never a verified fixed tag map.`;
      connect.disabled = false;
      renderRuntimeState();
    });
    dynamicPanel.appendChild(virtualButton);
  } else {
    summary.textContent = fixedTagMapSource === "derived"
      ? `検証済みAprilTag四隅から固定タグを生成: ${Object.keys(activeFixedTagMap.frames).join(", ")}`
      : `固定タグマップ有効: ${Object.keys(activeFixedTagMap.frames).join(", ")}`;
  }
  if (openSimulator) {
    const simulatorHint = document.createElement("div");
    simulatorHint.className = "dynamic-summary";
    simulatorHint.textContent = "この読込済みScene Package/SPZだけを使う、ROS実験シミュレータを開けます。";
    dynamicPanel.appendChild(simulatorHint);
    const simulatorButton = document.createElement("button");
    simulatorButton.type = "button";
    simulatorButton.textContent = "実験シミュレータを開く";
    simulatorButton.addEventListener("click", openSimulator);
    dynamicPanel.appendChild(simulatorButton);
  }
  // Freshness is immutable package policy. Production URLs cannot override
  // this value because the same Scene Package must have the same tracking
  // behaviour in runtime and in a distribution ZIP.
  const effectiveTimeoutMs = dynamicOverlay.freshness.timeout_ms;
  const urlInput = document.createElement("input");
  urlInput.type = "url";
  urlInput.value = "ws://127.0.0.1:9090";
  urlInput.ariaLabel = "rosbridge URL";
  urlInput.placeholder = "rosbridge URL";
  dynamicPanel.appendChild(urlInput);
  const policyHint = document.createElement("div");
  policyHint.className = "dynamic-summary";
  policyHint.textContent = `ROS topic: ${dynamicOverlay.topic} / timeout: ${effectiveTimeoutMs} ms（Scene Package設定）`;
  dynamicPanel.appendChild(policyHint);

  // Optional, memory-only end-to-end tracing. It is deliberately isolated
  // from the tracking state machine: when stopped (the default), every probe
  // is a no-op and no browser storage or image data is written.
  const traceDetails = document.createElement("details");
  traceDetails.className = "dynamic-diagnostics dynamic-tracking-trace";
  const traceSummary = document.createElement("summary");
  traceSummary.textContent = "追跡診断ログ（任意）";
  const traceBody = document.createElement("div");
  traceBody.className = "dynamic-diagnostics-body";
  const traceHelp = document.createElement("p");
  traceHelp.textContent = "明示的に開始した間だけ、カメラ処理・ROS送受信・Viewer採否・stale/lost遷移をメモリへ記録します。画像は保存しません。";
  const traceActions = document.createElement("div");
  traceActions.className = "dynamic-trace-actions";
  const traceStart = document.createElement("button");
  traceStart.type = "button";
  traceStart.textContent = "診断ログを開始";
  const traceStop = document.createElement("button");
  traceStop.type = "button";
  traceStop.textContent = "診断ログを停止";
  const traceSave = document.createElement("button");
  traceSave.type = "button";
  traceSave.textContent = "診断ログをJSON保存";
  const traceClear = document.createElement("button");
  traceClear.type = "button";
  traceClear.textContent = "診断ログを破棄";
  const traceStatus = document.createElement("div");
  traceStatus.className = "dynamic-trace-status";
  traceActions.append(traceStart, traceStop, traceSave, traceClear);
  traceBody.append(traceHelp, traceActions, traceStatus);
  traceDetails.append(traceSummary, traceBody);
  dynamicPanel.appendChild(traceDetails);
  traceStart.addEventListener("click", () => {
    startDynamicTrackingDiagnostics({
      scene_package_id: runtimeScenePackageId ?? null,
      rosbridge_url: urlInput.value.trim(),
      ros_topic: dynamicOverlay.topic,
      package_timeout_ms: effectiveTimeoutMs,
      fixed_tag_frames: Object.keys(activeFixedTagMap?.frames ?? {}),
    });
  });
  traceStop.addEventListener("click", () => { stopDynamicTrackingDiagnostics(); });
  traceSave.addEventListener("click", () => {
    const snapshot = dynamicTrackingDiagnosticSnapshot();
    if (!snapshot) return;
    const blob = new Blob([`${JSON.stringify(snapshot, null, 2)}\n`], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = `dynamic-tracking-${snapshot.session_id}.json`;
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    URL.revokeObjectURL(url);
  });
  traceClear.addEventListener("click", () => { clearDynamicTrackingDiagnostics(); });
  dynamicDiagnosticUnsubscribe = subscribeDynamicTrackingDiagnostics((traceState) => {
    traceStart.disabled = traceState.active;
    traceStop.disabled = !traceState.active;
    traceSave.disabled = !traceState.has_snapshot;
    traceClear.disabled = !traceState.has_snapshot;
    traceStatus.textContent = traceState.active
      ? `記録中: ${traceState.event_count}件（画像なし・メモリのみ）`
      : traceState.has_snapshot
        ? `停止: ${traceState.event_count}件。JSON保存後は破棄できます。`
        : "停止中（無記録）";
  });
  const latest = document.createElement("div");
  latest.className = "dynamic-latest dynamic-box-status-card";
  const trackingCardTitle = document.createElement("strong");
  trackingCardTitle.textContent = "箱の状況: 0箱を受信中";
  latest.appendChild(trackingCardTitle);
  const stateChip = document.createElement("div");
  stateChip.className = "dynamic-state-chip dynamic-state-disconnected";
  stateChip.textContent = "未接続";
  latest.appendChild(stateChip);
  const summaryRows = document.createElement("div");
  summaryRows.className = "dynamic-summary-rows";
  latest.appendChild(summaryRows);
  const diagnostics = document.createElement("details");
  diagnostics.className = "dynamic-diagnostics";
  const diagnosticsSummary = document.createElement("summary");
  diagnosticsSummary.textContent = "開発者向け診断情報";
  diagnostics.appendChild(diagnosticsSummary);
  const diagnosticsBody = document.createElement("div");
  diagnosticsBody.className = "dynamic-diagnostics-body";
  diagnostics.appendChild(diagnosticsBody);
  latest.appendChild(diagnostics);
  const latestNotice = document.createElement("div");
  latestNotice.className = "dynamic-notice";
  latest.appendChild(latestNotice);

  const dockerDetails = document.createElement("details");
  dockerDetails.className = "dynamic-diagnostics";
  const dockerSummary = document.createElement("summary");
  dockerSummary.textContent = "Docker接続テスト（開発者向け）";
  dockerDetails.appendChild(dockerSummary);
  const dockerBody = document.createElement("div");
  dockerBody.className = "dynamic-diagnostics-body";
  const dockerTestCommand = "cd ~/spatial-3dgs-system && docker compose --profile dynamic-test up -d --build rosbridge-mock";
  dockerBody.textContent = dockerTestCommand;
  dockerDetails.appendChild(dockerBody);
  dynamicPanel.appendChild(dockerDetails);
  const copyDockerCommand = document.createElement("button");
  copyDockerCommand.type = "button";
  copyDockerCommand.textContent = "Dockerコマンドをコピー";
  copyDockerCommand.addEventListener("click", () => {
    if (!navigator.clipboard) {
      latestNotice.textContent = "クリップボードを利用できません。上の詳細を確認してください。";
      return;
    }
    void navigator.clipboard.writeText(dockerTestCommand)
      .then(() => { latestNotice.textContent = "Dockerテストコマンドをコピーしました。"; })
      .catch(() => { latestNotice.textContent = "コピーできませんでした。上の詳細を確認してください。"; });
  });
  dynamicPanel.appendChild(copyDockerCommand);
  connect.type = "button";
  connect.textContent = "接続";
  connect.disabled = !activeFixedTagMap;
  const disconnect = document.createElement("button");
  disconnect.type = "button";
  disconnect.textContent = "切断";
  disconnect.disabled = true;
  dynamicPanel.appendChild(connect);
  dynamicPanel.appendChild(disconnect);
  const focusObjectSelect = document.createElement("select");
  focusObjectSelect.ariaLabel = "focus dynamic object";
  let refreshRuntimeBoxEditor = (): void => {};
  const refreshFocusOptions = (): void => {
    const objectIds = Object.keys(trackingRuntime.object_states).sort((left, right) => left.localeCompare(right, undefined, { numeric: true }));
    refreshDynamicObjectSelect(focusObjectSelect, objectIds, refreshRuntimeBoxEditor);
  };
  refreshFocusOptions();
  dynamicPanel.appendChild(focusObjectSelect);

  const runtimeBoxEditor = document.createElement("section");
  runtimeBoxEditor.className = "dynamic-runtime-box-editor";
  const runtimeBoxEditorTitle = document.createElement("strong");
  runtimeBoxEditorTitle.textContent = "選択した箱の形状・AprilTag貼付面";
  const runtimeBoxEditorGrid = document.createElement("div");
  runtimeBoxEditorGrid.className = "dynamic-runtime-box-grid";
  const runtimeBoxField = (label: string, value: string): { label: HTMLLabelElement; input: HTMLInputElement } => {
    const wrapper = document.createElement("label");
    const caption = document.createElement("span");
    caption.textContent = label;
    const input = document.createElement("input");
    input.type = "number";
    input.min = "0.001";
    input.step = "0.001";
    input.value = value;
    wrapper.append(caption, input);
    return { label: wrapper, input };
  };
  const widthSetting = runtimeBoxField("幅 W (m)", String(DEFAULT_DYNAMIC_BOX_DIMENSIONS_M.width_m));
  const depthSetting = runtimeBoxField("奥行 D (m)", String(DEFAULT_DYNAMIC_BOX_DIMENSIONS_M.depth_m));
  const heightSetting = runtimeBoxField("高さ H (m)", String(DEFAULT_DYNAMIC_BOX_DIMENSIONS_M.height_m));
  const faceSettingLabel = document.createElement("label");
  const faceSettingCaption = document.createElement("span");
  faceSettingCaption.textContent = "AprilTag貼付面";
  const faceSetting = document.createElement("select");
  faceSetting.ariaLabel = "selected box AprilTag face";
  const faceOptions: Array<{ value: BoxTagFace; label: string }> = [
    { value: "top", label: "上面" },
    { value: "front", label: "前面" },
    { value: "right", label: "右面" },
    { value: "left", label: "左面" },
    { value: "back", label: "背面" },
    { value: "bottom", label: "底面" },
  ];
  for (const item of faceOptions) {
    const option = document.createElement("option");
    option.value = item.value;
    option.textContent = item.label;
    faceSetting.appendChild(option);
  }
  faceSettingLabel.append(faceSettingCaption, faceSetting);
  runtimeBoxEditorGrid.append(widthSetting.label, depthSetting.label, heightSetting.label, faceSettingLabel);
  const runtimeBoxApply = document.createElement("button");
  runtimeBoxApply.type = "button";
  runtimeBoxApply.textContent = "この箱へ適用";
  const runtimeBoxHint = document.createElement("div");
  runtimeBoxHint.className = "dynamic-summary";
  runtimeBoxHint.textContent = "ROSからbox_<番号>を受信すると、IDを選んで実物に合わせて調整できます。固定Tagには適用しません。";
  runtimeBoxEditor.append(runtimeBoxEditorTitle, runtimeBoxEditorGrid, runtimeBoxApply, runtimeBoxHint);
  const runtimeBoxTransfer = document.createElement("div");
  runtimeBoxTransfer.className = "dynamic-runtime-box-transfer";
  const exportBoxProfile = document.createElement("button");
  exportBoxProfile.type = "button";
  exportBoxProfile.textContent = "箱設定をJSON保存";
  const importBoxProfile = document.createElement("button");
  importBoxProfile.type = "button";
  importBoxProfile.textContent = "箱設定をJSON読込";
  const importBoxProfileInput = document.createElement("input");
  importBoxProfileInput.type = "file";
  importBoxProfileInput.accept = "application/json,.json";
  importBoxProfileInput.hidden = true;
  runtimeBoxTransfer.append(exportBoxProfile, importBoxProfile, importBoxProfileInput);
  runtimeBoxEditor.appendChild(runtimeBoxTransfer);
  dynamicPanel.appendChild(runtimeBoxEditor);

  const packageObjectIds = new Set(registry.objects.map((object) => object.id));
  refreshRuntimeBoxEditor = (): void => {
    const objectId = focusObjectSelect.value;
    const packageOwned = packageObjectIds.has(objectId);
    const configured = runtimeScenePackageId
      ? loadRuntimeDynamicBoxProfile(runtimeScenePackageId)?.boxes.find((box) => box.id === objectId)
      : undefined;
    const dimensions = configured?.dimensions_m ?? DEFAULT_DYNAMIC_BOX_DIMENSIONS_M;
    widthSetting.input.value = String(dimensions.width_m);
    depthSetting.input.value = String(dimensions.depth_m);
    heightSetting.input.value = String(dimensions.height_m);
    faceSetting.value = configured?.tag_face ?? "top";
    const disabled = !objectId || packageOwned || !runtimeScenePackageId;
    for (const control of [widthSetting.input, depthSetting.input, heightSetting.input, faceSetting, runtimeBoxApply]) {
      control.disabled = disabled;
    }
    runtimeBoxHint.textContent = packageOwned
      ? `${objectId}はScene Package定義済みモデルのため、実行時設定では上書きしません。`
      : objectId
        ? `${objectId}の表示寸法と、箱中心を求めるAprilTag貼付面を設定します。変更は同じブラウザのAprilTag送信画面へ同期されます。`
        : "ROSからbox_<番号>を受信すると、IDを選んで実物に合わせて調整できます。固定Tagには適用しません。";
  };
  focusObjectSelect.addEventListener("change", refreshRuntimeBoxEditor);
  runtimeBoxApply.addEventListener("click", () => {
    const objectId = focusObjectSelect.value;
    if (!runtimeScenePackageId || !objectId || packageObjectIds.has(objectId)) return;
    const dimensions = {
      width_m: Number(widthSetting.input.value),
      depth_m: Number(depthSetting.input.value),
      height_m: Number(heightSetting.input.value),
    };
    if (!Object.values(dimensions).every((value) => Number.isFinite(value) && value > 0)) {
      latestNotice.textContent = "箱の幅・奥行・高さは正の有限値にしてください。";
      return;
    }
    const current = loadRuntimeDynamicBoxProfile(runtimeScenePackageId)?.boxes ?? [];
    saveRuntimeDynamicBoxProfile(runtimeScenePackageId, [
      ...current.filter((box) => box.id !== objectId),
      { id: objectId, dimensions_m: dimensions, tag_face: faceSetting.value as BoxTagFace },
    ]);
    dynamicObjectLayer?.setRuntimeCubeDimensions(objectId, dimensions);
    latestNotice.textContent = `${objectId}へ W${dimensions.width_m} × D${dimensions.depth_m} × H${dimensions.height_m} m・Tag ${faceSetting.selectedOptions[0]?.textContent ?? faceSetting.value}を適用しました。`;
    refreshRuntimeBoxEditor();
  });
  exportBoxProfile.addEventListener("click", () => {
    if (!runtimeScenePackageId) return;
    const profile = loadRuntimeDynamicBoxProfile(runtimeScenePackageId);
    if (!profile) {
      latestNotice.textContent = "保存済みの箱設定がありません。先に箱を選び、寸法を適用してください。";
      return;
    }
    const blob = new Blob([exportRuntimeDynamicBoxProfile(profile)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `${runtimeScenePackageId}-box-profile.json`;
    link.click();
    window.setTimeout(() => URL.revokeObjectURL(url), 0);
    latestNotice.textContent = "箱設定JSONを保存しました。Scene Package本体は変更していません。";
  });
  importBoxProfile.addEventListener("click", () => importBoxProfileInput.click());
  importBoxProfileInput.addEventListener("change", () => {
    const file = importBoxProfileInput.files?.[0];
    importBoxProfileInput.value = "";
    if (!file || !runtimeScenePackageId) return;
    void file.text().then((text) => {
      const profile = importRuntimeDynamicBoxProfile(text, runtimeScenePackageId);
      for (const box of profile.boxes) dynamicObjectLayer?.setRuntimeCubeDimensions(box.id, box.dimensions_m);
      latestNotice.textContent = `${profile.boxes.length}箱の設定を読み込みました。`;
      refreshRuntimeBoxEditor();
    }).catch((error) => {
      latestNotice.textContent = `箱設定JSONを読込できません: ${String(error)}`;
    });
  });
  refreshRuntimeBoxEditor();
  const focusBox = document.createElement("button");
  focusBox.type = "button";
  focusBox.textContent = "選択した箱へフォーカス";
  focusBox.disabled = true;
  focusBox.addEventListener("click", () => {
    const state = trackingRuntime.object_states[focusObjectSelect.value];
    const pose = state?.last_pose;
    if (!pose) return;
    const target = new THREE.Vector3(...pose.translation_m);
    const direction = camera.position.clone().sub(controls.target);
    if (direction.lengthSq() < 1e-8) direction.set(1, 0.7, 1);
    direction.normalize();
    const runtimeDimensions = runtimeScenePackageId
      ? loadRuntimeDynamicBoxProfile(runtimeScenePackageId)?.boxes.find((box) => box.id === pose.object_id)?.dimensions_m
      : undefined;
    const object = resolveDynamicObjectConfig(registry, pose.object_id);
    const dimensions = runtimeDimensions ?? (!isDynamicRejection(object) && object.model.format === "cube"
      ? object.model.dimensions_m
      : undefined);
    const size = dimensions
      ? Math.hypot(dimensions.width_m, dimensions.depth_m, dimensions.height_m)
      : 0.8;
    controls.enabled = true;
    controls.target.copy(target);
    camera.position.copy(target).addScaledVector(direction, Math.max(1.25, size * 2.8));
    camera.lookAt(target);
    controls.update();
    latestNotice.textContent = `${pose.object_id}へフォーカスしました。`;
  });
  dynamicPanel.appendChild(focusBox);
  const distanceToggle = document.createElement("button");
  distanceToggle.type = "button";
  distanceToggle.textContent = "固定タグ↔箱の距離を表示";
  distanceToggle.disabled = true;
  let fixedTagDistanceVisible = false;
  const updateFixedTagDistance = (): void => {
    const overlay = dynamicFixedTagDistanceOverlay;
    let measurementCount = 0;
    for (const [objectId, state] of Object.entries(trackingRuntime.object_states)) {
      const pose = state?.last_pose;
      const fixedFrame = state?.active_fixed_tag_frame;
      const fixedPose = fixedFrame ? activeFixedTagMap?.frames[fixedFrame] : undefined;
      const canMeasure = Boolean(
        overlay && state && pose && fixedPose &&
        (state.tracking_state === "tracking" || state.tracking_state === "stale"),
      );
      if (!canMeasure || !overlay || !pose || !fixedPose || !state) {
        overlay?.clear(objectId);
        continue;
      }
      const suffix = state.tracking_alignment === "uncalibrated" ? " (debug anchor; uncalibrated)" : "";
      overlay.update(objectId, fixedPose.translation_m, pose.translation_m, suffix);
      measurementCount += 1;
    }
    distanceToggle.disabled = measurementCount === 0;
    overlay?.setVisible(fixedTagDistanceVisible);
  };
  distanceToggle.addEventListener("click", () => {
    fixedTagDistanceVisible = !fixedTagDistanceVisible;
    dynamicFixedTagDistanceOverlay?.setVisible(fixedTagDistanceVisible);
    distanceToggle.textContent = fixedTagDistanceVisible ? "固定タグ↔箱の距離を非表示" : "固定タグ↔箱の距離を表示";
  });
  dynamicPanel.appendChild(distanceToggle);
  const distanceLegend = document.createElement("div");
  distanceLegend.className = "dynamic-summary";
  distanceLegend.textContent = "距離: magenta=固定タグ中心 / 色付き=箱中心（箱ごとに独立した中心間距離・m）";
  dynamicPanel.appendChild(distanceLegend);
  const trackingCardToggle = document.createElement("button");
  trackingCardToggle.type = "button";
  trackingCardToggle.textContent = "箱の状況カードを隠す";
  let trackingCardVisible = true;
  trackingCardToggle.addEventListener("click", () => {
    trackingCardVisible = !trackingCardVisible;
    latest.hidden = !trackingCardVisible;
    trackingCardToggle.textContent = trackingCardVisible ? "箱の状況カードを隠す" : "箱の状況カードを表示";
  });
  dynamicPanel.appendChild(trackingCardToggle);
  dynamicPanel.appendChild(latest);

  let rosbridgeConnectionState: RosbridgeConnectionStatus = "disconnected";
  const renderRuntimeState = (detail?: string): void => {
    updateFixedTagDistance();
    refreshFocusOptions();
    const stateEntries = Object.entries(trackingRuntime.object_states)
      .sort(([left], [right]) => left.localeCompare(right, undefined, { numeric: true }));
    const states = stateEntries.map(([, state]) => state);
    const connectionState = rosbridgeConnectionState;
    const trackingCount = states.filter((state) => state?.tracking_state === "tracking").length;
    const staleCount = states.filter((state) => state?.tracking_state === "stale").length;
    const lostCount = states.filter((state) => state?.tracking_state === "lost").length;
    // A stale pose is still visible and only one receive-time tick away from
    // being lost, so preserve the warning until the configured timeout. Lost
    // poses are hidden and therefore no longer contribute to congestion.
    const detectedBoxCount = trackingCount + staleCount;
    congestionOverlay.update(detectedBoxCount, staleCount);
    const statusKey = connectionState === "disconnected"
      ? "disconnected"
      : trackingCount > 0
        ? "tracking"
        : staleCount > 0
          ? "stale"
          : lostCount === states.length && states.length > 0
            ? "lost"
            : trackingRuntime.latest_rejection
              ? "invalid"
              : "waiting";
    const statusLabels: Record<string, string> = {
      disconnected: "未接続",
      tracking: "受信中",
      stale: "更新遅延",
      lost: "追跡失効",
      invalid: "受信拒否",
      waiting: "受信待ち",
    };
    stateChip.className = `dynamic-state-chip dynamic-state-${statusKey}`;
    stateChip.textContent = statusKey === "tracking"
      ? `${statusLabels[statusKey]} ${trackingCount}/${states.length}`
      : statusKey === "stale"
        ? `${statusLabels[statusKey]} ${staleCount}/${states.length}`
        : statusLabels[statusKey] ?? statusKey;
    trackingCardTitle.textContent = states.length
      ? `箱の状況: ${trackingCount}/${states.length}箱を受信中`
      : "箱の状況: 検出待ち（ROSから box_<番号> を受信すると表示）";
    summaryRows.innerHTML = "";
    const addRow = (label: string, value: string): void => {
      const row = document.createElement("div");
      row.className = "dynamic-summary-row";
      const rowLabel = document.createElement("span");
      rowLabel.textContent = label;
      const rowValue = document.createElement("span");
      rowValue.textContent = value;
      row.append(rowLabel, rowValue);
      summaryRows.appendChild(row);
    };
    for (const [objectId, state] of stateEntries) {
      const pose = state?.last_pose;
      const fixedFrame = state?.active_fixed_tag_frame;
      const fixedPose = fixedFrame ? activeFixedTagMap?.frames[fixedFrame] : undefined;
      const measuredDistance = pose && fixedPose ? distanceM(fixedPose.translation_m, pose.translation_m) : undefined;
      const age = state?.last_received_monotonic_ms === undefined
        ? "—"
        : `${Math.max(0, Math.round(monotonicNowMs() - state.last_received_monotonic_ms))} ms`;
      const value = pose && (state.tracking_state === "tracking" || state.tracking_state === "stale")
        ? `${state.tracking_state} / ${fixedFrame ?? "—"} / ${measuredDistance?.toFixed(3) ?? "—"} m / ${age}`
        : `${state?.tracking_state ?? "waiting"} / 測定不可`;
      addRow(objectId, value);
    }
    diagnosticsBody.innerHTML = "";
    const diagnosticLines = stateEntries.flatMap(([objectId, state]) => {
      if (!state) return [`${objectId}: state unavailable`];
      return [
        `${objectId}: 接続=${state.connection_state}, 追跡=${state.tracking_state}, 固定タグ=${state.active_fixed_tag_frame ?? "—"}`,
        `  入力=${state.source_mode}, 座標整合=${state.tracking_alignment}, 時刻=${state.last_source_stamp_ms ?? "—"}`,
        `  受信=${state.accepted_message_count} / 拒否=${state.rejected_message_count}${state.latest_rejection_reason ? ` (${state.latest_rejection_reason})` : ""}`,
        state.last_pose ? `  位置=[${state.last_pose.translation_m.map((value) => value.toFixed(3)).join(", ")}]` : "  位置=—",
      ];
    });
    if (trackingRuntime.latest_rejection) {
      diagnosticLines.push(`最新拒否: ${trackingRuntime.latest_rejection.reason}: ${trackingRuntime.latest_rejection.detail}`);
    }
    if (detail) diagnosticLines.push(detail);
    diagnosticsBody.textContent = diagnosticLines.join("\n");
    const selectedState = trackingRuntime.object_states[focusObjectSelect.value];
    focusBox.disabled = !selectedState?.last_pose || selectedState.tracking_state === "lost";
    if (!detail) latestNotice.textContent = "";
  };

  let pendingRuntimeDetail: string | undefined;
  const scheduleRuntimeStateRender = (detail?: string): void => {
    if (detail) pendingRuntimeDetail = detail;
    if (dynamicRuntimeRenderFrame !== undefined) return;
    dynamicRuntimeRenderFrame = window.requestAnimationFrame(() => {
      dynamicRuntimeRenderFrame = undefined;
      const pendingDetail = pendingRuntimeDetail;
      pendingRuntimeDetail = undefined;
      renderRuntimeState(pendingDetail);
    });
  };

  const setClientStatus = (next: RosbridgeConnectionStatus, detail?: string): void => {
    recordDynamicTrackingDiagnostic("rosbridge_client", "connection_state", {
      status: next,
      detail: detail ?? null,
    });
    rosbridgeConnectionState = next;
    trackingRuntime = transitionDynamicTrackingCollectionConnection(trackingRuntime, next);
    if (next === "connecting" || next === "disconnected") {
      dynamicObjectLayer?.hideAll();
    }
    renderRuntimeState(detail);
    connect.disabled = next === "connecting" || next === "receiving";
    disconnect.disabled = next === "disconnected";
  };
  const onTransform = (message: unknown, receivedMonotonicMs: number): void => {
    const result = processDynamicTrackingCollectionMessage(
      message,
      activeFixedTagMap,
      registry,
      trackingRuntime,
      receivedMonotonicMs,
      undefined,
      viewerFromRos,
    );
    trackingRuntime = result.state;
    if (result.rejection) {
      recordDynamicTrackingDiagnostic("viewer_validation", "message_rejected", {
        reason: result.rejection.reason,
        detail: result.rejection.detail,
        received_monotonic_ms: receivedMonotonicMs,
      });
      scheduleRuntimeStateRender(`${result.rejection.reason}: ${result.rejection.detail}`);
      return;
    }
    if (!result.resolution || !dynamicObjectLayer?.setPose(result.resolution.pose)) {
      recordDynamicTrackingDiagnostic("viewer_validation", "render_rejected", {
        reason: result.resolution ? "dynamic_render_object_unavailable" : "resolution_unavailable",
        object_id: result.resolution?.pose.object_id ?? null,
        received_monotonic_ms: receivedMonotonicMs,
      });
      if (result.resolution) dynamicObjectLayer?.hide(result.resolution.pose.object_id);
      scheduleRuntimeStateRender("dynamic render object was unavailable");
      return;
    }
    recordDynamicTrackingDiagnostic("viewer_validation", "message_accepted", {
      object_id: result.resolution.pose.object_id,
      fixed_tag_frame: result.resolution.pose.fixed_tag_frame,
      source_stamp_ms: result.resolution.pose.source_stamp_ms,
      received_monotonic_ms: receivedMonotonicMs,
      translation_m: [...result.resolution.pose.translation_m],
      rotation_xyzw: [...result.resolution.pose.rotation_xyzw],
    });
    scheduleRuntimeStateRender();
  };
  dynamicTrackingClient = new RosbridgeTransformClient({
    onStatus: setClientStatus,
    onMessage: onTransform,
    topic: dynamicOverlay.topic,
  });
  connect.addEventListener("click", () => dynamicTrackingClient?.connect(urlInput.value.trim()));
  disconnect.addEventListener("click", () => {
    dynamicTrackingClient?.disconnect();
    dynamicObjectLayer?.hideAll();
    trackingRuntime = transitionDynamicTrackingCollectionConnection(trackingRuntime, "disconnected");
    renderRuntimeState();
  });
  dynamicStaleTimer = window.setInterval(() => {
    const previous = trackingRuntime;
    const freshnessNowMs = monotonicNowMs();
    trackingRuntime = advanceDynamicTrackingCollectionState(trackingRuntime, registry, freshnessNowMs, effectiveTimeoutMs);
    let detail: string | undefined;
    for (const objectId of Object.keys(trackingRuntime.object_states)) {
      const before = previous.object_states[objectId]?.tracking_state;
      const after = trackingRuntime.object_states[objectId]?.tracking_state;
      const nextState = trackingRuntime.object_states[objectId];
      if (before !== after) {
        recordDynamicTrackingDiagnostic("viewer_freshness", "state_transition", {
          object_id: objectId,
          from: before ?? null,
          to: after ?? null,
          receive_age_ms: nextState?.last_received_monotonic_ms === undefined
            ? null
            : Math.max(0, freshnessNowMs - nextState.last_received_monotonic_ms),
          package_timeout_ms: effectiveTimeoutMs,
          last_source_stamp_ms: nextState?.last_source_stamp_ms ?? null,
          accepted_message_count: nextState?.accepted_message_count ?? 0,
          rejected_message_count: nextState?.rejected_message_count ?? 0,
        });
      }
      if (after === "lost" && before !== "lost") {
        dynamicObjectLayer?.hide(objectId);
        detail = `${objectId}: last accepted message exceeded receive-time timeout`;
      } else if (after === "stale" && before !== "stale") {
        detail = `${objectId}: last accepted message is approaching timeout`;
      }
    }
    if (detail) renderRuntimeState(detail);
  }, 50);
}

async function loadSplat(): Promise<void> {
  try {
    const loadedScene = await loadSceneManifest(sceneManifestUrl);
    const {
      manifest,
      primarySplat,
      primarySplatUrl,
      cameraPresets,
      worldAlignment,
      aprilTagWorldCorners,
      sceneObjects,
      fixedTagMap,
      dynamicObjectRegistry,
      dynamicTrackingError,
      viewerFromRos,
      dynamicOverlay,
    } = loadedScene;

    // Generated runtime packages prior to the explicit Spark decoder contract
    // contain annotations in a plausible but incompatible coordinate space.
    // Keep the SPZ displayable, but fail closed for metric overlays and camera
    // presets rather than drawing them at a false location.
    const metricOverlayCoordinateVerified = !runtimeScenePackageId ||
      VERIFIED_GAUSSIAN_COORDINATE_CONTRACTS.has(
        manifest.provenance?.decoder_coordinate_contract?.id ?? "",
      );

    sceneObjectsDocument = sceneObjects;
    replaceSceneObjectVisuals();

    const allCameraPresets = [
      ...(metricOverlayCoordinateVerified ? cameraPresets.cameras : []),
      ...(loadedScene.proxyMetadata ? createProxyPlacementPresets(loadedScene.proxyMetadata) : [])
    ];
    sceneCameraPresets = allCameraPresets;
    renderCameraPresetButtons(allCameraPresets);
    renderAllCameraFrustums();
    renderObjectPanel();

    if (allCameraPresets.length > 0) {
      const requestedCameraId = new URLSearchParams(window.location.search).get("camera");
      const initialPreset =
        allCameraPresets.find((preset) => preset.id === requestedCameraId) ??
        allCameraPresets[0];
      applyCameraPreset(initialPreset);
    }
    applyRequestedViewerFocus();
    let frameAnalysisRestoreStatus = "";
    try {
      if (await applyRequestedFrameAnalysisPose()) {
        frameAnalysisRestoreStatus = `解析画像の撮影視点を復元しました: ${requestedFrameAnalysisId}`;
      }
    } catch (error) {
      const reason = error instanceof Error ? error.message : String(error);
      console.error("Frame analysis viewpoint restore failed", error);
      frameAnalysisRestoreStatus = `解析画像の撮影視点を復元できません: ${reason}`;
    }

    const withFrameAnalysisStatus = (message: string): string => (
      frameAnalysisRestoreStatus ? `${message} / ${frameAnalysisRestoreStatus}` : message
    );
    status.textContent = withFrameAnalysisStatus(`Loading splat: ${primarySplatUrl}`);

    const { mountGaussianSplatRuntime } = await import("./renderer/GaussianSplatRuntime");
    const gaussianRuntime = mountGaussianSplatRuntime(scene, renderer);
    const splat = gaussianRuntime.createSplat({
      url: primarySplatUrl,
      paged: primarySplat.paged
    });

    const transform = worldAlignment.generated_to_world;
    const [qx, qy, qz, qw] = transform.rotation_quaternion_xyzw;
    const [tx, ty, tz] = transform.translation_m;

    splat.position.set(tx, ty, tz);
    splat.quaternion.set(qx, qy, qz, qw);
    splat.scale.setScalar(transform.scale);

    scene.add(splat);

    const viewerFromRosPose: PoseTransform | undefined = viewerFromRos ? {
      translation_m: viewerFromRos.translation,
      rotation_xyzw: viewerFromRos.rotation_xyzw,
    } : undefined;
    let effectiveFixedTagMap = fixedTagMap;
    let effectiveDynamicTrackingError = dynamicTrackingError;
    let fixedTagMapSource: "package" | "derived" | undefined = fixedTagMap ? "package" : undefined;
    if (!effectiveFixedTagMap && !effectiveDynamicTrackingError) {
      if (!metricOverlayCoordinateVerified) {
        effectiveDynamicTrackingError = "AprilTag corners exist, but this legacy SPZ package lacks the Spark coordinate contract. Re-import the SPZ before dynamic tracking.";
      } else {
        const derived = deriveFixedTagMapFromAprilTagCorners(
          aprilTagWorldCorners,
          loadedScene.manifest.provenance?.metric_tracking?.status === "verified",
        );
        if (isDynamicRejection(derived)) {
          effectiveDynamicTrackingError = `AprilTag fixed tag map rejected: ${derived.reason}`;
        } else {
          effectiveFixedTagMap = derived;
          fixedTagMapSource = "derived";
        }
      }
    }
    const effectiveDynamicRegistry = dynamicObjectRegistry ??
      (!effectiveDynamicTrackingError ? createDefaultDynamicRegistry() : undefined);
    if (effectiveDynamicRegistry && !dynamicSimulatorRequested) {
      try {
        dynamicObjectLayer = await createDynamicObjectLayer(loadedScene.baseUrl, effectiveDynamicRegistry);
        const applyRuntimeBoxProfile = (): void => {
          if (!runtimeScenePackageId || !dynamicObjectLayer) return;
          const profile = loadRuntimeDynamicBoxProfile(runtimeScenePackageId);
          for (const box of profile?.boxes ?? []) {
            dynamicObjectLayer.setRuntimeCubeDimensions(box.id, box.dimensions_m);
          }
        };
        applyRuntimeBoxProfile();
        if (runtimeBoxProfileStorageListener) window.removeEventListener("storage", runtimeBoxProfileStorageListener);
        if (runtimeScenePackageId) {
          const profileKey = runtimeDynamicBoxProfileKey(runtimeScenePackageId);
          runtimeBoxProfileStorageListener = (event: StorageEvent): void => {
            if (event.storageArea === window.localStorage && event.key === profileKey) applyRuntimeBoxProfile();
          };
          window.addEventListener("storage", runtimeBoxProfileStorageListener);
        }
        scene.add(dynamicObjectLayer.group);
        console.info("Dynamic object layer ready", dynamicObjectLayer.objectIds);
      } catch (error) {
        dynamicObjectLayer = undefined;
        console.error("Dynamic object layer disabled", error);
      }
    } else if (effectiveDynamicTrackingError) {
      console.warn("Dynamic tracking disabled", effectiveDynamicTrackingError);
    }
    viewerSimulationSession?.dispose();
    viewerSimulationSession = undefined;
    if (!dynamicSimulatorRequested && runtimeScenePackageId && effectiveFixedTagMap && effectiveDynamicRegistry && !effectiveDynamicTrackingError) {
      viewerSimulationSession = startViewerSimulationSession(
        runtimeScenePackageId,
        Object.keys(effectiveFixedTagMap.frames),
        effectiveDynamicRegistry.objects.map((object) => object.id),
      );
    }
    dynamicSimulationPanelHandle?.dispose();
    dynamicSimulationPanelHandle = undefined;
    if (dynamicSimulatorRequested) {
      mountDynamicTrackingPanel(undefined, undefined, undefined, undefined, undefined);
      dynamicSimulationPanelHandle = mountDynamicSimulationPanel({
        panel: dynamicPanel,
        scene,
        camera,
        rendererElement: renderer.domElement,
        controls,
        scenePackageId: runtimeScenePackageId,
        parentSessionId: dynamicSimulatorParentSessionId,
        fixedTagMap: effectiveFixedTagMap,
        registry: effectiveDynamicRegistry,
      });
    } else {
      mountDynamicTrackingPanel(
        effectiveFixedTagMap,
        effectiveDynamicRegistry,
        effectiveDynamicTrackingError,
        dynamicOverlay,
        viewerFromRosPose,
        fixedTagMapSource,
        viewerSimulationSession
          ? () => {
              const url = new URL(window.location.href);
              url.searchParams.set("scene_package", runtimeScenePackageId!);
              url.searchParams.set("dynamic_simulator", "1");
              url.searchParams.set("viewer_session", viewerSimulationSession!.info.sessionId);
              window.open(url.toString(), "_blank", "noopener");
            }
          : undefined,
      );
    }

    if (metricOverlayCoordinateVerified) {
      aprilTagDebugLayer = createAprilTagDebugLayer(aprilTagWorldCorners);
      scene.add(aprilTagDebugLayer.group);
      aprilTagDebugLayer.setVisible(aprilTagDebugVisible);
      const allTagCorners = aprilTagCornerSets(aprilTagWorldCorners)
        .flatMap((tag) => tag.corners_world_m);
      proxyTarget = allTagCorners
        .reduce((sum, corner) => sum.add(new THREE.Vector3(...corner.xyz_world_m)), new THREE.Vector3())
        .multiplyScalar(1 / allTagCorners.length);
    } else {
      status.textContent = "Loaded scene package; metric overlays disabled (missing Spark SPZ coordinate contract)";
    }

    if (loadedScene.proxyMeshUrl && loadedScene.proxyMetadata && loadedScene.proxyQuality) {
      try {
        const gltf = await new GLTFLoader().loadAsync(loadedScene.proxyMeshUrl);
      proxyObject = gltf.scene;
      proxyObject.name = "Automatic verified proxy geometry";
      proxyObject.traverse((child) => {
        if (!(child instanceof THREE.Mesh)) return;
        const materials = Array.isArray(child.material) ? child.material : [child.material];
        const softened = materials.map((material) => {
          const clone = material.clone();
          clone.transparent = true;
          clone.opacity = 0.2;
          clone.depthWrite = false;
          clone.side = THREE.DoubleSide;
          return clone;
        });
        child.material = softened.length === 1 ? softened[0] : softened;
      });
      scene.add(proxyObject);
        proxyCenter = new THREE.Box3().setFromObject(proxyObject).getCenter(new THREE.Vector3());
        surfacePlacementSettings = loadedScene.proxyMetadata.generation_parameters?.surface_placement;
      const trajectoryHeights = cameraPresets.cameras.map((preset) => preset.position_m[1]).sort((a, b) => a - b);
      const trajectoryMedianY = trajectoryHeights.length ? trajectoryHeights[Math.floor(trajectoryHeights.length / 2)] : proxyCenter.y;
      proxyInteractionLayer = createProxyInteractionLayer(proxyObject, (point, normal) => {
        if (surfacePlacementMode) {
          viewFromSurface(point, normal, trajectoryMedianY);
        }
      });
      scene.add(proxyInteractionLayer.group);
      proxyPanel.innerHTML = "";
      const capability = document.createElement("div");
      capability.className = "capability-status capability-verified";
      capability.textContent = "Proxy品質: verified / 解析機能: 有効";
      proxyPanel.appendChild(capability);
      const splatToggle = document.createElement("button");
      splatToggle.type = "button";
      splatToggle.textContent = "SPZ: 表示";
      splatToggle.addEventListener("click", () => {
        splat.visible = !splat.visible;
        splatToggle.textContent = `SPZ: ${splat.visible ? "表示" : "非表示"}`;
      });
      proxyPanel.appendChild(splatToggle);
      const toggle = document.createElement("button");
      toggle.type = "button";
      toggle.textContent = "Proxy: 表示";
      toggle.addEventListener("click", () => {
        const visible = !proxyObject?.visible;
        proxyInteractionLayer?.setVisible(visible);
        toggle.textContent = `Proxy: ${visible ? "表示" : "非表示"}`;
      });
      proxyPanel.appendChild(toggle);
      proxyInteractionLayer.setVisible(false);
      toggle.textContent = "Proxy: 非表示";
      renderObjectPanel();
      const placement = document.createElement("button");
      placement.type = "button";
      surfacePlacementButton = placement;
      placement.addEventListener("click", () => setSurfacePlacementMode(!surfacePlacementMode));
      proxyPanel.appendChild(placement);
      const clearMeasurement = document.createElement("button");
      clearMeasurement.type = "button";
      clearMeasurement.textContent = "計測を消去";
      clearMeasurement.addEventListener("click", () => proxyInteractionLayer?.clearMeasurement());
      proxyPanel.appendChild(clearMeasurement);
      setSurfacePlacementMode(false);
      const occlusion = document.createElement("button");
      occlusion.type = "button";
        occlusion.textContent = "AprilTagの遮蔽を確認";
        occlusion.addEventListener("click", () => {
          const result = proxyObject
            ? evaluateProxyVisibility(proxyObject, camera.position, proxyTarget, "active_camera", "apriltag_center")
            : { status: "unavailable" as const };
          occlusion.dataset.result = JSON.stringify(result);
          occlusion.textContent = result.status === "occluded" ? "遮蔽あり" : result.status === "visible" ? "遮蔽なし" : "判定不可";
        });
        proxyPanel.appendChild(occlusion);
        if (import.meta.env.DEV) {
          (window as typeof window & {
          __viewerAcceptance?: {
              visibility: (origin: [number, number, number], target: [number, number, number]) => ReturnType<typeof evaluateProxyVisibility>;
              state: () => { proxy_loaded: boolean; placement_settings: typeof surfacePlacementSettings };
              pick_screen: (x: number, y: number) => { point: [number, number, number]; normal: [number, number, number]; classification: "floor" | "wall" | "ceiling" } | null;
            };
          }).__viewerAcceptance = {
            visibility: (origin, target) => evaluateProxyVisibility(
              proxyObject!,
              new THREE.Vector3(...origin),
              new THREE.Vector3(...target),
              "acceptance_camera",
              "acceptance_target"
            ),
            state: () => ({ proxy_loaded: Boolean(proxyObject), placement_settings: surfacePlacementSettings }),
            pick_screen: (x, y) => {
              if (!proxyObject || !surfacePlacementSettings) return null;
              const rect = renderer.domElement.getBoundingClientRect();
              const pointer = new THREE.Vector2(
                ((x - rect.left) / rect.width) * 2 - 1,
                -((y - rect.top) / rect.height) * 2 + 1
              );
              const picker = new THREE.Raycaster();
              picker.setFromCamera(pointer, camera);
              const hit = picker.intersectObject(proxyObject, true)[0];
              if (!hit) return null;
              const normal = hit.face?.normal.clone() ?? new THREE.Vector3(0, 1, 0);
              if (hit.object instanceof THREE.Mesh) normal.transformDirection(hit.object.matrixWorld);
              const trajectoryHeights = cameraPresets.cameras.map((preset) => preset.position_m[1]).sort((a, b) => a - b);
              const trajectoryMedianY = trajectoryHeights.length ? trajectoryHeights[Math.floor(trajectoryHeights.length / 2)] : proxyCenter.y;
              const point = hit.point.clone();
              const classification = Math.abs(normal.y) >= surfacePlacementSettings.horizontal_threshold
                ? (point.y <= trajectoryMedianY ? "floor" : "ceiling")
                : "wall";
              return { point: [point.x, point.y, point.z], normal: [normal.x, normal.y, normal.z], classification };
            },
          };
        }
      } catch (error) {
        console.error(error);
        proxyPanel.innerHTML = "";
        const capability = document.createElement("div");
        capability.className = "capability-status capability-failed";
        capability.textContent = "Proxy品質: failed / 解析機能: 無効";
        proxyPanel.appendChild(capability);
        const reason = document.createElement("details");
        const reasonSummary = document.createElement("summary");
        reasonSummary.textContent = "理由";
        reason.appendChild(reasonSummary);
        reason.appendChild(document.createTextNode("GLBの読み込みに失敗しました。"));
        proxyPanel.appendChild(reason);
      }
    } else {
      proxyPanel.innerHTML = "";
      const capability = document.createElement("div");
      capability.className = "capability-status capability-unverified";
      capability.textContent = "Proxy品質: unverified / 解析機能: 無効";
      proxyPanel.appendChild(capability);
      const reason = document.createElement("details");
      const reasonSummary = document.createElement("summary");
      reasonSummary.textContent = "理由";
      reason.appendChild(reasonSummary);
      reason.appendChild(document.createTextNode("品質検証済みProxyがScene Packageにありません。"));
      proxyPanel.appendChild(reason);
    }

    status.textContent = withFrameAnalysisStatus(metricOverlayCoordinateVerified
      ? `Loaded scene package: ${manifest.scene_id}`
      : `Loaded scene package: ${manifest.scene_id}; metric overlays disabled (missing Spark SPZ coordinate contract)`);
    void splat.initialized
      .then(() => {
        status.textContent = withFrameAnalysisStatus(metricOverlayCoordinateVerified
          ? `Loaded scene: ${manifest.scene_id}`
          : `Loaded scene: ${manifest.scene_id}; metric overlays disabled (missing Spark SPZ coordinate contract)`);
      })
      .catch((error) => {
        console.error(error);
        status.textContent = withFrameAnalysisStatus(`Loaded scene package: ${manifest.scene_id} (SPZ unavailable)`);
      });
  } catch (error) {
    console.error(error);
    const reason = error instanceof Error ? error.message : String(error);
    status.textContent = `Failed to load scene package: ${reason}`;
  }
}

void loadSplat();

renderer.domElement.addEventListener("pointerdown", (event) => {
  pointerGestureStart = { x: event.clientX, y: event.clientY };
  pointerGestureMoved = false;
});

renderer.domElement.addEventListener("pointermove", (event) => {
  if (!pointerGestureStart) return;
  if (Math.hypot(event.clientX - pointerGestureStart.x, event.clientY - pointerGestureStart.y) > 4) {
    pointerGestureMoved = true;
  }
});

renderer.domElement.addEventListener("pointercancel", () => {
  pointerGestureStart = undefined;
  pointerGestureMoved = true;
});

renderer.domElement.addEventListener("click", (event) => {
  if (pointerGestureMoved) {
    pointerGestureStart = undefined;
    pointerGestureMoved = false;
    return;
  }
  pointerGestureStart = undefined;
  const objectId = pickSceneObject(sceneObjectsRoot, event, camera, renderer);
  if (objectId) {
    setSelectedObject(objectId);
    return;
  }
  aprilTagDebugLayer?.handleClick(event, camera, renderer);
  proxyInteractionLayer?.handleClick(event, camera, renderer);
});

function onResize(): void {
  camera.aspect = window.innerWidth / window.innerHeight;
  camera.updateProjectionMatrix();

  interactionQuality.resize();
}

window.addEventListener("resize", onResize);

const IDLE_RENDER_INTERVAL_MS = 1000 / 30;
let lastRenderedAt = -Infinity;
renderer.setAnimationLoop((timestamp) => {
  // A 30 fps idle cap is sufficient for a stationary splat scene and leaves
  // GPU/CPU headroom for the live camera pipeline in another window. During
  // direct first-person interaction, render every display frame.
  if (!interactionQuality.interacting && timestamp - lastRenderedAt < IDLE_RENDER_INTERVAL_MS) return;
  lastRenderedAt = timestamp;
  if (interactionQuality.interacting) {
    controls.update();
  }
  firstPersonNavigation.update(Math.min(navigationClock.getDelta(), 0.1));
  aprilTagDebugLayer?.updateLabels(camera, renderer);
  proxyInteractionLayer?.updateLabels(camera, renderer);
  dynamicFixedTagDistanceOverlay?.updateLabel(camera, renderer);
  renderer.render(scene, camera);
});
}
