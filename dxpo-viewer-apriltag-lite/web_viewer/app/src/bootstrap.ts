import "./style.css";
import type { SpatialAppId } from "./apps/AppPackageContract";

const target = __SPATIAL_APP_TARGET__ as SpatialAppId;
const query = new URLSearchParams(window.location.search);
const hasScenePackage = query.has("scene_package");

function mountViewer(): void {
  const variant = query.get("viewer") === "simple" ? "simple" : __SPATIAL_VIEWER_VARIANT__;
  void (variant === "simple" ? import("./simple_viewer") : import("./main"));
}

if (target === "suite" || hasScenePackage) {
  if (hasScenePackage) mountViewer();
  else void import("./main");
} else {
  void import("./apps/StandalonePreviewApp").then(({ mountStandalonePreviewApp }) => {
    mountStandalonePreviewApp(target);
  });
}
