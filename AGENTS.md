# Keirai - Lightweight AI Agent

## Goal

Build an extremely lightweight AI agent that runs on a low-resource server (1GB RAM). The agent performs tasks on the server and interacts with users via Telegram Bot API.

## Core Rules

1. **Keep it lightweight** - Every dependency, every feature must justify its memory/CPU cost. If it can be done without a library, do it without a library.
2. **Keep it expandable** - The architecture must allow adding new tools and capabilities without major refactoring.
3. **No unnecessary abstractions** - Use the simplest solution that works.
4. **Prefer built-in modules** - Only add external dependencies when the built-in alternative is truly insufficient.
5. **Ask when unsure** - Ask the user for clarification on everything that is ambiguous or needs a decision; don't guess.

## Tech Stack

- **Language**: Python 3.11+ (tomllib is used from the stdlib for config parsing)
- **Telegram**: raw HTTP calls with stdlib only (python-telegram-bot/aiogram evaluated; both pull heavy asyncio HTTP stacks for little gain - the Bot API is plain JSON over HTTPS)
- **AI Backend**: OpenCode Zen and OpenCode Go via their OpenAI-compatible endpoints (`https://opencode.ai/zen/v1`, `https://opencode.ai/zen/go/v1`)
- **Config**: TOML (`config.toml`, comments allowed) - chosen over JSON (no comments) and YAML (would need PyYAML)
- **Dependencies**: none - everything runs on the Python standard library

## Project Structure

```
keirai/
├── AGENTS.md            # This file - project goals and rules
├── TODO.md              # Planned features
├── LICENSE              # AGPLv3
├── .gitignore           # Ignores config.json (secrets), cache/, __pycache__
├── main.py              # Entry point: polling loop, commands, routing
├── config.py            # Config loading (JSON + env overrides)
├── telegram.py          # Telegram Bot API client (raw HTTP, multipart)
├── md2tg.py             # Markdown -> Telegram HTML, message splitting
├── providers.py         # OpenCode Zen/Go clients, model stats cache
├── config.example.toml  # Config template (TOML, comments allowed)
└── tests/               # Unit tests (unittest, stdlib)
    ├── test_md2tg.py    #   markdown conversion + splitting
    ├── test_config.py   #   config loading, access control
    ├── test_providers.py#   provider parsing, cache, multipart
    └── test_main.py     #   routing, commands, AI flow (offline, faked)
```

Run tests: `python -m unittest discover -s tests`

## Current Status

P0 complete and tested (78 tests). The bot does:
- Long polling with access control (allow-list by user ID/username, or allow all)
- Config in TOML (`config.toml`, comments allowed) with env overrides
- AI chat via OpenCode Zen / OpenCode Go (OpenAI-compatible), in-memory history per chat
- Sessions via Telegram topics (Bot API 9.3 private-chat topics, needs @BotFather toggle): `/new [name]`, `/rename <name>`, per-topic context isolation; `/reset-all` wipes everything (2-string confirmation, deletes tracked topics)
- AI answers sent as Bot API 10.1 Rich Messages: markdown passthrough (native tables, task lists, headings, formulas, 32k chars), thinking as collapsible `<details>`; `rich_messages` config toggle + automatic fallback to regular messages
- Regular-message fallback path: Markdown -> Telegram HTML rendering with 4096-char splitting (never breaks code blocks; tables as aligned `<pre>`, nested list bullets)
- `/test_md` (regular pipeline) and `/test_rich` (rich pipeline) for live rendering verification
- `/models` lists models with auto-fetched stats (context window, costs) from provider APIs, cached on disk; `/model provider/id` switches models
- Photos -> vision models; media round-trip test via caption `media_test`
- Logging: stderr + `logs/keirai.log` (rotating)

Not implemented yet: context compaction, session persistence, tools (P1+ - see TODO.md).

## License

Keirai is licensed under **AGPLv3** (see LICENSE). Keep this in mind when adding dependencies: all project code must remain AGPLv3-compatible. Library licenses must be checked before implementing (see TODO.md).

## Future Development

See **TODO.md** for planned features. All code should be written with awareness of what's coming - don't paint yourself into corners.

## Memory Budget

Target: Agent must run comfortably within 256MB RAM, leaving room for the OS on a 1GB server.