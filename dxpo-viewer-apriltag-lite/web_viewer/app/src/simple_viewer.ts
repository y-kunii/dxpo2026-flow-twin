import "./style.css";
import "./simple_viewer.css";
import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { TransformControls } from "three/addons/controls/TransformControls.js";
import { createFirstPersonNavigation } from "./camera/FirstPersonNavigation";
import {
  requestedFrameAnalysisId as parseRequestedFrameAnalysisId,
  restoreFrameAnalysisCameraPose,
} from "./camera/FrameAnalysisCameraPose";
import { createAprilTagDebugLayer, type AprilTagDebugLayer } from "./measurement/AprilTagDebug";
import type { CameraPreset } from "./scene/CameraPresets";
import { createMetricRulerLayer } from "./simple/MetricRuler";
import { loadSimpleSceneManifest } from "./simple/SimpleSceneLoader";

const SPARK_SPZ_COORDINATE_CONTRACT = "spark_spz_decoder_rx180";
const VERIFIED_GAUSSIAN_COORDINATE_CONTRACTS = new Set([
  SPARK_SPZ_COORDINATE_CONTRACT,
  "spark_gaussian_decoder_rx180",
  "viewer_native_identity",
]);
const query = new URLSearchParams(window.location.search);
const requestedFrameAnalysisId = parseRequestedFrameAnalysisId(query);
const requestedPackage = query.get("scene_package");
const requestedOrDefaultPackage = requestedPackage || __SPATIAL_DEFAULT_SCENE_PACKAGE__;
const scenePackageId = requestedOrDefaultPackage && /^[A-Za-z0-9][A-Za-z0-9_.-]*$/.test(requestedOrDefaultPackage)
  ? requestedOrDefaultPackage
  : undefined;

if (!scenePackageId) {
  window.location.replace("/?preview=packages");
} else {
  mountSimpleViewer(scenePackageId);
}

