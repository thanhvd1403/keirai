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
├── .gitignore           # Ignores config.toml (secrets), sessions.db, tools/, cache/, logs/, __pycache__
├── main.py              # Entry point: polling loop, commands, routing, streaming, tool loop, /stop
├── config.py            # Config loading (TOML + env overrides)
├── telegram.py          # Telegram Bot API client (raw HTTP, multipart, rich messages, live edits)
├── md2tg.py             # Markdown -> Telegram HTML, message splitting
├── providers.py         # OpenCode Zen/Go clients (blocking + streaming), model stats cache
├── sessions.py          # SQLite session store (history, models, topics)
├── tools.py             # LLM tool registry: files, shell (blacklist+timeout), dispatch
├── websearch.py         # Parallel Search MCP client (free web_search/web_fetch, stdlib HTTP)
├── browser.py           # Lightpanda wrapper (optional browse tool, subprocess on demand)
├── cli.py               # Session management CLI (list/delete/rename, no Telegram needed)
├── deploy/keirai.service# systemd unit (keep-alive + autostart)
├── deploy/install_lightpanda.sh # installs the browser binary into ./tools/
├── config.example.toml  # Config template (TOML, comments allowed)
└── tests/               # Unit tests (unittest, stdlib)
    ├── test_md2tg.py    #   markdown conversion, splitting, rich split, tables, lists
    ├── test_config.py   #   config loading, access control
    ├── test_providers.py#   provider parsing, cache, multipart, session header
    ├── test_tools.py     #   tool specs, file ops, shell safety/interrupt
    ├── test_websearch.py #   MCP client parsing, handshake, tool args
    ├── test_browser.py   #   lightpanda discovery + subprocess wrapper
    └── test_main.py      #   routing, commands, sessions, streaming, compaction, CLI,
                          #   tool loop, /stop, reply-to, edit-mode streaming
```

Run tests: `python -m unittest discover -s tests`

## Current Status

P0 + P1 + P2 complete and tested (157 tests). The bot does:
- Long polling with access control (allow-list by user ID/username, or allow all)
- Config in TOML (`config.toml`, comments allowed) with env overrides
- AI chat via OpenCode Zen / OpenCode Go (OpenAI-compatible), history persisted per session in SQLite
- Sessions via Telegram topics (Bot API 9.3+ private-chat topics - enable "Threaded mode" in the @BotFather Mini App): `/new [name]`, `/rename <name>`, per-topic context isolation; `/reset-all` wipes everything (2-string confirmation, deletes tracked topics)
- AI answers sent as Bot API 10.1 Rich Messages: markdown passthrough (native tables, task lists, headings, formulas, 32k chars), thinking as collapsible `<details>`; `rich_messages` config toggle + automatic fallback to regular messages
- Live streaming in private chats via `sendRichMessageDraft` (reasoning as `<tg-thinking>`, then answer tokens; `stream_drafts` toggle, falls back to blocking)
- Session persistence in SQLite (`sessions.db`): history, per-session model, topic registry - survives restarts; `/delete` clears one session's context
- Context window management: `context_limit_chars` limit, `/compact` manual + auto-compact (summarize older messages, keep last 4)
- CLI: `python cli.py list|delete|rename` for session admin without Telegram
- Agent tools (OpenAI function-calling, `tools.py`): `read_file`/`write_file`/`edit_file`, `bash` (destructive-command blacklist, agent-settable 1-300s timeout, long output saved to `logs/bash-out-*.txt`), `web_search`/`web_fetch` (Parallel Search MCP - free, no key), optional `browse` (Lightpanda, install via `deploy/install_lightpanda.sh` -> `./tools/lightpanda`)
- Tool rounds run in-turn (max 6 rounds) and are not persisted - only the final answer lands in history
- `/stop` interrupts a running reply/tool: an update-watcher thread owns getUpdates, intercepts /stop mid-turn, kills running `bash`, and records a neutral "[This run was interrupted by the user...]" marker in context
- Reply-to/quote context: replies and quoted parts are injected into the prompt as annotations (item 17)
- Dynamic `/help` built from the command registry; commands registered with `setMyCommands`; OpenCode Go session header (`x-opencode-session`) + `keirai/0.1` user agent
- Regular-message fallback path: Markdown -> Telegram HTML rendering with 4096-char splitting (never breaks code blocks; tables as aligned `<pre>`, nested list bullets)
- `/test_md` (regular pipeline) and `/test_rich` (rich pipeline) for live rendering verification
- `/models` lists models with auto-fetched stats (context window, costs) from provider APIs, cached on disk; `/model provider/id` switches models
- Photos -> vision models; media round-trip test via caption `media_test`
- Logging: stderr + `logs/keirai.log` (rotating)

Not implemented yet: P3 (custom OpenAI-compatible providers, interactive setup script) - see TODO.md.

## External Components & Licenses

- Keirai code: AGPLv3 (LICENSE)
- Lightpanda browser (optional tool): **AGPLv3** - same license, compatible; invoked as a separate process, never linked
- Parallel Search MCP: commercial service with a free anonymous tier; we only make REST calls (no license implications)

## License

Keirai is licensed under **AGPLv3** (see LICENSE). Keep this in mind when adding dependencies: all project code must remain AGPLv3-compatible. Library licenses must be checked before implementing (see TODO.md).

## Future Development

See **TODO.md** for planned features. All code should be written with awareness of what's coming - don't paint yourself into corners.

## Memory Budget

Target: Agent must run comfortably within 256MB RAM, leaving room for the OS on a 1GB server.