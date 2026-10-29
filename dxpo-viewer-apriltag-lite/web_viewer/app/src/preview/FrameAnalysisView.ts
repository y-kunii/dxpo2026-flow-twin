import { api } from "./PreviewApiClient";
import { button, field, section, selectField } from "./PreviewUi";
import { openViewer, resolveViewerUrl } from "./ViewerNavigation";
import {
  saveFrameAnalysisReturnState,
  takeFrameAnalysisReturnState,
} from "./FrameAnalysisNavigation";

interface SceneVersion {
  scene_version_id: string;
  scene_package_id: string;
  site_id: string;
  site_name: string;
  label: string;
  analysis_ready?: boolean;
  analysis_readiness_reason?: string | null;
}

interface ScenePackageTarget extends SceneVersion {
  scene_version_count: number;
}

interface Pose {
  position: { x: number; y: number; z: number };
  rotation: { x: number; y: number; z: number; w: number };
}

interface FrameAnalysis {
  frame_analysis_id?: string;
  record_id?: string;
  analysis_run_id?: string;
  scene_version_id?: string;
  scene_package_id?: string;
  image_name?: string;
  title?: string;
  summary: string;
  keywords: string[];
  thumbnail_url: string;
  image_url?: string;
  viewer_url?: string | null;
  source_time_sec: number;
  source_frame_index?: number;
  camera_id?: string;
  timestamp?: string;
  pose?: Pose;
  focus?: { x: number; y: number; z: number };
  projection?: Record<string, unknown>;
  model_summary?: string;
  extra?: FrameObservation;
  review_status?: "unreviewed" | "accepted" | "corrected" | "rejected";
  review?: {
    review_event_id: string;
    decision: "accepted" | "corrected" | "rejected";
    reason?: string;
    actor_id?: string;
    created_at?: string;
  } | null;
}

interface ObservationItem {
  label?: string;
  category?: string;
  state?: string;
  subject?: string;
  observation?: string;
  evidence?: string;
  severity?: "info" | "attention" | "critical";
  confidence?: number;
  text?: string;
  context?: string;
  identifier_text?: string;
  condition?: string;
  language?: string;
  legibility?: "clear" | "partial" | "unreadable";
}

interface FrameObservation {
  schema_version?: string;
  area?: { label?: string; confidence?: number };
  visible_assets?: ObservationItem[];
  conditions?: ObservationItem[];
  safety_observations?: ObservationItem[];
  visible_text?: ObservationItem[];
  assets?: ObservationItem[];
  inspection_observations?: ObservationItem[];
  text_regions?: ObservationItem[];
  signage?: ObservationItem[];
  caption?: string;
  scene_type?: string;
  main_subjects?: string[];
  image_quality?: {
    usable?: boolean;
    visibility?: "good" | "partial" | "poor";
    blur?: "none" | "mild" | "severe";
    occlusion?: "none" | "partial" | "heavy";
  };
  limitations?: string[];
}

interface AnalysisRun {
  analysis_run_id: string;
  scene_version_id: string;
  model_id: string;
  model_revision: string;
  prompt_version: string;
  analysis_policy_id?: string;
  output_schema_version?: string | null;
  interval_sec: number;
  similarity_threshold: number;
  status: "queued" | "running" | "completed" | "failed" | "cancelled";
  output_count: number;
  target_count?: number | null;
  progress_percent?: number | null;
  failure_reason?: string | null;
  is_active: boolean;
  created_at: string;
  parameters?: Record<string, unknown>;
  frames?: FrameAnalysis[];
}

interface FrameSearchResponse {
  count: number;
  total_count: number;
  offset: number;
  limit: number;
  results: FrameAnalysis[];
}

interface AnalysisQualitySummary {
  analysis_run_id: string;
  analysis_policy_id: string;
  output_schema_version: string | null;
  total_records: number;
  structured_records: number;
  usable_records: number;
  limited_records: number;
  attention_records: number;
  critical_records: number;
  unreviewed_records: number;
  accepted_records: number;
  corrected_records: number;
  rejected_records: number;
  ready_for_downstream_records: number;
}

interface VlmModelStatus {
  model_id: string;
  display_name?: string;
  revision: string;
  license: string;
  source_provider?: string;
  source_url?: string;
  storage_directory?: string;
  download_contents?: string[];
  parameter_scale: string;
  approximate_download_gib: number;
  minimum_vram_gib: number;
  recommended_vram_gib: number;
  quality_tier: string;
  installed: boolean;
  status: "not_installed" | "queued" | "downloading" | "installed" | "failed";
  error?: string | null;
}

interface VramRecommendation {
  vram_gib: number;
  model_id: string;
  label: string;
  note: string;
}

interface VlmModelCatalog {
  schema_version: string;
  default_model_id: string;
  vram_estimate_notice: string;
  vram_recommendations: VramRecommendation[];
  models: VlmModelStatus[];
}

interface AnalysisPolicy {
  analysis_policy_id: string;
  label: string;
  description: string;
  use_when: string;
  prompt_template_id: string;
  prompt_version: string;
  prompt_template_sha256: string;
  output_schema_version: string;
  downstream_uses: string[];
  output_fields: string[];
  review_required: boolean;
}

interface AnalysisPolicyCatalog {
  default_analysis_policy_id: string;
  policies: AnalysisPolicy[];
}

interface AnalysisResetPreflight {
  status: "ready" | "blocked";
  can_reset: boolean;
  scene_version_id: string;
  scene_package_id: string;
  site_id: string;
  state_token: string;
  confirmation_phrase: string;
  blockers: string[];
  attachments_retained: boolean;
  counts: {
    analysis_runs: number;
    active_analysis_runs: number;
    frame_analyses: number;
    legacy_frame_analyses: number;
    review_events: number;
    external_review_inheritance_refs: number;
    observation_sets: number;
    observation_items: number;
    activation_events: number;
    active_evidence_inquiries: number;
    evidence_citations: number;
  };
}

interface AnalysisResetResult {
  status: "reset";
  scene_version_id: string;
  scene_package_id: string;
  deleted: AnalysisResetPreflight["counts"];
  backup_uri: string;
  attachments_retained: boolean;
  audit_event_id: string;
  reset_at: string;
}

const FRAME_SEARCH_PAGE_SIZE = 100;

function value(host: ParentNode, name: string): string {
  return host.querySelector<HTMLInputElement | HTMLSelectElement>(`[data-field="${name}"]`)?.value.trim() ?? "";
}

function statusLine(): HTMLParagraphElement {
  const result = document.createElement("p");
  result.className = "preview-help";
  result.setAttribute("role", "status");
  result.setAttribute("aria-live", "polite");
  return result;
}

function finite(value: unknown, digits = 3): string {
  return typeof value === "number" && Number.isFinite(value) ? value.toFixed(digits) : "—";
}

function dataRow(label: string, content: string): HTMLDivElement {
  const row = document.createElement("div");
  const key = document.createElement("dt");
  const description = document.createElement("dd");
  key.textContent = label;
  description.textContent = content || "—";
  row.append(key, description);
  return row;
}

function reviewLabel(status: FrameAnalysis["review_status"]): string {
  return {
    accepted: "確認済み",
    corrected: "修正して採用",
    rejected: "対象外",
    unreviewed: "AI生成・未確認",
  }[status ?? "unreviewed"];
}

function confidenceLabel(value: number | undefined): string {
  return typeof value === "number" && Number.isFinite(value)
    ? `確信度 ${Math.round(value * 100)}%`
    : "確信度未記録";
}

function scenePackageTargets(scenes: SceneVersion[]): ScenePackageTarget[] {
  const targets = new Map<string, ScenePackageTarget>();
  for (const scene of scenes) {
    const current = targets.get(scene.scene_package_id);
    if (current) {
      current.scene_version_count += 1;
      continue;
    }
    targets.set(scene.scene_package_id, { ...scene, scene_version_count: 1 });
  }
  return [...targets.values()];
}

