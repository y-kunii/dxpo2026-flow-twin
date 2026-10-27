import { recordDynamicTrackingDiagnostic } from "./DynamicTrackingDiagnostics.js";

export type RosbridgeConnectionStatus = "disconnected" | "connecting" | "receiving" | "stale" | "rejected";

export interface RosbridgeTransformClientOptions {
  onStatus: (status: RosbridgeConnectionStatus, detail?: string) => void;
  /** The receive time comes from a monotonic viewer clock.  Source ROS epoch
   * timestamps remain inside the message strictly for ordering checks. */
  onMessage: (message: unknown, receivedMonotonicMs: number) => void;
  /** Scene Package-owned custom topic. This client never subscribes to /tf. */
  topic?: "/box/relative_transform";
  reconnect?: { enabled?: boolean; initial_delay_ms?: number; maximum_delay_ms?: number; maximum_attempts?: number };
}

interface WebSocketLike {
  readonly OPEN: number;
  readonly readyState: number;
  send(data: string): void;
  close(): void;
  onopen: (() => void) | null;
  onmessage: ((event: { data: unknown }) => void) | null;
  onerror: (() => void) | null;
  onclose: (() => void) | null;
}

type WebSocketFactory = (url: string) => WebSocketLike;

const TOPIC = "/box/relative_transform";
const MESSAGE_TYPE = "geometry_msgs/msg/TransformStamped";

function defaultFactory(url: string): WebSocketLike {
  return new WebSocket(url) as unknown as WebSocketLike;
}

function receivedMessageSummary(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object") return { payload_type: typeof value };
  const message = value as {
    header?: { frame_id?: unknown; stamp?: { sec?: unknown; nanosec?: unknown } };
    child_frame_id?: unknown;
  };
  return {
    parent_frame_id: message.header?.frame_id,
    child_frame_id: message.child_frame_id,
    stamp_sec: message.header?.stamp?.sec,
    stamp_nanosec: message.header?.stamp?.nanosec,
  };
}

/** Minimal rosbridge subscriber with bounded reconnect. It never publishes ROS commands. */
export class RosbridgeTransformClient {
  private socket: WebSocketLike | undefined;
  private readonly createSocket: WebSocketFactory;
  private readonly options: RosbridgeTransformClientOptions;
  private readonly topic: string;
  private reconnectTimer: ReturnType<typeof setTimeout> | undefined;
  private reconnectAttempts = 0;
  private requestedUrl: string | undefined;
  private intentionalDisconnect = true;

  constructor(options: RosbridgeTransformClientOptions, createSocket: WebSocketFactory = defaultFactory) {
    this.options = options;
    this.createSocket = createSocket;
    this.topic = options.topic ?? TOPIC;
  }

  get connected(): boolean {
    return Boolean(this.socket && this.socket.readyState === this.socket.OPEN);
  }

  connect(url: string): void {
    this.disconnect();
    this.intentionalDisconnect = false;
    this.requestedUrl = url;
    this.open(url);
  }

