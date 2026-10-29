import { api, type JsonObject } from "./PreviewApiClient";
import { button, byteSize } from "./PreviewUi";


const CATEGORY_LABELS: Record<string, string> = {
  scene_package: "Scene Package",
  metric_reconstruction: "MRP",
  source_asset: "入力原本",
  site_records: "Site Records",
  deliverable: "配布物",
  viewer_runtime_orphan: "Viewer孤立コピー",
};

const STATE_LABELS: Record<string, string> = {
  current: "現行",
  legacy_superseded: "旧式・移行済み",
  legacy_without_replacement: "旧式・代替なし",
  referenced: "参照中",
  unreferenced: "未参照",
  retained_source: "原本保護",
  active_registry: "稼働中台帳",
  regenerable: "再生成可能",
  orphaned_derived_copy: "孤立コピー",
};

const PROTECTION_LABELS: Record<string, string> = {
  field_memory_reference_status_unknown: "Field Memory参照を確認できません",
  referenced_by_field_memory: "Field Memoryから参照中",
  canonical_path_invalid: "正本パスが不正または不足",
  referenced_by_scene_package: "Scene Packageから参照中",
  explicit_source_release_workflow_required: "入力原本は明示的な保管解除が必要",
  registry_backup_and_release_workflow_required: "台帳はバックアップと保管解除が必要",
};

function objectItems(value: unknown): JsonObject[] {
  if (!Array.isArray(value)) return [];
  return value.filter(
    (item): item is JsonObject => item !== null && typeof item === "object" && !Array.isArray(item),
  );
}

