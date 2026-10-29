import {
  isRuntimeDynamicBoxId,
  normalizeBoxDimensions,
  BOX_TAG_FACES,
  type BoxDimensionsM,
  type BoxTagFace,
} from "./DynamicTrackingContract";

export interface RuntimeDynamicBoxProfileEntry {
  id: string;
  dimensions_m: BoxDimensionsM;
  tag_face: BoxTagFace;
}

export interface RuntimeDynamicBoxProfile {
  schema_version: "1.1.0";
  scene_package_id: string;
  updated_at: string;
  boxes: RuntimeDynamicBoxProfileEntry[];
}

const KEY_PREFIX = "spatial-3dgs.runtime-box-profile.v1.";

export function runtimeDynamicBoxProfileKey(scenePackageId: string): string {
  return `${KEY_PREFIX}${scenePackageId}`;
}

export function validateRuntimeDynamicBoxProfile(value: unknown, scenePackageId: string): RuntimeDynamicBoxProfile | undefined {
  if (!value || typeof value !== "object") return undefined;
  const candidate = value as Partial<RuntimeDynamicBoxProfile> & { schema_version?: string };
  if (!(["1.0.0", "1.1.0"].includes(candidate.schema_version ?? "")) || candidate.scene_package_id !== scenePackageId ||
      typeof candidate.updated_at !== "string" || !Array.isArray(candidate.boxes)) return undefined;
  const ids = new Set<string>();
  const boxes: RuntimeDynamicBoxProfileEntry[] = [];
  for (const item of candidate.boxes) {
    if (!item || typeof item !== "object") return undefined;
    const box = item as Partial<RuntimeDynamicBoxProfileEntry>;
    const dimensions = normalizeBoxDimensions(box.dimensions_m);
    const tagFace = box.tag_face ?? "top";
    if (typeof box.id !== "string" || !isRuntimeDynamicBoxId(box.id) || ids.has(box.id) || !dimensions ||
        !BOX_TAG_FACES.includes(tagFace)) return undefined;
    ids.add(box.id);
    boxes.push({ id: box.id, dimensions_m: dimensions, tag_face: tagFace });
  }
  return {
    schema_version: "1.1.0",
    scene_package_id: scenePackageId,
    updated_at: candidate.updated_at,
    boxes,
  };
}

export function loadRuntimeDynamicBoxProfile(
  scenePackageId: string,
  storage: Storage = window.localStorage,
): RuntimeDynamicBoxProfile | undefined {
  const raw = storage.getItem(runtimeDynamicBoxProfileKey(scenePackageId));
  if (!raw) return undefined;
  try {
    return validateRuntimeDynamicBoxProfile(JSON.parse(raw), scenePackageId);
  } catch {
    return undefined;
  }
}

export function saveRuntimeDynamicBoxProfile(
  scenePackageId: string,
  boxes: RuntimeDynamicBoxProfileEntry[],
  storage: Storage = window.localStorage,
): RuntimeDynamicBoxProfile {
  const profile = validateRuntimeDynamicBoxProfile({
    schema_version: "1.1.0",
    scene_package_id: scenePackageId,
    updated_at: new Date().toISOString(),
    boxes,
  }, scenePackageId);
  if (!profile) throw new Error("箱ごとの実行時表示設定が不正です。");
  storage.setItem(runtimeDynamicBoxProfileKey(scenePackageId), JSON.stringify(profile));
  return profile;
}

export function exportRuntimeDynamicBoxProfile(profile: RuntimeDynamicBoxProfile): string {
  const validated = validateRuntimeDynamicBoxProfile(profile, profile.scene_package_id);
  if (!validated) throw new Error("箱設定が不正なためエクスポートできません。");
  return JSON.stringify(validated, null, 2);
}

export function importRuntimeDynamicBoxProfile(
  text: string,
  scenePackageId: string,
  storage: Storage = window.localStorage,
): RuntimeDynamicBoxProfile {
  let value: unknown;
  try {
    value = JSON.parse(text);
  } catch (error) {
    throw new Error(`箱設定JSONを解析できません: ${String(error)}`);
  }
  const profile = validateRuntimeDynamicBoxProfile(value, scenePackageId);
  if (!profile) throw new Error("箱設定JSONが不正、または別のScene Package用です。");
  const imported = { ...profile, updated_at: new Date().toISOString() };
  storage.setItem(runtimeDynamicBoxProfileKey(scenePackageId), JSON.stringify(imported));
  return imported;
}
