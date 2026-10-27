const FRAME_ANALYSIS_RETURN_STATE_KEY = "spatial.frame-analysis.return-state.v1";
const FRAME_ANALYSIS_RETURN_STATE_MAX_AGE_MS = 24 * 60 * 60 * 1000;

interface FrameAnalysisReturnState {
  scene_version_id: string;
  query: string;
  page?: number;
  scroll_y: number;
  saved_at_ms: number;
}

function validState(value: unknown): value is FrameAnalysisReturnState {
  if (!value || typeof value !== "object") return false;
  const state = value as Partial<FrameAnalysisReturnState>;
  return typeof state.scene_version_id === "string"
    && typeof state.query === "string"
    && (state.page === undefined || (
      typeof state.page === "number"
      && Number.isInteger(state.page)
      && state.page >= 0
    ))
    && typeof state.scroll_y === "number"
    && Number.isFinite(state.scroll_y)
    && state.scroll_y >= 0
    && typeof state.saved_at_ms === "number"
    && Number.isFinite(state.saved_at_ms)
    && Date.now() - state.saved_at_ms <= FRAME_ANALYSIS_RETURN_STATE_MAX_AGE_MS;
}

export function saveFrameAnalysisReturnState(
  sceneVersionId: string,
  query: string,
  scrollY: number,
  page = 0,
): void {
  const state: FrameAnalysisReturnState = {
    scene_version_id: sceneVersionId,
    query,
    page: Math.max(0, Number.isInteger(page) ? page : 0),
    scroll_y: Math.max(0, Number.isFinite(scrollY) ? scrollY : 0),
    saved_at_ms: Date.now(),
  };
  try {
    window.localStorage.setItem(FRAME_ANALYSIS_RETURN_STATE_KEY, JSON.stringify(state));
  } catch {
    // Navigation still works when storage is disabled; only state restoration is skipped.
  }
}

export function takeFrameAnalysisReturnState(): FrameAnalysisReturnState | undefined {
  try {
    const raw = window.localStorage.getItem(FRAME_ANALYSIS_RETURN_STATE_KEY);
    window.localStorage.removeItem(FRAME_ANALYSIS_RETURN_STATE_KEY);
    if (!raw) return undefined;
    const parsed: unknown = JSON.parse(raw);
    return validState(parsed) ? parsed : undefined;
  } catch {
    return undefined;
  }
}

export function frameAnalysisReturnUrl(): string {
  if (document.referrer) {
    try {
      const referrer = new URL(document.referrer);
      if ((referrer.protocol === "http:" || referrer.protocol === "https:")
        && !referrer.searchParams.has("scene_package")) {
        return referrer.toString();
      }
    } catch {
      // Fall through to the local frame-analysis entry point.
    }
  }
  return new URL("/?preview=frame-analysis", window.location.origin).toString();
}
