import {
  type FirstPersonNavigationHandle,
  type FirstPersonNavigationSettings,
} from "./FirstPersonNavigation";

const STORAGE_KEY = "spatial-3dgs.first-person-navigation-settings.v1";

function finitePositive(raw: string, fallback: number): number {
  const value = Number(raw);
  return Number.isFinite(value) && value > 0 ? value : fallback;
}

function loadSettings(): Partial<FirstPersonNavigationSettings> {
  try {
    const parsed = JSON.parse(window.localStorage.getItem(STORAGE_KEY) ?? "{}") as Record<string, unknown>;
    const result: Partial<FirstPersonNavigationSettings> = {};
    for (const key of ["speedMetersPerSecond", "lookRadiansPerPixel", "panMetersPerPixel", "wheelMetersPerStep"] as const) {
      if (typeof parsed[key] === "number" && Number.isFinite(parsed[key])) result[key] = parsed[key] as never;
    }
    return result;
  } catch {
    return {};
  }
}

function persistSettings(settings: Readonly<FirstPersonNavigationSettings>): void {
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(settings));
  } catch {
    // Storage can be disabled in private or embedded contexts. Navigation must
    // still remain usable with the current in-memory settings.
  }
}

function createNumberInput(value: number | undefined, step: string, placeholder?: string): HTMLInputElement {
  const input = document.createElement("input");
  input.type = "number";
  input.step = step;
  input.value = value === undefined ? "" : String(value);
  if (placeholder) input.placeholder = placeholder;
  return input;
}

function addField(parent: HTMLElement, labelText: string, input: HTMLInputElement): void {
  const label = document.createElement("label");
  label.textContent = labelText;
  label.appendChild(input);
  parent.appendChild(label);
}

export function mountFirstPersonNavigationSettings(
  navigation: FirstPersonNavigationHandle,
): HTMLElement {
  navigation.setSettings(loadSettings());
  const panel = document.createElement("details");
  panel.className = "navigation-panel";
  const summary = document.createElement("summary");
  summary.textContent = "一人称操作・設定";
  panel.appendChild(summary);

  const fields = document.createElement("div");
  fields.className = "navigation-settings-grid";
  const settings = navigation.getSettings();
  const speedInput = createNumberInput(settings.speedMetersPerSecond, "0.1");
  const lookInput = createNumberInput(settings.lookRadiansPerPixel, "0.0001");
  const panInput = createNumberInput(settings.panMetersPerPixel, "0.0005");
  const wheelInput = createNumberInput(settings.wheelMetersPerStep, "0.05");
  addField(fields, "移動速度 (m/s)", speedInput);
  addField(fields, "視点感度 (rad/px)", lookInput);
  addField(fields, "右ドラッグ移動 (m/px)", panInput);
  addField(fields, "ホイール移動 (m)", wheelInput);
  panel.appendChild(fields);

  const status = document.createElement("div");
  status.className = "navigation-settings-status";
  status.setAttribute("aria-live", "polite");

  const applyInputs = (): void => {
    const previous = navigation.getSettings();
    navigation.setSettings({
      speedMetersPerSecond: finitePositive(speedInput.value, previous.speedMetersPerSecond),
      lookRadiansPerPixel: finitePositive(lookInput.value, previous.lookRadiansPerPixel),
      panMetersPerPixel: finitePositive(panInput.value, previous.panMetersPerPixel),
      wheelMetersPerStep: finitePositive(wheelInput.value, previous.wheelMetersPerStep),
    });
    persistSettings(navigation.getSettings());
    status.textContent = "設定を保存しました。";
  };
  for (const input of [speedInput, lookInput, panInput, wheelInput]) {
    input.addEventListener("change", applyInputs);
  }

  const actions = document.createElement("div");
  actions.className = "navigation-settings-actions";
  const lockButton = document.createElement("button");
  lockButton.type = "button";
  const updateLockButton = (): void => {
    lockButton.textContent = navigation.isPointerLocked() ? "マウス固定を解除 (Esc)" : "マウス固定で視点回転";
  };
  lockButton.addEventListener("click", () => {
    if (navigation.isPointerLocked()) navigation.exitPointerLock();
    else navigation.requestPointerLock();
    window.setTimeout(updateLockButton, 0);
  });
  document.addEventListener("pointerlockchange", updateLockButton);
  updateLockButton();
  actions.appendChild(lockButton);

  panel.appendChild(actions);

  const hint = document.createElement("p");
  hint.className = "navigation-settings-hint";
  hint.textContent = "左ドラッグまたはマウス固定で視点回転。右ドラッグで画面平面上を移動。WASDで水平移動、Q/E・Shift/Spaceで上下、ホイールで前後。";
  panel.append(hint, status);
  return panel;
}
