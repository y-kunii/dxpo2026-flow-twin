import { api, querySimilarFieldImage } from "./PreviewApiClient";
import { button, field, section, selectField } from "./PreviewUi";
import { openViewer, resolveViewerUrl } from "./ViewerNavigation";

interface SiteRecord {
  site_id: string;
  name: string;
}

interface SearchResult {
  record_type: string;
  record_id: string;
  title: string;
  summary?: string;
  timestamp?: string;
  asset_type?: string;
  asset_status?: string;
  focus?: { x: number; y: number; z: number };
  viewer_url?: string;
  thumbnail_url?: string;
  keywords?: string[];
  score?: number;
}

interface SearchResponse {
  count: number;
  results?: SearchResult[];
  timeline?: SearchResult[];
}

interface ImageIndexJob {
  job_id: string;
  status: "queued" | "running" | "completed" | "failed";
  output_count: number;
  failure_reason?: string | null;
  created_at: string;
}

export interface SpatialSearchViewOptions {
  siteId?: string;
  embedded?: boolean;
}

function fixedSiteField(siteId: string): HTMLInputElement {
  const input = document.createElement("input");
  input.type = "hidden";
  input.value = siteId;
  input.dataset.field = "site_id";
  return input;
}

function value(host: ParentNode, key: string): string {
  return host.querySelector<HTMLInputElement | HTMLSelectElement>(`[data-field="${key}"]`)?.value.trim() ?? "";
}

function resultCard(item: SearchResult): HTMLElement {
  const card = document.createElement("article");
  card.className = "field-memory-card spatial-search-result";
  const heading = document.createElement("h3");
  heading.textContent = item.title;
  const type = document.createElement("span");
  type.className = "preview-status";
  type.textContent = item.record_type;
  const summary = document.createElement("p");
  summary.textContent = item.summary || item.record_id;
  const metadata = document.createElement("p");
  metadata.className = "preview-help";
  metadata.textContent = [
    item.timestamp,
    item.asset_type,
    item.asset_status,
    typeof item.score === "number" ? `類似度 ${item.score.toFixed(4)}` : "",
    item.focus ? `(${item.focus.x.toFixed(3)}, ${item.focus.y.toFixed(3)}, ${item.focus.z.toFixed(3)}) m` : "",
  ].filter(Boolean).join(" / ");
  card.append(heading, type, summary, metadata);
  if (item.thumbnail_url) {
    const image = document.createElement("img");
    image.src = item.thumbnail_url;
    image.alt = item.viewer_url ? `${item.title}の撮影視点を3DGS Viewerで開く` : item.title;
    image.loading = "lazy";
    if (item.viewer_url) {
      const link = document.createElement("a");
      link.className = "frame-analysis-thumbnail-link";
      link.href = resolveViewerUrl(item.viewer_url);
      link.target = "_blank";
      link.rel = "noopener";
      link.appendChild(image);
      card.insertBefore(link, heading);
    } else {
      card.insertBefore(image, heading);
    }
  }
  if (item.viewer_url) {
    const open = button("3DGS Viewerで位置を開く", () => openViewer(item.viewer_url ?? ""));
    open.className = "preview-secondary";
    card.appendChild(open);
  }
  return card;
}

