import { api, detectAprilTagFrame, publishedScenePackages } from "./PreviewApiClient";
import { button, field, selectField } from "./PreviewUi";
import { RosbridgeTransformPublisher } from "../dynamic/RosbridgeTransformPublisher";
import { recordDynamicTrackingDiagnostic } from "../dynamic/DynamicTrackingDiagnostics";
import {
  DEFAULT_DYNAMIC_BOX_DIMENSIONS_M,
  type BoxTagFace,
} from "../dynamic/DynamicTrackingContract";
import {
  loadRuntimeDynamicBoxProfile,
  runtimeDynamicBoxProfileKey,
} from "../dynamic/RuntimeDynamicBoxProfile";
import {
  AprilTagViewerBridgeRuntime,
  validateBoxTagMappings,
  type AprilTagViewerBridgeConfiguration,
  type BoxTagMapping,
} from "./AprilTagViewerBridge";
import {
  parseCameraCalibration,
  sanitizedMediaTrackRecord,
  scaledCameraCalibration,
  type CameraCalibration,
} from "./CameraCalibration";

export type { AprilTagLiveCheckOptions } from "./AprilTagLiveSupport";
import {
  AUTO_EXPOSURE_SETTLE_MS,
  DARK_FRAME_LUMA_THRESHOLD,
  DARK_FRAME_RECOVERY_COUNT,
  INTEGRATED_VIEWER_SESSION_KEY,
  MAX_DIAGNOSTIC_FRAMES,
  MAX_LATENCY_COMPENSATION_MS,
  MOTION_EXPOSURE_SAFE_FLOOR,
  OVERLAY_HOLD_MS,
  SIDEBAR_RETENTION_MS,
  boundedVelocity,
  canvasBlob,
  finiteNumber,
  formatVector,
  loadViewerBridgeTargets,
  meanCanvasLuminance,
  numberInput,
  predictDetection,
  selectInput,
  smoothDetection,
  type AprilTagFamilyContract,
  type AprilTagLiveCheckOptions,
  type IntegratedViewerSession,
  type LiveDetectionResponse,
  type SeenDetection,
  type ViewerBridgeTarget,
} from "./AprilTagLiveSupport";
import { createVideoFrameClock } from "./VideoFrameClock";

