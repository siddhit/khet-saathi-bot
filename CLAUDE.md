# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Khet-Saathi bot is a WhatsApp bot deployed on Vercel (Python serverless runtime) that integrates the Anthropic Claude API to provide agricultural advice. Incoming WhatsApp messages arrive via Meta's Graph API webhook and responses are sent back through the same API.

## Architecture

- **Entrypoint**: `api/webhook.py` — a `BaseHTTPRequestHandler` subclass named `handler`, which is the contract Vercel's Python runtime requires.
- **Routing**: `vercel.json` rewrites `/webhook` → `/api/webhook`. All WhatsApp traffic hits a single endpoint.
- **GET**: Webhook verification handshake (Meta calls this once on registration).
- **POST**: Incoming message handler. Must return HTTP 200 to Meta immediately — retries are triggered by any non-200. Processing happens after the response is written (safe in Vercel's single-invocation model).
- **Phase 0** (current): Synchronous — process and reply within the same invocation.
- **Phase 1+** (planned): Enqueue job immediately, return 200, process asynchronously. Required once AI response time exceeds ~3–5 seconds.

## Required Environment Variables

Set these in Vercel's dashboard (not in `.env` committed to source control):

| Variable | Description |
|---|---|
| `WHATSAPP_VERIFY_TOKEN` | Arbitrary string set in Meta Business Console when registering the webhook |
| `WHATSAPP_ACCESS_TOKEN` | Bearer token from Meta App Dashboard for Graph API calls |
| `WHATSAPP_PHONE_NUMBER_ID` | Numeric phone number ID from the WhatsApp Business Account |
| `ANTHROPIC_API_KEY` | API key for the Anthropic Claude API |

For local development, create a `.env.local` file (gitignored) with these values and run `vercel dev`.

## Lint & Format

```bash
ruff check .        # Lint
ruff format .       # Format
```

Config is in `pyproject.toml` under `[tool.ruff]`. Run both before committing.

## Local Development

```bash
vercel dev          # Start local serverless runtime on localhost:3000
```

Expose the local server to Meta's webhook verification using ngrok:

```bash
ngrok http 3000
```

Then register the ngrok HTTPS URL + `/webhook` in Meta's Business Console. The `WHATSAPP_VERIFY_TOKEN` must match what is set in the console.

## AI Integration (Anthropic Claude API)

Use the `anthropic` Python SDK. Default to `claude-sonnet-4-6` for production responses (balance of quality and latency). Use `claude-haiku-4-5-20251001` for any classification or pre-filtering steps where speed matters.

```bash
pip install anthropic
```

Add `anthropic` to `pyproject.toml` dependencies and `requirements.txt`.

## Webhook Payload Quirks

- **Status updates** (read receipts, delivery confirmations) arrive on the same POST endpoint as real messages. They have a `statuses` key but no `messages` key — always filter these out before processing.
- **Non-text messages** (images, audio, location, etc.) must be handled explicitly; `message["type"] != "text"` is the guard.
- The `entry[0].changes[0].value.messages[0]` path is the standard extraction path for text messages. Wrap in try/except for KeyError/IndexError.

## Deployment

Push to `main` triggers an automatic Vercel deployment. No build step — Vercel installs dependencies from `requirements.txt` directly.

## Branch & PR Conventions

- Use feature branches; open a PR for each change.
- Tag `@Codex` on PRs for automated review before merging.
- Branch naming: `feature/<short-description>` or `fix/<short-description>`.
