export type DynamicDiagnosticComponent =
  | "browser"
  | "apriltag_sender"
  | "rosbridge_publisher"
  | "rosbridge_client"
  | "viewer_validation"
  | "viewer_freshness";

export interface DynamicDiagnosticEvent {
  sequence: number;
  epoch_ms: number;
  monotonic_ms: number;
  component: DynamicDiagnosticComponent;
  event: string;
  same_event_gap_ms?: number;
  /** Stable source-frame key shared by sender, rosbridge and Viewer events. */
  correlation_id?: string;
  visibility: DocumentVisibilityState | "unavailable";
  data: Record<string, unknown>;
}

export interface DynamicDiagnosticSnapshot {
  schema_version: "dynamic_tracking_diagnostics_v1";
  session_id: string;
  started_at: string;
  ended_at?: string;
  active: boolean;
  context: Record<string, unknown>;
  privacy: {
    images_recorded: false;
    ros_payload_values_recorded: true;
    device_identifiers_recorded: false;
    persistence: "memory_until_explicit_json_download";
  };
  dropped_event_count: number;
  analysis: {
    event_count: number;
    max_camera_cycle_gap_ms: number | null;
    max_publish_gap_ms: number | null;
    max_receive_gap_ms: number | null;
    max_event_loop_drift_ms: number | null;
    stale_transition_count: number;
    lost_transition_count: number;
    rejected_message_count: number;
    document_hidden_count: number;
  };
  events: DynamicDiagnosticEvent[];
}

export interface DynamicDiagnosticState {
  active: boolean;
  event_count: number;
  dropped_event_count: number;
  has_snapshot: boolean;
  last_event?: string;
}

interface MutableSession {
  session_id: string;
  started_at: string;
  ended_at?: string;
  context: Record<string, unknown>;
  events: DynamicDiagnosticEvent[];
  dropped_event_count: number;
  last_event_monotonic_ms: Map<string, number>;
}

const MAX_EVENTS = 20_000;
const EVENT_LOOP_SAMPLE_MS = 100;
const EVENT_LOOP_DRIFT_THRESHOLD_MS = 50;
const listeners = new Set<(state: DynamicDiagnosticState) => void>();
let activeSession: MutableSession | undefined;
let retainedSession: MutableSession | undefined;
let eventLoopTimer: number | undefined;
let expectedEventLoopTickMs = 0;
let longTaskObserver: PerformanceObserver | undefined;
let visibilityListener: (() => void) | undefined;
let pageHideListener: (() => void) | undefined;

function monotonicNowMs(): number {
  return typeof performance !== "undefined" && Number.isFinite(performance.now())
    ? performance.now()
    : Date.now();
}

function currentVisibility(): DocumentVisibilityState | "unavailable" {
  return typeof document === "undefined" ? "unavailable" : document.visibilityState;
}

function sourceCorrelationId(data: Record<string, unknown>): string | undefined {
  const direct = Number(data.source_frame_epoch_ms ?? data.source_stamp_ms);
  if (Number.isFinite(direct)) return `source-ms:${direct.toFixed(6)}`;
  const sec = Number(data.stamp_sec);
  const nanosec = Number(data.stamp_nanosec);
  if (Number.isInteger(sec) && Number.isInteger(nanosec)) return `source-ms:${(sec * 1000 + nanosec / 1_000_000).toFixed(6)}`;
  return undefined;
}

function state(): DynamicDiagnosticState {
  const session = activeSession ?? retainedSession;
  const last = session?.events.at(-1);
  return {
    active: Boolean(activeSession),
    event_count: session?.events.length ?? 0,
    dropped_event_count: session?.dropped_event_count ?? 0,
    has_snapshot: Boolean(session),
    ...(last ? { last_event: `${last.component}:${last.event}` } : {}),
  };
}

function notify(): void {
  const value = state();
  for (const listener of listeners) listener(value);
}

function detachRuntimeProbes(): void {
  if (eventLoopTimer !== undefined) window.clearInterval(eventLoopTimer);
  eventLoopTimer = undefined;
  longTaskObserver?.disconnect();
  longTaskObserver = undefined;
  if (visibilityListener) document.removeEventListener("visibilitychange", visibilityListener);
  visibilityListener = undefined;
  if (pageHideListener) window.removeEventListener("pagehide", pageHideListener);
  pageHideListener = undefined;
}

export function recordDynamicTrackingDiagnostic(
  component: DynamicDiagnosticComponent,
  event: string,
  data: Record<string, unknown> = {},
): void {
  const session = activeSession;
  if (!session) return;
  const monotonicMs = monotonicNowMs();
  const key = `${component}:${event}`;
  const previous = session.last_event_monotonic_ms.get(key);
  session.last_event_monotonic_ms.set(key, monotonicMs);
  const record: DynamicDiagnosticEvent = {
    sequence: session.events.length + session.dropped_event_count + 1,
    epoch_ms: Date.now(),
    monotonic_ms: monotonicMs,
    component,
    event,
    ...(previous === undefined ? {} : { same_event_gap_ms: monotonicMs - previous }),
    ...(sourceCorrelationId(data) ? { correlation_id: sourceCorrelationId(data) } : {}),
    visibility: currentVisibility(),
    data,
  };
  session.events.push(record);
  if (session.events.length > MAX_EVENTS) {
    const removeCount = session.events.length - MAX_EVENTS;
    session.events.splice(0, removeCount);
    session.dropped_event_count += removeCount;
  }
  // Updating the inspector for every camera/ROS event can itself add main
  // thread pressure and distort the timing we are trying to diagnose.
  if (session.events.length % 25 === 0) notify();
}

