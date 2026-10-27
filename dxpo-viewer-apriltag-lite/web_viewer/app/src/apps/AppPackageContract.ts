export type SpatialAppId =
  | "suite"
  | "colmap"
  | "apriltag-check"
  | "spz-import"
  | "scene-manager"
  | "project-manager"
  | "package-builder"
  | "field-memory"
  | "frame-analysis"
  | "site-insights"
  | "viewer";

export type PreviewMode = "pipeline" | "tag-check" | "packages" | "spatial" | "projects" | "package-builder" | "field-memory" | "frame-analysis" | "site-insights";

export type SpatialAppCategoryId = "create" | "manage" | "operate" | "analyze";
export type SpatialAppAudience = "basic" | "operator" | "administrator" | "advanced";

export interface SpatialAppCategoryDefinition {
  id: SpatialAppCategoryId;
  label: string;
  description: string;
}

export interface SpatialAppDefinition {
  id: SpatialAppId;
  label: string;
  description: string;
  useWhen: string;
  category?: SpatialAppCategoryId;
  audience: SpatialAppAudience;
  docsPath: string;
  previewMode?: PreviewMode;
  apiFeature?: string;
  needsApi: boolean;
  needsGpu: boolean;
  needsCamera: boolean;
  supportsRosbridge: boolean;
  canonicalWrites: readonly string[];
}

export const SPATIAL_APP_CATEGORIES: readonly SpatialAppCategoryDefinition[] = [
  { id: "create", label: "3Dデータを作る", description: "撮影データの確認からCOLMAP処理、SPZ登録までを行います。" },
  { id: "manage", label: "データを管理・配布する", description: "処理履歴、Scene Package、配布物を安全に管理します。" },
  { id: "operate", label: "現場で見る・記録する", description: "3Dシーンを閲覧し、現場・設備の情報を記録します。" },
  { id: "analyze", label: "検索・AI・変化を確認する", description: "登録済みデータを検索、解析、比較します。" },
];

export const SPATIAL_APP_ORDER: readonly SpatialAppId[] = [
  "colmap", "apriltag-check", "spz-import",
  "scene-manager", "project-manager", "package-builder",
  "viewer", "field-memory",
  "frame-analysis", "site-insights",
];

export const PREVIEW_MODE_APP_IDS: Readonly<Record<PreviewMode, SpatialAppId>> = {
  pipeline: "colmap",
  "tag-check": "apriltag-check",
  spatial: "spz-import",
  packages: "scene-manager",
  projects: "project-manager",
  "package-builder": "package-builder",
  "field-memory": "field-memory",
  "frame-analysis": "frame-analysis",
  "site-insights": "site-insights",
};

