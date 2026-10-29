import { api, type JsonObject } from "./PreviewApiClient";
import { button, byteSize, lifecycleLabel, pathLeaf, section, statusBadge, stringValue } from "./PreviewUi";

export async function mountProjectWorkspaceView(
content: HTMLElement,
onRegisterSpz: () => void,
): Promise<void> {
  content.replaceChildren();
  const heading = document.createElement("div");
  heading.className = "preview-heading";
  const title = document.createElement("h1");
  title.textContent = "処理履歴・成果物管理";
  const description = document.createElement("p");
  description.textContent = "COLMAP\u51e6\u7406\u30d7\u30ed\u30b8\u30a7\u30af\u30c8\u306e\u5b9f\u884c\u72b6\u614b\u30fb\u5bb9\u91cf\u30fbMRP\u30fbScene Package\u3092\u78ba\u8a8d\u3067\u304d\u307e\u3059\u3002\u524a\u9664\u306f\u54c1\u8cea\u4e0d\u5408\u683c\u304b\u3064\u672a\u53c2\u7167\u306eProject\u3060\u3051\u306b\u9650\u5b9a\u3055\u308c\u307e\u3059\u3002";
  heading.append(title, description);

  const panel = section("COLMAP処理プロジェクト");
  const refresh = button("\u4e00\u89a7\u3092\u66f4\u65b0", () => void mountProjectWorkspaceView(content, onRegisterSpz));
  refresh.className = "preview-secondary";
  const storageStatus = document.createElement("p");
  storageStatus.className = "preview-package-note";
  storageStatus.textContent = "ストレージ容量を確認中…";
  const cleanupActions = document.createElement("div");
  cleanupActions.className = "preview-actions";
  const cleanupStatus = document.createElement("p");
  cleanupStatus.className = "preview-inline-status";
  const executeCleanup = button("候補を削除", () => undefined, true);
  executeCleanup.className = "preview-danger";
  const previewCleanup = button("整理候補を確認", () => {
    previewCleanup.disabled = true;
    executeCleanup.disabled = true;
    cleanupStatus.textContent = "再生成可能なデータだけを確認中…";
    void api<JsonObject>("/api/storage/cleanup-preview", { method: "POST" })
      .then((result) => {
        const count = Number(result.candidate_count ?? 0);
        cleanupStatus.textContent = count > 0
          ? `${count}件（${byteSize(result.candidate_bytes)}）が整理対象です。MRP・SPZ・Scene Package・Projectは含みません。`
          : "保持期限を超えた整理対象はありません。";
        executeCleanup.disabled = count === 0;
      })
      .catch((error) => { cleanupStatus.textContent = `整理候補を取得できません: ${String(error)}`; })
      .finally(() => { previewCleanup.disabled = false; });
  });
  executeCleanup.onclick = () => {
    if (!window.confirm("表示した再生成可能データだけを削除します。続行しますか？")) return;
    executeCleanup.disabled = true;
    cleanupStatus.textContent = "整理中…";
    void api<JsonObject>("/api/storage/cleanup", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ confirm: true }),
    }).then((result) => {
      cleanupStatus.textContent = `${String(result.removed_count ?? 0)}件（${byteSize(result.removed_bytes)}）を整理しました。`;
      void mountProjectWorkspaceView(content, onRegisterSpz);
    }).catch((error) => {
      cleanupStatus.textContent = `整理できません: ${String(error)}`;
      executeCleanup.disabled = false;
    });
  };
  cleanupActions.append(previewCleanup, executeCleanup, cleanupStatus);
  const list = document.createElement("div");
  list.className = "preview-package-list";
  panel.append(refresh, storageStatus, cleanupActions, list);
  content.append(heading, panel);

  try {
    const [assets, workspaces, storage] = await Promise.all([
      api<JsonObject[]>("/api/projects"),
      api<JsonObject[]>("/api/project-workspaces"),
      api<JsonObject>("/api/storage"),
    ]);
    const freeBytes = byteSize(storage.free_bytes);
    const minimumBytes = byteSize(storage.minimum_free_bytes);
    storageStatus.textContent = storage.status === "verified"
      ? `ストレージ: 空き ${freeBytes}（処理用に最低 ${minimumBytes} を予約）`
      : `ストレージ不足: 空き ${freeBytes}。最低 ${minimumBytes} を確保するまでアップロードと新規処理を開始できません。`;
    const assetsByName = new Map<string, JsonObject>();
    const workspacesByName = new Map<string, JsonObject>();
    for (const asset of assets) {
      const projectName = stringValue(asset.project_name, stringValue(asset.project_id, ""));
      if (projectName) assetsByName.set(projectName, asset);
    }
    for (const workspace of workspaces) {
      const projectName = stringValue(workspace.project_name, "");
      if (projectName) workspacesByName.set(projectName, workspace);
    }
    const projectNames = [...new Set([...assetsByName.keys(), ...workspacesByName.keys()])].sort();
    if (projectNames.length === 0) {
      const empty = document.createElement("p");
      empty.className = "preview-empty-state";
      empty.textContent = "COLMAP\u51e6\u7406\u30d7\u30ed\u30b8\u30a7\u30af\u30c8\u306f\u307e\u3060\u3042\u308a\u307e\u305b\u3093\u3002";
      list.appendChild(empty);
      return;
    }

    for (const projectName of projectNames) {
      const asset = assetsByName.get(projectName);
      const workspace = workspacesByName.get(projectName);
      const lifecycle = stringValue(workspace?.lifecycle, stringValue(asset?.status, "review"));
      const packages: JsonObject[] = Array.isArray(asset?.spz_packages)
        ? asset.spz_packages.filter((entry): entry is JsonObject => entry !== null && typeof entry === "object" && !Array.isArray(entry))
        : [];
      const card = document.createElement("article");
      card.className = "preview-package-card";
      const cardTitle = document.createElement("h3");
      cardTitle.textContent = projectName;
      const state = statusBadge(lifecycle);
      state.textContent = lifecycleLabel(lifecycle);
      cardTitle.append(" ", state);

      const metadata = document.createElement("dl");
      metadata.className = "preview-package-meta";
      const jobCount = Array.isArray(workspace?.job_ids) ? workspace.job_ids.length : 0;
      const entries: Array<[string, string]> = [
        ["\u5b9f\u884c\u72b6\u614b", lifecycleLabel(lifecycle)],
        ["\u4f5c\u696d\u30d5\u30a9\u30eb\u30c0\u5bb9\u91cf", byteSize(workspace?.size_bytes)],
        ["MRP", pathLeaf(asset?.mrp_uri)],
        ["LichtFeld input", pathLeaf(asset?.lfs_input_uri)],
        ["Scene Package", String(packages.length) + "\u4ef6"],
        ["\u30b8\u30e7\u30d6", String(jobCount) + "\u4ef6"],
        ["\u4f5c\u696d\u30d5\u30a9\u30eb\u30c0", stringValue(workspace?.project_uri, stringValue(asset?.source_workspace_uri))],
      ];
      for (const [label, value] of entries) {
        const term = document.createElement("dt");
        term.textContent = label;
        const detail = document.createElement("dd");
        detail.textContent = value;
        metadata.append(term, detail);
      }

      const note = document.createElement("p");
      note.className = "preview-package-note";
      note.textContent = stringValue(workspace?.retention_reason, "\u54c1\u8cea\u307e\u305f\u306f\u53c2\u7167\u72b6\u614b\u306e\u78ba\u8a8d\u60c5\u5831\u306f\u3042\u308a\u307e\u305b\u3093\u3002");
      card.append(cardTitle, metadata, note);

      if (packages.length > 0) {
        const packageList = document.createElement("div");
        packageList.className = "preview-project-packages";
        for (const scenePackage of packages) {
          const item = document.createElement("div");
          const packageId = stringValue(scenePackage.scene_package_id, "\u540d\u79f0\u672a\u8a2d\u5b9a\u306eScene Package");
          const packageState = stringValue(scenePackage.status, "unknown");
          const packageLabel = document.createElement("span");
          packageLabel.textContent = packageId;
          const packageStatus = statusBadge(packageState);
          const viewerUrl = stringValue(scenePackage.viewer_url, "");
          const canOpen = packageState === "verified" && scenePackage.published === true && Boolean(viewerUrl);
          const open = button("\u958b\u304f", () => { window.location.href = viewerUrl; }, !canOpen);
          item.append(packageLabel, packageStatus, open);
          packageList.appendChild(item);
        }
        card.appendChild(packageList);
      }

      const actions = document.createElement("div");
      actions.className = "preview-actions";
      actions.append(button("3DGS\u30c7\u30fc\u30bf\u3092\u767b\u9332", onRegisterSpz, !asset));
      if (workspace?.removable === true) {
        const removalStatus = document.createElement("p");
        removalStatus.className = "preview-inline-status";
        const remove = button("\u4e0d\u5408\u683cProject\u3092\u5b8c\u5168\u524a\u9664", () => {
          if (!window.confirm("\u300c" + projectName + "\u300d\u3092\u5b8c\u5168\u524a\u9664\u3057\u307e\u3059\u3002MRP\u3001Scene Package\u3001\u5143\u52d5\u753b\u306f\u542b\u307e\u308c\u307e\u305b\u3093\u3002\u7d9a\u884c\u3057\u307e\u3059\u304b\u3002")) return;
          remove.disabled = true;
          removalStatus.textContent = "\u524a\u9664\u4e2d\u2026";
          void api<JsonObject>("/api/project-workspaces/" + encodeURIComponent(projectName), { method: "DELETE" })
            .then((result) => {
              removalStatus.textContent = byteSize(result.freed_bytes) + " \u3092\u89e3\u653e\u3057\u307e\u3057\u305f\u3002";
              void mountProjectWorkspaceView(content, onRegisterSpz);
            })
            .catch((error) => {
              removalStatus.textContent = "\u524a\u9664\u3067\u304d\u307e\u305b\u3093: " + String(error);
              remove.disabled = false;
            });
        });
        remove.className = "preview-danger";
        actions.append(remove, removalStatus);
      }
      card.append(actions);
      list.appendChild(card);
    }
  } catch (error) {
    const failed = document.createElement("p");
    failed.className = "preview-empty-state";
    failed.textContent = "Project\u4e00\u89a7\u3092\u53d6\u5f97\u3067\u304d\u307e\u305b\u3093: " + String(error);
    list.appendChild(failed);
  }
}
