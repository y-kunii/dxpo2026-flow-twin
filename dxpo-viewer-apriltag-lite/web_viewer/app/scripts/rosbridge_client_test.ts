import { RosbridgeTransformClient } from "../src/dynamic/RosbridgeTransformClient.js";

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}

class FakeSocket {
  readonly OPEN = 1;
  readyState = 0;
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: unknown }) => void) | null = null;
  onerror: (() => void) | null = null;
  onclose: (() => void) | null = null;
  sent: string[] = [];
  send(data: string): void { this.sent.push(data); }
  close(): void { this.readyState = 3; this.onclose?.(); }
}

const socket = new FakeSocket();
const statuses: string[] = [];
let received: unknown;
let receivedMonotonicMs: number | undefined;
const client = new RosbridgeTransformClient({
  onStatus: (status, detail) => statuses.push(detail ? `${status}:${detail}` : status),
  onMessage: (message, monotonicMs) => { received = message; receivedMonotonicMs = monotonicMs; },
}, () => socket);

client.connect("ws://127.0.0.1:9090");
assert(statuses[0] === "connecting", "connect status");
socket.readyState = socket.OPEN;
socket.onopen?.();
const subscription = JSON.parse(socket.sent[0]) as { op?: string; viewer_clock_ms?: unknown };
assert(subscription.op === "subscribe", "subscribe sent");
assert(subscription.viewer_clock_ms === undefined, "source ROS epoch is never coupled to browser wall clock");
socket.onmessage?.({ data: JSON.stringify({ op: "publish", topic: "/box/relative_transform", msg: { child_frame_id: "box_01" } }) });
assert(Boolean(received), "publish message forwarded");
assert(typeof receivedMonotonicMs === "number" && Number.isFinite(receivedMonotonicMs), "monotonic receive time forwarded");
socket.onmessage?.({ data: "not json" });
assert(statuses.some((status) => status.startsWith("rejected:")), "invalid JSON rejected");
client.disconnect();
assert(JSON.parse(socket.sent[1]).op === "unsubscribe", "unsubscribe sent");

console.log("rosbridge client tests: ok");
