# spectral-bridge

Connect your private AI system to external testing and evaluation platforms — safely, and without any network changes. Use it for red-teaming, capability evaluations, safety benchmarks, and any other workflow where an outside platform needs to send prompts to a model it can't reach directly.

`spectral-bridge` is a relay client that makes AI systems reachable to outside platforms over a single outbound connection, without exposing them to the internet or requiring firewall and VPN changes. Built on an open protocol any platform can adopt.

## Who this is for

**AI teams running models on private infrastructure.** If your AI system runs somewhere that isn't reachable from the public internet — inside a corporate VPN, on an air-gapped research cluster, on a private cloud subnet, or on a developer's laptop — you've probably hit a wall when trying to use external testing or evaluation platforms. They expect a public URL. You don't have one, and getting one means weeks of security review, firewall exceptions, or sharing VPN credentials with a third party.

**Testing and evaluation platforms.** If you run a platform that needs to send requests to customer-hosted AI systems, you've seen the same wall from the other side. Every new customer is a custom integration: their network, their security team, their constraints. `spectral-bridge` gives you a single protocol to support instead.

## What it solves

The core problem is asymmetric: testing platforms need to *initiate* requests, but private AI systems can't *receive* them from the outside. The usual fixes all have real costs:

- **Opening inbound firewall rules** requires a security review and creates a permanent attack surface.
- **Granting VPN access** to a third party means sharing credentials to your whole network, not just the AI system.
- **Deploying a public proxy** means standing up and securing new infrastructure for every platform you want to use.
- **Skipping external evaluation entirely** means flying blind on safety, capability, and regression testing.

`spectral-bridge` replaces all of these with a single outbound WebSocket connection from inside your network to the testing platform's relay. Nothing inbound. No VPN sharing. No new public infrastructure. The only thing that crosses the boundary is the AI system's text response to a test prompt — nothing else on your network is reachable.

## How it works

`spectral-bridge` defines an [open protocol](https://github.com/Principled-Intelligence/spectral-bridge/blob/main/PROTOCOL.md) that testing platforms can adopt to let you safely connect your AI system to their infrastructure. A client runs inside your network and opens a single **outbound** WebSocket connection to a relay server. Requests flow from the relay to the client; responses flow back the same way. No inbound ports, no firewall changes, no VPN sharing.

![spectral-bridge architecture](https://raw.githubusercontent.com/Principled-Intelligence/spectral-bridge/main/assets/overview.svg)

**Client.** This repo provides a ready-to-use implementation, released under [Apache License 2.0](https://github.com/Principled-Intelligence/spectral-bridge/blob/main/LICENSE). It's written in Python, released on [PyPI](https://pypi.org/project/spectral-bridge/), and works out of the box with all compliant servers.

**Server.** The relay server is the testing platform's responsibility.

**Security boundary.** The relay does not give the testing platform access to your network. Traffic terminates at the adapter — a small process you control that exposes only a single OpenAI-compatible endpoint. Nothing else is forwarded.

## Quickstart

### Install

```bash
pip install spectral-bridge

# Include the built-in pass-through adapter:
pip install "spectral-bridge[pass-through]"
```

### Connect

Your testing platform will provide a relay URL and an API key. Set the key as an environment variable (this keeps it out of shell history):

```bash
export SPECTRAL_BRIDGE_API_KEY=<your-api-key>
```

If your AI system already exposes an OpenAI-compatible HTTP endpoint, the built-in `pass-through` adapter is all you need. Point `--target` at your internal AI system's URL:

```bash
spectral-bridge start \
  --relay-url  wss://relay.example.com/connect \
  --adapter    pass-through \
  --target     http://internal-host:8080
```

This starts the adapter locally, connects to the relay, and begins forwarding requests.

## Adapters

An adapter is a thin translation layer that sits between `spectral-bridge` and your AI system. It serves a single endpoint — `POST /v1/chat/completions` — using standard OpenAI-compatible request and response shapes.

Adapters can be written in any language. Any process, container, or script that serves that endpoint qualifies.

The built-in [`pass-through`](https://github.com/Principled-Intelligence/spectral-bridge/blob/main/adapters/pass-through/) adapter proxies to an existing OpenAI-compatible endpoint and covers most cases. If your AI system has a different shape, writing a custom adapter takes minimal effort — one HTTP endpoint, one JSON schema. See the [adapter documentation](https://spectral.com/docs/spectral-bridge/adapters/custom) for details.

## Protocol

`spectral-bridge` is built on an open protocol so that any testing platform can integrate relay support. The protocol has three parts:

1. **Adapter contract** — any process serving `POST /v1/chat/completions` qualifies. Only the chatbot's text response crosses the network boundary; no raw traffic from the internal host is forwarded.

2. **Relay client** — connects outbound to `wss://<relay-host>/connect` with bearer auth. The server pushes request frames; the client dispatches them to the adapter concurrently and returns response frames matched by `request_id`. Reconnects automatically with exponential backoff.

3. **Relay server** — must expose `/connect` and treat reconnects from the same key as an upsert. How callers reach the relay is up to the platform — the spec accommodates single-tenant, multi-tenant, and dynamic provisioning designs.

The full specification is in [PROTOCOL.md](https://github.com/Principled-Intelligence/spectral-bridge/blob/main/PROTOCOL.md).

## License

`spectral-bridge` — both the client and the protocol specification — is licensed under the [Apache License 2.0](https://github.com/Principled-Intelligence/spectral-bridge/blob/main/LICENSE). You're free to use, modify, and distribute it, including for commercial purposes. The license includes an explicit patent grant, which means anyone implementing the protocol can do so without worrying about future patent claims from the project's authors.

Contributions are welcome. We use the [Apache Individual Contributor License Agreement](https://www.apache.org/licenses/icla.pdf), which contributors sign once when opening their first pull request. See [CONTRIBUTING.md](https://github.com/Principled-Intelligence/spectral-bridge/blob/main/CONTRIBUTING.md) for details.
