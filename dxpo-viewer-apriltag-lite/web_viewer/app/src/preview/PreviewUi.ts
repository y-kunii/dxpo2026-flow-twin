export function button(label: string, onClick: () => void, disabled = false): HTMLButtonElement {
  const element = document.createElement("button");
  element.type = "button";
  element.textContent = label;
  element.disabled = disabled;
  element.addEventListener("click", onClick);
  return element;
}

export function statusClass(status: unknown): string {
  return `preview-status preview-status-${String(status ?? "unknown").replace(/[^a-z0-9_-]/gi, "-")}`;
}

export function statusBadge(status: unknown): HTMLSpanElement {
  const badge = document.createElement("span");
  badge.className = statusClass(status);
  badge.textContent = String(status ?? "unknown");
  return badge;
}

export function field(label: string, value: string, type = "text", key = label): HTMLLabelElement {
  const wrapper = document.createElement("label");
  wrapper.className = "preview-field";
  const caption = document.createElement("span");
  caption.textContent = label;
  const input = document.createElement("input");
  input.type = type;
  input.value = value;
  input.dataset.field = key;
  wrapper.append(caption, input);
  return wrapper;
}

export function checkboxField(label: string, checked: boolean, key: string): HTMLLabelElement {
  const wrapper = document.createElement("label");
  wrapper.className = "preview-field preview-checkbox-field";
  const input = document.createElement("input");
  input.type = "checkbox";
  input.checked = checked;
  input.dataset.field = key;
  const caption = document.createElement("span");
  caption.textContent = label;
  wrapper.append(input, caption);
  return wrapper;
}

export function selectField(
  label: string,
  value: string,
  key: string,
  options: Array<{ value: string; label: string }>,
): HTMLLabelElement {
  const wrapper = document.createElement("label");
  wrapper.className = "preview-field";
  const caption = document.createElement("span");
  caption.textContent = label;
  const select = document.createElement("select");
  select.dataset.field = key;
  for (const optionValue of options) {
    const option = document.createElement("option");
    option.value = optionValue.value;
    option.textContent = optionValue.label;
    option.selected = optionValue.value === value;
    select.appendChild(option);
  }
  wrapper.append(caption, select);
  return wrapper;
}

export function section(title: string): HTMLElement {
  const element = document.createElement("section");
  element.className = "preview-section";
  const heading = document.createElement("h2");
  heading.textContent = title;
  element.appendChild(heading);
  return element;
}

export function preJson(value: unknown): HTMLPreElement {
  const pre = document.createElement("pre");
  pre.textContent = JSON.stringify(value, null, 2);
  return pre;
}

export function stringValue(value: unknown, fallback = "\u2014"): string {
  return typeof value === "string" && value ? value : fallback;
}

export function pathLeaf(value: unknown, fallback = "\u2014"): string {
  const path = stringValue(value, "").replace(/\\/g, "/").replace(/\/+$/, "");
  return path.split("/").filter(Boolean).pop() || fallback;
}

export function byteSize(value: unknown): string {
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0) return "\u2014";
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  let amount = value;
  let index = 0;
  while (amount >= 1024 && index < units.length - 1) {
    amount /= 1024;
    index += 1;
  }
  return `${amount >= 10 || index === 0 ? amount.toFixed(0) : amount.toFixed(1)} ${units[index]}`;
}

export function lifecycleLabel(value: unknown): string {
  const labels: Record<string, string> = {
    running: "\u5b9f\u884c\u4e2d",
    retained: "\u4fdd\u8b77",
    rejected: "\u524a\u9664\u53ef\u80fd",
    review: "\u8981\u78ba\u8a8d",
  };
  const lifecycle = stringValue(value, "review");
  return labels[lifecycle] ?? lifecycle;
}
