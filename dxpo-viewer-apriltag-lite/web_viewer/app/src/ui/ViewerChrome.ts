export interface ViewerChrome {
  status: HTMLDivElement;
  cameraPanel: HTMLDivElement;
  proxyPanel: HTMLDivElement;
  objectPanel: HTMLDivElement;
  dynamicPanel: HTMLDivElement;
}

/** Mount the Viewer shell independently from scene and tracking orchestration. */
export function mountViewerChrome(navigationPanel: HTMLElement): ViewerChrome {
  const status = document.createElement("div");
  status.className = "status";
  status.textContent = "Loading scene manifest...";
  document.body.appendChild(status);

  const navigationHelp = document.createElement("div");
  navigationHelp.className = "navigation-help";
  navigationHelp.textContent = "WASD: 一人称移動 | 左ドラッグ: 視点回転 | 右ドラッグ: 平行移動 | Q/E・Shift/Space: 上下移動 | ホイール: 前後移動";
  document.body.appendChild(navigationHelp);

  const cameraPanel = document.createElement("div");
  cameraPanel.className = "camera-panel";
  cameraPanel.innerHTML = "<strong>Camera presets</strong>";
  const proxyPanel = document.createElement("div");
  proxyPanel.className = "proxy-panel";
  const objectPanel = document.createElement("div");
  objectPanel.className = "object-panel";
  document.body.appendChild(objectPanel);
  const dynamicPanel = document.createElement("div");
  dynamicPanel.className = "dynamic-panel";

  const inspector = document.createElement("aside");
  inspector.className = "viewer-inspector";
  inspector.setAttribute("aria-label", "Viewer inspector");
  const inspectorHeader = document.createElement("div");
  inspectorHeader.className = "viewer-inspector-header";
  const inspectorTitle = document.createElement("strong");
  inspectorTitle.textContent = "Viewer Inspector";
  const inspectorToggle = document.createElement("button");
  inspectorToggle.type = "button";
  inspectorToggle.className = "viewer-inspector-toggle";
  inspectorToggle.textContent = "Inspectorを隠す";
  inspectorHeader.append(inspectorTitle, inspectorToggle);
  const inspectorBody = document.createElement("div");
  inspectorBody.className = "viewer-inspector-body";
  inspectorBody.append(navigationPanel, cameraPanel, proxyPanel, dynamicPanel);
  inspector.append(inspectorHeader, inspectorBody);
  document.body.appendChild(inspector);

  const visibilityToggle = document.createElement("button");
  visibilityToggle.type = "button";
  visibilityToggle.className = "viewer-ui-visibility-toggle";
  visibilityToggle.textContent = "UIを隠す";
  visibilityToggle.setAttribute("aria-pressed", "false");
  document.body.appendChild(visibilityToggle);
  let hidden = false;
  inspectorToggle.addEventListener("click", () => {
    inspectorBody.hidden = !inspectorBody.hidden;
    inspectorToggle.textContent = inspectorBody.hidden ? "Inspectorを表示" : "Inspectorを隠す";
  });
  visibilityToggle.addEventListener("click", () => {
    hidden = !hidden;
    document.body.classList.toggle("viewer-ui-hidden", hidden);
    visibilityToggle.textContent = hidden ? "UIを表示" : "UIを隠す";
    visibilityToggle.setAttribute("aria-pressed", String(hidden));
  });

  return { status, cameraPanel, proxyPanel, objectPanel, dynamicPanel };
}
