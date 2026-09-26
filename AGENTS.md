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
├── main.py              # Entry point: polling loop, commands, routing, streaming, tool loop, /stop, topic flow, /session, titles
├── config.py            # Config loading (TOML + env overrides)
├── telegram.py          # Telegram Bot API client (raw HTTP, multipart, rich messages, live edits)
├── md2tg.py             # Markdown -> Telegram HTML, message splitting
├── providers.py         # OpenCode Zen/Go clients (blocking + streaming), usage capture, models.dev metadata + pricing cache
├── sessions.py          # SQLite session store (history, models, topics, session meta: ids + cost/token accumulators)
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
    ├── test_providers.py#   provider parsing, usage capture, models.dev metadata, cost math, cache, session header
    ├── test_tools.py     #   tool specs, file ops, shell safety/interrupt
    ├── test_websearch.py #   MCP client parsing, handshake, tool args
    ├── test_browser.py   #   lightpanda discovery + subprocess wrapper
    └── test_main.py      #   routing, commands, sessions, streaming, compaction, CLI,
                          #   tool loop, /stop, reply-to, edit-mode streaming,
                          #   /context, /cost, provider fallback, usage accounting
```

Run tests: `python -m unittest discover -s tests`

## Current Status

P0 + P1 + P2 + P3 + P4 complete and tested (220 tests). The bot does:
- Long polling with access control (allow-list by user ID/username, or allow all)
- Config in TOML (`config.toml`, comments allowed) with env overrides
- AI chat via OpenCode Zen / OpenCode Go (OpenAI-compatible), history persisted per session in SQLite
- Sessions via Telegram topics (Bot API 9.3+ private-chat topics - enable "Threaded mode" in the @BotFather Mini App): `/new [name]`, `/rename <name>`, per-topic context isolation; `/reset-all` wipes everything (2-string confirmation, deletes tracked topics)
- AI answers sent as Bot API 10.1 Rich Messages: markdown passthrough (native tables, task lists, headings, formulas, 32k chars), thinking as collapsible `<details>`; `rich_messages` config toggle + automatic fallback to regular messages
- Live streaming in private chats via `sendRichMessageDraft` (reasoning as `<tg-thinking>`, then answer tokens; `stream_drafts` toggle, falls back to blocking)
- Session persistence in SQLite (`sessions.db`): history, per-session model, topic registry - survives restarts; `/delete` clears one session's context
- Context window management: `/context` shows used/max against the model's **real context window** (models.dev tokens, chars shown too); auto-compact fires at **100% of that window** by default (`context_limit_chars` in config.toml overrides); `/compact` manual (summarize older messages, keep last 4)
- CLI: `python cli.py list|delete|rename` for session admin without Telegram
- Agent tools (OpenAI function-calling, `tools.py`): `read_file`/`write_file`/`edit_file`, `bash` (destructive-command blacklist, agent-settable 1-300s timeout, long output saved to `logs/bash-out-*.txt`), `web_search`/`web_fetch` (Parallel Search MCP - free, no key), optional `browse` (Lightpanda, install via `deploy/install_lightpanda.sh` -> `./tools/lightpanda`)
- Tool rounds run in-turn **uncapped** (until the model stops asking for tools; `/stop` interrupts a runaway turn) and are not persisted - only the final answer lands in history; every turn logs `tool round N: X call(s)` and `turn done: ... tool_rounds=...`
- `/stop` interrupts a running reply/tool: an update-watcher thread owns getUpdates, intercepts /stop mid-turn, kills running `bash`, and records a neutral "[This run was interrupted by the user...]" marker in context
- Reply-to/quote context: replies and quoted parts are injected into the prompt as annotations (item 17)
- Dynamic `/help` built from the command registry; commands registered with `setMyCommands`; anonymized app-wide session header (`x-opencode-session: keirai` - no chat ids leave the machine) + `keirai/0.1` user agent
- Regular-message fallback path: Markdown -> Telegram HTML rendering with 4096-char splitting (never breaks code blocks; tables as aligned `<pre>`, nested list bullets)
- `/test_md` (regular pipeline) and `/test_rich` (rich pipeline) for live rendering verification
- `/models` lists models with context/cost stats from **models.dev** (OpenCode's own catalog, daily sync to `cache/models_meta.json`); `/model provider/id` switches models
- **Go-first inference**: default model `go/mimo-v2.6-flash`; on a provider error the call retries once on the other provider when it has a key AND its catalog serves the model
- **Usage accounting**: every call's `usage` (blocking + streaming via `stream_options.include_usage`) is accumulated per session in `sessions.db` (`session_meta`: session id `yyyymmdd-hhmm-4hex`, tokens in/out/cached read/cached write, notional cost with cached-read discount)
- `/context` shows context usage as `~used / max tokens` (max = model window from models.dev; chars + message count too), the auto-compact threshold on its own line, session name/id/model, token totals; `/cost` adds the session's notional total cost
- **Topic flow** (switchable via `/topic`, flag persisted in `config.toml`): entering requires Threaded mode ON **and** "Disallow users to create topics" ON (both checked live via getMe, typed yes/no confirmation); while ON, All accepts commands only (session-scoped ones rejected with a hint, `/model global` allowed) and every message in a topic needs a bound session - unbound topics get a rejection hint
- `/session [page N | N | <id>]`: lists sessions (name, id, timestamp, last-message preview - last-active first, current session excluded), switches them; in topic flow switching goes through typed per-slot prompts (switch-here / create-new / ping / do-nothing), with dead-topic ping recovery (`400 message thread not found` -> auto new topic)
- Sessions move between **slots** (`thread_id` >0 = bound to that topic, main chat = None, negative = parked/unbound); `/new` creates a topic+bound session in topic flow, or parks the old session and starts blank in normal flow
- **Agent titles**: after a session's first message one extra completion (default model) names it, prompted for a **3-4 word Title Case title** (no mechanical cap; light cleanup only - `Title:` marker/quotes/punctuation stripped); media-only messages named from the media when vision-capable; `/rename` mirrors session+topic; `/model global <id>` writes the default to `config.toml` (comments preserved)
- Photos -> vision models; media round-trip test via caption `media_test`
- Logging: stderr + `logs/keirai.log` (rotating)

Not implemented yet: P5 (custom OpenAI-compatible providers, interactive setup script) - see TODO.md. Topic flow is unit-tested; the live walkthrough on the dev/production bot is still pending.

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