export async function mountAprilTagLiveCheck(
  host: HTMLElement,
  options: AprilTagLiveCheckOptions = {},
): Promise<() => void> {
  host.replaceChildren();
  host.classList.add("apriltag-live-page");
  const [contract, packagePublication] = await Promise.all([
    api<AprilTagFamilyContract>("/api/apriltag-families"),
    publishedScenePackages().catch(() => ({ packages: [], source: "api" as const })),
  ]);
  const viewerBridgeTargets = await loadViewerBridgeTargets(packagePublication.packages);

  const heading = document.createElement("div");
  heading.className = "preview-heading";
  heading.innerHTML = "<h1>AprilTagカメラ確認</h1><p>撮影前にカメラ映像でAprilTagの検出、ID、BBOX、カメラ基準の位置・姿勢を確認します。画像は保存しません。</p>";

  const panel = document.createElement("section");
  panel.className = "preview-section apriltag-live-panel";
  const setupSection = document.createElement("section");
  setupSection.className = "apriltag-live-setup";
  const setupHeader = document.createElement("div");
  setupHeader.className = "apriltag-live-section-header";
  setupHeader.innerHTML = "<div><span>STEP 1</span><h2>認識条件を設定</h2></div><p>通常は推奨値のまま開始できます</p>";
  const controls = document.createElement("div");
  controls.className = "apriltag-live-controls";

  const cameraField = selectField("カメラ", "", "live_camera", [{ value: "", label: "既定カメラ" }]);
  const processingResolutionField = selectField("認識処理解像度", "960", "live_processing_width", [
    { value: "640", label: "640 px（最高速）" },
    { value: "960", label: "960 px（推奨）" },
    { value: "1280", label: "1280 px（小さいTag向け）" },
    { value: "native", label: "カメラ解像度のまま" },
  ]);
  const exposureProfileField = selectField("カメラ露光", "auto", "live_exposure_profile", [
    { value: "auto", label: "自動露光（推奨）" },
    { value: "motion", label: "動体優先（対応カメラ）" },
  ]);
  const familyField = selectField("AprilTag family", contract.default_family || "tag25h9", "live_family", contract.families);
  const sizeField = field("AprilTag size (m・固定/箱共通)", "0.1", "number", "live_tag_size");
  numberInput(sizeField).min = "0.001";
  numberInput(sizeField).step = "0.001";
  const intrinsicsMode = selectField("カメラ内部パラメータ", "fov", "live_intrinsics_mode", [
    { value: "fov", label: "水平画角から概算" },
    { value: "manual", label: "fx / fy / cx / cyを手入力" },
    { value: "calibration", label: "校正JSON（歪み補正あり・推奨）" },
  ]);
  const fovField = field("水平画角 (degree・概算用)", "60", "number", "live_horizontal_fov");
  numberInput(fovField).min = "10";
  numberInput(fovField).max = "170";
  numberInput(fovField).step = "0.1";
  const fxField = field("fx (pixel)", "", "number", "live_fx");
  const fyField = field("fy (pixel)", "", "number", "live_fy");
  const cxField = field("cx (pixel)", "", "number", "live_cx");
  const cyField = field("cy (pixel)", "", "number", "live_cy");
  for (const item of [fxField, fyField, cxField, cyField]) numberInput(item).step = "0.01";
  let loadedCalibration: CameraCalibration | undefined;
  const calibrationField = document.createElement("label");
  calibrationField.className = "preview-field apriltag-calibration-file";
  const calibrationLabel = document.createElement("span");
  calibrationLabel.textContent = "カメラ校正JSON";
  const calibrationInput = document.createElement("input");
  calibrationInput.type = "file";
  calibrationInput.accept = "application/json,.json";
  const calibrationStatus = document.createElement("small");
  calibrationStatus.textContent = "未読込";
  calibrationField.append(calibrationLabel, calibrationInput, calibrationStatus);
  controls.append(cameraField, familyField, sizeField, processingResolutionField, exposureProfileField);

  const advancedDetails = document.createElement("details");
  advancedDetails.className = "apriltag-live-advanced";
  const advancedSummary = document.createElement("summary");
  advancedSummary.textContent = "詳細設定（6DoF・カメラ内部パラメータ）";
  const advancedControls = document.createElement("div");
  advancedControls.className = "apriltag-live-advanced-grid";
  advancedControls.append(intrinsicsMode, calibrationField, fovField, fxField, fyField, cxField, cyField);
  advancedDetails.append(advancedSummary, advancedControls);

  const contractNote = document.createElement("div");
  contractNote.className = "preview-apriltag-mode apriltag-live-note";
  contractNote.setAttribute("role", "status");
  const updateControlMode = (): void => {
    const manual = selectInput(intrinsicsMode).value === "manual";
    const calibration = selectInput(intrinsicsMode).value === "calibration";
    calibrationField.hidden = !calibration;
    calibrationInput.disabled = !calibration;
    fovField.classList.toggle("is-disabled", manual || calibration);
    fovField.hidden = manual || calibration;
    numberInput(fovField).disabled = manual || calibration;
    for (const item of [fxField, fyField, cxField, cyField]) {
      item.classList.toggle("is-disabled", !manual);
      item.hidden = !manual;
      numberInput(item).disabled = !manual;
    }
    contractNote.innerHTML = "<strong>Tag IDは自動認識・familyは手動指定</strong><span>印刷したタグと同じfamilyを選び、黒枠の外側には背景と接しない白い余白を残してください。</span>";
  };
  calibrationInput.addEventListener("change", () => {
    const file = calibrationInput.files?.[0];
    if (!file) return;
    void file.text().then((text) => {
      loadedCalibration = parseCameraCalibration(JSON.parse(text));
      const camera = loadedCalibration.camera;
      calibrationStatus.textContent = `${camera.manufacturer ? `${camera.manufacturer} ` : ""}${camera.model} / ${loadedCalibration.image.width}×${loadedCalibration.image.height} / ${loadedCalibration.calibrated_at}`;
    }).catch((error) => {
      loadedCalibration = undefined;
      calibrationInput.value = "";
      calibrationStatus.textContent = `読込拒否: ${String(error)}`;
    });
  });
  selectInput(intrinsicsMode).addEventListener("change", updateControlMode);
  selectInput(familyField).addEventListener("change", updateControlMode);
  updateControlMode();

  const actionRow = document.createElement("div");
  actionRow.className = "preview-actions apriltag-live-actions";
  const cameraState = document.createElement("span");
  cameraState.className = "apriltag-live-camera-state is-stopped";
  cameraState.textContent = "カメラ停止中";
  const status = document.createElement("span");
  status.className = "preview-inline-status";
  status.textContent = "カメラを開始してください。";
  const diagnosticRow = document.createElement("div");
  diagnosticRow.className = "preview-actions apriltag-live-diagnostics";
  const diagnosticStatus = document.createElement("span");
  diagnosticStatus.className = "preview-inline-status";
  diagnosticStatus.textContent = "診断ログ: 停止中（画像は記録しません）";
  const includeDeviceIdentifiersLabel = document.createElement("label");
  includeDeviceIdentifiersLabel.className = "preview-check apriltag-diagnostic-privacy";
  const includeDeviceIdentifiers = document.createElement("input");
  includeDeviceIdentifiers.type = "checkbox";
  const includeDeviceIdentifiersText = document.createElement("span");
  includeDeviceIdentifiersText.textContent = "完全な機器識別情報を含める";
  includeDeviceIdentifiersLabel.append(includeDeviceIdentifiers, includeDeviceIdentifiersText);
  const diagnosticDetails = document.createElement("details");
  diagnosticDetails.className = "apriltag-live-diagnostic-panel";
  const diagnosticSummary = document.createElement("summary");
  diagnosticSummary.textContent = "診断・ログ（追従に問題がある場合）";
  diagnosticDetails.append(diagnosticSummary, diagnosticRow);

  const bridgeSection = document.createElement("section");
  bridgeSection.className = "apriltag-live-bridge";
  const bridgeHeader = document.createElement("div");
  bridgeHeader.className = "apriltag-live-section-header";
  bridgeHeader.innerHTML = "<div><span>OPTION</span><h2>Viewerへrosbridge送信</h2></div><p>固定Tagと箱Tagを同一画像で確定した場合だけ送信します</p>";
  const bridgeControls = document.createElement("div");
  bridgeControls.className = "apriltag-live-bridge-grid";
  const bridgeTargetOptions = viewerBridgeTargets.length > 0
    ? viewerBridgeTargets.map((target) => ({
      value: target.scenePackageId,
      label: `${target.projectName} — ${target.scenePackageId}`,
    }))
    : [{ value: "", label: "互換性のあるv1.2 Scene Packageがありません" }];
  const bridgeTargetField = selectField(
    "送信先Scene Package（座標契約）",
    viewerBridgeTargets[0]?.scenePackageId ?? "",
    "live_scene_package_target",
    bridgeTargetOptions,
  );
  bridgeTargetField.classList.add("apriltag-live-bridge-target");
  const bridgeUrlField = field("rosbridge URL", "ws://127.0.0.1:9090", "text", "live_rosbridge_url");
  const fixedTagIdField = field("固定Tag ID (0～9)", "0", "number", "live_fixed_tag_id");
  numberInput(fixedTagIdField).min = "0";
  numberInput(fixedTagIdField).max = "9";
  numberInput(fixedTagIdField).step = "1";
  const minimumMarginField = field("最小 decision margin", "10", "number", "live_minimum_margin");
  numberInput(minimumMarginField).min = "0";
  numberInput(minimumMarginField).step = "0.1";

  interface BoxMappingRowControls {
    element: HTMLDivElement;
    tagId: HTMLInputElement;
    childFrame: HTMLInputElement;
    remove: HTMLButtonElement;
  }
  const boxMappingsEditor = document.createElement("div");
  boxMappingsEditor.className = "apriltag-box-editor";
  const boxMappingsHeader = document.createElement("div");
  boxMappingsHeader.className = "apriltag-box-editor-header";
  boxMappingsHeader.innerHTML = "<div><strong>箱TagとViewer IDの対応</strong><span>Tag IDと受信後に表示するbox IDだけを対応付けます。箱の実寸・Tag貼付面は3DGS Viewer側で調整します。</span></div>";
  const boxMappingsList = document.createElement("div");
  boxMappingsList.className = "apriltag-box-list";
  const boxMappingRows: BoxMappingRowControls[] = [];
  const refreshBoxRows = (): void => {
    boxMappingRows.forEach((row, index) => {
      const number = index + 1;
      row.element.dataset.boxRow = String(number);
      row.tagId.setAttribute("aria-label", `箱${number} Tag ID`);
      row.childFrame.setAttribute("aria-label", `箱${number} child frame`);
      row.remove.disabled = boxMappingRows.length === 1;
    });
  };
  const addBoxMappingRow = (tagId: number, childFrame: string): void => {
    const row = document.createElement("div");
    row.className = "apriltag-box-row";
    const input = (value: string, type = "text"): HTMLInputElement => {
      const element = document.createElement("input");
      element.type = type;
      element.value = value;
      return element;
    };
    const tagIdInput = input(String(tagId), "number");
    tagIdInput.min = "20";
    tagIdInput.step = "1";
    const childFrameInput = input(childFrame);
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "apriltag-box-remove";
    remove.textContent = "削除";
    const controls: BoxMappingRowControls = {
      element: row,
      tagId: tagIdInput,
      childFrame: childFrameInput,
      remove,
    };
    remove.addEventListener("click", () => {
      const index = boxMappingRows.indexOf(controls);
      if (index < 0 || boxMappingRows.length === 1) return;
      boxMappingRows.splice(index, 1);
      row.remove();
      refreshBoxRows();
    });
    const labeled = (label: string, control: HTMLElement): HTMLLabelElement => {
      const wrapper = document.createElement("label");
      const caption = document.createElement("span");
      caption.textContent = label;
      wrapper.append(caption, control);
      return wrapper;
    };
    row.append(
      labeled("Tag ID", tagIdInput),
      labeled("Viewer ID", childFrameInput),
      remove,
    );
    boxMappingRows.push(controls);
    boxMappingsList.appendChild(row);
    refreshBoxRows();
  };
  const addBoxButton = document.createElement("button");
  addBoxButton.type = "button";
  addBoxButton.className = "apriltag-box-add";
  addBoxButton.textContent = "箱を追加";
  addBoxButton.addEventListener("click", () => {
    const next = boxMappingRows.reduce((maximum, row) => Math.max(maximum, Number(row.tagId.value) || 19), 19) + 1;
    addBoxMappingRow(next, `box_${String(boxMappingRows.length + 1).padStart(2, "0")}`);
  });
  boxMappingsHeader.appendChild(addBoxButton);
  boxMappingsEditor.append(boxMappingsHeader, boxMappingsList);
  addBoxMappingRow(20, "box_01");
  addBoxMappingRow(21, "box_02");
  addBoxMappingRow(22, "box_03");
  addBoxMappingRow(23, "box_04");

  const replaceBoxMappings = (mappings: IntegratedViewerSession["boxMappings"]): void => {
    boxMappingRows.splice(0, boxMappingRows.length);
    boxMappingsList.replaceChildren();
    for (const mapping of mappings) addBoxMappingRow(mapping.tagId, mapping.childFrame);
    if (boxMappingRows.length === 0) addBoxMappingRow(20, "box_01");
  };

  let restoredSession: IntegratedViewerSession | undefined;
  if (options.integratedViewerMode) {
    try {
      const candidate = JSON.parse(window.sessionStorage.getItem(INTEGRATED_VIEWER_SESSION_KEY) ?? "null") as Partial<IntegratedViewerSession> | null;
      const requestedPackage = new URLSearchParams(window.location.search).get("scene_package");
      if (candidate && candidate.scenePackageId === requestedPackage &&
          Array.isArray(candidate.boxMappings)) {
        restoredSession = candidate as IntegratedViewerSession;
        const targetSelect = selectInput(bridgeTargetField);
        if ([...targetSelect.options].some((item) => item.value === restoredSession?.scenePackageId)) {
          targetSelect.value = restoredSession.scenePackageId;
        }
        numberInput(bridgeUrlField).value = restoredSession.rosbridgeUrl;
        numberInput(fixedTagIdField).value = restoredSession.fixedTagId;
        numberInput(minimumMarginField).value = restoredSession.minimumMargin;
        selectInput(familyField).value = restoredSession.family;
        numberInput(sizeField).value = restoredSession.tagSizeM;
        selectInput(processingResolutionField).value = restoredSession.processingWidth;
        selectInput(exposureProfileField).value = restoredSession.exposureProfile;
        selectInput(intrinsicsMode).value = restoredSession.intrinsicsMode;
        numberInput(fovField).value = restoredSession.horizontalFov;
        numberInput(fxField).value = restoredSession.fx;
        numberInput(fyField).value = restoredSession.fy;
        numberInput(cxField).value = restoredSession.cx;
        numberInput(cyField).value = restoredSession.cy;
        loadedCalibration = restoredSession.calibration;
        if (loadedCalibration) calibrationStatus.textContent = `${loadedCalibration.camera.model} / ${loadedCalibration.image.width}×${loadedCalibration.image.height} / ${loadedCalibration.calibrated_at}`;
        replaceBoxMappings(restoredSession.boxMappings);
        updateControlMode();
      }
    } catch {
      window.sessionStorage.removeItem(INTEGRATED_VIEWER_SESSION_KEY);
    }
  }

  const readBoxMappings = (): BoxTagMapping[] => {
    const target = selectedBridgeTarget();
    const configuredBoxes = new Map(
      (target ? loadRuntimeDynamicBoxProfile(target.scenePackageId)?.boxes : undefined)
        ?.map((box) => [box.id, box] as const) ?? [],
    );
    return validateBoxTagMappings(boxMappingRows.map((row) => {
      const childFrame = row.childFrame.value.trim();
      const configured = configuredBoxes.get(childFrame);
      return {
        tag_id: finiteNumber(row.tagId, "箱Tag ID"),
        child_frame_id: childFrame,
        face: (configured?.tag_face ?? "top") as BoxTagFace,
        dimensions_m: configured?.dimensions_m ?? { ...DEFAULT_DYNAMIC_BOX_DIMENSIONS_M },
      };
    }));
  };
  bridgeControls.append(
    bridgeTargetField,
    bridgeUrlField,
    fixedTagIdField,
    minimumMarginField,
    boxMappingsEditor,
  );
  const bridgeNote = document.createElement("p");
  bridgeNote.className = "preview-help";
  bridgeNote.textContent = "送信先PackageのT_viewer_rosと固定Tag実寸を使用します。箱の実寸とTag貼付面は、受信したbox IDを3DGS Viewer側で選択して調整します。ROS契約は変更しません。";
  const bridgeActions = document.createElement("div");
  bridgeActions.className = "preview-actions apriltag-live-bridge-actions";
  const bridgeStatus = document.createElement("span");
  bridgeStatus.className = "preview-inline-status";
  bridgeStatus.textContent = "未接続";
  bridgeSection.append(bridgeHeader, bridgeControls, bridgeNote, bridgeActions);

  const cameraHeader = document.createElement("div");
  cameraHeader.className = "apriltag-live-section-header apriltag-live-camera-header";
  cameraHeader.innerHTML = "<div><span>STEP 2</span><h2>リアルタイムで確認</h2></div><p>四隅・ID・6DoFを連続表示します</p>";

  const workspace = document.createElement("div");
  workspace.className = "apriltag-live-workspace";
  const stage = document.createElement("div");
  stage.className = "apriltag-live-stage";
  const video = document.createElement("video");
  video.autoplay = true;
  video.muted = true;
  video.playsInline = true;
  const overlay = document.createElement("canvas");
  overlay.setAttribute("aria-label", "AprilTag BBOX overlay");
  const stageMessage = document.createElement("div");
  stageMessage.className = "apriltag-live-stage-message";
  stageMessage.textContent = "カメラ映像はまだ開始されていません。";
  stage.append(video, overlay, stageMessage);

  const sidebar = document.createElement("aside");
  sidebar.className = "apriltag-live-sidebar";
  const sidebarHeader = document.createElement("div");
  sidebarHeader.className = "apriltag-live-sidebar-header";
  const sidebarTitle = document.createElement("h2");
  sidebarTitle.textContent = "認識したTag";
  const detectionCount = document.createElement("strong");
  detectionCount.className = "apriltag-live-count";
  detectionCount.textContent = "0";
  detectionCount.setAttribute("aria-label", "現在の検出数");
  sidebarHeader.append(sidebarTitle, detectionCount);
  const summary = document.createElement("p");
  summary.className = "apriltag-live-summary";
  summary.textContent = "検出数: 0";
  const runtimeInfo = document.createElement("p");
  runtimeInfo.className = "apriltag-live-runtime";
  runtimeInfo.textContent = "カメラ設定を待っています";
  const tagList = document.createElement("div");
  tagList.className = "apriltag-live-tag-list";
  const empty = document.createElement("p");
  empty.className = "preview-empty-state";
  empty.textContent = "認識結果はまだありません。";
  tagList.appendChild(empty);
  const axisNote = document.createElement("p");
  axisNote.className = "apriltag-live-axis-note";
  axisNote.textContent = "6DoFはcamera_optical基準: +X右、+Y下、+Z前。校正JSONモードでは撮影解像度に合わせて内部パラメータを換算し、OpenCV plumb-bob歪み補正を適用します。";
  const axisDetails = document.createElement("details");
  axisDetails.className = "apriltag-live-axis-details";
  const axisSummary = document.createElement("summary");
  axisSummary.textContent = "座標系と精度について";
  axisDetails.append(axisSummary, axisNote);
  sidebar.append(sidebarHeader, summary, runtimeInfo, tagList, axisDetails);
  workspace.append(stage, sidebar);

  const capture = document.createElement("canvas");
  capture.hidden = true;
  const luminanceProbe = document.createElement("canvas");
  host.append(heading, panel);
  // The capture canvas is deliberately kept outside the DOM. It is only an
  // in-memory transfer buffer and must never appear as a second preview.
  setupSection.append(setupHeader, controls, advancedDetails, contractNote);
  panel.append(setupSection, bridgeSection, cameraHeader, actionRow, workspace, diagnosticDetails);

  let stream: MediaStream | undefined;
  let running = false;
  let disposed = false;
  let loopToken = 0;
  let overlayAnimationFrame = 0;
  let latestFrameSize = { width: 1, height: 1 };
  let cameraRuntime = "カメラ設定取得前";
  let consecutiveDarkFrames = 0;
  let recoveringExposure = false;
  let manualCameraStop = true;
  let cameraRecoveryAttempt = 0;
  let cameraRecoveryTimer: number | undefined;
  let trackEndedListener: (() => void) | undefined;
  let diagnosticRecording = false;
  let diagnosticStartedAt: string | undefined;
  let diagnosticEndedAt: string | undefined;
  let diagnosticSession: Record<string, unknown> | undefined;
  let diagnosticTruncated = false;
  let lastDiagnosticFrameAt: number | undefined;
  const diagnosticFrames: Array<Record<string, unknown>> = [];
  const diagnosticErrors: Array<Record<string, unknown>> = [];
  const seen = new Map<string, SeenDetection>();
  let bridgePublisher: RosbridgeTransformPublisher | undefined;
  let bridgeConfiguration: AprilTagViewerBridgeConfiguration | undefined;
  const bridgeRuntime = new AprilTagViewerBridgeRuntime();
  let bridgeLastSourceEpochMs = -1;
  let bridgeProfileStorageListener: ((event: StorageEvent) => void) | undefined;

  const videoFrameClock = createVideoFrameClock(video, () => !disposed && Boolean(stream));

  const setBridgeConfigurationDisabled = (disabled: boolean): void => {
    for (const control of bridgeControls.querySelectorAll<HTMLInputElement | HTMLSelectElement | HTMLButtonElement>("input, select, button")) control.disabled = disabled;
    if (!disabled) refreshBoxRows();
    selectInput(familyField).disabled = disabled;
    numberInput(sizeField).disabled = disabled;
  };

  const selectedBridgeTarget = (): ViewerBridgeTarget | undefined => {
    const selectedId = selectInput(bridgeTargetField).value;
    return viewerBridgeTargets.find((target) => target.scenePackageId === selectedId);
  };

  const selectedFixedTagContract = (target: ViewerBridgeTarget): ViewerBridgeTarget["fixedTags"][number] | undefined => {
    const family = selectInput(familyField).value;
    const fixedTagId = Number(numberInput(fixedTagIdField).value);
    return target.fixedTags.find((tag) => tag.family === family && tag.tagId === fixedTagId);
  };

  const updateBridgeTarget = (): void => {
    const target = selectedBridgeTarget();
    const fixedTag = target ? selectedFixedTagContract(target) : undefined;
    if (!target) {
      bridgeNote.textContent = "送信できません。動的overlayとT_viewer_rosを持つ公開済みv1.2 Scene Packageを先に登録してください。";
      return;
    }
    if (!fixedTag) {
      bridgeNote.textContent = `${target.projectName}: 選択中のfamily/固定Tag IDは、このScene Packageの固定Tag mapにありません。`;
      return;
    }
    numberInput(sizeField).value = String(fixedTag.physicalSideM);
    bridgeNote.textContent = `${target.projectName}: ${fixedTag.frameId} / Tag実寸 ${fixedTag.physicalSideM} m / PackageのT_viewer_rosで送信します。`;
  };

  selectInput(bridgeTargetField).addEventListener("change", updateBridgeTarget);
  selectInput(familyField).addEventListener("change", updateBridgeTarget);
  numberInput(fixedTagIdField).addEventListener("change", updateBridgeTarget);
  updateBridgeTarget();

  const disconnectBridge = (): void => {
    bridgePublisher?.disconnect();
    bridgePublisher = undefined;
    bridgeConfiguration = undefined;
    bridgeRuntime.reset();
    bridgeLastSourceEpochMs = -1;
    setBridgeConfigurationDisabled(false);
    bridgeConnectButton.disabled = viewerBridgeTargets.length === 0;
    bridgeDisconnectButton.disabled = true;
    bridgeStatus.textContent = "未接続";
    recordDynamicTrackingDiagnostic("apriltag_sender", "bridge_disconnected");
  };

  const connectBridge = (): void => {
    try {
      const target = selectedBridgeTarget();
      if (!target) throw new Error("送信先Scene Packageを選択してください。");
      const family = selectInput(familyField).value;
      if (family !== "tag25h9") throw new Error("箱Tag契約に従い、Viewer連携時はtag25h9を選択してください。");
      const fixedTagId = finiteNumber(numberInput(fixedTagIdField), "固定Tag ID");
      if (!Number.isInteger(fixedTagId) || fixedTagId < 0 || fixedTagId > 9) throw new Error("固定Tag IDは0～9の整数です。");
      const fixedTagContract = selectedFixedTagContract(target);
      if (!fixedTagContract) throw new Error("選択したScene Packageに、この固定Tagは登録されていません。");
      const tagSizeM = finiteNumber(numberInput(sizeField), "AprilTag size", true);
      if (Math.abs(tagSizeM - fixedTagContract.physicalSideM) > 1e-6) {
        throw new Error(`AprilTag sizeはScene Package登録値 ${fixedTagContract.physicalSideM} mに合わせてください。`);
      }
      const boxes = readBoxMappings();
      bridgeConfiguration = {
        family: "tag25h9",
        fixed_tag_id: fixedTagId,
        fixed_tags: target.fixedTags
          .filter((item) => item.family === family && Math.abs(item.physicalSideM - tagSizeM) <= 1e-6)
          .map((item) => ({ tag_id: item.tagId, frame_id: item.frameId, world_pose: item.worldPose })),
        boxes,
        minimum_decision_margin: finiteNumber(numberInput(minimumMarginField), "decision margin"),
        viewer_from_ros: target.viewerFromRos,
      };
      bridgeLastSourceEpochMs = -1;
      bridgeRuntime.reset();
      bridgePublisher?.disconnect();
      bridgePublisher = new RosbridgeTransformPublisher({
        onStatus: (connectionStatus, detail) => {
          recordDynamicTrackingDiagnostic("apriltag_sender", "bridge_connection_state", {
            status: connectionStatus,
            detail: detail ?? null,
          });
          if (connectionStatus === "receiving") {
            setBridgeConfigurationDisabled(true);
            bridgeConnectButton.disabled = true;
            bridgeDisconnectButton.disabled = false;
            bridgeStatus.textContent = "接続済み・同一フレームの固定Tag/箱Tagを待っています。";
          } else if (connectionStatus === "connecting") {
            bridgeStatus.textContent = "rosbridgeへ接続中…";
          } else if (connectionStatus === "rejected") {
            bridgeStatus.textContent = `接続エラー: ${detail ?? "rosbridge rejected"}`;
            setBridgeConfigurationDisabled(false);
            bridgeConnectButton.disabled = false;
            bridgeDisconnectButton.disabled = true;
          } else {
            bridgeStatus.textContent = "未接続";
          }
        },
      });
      bridgePublisher.connect(numberInput(bridgeUrlField).value.trim());
    } catch (error) {
      bridgeConfiguration = undefined;
      bridgeStatus.textContent = `設定エラー: ${String(error)}`;
      recordDynamicTrackingDiagnostic("apriltag_sender", "bridge_configuration_rejected", {
        reason: String(error),
      });
    }
  };

  const bridgeConnectButton = button("rosbridgeへ接続", connectBridge);
  bridgeConnectButton.classList.add("preview-primary");
  bridgeConnectButton.disabled = viewerBridgeTargets.length === 0;
  const bridgeDisconnectButton = button("切断", disconnectBridge, true);
  const openViewerButton = button(
    options.integratedViewerMode ? "Viewerを表示" : "同じ画面でScene Package Viewerを開く",
    () => {
    const target = selectedBridgeTarget();
    if (!target) return;
    try {
      const mappings = readBoxMappings();
      const requestedPackage = new URLSearchParams(window.location.search).get("scene_package");
      if (options.integratedViewerMode && requestedPackage === target.scenePackageId) {
        options.onShowViewer?.();
        return;
      }
      const session: IntegratedViewerSession = {
        scenePackageId: target.scenePackageId,
        rosbridgeUrl: numberInput(bridgeUrlField).value.trim(),
        fixedTagId: numberInput(fixedTagIdField).value,
        minimumMargin: numberInput(minimumMarginField).value,
        family: selectInput(familyField).value,
        tagSizeM: numberInput(sizeField).value,
        processingWidth: selectInput(processingResolutionField).value,
        exposureProfile: selectInput(exposureProfileField).value,
        intrinsicsMode: selectInput(intrinsicsMode).value,
        horizontalFov: numberInput(fovField).value,
        fx: numberInput(fxField).value,
        fy: numberInput(fyField).value,
        cx: numberInput(cxField).value,
        cy: numberInput(cyField).value,
        calibration: loadedCalibration,
        boxMappings: mappings.map((mapping) => ({
          tagId: mapping.tag_id,
          childFrame: mapping.child_frame_id,
        })),
      };
      window.sessionStorage.setItem(INTEGRATED_VIEWER_SESSION_KEY, JSON.stringify(session));
      const viewerUrl = new URL(target.viewerUrl, window.location.href);
      viewerUrl.searchParams.set("scene_package", target.scenePackageId);
      viewerUrl.searchParams.set("preview", "tag-check");
      viewerUrl.searchParams.set("live_viewer", "1");
      viewerUrl.searchParams.set("auto_start", "1");
      window.location.assign(viewerUrl.href);
    } catch (error) {
      bridgeStatus.textContent = `設定エラー: ${String(error)}`;
    }
    },
    viewerBridgeTargets.length === 0,
  );
  bridgeActions.append(bridgeConnectButton, bridgeDisconnectButton, openViewerButton, bridgeStatus);

  bridgeProfileStorageListener = (event: StorageEvent): void => {
    const target = selectedBridgeTarget();
    if (!target || event.storageArea !== window.localStorage ||
        event.key !== runtimeDynamicBoxProfileKey(target.scenePackageId)) return;
    try {
      if (bridgeConfiguration) {
        bridgeConfiguration = { ...bridgeConfiguration, boxes: readBoxMappings() };
        bridgeStatus.textContent = "Viewerで変更した箱の実寸・Tag貼付面を反映しました。";
      }
    } catch (error) {
      bridgeStatus.textContent = `Viewer設定の反映を拒否しました: ${String(error)}`;
    }
  };
  window.addEventListener("storage", bridgeProfileStorageListener);

  const publishBridgeFrame = (response: LiveDetectionResponse, sourceEpochMs: number): void => {
    const publisher = bridgePublisher;
    let configuration = bridgeConfiguration;
    if (!publisher?.connected || !configuration) {
      recordDynamicTrackingDiagnostic("apriltag_sender", "publish_batch_skipped", {
        reason: !configuration ? "bridge_not_configured" : "publisher_not_connected",
        detection_count: response.detections.length,
      });
      return;
    }
    if (sourceEpochMs <= bridgeLastSourceEpochMs) {
      bridgeStatus.textContent = "画像取得時刻が重複または逆行したため、このフレームは送信しませんでした。";
      recordDynamicTrackingDiagnostic("apriltag_sender", "publish_batch_skipped", {
        reason: "source_timestamp_not_increasing",
        source_frame_epoch_ms: sourceEpochMs,
        previous_source_frame_epoch_ms: bridgeLastSourceEpochMs,
      });
      return;
    }
    try {
      // The integrated Viewer lives in this document, so localStorage does not
      // emit a cross-document `storage` event when its box profile changes.
      // Read the Viewer-owned dimensions/tag face immediately before each
      // measurement instead of retaining stale adapter-side geometry.
      configuration = { ...configuration, boxes: readBoxMappings() };
      bridgeConfiguration = configuration;
      const batch = bridgeRuntime.build(response.detections, configuration, sourceEpochMs);
      if (batch.anchor) {
        recordDynamicTrackingDiagnostic("apriltag_sender", batch.anchor.switched ? "fixed_tag_anchor_switched" : "fixed_tag_anchor_state", {
          source_frame_epoch_ms: sourceEpochMs,
          ...batch.anchor,
          detail: batch.detail,
        });
      }
      if (batch.messages.length === 0) {
        bridgeStatus.textContent = batch.detail;
        recordDynamicTrackingDiagnostic("apriltag_sender", "publish_batch_empty", {
          detail: batch.detail,
          source_frame_epoch_ms: sourceEpochMs,
          detection_ids: response.detections.map((item) => `${item.family}:${item.tag_id}`),
          detector_detection_count: response.detector_detection_count ?? response.detection_count,
          tracked_detection_count: response.tracked_detection_count ?? 0,
        });
        return;
      }
      let published = 0;
      for (const message of batch.messages) if (publisher.publish(message)) published += 1;
      if (published > 0) bridgeLastSourceEpochMs = sourceEpochMs;
      recordDynamicTrackingDiagnostic("apriltag_sender", "publish_batch_completed", {
        source_frame_epoch_ms: sourceEpochMs,
        composed_count: batch.messages.length,
        published_count: published,
        child_frame_ids: batch.messages.map((item) => item.child_frame_id),
      });
      const stamp = batch.messages[0]?.header.stamp;
      bridgeStatus.textContent = published > 0
        ? `送信中: ${published}箱 / ${batch.messages[0].header.frame_id} / stamp ${stamp.sec}.${String(stamp.nanosec).padStart(9, "0")}`
        : "rosbridgeへ送信できませんでした。接続状態を確認してください。";
    } catch (error) {
      bridgeStatus.textContent = `送信拒否: ${String(error)}`;
      recordDynamicTrackingDiagnostic("apriltag_sender", "publish_batch_rejected", {
        reason: String(error),
        source_frame_epoch_ms: sourceEpochMs,
      });
    }
  };

  const updateCameraState = (state: "stopped" | "starting" | "running" | "warning", label: string): void => {
    cameraState.className = `apriltag-live-camera-state is-${state}`;
    cameraState.textContent = label;
  };

  const diagnosticConfiguration = (): Record<string, unknown> => {
    const track = stream?.getVideoTracks()[0];
    let capabilities: unknown;
    try {
      capabilities = typeof track?.getCapabilities === "function" ? track.getCapabilities() : undefined;
    } catch {
      capabilities = undefined;
    }
    return {
      selected_family: selectInput(familyField).value,
      processing_width: selectInput(processingResolutionField).value,
      exposure_profile: selectInput(exposureProfileField).value,
      tag_size_m_input: numberInput(sizeField).value,
      intrinsics_mode: selectInput(intrinsicsMode).value,
      horizontal_fov_input: numberInput(fovField).value,
      manual_intrinsics_input: {
        fx: numberInput(fxField).value,
        fy: numberInput(fyField).value,
        cx: numberInput(cxField).value,
        cy: numberInput(cyField).value,
      },
      camera: {
        runtime: cameraRuntime,
        label: track?.label,
        settings: track ? sanitizedMediaTrackRecord(track.getSettings() as unknown as Record<string, unknown>, includeDeviceIdentifiers.checked) : undefined,
        capabilities: capabilities && typeof capabilities === "object"
          ? sanitizedMediaTrackRecord(capabilities as Record<string, unknown>, includeDeviceIdentifiers.checked)
          : capabilities,
      },
      browser: {
        user_agent: navigator.userAgent,
        platform: navigator.platform,
        language: navigator.language,
        device_pixel_ratio: window.devicePixelRatio,
      },
      page_url: window.location.href,
    };
  };

  const updateDiagnosticStatus = (): void => {
    diagnosticStatus.textContent = diagnosticRecording
      ? `診断ログ: 記録中 ${diagnosticFrames.length}フレーム（画像なし）`
      : diagnosticFrames.length > 0 || diagnosticErrors.length > 0
        ? `診断ログ: 停止中 ${diagnosticFrames.length}フレーム／エラー${diagnosticErrors.length}件。JSONを保存できます。`
        : "診断ログ: 停止中（画像は記録しません）";
  };

  const stopDiagnostics = (): void => {
    if (!diagnosticRecording) return;
    diagnosticRecording = false;
    diagnosticEndedAt = new Date().toISOString();
    diagnosticStartButton.disabled = !running;
    diagnosticStopButton.disabled = true;
    diagnosticSaveButton.disabled = diagnosticFrames.length === 0 && diagnosticErrors.length === 0;
    updateDiagnosticStatus();
  };

  const startDiagnostics = (): void => {
    if (!running) {
      diagnosticStatus.textContent = "先にカメラを開始してください。";
      return;
    }
    diagnosticFrames.length = 0;
    diagnosticErrors.length = 0;
    diagnosticTruncated = false;
    lastDiagnosticFrameAt = undefined;
    diagnosticStartedAt = new Date().toISOString();
    diagnosticEndedAt = undefined;
    diagnosticSession = diagnosticConfiguration();
    diagnosticRecording = true;
    diagnosticStartButton.disabled = true;
    diagnosticStopButton.disabled = false;
    diagnosticSaveButton.disabled = true;
    updateDiagnosticStatus();
  };

  const saveDiagnostics = (): void => {
    stopDiagnostics();
    if (!diagnosticSession || (diagnosticFrames.length === 0 && diagnosticErrors.length === 0)) {
      diagnosticStatus.textContent = "保存できる診断ログがありません。";
      return;
    }
    const payload = {
      schema_version: "apriltag_live_session_diagnostics_v1",
      started_at: diagnosticStartedAt,
      ended_at: diagnosticEndedAt ?? new Date().toISOString(),
      truncated: diagnosticTruncated,
      maximum_frames: MAX_DIAGNOSTIC_FRAMES,
      privacy: {
        camera_images_recorded: false,
        device_identifiers_recorded: includeDeviceIdentifiers.checked,
      },
      session: diagnosticSession,
      frames: diagnosticFrames,
      errors: diagnosticErrors,
    };
    const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" });
    const downloadUrl = URL.createObjectURL(blob);
    const link = document.createElement("a");
    const stamp = (diagnosticStartedAt ?? new Date().toISOString()).replace(/[:.]/g, "-");
    link.href = downloadUrl;
    link.download = `apriltag-tracking-${stamp}.json`;
    link.click();
    window.setTimeout(() => URL.revokeObjectURL(downloadUrl), 0);
    diagnosticStatus.textContent = `診断ログを保存しました（${diagnosticFrames.length}フレーム、画像なし）。`;
  };

  const diagnosticStartButton = button("診断ログを開始", startDiagnostics, true);
  const diagnosticStopButton = button("診断ログを停止", stopDiagnostics, true);
  const diagnosticSaveButton = button("診断ログをJSON保存", saveDiagnostics, true);
  diagnosticRow.append(diagnosticStartButton, diagnosticStopButton, diagnosticSaveButton, includeDeviceIdentifiersLabel, diagnosticStatus);

  const recordDiagnosticFrame = (
    response: LiveDetectionResponse,
    client: Record<string, unknown>,
  ): void => {
    if (!diagnosticRecording) return;
    const now = performance.now();
    if (diagnosticFrames.length >= MAX_DIAGNOSTIC_FRAMES) {
      diagnosticFrames.shift();
      diagnosticTruncated = true;
    }
    diagnosticFrames.push({
      captured_at: new Date().toISOString(),
      client_frame_index: diagnosticFrames.length,
      client: {
        ...client,
        interval_from_previous_ms: lastDiagnosticFrameAt === undefined ? null : now - lastDiagnosticFrameAt,
        video_current_time_s: video.currentTime,
        document_visibility: document.visibilityState,
      },
      server: {
        status: response.status,
        frame: response.frame,
        detection_count: response.detection_count,
        detector_detection_count: response.detector_detection_count,
        tracked_detection_count: response.tracked_detection_count,
        processing_ms: response.processing_ms,
        scanned_families: response.scanned_families,
        diagnostics: response.diagnostics,
      },
      detections: response.detections,
    });
    lastDiagnosticFrameAt = now;
    diagnosticSaveButton.disabled = false;
    updateDiagnosticStatus();
  };

  const recordDiagnosticError = (error: unknown, client: Record<string, unknown>): void => {
    if (!diagnosticRecording) return;
    diagnosticErrors.push({
      captured_at: new Date().toISOString(),
      error: String(error),
      client,
    });
    diagnosticSaveButton.disabled = false;
    updateDiagnosticStatus();
  };

  const refreshCameras = async (): Promise<void> => {
    if (!navigator.mediaDevices?.enumerateDevices) return;
    const selected = selectInput(cameraField).value;
    const devices = (await navigator.mediaDevices.enumerateDevices()).filter((device) => device.kind === "videoinput");
    const cameraSelect = selectInput(cameraField);
    cameraSelect.replaceChildren();
    const defaultOption = document.createElement("option");
    defaultOption.value = "";
    defaultOption.textContent = "既定カメラ";
    cameraSelect.appendChild(defaultOption);
    devices.forEach((device, index) => {
      const option = document.createElement("option");
      option.value = device.deviceId;
      option.textContent = device.label || `カメラ ${index + 1}`;
      option.selected = device.deviceId === selected;
      cameraSelect.appendChild(option);
    });
  };

  const clearCameraRecovery = (): void => {
    if (cameraRecoveryTimer !== undefined) window.clearTimeout(cameraRecoveryTimer);
    cameraRecoveryTimer = undefined;
  };

  const detachTrackEndedListener = (): void => {
    const track = stream?.getVideoTracks()[0];
    if (track && trackEndedListener) track.removeEventListener("ended", trackEndedListener);
    trackEndedListener = undefined;
  };

  const scheduleCameraRecovery = (): void => {
    if (manualCameraStop || disposed || cameraRecoveryTimer !== undefined || cameraRecoveryAttempt >= 3) return;
    const delay = Math.min(4_000, 750 * 2 ** cameraRecoveryAttempt);
    cameraRecoveryAttempt += 1;
    updateCameraState("warning", `カメラ再接続待ち ${cameraRecoveryAttempt}/3`);
    status.textContent = `USBカメラの再接続を待っています。${Math.round(delay)} ms後に自動復旧を試します。`;
    cameraRecoveryTimer = window.setTimeout(() => {
      cameraRecoveryTimer = undefined;
      void startCamera(true);
    }, delay);
  };

  const handleCameraEnded = (): void => {
    if (manualCameraStop || disposed) return;
    recordDynamicTrackingDiagnostic("apriltag_sender", "camera_track_ended", { recovery_attempt: cameraRecoveryAttempt });
    running = false;
    loopToken += 1;
    videoFrameClock.stop();
    stream = undefined;
    video.srcObject = null;
    overlay.getContext("2d")?.clearRect(0, 0, overlay.width, overlay.height);
    stageMessage.hidden = false;
    stageMessage.textContent = "カメラが切断されました。再接続を待っています。";
    startButton.disabled = false;
    stopButton.disabled = true;
    scheduleCameraRecovery();
  };

  const stopCamera = (): void => {
    manualCameraStop = true;
    clearCameraRecovery();
    detachTrackEndedListener();
    recordDynamicTrackingDiagnostic("apriltag_sender", "camera_stopped", {
      was_running: running,
    });
    running = false;
    videoFrameClock.stop();
    consecutiveDarkFrames = 0;
    recoveringExposure = false;
    stopDiagnostics();
    diagnosticStartButton.disabled = true;
    loopToken += 1;
    stream?.getTracks().forEach((track) => track.stop());
    stream = undefined;
    video.srcObject = null;
    overlay.getContext("2d")?.clearRect(0, 0, overlay.width, overlay.height);
    if (overlayAnimationFrame) cancelAnimationFrame(overlayAnimationFrame);
    overlayAnimationFrame = 0;
    stageMessage.hidden = false;
    stageMessage.textContent = "カメラ映像は停止しています。";
    startButton.disabled = false;
    stopButton.disabled = true;
    updateCameraState("stopped", "カメラ停止中");
    status.textContent = "停止しました。";
  };

  const cameraParameters = (width: number, height: number): { fx: number; fy: number; cx: number; cy: number; source: string; distortion_coefficients?: number[] } => {
    if (selectInput(intrinsicsMode).value === "calibration") {
      if (!loadedCalibration) throw new Error("カメラ校正JSONを読み込んでください。");
      return scaledCameraCalibration(loadedCalibration, width, height);
    }
    if (selectInput(intrinsicsMode).value === "manual") {
      const scaleX = width / Math.max(1, video.videoWidth);
      const scaleY = height / Math.max(1, video.videoHeight);
      return {
        fx: finiteNumber(numberInput(fxField), "fx", true) * scaleX,
        fy: finiteNumber(numberInput(fyField), "fy", true) * scaleY,
        cx: finiteNumber(numberInput(cxField), "cx") * scaleX,
        cy: finiteNumber(numberInput(cyField), "cy") * scaleY,
        source: `手入力校正値を${width}×${height}へ換算`,
      };
    }
    const fov = finiteNumber(numberInput(fovField), "水平画角", true);
    if (fov >= 180) throw new Error("水平画角は180度未満にしてください。");
    const focal = width / (2 * Math.tan(fov * Math.PI / 360));
    return { fx: focal, fy: focal, cx: width / 2, cy: height / 2, source: `水平画角 ${fov.toFixed(1)}°から概算` };
  };

  const updateTracking = (
    response: LiveDetectionResponse,
    measurementCapturedMs: number,
    measurementLatencyMs: number,
  ): number => {
    const now = performance.now();
    const boundedMeasurementLatencyMs = Math.max(
      0,
      Math.min(MAX_LATENCY_COMPENSATION_MS, measurementLatencyMs),
    );
    for (const item of seen.values()) item.current = false;
    for (const detection of response.detections) {
      const key = `${detection.family}:${detection.tag_id}`;
      const previous = seen.get(key);
      const continuous = Boolean(previous && now - previous.lastSeenMs <= OVERLAY_HOLD_MS);
      const elapsedMs = continuous
        ? Math.max(1, measurementCapturedMs - previous!.measurementCapturedMs)
        : 1;
      const velocities = detection.corners_px.map((point, index) => {
        if (!continuous) return [0, 0] as [number, number];
        const priorPoint = previous!.targetDetection.corners_px[index] ?? point;
        const priorVelocity = previous!.cornerVelocityPxMs[index] ?? [0, 0];
        return [
          boundedVelocity(priorVelocity[0], (point[0] - priorPoint[0]) / elapsedMs),
          boundedVelocity(priorVelocity[1], (point[1] - priorPoint[1]) / elapsedMs),
        ] as [number, number];
      });
      seen.set(key, {
        detection: continuous ? previous!.detection : detection,
        targetDetection: detection,
        cornerVelocityPxMs: velocities,
        measurementCapturedMs,
        measurementLatencyMs: boundedMeasurementLatencyMs,
        lastSeenMs: now,
        current: true,
      });
    }
    for (const [key, item] of seen) if (now - item.lastSeenMs > SIDEBAR_RETENTION_MS) seen.delete(key);
    return now;
  };

  const drawDetections = (frame: LiveDetectionResponse["frame"], now: number): void => {
    if (overlay.width !== frame.width) overlay.width = frame.width;
    if (overlay.height !== frame.height) overlay.height = frame.height;
    const context = overlay.getContext("2d");
    if (!context) return;
    context.clearRect(0, 0, overlay.width, overlay.height);
    context.lineWidth = Math.max(2, overlay.width / 480);
    context.font = `${Math.max(14, overlay.width / 50)}px sans-serif`;
    for (const item of seen.values()) {
      const ageMs = now - item.lastSeenMs;
      if (ageMs > OVERLAY_HOLD_MS) continue;
      const predicted = predictDetection(item, now);
      item.detection = smoothDetection(item.detection, predicted);
      const detection = item.detection;
      const imageTracking = detection.tracking_state === "optical_flow";
      const predicting = !item.current || ageMs > 120;
      const suffix = imageTracking ? " / 画像追跡" : predicting ? " / 予測追従" : "";
      const label = `${detection.family} / ID ${detection.tag_id}${suffix}`;
      context.globalAlpha = item.current ? 1 : Math.max(0.35, 1 - ageMs / OVERLAY_HOLD_MS);
      context.lineJoin = "round";
      context.lineCap = "round";
      context.setLineDash(item.current && !imageTracking ? [] : [12, 8]);
      context.strokeStyle = imageTracking ? "#4dd7ff" : "#39f59a";
      context.beginPath();
      detection.corners_px.forEach(([x, y], index) => index === 0 ? context.moveTo(x, y) : context.lineTo(x, y));
      context.closePath();
      context.stroke();
      context.setLineDash([]);
      const cornerRadius = Math.max(4, overlay.width / 180);
      detection.corners_px.forEach(([x, y], index) => {
        context.beginPath();
        context.arc(x, y, cornerRadius, 0, Math.PI * 2);
        context.fillStyle = index === 0 ? "#ffe066" : "#39f59a";
        context.fill();
        context.strokeStyle = "#07111d";
        context.lineWidth = Math.max(1.5, overlay.width / 720);
        context.stroke();
      });
      context.lineWidth = Math.max(2, overlay.width / 480);
      const textWidth = context.measureText(label).width;
      const labelY = Math.max(22, detection.bbox_px.y - 5);
      context.fillStyle = "rgba(7, 17, 29, 0.88)";
      context.fillRect(detection.bbox_px.x, labelY - 20, textWidth + 12, 24);
      context.fillStyle = "#ffffff";
      context.fillText(label, detection.bbox_px.x + 6, labelY);
    }
    context.globalAlpha = 1;
    context.setLineDash([]);
  };

  const animateOverlay = (): void => {
    if (!running || disposed) {
      overlayAnimationFrame = 0;
      return;
    }
    drawDetections(latestFrameSize, performance.now());
    overlayAnimationFrame = requestAnimationFrame(animateOverlay);
  };

  const renderSidebar = (response: LiveDetectionResponse, intrinsicsSource: string, now: number): void => {
    const imageTracked = response.tracked_detection_count
      ?? response.detections.filter((detection) => detection.tracking_state === "optical_flow").length;
    const decoded = response.detector_detection_count ?? Math.max(0, response.detection_count - imageTracked);
    const records = [...seen.values()].sort((left, right) =>
      left.detection.family.localeCompare(right.detection.family) || left.detection.tag_id - right.detection.tag_id
    );
    const currentCount = records.filter((item) => item.current).length;
    detectionCount.textContent = String(currentCount);
    summary.textContent = `ID認識 ${decoded}件 · 画像追跡 ${imageTracked}件 · 処理 ${response.processing_ms.toFixed(1)} ms`;
    runtimeInfo.textContent = `${cameraRuntime} · ${intrinsicsSource}`;
    tagList.replaceChildren();
    if (records.length === 0) {
      tagList.appendChild(empty);
      return;
    }
    for (const item of records) {
      const detection = item.targetDetection;
      const card = document.createElement("article");
      card.className = `apriltag-live-tag-card${item.current ? " is-current" : " is-recent"}`;
      const title = document.createElement("h3");
      title.textContent = `${detection.family} / Tag ID ${detection.tag_id}`;
      const state = document.createElement("span");
      state.textContent = item.current && detection.tracking_state === "optical_flow"
        ? "画像追跡"
        : item.current
          ? "追従中"
          : now - item.lastSeenMs <= OVERLAY_HOLD_MS
            ? "予測追従"
            : "直近";
      title.append(" ", state);
      const quality = document.createElement("p");
      quality.className = "apriltag-live-quality";
      const source = detection.tracking_state === "enhanced"
        ? "補正画像で認識"
        : detection.tracking_state === "optical_flow"
          ? `画像追跡 ${detection.tracking_age_frames ?? 1} frame`
          : "通常画像で認識";
      const trackingQuality = detection.tracking_state === "optical_flow"
        ? ` / feature ${detection.tracking_feature_count ?? "?"} / inlier ${detection.tracking_inlier_count ?? "?"} (${detection.tracking_inlier_ratio === undefined ? "?" : `${(detection.tracking_inlier_ratio * 100).toFixed(0)}%`}) / seed ${detection.tracking_seed_score === undefined || detection.tracking_seed_score === null ? "—" : detection.tracking_seed_score.toFixed(2)}`
        : "";
      quality.textContent = `${source}${trackingQuality} / margin ${detection.decision_margin.toFixed(2)} / hamming ${detection.hamming} / center ${formatVector(detection.center_px, 1, "px")}`;
      const pose = detection.pose_camera_tag;
      if (pose) {
        const poseGrid = document.createElement("div");
        poseGrid.className = "apriltag-live-pose-grid";
        const position = document.createElement("p");
        position.innerHTML = `<strong>位置 X / Y / Z (m)</strong><br>${formatVector(pose.translation_m, 4)}`;
        const rotation = document.createElement("p");
        rotation.innerHTML = `<strong>回転 Roll / Pitch / Yaw (deg)</strong><br>${formatVector(pose.rotation_rpy_deg, 2, "°")}`;
        const quaternion = document.createElement("p");
        quaternion.innerHTML = `<strong>Quaternion x / y / z / w</strong><br>${formatVector(pose.rotation_quaternion_xyzw, 5)}`;
        const error = document.createElement("p");
        error.className = "apriltag-live-pose-note";
        error.textContent = detection.tracking_state === "optical_flow"
          ? "pose: 画像移動からの短時間推定（ID再認識待ち）"
          : `pose error: ${pose.pose_error === null ? "—" : pose.pose_error.toExponential(3)}`;
        poseGrid.append(position, rotation, quaternion);
        card.append(title, poseGrid, quality, error);
      } else {
        const unavailable = document.createElement("p");
        unavailable.textContent = "6DoFを算出できませんでした。";
        card.append(title, unavailable, quality);
      }
      tagList.appendChild(card);
    }
  };

  const analyze = async (token: number): Promise<void> => {
    if (!running || disposed || token !== loopToken) return;
    if (video.readyState < HTMLMediaElement.HAVE_CURRENT_DATA || video.videoWidth <= 0 || video.videoHeight <= 0) {
      window.setTimeout(() => void analyze(token), 50);
      return;
    }
    const cycleStarted = performance.now();
    const frameTimestamp = videoFrameClock.timestampForCycle(cycleStarted);
    const sourceFrameEpochMs = frameTimestamp.epochMs;
    const clientDiagnostic: Record<string, unknown> = {};
    clientDiagnostic.source_timestamp = { ...frameTimestamp };
    try {
      const processingWidthValue = selectInput(processingResolutionField).value;
      const requestedWidth = processingWidthValue === "native" ? video.videoWidth : Number(processingWidthValue);
      capture.width = Math.max(1, Math.min(video.videoWidth, requestedWidth));
      capture.height = Math.max(1, Math.round(video.videoHeight * capture.width / video.videoWidth));
      const context = capture.getContext("2d", { alpha: false });
      if (!context) throw new Error("camera_canvas_unavailable");
      const copyStarted = performance.now();
      context.drawImage(video, 0, 0, capture.width, capture.height);
      clientDiagnostic.canvas_copy_ms = performance.now() - copyStarted;
      clientDiagnostic.frame_width = capture.width;
      clientDiagnostic.frame_height = capture.height;
      const meanLuminance = meanCanvasLuminance(capture, luminanceProbe);
      clientDiagnostic.mean_luminance = meanLuminance;
      const videoTrack = stream?.getVideoTracks()[0];
      const exposureSettings = videoTrack?.getSettings() as (MediaTrackSettings & {
        exposureMode?: string;
      }) | undefined;
      if (meanLuminance < DARK_FRAME_LUMA_THRESHOLD && exposureSettings?.exposureMode === "manual") {
        consecutiveDarkFrames += 1;
      } else {
        consecutiveDarkFrames = 0;
      }
      if (
        consecutiveDarkFrames >= DARK_FRAME_RECOVERY_COUNT
        && videoTrack
        && !recoveringExposure
      ) {
        recoveringExposure = true;
        selectInput(exposureProfileField).value = "auto";
        updateCameraState("warning", "露光を自動復旧中");
        status.textContent = "黒いカメラ映像を検出したため、自動露光へ復帰しています…";
        await tuneCameraForMotion(videoTrack);
        cameraRuntime = describeCameraRuntime(videoTrack);
        consecutiveDarkFrames = 0;
        recoveringExposure = false;
        updateCameraState("running", "カメラ稼働中");
        return;
      }
      const intrinsics = cameraParameters(capture.width, capture.height);
      clientDiagnostic.intrinsics = intrinsics;
      const encodeStarted = performance.now();
      clientDiagnostic.jpeg_encoder = document.visibilityState === "hidden"
        ? "sync_data_url_background"
        : "async_to_blob_foreground";
      const frame = await canvasBlob(capture);
      clientDiagnostic.jpeg_encode_ms = performance.now() - encodeStarted;
      clientDiagnostic.jpeg_bytes = frame.size;
      const requestStarted = performance.now();
      const response = await detectAprilTagFrame<LiveDetectionResponse>(frame, {
        family: selectInput(familyField).value,
        tag_size_m: finiteNumber(numberInput(sizeField), "AprilTag size", true),
        fx: intrinsics.fx,
        fy: intrinsics.fy,
        cx: intrinsics.cx,
        cy: intrinsics.cy,
        distortion_coefficients: intrinsics.distortion_coefficients,
      });
      const responseReceivedMs = performance.now();
      const measurementLatencyMs = responseReceivedMs - cycleStarted;
      clientDiagnostic.request_round_trip_ms = responseReceivedMs - requestStarted;
      clientDiagnostic.cycle_total_ms = measurementLatencyMs;
      clientDiagnostic.display_latency_compensation_ms = Math.min(
        MAX_LATENCY_COMPENSATION_MS,
        measurementLatencyMs,
      );
      recordDynamicTrackingDiagnostic("apriltag_sender", "detection_cycle", {
        source_frame_epoch_ms: sourceFrameEpochMs,
        cycle_total_ms: measurementLatencyMs,
        request_round_trip_ms: clientDiagnostic.request_round_trip_ms,
        server_processing_ms: response.processing_ms,
        frame_width: response.frame.width,
        frame_height: response.frame.height,
        detector_detection_count: response.detector_detection_count ?? response.detection_count,
        tracked_detection_count: response.tracked_detection_count ?? 0,
        detections: response.detections.map((item) => ({
          family: item.family,
          tag_id: item.tag_id,
          tracking_state: item.tracking_state ?? "detected",
          decision_margin: item.decision_margin,
          hamming: item.hamming,
          has_pose: item.pose_camera_tag !== null,
        })),
      });
      recordDiagnosticFrame(response, clientDiagnostic);
      if (!running || token !== loopToken) return;
      const now = updateTracking(response, cycleStarted, measurementLatencyMs);
      latestFrameSize = response.frame;
      drawDetections(response.frame, now);
      renderSidebar(response, intrinsics.source, now);
      publishBridgeFrame(response, sourceFrameEpochMs);
      const imageTracked = response.tracked_detection_count ?? 0;
      const decoded = response.detector_detection_count ?? response.detection_count;
      status.textContent = imageTracked > 0
        ? `${response.scanned_families[0]}: ID認識 ${decoded}件 / 画像追跡 ${imageTracked}件。再認識を継続しています。`
        : `${response.scanned_families[0]}を連続追従中。Tag IDは自動判定です。`;
    } catch (error) {
      clientDiagnostic.cycle_total_ms = performance.now() - cycleStarted;
      recordDynamicTrackingDiagnostic("apriltag_sender", "detection_cycle_error", {
        source_frame_epoch_ms: sourceFrameEpochMs,
        cycle_total_ms: clientDiagnostic.cycle_total_ms,
        reason: String(error),
      });
      recordDiagnosticError(error, clientDiagnostic);
      status.textContent = `認識処理エラー: ${String(error)}`;
    } finally {
      if (running && !disposed && token === loopToken) {
        // A completed fetch already yields to the browser between cycles. Use
        // a microtask here so background timer clamping cannot insert another
        // one-second gap after the frame has been encoded and processed.
        queueMicrotask(() => void analyze(token));
      }
    }
  };

  const tuneCameraForMotion = async (track: MediaStreamTrack): Promise<void> => {
    if (typeof track.getCapabilities !== "function") return;
    const capabilities = track.getCapabilities() as MediaTrackCapabilities & {
      focusMode?: string[];
      exposureMode?: string[];
      exposureTime?: { min: number; max: number; step?: number };
      whiteBalanceMode?: string[];
    };
    const baseConstraints: Record<string, string | number> = {};
    if (capabilities.focusMode?.includes("continuous")) baseConstraints.focusMode = "continuous";
    if (capabilities.whiteBalanceMode?.includes("continuous")) baseConstraints.whiteBalanceMode = "continuous";
    const applyAdvanced = async (constraints: Record<string, string | number>): Promise<boolean> => {
      if (Object.keys(constraints).length === 0) return true;
      try {
        await track.applyConstraints({
          advanced: [constraints as unknown as MediaTrackConstraintSet],
        });
        return true;
      } catch {
        // Windows camera drivers may advertise controls they cannot apply.
        return false;
      }
    };

    const supportsContinuousExposure = capabilities.exposureMode?.includes("continuous") ?? false;
    const motionProfile = selectInput(exposureProfileField).value === "motion";
    if (!motionProfile) {
      await applyAdvanced({
        ...baseConstraints,
        ...(supportsContinuousExposure ? { exposureMode: "continuous" } : {}),
      });
      return;
    }

    // Always reset to auto before sampling exposure. Camera drivers often
    // preserve the previous manual value across stream restarts; halving that
    // persisted value on every start eventually produces an almost black
    // image. Auto reset makes each start independent of the previous one.
    if (supportsContinuousExposure) {
      await applyAdvanced({ ...baseConstraints, exposureMode: "continuous" });
      await new Promise<void>((resolve) => window.setTimeout(resolve, AUTO_EXPOSURE_SETTLE_MS));
    }

    const exposureRange = capabilities.exposureTime;
    if (!capabilities.exposureMode?.includes("manual") || !exposureRange) {
      await applyAdvanced(baseConstraints);
      return;
    }
    const minimumExposure = Number(exposureRange.min);
    const maximumExposure = Number(exposureRange.max);
    if (
      !Number.isFinite(minimumExposure)
      || !Number.isFinite(maximumExposure)
      || minimumExposure <= 0
      || maximumExposure < minimumExposure
    ) {
      await applyAdvanced({
        ...baseConstraints,
        ...(supportsContinuousExposure ? { exposureMode: "continuous" } : {}),
      });
      return;
    }
    const sampledSettings = track.getSettings() as MediaTrackSettings & {
      exposureMode?: string;
      exposureTime?: number;
    };
    const sampledExposure = Number(sampledSettings.exposureTime);
    const sampledFromAuto = sampledSettings.exposureMode === "continuous"
      && Number.isFinite(sampledExposure)
      && sampledExposure >= minimumExposure
      && sampledExposure <= maximumExposure;
    const requestedExposure = sampledFromAuto
      ? sampledExposure / 2
      : Math.sqrt(minimumExposure * maximumExposure);
    const safeRequestedExposure = Math.max(MOTION_EXPOSURE_SAFE_FLOOR, requestedExposure);
    const step = Number(exposureRange.step);
    const snappedExposure = Number.isFinite(step) && step > 0
      ? minimumExposure + Math.round((safeRequestedExposure - minimumExposure) / step) * step
      : safeRequestedExposure;
    const targetExposure = Math.max(minimumExposure, Math.min(maximumExposure, snappedExposure));
    const applied = await applyAdvanced({
      ...baseConstraints,
      exposureMode: "manual",
      exposureTime: targetExposure,
    });
    if (!applied && supportsContinuousExposure) {
      await applyAdvanced({ ...baseConstraints, exposureMode: "continuous" });
    }
  };

  const describeCameraRuntime = (track: MediaStreamTrack | undefined): string => {
    const settings = track?.getSettings();
    return settings
      ? `${settings.width ?? video.videoWidth}×${settings.height ?? video.videoHeight} / ${settings.frameRate?.toFixed(1) ?? "?"} fps${"exposureMode" in settings ? ` / 露光 ${String(settings.exposureMode)} ${String((settings as MediaTrackSettings & { exposureTime?: number }).exposureTime ?? "?")}` : ""}`
      : `${video.videoWidth}×${video.videoHeight}`;
  };

  const startCamera = async (automaticRecovery = false): Promise<void> => {
    if (!navigator.mediaDevices?.getUserMedia) {
      status.textContent = "このブラウザはカメラ入力に対応していません。localhostまたはHTTPSで開いてください。";
      return;
    }
    if (!automaticRecovery) cameraRecoveryAttempt = 0;
    manualCameraStop = false;
    clearCameraRecovery();
    startButton.disabled = true;
    updateCameraState("starting", "カメラを起動中");
    status.textContent = "カメラへのアクセス許可を待っています…";
    try {
      const deviceId = selectInput(cameraField).value;
      detachTrackEndedListener();
      stream?.getTracks().forEach((track) => track.stop());
      stream = await navigator.mediaDevices.getUserMedia({
        audio: false,
        video: deviceId
          ? { deviceId: { exact: deviceId }, width: { ideal: 1280 }, height: { ideal: 720 }, frameRate: { ideal: 60 } }
          : { facingMode: { ideal: "environment" }, width: { ideal: 1280 }, height: { ideal: 720 }, frameRate: { ideal: 60 } },
      });
      const videoTrack = stream.getVideoTracks()[0];
      if (!videoTrack) throw new Error("カメラ映像trackがありません。");
      const actualDeviceIdentifier = videoTrack.getSettings().deviceId;
      const expectedDeviceIdentifier = loadedCalibration?.camera.device_identifier;
      const calibrationDeviceMismatch = Boolean(
        expectedDeviceIdentifier
        && actualDeviceIdentifier
        && expectedDeviceIdentifier !== actualDeviceIdentifier,
      );
      trackEndedListener = handleCameraEnded;
      videoTrack.addEventListener("ended", trackEndedListener, { once: true });
      video.srcObject = stream;
      await video.play();
      videoFrameClock.start();
      if (videoTrack) await tuneCameraForMotion(videoTrack);
      cameraRuntime = describeCameraRuntime(videoTrack);
      cameraRecoveryAttempt = 0;
      consecutiveDarkFrames = 0;
      await refreshCameras();
      running = true;
      loopToken += 1;
      stageMessage.hidden = true;
      stopButton.disabled = false;
      diagnosticStartButton.disabled = false;
      updateCameraState("running", "カメラ稼働中");
      status.textContent = calibrationDeviceMismatch
        ? `カメラを開始しました（${cameraRuntime}）。校正ファイルの機器識別子と接続カメラが一致しないため、校正精度を確認してください。`
        : `カメラを開始しました（${cameraRuntime}）。認識処理を準備しています…`;
      recordDynamicTrackingDiagnostic("apriltag_sender", "camera_started", {
        runtime: cameraRuntime,
        selected_family: selectInput(familyField).value,
        processing_width: selectInput(processingResolutionField).value,
        calibration_loaded: Boolean(loadedCalibration),
        calibration_device_match: expectedDeviceIdentifier && actualDeviceIdentifier
          ? !calibrationDeviceMismatch
          : undefined,
      });
      if (!overlayAnimationFrame) overlayAnimationFrame = requestAnimationFrame(animateOverlay);
      void analyze(loopToken);
    } catch (error) {
      detachTrackEndedListener();
      stream?.getTracks().forEach((track) => track.stop());
      stream = undefined;
      startButton.disabled = false;
      stopButton.disabled = true;
      diagnosticStartButton.disabled = true;
      updateCameraState("stopped", "カメラ開始エラー");
      status.textContent = `カメラを開始できません: ${String(error)}`;
      recordDynamicTrackingDiagnostic("apriltag_sender", "camera_start_failed", {
        reason: String(error),
        automatic_recovery: automaticRecovery,
      });
      if (automaticRecovery) scheduleCameraRecovery();
    }
  };

  const startButton = button("カメラを開始", () => void startCamera());
  startButton.classList.add("preview-primary");
  const stopButton = button("カメラを停止", stopCamera, true);
  stopButton.classList.add("preview-danger-subtle");
  const refreshButton = button("カメラを再検出", () => void refreshCameras());
  refreshButton.classList.add("preview-tertiary");
  actionRow.append(cameraState, startButton, stopButton, refreshButton, status);
  selectInput(exposureProfileField).addEventListener("change", () => {
    const track = stream?.getVideoTracks()[0];
    if (!running || !track) return;
    status.textContent = "カメラ露光を再調整しています…";
    updateCameraState("starting", "露光を調整中");
    void tuneCameraForMotion(track).then(() => {
      cameraRuntime = describeCameraRuntime(track);
      updateCameraState("running", "カメラ稼働中");
      status.textContent = `カメラ露光を更新しました（${cameraRuntime}）。`;
    });
  });
  void refreshCameras().catch((error) => { status.textContent = `カメラ一覧を取得できません: ${String(error)}`; });
  const handleDeviceChange = (): void => {
    void refreshCameras().finally(() => {
      recordDynamicTrackingDiagnostic("apriltag_sender", "camera_devicechange", { running });
      if (!running && !manualCameraStop) {
        clearCameraRecovery();
        scheduleCameraRecovery();
      }
    });
  };
  navigator.mediaDevices?.addEventListener?.("devicechange", handleDeviceChange);

  const query = new URLSearchParams(window.location.search);
  if (options.integratedViewerMode && query.get("auto_start") === "1" && restoredSession) {
    query.delete("auto_start");
    const replacement = `${window.location.pathname}?${query.toString()}${window.location.hash}`;
    window.history.replaceState(window.history.state, "", replacement);
    window.sessionStorage.removeItem(INTEGRATED_VIEWER_SESSION_KEY);
    window.setTimeout(() => {
      connectBridge();
      void startCamera().then(() => {
        if (running) options.onShowViewer?.();
      });
    }, 0);
  }

  const dispose = (): void => {
    disposed = true;
    manualCameraStop = true;
    clearCameraRecovery();
    navigator.mediaDevices?.removeEventListener?.("devicechange", handleDeviceChange);
    detachTrackEndedListener();
    running = false;
    videoFrameClock.stop();
    loopToken += 1;
    if (overlayAnimationFrame) cancelAnimationFrame(overlayAnimationFrame);
    overlayAnimationFrame = 0;
    stream?.getTracks().forEach((track) => track.stop());
    stream = undefined;
    video.srcObject = null;
    bridgePublisher?.disconnect();
    bridgePublisher = undefined;
    if (bridgeProfileStorageListener) window.removeEventListener("storage", bridgeProfileStorageListener);
    bridgeProfileStorageListener = undefined;
    host.classList.remove("apriltag-live-page");
  };
  window.addEventListener("beforeunload", dispose, { once: true });
  return dispose;
}
