import { api, previewApiUrl, type JsonObject } from "./PreviewApiClient";

interface UiModule {
  id: string;
  label: string;
  preview_mode: string;
  required_backends: string[];
  needs_camera: boolean;
}

interface BackendComponent {
  id: string;
  label: string;
  adapter: string;
  api_feature: string | null;
  needs_gpu: boolean;
  needs_docker_socket: boolean;
}

interface ScenePackageCandidate {
  scene_package_id: string;
  scene_id: string;
  reconstruction_id?: string;
  project_name?: string;
  data_date?: string;
  data_date_source?:
    | "captured_at"
    | "identifier_date"
    | "metric_reconstruction_created_at"
    | "scene_package_created_at"
    | "dataset_created_at"
    | "registered_at";
  registered_at?: string;
  spz?: { filename?: string; size_bytes?: number } | null;
  schema_version?: string;
  dynamic_tracking_compatible?: boolean;
  dynamic_tracking_failure_reasons?: string[];
  capabilities?: Record<string, boolean>;
}

interface ProductProfile {
  metadata: { id: string; version: string; label: string; tier: string };
  composition: {
    ui_modules: string[];
    backend_components: string[];
    viewer_variant: "full" | "simple";
  };
  features: { required?: string[]; optional?: string[] };
  content: {
    minimum_scene_packages: number;
    maximum_scene_packages: number;
    required_scene_capabilities: string[];
    include_active_analysis: boolean;
  };
  platforms: {
    supported_targets?: string[];
    qualification_targets?: string[];
    planned?: string[];
  };
  distribution: { modes: Array<"split" | "offline" | "connected">; default_mode: string };
  acceptance: { suite: string; required: string[] };
}

interface ConditionalBackendRequirement {
  id: string;
  all_ui_modules: string[];
  minimum_scene_packages: number;
  required_backends: string[];
  reason?: string;
}

interface ViewerVariant {
  id: "full" | "simple";
  label: string;
  description: string;
}

interface PackageCatalog {
  ui_modules: UiModule[];
  backend_components: BackendComponent[];
  scene_packages: ScenePackageCandidate[];
  conditional_backend_requirements?: ConditionalBackendRequirement[];
  viewer_variants?: ViewerVariant[];
  product_profiles?: ProductProfile[];
}

interface BuiltPackage extends JsonObject {
  application_id?: string;
  label?: string;
  filename?: string;
  download_url?: string;
  output_path?: string;
  size_bytes?: number;
  created_at?: string;
  ui_modules?: string[];
  backend_components?: string[];
  scene_package_ids?: string[];
  viewer_variant?: "full" | "simple";
  requirements?: { gpu?: boolean; camera?: boolean; rosbridge?: boolean };
  setup_included?: boolean;
  analysis_overlay?: {
    included?: boolean;
    runs?: Array<{ frame_count?: number }>;
  };
  product_release?: {
    release_id?: string;
    lifecycle?: string;
    support_level?: string;
    distribution_mode?: string;
  };
  content_delivery?: {
    mode?: string;
    content_packs?: Array<{
      filename?: string;
      sha256?: string;
      size_bytes?: number;
      download_url?: string;
      scene_package_id?: string;
    }>;
  };
}

interface PackagePreflight extends JsonObject {
  can_build: boolean;
  available_free_bytes: number;
  required_free_bytes: number;
  peak_additional_bytes: number;
  staging_copy_bytes: number;
  estimated_zip_bytes: number;
  scene_package_bytes: number;
  content_pack_estimate_bytes?: number;
  runtime_staging_bytes?: number;
  analysis_overlay: {
    run_count: number;
    frame_count: number;
    attachment_count: number;
    attachment_bytes: number;
    total_bytes: number;
  };
}

interface PackageBuildJob extends JsonObject {
  job_id: string;
  status: "queued" | "running" | "completed" | "failed" | "cancelled" | "interrupted";
  stage?: string;
  progress_percent?: number;
  message?: string;
  cancellation_requested?: boolean;
  can_retry?: boolean;
  failure_reason?: string | null;
  result?: BuiltPackage;
}

const PACKAGE_ID_PATTERN = /^[a-z0-9][a-z0-9-]{1,47}$/;

interface ComponentCategory {
  id: string;
  label: string;
  description: string;
  componentIds: string[];
}

const UI_COMPONENT_CATEGORIES: ComponentCategory[] = [
  {
    id: "view-ai",
    label: "閲覧・AI活用",
    description: "3D閲覧、登録画像解析、検索・根拠質問・変化確定",
    componentIds: ["viewer", "simple-viewer", "frame-analysis", "site-insights"],
  },
  {
    id: "field-management",
    label: "現場データ管理",
    description: "現場台帳、Scene Package、処理履歴の管理",
    componentIds: ["field-memory", "scene-manager", "project-manager"],
  },
  {
    id: "data-creation",
    label: "データ作成・登録",
    description: "COLMAP、AprilTag確認、3DGSデータ登録",
    componentIds: ["colmap", "apriltag-check", "spz-import"],
  },
  {
    id: "distribution",
    label: "配布管理",
    description: "配布先でもパッケージを作成する場合に選択",
    componentIds: ["package-builder"],
  },
];

const BACKEND_COMPONENT_CATEGORIES: ComponentCategory[] = [
  {
    id: "viewer-data",
    label: "閲覧・データ管理",
    description: "Scene Package配信、現場台帳、プロジェクト管理",
    componentIds: ["scene-reader-api", "scene-manager-api", "field-memory-api", "project-manager-api", "package-builder-api"],
  },
  {
    id: "ai-search",
    label: "AI解析・検索",
    description: "登録画像解析、検索索引、根拠付きAI、変化確認",
    componentIds: ["frame-analysis-api", "frame-vlm-worker", "search-api", "image-feature-worker", "evidence-ai-api", "change-review-api"],
  },
  {
    id: "creation",
    label: "データ作成・登録",
    description: "COLMAP、AprilTag検出、SPZ登録",
    componentIds: ["pipeline-api", "apriltag-api", "spz-import-api"],
  },
  {
    id: "external",
    label: "外部接続",
    description: "必要な場合だけ同梱するROS接続",
    componentIds: ["rosbridge"],
  },
];

function createCategorySections(
  column: HTMLElement,
  categories: ComponentCategory[],
): { options: Map<string, HTMLElement>; sections: HTMLElement[] } {
  const options = new Map<string, HTMLElement>();
  const sections: HTMLElement[] = [];
  for (const category of categories) {
    const section = document.createElement("section");
    section.className = "package-builder-category";
    section.dataset.category = category.id;
    const header = document.createElement("header");
    const title = document.createElement("h4");
    title.textContent = category.label;
    const description = document.createElement("small");
    description.textContent = category.description;
    header.append(title, description);
    const optionList = document.createElement("div");
    optionList.className = "package-builder-category-options";
    section.append(header, optionList);
    column.appendChild(section);
    options.set(category.id, optionList);
    sections.push(section);
  }
  return { options, sections };
}

function appendCategorizedOption(
  itemId: string,
  option: HTMLElement,
  categories: ComponentCategory[],
  categoryOptions: Map<string, HTMLElement>,
): void {
  const category = categories.find((candidate) => candidate.componentIds.includes(itemId));
  const target = category ? categoryOptions.get(category.id) : undefined;
  (target ?? categoryOptions.values().next().value)?.appendChild(option);
}

function removeEmptyCategorySections(sections: HTMLElement[]): void {
  for (const section of sections) {
    if (!section.querySelector(".package-builder-option")) section.remove();
  }
}

