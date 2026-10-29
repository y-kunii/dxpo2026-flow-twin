import { mountApplicationPreview } from "../preview/ApplicationPreview";
import { spatialAppDefinition, type PreviewMode, type SpatialAppId } from "./AppPackageContract";

export function mountStandalonePreviewApp(appId: Exclude<SpatialAppId, "suite">): void {
  const definition = spatialAppDefinition(appId);
  if (!definition.previewMode) throw new Error(`preview_mode_missing:${appId}`);
  document.title = `Spatial 3DGS — ${definition.label}`;
  document.body.dataset.spatialApp = appId;
  mountApplicationPreview(document.body, {
    fixedMode: definition.previewMode,
    allowedModes: [definition.previewMode],
    applicationTitle: definition.label,
    standalone: true,
  });
}

export function mountComposedPreviewApp(modes: readonly PreviewMode[], label: string): void {
  if (modes.length === 0) throw new Error("composed_preview_mode_required");
  document.title = `Spatial 3DGS — ${label}`;
  document.body.dataset.spatialApp = "custom";
  mountApplicationPreview(document.body, {
    fixedMode: modes[0],
    allowedModes: modes,
    applicationTitle: label,
  });
}
