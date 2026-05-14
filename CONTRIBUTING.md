# Contributing to spectral-bridge

Thanks for your interest in contributing. This project is built on an open protocol, and contributions from the community — bug reports, fixes, new adapters, documentation improvements, protocol feedback — are genuinely welcome.

## Before you start

If you're planning a substantial change (a new feature, a protocol modification, or a refactor that touches multiple files), please [open an issue](https://github.com/Principled-Intelligence/spectral-bridge/issues) first to discuss it. This saves everyone time — we can flag any concerns early, point you at related work, or confirm the approach before you invest effort in a pull request.

For small fixes — typos, bug fixes, clarifying documentation — feel free to open a pull request directly.

## Contributor License Agreement

Before we can merge your first contribution, you'll need to sign our [Contributor License Agreement](https://www.apache.org/licenses/icla.pdf). This is the standard Apache Individual CLA — it confirms that you have the right to contribute the code and grants the project the rights it needs to use and distribute it. You retain copyright in your contribution.

The CLA is signed once and covers all your future contributions. Our CLA bot will guide you through the process automatically when you open your first pull request — it takes about a minute.

If you're contributing on behalf of your employer, you may need a Corporate CLA instead. Open an issue and we'll help you sort it out.

## How to contribute

1. **Fork the repository** and create a branch for your change.
2. **Make your change.** Keep commits focused — one logical change per commit makes review easier.
3. **Test it.** If you're adding or modifying behavior, include tests. If the project's test setup isn't clear, ask in the issue or PR and we'll point you at the right files.
4. **Open a pull request** against `main`. Describe what the change does and why. Link any related issues.
5. **Sign the CLA** when prompted by the bot, if you haven't already.
6. **Respond to review feedback.** We aim to review PRs promptly; please be patient if it takes a few days.

## Reporting bugs and security issues

For regular bugs, please [open an issue](https://github.com/Principled-Intelligence/spectral-bridge/issues) with a clear description, steps to reproduce, and your environment details (Python version, OS, `spectral-bridge` version).

**Security vulnerabilities should not be reported as public issues.** Please email hello@principledintelligence.com instead, and we'll respond within a reasonable timeframe.

## Building adapters

If you've built an adapter for a specific AI system and want it included in the project, that's great — open a PR adding it under `adapters/<your-adapter-name>/`. See the existing [`pass-through`](https://github.com/Principled-Intelligence/spectral-bridge/blob/main/adapters/pass-through/) adapter for the expected structure. Adapters live in this repo to make discovery easy and to ensure they stay compatible with the client as the protocol evolves.

## Code of conduct

We expect everyone interacting with this project — in issues, pull requests, discussions, and any other channel — to be respectful and constructive. Disagreements are fine; personal attacks, harassment, and dismissive behavior are not. Maintainers may remove comments or block users who don't follow these expectations.

## Questions

If you're not sure where to start, or you have a question that doesn't fit the formats above, [open a discussion](https://github.com/Principled-Intelligence/spectral-bridge/discussions) and we'll help you find the right place.