export const SPATIAL_APP_DEFINITIONS: Readonly<Record<SpatialAppId, SpatialAppDefinition>> = {
  suite: {
    id: "suite",
    label: "Spatial 3DGS 統合TOP",
    description: "すべてのアプリを目的別に選んで利用する統合入口です。",
    useWhen: "通常運用を開始するとき",
    audience: "basic",
    docsPath: "docs/apps/suite/README.md",
    apiFeature: "suite",
    needsApi: true,
    needsGpu: true,
    needsCamera: true,
    supportsRosbridge: true,
    canonicalWrites: ["metric_reconstruction_packages", "scene_packages"],
  },
  colmap: {
    id: "colmap",
    label: "3D再構成（COLMAP）",
    description: "動画から人物を除外した登録画像とCOLMAP・MRPを作成します。",
    useWhen: "新しい撮影データを3D再構成するとき",
    category: "create",
    audience: "basic",
    docsPath: "docs/apps/colmap/README.md",
    previewMode: "pipeline",
    apiFeature: "pipeline",
    needsApi: true,
    needsGpu: true,
    needsCamera: false,
    supportsRosbridge: false,
    canonicalWrites: ["metric_reconstruction_packages"],
  },
  "apriltag-check": {
    id: "apriltag-check",
    label: "AprilTagカメラ確認",
    description: "カメラ映像でAprilTagの検出、ID、位置・姿勢を確認します。",
    useWhen: "撮影前にTagとカメラ設定を確認するとき",
    category: "create",
    audience: "operator",
    docsPath: "docs/apps/apriltag-check/README.md",
    previewMode: "tag-check",
    apiFeature: "apriltag-check",
    needsApi: true,
    needsGpu: false,
    needsCamera: true,
    supportsRosbridge: true,
    canonicalWrites: [],
  },
  "spz-import": {
    id: "spz-import",
    label: "3DGSデータ登録",
    description: "LichtFeld StudioのSPZとMRPを対応付け、Scene Packageへ登録します。",
    useWhen: "学習済みSPZをシステムへ取り込むとき",
    category: "create",
    audience: "basic",
    docsPath: "docs/apps/spz-import/README.md",
    previewMode: "spatial",
    apiFeature: "spz-import",
    needsApi: true,
    needsGpu: false,
    needsCamera: false,
    supportsRosbridge: false,
    canonicalWrites: ["scene_packages"],
  },
  "scene-manager": {
    id: "scene-manager",
    label: "シーンデータ管理",
    description: "Scene Package、MRP、入力原本、台帳、配布物を一覧・整理します。",
    useWhen: "データの状態確認、選択削除、旧式データ整理をするとき",
    category: "manage",
    audience: "administrator",
    docsPath: "docs/apps/scene-manager/README.md",
    previewMode: "packages",
    apiFeature: "scene-manager",
    needsApi: true,
    needsGpu: false,
    needsCamera: false,
    supportsRosbridge: false,
    canonicalWrites: ["repository_items:recoverable-quarantine-only"],
  },
  "project-manager": {
    id: "project-manager",
    label: "処理履歴・成果物管理",
    description: "COLMAP処理、MRP、Scene Packageの対応と容量を確認します。",
    useWhen: "処理結果や失敗workspaceを追跡するとき",
    category: "manage",
    audience: "operator",
    docsPath: "docs/apps/project-manager/README.md",
    previewMode: "projects",
    apiFeature: "project-manager",
    needsApi: true,
    needsGpu: false,
    needsCamera: false,
    supportsRosbridge: false,
    canonicalWrites: [],
  },
  "package-builder": {
    id: "package-builder",
    label: "配布アプリ作成",
    description: "必要な画面とScene Packageを選び、配布用Docker ZIPを作成します。",
    useWhen: "用途を限定したアプリを別環境へ渡すとき",
    category: "manage",
    audience: "administrator",
    docsPath: "docs/apps/package-builder/README.md",
    previewMode: "package-builder",
    apiFeature: "package-builder",
    needsApi: true,
    needsGpu: false,
    needsCamera: false,
    supportsRosbridge: false,
    canonicalWrites: [],
  },
  "field-memory": {
    id: "field-memory",
    label: "現場・設備台帳",
    description: "現場、設備、観測、文書、保全履歴をSceneVersionへ関連付けます。",
    useWhen: "現場情報や設備履歴を記録するとき",
    category: "operate",
    audience: "operator",
    docsPath: "docs/apps/field-memory/README.md",
    previewMode: "field-memory",
    apiFeature: "field-memory",
    needsApi: true,
    needsGpu: false,
    needsCamera: false,
    supportsRosbridge: false,
    canonicalWrites: ["field_memory"],
  },
  "frame-analysis": {
    id: "frame-analysis",
    label: "登録画像AI解析",
    description: "登録画像を、3D視点・構造化観測・人の確認を持つ検索可能な現場観測へ変換します。",
    useWhen: "画像から現場の設備・状態を探せる観測DBを作るとき",
    category: "analyze",
    audience: "advanced",
    docsPath: "docs/apps/frame-analysis/README.md",
    previewMode: "frame-analysis",
    apiFeature: "frame-analysis",
    needsApi: true,
    needsGpu: true,
    needsCamera: false,
    supportsRosbridge: false,
    canonicalWrites: ["field_memory"],
  },
  "site-insights": {
    id: "site-insights",
    label: "現場インサイト",
    description: "同じ現場の記録を検索し、根拠付きで質問し、SceneVersion間の変化を確認・確定します。",
    useWhen: "現場データから判断材料を探し、根拠と変化を一続きで確認するとき",
    category: "analyze",
    audience: "operator",
    docsPath: "docs/apps/site-insights/README.md",
    previewMode: "site-insights",
    apiFeature: "site-insights",
    needsApi: true,
    needsGpu: true,
    needsCamera: false,
    supportsRosbridge: false,
    canonicalWrites: ["field_memory"],
  },
  viewer: {
    id: "viewer",
    label: "3Dシーン閲覧",
    description: "公開済みScene Packageを選び、3DGS空間と登録視点を表示します。",
    useWhen: "3Dシーンを見て計測・配置確認するとき",
    category: "operate",
    audience: "basic",
    docsPath: "docs/apps/viewer/README.md",
    previewMode: "packages",
    needsApi: false,
    needsGpu: false,
    needsCamera: false,
    supportsRosbridge: true,
    canonicalWrites: [],
  },
};

export function spatialAppDefinition(value: string): SpatialAppDefinition {
  if (!(value in SPATIAL_APP_DEFINITIONS)) throw new Error(`unknown_spatial_app:${value}`);
  return SPATIAL_APP_DEFINITIONS[value as SpatialAppId];
}