function bytes(value: unknown): string {
  const size = typeof value === "number" ? value : Number(value);
  if (!Number.isFinite(size)) return "—";
  if (size < 1024 * 1024) return `${Math.round(size / 1024)} KiB`;
  return `${(size / 1024 / 1024).toFixed(1)} MiB`;
}

function packageIdSlug(value: string): string {
  const slug = value
    .normalize("NFKC")
    .trim()
    .toLowerCase()
    .replace(/[_\s]+/g, "-")
    .replace(/[^a-z0-9-]+/g, "-")
    .replace(/-+/g, "-")
    .replace(/^-+|-+$/g, "");
  return slug.slice(0, 48).replace(/-+$/g, "");
}

function formatDate(value: unknown): string {
  if (typeof value !== "string" || !value) return "日時不明";
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? value : date.toLocaleString("ja-JP");
}

function friendlyBuildError(error: unknown): { message: string; technical: string } {
  const technical = error instanceof Error ? error.message : String(error);
  if (technical.includes("package_recipe_package_id_invalid")) {
    return {
      message: "パッケージIDが不正です。英小文字または数字で始め、英小文字・数字・ハイフンだけを使用してください。",
      technical,
    };
  }
  if (technical.includes("package_recipe_ui_module_required")) {
    return { message: "配布するWeb画面を1つ以上選択してください。", technical };
  }
  if (technical.includes("package_recipe_missing_backend")) {
    return { message: "選択したWeb画面に必要なBackend部品が不足しています。選択内容を更新してください。", technical };
  }
  if (technical.includes("package_recipe_scene_package_not_verified")) {
    return { message: "選択したScene Packageは現在の検証済み一覧にありません。一覧を再読み込みしてください。", technical };
  }
  if (technical.includes("product_profile_scene_capability_missing")) {
    return { message: "選択したScene Packageは、この標準製品に必要な表示・計測capabilityを満たしていません。互換表示のあるPackageを選択してください。", technical };
  }
  if (technical.includes("formal_product_build_requires_clean_worktree")) {
    return { message: "標準製品のcandidate生成には、未コミット差分のないGit状態が必要です。変更を検証してコミットしてから再実行してください。", technical };
  }
  if (technical.includes("product_profile_composition_mismatch")) {
    return { message: "標準製品の画面・Backend構成が変更されています。製品プロファイルを選び直してください。", technical };
  }
  if (technical.includes("package_recipe_simple_viewer_requires_one_scene_package")) {
    return { message: "シンプル3DGS ViewerにはScene Packageを1件だけ選択してください。", technical };
  }
  if (technical.includes("package_recipe_viewer_requires_scene_package")) {
    return { message: "多機能3Dシーン閲覧にはScene Packageを1件以上選択してください。", technical };
  }
  if (technical.includes("package_recipe_frame_analysis_requires_scene_package")) {
    return { message: "登録画像AI解析には、解析対象のScene Packageを1件以上選択してください。", technical };
  }
  if (technical.includes("package_recipe_simple_viewer_must_be_exclusive")) {
    return { message: "シンプル3DGS Viewerは、登録画像AI解析以外のWeb画面とは同時に選択できません。", technical };
  }
  if (technical.includes("package_recipe_scene_package_dynamic_tracking_incompatible")) {
    return {
      message: "AprilTag→3DGS Viewer連携には、T_viewer_rosとdynamic_overlayを持つScene Package v1.2が必要です。対応表示のあるPackageを選択してください。",
      technical,
    };
  }
  if (technical.includes("storage_headroom") || technical.includes("insufficient_free_space")) {
    return { message: "ZIPを生成するための空きディスク容量が不足しています。", technical };
  }
  return { message: "Docker ZIPを生成できませんでした。技術情報を確認してください。", technical };
}

function displayIds(ids: unknown, labels: Map<string, string>): string {
  if (!Array.isArray(ids) || ids.length === 0) return "なし";
  return ids.map((id) => labels.get(String(id)) ?? String(id)).join("、");
}

function checklistItem(
  title: string,
  detail: string,
  value: string,
): { root: HTMLLabelElement; input: HTMLInputElement } {
  const root = document.createElement("label");
  root.className = "package-builder-option";
  const input = document.createElement("input");
  input.type = "checkbox";
  input.value = value;
  const body = document.createElement("span");
  const strong = document.createElement("strong");
  strong.textContent = title;
  const small = document.createElement("small");
  small.textContent = detail;
  body.append(strong, small);
  root.append(input, body);
  return { root, input };
}

type ScenePackageSortMode = "data-desc" | "data-asc" | "registered-desc" | "name-asc";

const SCENE_PACKAGE_DATE_SOURCE_LABELS: Record<string, string> = {
  captured_at: "撮影日時",
  identifier_date: "名称の日付",
  metric_reconstruction_created_at: "再構成日時",
  scene_package_created_at: "Package作成日時",
  dataset_created_at: "Dataset作成日時",
  registered_at: "登録日時",
};

function scenePackageDisplayName(item: ScenePackageCandidate): string {
  const source = item.project_name?.trim() || item.reconstruction_id?.trim() || item.scene_id;
  const withoutDate = source.replace(/^(?:19|20)\d{6}[_-]?/, "");
  const readable = (withoutDate || source).replace(/[_-]+/g, " ").trim();
  return readable || item.scene_package_id;
}

function scenePackageDateValue(value: string | undefined): number {
  if (!value) return Number.NEGATIVE_INFINITY;
  const parsed = Date.parse(/^\d{4}-\d{2}-\d{2}$/.test(value) ? `${value}T00:00:00Z` : value);
  return Number.isFinite(parsed) ? parsed : Number.NEGATIVE_INFINITY;
}

function formatScenePackageDate(value: string | undefined): string {
  if (!value) return "不明";
  const dateOnly = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
  if (dateOnly) return `${dateOnly[1]}/${dateOnly[2]}/${dateOnly[3]}`;
  const parsed = new Date(value);
  return Number.isNaN(parsed.valueOf())
    ? value
    : new Intl.DateTimeFormat("ja-JP", {
        dateStyle: "medium",
        timeStyle: "short",
        timeZone: "Asia/Tokyo",
      }).format(parsed);
}

function scenePackageOption(
  item: ScenePackageCandidate,
): { root: HTMLLabelElement; input: HTMLInputElement } {
  const root = document.createElement("label");
  root.className = "package-builder-option package-builder-scene-card";
  root.dataset.scenePackageId = item.scene_package_id;

  const input = document.createElement("input");
  input.type = "checkbox";
  input.value = item.scene_package_id;

  const body = document.createElement("span");
  body.className = "package-builder-scene-card-body";
  const header = document.createElement("span");
  header.className = "package-builder-scene-card-header";
  const title = document.createElement("strong");
  title.textContent = scenePackageDisplayName(item);
  const dataDate = document.createElement("span");
  dataDate.className = "package-builder-scene-date";
  dataDate.textContent = formatScenePackageDate(item.data_date);
  header.append(title, dataDate);

  const packageId = document.createElement("code");
  packageId.className = "package-builder-scene-id";
  packageId.textContent = item.scene_package_id;

  const metadata = document.createElement("span");
  metadata.className = "package-builder-scene-meta";
  const tracking = item.dynamic_tracking_compatible
    ? "動的追跡対応"
    : "表示専用";
  const dateSource = SCENE_PACKAGE_DATE_SOURCE_LABELS[item.data_date_source ?? ""] ?? "データ日";
  const entries = [
    `${dateSource}: ${formatScenePackageDate(item.data_date)}`,
    `登録: ${formatScenePackageDate(item.registered_at)}`,
    `形式: ${item.schema_version ?? "不明"}`,
    `SPZ: ${bytes(item.spz?.size_bytes)}`,
    tracking,
  ];
  for (const entry of entries) {
    const value = document.createElement("small");
    value.textContent = entry;
    metadata.appendChild(value);
  }

  body.append(header, packageId, metadata);
  root.append(input, body);
  return { root, input };
}

