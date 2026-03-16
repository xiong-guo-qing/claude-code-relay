# Claude Code Relay

Anthropic-compatible relay server for using GPT/OpenAI-compatible models with Claude Code CLI.

## What it provides

Minimal Anthropic-style API:
- `GET /health`
- `GET /v1/models`
- `POST /v1/messages`

Then translates requests to an OpenAI-compatible backend (`/chat/completions`).

## Current backend (default in this workspace)

- Upstream base URL: `http://127.0.0.1:8317/v1`
- Upstream path: `/chat/completions`
- Default mapped model: `gpt-5.2`

## Run

Managed by systemd user service in this environment:

```bash
systemctl --user restart claude-code-relay.service
systemctl --user status claude-code-relay.service
```

Or run directly:

```bash
python3 server.py
```

Default listen: `127.0.0.1:8318`

## Configure models (no code changes)

Relay exposes model IDs based on `config.json -> model_map` keys.

- Add/modify a mapping:

```bash
cd /home/xgq/claude-code-relay
python3 relayctl.py add claude-sonnet-4-6 gpt-5.2
```

- Remove a mapping:

```bash
python3 relayctl.py del claude-opus-4
```

- List mappings:

```bash
python3 relayctl.py list
```

### Hot reload

`server.py` hot-reloads `config.json` on change (no restart required).

## Notes

- `/v1/models` is derived from the configured aliases (excluding `default`).
- `/v1/messages` requires the relay API key (header: `x-api-key` / `anthropic-api-key` / `Authorization: Bearer`).
