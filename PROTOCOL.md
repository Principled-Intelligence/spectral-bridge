# spectral-bridge Protocol Specification

This document defines three protocol contracts:

1. **Adapter Contract** — the interface any local adapter must implement
2. **Relay Client Protocol** — how the relay client communicates with the relay server
3. **Relay Server** — what a conforming relay server must implement, and the suggested internal routing design

This protocol addresses the **network isolation** problem: AI systems running on private infrastructure that is not reachable from the internet. It does not address the authentication problem for cloud-hosted systems that are internet-reachable but require credentials.

Any conforming implementation of either client-facing component (adapter or relay client) should interoperate with any conforming implementation of the other.

## 1. Adapter Contract

An adapter is any process that **translates** a local target into an OpenAI-compatible HTTP server, acting as a semantic boundary: only the chatbot's text response is forwarded — no raw network traffic from the internal host crosses the boundary. The relay client forwards requests to it over localhost and expects responses in the format below.

### Required Endpoint

```
POST /v1/chat/completions
```

The adapter must implement this endpoint. No other endpoints are required.

### Request Format

The relay client sends standard OpenAI chat completions requests:

```json
{
  "messages": [
    { "role": "user",      "content": "string" },
    { "role": "assistant", "content": "string" }
  ]
}
```

`messages` is an ordered list of prior turns followed by the current user message. The adapter may use the full history or only the last message, depending on whether the underlying target is stateful.

`model` is intentionally omitted. The OpenAI spec lists it as required, but many implementations treat it as optional.

### Response Format

The adapter must respond with a minimal OpenAI-compatible completion object:

```json
{
  "choices": [
    {
      "message": {
        "role": "assistant",
        "content": "string"
      }
    }
  ]
}
```

Additional fields (`id`, `model`, `usage`, etc.) are optional and ignored by the relay client.

### Error Responses

On failure, the adapter should return a standard HTTP error status (`4xx` / `5xx`) with a JSON body:

```json
{
  "error": {
    "message": "human-readable description"
  }
}
```

The relay client will propagate the status code and body back to the relay server unchanged.

### Concurrency

The adapter must handle concurrent requests. The relay client may forward multiple requests simultaneously (one per active conversation turn).

## 2. Relay Client Protocol

The relay client maintains a persistent, bidirectional WebSocket connection to the relay server. The connection is **initiated outbound** by the client — no inbound ports or firewall rules are required. Once established, the server pushes request frames down to the client, and the client pushes response frames back up.

### 2.1 Connection

Connect to:

```
wss://<relay-host>/connect
```

Pass the API key as a header during the WebSocket handshake:

```
Authorization: Bearer <api-key>
```

A **conforming** relay client must use TLS (`wss://`). Plain `ws://` is not permitted for conforming deployments.

Reference implementations may support plain `ws://` when the operator explicitly opts in (for example a `--insecure-relay` flag). That mode is for local development and debugging only; it is not conforming and must not be used where the relay traffic could leave a trusted network.

On successful authentication, the server sends a confirmation frame before any request frames:

```json
{ "type": "connected" }
```

The client should surface this to the user (e.g. print to terminal).

If authentication fails, the server closes the connection with code `4001`. The client must not retry with the same key without user intervention.

### 2.2 Message Format

All WebSocket frames carry UTF-8 encoded JSON. Two message types are defined.

#### Inbound: Request frame (server → client)

```json
{
  "type": "request",
  "request_id": "<opaque string>",
  "payload": {
    "method": "POST",
    "headers": {},
    "body": {}
  }
}
```

`request_id` is assigned by the relay server. It is opaque to the client — treat it as a correlation token and echo it back unchanged in the response.

The relay client always forwards requests to the adapter's `POST /v1/chat/completions` endpoint.

#### Outbound: Response frame (client → server)

```json
{
  "type": "response",
  "request_id": "<opaque string>",
  "payload": {
    "status": 200,
    "headers": { "content-type": "application/json" },
    "body": {}
  }
}
```

`request_id` must match the value from the corresponding request frame exactly.

#### Outbound: Error frame (client → server)

If the client cannot forward the request to the local adapter (e.g. adapter is down), it should send an error response rather than leaving the request unanswered:

