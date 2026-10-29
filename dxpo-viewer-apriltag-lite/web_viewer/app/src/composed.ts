import "./style.css";
import { mountComposedPreviewApp } from "./apps/StandalonePreviewApp";

// A selected Scene Package is part of the composed distribution as a
// read-only Viewer snapshot. Route its explicit URL to the shared Viewer;
// otherwise mount the selected management/application screens.
const hasScenePackageRequest = new URLSearchParams(window.location.search).has("scene_package");

const isDirectSimpleViewer = __SPATIAL_VIEWER_VARIANT__ === "simple"
  && __SPATIAL_APP_MODES__.length === 1
  && __SPATIAL_APP_MODES__[0] === "packages";

if (hasScenePackageRequest || isDirectSimpleViewer) {
  const requestedVariant = new URLSearchParams(window.location.search).get("viewer");
  void (__SPATIAL_VIEWER_VARIANT__ === "simple" || requestedVariant === "simple"
    ? import("./simple_viewer")
    : import("./main"));
} else {
  mountComposedPreviewApp(__SPATIAL_APP_MODES__, __SPATIAL_APP_LABEL__);
}