function attachRuntimeProbes(): void {
  expectedEventLoopTickMs = monotonicNowMs() + EVENT_LOOP_SAMPLE_MS;
  eventLoopTimer = window.setInterval(() => {
    const now = monotonicNowMs();
    const driftMs = Math.max(0, now - expectedEventLoopTickMs);
    expectedEventLoopTickMs = now + EVENT_LOOP_SAMPLE_MS;
    if (driftMs >= EVENT_LOOP_DRIFT_THRESHOLD_MS) {
      recordDynamicTrackingDiagnostic("browser", "event_loop_drift", { drift_ms: driftMs });
    }
  }, EVENT_LOOP_SAMPLE_MS);
  if (typeof PerformanceObserver !== "undefined") {
    try {
      longTaskObserver = new PerformanceObserver((list) => {
        for (const entry of list.getEntries()) {
          recordDynamicTrackingDiagnostic("browser", "long_task", {
            duration_ms: entry.duration,
            start_monotonic_ms: entry.startTime,
          });
        }
      });
      longTaskObserver.observe({ entryTypes: ["longtask"] });
    } catch {
      longTaskObserver = undefined;
    }
  }
  visibilityListener = () => {
    recordDynamicTrackingDiagnostic("browser", "visibility_changed", {
      visibility: currentVisibility(),
    });
  };
  document.addEventListener("visibilitychange", visibilityListener);
  pageHideListener = () => { stopDynamicTrackingDiagnostics(); };
  window.addEventListener("pagehide", pageHideListener);
}

export function startDynamicTrackingDiagnostics(context: Record<string, unknown> = {}): DynamicDiagnosticState {
  detachRuntimeProbes();
  const startedAt = new Date().toISOString();
  activeSession = {
    session_id: `${startedAt.replace(/[-:.TZ]/g, "")}-${Math.random().toString(16).slice(2, 10)}`,
    started_at: startedAt,
    context: {
      ...context,
      page_url: window.location.href,
      user_agent: navigator.userAgent,
      device_pixel_ratio: window.devicePixelRatio,
      viewport: { width: window.innerWidth, height: window.innerHeight },
    },
    events: [],
    dropped_event_count: 0,
    last_event_monotonic_ms: new Map(),
  };
  retainedSession = undefined;
  attachRuntimeProbes();
  recordDynamicTrackingDiagnostic("browser", "diagnostics_started", {
    visibility: currentVisibility(),
  });
  notify();
  return state();
}

export function stopDynamicTrackingDiagnostics(): DynamicDiagnosticState {
  if (!activeSession) return state();
  recordDynamicTrackingDiagnostic("browser", "diagnostics_stopped");
  activeSession.ended_at = new Date().toISOString();
  retainedSession = activeSession;
  activeSession = undefined;
  detachRuntimeProbes();
  notify();
  return state();
}

function maximumGap(events: DynamicDiagnosticEvent[], component: DynamicDiagnosticComponent, event: string): number | null {
  const values = events
    .filter((item) => item.component === component && item.event === event)
    .map((item) => item.same_event_gap_ms)
    .filter((value): value is number => typeof value === "number" && Number.isFinite(value));
  return values.length > 0 ? Math.max(...values) : null;
}

export function dynamicTrackingDiagnosticSnapshot(): DynamicDiagnosticSnapshot | undefined {
  const session = activeSession ?? retainedSession;
  if (!session) return undefined;
  const events = session.events.map((item) => ({ ...item, data: { ...item.data } }));
  const transitionCount = (name: string): number => events.filter((item) =>
    item.component === "viewer_freshness" && item.event === "state_transition" && item.data.to === name
  ).length;
  return {
    schema_version: "dynamic_tracking_diagnostics_v1",
    session_id: session.session_id,
    started_at: session.started_at,
    ...(session.ended_at ? { ended_at: session.ended_at } : {}),
    active: session === activeSession,
    context: { ...session.context },
    privacy: {
      images_recorded: false,
      ros_payload_values_recorded: true,
      device_identifiers_recorded: false,
      persistence: "memory_until_explicit_json_download",
    },
    dropped_event_count: session.dropped_event_count,
    analysis: {
      event_count: events.length,
      max_camera_cycle_gap_ms: maximumGap(events, "apriltag_sender", "detection_cycle"),
      max_publish_gap_ms: maximumGap(events, "rosbridge_publisher", "publish_sent"),
      max_receive_gap_ms: maximumGap(events, "rosbridge_client", "message_received"),
      max_event_loop_drift_ms: events
        .filter((item) => item.component === "browser" && item.event === "event_loop_drift")
        .reduce<number | null>((maximum, item) => {
          const value = Number(item.data.drift_ms);
          return Number.isFinite(value) ? Math.max(maximum ?? value, value) : maximum;
        }, null),
      stale_transition_count: transitionCount("stale"),
      lost_transition_count: transitionCount("lost"),
      rejected_message_count: events.filter((item) =>
        item.component === "viewer_validation" && item.event === "message_rejected"
      ).length,
      document_hidden_count: events.filter((item) =>
        item.component === "browser" && item.event === "visibility_changed" && item.data.visibility === "hidden"
      ).length,
    },
    events,
  };
}

export function clearDynamicTrackingDiagnostics(): void {
  if (activeSession) stopDynamicTrackingDiagnostics();
  retainedSession = undefined;
  notify();
}

export function dynamicTrackingDiagnosticState(): DynamicDiagnosticState {
  return state();
}

export function subscribeDynamicTrackingDiagnostics(
  listener: (state: DynamicDiagnosticState) => void,
): () => void {
  listeners.add(listener);
  listener(state());
  return () => { listeners.delete(listener); };
}