export function mountRepositoryDataManager(host: HTMLElement, onChanged: () => void): void {
  const description = document.createElement("p");
  description.textContent = "Scene Package、MRP、入力原本、Site Records、配布物を容量と参照状態付きで確認できます。削除操作は正本を直接消去せず、復旧可能なRepositoryゴミ箱へ退避します。";
  const summary = document.createElement("p");
  summary.className = "preview-package-note";
  summary.textContent = "Repositoryを読み込み中…";
  const controls = document.createElement("div");
  controls.className = "preview-repository-controls";
  const categoryFilter = document.createElement("select");
  categoryFilter.title = "データ種別で絞り込み";
  const list = document.createElement("div");
  list.className = "preview-repository-items";
  const selectionStatus = document.createElement("p");
  selectionStatus.className = "preview-inline-status";
  const selectedIds = new Set<string>();
  let inventoryItems: JsonObject[] = [];
  let confirmationToken = "";
  let previewedIds: string[] = [];

  const executeSelection = button("選択項目を一括退避（削除）", () => undefined, true);
  executeSelection.className = "preview-danger";

  const invalidatePreview = (): void => {
    confirmationToken = "";
    previewedIds = [];
    executeSelection.disabled = true;
  };

  const selectedItems = (): JsonObject[] => inventoryItems.filter(
    (item) => selectedIds.has(String(item.item_id ?? "")),
  );

  const updateSelectionStatus = (): void => {
    const selected = selectedItems();
    const bytes = selected.reduce((total, item) => total + Number(item.size_bytes ?? 0), 0);
    selectionStatus.textContent = selected.length > 0
      ? `選択中: ${selected.length}件 / ${byteSize(bytes)}`
      : "削除する項目を選択してください。保護中の項目は選択できません。";
  };

  const renderItems = (): void => {
    list.replaceChildren();
    const selectedCategory = categoryFilter.value;
    const visible = inventoryItems.filter(
      (item) => selectedCategory === "all" || item.category === selectedCategory,
    );
    if (visible.length === 0) {
      const empty = document.createElement("p");
      empty.className = "preview-empty-state";
      empty.textContent = "該当するデータはありません。";
      list.appendChild(empty);
    }
    for (const item of visible) {
      const row = document.createElement("label");
      row.className = `preview-repository-item${item.recommended_cleanup === true ? " preview-repository-item-recommended" : ""}`;
      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      const itemId = String(item.item_id ?? "");
      checkbox.checked = selectedIds.has(itemId);
      checkbox.disabled = item.removable !== true;
      checkbox.addEventListener("change", () => {
        if (checkbox.checked) selectedIds.add(itemId);
        else selectedIds.delete(itemId);
        invalidatePreview();
        updateSelectionStatus();
      });

      const body = document.createElement("span");
      body.className = "preview-repository-item-body";
      const titleLine = document.createElement("span");
      titleLine.className = "preview-repository-item-title";
      const title = document.createElement("strong");
      title.textContent = String(item.label ?? itemId);
      const badge = document.createElement("span");
      badge.className = "preview-repository-badge";
      badge.textContent = `${CATEGORY_LABELS[String(item.category)] ?? String(item.category)} / ${STATE_LABELS[String(item.state)] ?? String(item.state)}`;
      titleLine.append(title, badge);
      const detail = document.createElement("span");
      detail.className = "preview-repository-item-detail";
      detail.textContent = `${byteSize(item.size_bytes)} — ${String(item.path ?? "—")}`;
      body.append(titleLine, detail);

      const reasons = Array.isArray(item.protection_reasons)
        ? item.protection_reasons.map((reason) => PROTECTION_LABELS[String(reason)] ?? String(reason))
        : [];
      if (item.recommended_cleanup === true) {
        const recommendation = document.createElement("span");
        recommendation.className = "preview-repository-recommendation";
        recommendation.textContent = "安全な整理候補: 自己完結版へ移行済み";
        body.appendChild(recommendation);
      } else if (reasons.length > 0) {
        const protectedReason = document.createElement("span");
        protectedReason.className = "preview-repository-protection";
        protectedReason.textContent = `保護: ${reasons.join("、")}`;
        body.appendChild(protectedReason);
      }
      row.append(checkbox, body);
      list.appendChild(row);
    }
    updateSelectionStatus();
  };

  const selectRecommended = button("安全な旧式候補をすべて選択", () => {
    selectedIds.clear();
    for (const item of inventoryItems) {
      if (item.recommended_cleanup === true && item.removable === true) {
        selectedIds.add(String(item.item_id));
      }
    }
    invalidatePreview();
    renderItems();
  });
  const selectVisible = button("表示中の削除可能項目をすべて選択", () => {
    for (const item of inventoryItems) {
      if ((categoryFilter.value === "all" || item.category === categoryFilter.value) && item.removable === true) {
        selectedIds.add(String(item.item_id));
      }
    }
    invalidatePreview();
    renderItems();
  });
  const clearSelection = button("選択解除", () => {
    selectedIds.clear();
    invalidatePreview();
    renderItems();
  });
  selectRecommended.className = "preview-secondary";
  selectVisible.className = "preview-secondary";
  clearSelection.className = "preview-secondary";

  const previewSelection = button("削除内容を確認", () => {
    const itemIds = [...selectedIds].sort();
    if (itemIds.length === 0) {
      selectionStatus.textContent = "項目が選択されていません。";
      return;
    }
    previewSelection.disabled = true;
    executeSelection.disabled = true;
    selectionStatus.textContent = "参照関係を再確認中…";
    void api<JsonObject>("/api/repository/cleanup-preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ item_ids: itemIds }),
    }).then((result) => {
      if (result.ready !== true || typeof result.confirmation_token !== "string") {
        selectionStatus.textContent = `削除できない項目が${String(result.blocked_count ?? 0)}件あります。一覧を更新してください。`;
        return;
      }
      confirmationToken = result.confirmation_token;
      previewedIds = itemIds;
      selectionStatus.textContent = `確認済み: ${String(result.selected_count)}件 / ${byteSize(result.selected_bytes)}。実行するとRepositoryゴミ箱へ退避します。`;
      executeSelection.disabled = false;
    }).catch((error) => {
      selectionStatus.textContent = `削除内容を確認できません: ${String(error)}`;
    }).finally(() => { previewSelection.disabled = false; });
  });

  executeSelection.onclick = () => {
    if (!confirmationToken || previewedIds.length === 0) return;
    const bytes = selectedItems().reduce((total, item) => total + Number(item.size_bytes ?? 0), 0);
    if (!window.confirm(`${previewedIds.length}件（${byteSize(bytes)}）を一覧から削除し、復旧可能なゴミ箱へ退避します。続行しますか？`)) return;
    executeSelection.disabled = true;
    selectionStatus.textContent = "選択項目を退避中…";
    void api<JsonObject>("/api/repository/cleanup", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        confirm: true,
        item_ids: previewedIds,
        confirmation_token: confirmationToken,
      }),
    }).then((result) => {
      window.alert(`${String(result.removed_count ?? 0)}件を退避しました。退避先: ${String(result.trashed_uri ?? "—")}`);
      onChanged();
    }).catch((error) => {
      selectionStatus.textContent = `退避できません: ${String(error)}`;
      invalidatePreview();
    });
  };

  categoryFilter.addEventListener("change", renderItems);
  controls.append(
    categoryFilter,
    selectRecommended,
    selectVisible,
    clearSelection,
    previewSelection,
    executeSelection,
  );
  host.append(description, summary, controls, selectionStatus, list);

  void api<JsonObject>("/api/repository/items")
    .then((inventory) => {
      inventoryItems = objectItems(inventory.items);
      const inventorySummary = (inventory.summary as JsonObject | undefined) ?? {};
      summary.textContent = `管理対象 ${String(inventorySummary.item_count ?? 0)}件 / ${byteSize(inventorySummary.size_bytes)}。安全な旧式整理候補 ${String(inventorySummary.recommended_count ?? 0)}件 / ${byteSize(inventorySummary.recommended_bytes)}。`;
      categoryFilter.replaceChildren();
      const all = document.createElement("option");
      all.value = "all";
      all.textContent = "すべてのデータ";
      categoryFilter.appendChild(all);
      const categories = [...new Set(
        inventoryItems.map((item) => String(item.category ?? "unknown")),
      )].sort();
      for (const category of categories) {
        const option = document.createElement("option");
        option.value = category;
        option.textContent = CATEGORY_LABELS[category] ?? category;
        categoryFilter.appendChild(option);
      }
      renderItems();
    })
    .catch((error) => {
      summary.textContent = `Repositoryデータ一覧を取得できません: ${String(error)}`;
    });
}
