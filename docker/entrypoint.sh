#!/bin/sh
# Maps container environment variables onto the spectral-bridge CLI.
#
#   SPECTRAL_BRIDGE_API_KEY  relay API key (read by the CLI itself)
#   RELAY_URL                relay server WebSocket URL override (wss://...);
#                            defaults to the Spectral relay when unset
#   TARGET_URL               local OpenAI-compatible endpoint to proxy
#   ADAPTER_PORT             adapter port inside the container (default 8840)
#
# Extra arguments are appended to `spectral-bridge start`, e.g.:
#   docker run ... spectral-bridge-image --request-timeout 300

set -eu

: "${SPECTRAL_BRIDGE_API_KEY:?set SPECTRAL_BRIDGE_API_KEY to the relay API key}"
: "${TARGET_URL:?set TARGET_URL to the local OpenAI-compatible endpoint}"

if [ -n "${RELAY_URL:-}" ]; then
    set -- --relay-url "$RELAY_URL" "$@"
fi

exec spectral-bridge start \
    --adapter pass-through \
    --target "$TARGET_URL" \
    --port "${ADAPTER_PORT:-8840}" \
    "$@"
