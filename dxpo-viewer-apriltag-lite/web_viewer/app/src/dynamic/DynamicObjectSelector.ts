export function refreshDynamicObjectSelect(
  select: HTMLSelectElement,
  objectIds: readonly string[],
  onSelectionChanged: () => void,
): void {
  const previous = select.value;
  select.replaceChildren();
  for (const objectId of objectIds) {
    const option = document.createElement("option");
    option.value = objectId;
    option.textContent = `${objectId}へフォーカス`;
    select.appendChild(option);
  }
  select.disabled = objectIds.length === 0;
  if (objectIds.includes(previous)) select.value = previous;

  // This function also runs for every ROS state update. Keep an in-progress
  // editor draft unless the selected object itself has actually changed.
  if (select.value !== previous) onSelectionChanged();
}
