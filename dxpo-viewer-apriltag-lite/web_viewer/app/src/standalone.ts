import "./style.css";
import { mountComposedPreviewApp, mountStandalonePreviewApp } from "./apps/StandalonePreviewApp";

// Standalone applications also ship the read-only Viewer. Explicit Scene
// Package navigation must take precedence over the application's management
// screen so a frame-analysis record can restore its stored camera pose without
// requiring a second Web application.
const query = new URLSearchParams(window.location.search);
const hasScenePackageRequest = query.has("scene_package");

if (hasScenePackageRequest) {
  const requestedVariant = query.get("viewer");
  void (__SPATIAL_VIEWER_VARIANT__ === "simple" || requestedVariant === "simple"
    ? import("./simple_viewer")
    : import("./main"));
} else if (__SPATIAL_APP_TARGET__ === "custom") {
  mountComposedPreviewApp(__SPATIAL_APP_MODES__, __SPATIAL_APP_LABEL__);
} else if (__SPATIAL_APP_TARGET__ === "suite" || __SPATIAL_APP_TARGET__ === "viewer") {
  throw new Error(`standalone_preview_entry_cannot_mount:${__SPATIAL_APP_TARGET__}`);
} else {
  mountStandalonePreviewApp(__SPATIAL_APP_TARGET__);
}
