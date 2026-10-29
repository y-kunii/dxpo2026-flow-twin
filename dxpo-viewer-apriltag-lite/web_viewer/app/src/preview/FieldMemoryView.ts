import { api, type JsonObject, uploadFieldMemoryImage } from "./PreviewApiClient";
import { button, field, section, selectField, stringValue } from "./PreviewUi";

interface FieldMemorySummary {
  schema_version: string;
  counts: Record<string, number>;
}

interface SiteRecord extends JsonObject {
  site_id: string;
  name: string;
  description: string;
}

interface AssetRecord extends JsonObject {
  asset_id: string;
  site_id: string;
  name: string;
  asset_type: string;
  status: string;
  description: string;
}

interface SceneVersionRecord extends JsonObject {
  scene_version_id: string;
  site_id: string;
  scene_package_id: string;
  label: string;
  captured_at?: string | null;
}

interface SiteDetail extends SiteRecord {
  scene_versions: SceneVersionRecord[];
  assets: AssetRecord[];
}

interface InspectionRecord extends JsonObject {
  inspection_id: string;
  site_id: string;
  scene_version_id?: string | null;
  title: string;
  status: "planned" | "in_progress" | "completed" | "cancelled";
  inspector: string;
}

interface CorrectiveActionRecord extends JsonObject {
  corrective_action_id: string;
  issue_id: string;
  summary: string;
  status: "proposed" | "assigned" | "in_progress" | "completed" | "rejected";
  evidence_after_document_id?: string | null;
}

interface IssueRecord extends JsonObject {
  issue_id: string;
  inspection_id: string;
  title: string;
  description: string;
  severity: "low" | "medium" | "high" | "critical";
  status: "open" | "assigned" | "in_progress" | "resolved" | "closed" | "rejected";
  assignee?: string | null;
  corrective_actions?: CorrectiveActionRecord[];
}

function inputValue(container: ParentNode, key: string): string {
  return container.querySelector<HTMLInputElement | HTMLSelectElement>(`[data-field="${key}"]`)?.value.trim() ?? "";
}

function numberValue(container: ParentNode, key: string): number {
  return Number(inputValue(container, key));
}

function statusLine(): HTMLParagraphElement {
  const element = document.createElement("p");
  element.className = "preview-help field-memory-status";
  element.setAttribute("role", "status");
  element.setAttribute("aria-live", "polite");
  return element;
}

function formGrid(...children: HTMLElement[]): HTMLDivElement {
  const form = document.createElement("div");
  form.className = "preview-form field-memory-form";
  form.append(...children);
  return form;
}

function recordCard(titleText: string, entries: Array<[string, string]>): HTMLElement {
  const card = document.createElement("article");
  card.className = "field-memory-card";
  const title = document.createElement("h3");
  title.textContent = titleText;
  const metadata = document.createElement("dl");
  metadata.className = "preview-package-meta";
  for (const [label, value] of entries) {
    const term = document.createElement("dt");
    term.textContent = label;
    const detail = document.createElement("dd");
    detail.textContent = value;
    metadata.append(term, detail);
  }
  card.append(title, metadata);
  return card;
}