function frameRecordListItem(
  frame: FrameAnalysis,
  databaseRecord: boolean,
  beforeViewerOpen: () => void,
  onReview?: (
    frame: FrameAnalysis,
    payload: {
      decision: "accepted" | "corrected" | "rejected";
      summary?: string;
      keywords?: string[];
      extra?: FrameObservation;
      reason?: string;
    },
  ) => Promise<void>,
): HTMLElement {
  const record = document.createElement("article");
  record.className = "frame-analysis-record";
  record.setAttribute("role", "listitem");
  const frameId = frame.frame_analysis_id ?? frame.record_id ?? "";
  const titleText = frame.image_name ?? frame.title ?? frameId;
  const position = frame.pose?.position ?? frame.focus;

  let thumbnail: HTMLElement | null = null;
  if (frame.thumbnail_url) {
    const image = document.createElement("img");
    image.src = frame.thumbnail_url;
    image.alt = titleText;
    image.loading = "lazy";
    if (frame.viewer_url) {
      const imageLink = document.createElement("a");
      imageLink.className = "frame-analysis-record-thumbnail";
      imageLink.href = resolveViewerUrl(frame.viewer_url);
      imageLink.target = "_blank";
      imageLink.rel = "noopener";
      imageLink.title = "この画像の撮影視点を3Dシーンで開く";
      imageLink.addEventListener("click", beforeViewerOpen);
      imageLink.appendChild(image);
      thumbnail = imageLink;
    } else {
      const imageFrame = document.createElement("div");
      imageFrame.className = "frame-analysis-record-thumbnail";
      imageFrame.appendChild(image);
      thumbnail = imageFrame;
    }
    record.classList.add("has-thumbnail");
  }

  const content = document.createElement("div");
  content.className = "frame-analysis-record-content";

  const heading = document.createElement("h3");
  heading.textContent = titleText;
  const reviewStatus = frame.review_status ?? "unreviewed";
  const reviewBadge = document.createElement("span");
  reviewBadge.className = `frame-analysis-review-badge is-${reviewStatus}`;
  reviewBadge.textContent = reviewLabel(reviewStatus);
  heading.append(" ", reviewBadge);
  const meta = document.createElement("div");
  meta.className = "frame-analysis-record-meta";
  const time = document.createElement("span");
  time.textContent = `撮影 ${finite(frame.source_time_sec, 2)} 秒`;
  meta.appendChild(time);
  if (frame.source_frame_index !== undefined) {
    const sourceFrame = document.createElement("span");
    sourceFrame.textContent = `フレーム #${frame.source_frame_index}`;
    meta.appendChild(sourceFrame);
  }
  if (frame.camera_id) {
    const camera = document.createElement("span");
    camera.textContent = `仮想カメラ ${frame.camera_id}`;
    meta.appendChild(camera);
  }
  const summary = document.createElement("p");
  summary.className = "frame-analysis-summary";
  summary.textContent = frame.summary || "要約はありません。";
  content.append(heading, meta, summary);

  const observation = frame.extra;
  if (observation?.schema_version && observation.schema_version !== "legacy-caption") {
    const observationPanel = document.createElement("section");
    observationPanel.className = "frame-analysis-observation";
    const observationHeading = document.createElement("h4");
    observationHeading.textContent = observation.area?.label
      ? `観測: ${observation.area.label}`
      : "構造化観測";
    const quality = document.createElement("p");
    quality.className = "frame-analysis-observation-quality";
    const qualityStatus = observation.image_quality?.usable === false ? "要再確認画像" : "解析利用可";
    quality.textContent = `${qualityStatus} / ${confidenceLabel(observation.area?.confidence)}`;
    observationPanel.append(observationHeading, quality);

    const assets = observation.visible_assets ?? observation.assets ?? [];
    if (assets.length > 0) {
      const assetList = document.createElement("ul");
      assetList.className = "frame-analysis-observation-list";
      for (const asset of assets.slice(0, 6)) {
        const item = document.createElement("li");
        item.textContent = [
          asset.label,
          asset.category,
          asset.state ?? asset.condition,
          asset.identifier_text,
        ]
          .filter(Boolean).join(" / ") + `（${confidenceLabel(asset.confidence)}）`;
        assetList.appendChild(item);
      }
      observationPanel.appendChild(assetList);
    }
    const findings = [
      ...(observation.conditions ?? []),
      ...(observation.safety_observations ?? []),
      ...(observation.inspection_observations ?? []),
    ]
      .filter((item) => item.severity === "attention" || item.severity === "critical");
    for (const finding of findings.slice(0, 4)) {
      const notice = document.createElement("p");
      notice.className = `frame-analysis-observation-alert is-${finding.severity}`;
      notice.textContent = `${finding.severity === "critical" ? "重要" : "要確認"}: ${finding.observation ?? finding.label ?? "観測事項"}${finding.evidence ? ` — 根拠: ${finding.evidence}` : ""}（${confidenceLabel(finding.confidence)}）`;
      observationPanel.appendChild(notice);
    }
    const texts = observation.visible_text ?? observation.text_regions ?? observation.signage ?? [];
    if (texts.length > 0) {
      const textList = document.createElement("ul");
      textList.className = "frame-analysis-observation-list";
      for (const visibleText of texts.slice(0, 6)) {
        const item = document.createElement("li");
        item.textContent = [
          visibleText.text ?? visibleText.label,
          visibleText.context,
          visibleText.language,
          visibleText.legibility,
        ].filter(Boolean).join(" / ") + `（${confidenceLabel(visibleText.confidence)}）`;
        textList.appendChild(item);
      }
      observationPanel.appendChild(textList);
    }
    if (observation.caption) {
      const caption = document.createElement("p");
      caption.textContent = observation.caption;
      observationPanel.appendChild(caption);
    }
    if ((observation.main_subjects?.length ?? 0) > 0) {
      const subjects = document.createElement("p");
      subjects.textContent = `主要被写体: ${observation.main_subjects!.join(" / ")}`;
      observationPanel.appendChild(subjects);
    }
    if ((observation.limitations?.length ?? 0) > 0) {
      const limitations = document.createElement("p");
      limitations.className = "frame-analysis-observation-limitations";
      limitations.textContent = `判断上の制約: ${observation.limitations!.join(" / ")}`;
      observationPanel.appendChild(limitations);
    }
    content.appendChild(observationPanel);
  } else {
    const legacy = document.createElement("p");
    legacy.className = "frame-analysis-legacy-notice";
    legacy.textContent =
      "旧形式の解析結果です。検索・3D確認には使えますが、品質・確信度・観測項目は未記録で、現場AIの回答根拠には採用されません。";
    content.appendChild(legacy);
  }

  if (frame.keywords.length > 0) {
    const keywords = document.createElement("div");
    keywords.className = "frame-analysis-keywords";
    for (const keyword of frame.keywords) {
      const item = document.createElement("span");
      item.textContent = keyword;
      keywords.appendChild(item);
    }
    content.appendChild(keywords);
  }

  const actions = document.createElement("div");
  actions.className = "frame-analysis-record-actions";
  const poseSummary = document.createElement("p");
  poseSummary.className = "frame-analysis-pose-summary";
  poseSummary.textContent = position
    ? `視点位置: ${finite(position.x)}, ${finite(position.y)}, ${finite(position.z)} m`
    : "保存済みの撮影視点はありません。";
  actions.appendChild(poseSummary);

  if (databaseRecord || frame.pose || frame.projection) {
    const technicalDetails = document.createElement("details");
    technicalDetails.className = "frame-analysis-technical-details";
    const technicalSummary = document.createElement("summary");
    technicalSummary.textContent = "撮影姿勢・技術情報を表示";
    const details = document.createElement("dl");
    details.className = "frame-analysis-record-data";
    details.append(
      dataRow("DB record ID", frameId),
      dataRow("analysis_run_id", frame.analysis_run_id ?? ""),
      dataRow("SceneVersion", frame.scene_version_id ?? ""),
      dataRow("Scene Package", frame.scene_package_id ?? ""),
      dataRow("位置 (m)", position
        ? `${finite(position.x)}, ${finite(position.y)}, ${finite(position.z)}`
        : ""),
      dataRow("回転 quaternion (x,y,z,w)", frame.pose
        ? `${finite(frame.pose.rotation.x, 6)}, ${finite(frame.pose.rotation.y, 6)}, ${finite(frame.pose.rotation.z, 6)}, ${finite(frame.pose.rotation.w, 6)}`
        : ""),
    );
    technicalDetails.append(technicalSummary, details);
    if (frame.projection) {
      const projection = document.createElement("details");
      projection.className = "frame-analysis-projection";
      const projectionTitle = document.createElement("summary");
      projectionTitle.textContent = "カメラ内部パラメータ";
      const body = document.createElement("pre");
      body.textContent = JSON.stringify(frame.projection, null, 2);
      projection.append(projectionTitle, body);
      technicalDetails.appendChild(projection);
    }
    actions.appendChild(technicalDetails);
  }

  if (frame.viewer_url) {
    const open = button("3Dシーンで開く", () => {
      beforeViewerOpen();
      openViewer(frame.viewer_url ?? "");
    });
    open.className = "frame-analysis-open-viewer";
    actions.appendChild(open);
  }
  if (databaseRecord && frameId && onReview) {
    const reviewPanel = document.createElement("details");
    reviewPanel.className = "frame-analysis-review-panel";
    const reviewTitle = document.createElement("summary");
    reviewTitle.textContent = reviewStatus === "unreviewed" ? "この観測を確認する" : "確認内容を更新する";
    const reviewHelp = document.createElement("p");
    reviewHelp.className = "preview-help";
    reviewHelp.textContent = "AI結果は未確認の候補です。採用・修正・対象外の判断は追記型イベントとして保存されます。";
    const correctedSummary = document.createElement("textarea");
    correctedSummary.value = frame.summary;
    correctedSummary.rows = 3;
    correctedSummary.setAttribute("aria-label", "修正後の観測要約");
    const correctedKeywords = document.createElement("input");
    correctedKeywords.type = "text";
    correctedKeywords.value = frame.keywords.join("、");
    correctedKeywords.placeholder = "検索キーワード（読点区切り）";
    correctedKeywords.setAttribute("aria-label", "修正後の検索キーワード");
    const correctedObservation = document.createElement("textarea");
    correctedObservation.value = JSON.stringify(frame.extra ?? {}, null, 2);
    correctedObservation.rows = 10;
    correctedObservation.spellcheck = false;
    correctedObservation.setAttribute("aria-label", "修正後の構造化観測JSON");
    const canCorrectStructured = Boolean(frame.extra?.schema_version);
    const reason = document.createElement("input");
    reason.type = "text";
    reason.placeholder = "修正・対象外の理由";
    reason.setAttribute("aria-label", "確認理由");
    const reviewStatusLine = statusLine();
    const reviewActions = document.createElement("div");
    reviewActions.className = "preview-inline-actions frame-analysis-review-actions";
    const submitReview = (
      decision: "accepted" | "corrected" | "rejected",
      trigger: HTMLButtonElement,
    ): void => {
      if ((decision === "corrected" || decision === "rejected") && !reason.value.trim()) {
        reviewStatusLine.textContent = decision === "corrected"
          ? "修正理由を入力してください。"
          : "対象外にする理由を入力してください。";
        return;
      }
      let correctedExtra: FrameObservation | undefined;
      if (decision === "corrected" && canCorrectStructured) {
        try {
          const parsed = JSON.parse(correctedObservation.value);
          if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
            throw new Error("JSON object required");
          }
          correctedExtra = parsed as FrameObservation;
        } catch {
          reviewStatusLine.textContent = "構造化観測JSONの形式を確認してください。";
          return;
        }
      }
      trigger.disabled = true;
      reviewStatusLine.textContent = "確認結果を保存しています…";
      void onReview(frame, {
        decision,
        ...(decision === "corrected" ? {
          summary: correctedSummary.value.trim(),
          keywords: correctedKeywords.value.split(/[、,]/).map((item) => item.trim()).filter(Boolean),
          ...(correctedExtra ? { extra: correctedExtra } : {}),
        } : {}),
        reason: reason.value.trim(),
      }).catch((error) => {
        reviewStatusLine.textContent = `確認結果を保存できません: ${String(error)}`;
        trigger.disabled = false;
      });
    };
    const accept = button("この内容を採用", () => submitReview("accepted", accept));
    const correct = button("内容を修正して採用", () => submitReview("corrected", correct));
    const reject = button("対象外", () => submitReview("rejected", reject));
    reject.className = "preview-danger";
    reviewActions.append(accept, correct, reject);
    reviewPanel.append(
      reviewTitle,
      reviewHelp,
      correctedSummary,
      correctedKeywords,
    );
    if (canCorrectStructured) reviewPanel.appendChild(correctedObservation);
    reviewPanel.append(reason, reviewActions, reviewStatusLine);
    actions.appendChild(reviewPanel);
  }
  if (thumbnail) record.appendChild(thumbnail);
  record.append(content, actions);
  return record;
}

