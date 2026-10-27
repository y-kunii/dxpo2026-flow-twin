import type { PreviewMode, SpatialAppId } from "./apps/AppPackageContract";

declare global {
  const __SPATIAL_APP_TARGET__: SpatialAppId | "custom";
  const __SPATIAL_APP_MODES__: readonly PreviewMode[];
  const __SPATIAL_APP_LABEL__: string;
  const __SPATIAL_VIEWER_VARIANT__: "full" | "simple";
  const __SPATIAL_DEFAULT_SCENE_PACKAGE__: string;
  const __SPATIAL_VIEWER_BASE_URL__: string;
  const __SPATIAL_APP_HOME_URL__: string;
}

export {};
