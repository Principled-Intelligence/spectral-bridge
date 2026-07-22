#!/bin/sh
# Maps container environment variables onto the spectral-bridge CLI.
#
#   SPECTRAL_BRIDGE_API_KEY  relay API key (read by the CLI itself)
#   RELAY_URL                relay server WebSocket URL (wss://...)
#   TARGET_URL               local OpenAI-compatible endpoint to proxy
#   ADAPTER_PORT             adapter port inside the container (default 8840)
#
# Extra arguments are appended to `spectral-bridge start`, e.g.:
#   docker run ... spectral-bridge-image --request-timeout 300

set -eu

: "${SPECTRAL_BRIDGE_API_KEY:?set SPECTRAL_BRIDGE_API_KEY to the relay API key}"
: "${RELAY_URL:?set RELAY_URL to the relay server WebSocket URL (wss://...)}"
: "${TARGET_URL:?set TARGET_URL to the local OpenAI-compatible endpoint}"

exec spectral-bridge start \
    --adapter pass-through \
    --relay-url "$RELAY_URL" \
    --target "$TARGET_URL" \
    --port "${ADAPTER_PORT:-8840}" \
    "$@"