export async function mountFrameAnalysisView(host: HTMLElement): Promise<() => void> {
  host.classList.add("frame-analysis-page");
  const returnState = takeFrameAnalysisReturnState();
  let disposed = false;
  let selectedSceneVersionId = returnState?.scene_version_id ?? "";
  let selectedScenePackageId = "";
  let searchQuery = returnState?.query ?? "";
  let searchPage = returnState?.page ?? 0;
  let restoreScrollY = returnState?.scroll_y;
  let refreshTimer: number | undefined;
  let controlsCollapsed = false;
  let selectedModelId = "";
  let selectedAnalysisPolicyId = "";
  let reviewFilter = "";
  let lastResetResult: AnalysisResetResult | null = null;
  let controlsViewportCleanup: (() => void) | undefined;

  function preserveNavigationState(): void {
    saveFrameAnalysisReturnState(selectedSceneVersionId, searchQuery, window.scrollY, searchPage);
  }

  async function render(): Promise<void> {
    if (disposed) return;
    if (refreshTimer !== undefined) {
      window.clearTimeout(refreshTimer);
      refreshTimer = undefined;
    }
    controlsViewportCleanup?.();
    controlsViewportCleanup = undefined;
    host.replaceChildren();
    const heading = document.createElement("div");
    heading.className = "preview-heading";
    heading.innerHTML = "<h1>登録画像AI解析</h1><p>登録画像を、場所・見える設備・状態・注意事項・画像品質まで追跡できる観測インデックスへ変換します。AI結果は候補として保存し、人の確認後に検索・質問回答・変化確認へ再利用します。</p>";
    host.appendChild(heading);
    if (lastResetResult) {
      const resetNotice = document.createElement("section");
      resetNotice.className = "frame-analysis-reset-complete";
      resetNotice.setAttribute("role", "status");
      const resetTitle = document.createElement("strong");
      resetTitle.textContent = "解析DBのリセットが完了しました";
      const resetDetails = document.createElement("p");
      resetDetails.textContent = `${lastResetResult.scene_version_id} の解析run ${lastResetResult.deleted.analysis_runs.toLocaleString()}件・画像解析 ${lastResetResult.deleted.frame_analyses.toLocaleString()}件を削除しました。Scene Packageと現場台帳は保持されています。`;
      const resetBackup = document.createElement("code");
      resetBackup.textContent = `復旧用バックアップ: ${lastResetResult.backup_uri}`;
      resetNotice.append(resetTitle, resetDetails, resetBackup);
      host.appendChild(resetNotice);
      lastResetResult = null;
    }

    let scenes: SceneVersion[];
    let runs: AnalysisRun[];
    let modelCatalog: VlmModelCatalog;
    let analysisPolicyCatalog: AnalysisPolicyCatalog;
    try {
      [scenes, runs, modelCatalog, analysisPolicyCatalog] = await Promise.all([
        api<SceneVersion[]>("/api/frame-analysis/scene-versions"),
        api<AnalysisRun[]>("/api/frame-analysis/runs"),
        api<VlmModelCatalog>("/api/frame-analysis/models"),
        api<AnalysisPolicyCatalog>("/api/frame-analysis/policies"),
      ]);
    } catch (error) {
      const failed = section("解析画面を開けません");
      failed.append(`frame-analysis APIと現場・設備台帳DBを確認してください: ${String(error)}`);
      host.appendChild(failed);
      return;
    }

    const targets = scenePackageTargets(scenes);
    const restoredScene = scenes.find((scene) => scene.scene_version_id === selectedSceneVersionId);
    if (!selectedScenePackageId && restoredScene) {
      selectedScenePackageId = restoredScene.scene_package_id;
    }
    if (!selectedScenePackageId || !targets.some((target) => target.scene_package_id === selectedScenePackageId)) {
      selectedScenePackageId = targets[0]?.scene_package_id ?? "";
    }
    const selectedScene = targets.find((target) => target.scene_package_id === selectedScenePackageId);
    selectedSceneVersionId = selectedScene?.scene_version_id ?? "";
    const selectedRuns = runs.filter((run) => run.scene_version_id === selectedSceneVersionId);
    const activeRun = selectedRuns.find((run) => run.is_active);
    if (
      !selectedAnalysisPolicyId
      || !analysisPolicyCatalog.policies.some(
        (policy) => policy.analysis_policy_id === selectedAnalysisPolicyId,
      )
    ) {
      selectedAnalysisPolicyId = activeRun?.analysis_policy_id
        && analysisPolicyCatalog.policies.some(
          (policy) => policy.analysis_policy_id === activeRun.analysis_policy_id,
        )
        ? activeRun.analysis_policy_id
        : analysisPolicyCatalog.default_analysis_policy_id;
    }
    const selectedAnalysisPolicy = analysisPolicyCatalog.policies.find(
      (policy) => policy.analysis_policy_id === selectedAnalysisPolicyId,
    );
    if (!selectedModelId || !modelCatalog.models.some((model) => model.model_id === selectedModelId)) {
      selectedModelId = modelCatalog.models.some((model) => model.model_id === modelCatalog.default_model_id)
        ? modelCatalog.default_model_id
        : modelCatalog.models[0]?.model_id ?? "";
    }
    const selectedModel = modelCatalog.models.find((model) => model.model_id === selectedModelId);
    let qualitySummary: AnalysisQualitySummary | null = null;
    if (activeRun) {
      try {
        qualitySummary = await api<AnalysisQualitySummary>(
          `/api/frame-analysis/runs/${encodeURIComponent(activeRun.analysis_run_id)}/quality-summary`,
        );
      } catch {
        qualitySummary = null;
      }
    }
    const continuationBaseRun = selectedRuns
      .filter((run) => (
        run.output_count > 0
        && (run.status === "completed" || run.status === "failed" || run.status === "cancelled")
        && run.analysis_policy_id === selectedAnalysisPolicyId
        && run.output_schema_version === selectedAnalysisPolicy?.output_schema_version
        && run.model_id === selectedModelId
      ))
      .reduce<AnalysisRun | undefined>((best, run) => (
        !best || run.output_count > best.output_count ? run : best
      ), undefined);
    const relatedActiveRun = selectedScene
      ? runs.find((run) => {
          if (!run.is_active || run.output_count === 0 || run.scene_version_id === selectedScene.scene_version_id) return false;
          const runScene = scenes.find((scene) => scene.scene_version_id === run.scene_version_id);
          return runScene?.site_id === selectedScene.site_id;
        })
      : undefined;
    const relatedActiveScene = relatedActiveRun
      ? scenes.find((scene) => scene.scene_version_id === relatedActiveRun.scene_version_id)
      : undefined;

    const context = document.createElement("section");
    context.className = "frame-analysis-context";
    context.id = "frame-analysis-target";
    const contextTitle = document.createElement("h2");
    contextTitle.textContent = "解析対象";
    const contextHelp = document.createElement("p");
    contextHelp.className = "preview-help";
    contextHelp.textContent = "Scene Packageを選ぶと、その不変画像と撮影姿勢に紐づく解析版を表示します。AI観測と人の確認結果はScene Packageを書き換えず、運用DBへ追記します。";
    const scopeSelect = selectField(
      "Scene Package",
      selectedScenePackageId,
      "scene_package_scope",
      targets.length > 0
        ? targets.map((target) => ({
            value: target.scene_package_id,
            label: `${target.analysis_ready === false ? "[旧解析・閲覧のみ]" : "[人物mask対応]"} ${target.site_name} / ${target.scene_package_id} / ${target.label}${target.scene_version_count > 1 ? ` (${target.scene_version_count}版)` : ""}`,
          }))
        : [{ value: "", label: "Scene Packageがありません" }],
    );
    scopeSelect.querySelector("select")?.addEventListener("change", (event) => {
      selectedScenePackageId = (event.currentTarget as HTMLSelectElement).value;
      searchPage = 0;
      void render();
    });
    const contextStats = document.createElement("div");
    contextStats.className = "frame-analysis-context-stats";
    const contextStat = (label: string, content: string): HTMLDivElement => {
      const item = document.createElement("div");
      const caption = document.createElement("span");
      const amount = document.createElement("strong");
      caption.textContent = label;
      amount.textContent = content;
      amount.title = content;
      item.append(caption, amount);
      return item;
    };
    contextStats.append(
      contextStat("現在の解析結果", activeRun?.analysis_run_id ?? "未選択"),
      contextStat("解析済み画像", `${activeRun?.output_count ?? 0} 件`),
      contextStat("構造化観測", `${qualitySummary?.structured_records ?? 0} 件`),
      contextStat("人が確認済み", `${(qualitySummary?.accepted_records ?? 0) + (qualitySummary?.corrected_records ?? 0)} 件`),
      contextStat("未確認", `${qualitySummary?.unreviewed_records ?? activeRun?.output_count ?? 0} 件`),
      contextStat("要確認・重要", `${(qualitySummary?.attention_records ?? 0) + (qualitySummary?.critical_records ?? 0)} 件`),
    );
    context.append(contextTitle, contextHelp, scopeSelect, contextStats);
    if (selectedScene) {
      const resolved = document.createElement("p");
      resolved.className = "frame-analysis-resolved-scene";
      resolved.textContent = `解析対象SceneVersion: ${selectedScene.scene_version_id}`;
      context.appendChild(resolved);
    }
    const records = section("解析レコード");
    records.classList.add("frame-analysis-records-section");
    records.id = "frame-analysis-results";
    const purpose = document.createElement("div");
    purpose.className = "frame-analysis-purpose";
    const purposeTitle = document.createElement("h3");
    purposeTitle.textContent = "このアプリの成果物: 追跡可能な画像観測DB";
    const purposeText = document.createElement("p");
    purposeText.textContent = "画像を説明するだけではなく、①検索で該当箇所を見つける、②同じ撮影視点を3Dで確認する、③人が採否を残す、④確認済み観測を現場インサイトで横断利用するための基礎データを作ります。AIだけで設備台帳や不具合を確定しません。";
    const useInsights = document.createElement("a");
    useInsights.href = "/?preview=site-insights";
    useInsights.textContent = "確認済み観測を現場インサイトで使う →";
    purpose.append(purposeTitle, purposeText, useInsights);
    const searchHelp = document.createElement("p");
    searchHelp.className = "preview-help";
    searchHelp.textContent = "使用中の解析版を、観測要約・設備・状態・注意事項・表示文字・キーワード・画像名で検索できます。確認状態で絞り込み、人が採用した内容を業務利用してください。";
    const searchScope = document.createElement("p");
    searchScope.className = "frame-analysis-search-scope";
    searchScope.textContent = selectedScene
      ? `検索範囲: ${selectedScene.site_name} / ${selectedScene.label} / ${activeRun?.analysis_run_id ?? "使用中の解析版は未選択"}`
      : "検索範囲: SceneVersionを選択してください。";
    const relatedResult = document.createElement("div");
    relatedResult.className = "field-memory-card frame-analysis-related-result";
    relatedResult.hidden = Boolean(activeRun) || !relatedActiveRun || !relatedActiveScene;
    if (relatedActiveRun && relatedActiveScene) {
      const relatedTitle = document.createElement("h3");
      relatedTitle.textContent = `以前の解析結果が${relatedActiveRun.output_count.toLocaleString()}件あります`;
      const relatedHelp = document.createElement("p");
      relatedHelp.className = "preview-help";
      relatedHelp.textContent = "旧mask契約の結果は削除されていません。確認用の旧Scene Packageへ切り替えられます。新しい人物mask版とは混在させません。";
      const openRelated = button("以前の解析結果を表示", () => {
        selectedScenePackageId = relatedActiveScene.scene_package_id;
        selectedSceneVersionId = relatedActiveScene.scene_version_id;
        searchPage = 0;
        void render();
      });
      openRelated.className = "preview-secondary";
      relatedResult.append(relatedTitle, relatedHelp, openRelated);
    }
    const searchForm = document.createElement("div");
    searchForm.className = "preview-form frame-analysis-search-form";
    const searchInputField = field("キーワード", searchQuery, "search", "record_query");
    const reviewFilterField = selectField(
      "確認状態",
      reviewFilter,
      "review_status",
      [
        { value: "", label: "すべて" },
        { value: "unreviewed", label: "AI生成・未確認" },
        { value: "accepted", label: "確認済み" },
        { value: "corrected", label: "修正して採用" },
        { value: "rejected", label: "対象外" },
      ],
    );
    reviewFilterField.querySelector("select")?.addEventListener("change", (event) => {
      reviewFilter = (event.currentTarget as HTMLSelectElement).value;
      void executeRecordSearch(value(searchForm, "record_query"), 0);
    });
    searchForm.append(searchInputField, reviewFilterField);
    const searchActions = document.createElement("div");
    searchActions.className = "preview-inline-actions frame-analysis-search-actions";
    const searchStatus = statusLine();
    searchStatus.classList.add("frame-analysis-search-status");
    const searchResults = document.createElement("div");
    searchResults.className = "frame-analysis-record-list";
    searchResults.setAttribute("role", "list");
    const searchPagination = document.createElement("nav");
    searchPagination.className = "frame-analysis-pagination";
    searchPagination.setAttribute("aria-label", "解析レコードのページ送り");
    searchPagination.hidden = true;

    async function executeRecordSearch(query: string, requestedPage = 0): Promise<void> {
      searchQuery = query;
      searchPage = Math.max(0, Math.trunc(requestedPage));
      if (!selectedScene) {
        searchStatus.textContent = "SceneVersionを選択してください。";
        return;
      }
      searchStatus.textContent = "DBを検索しています…";
      searchResults.replaceChildren();
      searchPagination.replaceChildren();
      searchPagination.hidden = true;
      try {
        const response = await api<FrameSearchResponse>("/api/spatial-search/query", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            site_id: selectedScene.site_id,
            scene_version_id: selectedScene.scene_version_id,
            query,
            record_types: ["frame_analysis"],
            ...(reviewFilter ? { frame_review_status: reviewFilter } : {}),
            offset: searchPage * FRAME_SEARCH_PAGE_SIZE,
            limit: FRAME_SEARCH_PAGE_SIZE,
          }),
        });
        if (disposed) return;
        const totalCount = response.total_count ?? response.count;
        const pageSize = response.limit || FRAME_SEARCH_PAGE_SIZE;
        const totalPages = Math.max(1, Math.ceil(totalCount / pageSize));
        if (totalCount > 0 && searchPage >= totalPages) {
          await executeRecordSearch(query, totalPages - 1);
          return;
        }
        const resultOffset = response.offset ?? searchPage * pageSize;
        const rangeStart = totalCount === 0 ? 0 : resultOffset + 1;
        const rangeEnd = resultOffset + response.results.length;
        searchStatus.textContent = totalCount === 0
          ? "0件の解析レコード"
          : `${totalCount.toLocaleString()}件中 ${rangeStart.toLocaleString()}〜${rangeEnd.toLocaleString()}件を表示`;
        if (totalCount > pageSize) {
          const previous = button("前のページ", () => {
            void executeRecordSearch(searchQuery, searchPage - 1);
          }, searchPage === 0);
          previous.className = "preview-secondary";
          const pageLabel = document.createElement("span");
          pageLabel.className = "frame-analysis-pagination-label";
          pageLabel.textContent = `${searchPage + 1} / ${totalPages}ページ`;
          const next = button("次のページ", () => {
            void executeRecordSearch(searchQuery, searchPage + 1);
          }, searchPage + 1 >= totalPages);
          next.className = "preview-secondary";
          searchPagination.append(previous, pageLabel, next);
          searchPagination.hidden = false;
        }
        if (response.results.length === 0) {
          const empty = document.createElement("p");
          empty.className = "preview-empty-state";
          empty.textContent = query
            ? "一致するレコードはありません。検索語を変えてください。"
            : "使用中の解析版にDBレコードがありません。完了した解析版を使用中に切り替えてください。";
          searchResults.appendChild(empty);
          return;
        }
        searchResults.append(...response.results.map((frame) => (
          frameRecordListItem(
            frame,
            true,
            preserveNavigationState,
            async (reviewedFrame, reviewPayload) => {
              const frameId = reviewedFrame.frame_analysis_id ?? reviewedFrame.record_id;
              if (!frameId) throw new Error("frame_analysis_id_missing");
              await api(`/api/frame-analysis/frames/${encodeURIComponent(frameId)}/reviews`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(reviewPayload),
              });
              await executeRecordSearch(searchQuery, searchPage);
            },
          )
        )));
      } catch (error) {
        searchStatus.textContent = `DB検索に失敗しました: ${String(error)}`;
        searchPagination.replaceChildren();
        searchPagination.hidden = true;
      }
    }

    const searchButton = button("検索", () => void executeRecordSearch(value(searchForm, "record_query"), 0));
    searchButton.className = "frame-analysis-search-button";
    const allButton = button("検索をクリア", () => {
      const input = searchForm.querySelector<HTMLInputElement>("[data-field=record_query]");
      if (input) input.value = "";
      void executeRecordSearch("", 0);
    });
    allButton.className = "preview-secondary";
    searchInputField.querySelector("input")?.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        void executeRecordSearch(value(searchForm, "record_query"), 0);
      }
    });
    searchActions.append(searchButton, allButton, searchStatus);
    records.append(purpose, searchHelp, searchScope, relatedResult, searchForm, searchActions, searchPagination, searchResults);

    const allFrames = section("AI解析を実行");
    allFrames.classList.add("frame-analysis-all-frames-section");
    allFrames.id = "frame-analysis-run";
    const allFramesHelp = document.createElement("p");
    allFramesHelp.className = "preview-help";
    allFramesHelp.textContent = "解析目的を先に選び、COLMAP登録画像を目的別のDBスキーマへ登録します。人物maskだけを中立色で除外し、過大maskフレームは対象外です。結果は未確認候補であり、人の判断を置き換えません。";
    const allFramesScope = document.createElement("p");
    allFramesScope.className = "frame-analysis-search-scope";
    allFramesScope.textContent = selectedScene
      ? `対象Scene Package: ${selectedScene.scene_package_id}`
      : "Scene Packageを選択してください。";
    const policyControls = document.createElement("div");
    policyControls.className = "frame-analysis-policy-controls";
    const policySelect = selectField(
      "解析目的（保存する観測の種類）",
      selectedAnalysisPolicyId,
      "analysis_policy",
      analysisPolicyCatalog.policies.map((policy) => ({
        value: policy.analysis_policy_id,
        label: `${policy.label}｜${policy.output_fields.join("・")}`,
      })),
    );
    policySelect.querySelector("select")?.addEventListener("change", (event) => {
      selectedAnalysisPolicyId = (event.currentTarget as HTMLSelectElement).value;
      void render();
    });
    const policyDetails = document.createElement("section");
    policyDetails.className = "frame-analysis-policy-details";
    policyDetails.dataset.testid = "analysis-policy-details";
    if (selectedAnalysisPolicy) {
      const title = document.createElement("h5");
      title.textContent = selectedAnalysisPolicy.label;
      const description = document.createElement("p");
      description.textContent = selectedAnalysisPolicy.description;
      const useWhen = document.createElement("p");
      useWhen.textContent = `使う場面: ${selectedAnalysisPolicy.use_when}`;
      const contract = document.createElement("p");
      const downstream = selectedAnalysisPolicy.downstream_uses.includes("grounded-ai")
        ? "人の確認後、現場AIの根拠に利用可能"
        : "現場AIの根拠には利用しない";
      contract.textContent = `DB契約: ${selectedAnalysisPolicy.output_schema_version} / ${downstream}`;
      policyDetails.append(title, description, useWhen, contract);
    }
    policyControls.append(policySelect, policyDetails);
    const modelControls = document.createElement("div");
    modelControls.className = "frame-analysis-model-controls";
    const modelSelect = selectField(
      "解析モデル（Qwen型名・推奨VRAM）",
      selectedModelId,
      "analysis_model",
      modelCatalog.models.length > 0
        ? modelCatalog.models.map((model) => ({
          value: model.model_id,
          label: `${model.installed ? "[導入済み]" : model.status === "downloading" || model.status === "queued" ? "[ダウンロード中]" : "[未導入]"} ${model.model_id.split("/").pop() ?? model.model_id}｜型: ${model.parameter_scale}｜推奨VRAM: ${model.recommended_vram_gib} GB`,
          }))
        : [{ value: "", label: "利用可能な審査済みモデルがありません" }],
    );
    modelSelect.querySelector("select")?.addEventListener("change", (event) => {
      selectedModelId = (event.currentTarget as HTMLSelectElement).value;
      void render();
    });
    const modelDownloadDetails = document.createElement("section");
    modelDownloadDetails.className = "frame-analysis-model-download-details";
    modelDownloadDetails.dataset.testid = "model-download-details";
    if (selectedModel) {
      const displayName = selectedModel.display_name ?? `Qwen3-VL ${selectedModel.parameter_scale} Instruct`;
      const detailsTitle = document.createElement("h5");
      detailsTitle.textContent = selectedModel.installed ? "選択中のローカルAIモデル" : "ダウンロードされる内容";
      const detailsLead = document.createElement("p");
      detailsLead.className = "frame-analysis-model-download-lead";
      detailsLead.textContent = `${displayName} — 登録画像から観測要約・検索語・構造化観測・品質・確信度を生成するローカルVLMです。`;
      const detailsList = document.createElement("dl");
      const detailRows: Array<[string, string]> = [
        ["正式ID", selectedModel.model_id],
        ["提供元", selectedModel.source_provider ?? "Hugging Face / Qwen"],
        ["取得内容", "モデル重み（.safetensors）、設定ファイル、画像プロセッサ、トークナイザー"],
        ["容量", `約${selectedModel.approximate_download_gib} GiB`],
        ["保存先", `ローカルのモデル領域 / ${selectedModel.storage_directory ?? selectedModel.parameter_scale.toLowerCase()}`],
      ];
      for (const [term, description] of detailRows) {
        const dt = document.createElement("dt");
        dt.textContent = term;
        const dd = document.createElement("dd");
        dd.textContent = description;
        detailsList.append(dt, dd);
      }
      const sourceLink = document.createElement("a");
      sourceLink.className = "frame-analysis-model-source-link";
      sourceLink.href = selectedModel.source_url
        ?? `https://huggingface.co/${selectedModel.model_id}/tree/${selectedModel.revision}`;
      sourceLink.target = "_blank";
      sourceLink.rel = "noreferrer noopener";
      sourceLink.textContent = `Hugging Faceで固定revision ${selectedModel.revision.slice(0, 12)}…を確認`;
      const privacyNote = document.createElement("p");
      privacyNote.className = "frame-analysis-model-download-note";
      privacyNote.textContent = "外部通信はモデル取得時だけです。解析画像は外部へ送信しません。人物マスク生成用のGroundingDINO／SAM2ではありません。";
      modelDownloadDetails.append(detailsTitle, detailsLead, detailsList, sourceLink, privacyNote);
    }
    const modelStatus = statusLine();
    if (selectedModel?.installed) {
      modelStatus.textContent = `導入済み: 固定revision ${selectedModel.revision.slice(0, 12)}…`;
    } else if (selectedModel?.status === "downloading" || selectedModel?.status === "queued") {
      modelStatus.textContent = `モデルをダウンロードしています（約${selectedModel.approximate_download_gib} GiB）。画面を閉じても処理は継続します。`;
    } else if (selectedModel?.status === "failed") {
      modelStatus.textContent = `モデルの導入に失敗しました: ${selectedModel.error ?? "詳細不明"}`;
    } else {
      modelStatus.textContent = selectedModel
        ? `解析前にモデルをダウンロードしてください（約${selectedModel.approximate_download_gib} GiB）。画像は外部へ送信しません。`
        : "審査済みモデルが登録されていません。";
    }
    if (selectedModel) {
      modelStatus.textContent += ` 必要VRAM目安: 最低${selectedModel.minimum_vram_gib} GB / 推奨${selectedModel.recommended_vram_gib} GB。固定revision ${selectedModel.revision.slice(0, 12)}… / ${selectedModel.license}。`;
    }
    modelControls.append(modelSelect, modelDownloadDetails, modelStatus);
    if (selectedModel && !selectedModel.installed) {
      const displayName = selectedModel.display_name ?? `Qwen3-VL ${selectedModel.parameter_scale} Instruct`;
      const installModel = button(
        selectedModel.status === "downloading" || selectedModel.status === "queued"
          ? `${displayName}をダウンロード中`
          : `${displayName}（約${selectedModel.approximate_download_gib} GiB）をダウンロード`,
        () => {
          installModel.disabled = true;
          modelStatus.textContent = "ダウンロードを開始しています…";
          void api<VlmModelStatus>("/api/frame-analysis/models/install", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ model_id: selectedModel.model_id }),
          }).then(() => void render()).catch((error) => {
            modelStatus.textContent = `モデルの導入を開始できません: ${String(error)}`;
            installModel.disabled = false;
          });
        },
        selectedModel.status === "downloading" || selectedModel.status === "queued",
      );
      installModel.className = "preview-secondary";
      installModel.dataset.testid = "download-selected-model";
      modelControls.appendChild(installModel);
    }
    const allFramesActions = document.createElement("div");
    allFramesActions.className = "preview-inline-actions frame-analysis-search-actions";
    const allFramesStatus = statusLine();
    allFramesStatus.classList.add("frame-analysis-search-status");
    const allFramesProgress = document.createElement("div");
    allFramesProgress.className = "frame-analysis-run-progress";
    allFramesProgress.hidden = true;
    const activeAllFramesRun = selectedRuns.find((run) => (
      (run.status === "queued" || run.status === "running")
      && run.parameters?.selection_policy_version === "all-registered-images-v1"
    ));
    const latestPartialAllFramesRun = selectedRuns.find((run) => (
      run.analysis_run_id !== activeAllFramesRun?.analysis_run_id
      && run.status === "failed"
      && run.output_count > 0
      && run.parameters?.selection_policy_version === "all-registered-images-v1"
    ));

    const updateAllFramesProgress = (run: AnalysisRun): void => {
      const completedCount = run.output_count;
      const targetCount = run.target_count ?? null;
      const progressPercent = run.progress_percent
        ?? (targetCount ? Math.min(100, Math.round(completedCount * 1000 / targetCount) / 10) : null);
      const operation = run.status === "queued"
        ? "worker待機中"
        : completedCount === 0
          ? "モデル読込・初回推論中"
          : "解析中";
      const countLabel = targetCount === null
        ? `${completedCount.toLocaleString()}件`
        : `${completedCount.toLocaleString()} / ${targetCount.toLocaleString()}件`;
      const percentLabel = progressPercent === null ? "" : `（${progressPercent.toFixed(1)}%）`;
      allFramesStatus.textContent = `${operation}: ${countLabel}${percentLabel}`;

      const progress = document.createElement("progress");
      progress.max = Math.max(targetCount ?? 1, 1);
      progress.value = Math.min(completedCount, progress.max);
      progress.setAttribute("aria-label", "全登録フレームのVLM解析進捗");
      const progressMeta = document.createElement("div");
      progressMeta.className = "frame-analysis-run-progress-meta";
      progressMeta.textContent = targetCount === null
        ? `解析版: ${run.analysis_run_id}`
        : `解析版: ${run.analysis_run_id} ・ 残り ${Math.max(targetCount - completedCount, 0).toLocaleString()}件`;
      const progressHelp = document.createElement("p");
      progressHelp.className = "preview-help";
      progressHelp.textContent = completedCount === 0 && run.status === "running"
        ? `${run.model_id}の読込と最初の推論には数分かかる場合があります。進捗だけを5秒ごとに更新します。`
        : "DBへ保存済みの件数です。入力欄・開閉状態・スクロールを維持したまま進捗だけを更新します。";
      allFramesProgress.replaceChildren(progress, progressMeta, progressHelp);
      allFramesProgress.hidden = false;
    };

    if (activeAllFramesRun) {
      updateAllFramesProgress(activeAllFramesRun);
      const cancel = button("AI解析を停止", () => {
        cancel.disabled = true;
        allFramesStatus.textContent = "停止を要求しています…";
        void api<AnalysisRun>(`/api/frame-analysis/runs/${encodeURIComponent(activeAllFramesRun.analysis_run_id)}/cancel`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: "{}",
        }).then(() => void render()).catch((error) => {
          allFramesStatus.textContent = `停止できません: ${String(error)}`;
          cancel.disabled = false;
        });
      });
      cancel.className = "preview-danger";
      allFramesActions.appendChild(cancel);
    } else {
      const analysisReady = selectedScene?.analysis_ready === true;
      const queueAllRegistered = (freshStart: boolean, trigger: HTMLButtonElement): void => {
        if (!selectedScene) {
          allFramesStatus.textContent = "Scene Packageを選択してください。";
          return;
        }
        trigger.disabled = true;
        allFramesStatus.textContent = freshStart
          ? "既存結果を引き継がない全件再解析を予約しています…"
          : continuationBaseRun
            ? `${continuationBaseRun.output_count.toLocaleString()}件を引き継ぐ追加解析を予約しています…`
            : "全登録フレームの解析を予約しています…";
        void api<AnalysisRun>(
          `/api/frame-analysis/scene-versions/${encodeURIComponent(selectedScene.scene_version_id)}/all-registered-images`,
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              model_id: selectedModelId,
              analysis_policy_id: selectedAnalysisPolicyId,
              interval_sec: 0.1,
              similarity_threshold: 1,
              ...(freshStart || !continuationBaseRun
                ? { fresh_start: true }
                : { base_analysis_run_id: continuationBaseRun.analysis_run_id }),
            }),
          },
        ).then(() => void render()).catch((error) => {
          allFramesStatus.textContent = `AI解析を予約できません: ${String(error)}`;
          trigger.disabled = false;
        });
      };
      const registerAll = button(
        continuationBaseRun
          ? `未解析画像を追加解析（${continuationBaseRun.output_count.toLocaleString()}件を引継ぎ）`
          : "全登録画像をAI解析",
        () => queueAllRegistered(false, registerAll),
        !selectedScene || !analysisReady || !selectedModel?.installed,
      );
      registerAll.className = "frame-analysis-search-button";
      allFramesActions.appendChild(registerAll);
      if (
        activeRun
        && continuationBaseRun
        && continuationBaseRun.analysis_run_id !== activeRun.analysis_run_id
        && continuationBaseRun.output_count > activeRun.output_count
      ) {
        const restoreLarger = button(
          `${continuationBaseRun.output_count.toLocaleString()}件の解析結果を表示に戻す`,
          () => {
            restoreLarger.disabled = true;
            allFramesStatus.textContent = `${continuationBaseRun.analysis_run_id} を使用中へ戻しています…`;
            void api(`/api/frame-analysis/scene-versions/${encodeURIComponent(selectedScene!.scene_version_id)}/active-run`, {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ analysis_run_id: continuationBaseRun.analysis_run_id }),
            }).then(() => void render()).catch((error) => {
              allFramesStatus.textContent = `解析結果を戻せません: ${String(error)}`;
              restoreLarger.disabled = false;
            });
          },
        );
        restoreLarger.className = "preview-secondary";
        allFramesActions.appendChild(restoreLarger);
      }
      if (continuationBaseRun) {
        const freshAnalysis = button(
          "全件を新しい解析版で再解析",
          () => queueAllRegistered(true, freshAnalysis),
          !selectedScene || !analysisReady || !selectedModel?.installed,
        );
        freshAnalysis.className = "preview-secondary";
        allFramesActions.appendChild(freshAnalysis);
      }
      allFramesStatus.textContent = !selectedModel?.installed
        ? "解析モデルを導入すると実行できます。既存の解析結果はそのまま閲覧できます。"
        : !analysisReady && selectedScene
        ? "この旧Scene Packageは閲覧専用です。人物mask対応Packageを選ぶと新しい解析を実行できます。"
        : activeRun && continuationBaseRun
          && continuationBaseRun.analysis_run_id !== activeRun.analysis_run_id
          && continuationBaseRun.output_count > activeRun.output_count
          ? `現在は${activeRun.output_count.toLocaleString()}件の解析版を表示中ですが、${continuationBaseRun.output_count.toLocaleString()}件の旧解析版は削除されず残っています。表示を戻すか、その${continuationBaseRun.output_count.toLocaleString()}件を引き継いで追加解析できます。`
        : continuationBaseRun
          ? `${continuationBaseRun.analysis_run_id} の${continuationBaseRun.output_count.toLocaleString()}件を複製して保持し、未解析画像だけを追加します。元の解析版も履歴に残ります。`
        : latestPartialAllFramesRun
          ? `前回は${latestPartialAllFramesRun.output_count.toLocaleString()}件を保存して停止しました。解析履歴から詳細を確認できます。`
          : "登録済みの全画像が対象です。実行中も、保存された結果を順次確認できます。";
    }
    allFrames.append(
      allFramesHelp,
      allFramesScope,
      policyControls,
      modelControls,
      allFramesActions,
      allFramesStatus,
      allFramesProgress,
    );

    const resetManagement = document.createElement("details");
    resetManagement.className = "frame-analysis-reset-management";
    resetManagement.dataset.testid = "analysis-reset-management";
    const resetSummary = document.createElement("summary");
    resetSummary.textContent = "解析DBをリセット";
    const resetLead = document.createElement("p");
    resetLead.className = "preview-help";
    resetLead.textContent = "選択中SceneVersionの解析run・画像解析・確認履歴・構造化観測・有効化履歴だけを削除します。共有SQLite全体、Scene Package、現場・設備台帳、根拠記録、添付ファイルは削除しません。管理者向けの復旧可能な操作です。";
    const resetScope = document.createElement("p");
    resetScope.className = "frame-analysis-search-scope";
    resetScope.textContent = selectedScene
      ? `対象: ${selectedScene.label} / ${selectedScene.scene_version_id}`
      : "対象SceneVersionがありません。";
    const resetPanel = document.createElement("div");
    resetPanel.className = "frame-analysis-reset-panel";
    const resetStatus = statusLine();
    const inspectReset = button("削除内容を確認", () => {
      if (!selectedScene) return;
      inspectReset.disabled = true;
      resetStatus.textContent = "解析DBの参照関係と削除件数を確認しています…";
      resetPanel.replaceChildren(resetStatus);
      void api<AnalysisResetPreflight>(
        `/api/frame-analysis/scene-versions/${encodeURIComponent(selectedScene.scene_version_id)}/reset-preflight`,
      ).then((preflight) => {
        if (disposed) return;
        const counts = document.createElement("dl");
        counts.className = "frame-analysis-reset-counts";
        counts.append(
          dataRow("解析run", `${preflight.counts.analysis_runs.toLocaleString()} 件`),
          dataRow("画像解析", `${preflight.counts.frame_analyses.toLocaleString()} 件`),
          dataRow("うち旧形式", `${preflight.counts.legacy_frame_analyses.toLocaleString()} 件`),
          dataRow("人の確認履歴", `${preflight.counts.review_events.toLocaleString()} 件`),
          dataRow("構造化観測", `${preflight.counts.observation_items.toLocaleString()} 件`),
          dataRow("根拠から引用中", `${preflight.counts.evidence_citations.toLocaleString()} 件`),
        );
        const safety = document.createElement("p");
        safety.className = "preview-help";
        safety.textContent = "実行直前にSQLiteの完全バックアップを作成し、整合性を検査します。content-addressed添付ファイルは共有参照に備えて保持します。";
        resetPanel.replaceChildren(counts, safety);
        if (!preflight.can_reset) {
          const blockerLabels: Record<string, string> = {
            frame_analysis_reset_nothing_to_delete: "削除対象の解析データがありません。",
            frame_analysis_reset_active_run_exists: "実行中または待機中の解析があります。先に停止してください。",
            frame_analysis_reset_active_evidence_inquiry_exists: "この現場で根拠付き回答を生成中です。完了後に再確認してください。",
            frame_analysis_reset_evidence_citations_exist: "解析レコードが根拠付き回答から引用されています。監査根拠を壊すため削除できません。",
            frame_analysis_reset_external_review_reference_exists: "別のSceneVersionの確認履歴が対象データを継承しています。参照関係を保護するため削除できません。",
          };
          const blocked = document.createElement("p");
          blocked.className = "frame-analysis-reset-blocked";
          blocked.textContent = preflight.blockers.map((item) => blockerLabels[item] ?? item).join(" ");
          resetPanel.appendChild(blocked);
          return;
        }

        const executeReset = button("上記の解析データをリセット", () => {
          if (!selectedScene) return;
          executeReset.disabled = true;
          resetStatus.textContent = "バックアップを作成して解析データを削除しています…";
          void api<AnalysisResetResult>(
            `/api/frame-analysis/scene-versions/${encodeURIComponent(selectedScene.scene_version_id)}/reset`,
            {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({
                reason: "frame_analysis_reset_requested_from_management_ui",
                confirmation: preflight.confirmation_phrase,
                state_token: preflight.state_token,
              }),
            },
          ).then((result) => {
            lastResetResult = result;
            searchPage = 0;
            void render();
          }).catch((error) => {
            resetStatus.textContent = `解析DBをリセットできません: ${String(error)}`;
            executeReset.disabled = false;
          });
        });
        executeReset.className = "preview-danger";
        executeReset.dataset.testid = "execute-analysis-reset";
        resetStatus.textContent = "対象件数を確認して、リセットを実行してください。";
        resetPanel.append(executeReset, resetStatus);
      }).catch((error) => {
        resetStatus.textContent = `削除内容を確認できません: ${String(error)}。この操作は管理者だけが実行できます。`;
        inspectReset.disabled = false;
      });
    }, !selectedScene || selectedRuns.length === 0);
    inspectReset.className = "preview-secondary";
    resetPanel.append(inspectReset, resetStatus);
    resetManagement.append(resetSummary, resetLead, resetScope, resetPanel);

    await executeRecordSearch(searchQuery, searchPage);

    const history = document.createElement("div");
    history.className = "frame-analysis-history-content";
    const refresh = button("一覧を更新", () => void render());
    refresh.className = "preview-secondary";
    history.appendChild(refresh);
    if (selectedRuns.length === 0) {
      const empty = document.createElement("p");
      empty.className = "preview-empty-state";
      empty.textContent = "このSceneVersionの解析版はまだありません。";
      history.appendChild(empty);
    }
    for (const summary of selectedRuns) {
      const hasUsablePartialResult = summary.status === "cancelled" && summary.output_count > 0;
      const card = document.createElement("article");
      card.className = "field-memory-card";
      const title = document.createElement("h3");
      title.textContent = `${summary.is_active ? "使用中 / " : ""}${summary.analysis_run_id}`;
      const metadata = document.createElement("p");
      metadata.className = "preview-help";
      const statusLabel = summary.status === "cancelled"
        ? "中止済み"
        : summary.status;
      const policyLabel = analysisPolicyCatalog.policies.find(
        (policy) => policy.analysis_policy_id === summary.analysis_policy_id,
      )?.label ?? summary.analysis_policy_id ?? "旧形式";
      metadata.textContent = `${statusLabel} / ${summary.output_count}件 / ${policyLabel} / ${summary.model_id} / ${summary.created_at}${summary.failure_reason && summary.failure_reason !== "cancelled_by_operator" ? ` / ${summary.failure_reason}` : ""}`;
      card.append(title, metadata);
      if (summary.status === "queued" || summary.status === "running") {
        const cancel = button("この解析を停止", () => {
          cancel.disabled = true;
          metadata.textContent = "停止を要求しています…";
          void api<AnalysisRun>(`/api/frame-analysis/runs/${encodeURIComponent(summary.analysis_run_id)}/cancel`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: "{}",
          }).then(() => void render()).catch((error) => {
            metadata.textContent = `停止できません: ${String(error)}`;
            cancel.disabled = false;
          });
        });
        cancel.className = "preview-danger";
        card.appendChild(cancel);
      }
      if ((summary.status === "completed" || hasUsablePartialResult) && !summary.is_active) {
        const activate = button(hasUsablePartialResult ? "この途中結果を使用する" : "この解析版を使用する", () => {
          activate.disabled = true;
          void api(`/api/frame-analysis/scene-versions/${encodeURIComponent(summary.scene_version_id)}/active-run`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ analysis_run_id: summary.analysis_run_id }),
          }).then(() => void render()).catch((error) => {
            metadata.textContent = `切り替えできません: ${String(error)}`;
            activate.disabled = false;
          });
        });
        card.appendChild(activate);
      }
      if (summary.status === "completed" || hasUsablePartialResult) {
        const disclosure = document.createElement("details");
        disclosure.className = "frame-analysis-run-frames";
        const disclosureTitle = document.createElement("summary");
        disclosureTitle.textContent = `${summary.output_count}件の解析レコードを表示`;
        const frames = document.createElement("div");
        frames.className = "frame-analysis-record-list";
        frames.setAttribute("role", "list");
        let framesLoaded = false;
        disclosure.addEventListener("toggle", () => {
          if (!disclosure.open || framesLoaded) return;
          framesLoaded = true;
          const loading = statusLine();
          loading.textContent = "解析レコードを読み込んでいます…";
          frames.replaceChildren(loading);
          void api<AnalysisRun>(`/api/frame-analysis/runs/${encodeURIComponent(summary.analysis_run_id)}`)
            .then((detail) => {
              if (disposed) return;
              frames.replaceChildren(...(detail.frames ?? []).map((frame) => {
                const historyFrame = { ...frame };
                if (hasUsablePartialResult) historyFrame.thumbnail_url = "";
                return frameRecordListItem(historyFrame, false, preserveNavigationState);
              }));
              if ((detail.frames?.length ?? 0) === 0) {
                const empty = document.createElement("p");
                empty.className = "preview-empty-state";
                empty.textContent = "この解析版にレコードはありません。";
                frames.appendChild(empty);
              }
            })
            .catch((error) => {
              framesLoaded = false;
              loading.textContent = `解析レコードを取得できません: ${String(error)}`;
            });
        });
        disclosure.append(disclosureTitle, frames);
        card.appendChild(disclosure);
      }
      history.appendChild(card);
    }

    const layout = document.createElement("div");
    layout.className = "frame-analysis-layout";

    const sidebar = document.createElement("aside");
    sidebar.className = "frame-analysis-controls";
    sidebar.setAttribute("aria-label", "解析対象とAI解析の設定");
    const sidebarHeader = document.createElement("div");
    sidebarHeader.className = "frame-analysis-controls-header";
    const sidebarTitle = document.createElement("h2");
    sidebarTitle.textContent = "解析設定";
    const sidebarToggle = document.createElement("button");
    sidebarToggle.type = "button";
    sidebarToggle.className = "preview-secondary frame-analysis-controls-toggle";
    const toggleIcon = document.createElement("span");
    toggleIcon.setAttribute("aria-hidden", "true");
    const toggleLabel = document.createElement("span");
    toggleLabel.className = "frame-analysis-controls-toggle-label";
    const sidebarBody = document.createElement("div");
    sidebarBody.className = "frame-analysis-controls-body";
    sidebarBody.append(context, allFrames, resetManagement);

    const applyControlsState = (): void => {
      sidebar.classList.toggle("is-collapsed", controlsCollapsed);
      layout.classList.toggle("is-controls-collapsed", controlsCollapsed);
      sidebarBody.hidden = controlsCollapsed;
      toggleIcon.textContent = controlsCollapsed ? "▶" : "◀";
      toggleLabel.textContent = controlsCollapsed ? "設定を開く" : "設定を閉じる";
      sidebarToggle.setAttribute("aria-label", controlsCollapsed ? "解析設定を展開" : "解析設定を折りたたむ");
      sidebarToggle.setAttribute("aria-expanded", String(!controlsCollapsed));
    };
    sidebarToggle.addEventListener("click", () => {
      controlsCollapsed = !controlsCollapsed;
      applyControlsState();
    });
    sidebarToggle.append(toggleIcon, toggleLabel);
    sidebarHeader.append(sidebarTitle, sidebarToggle);
    sidebar.append(sidebarHeader, sidebarBody);

    const historyPanel = document.createElement("details");
    historyPanel.className = "frame-analysis-history-panel";
    const historySummary = document.createElement("summary");
    const historyLabel = document.createElement("strong");
    historyLabel.textContent = `解析履歴（${selectedRuns.length}件）`;
    const historyHint = document.createElement("span");
    historyHint.textContent = "解析結果の切り替え・停止・詳細確認";
    historySummary.append(historyLabel, historyHint);
    historyPanel.append(historySummary, history);
    historyPanel.open = !activeRun && selectedRuns.length > 0;
    const main = document.createElement("main");
    main.className = "frame-analysis-main";
    main.append(records, historyPanel);
    layout.append(sidebar, main);
    applyControlsState();
    host.appendChild(layout);

    const scrollContainer = host.closest<HTMLElement>(".preview-shell");
    let controlsHeightFrame: number | undefined;
    const syncControlsHeight = (): void => {
      controlsHeightFrame = undefined;
      if (window.matchMedia("(max-width: 980px)").matches) {
        sidebar.style.removeProperty("--frame-analysis-controls-available-height");
        return;
      }
      const sidebarTop = Math.max(sidebar.getBoundingClientRect().top, 16);
      const availableHeight = Math.max(0, Math.floor(window.innerHeight - sidebarTop - 16));
      sidebar.style.setProperty("--frame-analysis-controls-available-height", `${availableHeight}px`);
    };
    const scheduleControlsHeightSync = (): void => {
      if (controlsHeightFrame !== undefined) return;
      controlsHeightFrame = window.requestAnimationFrame(syncControlsHeight);
    };
    window.addEventListener("resize", scheduleControlsHeightSync);
    scrollContainer?.addEventListener("scroll", scheduleControlsHeightSync, { passive: true });
    syncControlsHeight();
    controlsViewportCleanup = () => {
      window.removeEventListener("resize", scheduleControlsHeightSync);
      scrollContainer?.removeEventListener("scroll", scheduleControlsHeightSync);
      if (controlsHeightFrame !== undefined) window.cancelAnimationFrame(controlsHeightFrame);
    };

    if (restoreScrollY !== undefined) {
      const scrollY = restoreScrollY;
      restoreScrollY = undefined;
      window.requestAnimationFrame(() => {
        window.requestAnimationFrame(() => window.scrollTo({ top: scrollY, behavior: "auto" }));
      });
    }

    const pending = selectedRuns.some((run) => run.status === "queued" || run.status === "running")
      || modelCatalog.models.some((model) => model.status === "queued" || model.status === "downloading");
    const scheduleProgressRefresh = (): void => {
      refreshTimer = window.setTimeout(() => {
        void api<AnalysisRun[]>("/api/frame-analysis/runs").then((latestRuns) => {
          if (disposed) return;
          const latestPending = latestRuns.find((run) => (
            run.scene_version_id === selectedSceneVersionId
            && (run.status === "queued" || run.status === "running")
            && run.parameters?.selection_policy_version === "all-registered-images-v1"
          ));
          if (!latestPending) {
            restoreScrollY = window.scrollY;
            void render();
            return;
          }
          updateAllFramesProgress(latestPending);
          scheduleProgressRefresh();
        }).catch(() => {
          if (!disposed) scheduleProgressRefresh();
        });
      }, 5000);
    };
    if (pending) scheduleProgressRefresh();
  }

  await render();
  return () => {
    disposed = true;
    if (refreshTimer !== undefined) window.clearTimeout(refreshTimer);
    controlsViewportCleanup?.();
    host.classList.remove("frame-analysis-page");
  };
}
