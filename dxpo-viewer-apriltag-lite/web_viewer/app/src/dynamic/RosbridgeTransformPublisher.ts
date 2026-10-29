import type { TransformStampedMessage } from "./DynamicTrackingContract.js";
import { recordDynamicTrackingDiagnostic } from "./DynamicTrackingDiagnostics.js";
import { ROSBRIDGE_MESSAGE_TYPE, ROSBRIDGE_TOPIC, type RosbridgeConnectionStatus } from "./RosbridgeTransformClient.js";

interface WebSocketLike {
  readonly OPEN: number;
  readonly readyState: number;
  send(data: string): void;
  close(): void;
  onopen: (() => void) | null;
  onerror: (() => void) | null;
  onclose: (() => void) | null;
}

type WebSocketFactory = (url: string) => WebSocketLike;

export interface RosbridgeTransformPublisherOptions {
  onStatus: (status: RosbridgeConnectionStatus, detail?: string) => void;
  reconnect?: { enabled?: boolean; initial_delay_ms?: number; maximum_delay_ms?: number; maximum_attempts?: number };
}

function defaultFactory(url: string): WebSocketLike {
  return new WebSocket(url) as unknown as WebSocketLike;
}

/** Serialize the internal pose to native ROS 2 TransformStamped JSON for
 * rosbridge. This is also the exact form real ROS publishers expose. */
function asRosTransformStamped(message: TransformStampedMessage): Record<string, unknown> {
  const [x, y, z] = message.transform.translation_m;
  const [qx, qy, qz, qw] = message.transform.rotation_xyzw;
  return {
    header: {
      stamp: { sec: message.header.stamp.sec, nanosec: message.header.stamp.nanosec },
      frame_id: message.header.frame_id,
    },
    child_frame_id: message.child_frame_id,
    transform: {
      translation: { x, y, z },
      rotation: { x: qx, y: qy, z: qz, w: qw },
    },
  };
}

/** Minimal companion to the Viewer subscriber for integration-test adapters.
 *
 * This class publishes the already-composed `T_fixedTag_box` in native ROS 2
 * `geometry_msgs/msg/TransformStamped` form;
 * it does not perform image detection, issue ROS commands, or alter a Scene
 * Package. The production Viewer remains a subscriber only; Docker simulation
 * and the local AprilTag check explicitly opt in to this publisher.
 */
export class RosbridgeTransformPublisher {
  private socket: WebSocketLike | undefined;
  private readonly createSocket: WebSocketFactory;
  private readonly options: RosbridgeTransformPublisherOptions;
  private reconnectTimer: ReturnType<typeof setTimeout> | undefined;
  private reconnectAttempts = 0;
  private requestedUrl: string | undefined;
  private intentionalDisconnect = true;

  constructor(options: RosbridgeTransformPublisherOptions, createSocket: WebSocketFactory = defaultFactory) {
    this.options = options;
    this.createSocket = createSocket;
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
    recordDynamicTrackingDiagnostic("rosbridge_publisher", "connect_requested", { url });
    if (!/^ws(s?):\/\//.test(url)) {
      recordDynamicTrackingDiagnostic("rosbridge_publisher", "connection_rejected", {
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
      recordDynamicTrackingDiagnostic("rosbridge_publisher", "connection_rejected", {
        url,
        reason: error instanceof Error ? error.message : String(error),
      });
      this.options.onStatus("rejected", error instanceof Error ? error.message : String(error));
      this.scheduleReconnect();
      return;
    }
    this.socket = socket;
    let connectionRejected = false;
    socket.onopen = () => {
      if (this.socket !== socket) return;
      // rosbridge accepts this explicit advertisement; the repository's
      // small Docker bridge treats it as metadata and relays only publish.
      socket.send(JSON.stringify({ op: "advertise", topic: ROSBRIDGE_TOPIC, type: ROSBRIDGE_MESSAGE_TYPE }));
      recordDynamicTrackingDiagnostic("rosbridge_publisher", "connection_opened", {
        url,
        topic: ROSBRIDGE_TOPIC,
      });
      this.options.onStatus("receiving");
      this.reconnectAttempts = 0;
    };
    socket.onerror = () => {
      if (this.socket === socket) {
        connectionRejected = true;
        recordDynamicTrackingDiagnostic("rosbridge_publisher", "connection_error", { url });
        this.options.onStatus("rejected", `WebSocket connection failed: ${url}`);
      }
    };
    socket.onclose = () => {
      if (this.socket === socket) {
        this.socket = undefined;
        recordDynamicTrackingDiagnostic("rosbridge_publisher", "connection_closed", {
          url,
          rejected: connectionRejected,
        });
        // Browsers normally emit error followed by close. Preserve the useful
        // rejected status instead of immediately replacing it with the vague
        // disconnected state.
        if (!connectionRejected) this.options.onStatus("disconnected");
        this.scheduleReconnect();
      }
    };
  }

  private scheduleReconnect(): void {
    const policy = this.options.reconnect;
    const maximumAttempts = policy?.maximum_attempts ?? 5;
    if (this.intentionalDisconnect || policy?.enabled === false || !this.requestedUrl ||
        this.reconnectTimer !== undefined || this.reconnectAttempts >= maximumAttempts) return;
    const delay = Math.min(policy?.maximum_delay_ms ?? 8_000, (policy?.initial_delay_ms ?? 500) * 2 ** this.reconnectAttempts);
    this.reconnectAttempts += 1;
    // Keep the concrete connection error visible during backoff. Replacing it
    // immediately with "connecting" hides the actionable reason from the
    // operator; `open()` changes the state when the next attempt really starts.
    recordDynamicTrackingDiagnostic("rosbridge_publisher", "reconnect_scheduled", {
      attempt: this.reconnectAttempts,
      maximum_attempts: maximumAttempts,
      delay_ms: delay,
    });
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = undefined;
      if (!this.intentionalDisconnect && this.requestedUrl) this.open(this.requestedUrl);
    }, delay);
  }

  publish(message: TransformStampedMessage): boolean {
    const socket = this.socket;
    if (!socket || socket.readyState !== socket.OPEN) {
      recordDynamicTrackingDiagnostic("rosbridge_publisher", "publish_skipped", {
        reason: "socket_not_open",
        child_frame_id: message.child_frame_id,
      });
      return false;
    }
    socket.send(JSON.stringify({ op: "publish", topic: ROSBRIDGE_TOPIC, msg: asRosTransformStamped(message) }));
    recordDynamicTrackingDiagnostic("rosbridge_publisher", "publish_sent", {
      topic: ROSBRIDGE_TOPIC,
      parent_frame_id: message.header.frame_id,
      child_frame_id: message.child_frame_id,
      source_stamp_ms: message.header.stamp.sec * 1_000 + message.header.stamp.nanosec / 1_000_000,
      translation_m: [...message.transform.translation_m],
      rotation_xyzw: [...message.transform.rotation_xyzw],
    });
    return true;
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
      socket.send(JSON.stringify({ op: "unadvertise", topic: ROSBRIDGE_TOPIC }));
    }
    recordDynamicTrackingDiagnostic("rosbridge_publisher", "disconnect_requested", {
      topic: ROSBRIDGE_TOPIC,
    });
    socket.close();
    this.options.onStatus("disconnected");
  }
}