  private open(url: string): void {
    recordDynamicTrackingDiagnostic("rosbridge_client", "connect_requested", { url, topic: this.topic });
    if (!/^ws(s?):\/\//.test(url)) {
      recordDynamicTrackingDiagnostic("rosbridge_client", "connection_rejected", {
        url,
        reason: "invalid_url_scheme",
      });
      this.options.onStatus("rejected", "rosbridge URL must use ws:// or wss://");
      return;
    }
    this.options.onStatus("connecting");
    let socket: WebSocketLike;
    try {
      socket = this.createSocket(url);
    } catch (error) {
      recordDynamicTrackingDiagnostic("rosbridge_client", "connection_rejected", {
        url,
        reason: error instanceof Error ? error.message : String(error),
      });
      this.options.onStatus("rejected", error instanceof Error ? error.message : String(error));
      this.scheduleReconnect();
      return;
    }
    this.socket = socket;
    socket.onopen = () => {
      if (this.socket !== socket) return;
      socket.send(JSON.stringify({
        op: "subscribe",
        topic: this.topic,
        type: MESSAGE_TYPE,
      }));
      recordDynamicTrackingDiagnostic("rosbridge_client", "connection_opened", {
        url,
        topic: this.topic,
      });
      this.reconnectAttempts = 0;
    };
    socket.onmessage = (event) => {
      if (this.socket !== socket) return;
      let value: unknown;
      try {
        value = typeof event.data === "string" ? JSON.parse(event.data) : event.data;
      } catch {
        recordDynamicTrackingDiagnostic("rosbridge_client", "message_rejected_before_validation", {
          reason: "invalid_json",
        });
        this.options.onStatus("rejected", "rosbridge message is not valid JSON");
        return;
      }
      if (!value || typeof value !== "object") {
        recordDynamicTrackingDiagnostic("rosbridge_client", "message_rejected_before_validation", {
          reason: "payload_not_object",
        });
        this.options.onStatus("rejected", "rosbridge message must be an object");
        return;
      }
      const message = value as { op?: unknown; topic?: unknown; msg?: unknown };
      if (message.op !== "publish" || message.topic !== this.topic || message.msg === undefined) {
        return;
      }
      this.options.onStatus("receiving");
      const receivedMonotonicMs = typeof performance !== "undefined" && Number.isFinite(performance.now())
        ? performance.now()
        : Date.now();
      recordDynamicTrackingDiagnostic("rosbridge_client", "message_received", {
        topic: this.topic,
        received_monotonic_ms: receivedMonotonicMs,
        ...receivedMessageSummary(message.msg),
      });
      this.options.onMessage(message.msg, receivedMonotonicMs);
    };
    socket.onerror = () => {
      if (this.socket === socket) {
        recordDynamicTrackingDiagnostic("rosbridge_client", "connection_error", { url, topic: this.topic });
        this.options.onStatus("rejected", "rosbridge socket error");
      }
    };
    socket.onclose = () => {
      if (this.socket === socket) {
        this.socket = undefined;
        recordDynamicTrackingDiagnostic("rosbridge_client", "connection_closed", { url, topic: this.topic });
        this.options.onStatus("disconnected");
        this.scheduleReconnect();
      }
    };
  }

  private scheduleReconnect(): void {
    const policy = this.options.reconnect;
    const maximumAttempts = policy?.maximum_attempts ?? 5;
    if (this.intentionalDisconnect || policy?.enabled === false || !this.requestedUrl ||
        this.reconnectTimer !== undefined || this.reconnectAttempts >= maximumAttempts) return;
    const delay = Math.min(
      policy?.maximum_delay_ms ?? 8_000,
      (policy?.initial_delay_ms ?? 500) * 2 ** this.reconnectAttempts,
    );
    this.reconnectAttempts += 1;
    this.options.onStatus("connecting", `rosbridge再接続 ${this.reconnectAttempts}/${maximumAttempts}（${delay} ms後）`);
    recordDynamicTrackingDiagnostic("rosbridge_client", "reconnect_scheduled", {
      attempt: this.reconnectAttempts,
      maximum_attempts: maximumAttempts,
      delay_ms: delay,
      topic: this.topic,
    });
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = undefined;
      if (!this.intentionalDisconnect && this.requestedUrl) this.open(this.requestedUrl);
    }, delay);
  }

  disconnect(): void {
    this.intentionalDisconnect = true;
    this.requestedUrl = undefined;
    this.reconnectAttempts = 0;
    if (this.reconnectTimer !== undefined) clearTimeout(this.reconnectTimer);
    this.reconnectTimer = undefined;
    const socket = this.socket;
    this.socket = undefined;
    if (!socket) return;
    if (socket.readyState === socket.OPEN) {
      socket.send(JSON.stringify({ op: "unsubscribe", topic: this.topic }));
    }
    recordDynamicTrackingDiagnostic("rosbridge_client", "disconnect_requested", { topic: this.topic });
    socket.close();
    this.options.onStatus("disconnected");
  }
}

export { MESSAGE_TYPE as ROSBRIDGE_MESSAGE_TYPE, TOPIC as ROSBRIDGE_TOPIC };