export async function mountFieldMemoryView(host: HTMLElement): Promise<void> {
  let selectedSiteId = "";
  let selectedAssetId = "";
  let selectedInspectionId = "";
  let selectedIssueId = "";

  async function render(): Promise<void> {
    host.replaceChildren();
    const heading = document.createElement("div");
    heading.className = "preview-heading";
    heading.innerHTML = "<h1>現場・設備台帳</h1><p>現場、設備、観測、文書、保全履歴をSceneVersionへ関連付けます。MRP、SPZ、Scene Packageは変更しません。</p>";
    host.appendChild(heading);

    let summary: FieldMemorySummary;
    let sites: SiteRecord[];
    try {
      [summary, sites] = await Promise.all([
        api<FieldMemorySummary>("/api/field-memory/summary"),
        api<SiteRecord[]>("/api/field-memory/sites"),
      ]);
    } catch (error) {
      const failed = section("現場・設備台帳を開けません");
      failed.append(`APIまたは専用データベースを確認してください: ${String(error)}`);
      host.appendChild(failed);
      return;
    }

    if (!selectedSiteId || !sites.some((site) => site.site_id === selectedSiteId)) {
      selectedSiteId = sites[0]?.site_id ?? "";
      selectedAssetId = "";
      selectedInspectionId = "";
      selectedIssueId = "";
    }

    const summaryPanel = section("登録状況");
    const summaryGrid = document.createElement("div");
    summaryGrid.className = "field-memory-summary";
    const labels: Record<string, string> = {
      sites: "現場",
      scene_versions: "SceneVersion",
      assets: "設備・物品",
      observations: "位置観測",
      documents: "文書",
    };
    for (const [key, label] of Object.entries(labels)) {
      const item = document.createElement("div");
      item.innerHTML = `<span>${label}</span><strong>${summary.counts[key] ?? 0}</strong>`;
      summaryGrid.appendChild(item);
    }
    summaryPanel.appendChild(summaryGrid);
    host.appendChild(summaryPanel);

    const sitePanel = section("現場を選択・登録");
    const siteSelector = selectField(
      "対象の現場",
      selectedSiteId,
      "selected_site",
      sites.length > 0
        ? sites.map((site) => ({ value: site.site_id, label: `${site.name} (${site.site_id})` }))
        : [{ value: "", label: "まだ現場がありません" }],
    );
    siteSelector.querySelector("select")?.addEventListener("change", (event) => {
      selectedSiteId = (event.currentTarget as HTMLSelectElement).value;
      selectedAssetId = "";
      selectedInspectionId = "";
      selectedIssueId = "";
      void render();
    });
    const siteName = field("現場名", "", "text", "site_name");
    const siteId = field("現場ID（省略時は自動生成）", "", "text", "site_id");
    const siteDescription = field("説明", "", "text", "site_description");
    const siteStatus = statusLine();
    const createSite = button("現場を登録", () => {
      createSite.disabled = true;
      siteStatus.textContent = "登録しています…";
      void api<SiteRecord>("/api/field-memory/sites", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          site_id: inputValue(sitePanel, "site_id") || undefined,
          name: inputValue(sitePanel, "site_name"),
          description: inputValue(sitePanel, "site_description"),
        }),
      }).then((created) => {
        selectedSiteId = created.site_id;
        selectedAssetId = "";
        selectedInspectionId = "";
        selectedIssueId = "";
        void render();
      }).catch((error) => {
        siteStatus.textContent = `登録できません: ${String(error)}`;
        createSite.disabled = false;
      });
    });
    createSite.className = "preview-primary";
    sitePanel.append(siteSelector, formGrid(siteName, siteId, siteDescription), createSite, siteStatus);
    host.appendChild(sitePanel);

    if (!selectedSiteId) return;

    let detail: SiteDetail;
    try {
      detail = await api<SiteDetail>(`/api/field-memory/sites/${encodeURIComponent(selectedSiteId)}`);
    } catch (error) {
      const failed = section("現場を取得できません");
      failed.append(String(error));
      host.appendChild(failed);
      return;
    }

    const columns = document.createElement("div");
    columns.className = "field-memory-columns";
    host.appendChild(columns);

    const scenePanel = section("SceneVersion");
    const sceneHelp = document.createElement("p");
    sceneHelp.className = "preview-help";
    sceneHelp.textContent = "検証済みScene Packageを、現場の特定時点を表す不変SceneVersionとして登録します。";
    let packages: JsonObject[] = [];
    try {
      packages = await api<JsonObject[]>("/api/field-memory/verified-scene-packages");
    } catch {
      // Registration remains disabled; existing records are still readable.
    }
    const packageSelector = selectField(
      "Scene Package",
      "",
      "scene_package_id",
      packages.length > 0
        ? packages.map((item) => ({ value: String(item.scene_package_id), label: `${String(item.project_name ?? item.scene_id ?? "Scene")} — ${String(item.scene_package_id)}` }))
        : [{ value: "", label: "検証済みScene Packageがありません" }],
    );
    const sceneLabel = field("表示名", "", "text", "scene_label");
    const capturedAt = field("撮影日時（ISO 8601・任意）", "", "datetime-local", "captured_at");
    const sceneStatus = statusLine();
    const createScene = button("SceneVersionを登録", () => {
      createScene.disabled = true;
      sceneStatus.textContent = "登録しています…";
      void api<SceneVersionRecord>(`/api/field-memory/sites/${encodeURIComponent(selectedSiteId)}/scene-versions`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          scene_package_id: inputValue(scenePanel, "scene_package_id"),
          label: inputValue(scenePanel, "scene_label") || inputValue(scenePanel, "scene_package_id"),
          captured_at: inputValue(scenePanel, "captured_at") || undefined,
        }),
      }).then(() => void render()).catch((error) => {
        sceneStatus.textContent = `登録できません: ${String(error)}`;
        createScene.disabled = false;
      });
    }, packages.length === 0);
    const sceneList = document.createElement("div");
    sceneList.className = "field-memory-list";
    for (const item of detail.scene_versions) {
      sceneList.appendChild(recordCard(item.label, [
        ["SceneVersion ID", item.scene_version_id],
        ["Scene Package", item.scene_package_id],
        ["撮影日時", stringValue(item.captured_at)],
      ]));
    }
    if (detail.scene_versions.length === 0) sceneList.textContent = "SceneVersionは未登録です。";
    scenePanel.append(sceneHelp, packageSelector, formGrid(sceneLabel, capturedAt), createScene, sceneStatus, sceneList);

    const assetPanel = section("設備・物品");
    const assetName = field("名称", "", "text", "asset_name");
    const assetType = selectField("種別", "equipment", "asset_type", [
      { value: "equipment", label: "設備" },
      { value: "fixture", label: "備品" },
      { value: "area", label: "区画" },
      { value: "other", label: "その他" },
    ]);
    const assetDescription = field("説明", "", "text", "asset_description");
    const assetStatus = statusLine();
    const createAsset = button("設備・物品を登録", () => {
      createAsset.disabled = true;
      assetStatus.textContent = "登録しています…";
      void api<AssetRecord>(`/api/field-memory/sites/${encodeURIComponent(selectedSiteId)}/assets`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name: inputValue(assetPanel, "asset_name"),
          asset_type: inputValue(assetPanel, "asset_type"),
          description: inputValue(assetPanel, "asset_description"),
        }),
      }).then((created) => {
        selectedAssetId = created.asset_id;
        void render();
      }).catch((error) => {
        assetStatus.textContent = `登録できません: ${String(error)}`;
        createAsset.disabled = false;
      });
    });
    const assetList = document.createElement("div");
    assetList.className = "field-memory-list";
    for (const item of detail.assets) {
      const card = recordCard(item.name, [["ID", item.asset_id], ["種別", item.asset_type], ["状態", item.status]]);
      card.classList.toggle("is-selected", item.asset_id === selectedAssetId);
      card.appendChild(button("この設備を編集対象にする", () => {
        selectedAssetId = item.asset_id;
        void render();
      }));
      assetList.appendChild(card);
    }
    if (detail.assets.length === 0) assetList.textContent = "設備・物品は未登録です。";
    assetPanel.append(formGrid(assetName, assetType, assetDescription), createAsset, assetStatus, assetList);
    columns.append(scenePanel, assetPanel);

    const [inspections, issueList] = await Promise.all([
      api<InspectionRecord[]>(`/api/field-memory/sites/${encodeURIComponent(selectedSiteId)}/inspections`),
      api<IssueRecord[]>(`/api/field-memory/sites/${encodeURIComponent(selectedSiteId)}/issues`),
    ]);
    if (!selectedInspectionId || !inspections.some((item) => item.inspection_id === selectedInspectionId)) {
      selectedInspectionId = inspections.find((item) => item.status === "in_progress")?.inspection_id ?? inspections[0]?.inspection_id ?? "";
    }
    if (!selectedIssueId || !issueList.some((item) => item.issue_id === selectedIssueId)) {
      selectedIssueId = issueList.find((item) => !["closed", "rejected"].includes(item.status))?.issue_id ?? issueList[0]?.issue_id ?? "";
    }

    const workflowPanel = section("点検・指摘・是正");
    const workflowHelp = document.createElement("p");
    workflowHelp.className = "preview-help";
    workflowHelp.textContent = "点検開始から指摘、担当、是正前後の証跡、レビュー承認、完了までを監査履歴付きで管理します。承認前の指摘は完了にできません。";
    const workflowColumns = document.createElement("div");
    workflowColumns.className = "field-memory-columns field-workflow-columns";

    const inspectionPanel = document.createElement("div");
    inspectionPanel.className = "field-memory-subpanel";
    inspectionPanel.innerHTML = "<h3>1. 点検</h3>";
    const inspectionTitle = field("点検名", "", "text", "inspection_title");
    const inspector = field("点検担当", "", "text", "inspection_inspector");
    const inspectionScene = selectField(
      "対象SceneVersion", detail.scene_versions[0]?.scene_version_id ?? "", "inspection_scene",
      detail.scene_versions.length > 0
        ? detail.scene_versions.map((item) => ({ value: item.scene_version_id, label: item.label }))
        : [{ value: "", label: "SceneVersion未登録" }],
    );
    const inspectionStatus = statusLine();
    const createInspection = button("点検を作成", () => {
      createInspection.disabled = true;
      void api<InspectionRecord>(`/api/field-memory/sites/${encodeURIComponent(selectedSiteId)}/inspections`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          title: inputValue(inspectionPanel, "inspection_title"),
          inspector: inputValue(inspectionPanel, "inspection_inspector") || "未割当",
          scene_version_id: inputValue(inspectionPanel, "inspection_scene") || undefined,
        }),
      }).then((created) => { selectedInspectionId = created.inspection_id; void render(); })
        .catch((error) => { inspectionStatus.textContent = `作成できません: ${String(error)}`; createInspection.disabled = false; });
    });
    inspectionPanel.append(inspectionTitle, inspector, inspectionScene, createInspection, inspectionStatus);
    const inspectionCards = document.createElement("div");
    inspectionCards.className = "field-memory-list";
    for (const item of inspections) {
      const card = recordCard(item.title, [["状態", item.status], ["担当", item.inspector], ["ID", item.inspection_id]]);
      card.classList.toggle("is-selected", item.inspection_id === selectedInspectionId);
      card.append(button("選択", () => { selectedInspectionId = item.inspection_id; void render(); }));
      const transition = (status: string, reason: string) => {
        void api<InspectionRecord>(`/api/field-memory/inspections/${encodeURIComponent(item.inspection_id)}/transition`, {
          method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ status, reason }),
        }).then(() => void render()).catch((error) => { inspectionStatus.textContent = `状態を変更できません: ${String(error)}`; });
      };
      if (item.status === "planned") card.append(button("点検を開始", () => transition("in_progress", "画面から点検開始")));
      if (item.status === "in_progress") card.append(button("点検を完了", () => transition("completed", "全指摘の完了を確認")));
      inspectionCards.append(card);
    }
    if (inspections.length === 0) inspectionCards.textContent = "点検はまだありません。";
    inspectionPanel.append(inspectionCards);

    const issuePanel = document.createElement("div");
    issuePanel.className = "field-memory-subpanel";
    issuePanel.innerHTML = "<h3>2. 観測・指摘</h3>";
    const activeInspection = inspections.find((item) => item.inspection_id === selectedInspectionId);
    const issueTitle = field("指摘件名", "", "text", "issue_title");
    const issueDescription = field("内容", "", "text", "issue_description");
    const issueSeverity = selectField("重要度", "medium", "issue_severity", [
      { value: "low", label: "低" }, { value: "medium", label: "中" },
      { value: "high", label: "高" }, { value: "critical", label: "重大" },
    ]);
    const issueAssignee = field("担当者", "", "text", "issue_assignee");
    const issueEvidence = field("是正前の文書ID（任意）", "", "text", "issue_evidence_before");
    const issueStatus = statusLine();
    const createIssue = button("非適合観測と指摘を登録", () => {
      if (!activeInspection) return;
      createIssue.disabled = true;
      const createObservationRequest = api<JsonObject>(
        `/api/field-memory/inspections/${encodeURIComponent(activeInspection.inspection_id)}/observations`, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            asset_id: selectedAssetId || undefined, result: "nonconforming",
            notes: inputValue(issuePanel, "issue_description"),
            evidence_document_id: inputValue(issuePanel, "issue_evidence_before") || undefined,
          }),
        },
      );
      void createObservationRequest.then((observation) => api<IssueRecord>(
        `/api/field-memory/sites/${encodeURIComponent(selectedSiteId)}/issues`, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            inspection_id: activeInspection.inspection_id,
            inspection_observation_id: String(observation.inspection_observation_id),
            asset_id: selectedAssetId || undefined,
            scene_version_id: activeInspection.scene_version_id || undefined,
            title: inputValue(issuePanel, "issue_title"), description: inputValue(issuePanel, "issue_description"),
            severity: inputValue(issuePanel, "issue_severity"), assignee: inputValue(issuePanel, "issue_assignee") || undefined,
            evidence_before_document_id: inputValue(issuePanel, "issue_evidence_before") || undefined,
          }),
        },
      )).then((created) => { selectedIssueId = created.issue_id; void render(); })
        .catch((error) => { issueStatus.textContent = `登録できません: ${String(error)}`; createIssue.disabled = false; });
    }, activeInspection?.status !== "in_progress");
    issuePanel.append(
      activeInspection ? document.createTextNode(`選択中: ${activeInspection.title}`) : document.createTextNode("点検を選択してください。"),
      issueTitle, issueDescription, issueSeverity, issueAssignee, issueEvidence, createIssue, issueStatus,
    );
    const issueCards = document.createElement("div");
    issueCards.className = "field-memory-list";
    for (const item of issueList) {
      const card = recordCard(item.title, [["重要度", item.severity], ["状態", item.status], ["担当", item.assignee ?? "未割当"]]);
      card.classList.toggle("is-selected", item.issue_id === selectedIssueId);
      card.append(button("選択", () => { selectedIssueId = item.issue_id; void render(); }));
      issueCards.append(card);
    }
    if (issueList.length === 0) issueCards.textContent = "指摘はまだありません。";
    issuePanel.append(issueCards);

    const correctionPanel = document.createElement("div");
    correctionPanel.className = "field-memory-subpanel";
    correctionPanel.innerHTML = "<h3>3. 是正・承認</h3>";
    const selectedIssue = selectedIssueId
      ? await api<IssueRecord>(`/api/field-memory/issues/${encodeURIComponent(selectedIssueId)}`)
      : null;
    const correctionSummary = field("是正内容", "", "text", "correction_summary");
    const correctionAssignee = field("是正担当", "", "text", "correction_assignee");
    const correctionEvidence = field("是正後の文書ID", "", "text", "correction_evidence_after");
    const correctionReason = field("状態変更・承認理由", "", "text", "correction_reason");
    const correctionStatus = statusLine();
    const createCorrection = button("是正処置を登録", () => {
      if (!selectedIssue) return;
      void api<CorrectiveActionRecord>(`/api/field-memory/issues/${encodeURIComponent(selectedIssue.issue_id)}/actions`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({
          summary: inputValue(correctionPanel, "correction_summary"),
          assigned_to: inputValue(correctionPanel, "correction_assignee") || undefined,
        }),
      }).then(() => void render()).catch((error) => { correctionStatus.textContent = `登録できません: ${String(error)}`; });
    }, !selectedIssue || ["closed", "rejected"].includes(selectedIssue.status));
    correctionPanel.append(
      selectedIssue ? document.createTextNode(`選択中: ${selectedIssue.title}`) : document.createTextNode("指摘を選択してください。"),
      correctionSummary, correctionAssignee, correctionEvidence, correctionReason, createCorrection, correctionStatus,
    );
    for (const action of selectedIssue?.corrective_actions ?? []) {
      const card = recordCard(action.summary, [["状態", action.status], ["ID", action.corrective_action_id]]);
      const actionTransition = (status: string) => {
        void api<CorrectiveActionRecord>(`/api/field-memory/corrective-actions/${encodeURIComponent(action.corrective_action_id)}/transition`, {
          method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({
            status, reason: inputValue(correctionPanel, "correction_reason") || "画面操作",
            evidence_after_document_id: inputValue(correctionPanel, "correction_evidence_after") || undefined,
          }),
        }).then(() => void render()).catch((error) => { correctionStatus.textContent = `状態を変更できません: ${String(error)}`; });
      };
      if (["proposed", "assigned"].includes(action.status)) card.append(button("作業開始", () => actionTransition("in_progress")));
      if (action.status === "in_progress") card.append(button("証跡付きで完了", () => actionTransition("completed")));
      if (action.status === "completed") card.append(button("レビュー承認", () => {
        void api<CorrectiveActionRecord>(`/api/field-memory/corrective-actions/${encodeURIComponent(action.corrective_action_id)}/approvals`, {
          method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({
            decision: "approved", reason: inputValue(correctionPanel, "correction_reason") || "証跡を確認",
          }),
        }).then(() => void render()).catch((error) => { correctionStatus.textContent = `承認できません: ${String(error)}`; });
      }));
      correctionPanel.append(card);
    }
    if (selectedIssue && !["closed", "rejected"].includes(selectedIssue.status)) {
      const issueTransition = (status: string) => {
        void api<IssueRecord>(`/api/field-memory/issues/${encodeURIComponent(selectedIssue.issue_id)}/transition`, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ status, reason: inputValue(correctionPanel, "correction_reason") || "画面操作" }),
        }).then(() => void render()).catch((error) => { correctionStatus.textContent = `指摘を更新できません: ${String(error)}`; });
      };
      if (["open", "assigned"].includes(selectedIssue.status)) correctionPanel.append(button("対応開始", () => issueTransition("in_progress")));
      if (selectedIssue.status === "in_progress") correctionPanel.append(button("解決済みにする", () => issueTransition("resolved")));
      if (selectedIssue.status === "resolved") correctionPanel.append(button("承認済みとして閉じる", () => issueTransition("closed")));
    }
    workflowColumns.append(inspectionPanel, issuePanel, correctionPanel);
    workflowPanel.append(workflowHelp, workflowColumns);

    const reportPanel = section("報告書");
    const reportStatus = statusLine();
    const reportLinks = document.createElement("div");
    reportLinks.className = "field-report-links";
    const createReport = button("PDF・CSV報告書を生成", () => {
      createReport.disabled = true;
      reportStatus.textContent = "報告書を生成しています…";
      void api<JsonObject>(`/api/field-memory/sites/${encodeURIComponent(selectedSiteId)}/reports`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: "{}",
      }).then((report) => {
        const downloads = Array.isArray(report.downloads) ? report.downloads : [];
        for (const item of downloads) {
          if (!item || typeof item !== "object") continue;
          const link = document.createElement("a");
          link.href = String((item as JsonObject).url);
          link.textContent = `${String((item as JsonObject).filename)} をダウンロード`;
          reportLinks.append(link);
        }
        reportStatus.textContent = `生成完了: ${String(report.report_id)}`;
      }).catch((error) => { reportStatus.textContent = `生成できません: ${String(error)}`; createReport.disabled = false; });
    });
    reportPanel.append(
      document.createTextNode("SceneVersion、点検、AIモデル、証跡参照、レコード集合SHA-256を記録します。"),
      createReport, reportStatus, reportLinks,
    );
    host.append(workflowPanel, reportPanel);

    if (!selectedAssetId || !detail.assets.some((asset) => asset.asset_id === selectedAssetId)) return;
    const selectedAsset = detail.assets.find((asset) => asset.asset_id === selectedAssetId)!;
    const evidencePanel = section(`設備の証跡: ${selectedAsset.name}`);
    const evidenceColumns = document.createElement("div");
    evidenceColumns.className = "field-memory-columns";

    const observation = document.createElement("div");
    observation.className = "field-memory-subpanel";
    observation.innerHTML = "<h3>SceneVersion上の位置</h3>";
    const sceneVersionSelector = selectField(
      "SceneVersion",
      detail.scene_versions[0]?.scene_version_id ?? "",
      "observation_scene_version",
      detail.scene_versions.length > 0
        ? detail.scene_versions.map((item) => ({ value: item.scene_version_id, label: item.label }))
        : [{ value: "", label: "SceneVersionを先に登録してください" }],
    );
    const poseFields = formGrid(
      field("X (m)", "0", "number", "position_x"), field("Y (m)", "0", "number", "position_y"), field("Z (m)", "0", "number", "position_z"),
      field("Quaternion X", "0", "number", "rotation_x"), field("Quaternion Y", "0", "number", "rotation_y"), field("Quaternion Z", "0", "number", "rotation_z"), field("Quaternion W", "1", "number", "rotation_w"),
    );
    poseFields.querySelectorAll<HTMLInputElement>('input[type="number"]').forEach((input) => { input.step = "any"; });
    const observationStatus = statusLine();
    const createObservation = button("位置を記録", () => {
      createObservation.disabled = true;
      observationStatus.textContent = "記録しています…";
      void api<JsonObject>(`/api/field-memory/assets/${encodeURIComponent(selectedAssetId)}/observations`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          scene_version_id: inputValue(observation, "observation_scene_version"),
          pose: {
            position: { x: numberValue(observation, "position_x"), y: numberValue(observation, "position_y"), z: numberValue(observation, "position_z") },
            rotation: { x: numberValue(observation, "rotation_x"), y: numberValue(observation, "rotation_y"), z: numberValue(observation, "rotation_z"), w: numberValue(observation, "rotation_w") },
          },
          source: "manual",
        }),
      }).then(() => void render()).catch((error) => {
        observationStatus.textContent = `記録できません: ${String(error)}`;
        createObservation.disabled = false;
      });
    }, detail.scene_versions.length === 0);
    const observations = await api<JsonObject[]>(`/api/field-memory/assets/${encodeURIComponent(selectedAssetId)}/observations`);
    observation.append(sceneVersionSelector, poseFields, createObservation, observationStatus, document.createTextNode(`記録済み: ${observations.length}件`));

    const documentPanel = document.createElement("div");
    documentPanel.className = "field-memory-subpanel";
    documentPanel.innerHTML = "<h3>関連文書</h3>";
    const documentFields = formGrid(
      selectField("種別", "manual", "document_kind", [
        { value: "manual", label: "取扱説明書" },
        { value: "inspection", label: "点検記録" },
        { value: "drawing", label: "図面" },
        { value: "other", label: "その他" },
      ]),
      field("文書名", "", "text", "document_title"),
      field("URLまたは管理パス", "", "text", "document_uri"),
    );
    const documentStatus = statusLine();
    const createDocument = button("文書メタデータを登録", () => {
      createDocument.disabled = true;
      documentStatus.textContent = "登録しています…";
      void api<JsonObject>(`/api/field-memory/assets/${encodeURIComponent(selectedAssetId)}/documents`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          kind: inputValue(documentPanel, "document_kind"),
          title: inputValue(documentPanel, "document_title"),
          uri: inputValue(documentPanel, "document_uri"),
        }),
      }).then(() => void render()).catch((error) => {
        documentStatus.textContent = `登録できません: ${String(error)}`;
        createDocument.disabled = false;
      });
    });
    const documents = await api<JsonObject[]>(`/api/field-memory/assets/${encodeURIComponent(selectedAssetId)}/documents`);
    const imageHeading = document.createElement("h3");
    imageHeading.textContent = "画像証跡";
    const imageHelp = document.createElement("p");
    imageHelp.className = "preview-help";
    imageHelp.textContent = "JPEG・PNG・WebP（20 MiB以下）を内容ハッシュで重複排除して保存します。画像特徴量は別ワーカーが派生データとして生成します。";
    const imageInput = document.createElement("input");
    imageInput.type = "file";
    imageInput.accept = "image/jpeg,image/png,image/webp";
    const imageTitle = field("画像名（任意）", "", "text", "image_title");
    const imageStatus = statusLine();
    const uploadImage = button("画像を登録", () => {
      const file = imageInput.files?.[0];
      if (!file) {
        imageStatus.textContent = "画像を選択してください。";
        return;
      }
      uploadImage.disabled = true;
      imageStatus.textContent = "保存・検証しています…";
      void uploadFieldMemoryImage(selectedAssetId, file, inputValue(documentPanel, "image_title"))
        .then(() => void render())
        .catch((error) => {
          imageStatus.textContent = `登録できません: ${String(error)}`;
          uploadImage.disabled = false;
        });
    });
    documentPanel.append(
      documentFields,
      createDocument,
      documentStatus,
      document.createTextNode(`登録済み: ${documents.length}件`),
      imageHeading,
      imageHelp,
      imageTitle,
      imageInput,
      uploadImage,
      imageStatus,
    );
    evidenceColumns.append(observation, documentPanel);
    evidencePanel.appendChild(evidenceColumns);
    host.appendChild(evidencePanel);
  }

  await render();
}
