import { defineConfig } from "vite";
import { tmpdir } from "node:os";
import { join } from "node:path";

const APP_MODES = new Set([
  "colmap",
  "apriltag-check",
  "spz-import",
  "scene-manager",
  "project-manager",
  "package-builder",
  "field-memory",
  "site-insights",
  "frame-analysis",
  "viewer",
  "custom",
]);

const PREVIEW_MODES = new Set([
  "pipeline", "tag-check", "spatial", "packages", "projects", "package-builder",
  "field-memory", "site-insights", "frame-analysis",
]);

export default defineConfig(({ mode }) => {
  const target = APP_MODES.has(mode) ? mode : "suite";
  const configuredModes = (process.env.SPATIAL_APP_MODES ?? "")
    .split(",")
    .map((value) => value.trim())
    .filter((value) => value.length > 0);
  const controlApiTarget = process.env.SPATIAL_CONTROL_API_URL ?? "http://127.0.0.1:8788";
  const controlToken = process.env.SPATIAL_CONTROL_TOKEN ?? "";
  const rbacRequired = process.env.SPATIAL_RBAC_REQUIRED === "1";
  const proxyActorId = (process.env.SPATIAL_ACTOR_ID ?? "").trim();
  const proxyActorRole = (process.env.SPATIAL_ACTOR_ROLE ?? "").trim().toLowerCase();
  const proxySiteId = (process.env.SPATIAL_SITE_ID ?? "").trim();
  const identityConfigured = Boolean(proxyActorId || proxyActorRole || proxySiteId);
  const validRoles = new Set(["viewer", "operator", "reviewer", "admin"]);
  const viewerVariant = process.env.SPATIAL_VIEWER_VARIANT === "simple" ? "simple" : "full";
  const defaultScenePackage = process.env.SPATIAL_DEFAULT_SCENE_PACKAGE ?? "";
  if (target === "custom" && (configuredModes.length === 0 || configuredModes.some((value) => !PREVIEW_MODES.has(value)))) {
    throw new Error("SPATIAL_APP_MODES must contain registered preview modes for custom builds");
  }
  if (
    (rbacRequired || identityConfigured) &&
    (!proxyActorId || !validRoles.has(proxyActorRole) || (proxyActorRole !== "admin" && !proxySiteId))
  ) {
    throw new Error(
      "RBAC proxy identity requires SPATIAL_ACTOR_ID, a valid SPATIAL_ACTOR_ROLE, " +
      "and SPATIAL_SITE_ID for non-admin roles",
    );
  }
  return {
    // The same workspace is used from Docker and WSL. Keeping Vite's mutable
    // cache in node_modules lets a root-owned container cache block the host
    // dev server with EACCES. The cache is disposable, so isolate it per app in
    // the OS temporary directory instead of mixing it with application files.
    cacheDir: process.env.SPATIAL_VITE_CACHE_DIR ?? join(tmpdir(), "spatial-3dgs-vite", target),
    plugins: target !== "suite" && target !== "viewer" ? [{
      name: "spatial-standalone-entry",
      transformIndexHtml: {
        order: "pre",
        handler(html: string) {
          const entry = target === "custom" ? "/src/composed.ts" : "/src/standalone.ts";
          return html.replace("/src/bootstrap.ts", entry);
        },
      },
    }] : [],
    define: {
      __SPATIAL_APP_TARGET__: JSON.stringify(target),
      __SPATIAL_APP_MODES__: JSON.stringify(configuredModes),
      __SPATIAL_APP_LABEL__: JSON.stringify(process.env.SPATIAL_APP_LABEL ?? "Spatial 3DGS Application"),
      __SPATIAL_VIEWER_VARIANT__: JSON.stringify(viewerVariant),
      __SPATIAL_DEFAULT_SCENE_PACKAGE__: JSON.stringify(defaultScenePackage),
      __SPATIAL_VIEWER_BASE_URL__: JSON.stringify(process.env.SPATIAL_VIEWER_BASE_URL ?? ""),
      __SPATIAL_APP_HOME_URL__: JSON.stringify(
        process.env.SPATIAL_APP_HOME_URL ?? (target === "suite" ? "/" : ""),
      ),
    },
    server: {
      proxy: {
        "/api": {
          target: controlApiTarget,
          changeOrigin: false,
          headers: controlToken ? { "X-Spatial-Control-Token": controlToken } : undefined,
          configure(proxy) {
            proxy.on("proxyReq", (proxyRequest) => {
              // Browser-supplied identity is never trusted. A fixed local
              // operator identity may be injected only from server-side env.
              for (const header of [
                "x-spatial-actor-id",
                "x-spatial-role",
                "x-spatial-site-id",
              ]) {
                proxyRequest.removeHeader(header);
              }
              if (!identityConfigured) return;
              proxyRequest.setHeader("X-Spatial-Actor-Id", proxyActorId);
              proxyRequest.setHeader("X-Spatial-Role", proxyActorRole);
              if (proxySiteId) proxyRequest.setHeader("X-Spatial-Site-Id", proxySiteId);
            });
          },
        },
      },
    },
    build: {
      outDir: target === "suite" ? "dist" : `dist/apps/${target}`,
      emptyOutDir: true,
      // `public/scene_package` is a runtime publication mount, not part of an
      // application binary.  Copying it here duplicated every published Scene
      // Package into every standalone app (many gigabytes per target).  A
      // deliberately self-contained static export may opt in explicitly; the
      // normal Viewer and application distributions serve/mount runtime data
      // separately.
      copyPublicDir: process.env.SPATIAL_COPY_PUBLIC_ON_BUILD === "1",
      // Spark ships a lazy WebAssembly-backed renderer chunk.  Vite's raw
      // 500 kB warning does not distinguish that on-demand dependency from an
      // eagerly loaded application regression.  The post-build budget checks
      // gzip sizes separately (256 KiB normal / 2 MiB lazy Spark).
      chunkSizeWarningLimit: 5_000,
    },
  };
});