```json
{
  "type": "response",
  "request_id": "<opaque string>",
  "payload": {
    "status": 503,
    "headers": { "content-type": "application/json" },
    "body": { "error": { "message": "Adapter unavailable" } }
  }
}
```

### 2.3 Concurrency

The client must handle request frames concurrently. Upon receiving a request frame, the client should immediately dispatch it to the local adapter and begin listening for the next frame — not wait for the adapter to respond before reading again. Response frames may be sent back in any order; the `request_id` provides correlation.

### 2.4 Keepalive

The client sends a WebSocket ping frame every **30 seconds**. If no pong is received within **10 seconds** of a ping, the connection is considered stale and the client must close it and initiate a reconnect.

### 2.5 Reconnection

The client reconnects automatically on any disconnect — whether caused by a network blip, relay server restart, or the client process being stopped and restarted later.

Reconnection uses **truncated exponential backoff**:

| Attempt | Delay  |
|---------|--------|
| 1       | 1 s    |
| 2       | 2 s    |
| 3       | 4 s    |
| 4       | 8 s    |
| 5+      | 30 s   |

On reconnect, the client presents the same API key. The relay server resolves the key to the same tunnel identity — the caller's endpoint does not change between reconnects.

**The client must not replay or resubmit requests that were in-flight at the time of disconnect.** Those requests are the relay server's responsibility to time out and report as errors to the caller.

## 3. Relay Server

Only the `/connect` WebSocket endpoint is mandatory — it is the protocol boundary that any conforming relay client interoperates with. How the server exposes a forwarding endpoint to its callers is an implementation concern; the sections below describe three progressively richer designs.

### 3.1 Mandatory: `/connect`

The server must expose a public WebSocket endpoint at `/connect`. On connection:

- Read the `Authorization: Bearer <api-key>` header. If absent or invalid, close with code `4001`.
- Validate the key. The same key must always authenticate to the same connection slot, regardless of how many times the client reconnects.
- Register the active WebSocket connection.
- Send a `{ "type": "connected" }` frame before any request frames.

**Reconnection**

- On reconnect with the same key: upsert the registered connection. Do not reject a reconnect from a known key.
- On concurrent connections with the same key: accept the newer connection, close the older one with a clean WebSocket close frame.

**Request forwarding**

- Push request frames to the active connection.
- Await the corresponding response frame, matched by `request_id`.
- If no active connection exists: return an error to the caller immediately (suggested: HTTP `503`).
- If no response frame is received within **30 seconds**: time out and return an error to the caller (suggested: HTTP `504`).

### 3.2 Suggested: Simple scenario

A single spectral-bridge client connects to the relay. The API key is a static secret (e.g. set via an `API_KEY` environment variable). Callers forward requests via a single endpoint:

```
POST /v1/chat/completions
```

### 3.3 Suggested: Multi-client scenario

Multiple spectral-bridge clients connect simultaneously, each with its own API key. Each key maps to a **relay ID** — an opaque identifier used by the platform to address a specific tunnel. Callers target a specific client via:

```
POST /relays/{relay-id}/v1/chat/completions
```

This endpoint must not be publicly reachable. The server maintains a mapping:

```
api-key  →  relay-id  (persistent)
relay-id →  active WebSocket connection  (live, in memory)
```

On forwarded request: resolve relay-id → active connection, push the request frame, await response.

### 3.4 Suggested: Dynamic multi-client scenario

Extends 3.3 with a provisioning endpoint that creates relay entries at runtime:

```
POST /relays
```

The caller supplies a relay ID; the server generates and returns an API key. This allows relay slots to be created on demand — for example, when a user registers a new private target on the testing platform — without redeploying or reconfiguring the server.

### 3.5 Authentication on the HTTP forwarding surface

While the WebSocket connection is authenticated with `Authorization: Bearer`, the protocol **does not** specify how callers authenticate to the relay server's HTTP surface that forwards into that tunnel (§3.2–3.4).

That is intentional: platforms will use different gateways, networks, and credential models. What is normative is that operators treat unauthenticated access to a forwarding URL as equivalent to full use of the attached adapter — anyone who can `POST` the completion endpoint can drive the private target. Production deployments should enforce authentication (or equivalent access control: private network only, mutual TLS, API gateway, etc.) on whatever HTTP path reaches the relay's forwarder.
