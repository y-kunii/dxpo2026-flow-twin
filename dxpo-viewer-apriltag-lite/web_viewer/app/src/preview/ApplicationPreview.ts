import {
  api,
  bundledScenePackages,
  publishedScenePackages,
  uploadSelectedVideo,
  type JsonObject,
} from "./PreviewApiClient";
import {
  button,
  byteSize,
  checkboxField,
  field,
  section,
  selectField,
  statusBadge,
  stringValue,
} from "./PreviewUi";
import { mountAprilTagLiveCheck } from "./AprilTagLiveCheck";
import { mountPackageBuilderView } from "./PackageBuilderView";
import { mountFieldMemoryView } from "./FieldMemoryView";
import { mountFrameAnalysisView } from "./FrameAnalysisView";
import { mountSiteInsightsView } from "./SiteInsightsView";
import { mountRepositoryDataManager } from "./RepositoryDataManagerView";
import { mountSpzImportView } from "./SpzImportView";
import { mountProjectWorkspaceView } from "./ProjectWorkspaceView";
import {
  PREVIEW_MODE_APP_IDS,
  SPATIAL_APP_CATEGORIES,
  SPATIAL_APP_DEFINITIONS,
  SPATIAL_APP_ORDER,
  type PreviewMode,
} from "../apps/AppPackageContract";

export interface ApplicationPreviewOptions {
  fixedMode?: PreviewMode;
  allowedModes?: readonly PreviewMode[];
  applicationTitle?: string;
  standalone?: boolean;
}
// New App A projects use a neutral fixed-rig template.  Historic packages
// retain their original scene IDs and are selected by their MRP instead.
const DEFAULT_SCENE_TEMPLATE_ID = "app_a_fixed_rig";

