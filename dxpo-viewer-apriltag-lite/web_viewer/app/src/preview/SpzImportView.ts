import { api, type JsonObject } from "./PreviewApiClient";
import { button, section } from "./PreviewUi";

export async function mountSpzImportView(content: HTMLElement): Promise<void> {
  content.replaceChildren();
  const heading = document.createElement("div");
  heading.className = "preview-heading";
  heading.innerHTML = "<h1>3DGSデータ登録</h1><p>LichtFeld Studioなどで作成したSPZ・Gaussian PLY・RADと、学習元MRPを対応付けて自己完結Scene Packageへ登録します。</p>";
  content.appendChild(heading);
  const panel = section("取込ウィザード");
  const grid = document.createElement("div");
  grid.className = "preview-import-grid";
  const rows: Array<[string, string]> = [
    ["対象", "COLMAP処理プロジェクトのMRPを選択"],
    ["MRP", "選択後に検証"],
    ["coordinate stage", "reported after MRP validation"],
    ["unit", "meter"],
    ["up axis", "Y"],
    ["Gaussian asset", "SPZ / standard 3DGS PLY / Spark RAD"],
    ["検証", "形式・件数・有限値またはchunk範囲・SHA-256"],
  ];
  for (const [label, value] of rows) {
    const row = document.createElement("div");
    row.innerHTML = `<span>${label}</span><strong>${value}</strong>`;
    grid.appendChild(row);
  }
  panel.appendChild(grid);
  const importPanel = document.createElement("div");
  importPanel.className = "preview-import-panel";
  const importTitle = document.createElement("h3");
  importTitle.textContent = "Gaussian assetとMRPを対応付ける";
  const importNote = document.createElement("p");
  importNote.textContent = "assetを学習した入力と同じCOLMAP処理プロジェクトのMRPを選び、assetの座標契約を明示します。対応や座標変換が不明なassetは登録しないでください。";
  const steps = document.createElement("ol");
  steps.className = "preview-import-steps";
  const stepLabels = ["3DGSファイルを選ぶ", "学習元のMRPと座標契約を選ぶ", "MRPを検証する", "登録してViewerを開く"];
  const stepItems = stepLabels.map((label, index) => {
    const item = document.createElement("li");
    item.textContent = `${index + 1}. ${label}`;
    item.dataset.step = String(index + 1);
    steps.appendChild(item);
    return item;
  });
  const setStep = (step: number): void => {
    stepItems.forEach((item, index) => {
      item.classList.toggle("is-current", index + 1 === step);
      item.classList.toggle("is-complete", index + 1 < step);
    });
  };
  setStep(1);

  const spzStep = document.createElement("div");
  spzStep.className = "preview-import-step";
  const spzLabel = document.createElement("label");
  spzLabel.textContent = "1. 学習済み3DGSファイル";
  const spzHelp = document.createElement("p");
  spzHelp.textContent = "LichtFeld Studioなどで書き出した .spz、standard 3DGS .ply、Spark .rad を選択します。ここではまだ登録されません。";
  const spzFile = document.createElement("input");
  spzFile.type = "file";
  spzFile.accept = ".spz,.ply,.rad,application/gzip,application/octet-stream";
  spzFile.setAttribute("aria-label", "Gaussian asset file");
  const selectedSpz = document.createElement("div");
  selectedSpz.className = "preview-selection-summary";
  selectedSpz.textContent = "3DGSファイルは未選択です。";
  spzStep.append(spzLabel, spzHelp, spzFile, selectedSpz);

  const mrpStep = document.createElement("div");
  mrpStep.className = "preview-import-step";
  const mrpLabel = document.createElement("label");
  mrpLabel.textContent = "2. この3DGSを学習した元のMRP";
  const mrpSelect = document.createElement("select");
  mrpSelect.className = "preview-select";
  mrpSelect.setAttribute("aria-label", "Metric Reconstruction Package");
  const allMrps = await api<JsonObject[]>("/api/mrps");
  const applicationWorkspacePrefixes = [
    "var/spatial/workspaces/projects/",
    "data/work/application_preview/projects/",
  ];
  const availableMrps = allMrps.filter((item) => {
    const manifest = (item.manifest as JsonObject | undefined) ?? {};
    const provenance = (manifest.provenance as JsonObject | undefined) ?? {};
    const sourceWorkspace = String(provenance.source_workspace_uri ?? "");
    return applicationWorkspacePrefixes.some((prefix) => sourceWorkspace.startsWith(prefix));
  });
  const placeholder = document.createElement("option");
  placeholder.value = "";
  placeholder.textContent = "MRPを選択してください";
  placeholder.selected = true;
  placeholder.disabled = true;
  mrpSelect.appendChild(placeholder);
  const mrpById = new Map<string, JsonObject>();
  for (const item of availableMrps) {
    const reconstructionId = String(item.reconstruction_id ?? "");
    const sourceSceneId = String(item.scene_id ?? "");
    const mrpKey = `${sourceSceneId}:${reconstructionId}`;
    mrpById.set(mrpKey, item);
    const manifest = (item.manifest as JsonObject | undefined) ?? {};
    const provenance = (manifest.provenance as JsonObject | undefined) ?? {};
    const sourceWorkspace = String(provenance.source_workspace_uri ?? "unknown COLMAP project");
    const createdAt = String(manifest.created_at ?? "unknown date").replace("T", " ").replace(/\..*$/, "");
    const option = document.createElement("option");
    option.value = mrpKey;
    option.textContent = `${reconstructionId} — ${sourceWorkspace} — ${createdAt}`;
    mrpSelect.appendChild(option);
  }
  const mrpHelp = document.createElement("p");
  mrpHelp.textContent = "3D再構成（COLMAP）の処理結果に表示された「LichtFeld Studioに読み込むフォルダ」と同じプロジェクトのMRPを選びます。";
  const mrpDetails = document.createElement("div");
  mrpDetails.className = "preview-selection-summary";
  mrpDetails.textContent = "MRPは未選択です。";
  const coordinateLabel = document.createElement("label");
  coordinateLabel.textContent = "asset座標契約";
  const coordinateSelect = document.createElement("select");
  coordinateSelect.className = "preview-select";
  coordinateSelect.setAttribute("aria-label", "Gaussian coordinate contract");
  for (const [value, label] of [
    ["spark_gaussian_decoder_rx180", "LichtFeld / Spark標準（X軸180°変換）"],
    ["viewer_native_identity", "Viewer座標へ変換済み（identity）"],
  ]) {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = label;
    coordinateSelect.appendChild(option);
  }
  const coordinateHelp = document.createElement("p");
  coordinateHelp.textContent = "SPZは既定契約を自動使用します。PLY/RADは書き出し時の座標系に一致する契約を明示してください。";
  mrpStep.append(mrpLabel, mrpHelp, mrpSelect, coordinateLabel, coordinateSelect, coordinateHelp, mrpDetails);

  const validationStep = document.createElement("div");
  validationStep.className = "preview-import-step";
  const validationLabel = document.createElement("strong");
  validationLabel.textContent = "3. 選択したMRPを検証";
  const importStatus = document.createElement("div");
  importStatus.className = "preview-inline-status";
  let mrpVerified = false;
  const validateButton = button("MRPを検証する", () => {
    const selected = mrpById.get(mrpSelect.value);
    const reconstructionId = String(selected?.reconstruction_id ?? "");
    const sourceSceneId = String(selected?.scene_id ?? "");
    if (!reconstructionId || !sourceSceneId) {
      importStatus.textContent = "先に「この3DGSを学習した元のMRP」を選択してください。";
      return;
    }
    validateButton.disabled = true;
    importButton.disabled = true;
    importStatus.textContent = "Validating the selected MRP and its RGBA/LFS inputs…";
    void api<JsonObject>(`/api/mrps/${encodeURIComponent(sourceSceneId)}/${encodeURIComponent(reconstructionId)}/validate`, { method: "POST" })
      .then((validation) => {
        mrpVerified = validation.status === "verified";
        const reasons = Array.isArray(validation.failure_reasons) ? validation.failure_reasons.join(", ") : "";
        importStatus.textContent = mrpVerified
          ? `MRP verified: ${reconstructionId}. 3DGSを登録できます。`
          : `MRP validation failed: ${reasons || "unknown reason"}`;
        importButton.disabled = !mrpVerified || !spzFile.files?.[0];
        setStep(mrpVerified ? 4 : 3);
      })
      .catch((error) => { importStatus.textContent = `MRP validation failed: ${String(error)}`; })
      .finally(() => { validateButton.disabled = availableMrps.length === 0; });
  }, availableMrps.length === 0);
  validationStep.append(validationLabel, validateButton, importStatus);

  const publishStep = document.createElement("div");
  publishStep.className = "preview-import-step";
  const publishLabel = document.createElement("strong");
  publishLabel.textContent = "4. Scene Packageへ登録してViewerを開く";
  const publishHelp = document.createElement("p");
  publishHelp.textContent = "登録後、Scene PackageはGit管理外のcanonical保存先とViewer runtimeへ公開されます。Proxyが未検証なら、表示以外の機能はfail-closedです。";
  mrpSelect.addEventListener("change", () => {
    mrpVerified = false;
    importButton.disabled = true;
    const selected = mrpById.get(mrpSelect.value);
    const reconstructionId = String(selected?.reconstruction_id ?? "");
    const manifest = (selected?.manifest as JsonObject | undefined) ?? {};
    const provenance = (manifest.provenance as JsonObject | undefined) ?? {};
    const sourceWorkspace = String(provenance.source_workspace_uri ?? "unknown");
    mrpDetails.textContent = `選択中: ${reconstructionId}\nCOLMAP処理プロジェクト: ${sourceWorkspace}\nLichtFeld入力: このMRPに検証済みコピーがあります`;
    importStatus.textContent = "選択を変更しました。次にMRPを検証してください。";
    setStep(spzFile.files?.[0] ? 3 : 2);
  });
  spzFile.addEventListener("change", () => {
    const file = spzFile.files?.[0];
    selectedSpz.textContent = file
      ? `選択中: ${file.name} (${Math.round(file.size / 1024 / 1024)} MiB)`
      : "3DGSファイルは未選択です。";
    const suffix = file?.name.toLowerCase().split(".").pop() ?? "";
    coordinateSelect.disabled = suffix === "spz";
    importButton.disabled = !file || !mrpVerified;
    if (file && !mrpSelect.value) {
      importStatus.textContent = "次に、この3DGSを学習した元のMRPを選択してください。";
      setStep(2);
    } else if (file && mrpVerified) {
      importStatus.textContent = "3DGSとMRPは準備できました。登録してViewerを開けます。";
      setStep(4);
    }
  });
  const importButton = button("登録してViewerを開く", () => {
    const file = spzFile.files?.[0];
    const selected = mrpById.get(mrpSelect.value);
    const reconstructionId = String(selected?.reconstruction_id ?? "");
    const sourceSceneId = String(selected?.scene_id ?? "");
    if (!file) {
      importStatus.textContent = "まず3DGSファイルを選択してください。";
      return;
    }
    if (!reconstructionId || !sourceSceneId) {
      importStatus.textContent = "この3DGSを学習した元のMRPを選択してください。";
      return;
    }
    if (!mrpVerified) {
      importStatus.textContent = "選択したMRPの検証が必要です。";
      return;
    }
    importButton.disabled = true;
    importStatus.textContent = `Importing ${file.name} (${Math.round(file.size / 1024 / 1024)} MiB)…`;
    const suffix = file.name.toLowerCase().split(".").pop() ?? "";
    const coordinateContractId = suffix === "spz" ? "spark_spz_decoder_rx180" : coordinateSelect.value;
    const query = new URLSearchParams({
      filename: file.name,
      scene_id: sourceSceneId,
      reconstruction_id: reconstructionId,
      coordinate_contract_id: coordinateContractId,
    });
    void api<JsonObject>(`/api/import-gaussian?${query.toString()}`, {
      method: "PUT",
      headers: { "Content-Type": file.type || "application/octet-stream" },
      body: file,
    }).then((result) => {
      importStatus.textContent = `登録・公開済み: ${String(result.scene_package_id)}。Viewerを開きます。`;
      const viewerUrl = String(result.viewer_url ?? "");
      if (viewerUrl) window.location.href = viewerUrl;
    }).catch((error) => {
      importStatus.textContent = `3DGSデータ登録に失敗しました: ${String(error)}`;
      importButton.disabled = false;
    });
  }, true);
  publishStep.append(publishLabel, publishHelp, importButton);
  importPanel.append(importTitle, importNote, steps, spzStep, mrpStep, validationStep, publishStep);
  panel.appendChild(importPanel);
  content.append(panel);
}