export async function mountSpatialSearchView(host: HTMLElement, options: SpatialSearchViewOptions = {}): Promise<void> {
  host.replaceChildren();
  if (!options.embedded) {
    const heading = document.createElement("div");
    heading.className = "preview-heading";
    heading.innerHTML = "<h1>現場データ検索</h1><p>現場を指定して、設備、位置観測、SceneVersion、文書、登録画像の解析結果を横断検索します。検索範囲は選択した現場内に限定されます。</p>";
    host.appendChild(heading);
  }

  let sites: SiteRecord[] = [];
  if (!options.siteId) {
    try {
      sites = await api<SiteRecord[]>("/api/spatial-search/sites");
    } catch (error) {
      const failed = section("検索を開始できません");
      failed.append(`検索APIまたは現場・設備台帳DBを確認してください: ${String(error)}`);
      host.appendChild(failed);
      return;
    }
  }

  const controls = section("検索条件");
  controls.classList.add("spatial-search-controls");
  const form = document.createElement("div");
  form.className = "preview-form spatial-search-form";
  form.append(
    options.siteId
      ? fixedSiteField(options.siteId)
      : selectField("現場", sites[0]?.site_id ?? "", "site_id", sites.map((site) => ({ value: site.site_id, label: site.name }))),
    field("キーワード", "", "search", "query"),
    selectField("対象", "all", "record_type", [
      { value: "all", label: "すべて" },
      { value: "asset", label: "設備・物品" },
      { value: "observation", label: "位置観測" },
      { value: "document", label: "関連文書" },
      { value: "scene_version", label: "SceneVersion" },
      { value: "frame_analysis", label: "登録画像AI解析" },
    ]),
    field("開始日時", "", "datetime-local", "from"),
    field("終了日時", "", "datetime-local", "to"),
    field("Asset type", "", "text", "asset_type"),
    field("Asset status", "", "text", "asset_status"),
  );
  const spatial = document.createElement("details");
  spatial.className = "spatial-search-bounds";
  const spatialSummary = document.createElement("summary");
  spatialSummary.textContent = "空間範囲（meter、任意）";
  const boundsForm = document.createElement("div");
  boundsForm.className = "preview-form spatial-search-form";
  boundsForm.append(
    field("X min", "", "number", "min_x"),
    field("X max", "", "number", "max_x"),
    field("Y min", "", "number", "min_y"),
    field("Y max", "", "number", "max_y"),
    field("Z min", "", "number", "min_z"),
    field("Z max", "", "number", "max_z"),
  );
  spatial.append(spatialSummary, boundsForm);
  const actions = document.createElement("div");
  actions.className = "preview-inline-actions";
  const status = document.createElement("span");
  status.className = "preview-help";
  status.setAttribute("role", "status");
  status.setAttribute("aria-live", "polite");
  const searchButton = button("検索", () => void executeSearch(false));
  const timelineButton = button("タイムラインを表示", () => void executeSearch(true));
  actions.append(searchButton, timelineButton, status);
  controls.append(form, spatial, actions);
  host.appendChild(controls);

  const imagePanel = section("画像で類似検索");
  const imageHelp = document.createElement("p");
  imageHelp.className = "preview-help";
  imageHelp.textContent = "現場・設備台帳へ登録した画像から派生FAISSインデックスを再構築し、選択した現場の範囲内だけで類似画像を検索します。検索画像は保存しません。";
  const imageInput = document.createElement("input");
  imageInput.type = "file";
  imageInput.accept = "image/jpeg,image/png,image/webp";
  const imageActions = document.createElement("div");
  imageActions.className = "preview-inline-actions";
  const imageStatus = document.createElement("span");
  imageStatus.className = "preview-help";
  imageStatus.setAttribute("role", "status");
  imageStatus.setAttribute("aria-live", "polite");
  const imageResults = document.createElement("div");
  imageResults.className = "field-memory-list spatial-search-list";
  const rebuildButton = button("画像インデックスを再構築", () => void rebuildImageIndex());
  const refreshJobsButton = button("処理状況を更新", () => void refreshImageJobs());
  const imageSearchButton = button("選択画像で検索", () => void executeImageSearch());
  imageActions.append(rebuildButton, refreshJobsButton, imageSearchButton, imageStatus);
  imagePanel.append(imageHelp, imageInput, imageActions, imageResults);
  host.appendChild(imagePanel);

  const output = section("検索結果");
  const list = document.createElement("div");
  list.className = "field-memory-list spatial-search-list";
  output.appendChild(list);
  host.appendChild(output);

  async function rebuildImageIndex(): Promise<void> {
    const siteId = value(form, "site_id");
    if (!siteId) {
      imageStatus.textContent = "先に現場を登録してください。";
      return;
    }
    rebuildButton.disabled = true;
    imageStatus.textContent = "再構築を予約しています…";
    try {
      const job = await api<ImageIndexJob>("/api/spatial-search/image-index/jobs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ site_id: siteId }),
      });
      imageStatus.textContent = `予約しました: ${job.job_id}`;
      await refreshImageJobs();
    } catch (error) {
      imageStatus.textContent = `予約できません: ${String(error)}`;
    } finally {
      rebuildButton.disabled = false;
    }
  }

  async function refreshImageJobs(): Promise<void> {
    const siteId = value(form, "site_id");
    if (!siteId) return;
    refreshJobsButton.disabled = true;
    try {
      const jobs = await api<ImageIndexJob[]>("/api/spatial-search/image-index/jobs/query", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ site_id: siteId }),
      });
      const latest = jobs[0];
      imageStatus.textContent = latest
        ? `最新処理: ${latest.status} / ${latest.output_count}件${latest.failure_reason ? ` / ${latest.failure_reason}` : ""}`
        : "画像インデックスはまだ作成されていません。";
    } catch (error) {
      imageStatus.textContent = `処理状況を取得できません: ${String(error)}`;
    } finally {
      refreshJobsButton.disabled = false;
    }
  }

  async function executeImageSearch(): Promise<void> {
    const siteId = value(form, "site_id");
    const file = imageInput.files?.[0];
    if (!siteId || !file) {
      imageStatus.textContent = "現場と検索画像を選択してください。";
      return;
    }
    imageSearchButton.disabled = true;
    imageStatus.textContent = "画像を検索中…";
    imageResults.replaceChildren();
    try {
      const response = await querySimilarFieldImage<SearchResponse>(siteId, file, 20);
      const results = response.results ?? [];
      imageStatus.textContent = `${results.length}件`;
      if (results.length === 0) {
        imageResults.textContent = "類似する登録画像はありません。";
      } else {
        imageResults.append(...results.map(resultCard));
      }
    } catch (error) {
      imageStatus.textContent = `検索できません: ${String(error)}`;
    } finally {
      imageSearchButton.disabled = false;
    }
  }

  async function executeSearch(timeline: boolean): Promise<void> {
    const siteId = value(form, "site_id");
    if (!siteId) {
      status.textContent = "先に現場を登録してください。";
      return;
    }
    searchButton.disabled = true;
    timelineButton.disabled = true;
    status.textContent = timeline ? "タイムラインを取得中…" : "検索中…";
    list.replaceChildren();
    const type = value(form, "record_type");
    const payload: Record<string, unknown> = { site_id: siteId, query: value(form, "query"), limit: 100 };
    if (type !== "all") payload.record_types = [type];
    const from = value(form, "from");
    const to = value(form, "to");
    if (from) payload.from = new Date(from).toISOString();
    if (to) payload.to = new Date(to).toISOString();
    const assetType = value(form, "asset_type");
    const assetStatus = value(form, "asset_status");
    if (assetType) payload.asset_type = assetType;
    if (assetStatus) payload.status = assetStatus;
    const spatialBounds: { min: Record<string, number>; max: Record<string, number> } = { min: {}, max: {} };
    for (const key of ["min_x", "max_x", "min_y", "max_y", "min_z", "max_z"]) {
      const raw = value(boundsForm, key);
      if (raw) {
        const [edge, axis] = key.split("_") as ["min" | "max", "x" | "y" | "z"];
        spatialBounds[edge][axis] = Number(raw);
      }
    }
    if (Object.keys(spatialBounds.min).length + Object.keys(spatialBounds.max).length > 0) {
      payload.spatial_bounds = spatialBounds;
    }
    try {
      const path = timeline ? "/api/spatial-search/timeline" : "/api/spatial-search/query";
      const response = await api<SearchResponse>(path, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const results = timeline ? response.timeline ?? [] : response.results ?? [];
      status.textContent = `${results.length}件`;
      if (results.length === 0) {
        const empty = document.createElement("p");
        empty.className = "preview-empty-state";
        empty.textContent = "該当する記録はありません。";
        list.appendChild(empty);
      } else {
        list.append(...results.map(resultCard));
      }
    } catch (error) {
      status.textContent = `取得できません: ${String(error)}`;
    } finally {
      searchButton.disabled = false;
      timelineButton.disabled = false;
    }
  }
}
