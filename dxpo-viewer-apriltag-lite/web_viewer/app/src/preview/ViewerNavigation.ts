function viewerBase(): URL {
  const configuredBase = __SPATIAL_VIEWER_BASE_URL__.trim();
  if (configuredBase) {
    return new URL(configuredBase.endsWith("/") ? configuredBase : `${configuredBase}/`);
  }

  // Every shipped Web application contains the read-only Viewer entry point.
  // Keeping navigation on the current origin makes standalone applications and
  // Docker distributions self-contained. A split deployment can still opt in
  // to another Viewer by setting SPATIAL_VIEWER_BASE_URL at build time.
  return new URL(window.location.origin);
}

/** Resolve an API-provided Viewer path against the process that actually hosts 3DGS. */
export function resolveViewerUrl(viewerPath: string): string {
  return new URL(viewerPath, viewerBase()).toString();
}

export function openViewer(viewerPath: string): void {
  window.open(resolveViewerUrl(viewerPath), "_blank", "noopener");
}