export function mountApplicationPreview(host: HTMLElement, options: ApplicationPreviewOptions = {}): void {
  const query = new URLSearchParams(window.location.search);
  const requestedInitialValue = query.get("preview");
  const requestedInitial = requestedInitialValue === "spatial-search"
    || requestedInitialValue === "evidence-ai"
    || requestedInitialValue === "change-review"
    ? "site-insights"
    : requestedInitialValue;
  const initial = options.fixedMode ?? requestedInitial;
  if (!options.fixedMode && requestedInitialValue !== requestedInitial && requestedInitial === "site-insights") {
    const migrated = new URL(window.location.href);
    migrated.searchParams.set("preview", requestedInitial);
    window.history.replaceState({ spatialPreviewMode: requestedInitial }, "", `${migrated.pathname}${migrated.search}${migrated.hash}`);
  }
  let packageViewIntent = !options.fixedMode && initial === "packages" && query.get("intent") === "viewer";
  const allowedModes = new Set<PreviewMode>(options.allowedModes ?? ["pipeline", "tag-check", "packages", "spatial", "projects", "field-memory", "frame-analysis", "site-insights", "package-builder"]);
  const integratedViewerMode = initial === "tag-check" && query.get("live_viewer") === "1" &&
    /^[A-Za-z0-9][A-Za-z0-9_.-]*$/.test(query.get("scene_package") ?? "");
  const shell = document.createElement("div");
  shell.className = "preview-shell";
  shell.hidden = initial !== "pipeline" && initial !== "tag-check" && initial !== "packages" && initial !== "spatial" && initial !== "projects" && initial !== "field-memory" && initial !== "frame-analysis" && initial !== "site-insights" && initial !== "package-builder";
  host.appendChild(shell);

  const nav = document.createElement("nav");
  nav.className = "preview-nav";
  const title = document.createElement("strong");
  title.textContent = options.applicationTitle ?? "Spatial 3DGS";
  nav.appendChild(title);
  if (__SPATIAL_APP_HOME_URL__) {
    const home = document.createElement("a");
    home.className = "preview-home-link";
    home.href = __SPATIAL_APP_HOME_URL__;
    home.textContent = "アプリ選択TOP";
    nav.appendChild(home);
  }
  shell.appendChild(nav);

  const content = document.createElement("main");
  content.className = "preview-content";
  shell.appendChild(content);

  const viewerLink = button("3Dシーンを開く", () => {
    void openBundledViewerOrPackageList();
  });
  viewerLink.className = "preview-secondary";
  if (!options.standalone && allowedModes.has("packages")) nav.appendChild(viewerLink);

  const appSwitcher = document.createElement("label");
  appSwitcher.className = "preview-app-switcher";
  const appSwitcherLabel = document.createElement("span");
  appSwitcherLabel.textContent = "アプリを切り替える";
  const appSelect = document.createElement("select");
  appSelect.className = "preview-app-select";
  appSelect.setAttribute("aria-label", "アプリを切り替える");
  for (const category of SPATIAL_APP_CATEGORIES) {
    const group = document.createElement("optgroup");
    group.label = category.label;
    for (const id of SPATIAL_APP_ORDER) {
      const app = SPATIAL_APP_DEFINITIONS[id];
      const mode = app.previewMode;
      if (!mode || !allowedModes.has(mode)) continue;
      if (id !== "viewer" && PREVIEW_MODE_APP_IDS[mode] !== id) continue;
      if (app.category !== category.id) continue;
      const option = document.createElement("option");
      option.value = id === "viewer" ? "packages-viewer" : mode;
      option.textContent = app.label;
      group.appendChild(option);
    }
    if (group.childElementCount) appSelect.appendChild(group);
  }
  appSwitcher.append(appSwitcherLabel, appSelect);
  if (!options.standalone) nav.insertBefore(appSwitcher, viewerLink);

  let activeMode: PreviewMode = initial === "pipeline" || initial === "tag-check" || initial === "spatial" || initial === "packages" || initial === "projects" || initial === "field-memory" || initial === "frame-analysis" || initial === "site-insights" || initial === "package-builder" ? initial : "packages";
  if (!allowedModes.has(activeMode)) activeMode = [...allowedModes][0] ?? "packages";
  let activeViewCleanup: (() => void) | undefined;
  let sceneManagementAvailable: boolean | undefined;
  let renderSerial = 0;

  const updateIntegratedUrl = (mode: PreviewMode, viewerIntent: boolean): void => {
    if (options.fixedMode || options.standalone) return;
    const next = new URL(window.location.pathname, window.location.origin);
    next.searchParams.set("preview", mode);
    if (mode === "packages" && viewerIntent) next.searchParams.set("intent", "viewer");
    const target = `${next.pathname}${next.search}`;
    const current = `${window.location.pathname}${window.location.search}`;
    if (target === current) return;
    window.history.pushState({ spatialPreviewMode: mode, viewerIntent }, "", target);
  };

  const returnToAprilTagButton = button("AprilTagカメラ確認へ戻る", () => {
    shell.hidden = false;
    returnToAprilTagButton.hidden = true;
  });
  returnToAprilTagButton.className = "preview-launch preview-apriltag-return";
  returnToAprilTagButton.hidden = true;
  if (integratedViewerMode) document.body.appendChild(returnToAprilTagButton);

  const showIntegratedViewer = (): void => {
    if (!integratedViewerMode) return;
    shell.hidden = true;
    returnToAprilTagButton.hidden = false;
  };

  function setMode(mode: PreviewMode, updateUrl = true): void {
    if (!allowedModes.has(mode)) return;
    packageViewIntent = false;
    activeMode = mode;
    shell.hidden = false;
    appSelect.value = mode;
    if (updateUrl) updateIntegratedUrl(mode, false);
    void render();
  }

  const showPackageViewerList = (updateUrl = true): void => {
    if (!allowedModes.has("packages")) return;
    packageViewIntent = true;
    activeMode = "packages";
    shell.hidden = false;
    appSelect.value = "packages-viewer";
    if (updateUrl) updateIntegratedUrl("packages", true);
    void render();
  };

  const restoreModeFromUrl = (): void => {
    if (options.fixedMode || options.standalone) return;
    const restored = new URLSearchParams(window.location.search);
    const requested = restored.get("preview") as PreviewMode | null;
    if (!requested || !allowedModes.has(requested)) {
      window.location.reload();
      return;
    }
    if (requested === "packages" && restored.get("intent") === "viewer") {
      showPackageViewerList(false);
    } else {
      setMode(requested, false);
    }
  };
  window.addEventListener("popstate", restoreModeFromUrl);

  appSelect.addEventListener("change", () => {
    if (appSelect.value === "packages-viewer") showPackageViewerList();
    else setMode(appSelect.value as PreviewMode);
  });

  async function openBundledViewerOrPackageList(): Promise<void> {
    try {
      const { packages, source } = await publishedScenePackages();
      const openable = packages.filter(
        (item) => item.managed === true
          && item.status === "verified"
          && item.published === true
          && typeof item.viewer_url === "string",
      );
      if (source === "bundle" && openable.length === 1) {
        const url = new URL(String(openable[0].viewer_url), window.location.href);
        url.searchParams.set("viewer", __SPATIAL_VIEWER_VARIANT__);
        window.location.href = `${url.pathname}${url.search}${url.hash}`;
        return;
      }
    } catch {
      // The package list renders its own actionable error state.
    }
    showPackageViewerList();
  }

  appSelect.value = packageViewIntent && activeMode === "packages" ? "packages-viewer" : activeMode;

  if (integratedViewerMode) {
    const showViewerButton = button("Viewerを表示", showIntegratedViewer);
    showViewerButton.className = "preview-secondary preview-viewer-toggle";
    nav.insertBefore(showViewerButton, viewerLink);
  }

  async function renderPipeline(): Promise<() => void> {
    content.replaceChildren();
    const heading = document.createElement("div");
    heading.className = "preview-heading";
    heading.innerHTML = "<h1>3D再構成（COLMAP）</h1><p>動画から人物を除外した登録画像とCOLMAPモデルを作成します。AprilTagの実寸scaleが検証できた処理はMRPまで出力します。</p>";
    content.appendChild(heading);

    const overview = section("新規COLMAPプロジェクト");
    const [sceneConfigs, tagFamilyContract] = await Promise.all([
      api<JsonObject[]>("/api/scene-configs"),
      api<{ default_family: string; families: Array<{ value: string; label: string }> }>("/api/apriltag-families"),
    ]);
    const availableScenes = sceneConfigs
      .map((item) => String(item.scene_id ?? ""))
      .filter(Boolean);
    const sceneTemplateId = availableScenes.includes(DEFAULT_SCENE_TEMPLATE_ID)
      ? DEFAULT_SCENE_TEMPLATE_ID
      : (availableScenes[0] ?? DEFAULT_SCENE_TEMPLATE_ID);
    const sceneConfig = await api<{ config: JsonObject }>(`/api/scene-configs/${encodeURIComponent(sceneTemplateId)}`);
    const config = sceneConfig.config;
    const form = document.createElement("div");
    form.className = "preview-form";
    const marker = (config.metric_marker as JsonObject | undefined) ?? {};
    const personMask = (config.person_mask as JsonObject | undefined) ?? {};
    const sfm = (config.sfm as JsonObject | undefined) ?? {};
    const virtualRig = (config.virtual_rig as JsonObject | undefined) ?? {};
    const configuredProjection = String(config.input_projection ?? "equirectangular_360");
    const inputProjection = selectField(
      "入力動画の方式",
      configuredProjection === "perspective" ? "perspective" : "equirectangular_360",
      "input_projection",
      [
        { value: "equirectangular_360", label: "360度正距円筒（2:1）" },
        { value: "perspective", label: "通常カメラ（スマホ・ミラーレス）" },
      ],
    );
    const videoField = field("入力動画 path (absolute or repo-relative)", "", "text", "video");
    const videoInput = videoField.querySelector<HTMLInputElement>("input");
    if (videoInput) videoInput.placeholder = "エクスプローラーで動画を選択";
    const videoPicker = document.createElement("div");
    videoPicker.className = "preview-video-picker";
    const pickerStatus = document.createElement("span");
    pickerStatus.className = "preview-help";
    pickerStatus.textContent = "エクスプローラーから選んだ動画は、このPCのジョブ入力領域へコピーされます。";
    const videoFile = document.createElement("input");
    videoFile.type = "file";
    videoFile.accept = "video/mp4,video/quicktime,video/x-matroska,.mp4,.mov,.mkv";
    videoFile.hidden = true;
    videoFile.setAttribute("aria-label", "入力動画を選択");
    const pickerButton = button("エクスプローラーで選択", () => videoFile.click());
    pickerButton.className = "preview-secondary";
    videoFile.addEventListener("change", () => {
      const selected = videoFile.files?.[0];
      if (!selected) return;
      pickerButton.disabled = true;
      pickerStatus.textContent = `ローカル入力領域へコピー中: ${selected.name} (0%)`;
      void uploadSelectedVideo(selected, (percent) => {
        pickerStatus.textContent = `ローカル入力領域へコピー中: ${selected.name} (${percent}%)`;
      }).then((result) => {
        const input = videoField.querySelector<HTMLInputElement>("input");
        if (input) input.value = String(result.input_video ?? "");
        const size = typeof result.size_bytes === "number" ? byteSize(result.size_bytes) : "サイズ不明";
        pickerStatus.textContent = `選択済み: ${selected.name} (${size})`;
      }).catch((error: unknown) => {
        pickerStatus.textContent = `動画を取り込めませんでした: ${String(error)}`;
      }).finally(() => {
        pickerButton.disabled = false;
        videoFile.value = "";
      });
    });
    videoPicker.append(videoField, pickerButton, videoFile, pickerStatus);
    const preprocessing = (config.preprocessing as JsonObject | undefined) ?? {};
    const frameInterval = field("動画フレーム切り出し間隔（秒）", String(preprocessing.frame_interval_sec ?? "1.0"), "number", "frame_interval_sec");
    const frameIntervalInput = frameInterval.querySelector<HTMLInputElement>("input");
    if (frameIntervalInput) {
      frameIntervalInput.min = "0.05";
      frameIntervalInput.step = "0.05";
    }
    const defaultRigConfig = String(virtualRig.config ?? "");
    const virtualRigLayout = selectField(
      "仮想カメラ構成",
      defaultRigConfig.endsWith("virtual_rig.cube6.json") ? "cube_6" : "coverage_12",
      "virtual_rig_layout",
      [
        { value: "coverage_12", label: "12面（3段 × 4方向・従来設定）" },
        { value: "cube_6", label: "6面キューブ（前後左右上下）" },
      ],
    );
    const virtualRigNote = document.createElement("p");
    virtualRigNote.className = "preview-help preview-form-note";
    virtualRigNote.textContent = "6面キューブは90°の前後左右上下を各1枚ずつ生成します。生成画像数は12面の半分になります。選択は新規プロジェクトだけに保存され、既存プロジェクトやResumeの構成は変更しません。";
    const tagFamilyOptions = tagFamilyContract.families.filter((item) =>
      typeof item.value === "string" && item.value.length > 0 && typeof item.label === "string"
    );
    const configuredTagFamily = String(marker.family ?? tagFamilyContract.default_family ?? "tag25h9");
    const defaultTagFamily = tagFamilyOptions.some((item) => item.value === configuredTagFamily)
      ? configuredTagFamily
      : tagFamilyContract.default_family;
    const tagFamily = selectField("固定AprilTag family", defaultTagFamily, "tag_family", tagFamilyOptions);
    const configuredMarkerIds = Array.isArray(marker.markers)
      ? marker.markers.map((item) => item && typeof item === "object" && "id" in item ? String((item as JsonObject).id ?? "") : "").filter(Boolean)
      : [String(marker.id ?? "0")];
    const tagIds = field("固定AprilTag ID（0〜9、複数はカンマ区切り）", configuredMarkerIds.join(", "), "text", "tag_ids");
    const tagSize = field("AprilTag size (m)", String(marker.size_m ?? "0.082"), "number", "tag_size_m");
    const tagSizeInput = tagSize.querySelector<HTMLInputElement>("input");
    if (tagSizeInput) {
      tagSizeInput.min = "0.001";
      tagSizeInput.step = "0.001";
    }
    const apriltagEnabled = checkboxField("AprilTagを使う（実寸scale・MRPを生成）", marker.enabled !== false, "apriltag_enabled");
    const apriltagModeNote = document.createElement("div");
    apriltagModeNote.className = "preview-apriltag-mode";
    apriltagModeNote.setAttribute("role", "status");
    apriltagModeNote.setAttribute("aria-live", "polite");
    const horizontalization = checkboxField("カメラ姿勢から水平化を適用（常に有効）", true, "horizontalization");
    const horizontalizationInput = horizontalization.querySelector<HTMLInputElement>("input");
    if (horizontalizationInput) horizontalizationInput.disabled = true;
    const projectField = field("プロジェクト名（Scene ID・必須）", "", "text", "project");
    const projectInput = projectField.querySelector<HTMLInputElement>("input");
    if (projectInput) {
      projectInput.placeholder = "例: 20260727_jasa_room";
      projectInput.autocomplete = "off";
      projectInput.pattern = "[A-Za-z0-9][A-Za-z0-9_-]{0,63}";
      projectInput.maxLength = 64;
      projectInput.title = "半角英数字で始め、半角英数字・_・-だけを使用してください（64文字以内）。";
      const validateProjectName = (): void => {
        const value = projectInput.value.trim();
        projectInput.setCustomValidity(
          value.length === 0 || /^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/.test(value)
            ? ""
            : "プロジェクト名は半角英数字で始め、半角英数字・_・-だけを使用してください（64文字以内）。",
        );
      };
      projectInput.addEventListener("input", validateProjectName);
      validateProjectName();
    }
    const colmapProfile = field("COLMAP profile", String(sfm.profile ?? "fixed_rig"), "text", "colmap_profile");
    const colmapProfileInput = colmapProfile.querySelector<HTMLInputElement>("input");
    if (colmapProfileInput) colmapProfileInput.readOnly = true;
    const personMaskProfile = field("person mask profile", String(personMask.config ?? ""), "text", "person_mask_profile");
    const personMaskProfileInput = personMaskProfile.querySelector<HTMLInputElement>("input");
    if (personMaskProfileInput) personMaskProfileInput.readOnly = true;
    const projectionModeNote = document.createElement("div");
    projectionModeNote.className = "preview-apriltag-mode";
    const updateProjectionMode = (): void => {
      const projection = inputProjection.querySelector<HTMLSelectElement>("select")?.value ?? "equirectangular_360";
      const perspective = projection === "perspective";
      const rigSelect = virtualRigLayout.querySelector<HTMLSelectElement>("select");
      if (rigSelect) rigSelect.disabled = perspective;
      virtualRigLayout.classList.toggle("is-disabled", perspective);
      if (colmapProfileInput) colmapProfileInput.value = perspective ? "perspective" : "fixed_rig";
      projectionModeNote.innerHTML = perspective
        ? "<strong>通常カメラモード</strong><span>フレームを直接抽出し、単一カメラのSIMPLE_RADIALモデルでCOLMAP処理します。6面・12面への仮想展開は行いません。</span>"
        : "<strong>360度モード</strong><span>2:1の正距円筒動画を6面または12面へ展開し、固定rigとしてCOLMAP処理します。</span>";
      virtualRigNote.textContent = perspective
        ? "仮想カメラ構成は通常カメラモードでは使用しません。撮影中にレンズやデジタルズームを切り替えないでください。"
        : "6面キューブは90°の前後左右上下を各1枚ずつ生成します。生成画像数は12面の半分になります。選択は新規プロジェクトだけに保存され、既存プロジェクトやResumeの構成は変更しません。";
    };
    inputProjection.querySelector<HTMLSelectElement>("select")?.addEventListener("change", updateProjectionMode);
    updateProjectionMode();
    form.append(
      projectField,
      inputProjection,
      projectionModeNote,
      videoPicker,
      frameInterval,
      virtualRigLayout,
      tagFamily,
      tagIds,
      tagSize,
      apriltagEnabled,
      apriltagModeNote,
      horizontalization,
      personMaskProfile,
      checkboxField("人物マスクを適用（必須）", true, "person_mask_enabled"),
      colmapProfile,
    );
    const samplingNote = document.createElement("p");
    samplingNote.className = "preview-help preview-form-note";
    const markerIntervalMaximum = typeof preprocessing.max_frame_interval_for_metric_marker_sec === "number"
      ? preprocessing.max_frame_interval_for_metric_marker_sec : 1.0;
    const updateSamplingPolicy = (): void => {
      const metricScaleEnabled = apriltagEnabled.querySelector<HTMLInputElement>("input")?.checked ?? true;
      const tagInputs = [tagIds, tagSize]
        .map((item) => item.querySelector<HTMLInputElement>("input"))
        .filter((item): item is HTMLInputElement => item !== null);
      for (const input of tagInputs) input.disabled = !metricScaleEnabled;
      const tagFamilySelect = tagFamily.querySelector<HTMLSelectElement>("select");
      if (tagFamilySelect) tagFamilySelect.disabled = !metricScaleEnabled;
      for (const tagField of [tagFamily, tagIds, tagSize]) tagField.classList.toggle("is-disabled", !metricScaleEnabled);
      if (frameIntervalInput) frameIntervalInput.max = metricScaleEnabled ? String(markerIntervalMaximum) : "";
      apriltagModeNote.classList.toggle("is-no-apriltag", !metricScaleEnabled);
      const selectedFamily = tagFamilySelect?.value || tagFamilyContract.default_family || "tag25h9";
      apriltagModeNote.innerHTML = metricScaleEnabled
        ? `<strong>AprilTagありモード</strong><span>${selectedFamily}の固定タグID 0〜9から選び、実寸scaleとmeter単位のMRPを生成します。ID 10〜19は予備、20以上は箱タグ用です。</span>`
        : "<strong>AprilTagなしモード</strong><span>タグ欄と1秒上限を無効にし、カメラ姿勢から水平化したLichtFeld Studio入力までを生成します。実寸scale付きMRPは生成されないため、このJobのSPZ取込・Scene Package登録はできません。</span>";
      samplingNote.textContent = metricScaleEnabled
        ? `切り出し枚数の目安は「動画秒数 ÷ 間隔」です。1秒なら1秒ごとに1枚です。AprilTagで実寸scaleを適用するため、間隔は ${markerIntervalMaximum} 秒以下にしてください。水平化も行います。`
        : "切り出し枚数の目安は「動画秒数 ÷ 間隔」です。AprilTag実寸scaleは適用せず、カメラ姿勢から水平化したLichtFeld Studio入力までを生成します。";
    };
    apriltagEnabled.querySelector<HTMLInputElement>("input")?.addEventListener("change", updateSamplingPolicy);
    tagFamily.querySelector<HTMLSelectElement>("select")?.addEventListener("change", updateSamplingPolicy);
    updateSamplingPolicy();
    form.appendChild(samplingNote);
    form.appendChild(virtualRigNote);
    const inputHint = document.createElement("p");
    inputHint.className = "preview-help";
    inputHint.textContent = "入力したプロジェクト名をScene IDにも使用します。作業領域はvar/spatial/workspaces/projects/配下へ自動生成します。外部Windowsパス (例: C:\\video\\room.mp4) は、そのプロジェクト専用領域へリンクします。";
    form.appendChild(inputHint);
    overview.appendChild(form);
    const actions = document.createElement("div");
    actions.className = "preview-actions";
    const jobPicker = document.createElement("label");
    jobPicker.className = "preview-field preview-job-picker";
    const jobPickerCaption = document.createElement("span");
    jobPickerCaption.textContent = "処理履歴（表示対象）";
    const jobSelect = document.createElement("select");
    jobSelect.className = "preview-select";
    const jobPickerHelp = document.createElement("span");
    jobPickerHelp.className = "preview-help";
    jobPickerHelp.textContent = "上のプロジェクト名は新規処理用です。ここでは既存処理の進捗・成果物を切り替えます。";
    jobPicker.append(jobPickerCaption, jobSelect, jobPickerHelp);
    const jobStatus = document.createElement("div");
    jobStatus.className = "preview-inline-status";
    const jobOutcome = document.createElement("div");
    jobOutcome.className = "preview-job-outcome";
    jobOutcome.setAttribute("role", "status");
    jobOutcome.setAttribute("aria-live", "polite");
    const jobOutcomeTitle = document.createElement("strong");
    const jobOutcomeDetail = document.createElement("p");
    jobOutcome.append(jobOutcomeTitle, jobOutcomeDetail);
    const lfsPanel = document.createElement("section");
    lfsPanel.className = "preview-result-panel";
    const lfsTitle = document.createElement("strong");
    lfsTitle.textContent = "LichtFeld Studioへ渡すフォルダ";
    const lfsDescription = document.createElement("p");
    lfsDescription.className = "preview-help";
    const lfsPath = document.createElement("code");
    lfsPath.className = "preview-output-path";
    const lfsActions = document.createElement("div");
    lfsActions.className = "preview-actions";
    const openLfs = button("エクスプローラーで開く", () => {
      const jobId = openLfs.dataset.jobId;
      if (!jobId) return;
      openLfs.disabled = true;
      lfsDescription.textContent = "エクスプローラーを開いています…";
      void api<JsonObject>(`/api/jobs/${encodeURIComponent(jobId)}/open-lfs-folder`, { method: "POST" })
        .then((result) => {
          lfsDescription.textContent = result.opened === true
            ? `Windows Explorerで開きました: ${stringValue(result.path)}`
            : `Explorerを自動起動できませんでした。パスをコピーして開いてください: ${stringValue(result.path)}`;
        })
        .catch((error) => { lfsDescription.textContent = `開けませんでした: ${String(error)}`; })
        .finally(() => { openLfs.disabled = false; });
    }, true);
    const copyLfs = button("パスをコピー", () => {
      void navigator.clipboard?.writeText(copyLfs.dataset.path ?? "");
      lfsDescription.textContent = "パスをコピーしました。";
    }, true);
    lfsActions.append(openLfs, copyLfs);
    lfsPanel.append(lfsTitle, lfsDescription, lfsPath, lfsActions);
    const progressPanel = document.createElement("div");
    progressPanel.className = "preview-job-progress";
    const progressTitle = document.createElement("strong");
    const progressTrack = document.createElement("div");
    progressTrack.className = "preview-progress-track";
    progressTrack.setAttribute("role", "progressbar");
    progressTrack.setAttribute("aria-valuemin", "0");
    progressTrack.setAttribute("aria-valuemax", "100");
    const progressBar = document.createElement("span");
    progressTrack.appendChild(progressBar);
    const progressDetail = document.createElement("p");
    progressDetail.className = "preview-help";
    progressPanel.append(progressTitle, progressTrack, progressDetail);
    const stageTable = document.createElement("div");
    stageTable.className = "preview-stage-table";
    let refreshTimer: number | undefined;
    let refreshSerial = 0;
    let pipelineDisposed = false;
    let jobSelectionIsManual = false;
    let activeJobId: string | undefined = window.localStorage.getItem("spatial-3dgs.active-app-a-job") ?? undefined;
    const rememberActiveJob = (jobId: string | undefined): void => {
      activeJobId = jobId;
      if (jobId) window.localStorage.setItem("spatial-3dgs.active-app-a-job", jobId);
      else window.localStorage.removeItem("spatial-3dgs.active-app-a-job");
    };
    const sortedJobs = (jobs: JsonObject[]): JsonObject[] => [...jobs].sort((left, right) =>
      String(right.started_at ?? "").localeCompare(String(left.started_at ?? ""))
    );
    const updateJobPicker = (jobs: JsonObject[], requestedJobId?: string): JsonObject | undefined => {
      const ordered = sortedJobs(jobs);
      const remembered = ordered.find((job) => String(job.job_id) === activeJobId);
      const running = ordered.find((job) => job.status === "running" || job.status === "queued");
      let current = requestedJobId
        ? ordered.find((job) => String(job.job_id) === requestedJobId)
        : jobSelectionIsManual
          ? remembered
          : running ?? remembered;
      if (!current) current = running ?? ordered[0];
      rememberActiveJob(current ? String(current.job_id) : undefined);
      jobSelect.replaceChildren();
      if (ordered.length === 0) {
        const option = document.createElement("option");
        option.textContent = "処理履歴はまだありません";
        option.value = "";
        jobSelect.appendChild(option);
        jobSelect.disabled = true;
        return undefined;
      }
      jobSelect.disabled = false;
      for (const job of ordered) {
        const option = document.createElement("option");
        option.value = String(job.job_id);
        option.selected = job === current;
        option.textContent = `${stringValue(job.project_name, stringValue(job.scene_id, "project"))} — ${String(job.status)} — ${String(job.job_id)}`;
        jobSelect.appendChild(option);
      }
      return current;
    };
    const updateLfsPanel = (current: JsonObject | undefined): void => {
      const explorerPath = stringValue(current?.lfs_input_explorer_path, "");
      const localPath = stringValue(current?.lfs_input_path, stringValue(current?.lfs_input_uri, ""));
      const path = explorerPath || localPath;
      if (!current || !path) {
        lfsDescription.textContent = "この処理のLichtFeld Studio入力はまだ生成されていません。";
        lfsPath.textContent = "";
        openLfs.disabled = true;
        copyLfs.disabled = true;
        openLfs.dataset.jobId = "";
        copyLfs.dataset.path = "";
        return;
      }
      const imageCount = typeof current.lfs_image_count === "number" ? `（RGBA画像 ${current.lfs_image_count.toLocaleString()}枚）` : "";
      lfsDescription.textContent = `検証済みのLichtFeld Studio入力です${imageCount}。このフォルダをStudioで選択してください。`;
      lfsPath.textContent = path;
      openLfs.disabled = false;
      copyLfs.disabled = false;
      openLfs.dataset.jobId = String(current.job_id ?? "");
      copyLfs.dataset.path = path;
    };
    const updateProgress = (current: JsonObject | undefined): void => {
      if (!current) {
        progressTitle.textContent = "処理進捗: ジョブはまだありません";
        progressBar.style.width = "0%";
        progressTrack.setAttribute("aria-valuenow", "0");
        progressDetail.textContent = "処理を開始すると、現在工程・処理済み件数・全体進捗を自動更新します。";
        return;
      }
      const rawPercent = current.progress_percent;
      const percent = typeof rawPercent === "number" && Number.isFinite(rawPercent)
        ? Math.max(0, Math.min(100, Math.round(rawPercent))) : 0;
      const live = current.live_progress;
      const liveProgress = live && typeof live === "object" && !Array.isArray(live) ? live as JsonObject : undefined;
      progressTitle.textContent = current.status === "verified"
        ? "処理完了: 100%"
        : current.status === "failed" || current.status === "unverified"
          ? `処理失敗: ${percent}%`
          : `処理進捗: ${percent}%`;
      progressBar.style.width = `${percent}%`;
      progressTrack.setAttribute("aria-valuenow", String(percent));
      if (current.status === "verified") {
        progressDetail.textContent = "必要な全工程が正常に完了しました。";
      } else if (current.status === "failed" || current.status === "unverified") {
        const reasons = Array.isArray(current.failure_reasons) ? current.failure_reasons.map(String) : [];
        progressDetail.textContent = reasons.length ? reasons.join(" / ") : "処理が完了条件を満たしませんでした。";
      } else if (liveProgress) {
        const processed = typeof liveProgress.processed === "number" ? liveProgress.processed.toLocaleString() : "0";
        const total = typeof liveProgress.total === "number" ? liveProgress.total.toLocaleString() : "?";
        const unit = stringValue(liveProgress.unit, "件");
        const stagePercent = typeof liveProgress.stage_percent === "number" ? `${liveProgress.stage_percent}%` : "";
        progressDetail.textContent = `現在工程: ${stringValue(liveProgress.label, stringValue(current.current_stage, "準備中"))} — ${processed}/${total}${unit}（工程内 ${stagePercent}）`;
      } else {
        progressDetail.textContent = `現在工程: ${stringValue(current.current_stage, "完了")}`;
      }
    };
    const updateJobOutcome = (current: JsonObject | undefined): void => {
      jobOutcome.className = "preview-job-outcome";
      if (!current) {
        jobOutcome.hidden = true;
        return;
      }
      jobOutcome.hidden = false;
      const status = String(current.status ?? "unknown");
      jobOutcome.classList.add(`is-${status}`);
      if (status === "verified") {
        jobOutcomeTitle.textContent = "処理完了";
        const completedAt = stringValue(current.completed_at, "");
        const artifact = current.mrp_uri
          ? "Metric Reconstruction Packageまで正常に生成しました。"
          : "LichtFeld Studio入力まで正常に生成しました。";
        jobOutcomeDetail.textContent = `${artifact}${completedAt ? ` 完了: ${completedAt}` : ""}`;
      } else if (status === "failed" || status === "unverified") {
        jobOutcomeTitle.textContent = "処理失敗";
        const reasons = Array.isArray(current.failure_reasons) ? current.failure_reasons.map(String) : [];
        jobOutcomeDetail.textContent = reasons.length ? reasons.join(" / ") : "完了条件を満たしていません。";
      } else {
        jobOutcomeTitle.textContent = status === "queued" ? "開始待ち" : "処理中";
        jobOutcomeDetail.textContent = `Job ${String(current.job_id ?? "")}を実行しています。`;
      }
    };
    const scheduleJobRefresh = (serial: number): void => {
      refreshTimer = window.setTimeout(() => {
        refreshTimer = undefined;
        if (!pipelineDisposed && serial === refreshSerial) {
          void refreshStages(jobSelectionIsManual ? activeJobId : undefined);
        }
      }, 2000);
    };
    const refreshStages = async (jobId?: string): Promise<void> => {
      const serial = ++refreshSerial;
      if (refreshTimer !== undefined) {
        window.clearTimeout(refreshTimer);
        refreshTimer = undefined;
      }
      try {
        const jobs = await api<JsonObject[]>("/api/jobs");
        if (pipelineDisposed || serial !== refreshSerial) return;
        const current = updateJobPicker(jobs, jobId);
        const stages = current ? await api<JsonObject[]>(`/api/jobs/${String(current.job_id)}/stages`) : [];
        if (pipelineDisposed || serial !== refreshSerial) return;
        stageTable.replaceChildren();
        for (const stage of stages) {
          const row = document.createElement("div");
          row.className = "preview-stage-row";
          row.append(document.createElement("span"));
          row.children[0].textContent = String(stage.label);
          row.appendChild(statusBadge(stage.status));
          stageTable.appendChild(row);
        }
        if (current) {
          rememberActiveJob(String(current.job_id));
          jobStatus.textContent = `Job ${String(current.job_id)}: ${String(current.status)} (exit ${String(current.exit_code ?? "running")})`;
          updateJobOutcome(current);
          updateProgress(current);
          updateLfsPanel(current);
        } else {
          jobStatus.textContent = "No pipeline job has been started from this preview.";
          updateJobOutcome(undefined);
          updateProgress(undefined);
          updateLfsPanel(undefined);
        }
        scheduleJobRefresh(serial);
      } catch (error) {
        if (pipelineDisposed || serial !== refreshSerial) return;
        jobStatus.textContent = `API unavailable: ${String(error)}`;
        updateJobOutcome(undefined);
        updateProgress(undefined);
        updateLfsPanel(undefined);
        scheduleJobRefresh(serial);
      }
    };
    jobSelect.addEventListener("change", () => {
      jobSelectionIsManual = true;
      rememberActiveJob(jobSelect.value || undefined);
      void refreshStages(jobSelect.value || undefined);
    });

    const inputValue = (key: string): string => {
      const input = form.querySelector<HTMLInputElement>(`input[data-field="${key}"]`);
      return input?.value.trim() ?? "";
    };
    const inputChecked = (key: string): boolean => form.querySelector<HTMLInputElement>(`input[data-field="${key}"]`)?.checked ?? false;
    const selectValue = (key: string): string => form.querySelector<HTMLSelectElement>(`select[data-field="${key}"]`)?.value ?? "";
    const isAbsolutePath = (value: string): boolean => /^[A-Za-z]:[\\/]/.test(value) || value.startsWith("/");
    const start = async (mode: "run" | "resume" | "reuse"): Promise<void> => {
      try {
        jobStatus.textContent = `${mode} starting...`;
        const video = inputValue("video");
        const project = inputValue("project");
        if (mode === "run" && !project) {
          jobStatus.textContent = "新規処理を開始する前に、プロジェクト名を入力してください。";
          return;
        }
        if (mode === "run" && projectInput && !projectInput.reportValidity()) {
          jobStatus.textContent = "プロジェクト名の入力規則を確認してください。";
          return;
        }
        if (mode === "run" && !video) {
          jobStatus.textContent = "新規処理を開始する前に、入力動画を選択してください。";
          return;
        }
        const projection = selectValue("input_projection") || "equirectangular_360";
        const body: JsonObject = {
          scene_id: project,
          scene_template_id: sceneTemplateId,
          project,
          profile: "quality",
          stop_after: "metric_reconstruction_package",
          apriltag_enabled: inputChecked("apriltag_enabled"),
          tag_family: selectValue("tag_family"),
          tag_ids: inputValue("tag_ids"),
          tag_size_m: Number(inputValue("tag_size_m")),
          frame_interval_sec: Number(inputValue("frame_interval_sec")),
          person_mask_profile: inputValue("person_mask_profile"),
          person_mask_enabled: inputChecked("person_mask_enabled"),
          colmap_profile: inputValue("colmap_profile"),
          input_projection: projection,
        };
        if (projection === "equirectangular_360") {
          body.virtual_rig_layout = selectValue("virtual_rig_layout");
        }
        if (video) body[isAbsolutePath(video) ? "input_video_path" : "input_video"] = video;
        const targetJobId = activeJobId ?? "";
        if (mode !== "run" && !targetJobId) {
          jobStatus.textContent = "再開・再利用する処理を「表示する処理」から選択してください。";
          return;
        }
        const endpoint = mode === "run" ? "/api/jobs" : `/api/jobs/${encodeURIComponent(targetJobId)}/${mode}`;
        const job = await api<JsonObject>(endpoint, {
          method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body)
        });
        jobSelectionIsManual = false;
        rememberActiveJob(String(job.job_id));
        const allocatedProject = String(job.project_name ?? "");
        if (projectInput && allocatedProject) projectInput.value = allocatedProject;
        await refreshStages(String(job.job_id));
      } catch (error) {
        jobStatus.textContent = `${mode} failed: ${String(error)}`;
      }
    };
    actions.append(
      button("Run", () => void start("run")),
      button("Resume", () => void start("resume")),
      button("Reuse", () => void start("reuse")),
    );
    overview.append(actions, jobPicker, jobStatus, jobOutcome, lfsPanel, progressPanel, stageTable);
    const logPanel = section("Pipeline log");
    const logText = document.createElement("pre");
    logText.className = "preview-log";
    logText.textContent = "ジョブを開始するとログが表示されます。";
    logPanel.appendChild(logText);
    const refreshLog = async (): Promise<void> => {
      if (!activeJobId) return;
      try {
        const log = await api<JsonObject>(`/api/jobs/${encodeURIComponent(activeJobId)}/logs`);
        logText.textContent = String(log.text ?? "");
      } catch (error) {
        logText.textContent = String(error);
      }
    };
    actions.appendChild(button("Refresh log", () => void refreshLog()));
    content.appendChild(overview);
    content.appendChild(logPanel);
    await refreshStages();
    return () => {
      pipelineDisposed = true;
      refreshSerial += 1;
      if (refreshTimer !== undefined) window.clearTimeout(refreshTimer);
    };
  }

  async function renderPackages(): Promise<void> {
    content.replaceChildren();
    const repositoryPanel = section("データ保存領域");
    const repositorySummary = document.createElement("p");
    repositorySummary.textContent = "Repositoryカタログと保存領域を確認しています…";
    const repositoryMeta = document.createElement("dl");
    repositoryMeta.className = "preview-package-meta";
    const renderRepositoryStatus = (status: JsonObject): void => {
      repositoryMeta.replaceChildren();
      const counts = (status.counts as JsonObject | undefined) ?? {};
      const migration = (status.migration as JsonObject | undefined) ?? {};
      const packageMigration = (status.package_migration as JsonObject | undefined) ?? {};
      const layout = (status.layout as JsonObject | undefined) ?? {};
      const rows: Array<[string, string]> = [
        ["Repository", String(layout.repository ?? "data/scene_repository")],
        ["カタログ", `${String(status.status ?? "unknown")} / ${String(status.catalog_uri ?? "data/scene_repository/index/catalog.sqlite3")}`],
        ["Scene Package正本", String(layout.scene_packages ?? "data/scene_repository/packages/scenes")],
        ["入力資産", String(layout.sources ?? "data/scene_repository/assets/source_files/sha256")],
        ["現場・設備台帳", String(layout.site_records ?? "data/scene_repository/registries/site_records")],
        ["配布物", String(layout.deliverables ?? "data/scene_repository/deliverables")],
        ["実行領域", String(layout.runtime ?? "var/spatial")],
        ["Package / MRP / Project", `${String(counts.scene_packages ?? 0)} / ${String(counts.metric_reconstructions ?? 0)} / ${String(counts.projects ?? 0)}`],
        ["自己完結 / 未移行legacy", `${String(packageMigration.self_contained ?? 0)} / ${String(packageMigration.unmigrated_legacy ?? 0)}`],
      ];
      for (const [label, value] of rows) {
        const term = document.createElement("dt");
        term.textContent = label;
        const detail = document.createElement("dd");
        detail.textContent = value;
        repositoryMeta.append(term, detail);
      }
      const unmigrated = Number(packageMigration.unmigrated_legacy ?? 0);
      repositorySummary.textContent = unmigrated > 0
        ? `移行は適用済みです。元データ欠損により閲覧専用のlegacy Packageが${unmigrated}件残っています。`
        : `移行は${String(migration.status ?? "unknown")}です。全Packageが自己完結版へ対応付け済みです。`;
    };
    const rebuildCatalog = button("カタログを再構築", () => {
      rebuildCatalog.disabled = true;
      void api<JsonObject>("/api/repository/rebuild", { method: "POST" })
        .then(renderRepositoryStatus)
        .catch((error) => { repositorySummary.textContent = `カタログ再構築に失敗しました: ${String(error)}`; })
        .finally(() => { rebuildCatalog.disabled = false; });
    });
    rebuildCatalog.className = "preview-secondary";
    repositoryPanel.append(repositorySummary, repositoryMeta, rebuildCatalog);
    if (!packageViewIntent) {
      void api<JsonObject>("/api/repository")
        .then(renderRepositoryStatus)
        .catch((error) => { repositorySummary.textContent = `Repositoryの状態を取得できません: ${String(error)}`; });
    }
    const dataPanel = section("保存データの選択・整理");
    if (!packageViewIntent) mountRepositoryDataManager(dataPanel, () => void renderPackages());
    const heading = document.createElement("div");
    heading.className = "preview-heading";
    heading.innerHTML = packageViewIntent
      ? "<h1>3Dシーン閲覧</h1><p>公開済みScene Packageを選び、3DGS空間を開きます。この入口ではデータの削除やRepository設定は行いません。</p>"
      : "<h1>シーンデータ管理</h1><p>Scene Package、MRP、入力原本、現場・設備台帳、配布物を一つの画面で管理します。参照中データは削除できず、選択削除は復旧可能なゴミ箱へ退避します。</p>";
    const panel = section("登録済みScene Package");
    const description = document.createElement("p");
    description.textContent = "検証済みでViewer公開済みのPackageだけを開けます。Proxyが未検証の場合、Viewerは表示のみで測定・配置・遮蔽をfail-closedにします。";
    const list = document.createElement("div");
    list.className = "preview-package-list";
    const loading = document.createElement("p");
    loading.className = "preview-empty-state";
    loading.textContent = "Scene Package一覧を読み込んでいます…";
    list.appendChild(loading);
    const refresh = button("一覧を更新", () => void renderPackages());
    refresh.className = "preview-secondary";
    const packageActions = document.createElement("div");
    packageActions.className = "preview-actions";
    if (packageViewIntent) packageActions.append(refresh);
    else packageActions.append(button("3DGSデータを登録", () => setMode("spatial")), refresh);
    panel.append(description, packageActions, list);
    if (packageViewIntent) content.append(heading, panel);
    else content.append(heading, repositoryPanel, dataPanel, panel);
    try {
      const { packages, source } = await publishedScenePackages();
      list.replaceChildren();
      const managed = packages.filter((item) => item.managed === true);
      if (source === "bundle") {
        description.textContent = "この配布版は選択済みScene Packageの閲覧専用です。3DGSデータの新規登録・削除・3D再構成は含みません。";
      }
      if (managed.length === 0) {
        const empty = document.createElement("p");
        empty.className = "preview-empty-state";
        empty.textContent = "公開済み3DGSデータはありません。3DGSデータ登録で、SPZを学習元MRPへ対応付けてください。";
        list.appendChild(empty);
      }
      for (const item of managed) {
        const card = document.createElement("article");
        card.className = "preview-package-card";
        const title = document.createElement("h3");
        title.textContent = String(item.scene_package_id ?? "Scene Package");
        const metadata = document.createElement("dl");
        metadata.className = "preview-package-meta";
        const spz = (item.spz as JsonObject | undefined) ?? {};
        const dataset = (item.dataset as JsonObject | undefined) ?? {};
        const applicationCompatibility = (item.application_compatibility as JsonObject | undefined) ?? {};
        const compatibilityLabel = (applicationId: string): string => {
          const value = applicationCompatibility[applicationId];
          if (!value || typeof value !== "object" || Array.isArray(value)) return "不明";
          return (value as JsonObject).compatible === true ? "対応" : "非対応";
        };
        const referenceCount = typeof item.field_memory_reference_count === "number"
          ? item.field_memory_reference_count
          : undefined;
        const colmapMaskApplied = dataset.colmap_masks_applied_to_packaged_database;
        const maskApplication = colmapMaskApplied === true
          ? "現行人物マスクを適用済み"
          : colmapMaskApplied === false
            ? "未適用（COLMAP再実行が必要）"
            : "旧schema / 不明";
        const entries: Array<[string, string]> = packageViewIntent
          ? [
              ["プロジェクト", String(item.project_name ?? "—")],
              ["MRP", String(item.reconstruction_id ?? "—")],
              ["登録フレーム", `${String(dataset.frame_count ?? "—")}件`],
              ["3D閲覧", compatibilityLabel("viewer")],
            ]
          : [
              ["プロジェクト", String(item.project_name ?? "—")],
              ["MRP", String(item.reconstruction_id ?? "—")],
              ["SPZ", String(spz.filename ?? "—")],
              ["Scene Package", String(item.status ?? "unknown")],
              ["Viewer公開", item.published === true ? "公開済み" : "未公開"],
              ["Dataset schema", String(dataset.schema_version ?? "—")],
              ["登録フレーム / 人物マスク / COLMAPマスク", `${String(dataset.frame_count ?? "—")} / ${String(dataset.mask_count ?? "—")} / ${String(dataset.colmap_mask_count ?? "—")}`],
              ["人物マスク生成", String(dataset.mask_backend ?? "—")],
              ["COLMAPとのマスク整合", maskApplication],
              ["Field Memory参照", referenceCount === undefined ? "確認不能" : `${referenceCount}件`],
              ["アプリ互換性（3D閲覧 / 現場・設備台帳 / 登録画像AI）", `${compatibilityLabel("viewer")} / ${compatibilityLabel("field-memory")} / ${compatibilityLabel("frame-analysis")}`],
            ];
        for (const [label, value] of entries) {
          const term = document.createElement("dt");
          term.textContent = label;
          const detail = document.createElement("dd");
          detail.textContent = value;
          metadata.append(term, detail);
        }
        const actions = document.createElement("div");
        actions.className = "preview-actions";
        const canOpen = item.status === "verified" && item.published === true && typeof item.viewer_url === "string";
        const open = button("多機能Viewerで開く", () => {
          const url = new URL(String(item.viewer_url), window.location.href);
          url.searchParams.set("viewer", "full");
          window.location.href = `${url.pathname}${url.search}${url.hash}`;
        }, !canOpen);
        const openSimple = button("シンプルViewerで開く", () => {
          const url = new URL(String(item.viewer_url), window.location.href);
          url.searchParams.set("viewer", "simple");
          window.location.href = `${url.pathname}${url.search}${url.hash}`;
        }, !canOpen);
        openSimple.className = "preview-secondary";
        const note = document.createElement("p");
        note.className = "preview-package-note";
        note.textContent = canOpen
          ? "SPZとMRPは同一の reconstruction ID で対応付け済みです。"
          : "このPackageは未公開または検証不合格のため開けません。";
        if (__SPATIAL_VIEWER_VARIANT__ === "simple") actions.append(openSimple);
        else actions.append(open, openSimple);
        const packageId = typeof item.scene_package_id === "string" ? item.scene_package_id : "";
        const itemSceneId = typeof item.scene_id === "string" ? item.scene_id : "";
        if (!packageViewIntent && source === "api" && item.managed === true && packageId && itemSceneId) {
          const referenceStatus = String(item.field_memory_reference_status ?? "unknown");
          const unregister = button("登録解除", () => {
            if (!window.confirm(`「${packageId}」の登録を解除します。COLMAP/MRPと選択元のSPZは削除しません。`)) return;
            unregister.disabled = true;
            note.textContent = "登録を解除しています…";
            void api<JsonObject>(`/api/scene-packages/${encodeURIComponent(itemSceneId)}/${encodeURIComponent(packageId)}`, { method: "DELETE" })
              .then((result) => {
                note.textContent = `登録を解除しました: ${stringValue(result.trashed_uri)}`;
                void renderPackages();
              })
              .catch((error) => {
                note.textContent = `登録解除に失敗しました: ${String(error)}`;
                unregister.disabled = false;
              });
          }, referenceStatus !== "unreferenced");
          unregister.className = "preview-danger";
          unregister.title = referenceStatus === "referenced"
            ? "Field MemoryのSceneVersionから参照中のため登録解除できません。"
            : referenceStatus === "unknown"
              ? "Field Memory参照を確認できないため、安全のため登録解除できません。"
              : "";
          actions.append(unregister);
        }
        card.append(title, metadata, note, actions);
        list.appendChild(card);
      }
    } catch (error) {
      const failed = document.createElement("p");
      failed.className = "preview-empty-state";
      failed.textContent = `Scene Package一覧を取得できません: ${String(error)}`;
      list.replaceChildren(failed);
    }
    const bundled = await bundledScenePackages();
    if (!packageViewIntent && !bundled && allowedModes.has("spatial")) {
      const importPanel = section("SPZを新規登録する場合");
      const importText = document.createElement("p");
      importText.textContent = "SPZを学習したCOLMAP処理プロジェクトのMRPを選び、検証後に登録します。対応が不明なSPZは登録しません。";
      importPanel.append(importText, button("3DGSデータ登録へ進む", () => setMode("spatial")));
      content.appendChild(importPanel);
    }
  }

  async function render(): Promise<void> {
    const serial = ++renderSerial;
    activeViewCleanup?.();
    activeViewCleanup = undefined;
    if (activeMode === "packages" && sceneManagementAvailable === undefined) {
      try {
        const health = await api<JsonObject>("/api/health", { signal: AbortSignal.timeout(5_000) });
        if (Array.isArray(health.enabled_features)) {
          sceneManagementAvailable = health.enabled_features.includes("suite")
            || health.enabled_features.includes("scene-manager");
        }
      } catch {
        // Preserve the management view's real API error and retry next time.
      }
      if (serial !== renderSerial) return;
      if (sceneManagementAvailable === false) {
        appSelect.querySelector('option[value="packages"]')?.remove();
      }
    }
    if (activeMode === "packages" && sceneManagementAvailable === false) {
      packageViewIntent = true;
    }
    content.classList.toggle("package-builder-page", activeMode === "package-builder");
    viewerLink.hidden = packageViewIntent && activeMode === "packages";
    appSelect.value = packageViewIntent && activeMode === "packages" ? "packages-viewer" : activeMode;
    if (activeMode === "pipeline") activeViewCleanup = await renderPipeline();
    else if (activeMode === "tag-check") activeViewCleanup = await mountAprilTagLiveCheck(content, {
      integratedViewerMode,
      onShowViewer: showIntegratedViewer,
    });
    else if (activeMode === "packages") await renderPackages();
    else if (activeMode === "spatial") await mountSpzImportView(content);
    else if (activeMode === "projects") await mountProjectWorkspaceView(content, () => setMode("spatial"));
    else if (activeMode === "field-memory") await mountFieldMemoryView(content);
    else if (activeMode === "frame-analysis") activeViewCleanup = await mountFrameAnalysisView(content);
    else if (activeMode === "site-insights") activeViewCleanup = await mountSiteInsightsView(content);
    else await mountPackageBuilderView(content);
  }

  if (options.fixedMode || initial === "pipeline" || initial === "tag-check" || initial === "packages" || initial === "spatial" || initial === "projects" || initial === "field-memory" || initial === "frame-analysis" || initial === "site-insights" || initial === "package-builder") void render();
  else {
    const launchButton = button("Application Preview", () => setMode("packages"));
    launchButton.className = "preview-launch";
    document.body.appendChild(launchButton);
  }
}
