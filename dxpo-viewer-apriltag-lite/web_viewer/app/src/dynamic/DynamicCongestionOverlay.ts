import { assessDynamicBoxCongestion } from "./DynamicBoxCongestion";

export interface DynamicCongestionOverlayHandle {
  element: HTMLElement;
  showButton: HTMLButtonElement;
  setVisible(visible: boolean): void;
  update(detectedBoxCount: number, staleCount?: number): void;
  dispose(): void;
}

export function mountDynamicCongestionOverlay(host: HTMLElement = document.body): DynamicCongestionOverlayHandle {
  const overlay = document.createElement("section");
  overlay.className = "dynamic-congestion-card dynamic-congestion-overlay dynamic-congestion-normal";
  overlay.dataset.testid = "dynamic-congestion-overlay";

  const header = document.createElement("div");
  header.className = "dynamic-congestion-header";
  const title = document.createElement("strong");
  title.className = "dynamic-congestion-title";
  title.textContent = "箱の滞留状況";
  const hideButton = document.createElement("button");
  hideButton.type = "button";
  hideButton.className = "dynamic-congestion-toggle";
  hideButton.textContent = "隠す";
  hideButton.dataset.testid = "dynamic-congestion-hide";
  header.append(title, hideButton);

  const chip = document.createElement("span");
  chip.className = "dynamic-congestion-chip";
  chip.textContent = "通常";
  const count = document.createElement("div");
  count.className = "dynamic-congestion-count";
  count.textContent = "検出中: 0個";
  count.dataset.testid = "dynamic-congestion-count";
  const hint = document.createElement("div");
  hint.className = "dynamic-summary";
  hint.textContent = "箱の検出を待っています。";
  overlay.append(header, chip, count, hint);
  host.appendChild(overlay);

  const showButton = document.createElement("button");
  showButton.type = "button";
  showButton.className = "dynamic-congestion-show-button";
  showButton.textContent = "滞留状況を表示";
  showButton.dataset.testid = "dynamic-congestion-show";
  showButton.hidden = true;
  host.appendChild(showButton);

  const setVisible = (visible: boolean): void => {
    overlay.hidden = !visible;
    showButton.hidden = visible;
  };
  hideButton.addEventListener("click", () => setVisible(false));
  showButton.addEventListener("click", () => setVisible(true));

  return {
    element: overlay,
    showButton,
    setVisible,
    update(detectedBoxCount: number, staleCount = 0): void {
      const safeCount = Number.isFinite(detectedBoxCount) ? Math.max(0, Math.trunc(detectedBoxCount)) : 0;
      const safeStaleCount = Number.isFinite(staleCount) ? Math.max(0, Math.trunc(staleCount)) : 0;
      const congestion = assessDynamicBoxCongestion(safeCount);
      overlay.className = `dynamic-congestion-card dynamic-congestion-overlay dynamic-congestion-${congestion.severity}`;
      chip.textContent = congestion.label;
      count.textContent = `検出中: ${safeCount}個${safeStaleCount ? `（更新待ち ${safeStaleCount}個）` : ""}`;
      hint.textContent = congestion.message;
    },
    dispose(): void {
      overlay.remove();
      showButton.remove();
    },
  };
}
