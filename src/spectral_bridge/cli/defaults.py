"""Spectral platform defaults for the CLI.

The SDK (spectral_bridge.client) is vendor-neutral; only the CLI applies
these defaults. Point --relay-url (or SPECTRAL_BRIDGE_RELAY_URL) at any
conforming relay server to use another platform.
"""

SPECTRAL_RELAY_URL = "wss://bridge.spectral.principled.app/connect"
