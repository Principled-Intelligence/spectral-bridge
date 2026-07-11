# pass-through adapter

Transparent proxy for targets that already expose an OpenAI-compatible endpoint — `POST /v1/chat/completions` (Chat Completions API) and/or `POST /v1/responses` (Responses API). Each request is forwarded to the matching path on the target.

For full documentation see [spectral.principled.app/docs](https://spectral.principled.app/docs/spectral-bridge/adapters/passthrough).