function compareScenePackages(
  left: ScenePackageCandidate,
  right: ScenePackageCandidate,
  mode: ScenePackageSortMode,
): number {
  const byName = scenePackageDisplayName(left).localeCompare(
    scenePackageDisplayName(right),
    "ja",
    { numeric: true, sensitivity: "base" },
  );
  if (mode === "name-asc") return byName || left.scene_package_id.localeCompare(right.scene_package_id);
  if (mode === "registered-desc") {
    return scenePackageDateValue(right.registered_at) - scenePackageDateValue(left.registered_at)
      || byName
      || left.scene_package_id.localeCompare(right.scene_package_id);
  }
  const direction = mode === "data-asc" ? 1 : -1;
  const leftDataDate = scenePackageDateValue(left.data_date);
  const rightDataDate = scenePackageDateValue(right.data_date);
  if (leftDataDate !== rightDataDate) return direction * (leftDataDate < rightDataDate ? -1 : 1);
  return scenePackageDateValue(right.registered_at) - scenePackageDateValue(left.registered_at)
    || byName
    || left.scene_package_id.localeCompare(right.scene_package_id);
}

export async function mountPackageBuilderView(host: HTMLElement): Promise<void> {
  host.replaceChildren();
  const heading = document.createElement("div");
  heading.className = "preview-heading";
  heading.innerHTML = "<h1>配布アプリ作成</h1><p>用途に必要な画面と登録済みbackend部品を選び、1コマンドで起動できるDocker ZIPを生成します。管理者向けの機能です。</p>";
  host.appendChild(heading);

  const catalog = await api<PackageCatalog>("/api/app-package-catalog");
  const uiLabels = new Map(catalog.ui_modules.map((item) => [item.id, item.label]));
  const backendLabels = new Map(catalog.backend_components.map((item) => [item.id, item.label]));
  const form = document.createElement("section");
  form.className = "preview-section package-builder";
  form.innerHTML = "<h2>パッケージレシピ</h2>";

  const productProfiles = catalog.product_profiles ?? [];
  let selectedProfile: ProductProfile | undefined;
  const profileSection = document.createElement("section");
  profileSection.className = "package-builder-product-profile";
  const profileTitle = document.createElement("h3");
  profileTitle.textContent = "1. 製品プロファイルを選ぶ";
  const profileLead = document.createElement("p");
  profileLead.textContent = "標準製品は構成と受入条件を固定します。自由構成はCustom / Experimentalとしてのみ生成できます。";
  const profileLabel = document.createElement("label");
  profileLabel.textContent = "製品";
  const profileSelect = document.createElement("select");
  const customProfileOption = document.createElement("option");
  customProfileOption.value = "";
  customProfileOption.textContent = "Custom / Experimental（動作保証なし）";
  profileSelect.appendChild(customProfileOption);
  for (const profile of productProfiles) {
    const option = document.createElement("option");
    option.value = profile.metadata.id;
    option.textContent = `${profile.metadata.label} ${profile.metadata.version}（標準）`;
    profileSelect.appendChild(option);
  }
  const distributionModeLabel = document.createElement("label");
  distributionModeLabel.textContent = "配布形式";
  const distributionMode = document.createElement("select");
  for (const [value, label] of [
    ["split", "Runtime + Content Pack（推奨・重複保存なし）"],
    ["offline", "完全オフライン単体ZIP（大容量）"],
  ]) {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = label;
    distributionMode.appendChild(option);
  }
  profileLabel.appendChild(profileSelect);
  distributionModeLabel.appendChild(distributionMode);
  const profileControls = document.createElement("div");
  profileControls.className = "package-builder-profile-controls";
  profileControls.append(profileLabel, distributionModeLabel);
  const profileSummary = document.createElement("div");
  profileSummary.className = "package-builder-profile-summary";
  profileSection.append(profileTitle, profileLead, profileControls, profileSummary);
  form.appendChild(profileSection);

  const identity = document.createElement("div");
  identity.className = "package-builder-identity";
  const idLabel = document.createElement("label");
  idLabel.textContent = "パッケージID";
  const packageId = document.createElement("input");
  packageId.value = "my-spatial-app";
  packageId.required = true;
  packageId.pattern = "[a-z0-9][a-z0-9-]{1,47}";
  packageId.title = "英小文字または数字で始め、英小文字・数字・ハイフンのみ（2〜48文字）";
  const packageIdHint = document.createElement("small");
  packageIdHint.className = "package-builder-field-hint";
  packageIdHint.textContent = "配布ファイル名にそのまま使います。例: 20260806-jasa-room-spatial-app（_ は - に自動変換）";
  idLabel.append(packageId, packageIdHint);
  const nameLabel = document.createElement("label");
  nameLabel.textContent = "表示名";
  const packageLabel = document.createElement("input");
  packageLabel.value = packageId.value;
  packageLabel.required = true;
  packageLabel.maxLength = 80;
  nameLabel.append(packageLabel);
  identity.append(idLabel, nameLabel);
  form.appendChild(identity);

  let packageLabelEdited = false;
  packageLabel.addEventListener("input", () => {
    packageLabelEdited = true;
  });

  const syncDefaultPackageLabel = (): void => {
    if (!packageLabelEdited) packageLabel.value = packageId.value.trim();
  };

  const normalizeVisiblePackageId = (): boolean => {
    const before = packageId.value;
    const normalized = packageIdSlug(before);
    if (normalized && normalized !== before) {
      packageId.value = normalized;
      packageIdHint.textContent = `「${before}」をDocker配布用ID「${normalized}」へ整形しました。`;
      packageIdHint.classList.add("package-builder-field-hint-changed");
    }
    syncDefaultPackageLabel();
    const valid = PACKAGE_ID_PATTERN.test(packageId.value);
    packageId.setCustomValidity(valid ? "" : "英小文字または数字で始め、英小文字・数字・ハイフンだけを2〜48文字で入力してください。");
    return valid;
  };
  packageId.addEventListener("input", () => {
    syncDefaultPackageLabel();
    packageId.setCustomValidity("");
    packageIdHint.classList.remove("package-builder-field-hint-changed");
    packageIdHint.textContent = "配布ファイル名にそのまま使います。例: 20260806-jasa-room-spatial-app（_ は - に自動変換）";
  });
  packageId.addEventListener("blur", normalizeVisiblePackageId);

  const presetSection = document.createElement("section");
  presetSection.className = "package-builder-presets";
  const presetHeading = document.createElement("div");
  presetHeading.innerHTML = "<h3>用途から選ぶ</h3><p>よく使う構成は、必要なWeb画面とBackendをまとめて選択できます。</p>";
  const presetGrid = document.createElement("div");
  presetGrid.className = "package-builder-preset-grid";
  const viewerPresetActions = document.createElement("article");
  viewerPresetActions.className = "package-builder-viewer-preset package-builder-quick-start";
  const viewerPresetBody = document.createElement("div");
  const viewerPresetTitle = document.createElement("strong");
  viewerPresetTitle.textContent = "シンプルViewerだけを配布する";
  const simpleViewerPresetNote = document.createElement("small");
  simpleViewerPresetNote.textContent = "他のWeb画面と任意Backendを解除し、閲覧に必要な最小構成だけを選びます。Scene Packageはこの後1件選択します。";
  viewerPresetBody.append(viewerPresetTitle, simpleViewerPresetNote);
  const simpleViewerPreset = document.createElement("button");
  simpleViewerPreset.type = "button";
  simpleViewerPreset.className = "preview-primary";
  simpleViewerPreset.textContent = "シンプルViewer最小構成を選択";
  simpleViewerPreset.setAttribute("aria-pressed", "false");
  viewerPresetActions.append(viewerPresetBody, simpleViewerPreset);

  const analysisPresetActions = document.createElement("article");
  analysisPresetActions.className = "package-builder-viewer-preset package-builder-quick-start";
  const analysisPresetBody = document.createElement("div");
  const analysisPresetTitle = document.createElement("strong");
  analysisPresetTitle.textContent = "シンプルViewer＋登録画像AI解析";
  const analysisPresetNote = document.createElement("small");
  analysisPresetNote.textContent = "AI解析画面とシンプルViewerを同梱し、解析レコードから同じ撮影視点を3Dで開ける構成です。";
  analysisPresetBody.append(analysisPresetTitle, analysisPresetNote);
  const simpleAnalysisPreset = document.createElement("button");
  simpleAnalysisPreset.type = "button";
  simpleAnalysisPreset.className = "preview-primary";
  simpleAnalysisPreset.textContent = "Viewer＋AI解析を選択";
  simpleAnalysisPreset.setAttribute("aria-pressed", "false");
  analysisPresetActions.append(analysisPresetBody, simpleAnalysisPreset);

  presetGrid.append(viewerPresetActions, analysisPresetActions);
  presetSection.append(presetHeading, presetGrid);
  form.appendChild(presetSection);

  const grids = document.createElement("div");
  grids.className = "package-builder-grids";
  const uiColumn = document.createElement("div");
  uiColumn.innerHTML = "<h3>Web画面</h3><p>同じナビゲーション内に含める画面を選択します。</p>";
  const backendColumn = document.createElement("div");
  backendColumn.innerHTML = "<h3>Backend部品</h3><p>Web画面に必要な部品は自動選択されます。rosbridgeは任意同梱です。外部rosbridgeを使う場合や動的連携が不要な場合はOFFにできます。</p>";
  const uiInputs = new Map<string, HTMLInputElement>();
  const backendInputs = new Map<string, HTMLInputElement>();
  const scenePackageInputs = new Map<string, HTMLInputElement>();
  const scenePackageOptions = new Map<string, HTMLLabelElement>();
  const manuallySelectedBackends = new Set<string>();
  const uiCategorySections = createCategorySections(uiColumn, UI_COMPONENT_CATEGORIES);
  const backendCategorySections = createCategorySections(backendColumn, BACKEND_COMPONENT_CATEGORIES);

  for (const item of catalog.backend_components) {
    const flags = [item.adapter, item.api_feature ? `feature: ${item.api_feature}` : "独立service"];
    if (item.needs_gpu) flags.push("GPU");
    const option = checklistItem(item.label, flags.join(" / "), item.id);
    backendInputs.set(item.id, option.input);
    option.input.addEventListener("change", () => {
      if (option.input.checked) manuallySelectedBackends.add(item.id);
      else manuallySelectedBackends.delete(item.id);
    });
    appendCategorizedOption(
      item.id,
      option.root,
      BACKEND_COMPONENT_CATEGORIES,
      backendCategorySections.options,
    );
  }

  const syncBackendDependencies = (): void => {
    if (selectedProfile) {
      const selected = new Set(selectedProfile.composition.backend_components);
      for (const [backendId, input] of backendInputs) {
        input.checked = selected.has(backendId);
        input.disabled = true;
        input.closest(".package-builder-option")?.classList.toggle(
          "package-builder-option-required",
          input.checked,
        );
        input.title = "標準製品プロファイルで構成が固定されています";
      }
      return;
    }
    const required = new Set(
      catalog.ui_modules
        .filter((item) => uiInputs.get(item.id)?.checked === true)
        .flatMap((item) => item.required_backends),
    );
    const selectedScenePackageCount = [...scenePackageInputs.values()]
      .filter((input) => input.checked).length;
    for (const rule of catalog.conditional_backend_requirements ?? []) {
      const uiMatches = rule.all_ui_modules.every(
        (moduleId) => uiInputs.get(moduleId)?.checked === true,
      );
      if (uiMatches && selectedScenePackageCount >= rule.minimum_scene_packages) {
        for (const backendId of rule.required_backends) required.add(backendId);
      }
    }
    for (const [backendId, input] of backendInputs) {
      const isRequired = required.has(backendId);
      input.disabled = isRequired;
      input.checked = isRequired || manuallySelectedBackends.has(backendId);
      input.closest(".package-builder-option")?.classList.toggle(
        "package-builder-option-required",
        isRequired,
      );
      input.title = isRequired ? "選択中のWeb画面に必要なため自動選択されています" : "";
    }
  };

  for (const item of catalog.ui_modules) {
    const detail = `mode: ${item.preview_mode} / 必須: ${item.required_backends.join(", ")}`;
    const option = checklistItem(item.label, detail, item.id);
    uiInputs.set(item.id, option.input);
    option.input.addEventListener("change", syncBackendDependencies);
    appendCategorizedOption(
      item.id,
      option.root,
      UI_COMPONENT_CATEGORIES,
      uiCategorySections.options,
    );
  }
  removeEmptyCategorySections(uiCategorySections.sections);
  removeEmptyCategorySections(backendCategorySections.sections);
  grids.append(uiColumn, backendColumn);
  form.appendChild(grids);

  const sceneSection = document.createElement("div");
  sceneSection.className = "package-builder-scenes";
  sceneSection.innerHTML = "<h3>配布に含めるScene Package</h3><p>検証済みPackageだけを読み取り専用スナップショットとしてZIPへ含めます。登録画像AI解析を選ぶ場合は解析対象として1件以上、シンプルViewer単体では1件だけ選択してください。</p>";
  const sceneRequirement = document.createElement("p");
  sceneRequirement.className = "package-builder-scene-requirement";
  sceneRequirement.hidden = true;
  sceneSection.appendChild(sceneRequirement);
  const viewerVariantLabel = document.createElement("label");
  viewerVariantLabel.className = "package-builder-viewer-variant";
  const viewerVariantTitle = document.createElement("strong");
  viewerVariantTitle.textContent = "Scene Packageを開くViewer";
  const viewerVariant = document.createElement("select");
  const viewerVariants = catalog.viewer_variants ?? [
    { id: "full" as const, label: "多機能3DGS Viewer", description: "既存の全機能を含むViewer" },
    { id: "simple" as const, label: "シンプル3DGS Viewer", description: "AprilTagデバッグと移動・回転・寸法変更できる3D定規だけのViewer" },
  ];
  for (const item of viewerVariants) {
    const option = document.createElement("option");
    option.value = item.id;
    option.textContent = item.label;
    viewerVariant.appendChild(option);
  }
  const viewerVariantHelp = document.createElement("small");
  const updateViewerVariantHelp = (): void => {
    viewerVariantHelp.textContent = viewerVariants.find((item) => item.id === viewerVariant.value)?.description ?? "";
  };
  viewerVariant.addEventListener("change", updateViewerVariantHelp);
  updateViewerVariantHelp();
  viewerVariantLabel.append(viewerVariantTitle, viewerVariant, viewerVariantHelp);
  sceneSection.appendChild(viewerVariantLabel);
  const scenePackages = catalog.scene_packages ?? [];
  if (scenePackages.length === 0) {
    const empty = document.createElement("p");
    empty.className = "preview-note";
    empty.textContent = "同梱できる検証済みScene Packageはありません。先に3DGSデータ登録で検証・登録してください。";
    sceneSection.appendChild(empty);
  } else {
    const sceneToolbar = document.createElement("div");
    sceneToolbar.className = "package-builder-scene-toolbar";
    const sortLabel = document.createElement("label");
    sortLabel.textContent = "並び順";
    const sortSelect = document.createElement("select");
    sortSelect.setAttribute("aria-label", "Scene Packageの並び順");
    for (const [value, label] of [
      ["data-desc", "データ日：新しい順"],
      ["data-asc", "データ日：古い順"],
      ["registered-desc", "登録日時：新しい順"],
      ["name-asc", "名前：昇順"],
    ] as Array<[ScenePackageSortMode, string]>) {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = label;
      sortSelect.appendChild(option);
    }
    sortLabel.appendChild(sortSelect);
    const sceneCount = document.createElement("span");
    sceneCount.textContent = `${scenePackages.length}件の検証済みPackage`;
    sceneToolbar.append(sortLabel, sceneCount);
    sceneSection.appendChild(sceneToolbar);

    const sceneGrid = document.createElement("div");
    sceneGrid.className = "package-builder-scene-grid";
    for (const item of scenePackages) {
      const option = scenePackageOption(item);
      scenePackageInputs.set(item.scene_package_id, option.input);
      scenePackageOptions.set(item.scene_package_id, option.root);
      option.input.addEventListener("change", () => {
        const maximum = selectedProfile?.content.maximum_scene_packages
          ?? (uiInputs.get("simple-viewer")?.checked ? 1 : Number.POSITIVE_INFINITY);
        if (option.input.checked) {
          const checked = [...scenePackageInputs.values()].filter((input) => input.checked);
          for (const other of checked.slice(0, Math.max(0, checked.length - maximum))) {
            if (other !== option.input) other.checked = false;
          }
        }
        if (option.input.checked && uiInputs.get("simple-viewer")?.checked) {
          for (const [packageId, other] of scenePackageInputs) {
            if (packageId !== item.scene_package_id) other.checked = false;
          }
        }
        syncBackendDependencies();
      });
      sceneGrid.appendChild(option.root);
    }
    const renderScenePackageOrder = (): void => {
      const mode = sortSelect.value as ScenePackageSortMode;
      const sorted = [...scenePackages].sort((left, right) => compareScenePackages(left, right, mode));
      sceneGrid.replaceChildren(
        ...sorted.flatMap((item) => {
          const option = scenePackageOptions.get(item.scene_package_id);
          return option ? [option] : [];
        }),
      );
    };
    sortSelect.addEventListener("change", renderScenePackageOrder);
    renderScenePackageOrder();
    sceneSection.appendChild(sceneGrid);
  }
  form.appendChild(sceneSection);

  const overlaySection = document.createElement("div");
  overlaySection.className = "package-builder-analysis-overlay";
  overlaySection.hidden = true;
  const overlayLabel = document.createElement("label");
  const includeActiveAnalysis = document.createElement("input");
  includeActiveAnalysis.type = "checkbox";
  includeActiveAnalysis.checked = true;
  const overlayBody = document.createElement("span");
  const overlayTitle = document.createElement("strong");
  overlayTitle.textContent = "使用中のAI解析結果も同梱する";
  const overlayDescription = document.createElement("small");
  overlayDescription.textContent = "ON: 配布先ですぐ閲覧できます。OFF: 登録画像と人物マスクだけを含む軽量な再解析用パックです。";
  overlayBody.append(overlayTitle, overlayDescription);
  overlayLabel.append(includeActiveAnalysis, overlayBody);
  overlaySection.appendChild(overlayLabel);
  form.appendChild(overlaySection);

  const syncScenePackageRequirement = (): void => {
    const frameAnalysisSelected = uiInputs.get("frame-analysis")?.checked === true;
    const viewerSelected = uiInputs.get("viewer")?.checked === true;
    const selectedCount = [...scenePackageInputs.values()].filter((input) => input.checked).length;
    overlaySection.hidden = !frameAnalysisSelected;
    sceneRequirement.hidden = !frameAnalysisSelected && !viewerSelected;
    sceneRequirement.classList.toggle(
      "package-builder-scene-requirement-missing",
      (frameAnalysisSelected || viewerSelected) && selectedCount === 0,
    );
    if (frameAnalysisSelected) {
      sceneRequirement.textContent = selectedCount > 0
        ? `登録画像AI解析の対象を${selectedCount}件選択済みです。使用中の解析runがあれば、解析画像を含む配布専用overlayもZIPへ収録します。`
        : "登録画像AI解析にはScene Packageが必要です。下の一覧から解析対象を1件以上選択してください。";
    } else if (viewerSelected) {
      sceneRequirement.textContent = selectedCount > 0
        ? `多機能3Dシーン閲覧の対象を${selectedCount}件選択済みです。表示用の最小投影だけをZIPへ収録します。`
        : "多機能3Dシーン閲覧にはScene Packageが必要です。下の一覧から表示対象を1件以上選択してください。";
    }
  };
  for (const input of uiInputs.values()) {
    input.addEventListener("change", syncScenePackageRequirement);
  }
  for (const input of scenePackageInputs.values()) {
    input.addEventListener("change", syncScenePackageRequirement);
  }

  const syncScenePackageCompatibility = (): void => {
    const needsDynamicTracking =
      uiInputs.get("apriltag-check")?.checked === true
      && backendInputs.get("rosbridge")?.checked === true;
    for (const item of scenePackages) {
      const input = scenePackageInputs.get(item.scene_package_id);
      const root = scenePackageOptions.get(item.scene_package_id);
      if (!input || !root) continue;
      const missingProfileCapabilities = selectedProfile?.content.required_scene_capabilities.filter(
        (capability) => item.capabilities?.[capability] !== true,
      ) ?? [];
      const incompatible = (needsDynamicTracking && item.dynamic_tracking_compatible !== true)
        || missingProfileCapabilities.length > 0;
      if (incompatible) input.checked = false;
      input.disabled = incompatible;
      root.classList.toggle("package-builder-option-incompatible", incompatible);
      input.title = missingProfileCapabilities.length > 0
        ? `この標準製品に必要なScene capabilityが不足しています: ${missingProfileCapabilities.join(", ")}`
        : incompatible
          ? "このScene Packageは表示専用です。AprilTag→Viewer連携にはv1.2 Packageを選択してください"
          : "";
    }
  };
  uiInputs.get("apriltag-check")?.addEventListener("change", () => {
    syncScenePackageCompatibility();
    syncBackendDependencies();
  });
  backendInputs.get("rosbridge")?.addEventListener("change", () => {
    syncScenePackageCompatibility();
    syncBackendDependencies();
  });

  const simpleViewerInput = uiInputs.get("simple-viewer");
  const syncSimpleViewerSelection = (selectedByUser: boolean): void => {
    const simpleSelected = simpleViewerInput?.checked === true;
    const frameAnalysisSelected = uiInputs.get("frame-analysis")?.checked === true;
    const simpleOnlyPresetSelected = simpleSelected && !frameAnalysisSelected;
    const analysisPresetSelected = simpleSelected && frameAnalysisSelected;
    simpleViewerPreset.setAttribute("aria-pressed", simpleOnlyPresetSelected ? "true" : "false");
    simpleAnalysisPreset.setAttribute("aria-pressed", analysisPresetSelected ? "true" : "false");
    viewerPresetActions.classList.toggle("package-builder-quick-start-selected", simpleOnlyPresetSelected);
    analysisPresetActions.classList.toggle("package-builder-quick-start-selected", analysisPresetSelected);
    if (simpleSelected) {
      for (const [id, input] of uiInputs) {
        if (id !== "simple-viewer" && id !== "frame-analysis") input.checked = false;
      }
      const checkedPackages = [...scenePackageInputs.values()].filter((input) => input.checked);
      for (const input of checkedPackages.slice(1)) input.checked = false;
      viewerVariant.value = "simple";
      viewerVariant.disabled = true;
      if (selectedByUser) manuallySelectedBackends.clear();
    } else {
      viewerVariant.disabled = false;
      if (selectedByUser) viewerVariant.value = "full";
    }
    updateViewerVariantHelp();
    syncScenePackageCompatibility();
    syncBackendDependencies();
    syncScenePackageRequirement();
  };
  simpleViewerInput?.addEventListener("change", () => syncSimpleViewerSelection(true));
  for (const [id, input] of uiInputs) {
    if (id === "simple-viewer") continue;
    input.addEventListener("change", () => {
      if (input.checked && simpleViewerInput?.checked && id !== "frame-analysis") {
        simpleViewerInput.checked = false;
        viewerVariant.value = "full";
        syncSimpleViewerSelection(false);
      } else if (id === "frame-analysis" && simpleViewerInput?.checked) {
        syncSimpleViewerSelection(false);
      }
    });
  }

  const applyProductProfile = (): void => {
    selectedProfile = productProfiles.find(
      (profile) => profile.metadata.id === profileSelect.value,
    );
    const standard = selectedProfile !== undefined;
    presetSection.hidden = standard;
    if (standard && selectedProfile) {
      packageId.value = selectedProfile.metadata.id;
      packageLabel.value = selectedProfile.metadata.label;
      packageLabelEdited = false;
      distributionMode.value = selectedProfile.distribution.default_mode === "offline"
        ? "offline"
        : "split";
      for (const [id, input] of uiInputs) {
        input.checked = selectedProfile.composition.ui_modules.includes(id);
        input.disabled = true;
      }
      manuallySelectedBackends.clear();
      viewerVariant.value = selectedProfile.composition.viewer_variant;
      viewerVariant.disabled = true;
      includeActiveAnalysis.checked = selectedProfile.content.include_active_analysis;
      profileSummary.innerHTML = "";
      const support = document.createElement("strong");
      support.textContent = `${selectedProfile.metadata.label} / ${selectedProfile.metadata.tier} / candidate build`;
      const detail = document.createElement("p");
      detail.textContent = `必須機能: ${(selectedProfile.features.required ?? []).join("、")}。Scene Package: ${selectedProfile.content.minimum_scene_packages}〜${selectedProfile.content.maximum_scene_packages}件。Acceptance: ${selectedProfile.acceptance.suite}。`;
      const target = document.createElement("small");
      target.textContent = `検証対象: ${(selectedProfile.platforms.qualification_targets ?? []).join("、")}。qualified表示は別のAcceptance Record合格後だけです。`;
      profileSummary.append(support, detail, target);
    } else {
      for (const input of uiInputs.values()) input.disabled = false;
      viewerVariant.disabled = uiInputs.get("simple-viewer")?.checked === true;
      profileSummary.innerHTML = "<strong>Custom / Experimental</strong><p>構文と依存関係だけを検証します。Compatibility・Acceptance・サポート対象は保証しません。</p>";
    }
    updateViewerVariantHelp();
    syncScenePackageCompatibility();
    syncBackendDependencies();
    syncScenePackageRequirement();
  };
  profileSelect.addEventListener("change", applyProductProfile);

  simpleViewerPreset.addEventListener("click", () => {
    profileSelect.value = "";
    selectedProfile = undefined;
    applyProductProfile();
    for (const input of uiInputs.values()) input.checked = false;
    const simpleViewer = uiInputs.get("simple-viewer");
    if (simpleViewer) simpleViewer.checked = true;
    manuallySelectedBackends.clear();
    viewerVariant.value = "simple";
    syncSimpleViewerSelection(false);
    simpleViewerPresetNote.textContent = simpleViewer
      ? "シンプルViewer最小構成を選択しました。次に同梱するScene Packageを1件だけ選択してください。"
      : "シンプルViewerがカタログにないため、最小構成を選択できませんでした。";
    if (simpleViewer) sceneSection.scrollIntoView({ behavior: "smooth", block: "start" });
  });

  simpleAnalysisPreset.addEventListener("click", () => {
    profileSelect.value = "";
    selectedProfile = undefined;
    applyProductProfile();
    for (const input of uiInputs.values()) input.checked = false;
    const simpleViewer = uiInputs.get("simple-viewer");
    const frameAnalysis = uiInputs.get("frame-analysis");
    if (simpleViewer) simpleViewer.checked = true;
    if (frameAnalysis) frameAnalysis.checked = true;
    manuallySelectedBackends.clear();
    viewerVariant.value = "simple";
    syncSimpleViewerSelection(false);
    analysisPresetNote.textContent = simpleViewer && frameAnalysis
      ? "Viewer＋AI解析を選択しました。解析対象のScene Packageを1件選択してください。"
      : "必要な画面がカタログにないため、この構成を選択できませんでした。";
    if (simpleViewer && frameAnalysis) sceneSection.scrollIntoView({ behavior: "smooth", block: "start" });
  });

  profileSelect.value = productProfiles.some((item) => item.metadata.id === "viewer-lite")
    ? "viewer-lite"
    : "";
  applyProductProfile();

  const notice = document.createElement("p");
  notice.className = "preview-note";
  notice.textContent = "任意のDocker image・mount・commandは入力できません。標準構成はProduct Profileで固定し、Scene PackageはRuntimeと分離したContent Packへ一度だけ保存します。完全オフラインを選んだ場合だけ単体ZIPへ物理的に同梱します。";
  form.appendChild(notice);

  const buildPayload = (): Record<string, unknown> => {
    const payload: Record<string, unknown> = {
      package_id: packageId.value.trim(),
      label: packageLabel.value.trim(),
      ui_modules: [...uiInputs].filter(([, input]) => input.checked).map(([id]) => id),
      backend_components: [...backendInputs].filter(([, input]) => input.checked).map(([id]) => id),
      scene_package_ids: [...scenePackageInputs]
        .filter(([, input]) => input.checked)
        .map(([id]) => id),
      viewer_variant: viewerVariant.value === "simple" ? "simple" : "full",
      include_active_analysis: includeActiveAnalysis.checked,
      distribution_mode: distributionMode.value,
      release_lifecycle: selectedProfile ? "candidate" : "draft",
    };
    if (selectedProfile) payload.product_profile_id = selectedProfile.metadata.id;
    return payload;
  };

  const preflightSection = document.createElement("section");
  preflightSection.className = "package-builder-preflight";
  const preflightTitle = document.createElement("h3");
  preflightTitle.textContent = "生成前の容量確認";
  const preflightSummary = document.createElement("p");
  preflightSummary.textContent = "選択内容を確定すると、Scene Package・解析結果・作業コピー・最終ZIPを含む最大使用量を確認できます。";
  const preflightDetails = document.createElement("dl");
  const addPreflightDetail = (term: string, value: string): void => {
    const dt = document.createElement("dt");
    dt.textContent = term;
    const dd = document.createElement("dd");
    dd.textContent = value;
    preflightDetails.append(dt, dd);
  };
  preflightSection.append(preflightTitle, preflightSummary, preflightDetails);
  form.appendChild(preflightSection);

  const renderPreflight = (estimate: PackagePreflight): void => {
    preflightDetails.replaceChildren();
    addPreflightDetail("Scene Package", bytes(estimate.scene_package_bytes));
    addPreflightDetail(
      "使用中の解析結果",
      includeActiveAnalysis.checked
        ? `${estimate.analysis_overlay.run_count} run / ${estimate.analysis_overlay.frame_count}件 / ${bytes(estimate.analysis_overlay.total_bytes)}`
        : "同梱しない（配布先で再解析）",
    );
    addPreflightDetail("一時作業コピー", bytes(estimate.staging_copy_bytes));
    if (Number(estimate.content_pack_estimate_bytes ?? 0) > 0) {
      addPreflightDetail("共有Content Pack上限", bytes(estimate.content_pack_estimate_bytes));
    }
    addPreflightDetail("Runtime Bundle上限見積もり", bytes(estimate.estimated_zip_bytes));
    addPreflightDetail("生成中の最大追加使用量", bytes(estimate.peak_additional_bytes));
    addPreflightDetail("必要空き容量（保護領域込み）", bytes(estimate.required_free_bytes));
    addPreflightDetail("現在の空き容量", bytes(estimate.available_free_bytes));
    preflightSummary.textContent = estimate.can_build
      ? "容量確認済みです。この選択内容で生成できます。"
      : "空き容量が不足しています。解析結果の同梱を外すか、不要な成果物を整理してください。";
    preflightSection.classList.toggle("package-builder-preflight-ready", estimate.can_build);
    preflightSection.classList.toggle("package-builder-preflight-blocked", !estimate.can_build);
  };

  const runPreflight = async (): Promise<PackagePreflight> => {
    normalizeVisiblePackageId();
    const estimate = await api<PackagePreflight>("/api/app-packages/preflight", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(buildPayload()),
    });
    renderPreflight(estimate);
    return estimate;
  };

  const invalidatePreflight = (): void => {
    preflightDetails.replaceChildren();
    preflightSummary.textContent = "選択内容が変わりました。生成前に容量を再確認します。";
    preflightSection.classList.remove("package-builder-preflight-ready", "package-builder-preflight-blocked");
  };
  packageId.addEventListener("input", invalidatePreflight);
  packageLabel.addEventListener("input", invalidatePreflight);
  viewerVariant.addEventListener("change", invalidatePreflight);
  includeActiveAnalysis.addEventListener("change", invalidatePreflight);
  profileSelect.addEventListener("change", invalidatePreflight);
  distributionMode.addEventListener("change", invalidatePreflight);
  for (const input of [...uiInputs.values(), ...backendInputs.values(), ...scenePackageInputs.values()]) {
    input.addEventListener("change", invalidatePreflight);
  }

  const actions = document.createElement("div");
  actions.className = "preview-actions";
  const estimateButton = document.createElement("button");
  estimateButton.textContent = "容量を確認";
  estimateButton.type = "button";
  estimateButton.className = "preview-secondary";
  const create = document.createElement("button");
  create.textContent = "Docker ZIPを生成";
  create.type = "button";
  actions.append(estimateButton, create);
  form.appendChild(actions);

  const generationStatus = document.createElement("section");
  generationStatus.className = "package-builder-generation-status";
  generationStatus.hidden = true;
  generationStatus.setAttribute("role", "status");
  generationStatus.setAttribute("aria-live", "polite");
  const generationTitle = document.createElement("h3");
  const generationMessage = document.createElement("p");
  const generationProgress = document.createElement("progress");
  generationProgress.removeAttribute("value");
  const generationDetails = document.createElement("p");
  generationDetails.className = "package-builder-generation-details";
  const generationTechnical = document.createElement("details");
  generationTechnical.hidden = true;
  const generationTechnicalSummary = document.createElement("summary");
  generationTechnicalSummary.textContent = "技術情報";
  const generationTechnicalCode = document.createElement("code");
  generationTechnical.append(generationTechnicalSummary, generationTechnicalCode);
  const generationActions = document.createElement("div");
  generationActions.className = "preview-actions";
  const cancelGeneration = document.createElement("button");
  cancelGeneration.type = "button";
  cancelGeneration.className = "preview-secondary";
  cancelGeneration.textContent = "生成を中止";
  cancelGeneration.hidden = true;
  const retryGeneration = document.createElement("button");
  retryGeneration.type = "button";
  retryGeneration.className = "preview-secondary";
  retryGeneration.textContent = "同じ内容で再実行";
  retryGeneration.hidden = true;
  generationActions.append(cancelGeneration, retryGeneration);
  generationStatus.append(
    generationTitle,
    generationMessage,
    generationProgress,
    generationDetails,
    generationTechnical,
    generationActions,
  );
  form.appendChild(generationStatus);
  const workspace = document.createElement("div");
  workspace.className = "package-builder-workspace";
  workspace.appendChild(form);
  host.appendChild(workspace);

  const outputs = document.createElement("section");
  outputs.className = "preview-section package-builder-outputs";
  outputs.innerHTML = "<h2>生成済みDocker ZIP</h2><p>成果物は <code>data/scene_repository/deliverables/applications/</code> に保存されます。ZIP内の <code>SETUP.md</code> に起動・停止手順があります。</p>";
  const outputToolbar = document.createElement("div");
  outputToolbar.className = "package-builder-output-toolbar";
  const outputCount = document.createElement("strong");
  const refresh = document.createElement("button");
  refresh.type = "button";
  refresh.className = "preview-secondary";
  refresh.textContent = "成果物一覧を更新";
  outputToolbar.append(outputCount, refresh);
  const outputList = document.createElement("div");
  outputs.append(outputToolbar, outputList);
  workspace.appendChild(outputs);

  const refreshOutputs = async (): Promise<void> => {
    outputList.replaceChildren();
    outputCount.textContent = "読込中…";
    const packages = await api<BuiltPackage[]>("/api/app-packages");
    const totalBytes = packages.reduce(
      (sum, item) => sum + (Number.isFinite(Number(item.size_bytes)) ? Number(item.size_bytes) : 0),
      0,
    );
    outputCount.textContent = `${packages.length}件 / ${bytes(totalBytes)}`;
    if (packages.length === 0) {
      outputList.textContent = "まだパッケージは生成されていません。";
      return;
    }
    for (const item of packages) {
      const row = document.createElement("div");
      row.className = "package-builder-output";
      const header = document.createElement("div");
      header.className = "package-builder-output-header";
      const title = document.createElement("strong");
      title.textContent = String(item.label ?? item.application_id ?? item.filename ?? "Docker ZIP");
      const badge = document.createElement("span");
      badge.className = "package-builder-output-badge";
      badge.textContent = "生成完了";
      header.append(title, badge);

      const filename = document.createElement("code");
      filename.className = "package-builder-output-filename";
      filename.textContent = String(item.filename ?? "package.zip");

      const meta = document.createElement("dl");
      meta.className = "package-builder-output-meta";
      const addMeta = (term: string, description: string): void => {
        const dt = document.createElement("dt");
        dt.textContent = term;
        const dd = document.createElement("dd");
        dd.textContent = description;
        meta.append(dt, dd);
      };
      addMeta("作成日時", formatDate(item.created_at));
      addMeta("サイズ", bytes(item.size_bytes));
      if (item.product_release?.release_id) {
        addMeta(
          "Product Release",
          `${item.product_release.release_id} / ${item.product_release.lifecycle ?? "不明"} / ${item.product_release.support_level ?? "不明"}`,
        );
      }
      addMeta("Web画面", displayIds(item.ui_modules, uiLabels));
      addMeta("Backend", displayIds(item.backend_components, backendLabels));
      addMeta("Scene Package", Array.isArray(item.scene_package_ids) && item.scene_package_ids.length > 0
        ? item.scene_package_ids.join("、")
        : "同梱なし");
      const overlayRuns = Array.isArray(item.analysis_overlay?.runs) ? item.analysis_overlay.runs : [];
      if (item.ui_modules?.includes("frame-analysis")) {
        const overlayFrames = overlayRuns.reduce(
          (sum, run) => sum + (Number.isFinite(Number(run.frame_count)) ? Number(run.frame_count) : 0),
          0,
        );
        addMeta("解析結果overlay", overlayRuns.length > 0
          ? `${overlayRuns.length} run / ${overlayFrames}件を同梱`
          : "使用中runなし（配布先で新規解析）");
      }
      addMeta("Viewer", item.viewer_variant === "simple" ? "シンプル3DGS Viewer" : "多機能3DGS Viewer");
      addMeta("セットアップ", item.setup_included === false ? "SETUP.mdを確認できません" : "SETUP.md 同梱");

      const path = String(item.output_path ?? `data/scene_repository/deliverables/applications/${String(item.filename ?? "package.zip")}`);
      const outputPath = document.createElement("div");
      outputPath.className = "package-builder-output-path";
      const outputPathLabel = document.createElement("span");
      outputPathLabel.textContent = "保存先";
      const outputPathCode = document.createElement("code");
      outputPathCode.textContent = path;
      outputPath.append(outputPathLabel, outputPathCode);

      const rowActions = document.createElement("div");
      rowActions.className = "package-builder-output-actions";
      const download = document.createElement("a");
      download.textContent = "ZIPをダウンロード";
      download.className = "preview-secondary";
      download.href = previewApiUrl(String(item.download_url ?? ""));
      download.setAttribute("download", String(item.filename ?? "package.zip"));
      const copyPath = document.createElement("button");
      copyPath.type = "button";
      copyPath.className = "preview-secondary";
      copyPath.textContent = "保存先をコピー";
      copyPath.addEventListener("click", () => {
        void navigator.clipboard.writeText(path).then(() => {
          copyPath.textContent = "コピーしました";
          window.setTimeout(() => { copyPath.textContent = "保存先をコピー"; }, 1500);
        }).catch(() => {
          copyPath.textContent = "コピーできませんでした";
        });
      });
      rowActions.append(download, copyPath);
      const contentPacks = item.content_delivery?.content_packs ?? [];
      for (const contentPack of contentPacks) {
        if (!contentPack.download_url) continue;
        const contentDownload = document.createElement("a");
        contentDownload.className = "preview-secondary";
        contentDownload.textContent = `Content Packを取得 (${bytes(contentPack.size_bytes)})`;
        contentDownload.href = previewApiUrl(contentPack.download_url);
        contentDownload.setAttribute("download", contentPack.filename ?? `${contentPack.sha256}.zip`);
        rowActions.appendChild(contentDownload);
      }
      row.append(header, filename, meta, outputPath, rowActions);
      outputList.appendChild(row);
    }
  };

  refresh.addEventListener("click", () => { void refreshOutputs(); });
  estimateButton.addEventListener("click", () => {
    estimateButton.disabled = true;
    estimateButton.textContent = "確認中…";
    void runPreflight().catch((error) => {
      const failure = friendlyBuildError(error);
      preflightSummary.textContent = failure.message;
    }).finally(() => {
      estimateButton.disabled = false;
      estimateButton.textContent = "容量を確認";
    });
  });

  let activeJobId: string | null = null;
  const waitForNextPoll = (): Promise<void> => new Promise((resolve) => window.setTimeout(resolve, 750));
  const monitorJob = async (initial: PackageBuildJob): Promise<void> => {
    let job = initial;
    activeJobId = job.job_id;
    create.disabled = true;
    create.textContent = "バックグラウンド生成中";
    generationStatus.hidden = false;
    generationStatus.className = "package-builder-generation-status package-builder-generation-running";
    generationProgress.hidden = false;
    generationProgress.max = 100;
    cancelGeneration.hidden = false;
    retryGeneration.hidden = true;
    generationTechnical.hidden = true;
    while (job.status === "queued" || job.status === "running") {
      const percent = Math.max(0, Math.min(100, Number(job.progress_percent ?? 0)));
      generationProgress.value = percent;
      generationTitle.textContent = job.status === "queued" ? "生成待ちです" : "Docker ZIPを生成しています";
      generationMessage.textContent = String(job.message ?? "処理中です");
      generationDetails.textContent = `${percent}% / ${String(job.stage ?? "準備中")}`;
      await waitForNextPoll();
      job = await api<PackageBuildJob>(`/api/app-package-jobs/${job.job_id}`);
    }
    activeJobId = null;
    cancelGeneration.hidden = true;
    create.disabled = false;
    create.textContent = "Docker ZIPを生成";
    if (job.status === "completed" && job.result) {
      generationStatus.className = "package-builder-generation-status package-builder-generation-success";
      generationTitle.textContent = "Docker ZIPの生成が完了しました";
      generationMessage.textContent = `${String(job.result.filename ?? "ZIP")}（${bytes(job.result.size_bytes)}）`;
      generationProgress.value = 100;
      generationDetails.textContent = `保存先: ${String(job.result.output_path ?? "data/scene_repository/deliverables/applications/")} / SETUP.md同梱`;
      await refreshOutputs();
      return;
    }
    generationStatus.className = "package-builder-generation-status package-builder-generation-failed";
    generationTitle.textContent = job.status === "cancelled" ? "生成を中止しました" : "Docker ZIPを生成できませんでした";
    generationMessage.textContent = String(job.message ?? job.failure_reason ?? "生成処理が終了しました");
    generationProgress.hidden = true;
    generationDetails.textContent = "完成ZIPは公開されていません。入力データは変更されていません。";
    generationTechnical.hidden = !job.failure_reason;
    generationTechnicalCode.textContent = String(job.failure_reason ?? "");
    retryGeneration.hidden = job.can_retry !== true;
    retryGeneration.dataset.jobId = job.job_id;
  };

  cancelGeneration.addEventListener("click", () => {
    if (!activeJobId) return;
    cancelGeneration.disabled = true;
    cancelGeneration.textContent = "停止要求済み";
    void api<PackageBuildJob>(`/api/app-package-jobs/${activeJobId}/cancel`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    }).finally(() => {
      cancelGeneration.disabled = false;
      cancelGeneration.textContent = "生成を中止";
    });
  });

  retryGeneration.addEventListener("click", () => {
    const jobId = retryGeneration.dataset.jobId;
    if (!jobId) return;
    retryGeneration.hidden = true;
    void api<PackageBuildJob>(`/api/app-package-jobs/${jobId}/retry`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    }).then(monitorJob).catch((error) => {
      generationMessage.textContent = friendlyBuildError(error).message;
      retryGeneration.hidden = false;
    });
  });

  create.addEventListener("click", () => {
    normalizeVisiblePackageId();
    if (!packageId.reportValidity() || !packageLabel.reportValidity()) return;
    const payload = buildPayload();
    const selectedUi = payload.ui_modules as string[];
    const selectedPackages = payload.scene_package_ids as string[];
    if (selectedUi.includes("frame-analysis") && selectedPackages.length === 0) {
      syncScenePackageRequirement();
      sceneSection.scrollIntoView({ behavior: "smooth", block: "center" });
      [...scenePackageInputs.values()][0]?.focus();
      return;
    }
    create.disabled = true;
    create.textContent = "容量確認中…";
    void runPreflight().then((estimate) => {
      if (!estimate.can_build) throw new Error("insufficient_storage:create_app_package");
      return api<PackageBuildJob>("/api/app-packages", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
    }).then(monitorJob).catch((error) => {
      const failure = friendlyBuildError(error);
      generationStatus.hidden = false;
      generationStatus.className = "package-builder-generation-status package-builder-generation-failed";
      generationTitle.textContent = "Docker ZIPを生成できませんでした";
      generationMessage.textContent = failure.message;
      generationProgress.hidden = true;
      generationTechnical.hidden = false;
      generationTechnicalCode.textContent = failure.technical;
      create.disabled = false;
      create.textContent = "Docker ZIPを生成";
    });
  });

  await refreshOutputs();
}
