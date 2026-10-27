export type JsonObject = Record<string, unknown>;

export interface PublishedScenePackages {
  packages: JsonObject[];
  source: "api" | "bundle";
}

// The development/packaged Web server proxies /api to the local control
// service and attaches its secret server-side. Never embed the control token
// in browser JavaScript. VITE_PREVIEW_API_URL remains an explicit compatibility
// escape hatch for read-only deployments and tests.
const API_BASE = (import.meta.env.VITE_PREVIEW_API_URL as string | undefined) ?? "";

export function previewApiUrl(path: string): string {
  return `${API_BASE}${path}`;
}

export async function api<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(previewApiUrl(path), options);
  const payload = await response.json() as T & { failure_reasons?: string[] };
  if (!response.ok) {
    throw new Error(payload.failure_reasons?.join(", ") ?? `API ${response.status}`);
  }
  return payload;
}

export interface AprilTagDetectionRequest {
  family: string;
  tag_size_m: number;
  fx: number;
  fy: number;
  cx: number;
  cy: number;
  /** OpenCV plumb-bob order: k1,k2,p1,p2[,k3[,k4,k5,k6]]. */
  distortion_coefficients?: number[];
}

/** Process one transient browser-camera frame without storing the image. */
export async function detectAprilTagFrame<T>(frame: Blob, request: AprilTagDetectionRequest): Promise<T> {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(request)) {
    if (value === undefined) continue;
    query.set(key, Array.isArray(value) ? value.join(",") : String(value));
  }
  const response = await fetch(`${API_BASE}/api/apriltag-detect?${query.toString()}`, {
    method: "PUT",
    headers: { "Content-Type": frame.type || "image/jpeg" },
    body: frame,
  });
  const payload = await response.json() as T & { failure_reasons?: string[] };
  if (!response.ok) {
    throw new Error(payload.failure_reasons?.join(", ") ?? `API ${response.status}`);
  }
  return payload;
}

/** Stream a local video to the API without buffering the file in JavaScript. */
export function uploadSelectedVideo(file: File, onProgress: (percent: number) => void): Promise<JsonObject> {
  const query = new URLSearchParams({ filename: file.name });
  return new Promise((resolve, reject) => {
    const request = new XMLHttpRequest();
    request.open("PUT", `${API_BASE}/api/upload-video?${query.toString()}`);
    request.setRequestHeader("Content-Type", file.type || "application/octet-stream");
    request.upload.addEventListener("progress", (event) => {
      if (event.lengthComputable && event.total > 0) onProgress(Math.round(event.loaded / event.total * 100));
    });
    request.addEventListener("load", () => {
      try {
        const payload = JSON.parse(request.responseText || "{}") as JsonObject & { failure_reasons?: unknown };
        if (request.status >= 200 && request.status < 300) {
          resolve(payload);
          return;
        }
        const reasons = Array.isArray(payload.failure_reasons) ? payload.failure_reasons.join(", ") : "";
        reject(new Error(reasons || `API ${request.status}`));
      } catch (error) {
        reject(error);
      }
    });
    request.addEventListener("error", () => reject(new Error("local_video_upload_failed")));
    request.send(file);
  });
}

/** Store an immutable image as content-addressed evidence for one asset. */
export async function uploadFieldMemoryImage(assetId: string, file: File, title = ""): Promise<JsonObject> {
  const query = new URLSearchParams({ filename: file.name });
  if (title.trim()) query.set("title", title.trim());
  const response = await fetch(
    `${API_BASE}/api/field-memory/assets/${encodeURIComponent(assetId)}/images?${query.toString()}`,
    {
      method: "PUT",
      headers: { "Content-Type": file.type || "application/octet-stream" },
      body: file,
    },
  );
  const payload = await response.json() as JsonObject & { failure_reasons?: string[] };
  if (!response.ok) {
    throw new Error(payload.failure_reasons?.join(", ") ?? `API ${response.status}`);
  }
  return payload;
}

/** Query a site-scoped derived image index without storing the query image. */
export async function querySimilarFieldImage<T>(siteId: string, file: File, limit = 20): Promise<T> {
  const query = new URLSearchParams({ site_id: siteId, limit: String(limit) });
  const response = await fetch(`${API_BASE}/api/spatial-search/images/query?${query.toString()}`, {
    method: "PUT",
    headers: { "Content-Type": file.type || "application/octet-stream" },
    body: file,
  });
  const payload = await response.json() as T & { failure_reasons?: string[] };
  if (!response.ok) {
    throw new Error(payload.failure_reasons?.join(", ") ?? `API ${response.status}`);
  }
  return payload;
}

/** Viewer distributions are read-only and expose a static package index. */
async function bundledScenePackageIndex(): Promise<{ kind?: string; packages: JsonObject[] } | undefined> {
  try {
    const response = await fetch("/scene_package/distribution_index.json", { cache: "no-store" });
    if (!response.ok) return undefined;
    const payload = await response.json() as JsonObject | JsonObject[];
    const packages = Array.isArray(payload) ? payload : payload.packages;
    if (!Array.isArray(packages) || !packages.every((item) => item && typeof item === "object" && !Array.isArray(item))) {
      return undefined;
    }
    return {
      kind: Array.isArray(payload) ? undefined : String(payload.kind ?? ""),
      packages: packages as JsonObject[],
    };
  } catch {
    return undefined;
  }
}

export async function bundledScenePackages(): Promise<JsonObject[] | undefined> {
  return (await bundledScenePackageIndex())?.packages;
}

export async function publishedScenePackages(): Promise<PublishedScenePackages> {
  const bundledPromise = bundledScenePackageIndex();
  try {
    const packages = await api<JsonObject[]>("/api/scene-packages", {
      signal: AbortSignal.timeout(5_000),
    });
    const bundled = await bundledPromise;
    // A composed distribution deliberately keeps selected Scene Packages out
    // of mutable data/scene_repository/packages/scenes. Its packaged static index is therefore the
    // authoritative package list even while the restricted API is healthy.
    if (bundled?.kind === "spatial_3dgs_viewer_distribution_index") {
      return { packages: bundled.packages, source: "bundle" };
    }
    return { packages, source: "api" };
  } catch (apiError) {
    const bundled = await bundledPromise;
    if (bundled) return { packages: bundled.packages, source: "bundle" };
    throw apiError;
  }
}