function mountSimpleViewer(packageId: string): void {
  const app = document.querySelector<HTMLDivElement>("#app");
  if (!app) throw new Error("#app element not found");
  document.body.classList.add("simple-viewer");

  const scene = new THREE.Scene();
  scene.background = new THREE.Color(0x111827);
  const camera = new THREE.PerspectiveCamera(60, window.innerWidth / window.innerHeight, 0.01, 1000);
  camera.position.set(0, 1.5, 4);
  const renderer = new THREE.WebGLRenderer({ antialias: false, powerPreference: "high-performance" });
  const pixelRatio = (): number => Math.max(0.65, Math.min(
    window.devicePixelRatio,
    1.25,
    Math.sqrt((1600 * 900) / Math.max(1, window.innerWidth * window.innerHeight)),
  ));
  renderer.setPixelRatio(pixelRatio());
  renderer.setSize(window.innerWidth, window.innerHeight);
  app.appendChild(renderer.domElement);

  const controls = new OrbitControls(camera, renderer.domElement);
  // OrbitControls is retained only as the shared target used by presets,
  // frame-analysis pose restore, and the ruler. Direct navigation is handled
  // by FirstPersonNavigation so dragging rotates the camera in place instead
  // of orbiting the 3DGS scene around a remote point.
  controls.enableDamping = false;
  controls.enableRotate = false;
  controls.enablePan = false;
  controls.enableZoom = false;
  controls.target.set(0, 1, 0);
  controls.update();
  renderer.domElement.addEventListener("contextmenu", (event) => event.preventDefault());

  const navigationClock = new THREE.Clock();
  const firstPersonNavigation = createFirstPersonNavigation(camera, controls, renderer.domElement);

  const status = document.createElement("div");
  status.className = "simple-viewer-status";
  status.textContent = "Scene Packageを読み込んでいます…";
  document.body.appendChild(status);

  const tools = document.createElement("aside");
  tools.className = "simple-viewer-tools";
  const toolsHeader = document.createElement("header");
  const title = document.createElement("h1");
  title.textContent = "シンプル3DGS Viewer";
  const subtitle = document.createElement("p");
  subtitle.textContent = "AprilTagカメラ確認と、配置・寸法変更できる3D定規だけを備えた閲覧専用画面";
  toolsHeader.append(title, subtitle);
  tools.appendChild(toolsHeader);

  const tagSection = document.createElement("section");
  tagSection.className = "simple-viewer-tool-section";
  const tagTitle = document.createElement("h2");
  tagTitle.textContent = "AprilTagデバッグ";
  const tagToggleLabel = document.createElement("label");
  tagToggleLabel.className = "simple-viewer-toggle";
  const tagToggle = document.createElement("input");
  tagToggle.type = "checkbox";
  tagToggle.checked = true;
  tagToggleLabel.append(tagToggle, document.createTextNode("固定Tagの四隅・辺・対角線を表示"));
  const tagHint = document.createElement("p");
  tagHint.className = "simple-viewer-hint";
  tagHint.textContent = "四隅を2点クリックすると、その2点間の実距離も確認できます。";
  tagSection.append(tagTitle, tagToggleLabel, tagHint);
  tools.appendChild(tagSection);

  const ruler = createMetricRulerLayer();
  scene.add(ruler.group);
  const rulerTransform = new TransformControls(camera, renderer.domElement);
  rulerTransform.attach(ruler.group);
  rulerTransform.setMode("translate");
  rulerTransform.size = 0.78;
  const rulerTransformHelper = rulerTransform.getHelper();
  rulerTransformHelper.name = "Simple Viewer ruler transform gizmo";
  scene.add(rulerTransformHelper);
  const rulerSection = document.createElement("section");
  rulerSection.className = "simple-viewer-tool-section";
  const rulerTitle = document.createElement("h2");
  rulerTitle.textContent = "3D定規（meter）";
  const rulerToggleLabel = document.createElement("label");
  rulerToggleLabel.className = "simple-viewer-toggle";
  const rulerToggle = document.createElement("input");
  rulerToggle.type = "checkbox";
  rulerToggle.checked = true;
  rulerToggleLabel.append(rulerToggle, document.createTextNode("定規オブジェクトを表示"));

  const selectionBar = document.createElement("div");
  selectionBar.className = "simple-ruler-selection";
  const selectionState = document.createElement("span");
  selectionState.textContent = "選択中";
  const selectionButton = document.createElement("button");
  selectionButton.type = "button";
  selectionButton.textContent = "選択解除";
  selectionBar.append(selectionState, selectionButton);

  const modeActions = document.createElement("div");
  modeActions.className = "simple-ruler-modes";
  const moveMode = document.createElement("button");
  moveMode.type = "button";
  moveMode.textContent = "移動ギズモ";
  moveMode.dataset.active = "true";
  moveMode.setAttribute("aria-pressed", "true");
  const rotateMode = document.createElement("button");
  rotateMode.type = "button";
  rotateMode.textContent = "回転ギズモ";
  rotateMode.setAttribute("aria-pressed", "false");
  const scaleMode = document.createElement("button");
  scaleMode.type = "button";
  scaleMode.textContent = "長さギズモ";
  scaleMode.setAttribute("aria-pressed", "false");
  modeActions.append(moveMode, rotateMode, scaleMode);

  const makeNumberInput = (
    labelText: string,
    ariaLabel: string,
    step: string,
    minimum?: string,
    maximum?: string,
  ): { label: HTMLLabelElement; input: HTMLInputElement } => {
    const label = document.createElement("label");
    const caption = document.createElement("span");
    caption.textContent = labelText;
    const input = document.createElement("input");
    input.type = "number";
    input.step = step;
    input.setAttribute("aria-label", ariaLabel);
    if (minimum !== undefined) input.min = minimum;
    if (maximum !== undefined) input.max = maximum;
    label.append(caption, input);
    return { label, input };
  };

  const dimensionsGrid = document.createElement("div");
  dimensionsGrid.className = "simple-ruler-fields simple-ruler-dimensions";
  const lengthField = makeNumberInput("長さ (m)", "定規の長さ (m)", "0.01", "0.02", "100");
  const widthField = makeNumberInput("幅 (m)", "定規の幅 (m)", "0.005", "0.005", "10");
  const thicknessField = makeNumberInput("厚み (m)", "定規の厚み (m)", "0.001", "0.0005", "1");
  dimensionsGrid.append(lengthField.label, widthField.label, thicknessField.label);

  const positionGrid = document.createElement("div");
  positionGrid.className = "simple-ruler-fields simple-ruler-vector";
  const positionFields = ["X", "Y", "Z"].map((axis) => makeNumberInput(
    axis,
    `定規の位置 ${axis}`,
    "0.01",
  ));
  positionGrid.append(...positionFields.map((field) => field.label));

  const rotationGrid = document.createElement("div");
  rotationGrid.className = "simple-ruler-fields simple-ruler-vector";
  const rotationFields = ["X°", "Y°", "Z°"].map((axis) => makeNumberInput(
    axis,
    `定規の回転 ${axis}`,
    "1",
    "-360",
    "360",
  ));
  rotationGrid.append(...rotationFields.map((field) => field.label));

  const rulerActions = document.createElement("div");
  rulerActions.className = "simple-viewer-actions";
  const placeAtView = document.createElement("button");
  placeAtView.type = "button";
  placeAtView.textContent = "視点中心へ置く";
  const applyRuler = document.createElement("button");
  applyRuler.type = "button";
  applyRuler.textContent = "定規設定を反映";
  rulerActions.append(placeAtView, applyRuler);
  const rulerResult = document.createElement("p");
  rulerResult.className = "simple-ruler-result";
  const rulerHint = document.createElement("p");
  rulerHint.className = "simple-viewer-hint";
  rulerHint.textContent = "定規本体をクリックして選択します。左ドラッグで平行移動します。右ドラッグは定規中心を固定し、左右で長辺の方位角、上下で長辺の仰角を変更します。ロール方向にはねじれません。選択中のホイールで長さを変更できます。ギズモのボタンは軸操作の種類を切り替えます。空間クリックまたは『選択解除』で編集を終了します。位置は3DGS world座標、回転はdegreeです。";
  rulerSection.append(
    rulerTitle,
    rulerToggleLabel,
    selectionBar,
    modeActions,
    dimensionsGrid,
    positionGrid,
    rotationGrid,
    rulerActions,
    rulerResult,
    rulerHint,
  );
  tools.appendChild(rulerSection);
  document.body.appendChild(tools);

  const navigation = document.createElement("div");
  navigation.className = "simple-viewer-navigation";
  navigation.textContent = "FPS操作: 左ドラッグ=視点回転 ｜ 右ドラッグ=平行移動 ｜ WASD=前後左右 ｜ Q/E・Shift/Space=下降/上昇 ｜ ホイール=視線方向へ移動 ｜ 定規選択中は定規を操作";
  document.body.appendChild(navigation);

  const uiToggle = document.createElement("button");
  uiToggle.type = "button";
  uiToggle.className = "simple-viewer-ui-toggle";
  const setUiVisible = (visible: boolean): void => {
    document.body.classList.toggle("simple-viewer-ui-hidden", !visible);
    uiToggle.textContent = visible ? "UIを隠す" : "UIを表示";
    uiToggle.setAttribute("aria-pressed", String(!visible));
    uiToggle.setAttribute("aria-label", visible ? "Viewer UIを隠す" : "Viewer UIを表示");
    uiToggle.title = visible
      ? "操作パネル・読込状態・操作ガイドを隠します"
      : "操作パネル・読込状態・操作ガイドを表示します";
  };
  uiToggle.addEventListener("click", () => {
    setUiVisible(document.body.classList.contains("simple-viewer-ui-hidden"));
  });
  setUiVisible(true);
  document.body.appendChild(uiToggle);

  let tagLayer: AprilTagDebugLayer | undefined;
  let transformingRuler = false;
  let rulerDragOccurred = false;
  let rulerSelected = true;
  const renderRulerInputs = (): void => {
    const displayedLength = ruler.dimensions.lengthM * Math.abs(ruler.group.scale.x);
    lengthField.input.value = displayedLength.toFixed(3);
    widthField.input.value = ruler.dimensions.widthM.toFixed(3);
    thicknessField.input.value = ruler.dimensions.thicknessM.toFixed(4);
    positionFields.forEach((field, index) => {
      field.input.value = ruler.group.position.getComponent(index).toFixed(3);
    });
    rotationFields.forEach((field, index) => {
      const radians = [ruler.group.rotation.x, ruler.group.rotation.y, ruler.group.rotation.z][index];
      field.input.value = THREE.MathUtils.radToDeg(radians).toFixed(1);
    });
    rulerResult.dataset.state = "ready";
    rulerResult.textContent = `定規サイズ: ${displayedLength.toFixed(3)} × ${ruler.dimensions.widthM.toFixed(3)} × ${ruler.dimensions.thicknessM.toFixed(4)} m`;
  };
  const setRulerSelected = (selected: boolean): void => {
    rulerSelected = selected && rulerToggle.checked;
    rulerSection.dataset.selected = String(rulerSelected);
    selectionState.textContent = rulerSelected ? "選択中：マウス編集できます" : "未選択：定規本体をクリックしてください";
    selectionButton.textContent = rulerSelected ? "選択解除" : "定規を選択";
    rulerTransform.enabled = rulerSelected;
    rulerTransformHelper.visible = rulerSelected;
    if (rulerSelected) rulerTransform.attach(ruler.group);
    else rulerTransform.detach();
    for (const button of [moveMode, rotateMode, scaleMode]) button.disabled = !rulerSelected;
    renderer.domElement.style.cursor = rulerSelected ? "grab" : "default";
  };
  const ensureRulerSelected = (): void => {
    if (!rulerToggle.checked) {
      rulerToggle.checked = true;
      ruler.setVisible(true);
    }
    setRulerSelected(true);
  };
  const placeRulerAtTarget = (): void => {
    ensureRulerSelected();
    ruler.setPose(controls.target.clone(), new THREE.Euler(0, 0, 0, "XYZ"));
    renderRulerInputs();
  };
  const selectTransformMode = (mode: "translate" | "rotate" | "scale"): void => {
    ensureRulerSelected();
    rulerTransform.setMode(mode);
    rulerTransform.setSpace(mode === "translate" ? "world" : "local");
    rulerTransform.showX = true;
    rulerTransform.showY = mode !== "scale";
    rulerTransform.showZ = mode !== "scale";
    moveMode.dataset.active = String(mode === "translate");
    rotateMode.dataset.active = String(mode === "rotate");
    scaleMode.dataset.active = String(mode === "scale");
    moveMode.setAttribute("aria-pressed", String(mode === "translate"));
    rotateMode.setAttribute("aria-pressed", String(mode === "rotate"));
    scaleMode.setAttribute("aria-pressed", String(mode === "scale"));
    renderer.domElement.style.cursor = "grab";
  };
  moveMode.addEventListener("click", () => selectTransformMode("translate"));
  rotateMode.addEventListener("click", () => selectTransformMode("rotate"));
  scaleMode.addEventListener("click", () => selectTransformMode("scale"));
  selectionButton.addEventListener("click", () => {
    if (rulerSelected) setRulerSelected(false);
    else ensureRulerSelected();
  });
  placeAtView.addEventListener("click", placeRulerAtTarget);
  applyRuler.addEventListener("click", () => {
    const dimensions = [lengthField.input, widthField.input, thicknessField.input].map((input) => Number(input.value));
    const position = positionFields.map((field) => Number(field.input.value));
    const rotation = rotationFields.map((field) => Number(field.input.value));
    if (![...dimensions, ...position, ...rotation].every(Number.isFinite)) {
      rulerResult.dataset.state = "failed";
      rulerResult.textContent = "定規の寸法・位置・回転へ有限の数値を入力してください。";
      return;
    }
    try {
      ruler.group.scale.set(1, 1, 1);
      ruler.setDimensions({ lengthM: dimensions[0], widthM: dimensions[1], thicknessM: dimensions[2] });
      ruler.setPose(
        new THREE.Vector3(position[0], position[1], position[2]),
        new THREE.Euler(
          THREE.MathUtils.degToRad(rotation[0]),
          THREE.MathUtils.degToRad(rotation[1]),
          THREE.MathUtils.degToRad(rotation[2]),
          "XYZ",
        ),
      );
      ensureRulerSelected();
      renderRulerInputs();
    } catch (error) {
      rulerResult.dataset.state = "failed";
      rulerResult.textContent = error instanceof Error ? error.message : String(error);
    }
  });
  rulerTransform.addEventListener("dragging-changed", (event) => {
    transformingRuler = Boolean(event.value);
    if (transformingRuler) rulerDragOccurred = true;
    controls.enabled = !transformingRuler;
    if (!transformingRuler) {
      if (rulerTransform.getMode() === "scale") {
        const scaledLength = THREE.MathUtils.clamp(
          ruler.dimensions.lengthM * Math.abs(ruler.group.scale.x),
          0.02,
          100,
        );
        ruler.group.scale.set(1, 1, 1);
        ruler.setDimensions({ ...ruler.dimensions, lengthM: scaledLength });
      }
      renderRulerInputs();
    }
  });
  rulerTransform.addEventListener("objectChange", renderRulerInputs);
  rulerToggle.addEventListener("change", () => {
    ruler.setVisible(rulerToggle.checked);
    setRulerSelected(rulerToggle.checked);
  });
  tagToggle.addEventListener("change", () => tagLayer?.setVisible(tagToggle.checked));
  selectTransformMode("translate");
  setRulerSelected(true);
  renderRulerInputs();

  const applyCameraPreset = (preset: CameraPreset): void => {
    camera.position.set(...preset.position_m);
    camera.up.set(...(preset.up_world ?? [0, 1, 0]));
    camera.fov = preset.vertical_fov_deg;
    camera.near = preset.near_m ?? 0.01;
    camera.far = preset.far_m ?? 1000;
    controls.target.set(...preset.look_at_m);
    camera.updateProjectionMatrix();
    controls.update();
    camera.updateMatrixWorld(true);
    firstPersonNavigation.syncFromCamera();
  };

  const manifestUrl = `/scene_package/runtime/${encodeURIComponent(packageId)}/scene_manifest.json`;
  void loadSimpleSceneManifest(manifestUrl).then(async (loaded) => {
    if (loaded.cameraPresets.cameras.length > 0) applyCameraPreset(loaded.cameraPresets.cameras[0]);
    let frameAnalysisRestoreStatus = "";
    if (requestedFrameAnalysisId) {
      try {
        await restoreFrameAnalysisCameraPose(
          requestedFrameAnalysisId,
          packageId,
          camera,
          controls,
          () => firstPersonNavigation.syncFromCamera(),
        );
        frameAnalysisRestoreStatus = ` / 解析画像の撮影視点を復元しました: ${requestedFrameAnalysisId}`;
        status.dataset.frameAnalysisId = requestedFrameAnalysisId;
        status.dataset.frameAnalysisPose = camera.position.toArray().join(",");
      } catch (error) {
        const reason = error instanceof Error ? error.message : String(error);
        console.error("Frame analysis viewpoint restore failed", error);
        frameAnalysisRestoreStatus = ` / 解析画像の撮影視点を復元できません: ${reason}`;
        status.dataset.state = "warning";
      }
    }
    placeRulerAtTarget();
    const { mountGaussianSplatRuntime } = await import("./renderer/GaussianSplatRuntime");
    const gaussianRuntime = mountGaussianSplatRuntime(scene, renderer);
    const splat = gaussianRuntime.createSplat({ url: loaded.primarySplatUrl, paged: loaded.primarySplat.paged });
    const transform = loaded.worldAlignment.generated_to_world;
    splat.position.set(...transform.translation_m);
    splat.quaternion.set(...transform.rotation_quaternion_xyzw);
    splat.scale.setScalar(transform.scale);
    scene.add(splat);

    const metricOverlayVerified = loaded.manifest.provenance?.decoder_coordinate_contract?.id
      !== undefined
      && VERIFIED_GAUSSIAN_COORDINATE_CONTRACTS.has(
        loaded.manifest.provenance?.decoder_coordinate_contract?.id ?? "",
      );
    if (metricOverlayVerified) {
      tagLayer = createAprilTagDebugLayer(loaded.aprilTagWorldCorners);
      tagLayer.setVisible(tagToggle.checked);
      scene.add(tagLayer.group);
    } else {
      tagToggle.checked = false;
      tagToggle.disabled = true;
      tagHint.textContent = "この旧Packageには互換座標契約がないため、AprilTagデバッグを安全に表示できません。";
    }
    status.textContent = `読込済み: ${loaded.manifest.scene_id} / ${packageId}${frameAnalysisRestoreStatus}`;
    return splat.initialized;
  }).catch((error) => {
    console.error(error);
    status.dataset.state = "failed";
    status.textContent = `Scene Packageを開けません: ${error instanceof Error ? error.message : String(error)}`;
  });

  let pointerStart: { x: number; y: number } | undefined;
  let pointerMoved = false;
  const rulerRaycaster = new THREE.Raycaster();
  const rulerPointer = new THREE.Vector2();
  const rulerDragPlane = new THREE.Plane();
  const rulerDragIntersection = new THREE.Vector3();
  type DirectRulerDrag = {
    mode: "translate";
    pointerId: number;
    offset: THREE.Vector3;
  } | {
    mode: "rotate";
    pointerId: number;
    startPointer: THREE.Vector2;
    startYawRad: number;
    startPitchRad: number;
  };
  let directRulerDrag: DirectRulerDrag | undefined;
  const updateRulerPointerRay = (event: MouseEvent): void => {
    const rect = renderer.domElement.getBoundingClientRect();
    rulerPointer.set(
      ((event.clientX - rect.left) / rect.width) * 2 - 1,
      -((event.clientY - rect.top) / rect.height) * 2 + 1,
    );
    rulerRaycaster.setFromCamera(rulerPointer, camera);
  };
  const pointerHitsRuler = (event: MouseEvent): boolean => {
    if (!ruler.group.visible) return false;
    updateRulerPointerRay(event);
    return rulerRaycaster.intersectObject(ruler.group, true)
      .some((intersection) => intersection.object instanceof THREE.Mesh);
  };
  const finishDirectRulerDrag = (event: PointerEvent): void => {
    if (!directRulerDrag || directRulerDrag.pointerId !== event.pointerId) return;
    directRulerDrag = undefined;
    controls.enabled = true;
    renderer.domElement.style.cursor = "grab";
    if (renderer.domElement.hasPointerCapture(event.pointerId)) {
      renderer.domElement.releasePointerCapture(event.pointerId);
    }
    renderRulerInputs();
  };
  renderer.domElement.addEventListener("pointerdown", (event) => {
    pointerStart = { x: event.clientX, y: event.clientY };
    pointerMoved = false;
    if (!rulerSelected || transformingRuler || !pointerHitsRuler(event)) return;
    const mode = event.button === 0 ? "translate" : event.button === 2 ? "rotate" : undefined;
    if (!mode) return;
    if (mode === "translate") {
      updateRulerPointerRay(event);
      camera.getWorldDirection(rulerDragPlane.normal).normalize();
      rulerDragPlane.setFromNormalAndCoplanarPoint(rulerDragPlane.normal, ruler.group.position);
      if (!rulerRaycaster.ray.intersectPlane(rulerDragPlane, rulerDragIntersection)) return;
      directRulerDrag = {
        mode,
        pointerId: event.pointerId,
        offset: ruler.group.position.clone().sub(rulerDragIntersection),
      };
    } else if (mode === "rotate") {
      const longAxis = new THREE.Vector3(1, 0, 0).applyQuaternion(ruler.group.quaternion).normalize();
      directRulerDrag = {
        mode,
        pointerId: event.pointerId,
        startPointer: new THREE.Vector2(event.clientX, event.clientY),
        startYawRad: Math.atan2(longAxis.z, longAxis.x),
        startPitchRad: Math.asin(THREE.MathUtils.clamp(longAxis.y, -1, 1)),
      };
    }
    if (directRulerDrag) {
      controls.enabled = false;
      renderer.domElement.style.cursor = "grabbing";
      renderer.domElement.setPointerCapture(event.pointerId);
      event.preventDefault();
    }
  });
  renderer.domElement.addEventListener("pointermove", (event) => {
    if (directRulerDrag?.pointerId === event.pointerId) {
      pointerMoved = true;
      if (directRulerDrag.mode === "translate") {
        updateRulerPointerRay(event);
        if (rulerRaycaster.ray.intersectPlane(rulerDragPlane, rulerDragIntersection)) {
          ruler.group.position.copy(rulerDragIntersection).add(directRulerDrag.offset);
        }
      } else {
        const deltaX = event.clientX - directRulerDrag.startPointer.x;
        const deltaY = event.clientY - directRulerDrag.startPointer.y;
        const yaw = directRulerDrag.startYawRad + deltaX * 0.008;
        const pitch = THREE.MathUtils.clamp(
          directRulerDrag.startPitchRad - deltaY * 0.008,
          THREE.MathUtils.degToRad(-89),
          THREE.MathUtils.degToRad(89),
        );
        const longAxis = new THREE.Vector3(
          Math.cos(pitch) * Math.cos(yaw),
          Math.sin(pitch),
          Math.cos(pitch) * Math.sin(yaw),
        ).normalize();
        const localUp = new THREE.Vector3(0, 1, 0)
          .addScaledVector(longAxis, -longAxis.y)
          .normalize();
        const localSide = new THREE.Vector3().crossVectors(longAxis, localUp).normalize();
        const orientation = new THREE.Matrix4().makeBasis(longAxis, localUp, localSide);
        ruler.group.quaternion.setFromRotationMatrix(orientation).normalize();
      }
      renderRulerInputs();
      event.preventDefault();
      return;
    }
    if (pointerStart && Math.hypot(event.clientX - pointerStart.x, event.clientY - pointerStart.y) > 4) {
      pointerMoved = true;
    }
  });
  renderer.domElement.addEventListener("pointerup", (event) => {
    if (directRulerDrag?.pointerId === event.pointerId) {
      finishDirectRulerDrag(event);
      rulerDragOccurred = false;
      pointerStart = undefined;
      pointerMoved = false;
      return;
    }
    if (!pointerMoved && !rulerDragOccurred && !transformingRuler) {
      if (pointerHitsRuler(event)) {
        ensureRulerSelected();
      } else {
        setRulerSelected(false);
        tagLayer?.handleClick(event, camera, renderer);
      }
    }
    rulerDragOccurred = false;
    pointerStart = undefined;
    pointerMoved = false;
  });
  renderer.domElement.addEventListener("pointercancel", (event) => {
    finishDirectRulerDrag(event);
    rulerDragOccurred = false;
    pointerStart = undefined;
    pointerMoved = false;
  });
  renderer.domElement.addEventListener("contextmenu", (event) => {
    if (rulerSelected && pointerHitsRuler(event)) event.preventDefault();
  });
  renderer.domElement.addEventListener("wheel", (event) => {
    if (!rulerSelected || !rulerToggle.checked) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    const normalizedDelta = THREE.MathUtils.clamp(event.deltaY, -240, 240);
    const nextLength = THREE.MathUtils.clamp(
      ruler.dimensions.lengthM * Math.exp(-normalizedDelta * 0.0015),
      0.02,
      100,
    );
    ruler.group.scale.set(1, 1, 1);
    ruler.setDimensions({ ...ruler.dimensions, lengthM: nextLength });
    renderRulerInputs();
  }, { capture: true, passive: false });

  window.addEventListener("resize", () => {
    camera.aspect = window.innerWidth / window.innerHeight;
    camera.updateProjectionMatrix();
    renderer.setPixelRatio(pixelRatio());
    renderer.setSize(window.innerWidth, window.innerHeight);
  });
  renderer.setAnimationLoop(() => {
    firstPersonNavigation.update(Math.min(navigationClock.getDelta(), 0.1));
    tagLayer?.updateLabels(camera, renderer);
    ruler.updateLabel(camera, renderer);
    renderer.render(scene, camera);
  });
}
