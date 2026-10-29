// Node 20 does not provide the browser WebSocket global used by the Viewer.
// Process-level integration tests preload this standards-compatible client;
// browser production code remains unchanged.
import WebSocket from "ws";

globalThis.WebSocket = WebSocket;
