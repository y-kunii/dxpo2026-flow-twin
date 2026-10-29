export interface ViewerSimulationSessionInfo {
  scenePackageId: string;
  sessionId: string;
  fixedTagFrames: readonly string[];
  /** True when the parent admits runtime `box_<N>` child frames. */
  acceptsRuntimeDynamicBoxes: boolean;
  dynamicObjectIds: readonly string[];
}

export interface ViewerSimulationSessionGuard {
  dispose(): void;
}

const CHANNEL_NAME = "spatial-3dgs-viewer-dynamic-simulation-v1";
const HEARTBEAT_INTERVAL_MS = 750;
const HEARTBEAT_TIMEOUT_MS = 2_500;

interface SessionMessage extends ViewerSimulationSessionInfo {
  type: "viewer-ready" | "viewer-heartbeat" | "simulator-request";
}

function createSessionId(): string {
  if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID();
  return `viewer-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

function usableBroadcastChannel(): BroadcastChannel | undefined {
  try {
    return typeof BroadcastChannel === "undefined" ? undefined : new BroadcastChannel(CHANNEL_NAME);
  } catch {
    return undefined;
  }
}

function isMatchingSession(value: unknown, scenePackageId: string, sessionId: string): value is SessionMessage {
  if (!value || typeof value !== "object") return false;
  const candidate = value as Partial<SessionMessage>;
  return (candidate.type === "viewer-ready" || candidate.type === "viewer-heartbeat") &&
    candidate.scenePackageId === scenePackageId && candidate.sessionId === sessionId &&
    Array.isArray(candidate.fixedTagFrames) && Array.isArray(candidate.dynamicObjectIds) &&
    candidate.acceptsRuntimeDynamicBoxes === true;
}

/** Advertise a fully loaded Viewer as the only permitted source for a
 * simulation child.  The message carries a package ID, never an arbitrary
 * filesystem path or an SPZ URI. */
export function startViewerSimulationSession(
  scenePackageId: string,
  fixedTagFrames: readonly string[],
  dynamicObjectIds: readonly string[],
): { info: ViewerSimulationSessionInfo; dispose(): void } | undefined {
  if (!scenePackageId || !fixedTagFrames.length) return undefined;
  const channel = usableBroadcastChannel();
  if (!channel) return undefined;
  const info: ViewerSimulationSessionInfo = {
    scenePackageId,
    sessionId: createSessionId(),
    fixedTagFrames: [...fixedTagFrames],
    acceptsRuntimeDynamicBoxes: true,
    dynamicObjectIds: [...dynamicObjectIds],
  };
  const announce = (type: "viewer-ready" | "viewer-heartbeat"): void => {
    channel.postMessage({ type, ...info } satisfies SessionMessage);
  };
  channel.onmessage = (event: MessageEvent<unknown>) => {
    const value = event.data as Partial<SessionMessage> | undefined;
    if (value?.type === "simulator-request" && value.scenePackageId === info.scenePackageId && value.sessionId === info.sessionId) {
      announce("viewer-ready");
    }
  };
  announce("viewer-ready");
  const timer = window.setInterval(() => announce("viewer-heartbeat"), HEARTBEAT_INTERVAL_MS);
  return {
    info,
    dispose: () => {
      window.clearInterval(timer);
      channel.close();
    },
  };
}

/** Require a live parent Viewer session.  A child opened directly, a child
 * with another package ID, or a child after its parent closes is fail-closed.
 */
export function requireViewerSimulationParent(
  scenePackageId: string,
  sessionId: string | undefined,
  onAvailability: (available: boolean, detail: string, info?: ViewerSimulationSessionInfo) => void,
): ViewerSimulationSessionGuard {
  const channel = usableBroadcastChannel();
  if (!scenePackageId || !sessionId || !channel) {
    onAvailability(false, "親3DGS Viewerから起動してください。");
    return { dispose: () => undefined };
  }
  let lastHeartbeatMs: number | undefined;
  let latestInfo: ViewerSimulationSessionInfo | undefined;
  const announceState = (): void => {
    const available = lastHeartbeatMs !== undefined && performance.now() - lastHeartbeatMs <= HEARTBEAT_TIMEOUT_MS;
    onAvailability(
      available,
      available ? "親3DGS Viewerに接続済み。このScene Packageだけを実験に使用します。" : "親3DGS Viewerが停止または未読込です。ROS送信を停止しました。",
      latestInfo,
    );
  };
  channel.onmessage = (event: MessageEvent<unknown>) => {
    if (!isMatchingSession(event.data, scenePackageId, sessionId)) return;
    latestInfo = {
      scenePackageId,
      sessionId,
      fixedTagFrames: [...event.data.fixedTagFrames],
      acceptsRuntimeDynamicBoxes: true,
      dynamicObjectIds: [...event.data.dynamicObjectIds],
    };
    lastHeartbeatMs = performance.now();
    announceState();
  };
  channel.postMessage({
    type: "simulator-request",
    scenePackageId,
    sessionId,
    fixedTagFrames: [],
    acceptsRuntimeDynamicBoxes: true,
    dynamicObjectIds: [],
  } satisfies SessionMessage);
  const timer = window.setInterval(announceState, 250);
  announceState();
  return {
    dispose: () => {
      window.clearInterval(timer);
      channel.close();
    },
  };
}
