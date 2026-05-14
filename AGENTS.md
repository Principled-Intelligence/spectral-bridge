# spectral-bridge

Open protocol and implementation of relay client for testing AI systems that are not reachable from the internet (on-prem, VPN-only, private networks). A local process connects outbound to a cloud relay server via WebSocket, letting a testing platform send requests without the internal target ever being directly exposed.

## Protocol Overview

```
Cloud platform
    ↓ requests
Relay server
    ↑ outbound WebSocket (customer initiates)
spectral-bridge
    ↓ localhost
Local adapter → Internal target
```

- **Relay client** (`src/spectral_bridge/`): outbound WebSocket connection to relay server, forwards requests to a local adapter
- **Adapters** (`adapters/`): translate any local target into OpenAI-compatible `POST /v1/chat/completions`

See `PROTOCOL.md` for the full spec.

## Project Structure

```
src/spectral_bridge/     # Relay client SDK and CLI
  client.py              # WebSocket relay client
  cli/                   # `spectral-bridge start` entrypoint
adapters/                # Each adapter is self-contained (any language)
  pass-through/          # Proxy for local OpenAI-compatible endpoints
PROTOCOL.md              # Relay protocol and adapter contract spec
```

## Conventions

- **Logging:** short, lowercase, minimal punctuation — `connected to relay`, `adapter unavailable`, `reconnecting in 4s`
- **Dependencies:** `src/spectral_bridge/` stays minimal. Heavy deps (Playwright, browser-use, etc.) belong in `adapters/` only. Exception: `rich` is an allowed core dep (used for CLI logging).
- **Adapters**: can be in any language and must be self-contained within the `adapters/` folder
