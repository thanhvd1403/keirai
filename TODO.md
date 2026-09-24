# Keirai - TODO

> Features planned for future implementation. Write code with these in mind.
> Priorities: P0 = highest.

## P0 - Telegram Bot with Markdown & Reasoning Display

### 1. Library research & decision
- [x] Compare options: python-telegram-bot, aiogram, raw HTTP calls
  - Evaluate: memory footprint, effort saved, maintenance status
- [x] Check license of chosen library before implementing (must be compatible)
  - Decision: raw HTTP with stdlib only - zero dependencies, zero license risk, minimal RAM

### 2. Bot skeleton & access control
- [x] Config system (TOML `config.toml`, comments allowed; env var overrides; Python 3.11+ via stdlib tomllib)
- [x] Config for allowed users (list of user IDs/usernames), with an option to allow all users
- [x] Ignore/reject messages from users not on the allow list
- [x] Minimal bot: receive message → send it back (proof of life)
- [x] Decide long polling vs webhook (polling first: simpler, no TLS needed)

### 3. Markdown processing
- [x] Implement full markdown → Telegram conversion (Telegram MarkdownV2 or HTML parse mode)
  - Handle: bold, italic, code, code blocks, links, lists, headers
  - Escape special characters correctly (Telegram MarkdownV2 is strict)
  - Decision: HTML parse mode (robust, supports expandable blockquotes)
- [x] Message splitting at Telegram's 4096 char limit
  - Split without breaking markdown (e.g. don't cut an open code block in half)
- [x] Test interface: command (e.g. `/test_md <message>`) that runs the markdown pipeline and sends the result to Telegram
  - Lets us verify rendering without a live AI backend

### 4. Thinking / reasoning display
- [x] Render "thinking" / reasoning content separately from the answer (e.g. collapsible blockquote or spoiler)
  - Done via expandable blockquote
- [x] Config option: default on/off for showing thinking
- [x] Bot commands to toggle at runtime (e.g. `/thinking on`, `/thinking off`)
- [x] Persist the user's toggle choice (in-memory first, storage later)

### 5. AI providers: OpenCode Go & OpenCode Zen
- [x] Support OpenCode Go as a model provider
- [x] Support OpenCode Zen as a model provider
  - OpenAI-compatible: POST {base}/chat/completions, reasoning from `reasoning_content`/`reasoning`
- [x] List available models from each provider (slash command, e.g. `/models`)
- [x] Auto-retrieve model stats: max context window, costs, ...
  - GET {base}/models, normalized + cached to disk (24h TTL), falls back to cache when offline

### 6. Images & media files
- [x] Receive photos and media files from users (download via Telegram file API)
- [x] Send images/media files back to users (correct type handling)
- [x] Pass images to AI providers (vision/multimodal models, where supported)
- [x] Size limits + memory-safe handling (don't load big files fully into RAM)
  - Download cap 20 MB (Bot API limit), upload cap 45 MB, config `max_file_mb`

### 7. Logging & ops
- [x] Log to file: `logs/keirai.log` (rotating 2MB x 3, plus stderr for journald)
  - Stream live: `tail -f ~/keirai/logs/keirai.log` (or `less +F`, `journalctl -u keirai -f`)
- [x] `/reset-all`: wipes everything for the chat - all sessions/context and deletes the Telegram topics the bot created
  - Confirmation: bot generates two random strings, user must type them back (e.g. `/reset-all KX3P QW9Z`)
  - Hidden from /help (tracked topics only - topics created before this feature can't be listed via Bot API)

### 8. Rich Messages (Bot API 10.1)
- [x] Send ALL AI answers via `sendRichMessage` - markdown passed through, Telegram's server parses GFM: native tables, task lists, headings, `$$formulas$$`, 32768 char limit
  - `rich_messages` config kill-switch (default on); automatic fallback to regular messages on TelegramError
- [x] Thinking display as native collapsible `<details><summary>` block (rich HTML path, converted via our md2tg)
- [x] `/test_rich` command + regular-message fallback kept for old paths (`/test_md`)
- [x] Streaming drafts: `sendRichMessageDraft` (private chats) while generating
  - Reasoning shown live as `<tg-thinking>` placeholder, then answer markdown streamed (first flush immediate, then every 1.5s)
  - Final `sendRichMessage` persists the message (draft is ephemeral, ~30s)
  - `stream_drafts` config toggle; automatic fallback to blocking call on any error
- [x] `==marked==`, footnotes, `$$formulas$$`, inline HTML - work automatically via GFM markdown passthrough
- [x] Media blocks in answers: `![alt](url)` passes through natively on the rich path; the regular path degrades it to a link (Telegram HTML has no `<img>`)
- [x] `editMessageText(rich_message=...)` for live edits after send
  - Non-private chats stream via send placeholder + rich edits (drafts are private-only); first chunk of the final answer edits the placeholder in
  - Any edit failure deletes the placeholder and sends fresh

## P1 - Session Management & Context Control

### 9. Sessions via Telegram topics
- [x] Use Telegram's forum topic API: each topic = 1 chat session with its own context
  - Bot API 9.3 (Dec 2025) brought topics to private chats with bots - enable topic mode via @BotFather; `getMe.has_topics_enabled` confirms it
  - Works in forum supergroups the same way (message_thread_id routing)
  - Fallback when topics are disabled: `/new` clears the current session in place
- [x] Create sessions: `/new [name]` creates a new topic + fresh session (auto-names "Session N" without arg)
- [x] Rename sessions: `/rename <name>` renames the current topic/session
- [x] Keep context strictly isolated per topic (history + model choice keyed by (chat, thread))
- [x] Storage for session context: SQLite `sessions.db` (stdlib sqlite3) - history, model choice and topic registry
  - Write-through after every turn, loaded on startup - sessions survive restarts and are CLI-manageable

### 10. /delete command
- [x] `/delete` clears the current session's context (the conversation memory) and its db row
- [x] Do NOT delete the Telegram topic - send a confirmation that the session is deleted and note the user can delete the topic manually

### 11. Context window management
- [x] Configurable limit for context size: `context_limit_chars` (chars; default 120000 ~ 30k tokens - no tokenizer dependency)
- [x] `/compact` slash command: manually compact the current session's context (reports before/after sizes)
- [x] Auto-compact when context exceeds the configured limit (falls back to trimming oldest on failure)
- [x] Compaction strategy: summarize older messages via the provider into a `[Summary of earlier conversation]` marker message, keep the last 4 messages

### 12. CLI fallback for session management
- [x] `python cli.py list|delete|rename` - works without Telegram, operates directly on sessions.db
- [x] List sessions (chat, thread, name, message count, model, last active)
- [x] Rename sessions (stored session/topic name)
- [x] Delete sessions (context only; prints a hint that the Telegram topic is untouched)

### 13. /help command listing
- [x] `/help` lists all available commands dynamically - built from the COMMANDS registry, hidden commands excluded
- [x] Register commands with `setMyCommands` at startup so they show up in the bot's UI menu

## P2 - Core Tools & Message Context

> First real tools for the agent. Built on OpenAI function-calling specs
> (`tools.py` registry: `available_specs(cfg)` + `execute(cfg, name, args, ctx)`);
> tool rounds are not persisted to history - only the final answer is.

### 14. File & shell tools
- [x] `read_file` - read file contents (size caps: 200 KB hard, default 50 KB, `max_chars` arg)
- [x] `write_file` - create/overwrite files (creates parent directories)
- [x] `edit_file` - targeted edit (exact match, must be unique - errors otherwise, no full rewrites)
- [x] `bash` - execute shell commands
  - Timeout: config `bash_timeout` (default 30s), agent-settable per call (`timeout_seconds`, clamped 1-300)
  - Safety per agreed decision: destructive-command blacklist (rm -rf on /, mkfs, dd to /dev, shutdown/reboot, fork bombs, curl|sh, ...); NO path sandbox (single trusted operator)
  - Output too long -> saved to `logs/bash-out-*.txt`, model told to `read_file` it
  - Interruptible: polls the /stop flag and kills the process mid-run

### 15. Web search & fetch (Parallel API)
- [x] `web_search` via Parallel Search MCP (free, anonymous - no API key needed)
- [x] `web_fetch` via the same MCP (excerpts or `full_content`; handles JS pages and PDFs)
- [x] API key via config: optional `parallel_api_key` / `PARALLEL_API_KEY` only raises rate limits
- [x] License/pricing checked: Search MCP is free at https://search.parallel.ai/mcp
  (paid API from $1/1k requests, free monthly allowance); commercial service - no license issue (REST calls only)
- [x] Stdlib streamable-HTTP MCP client (`websearch.py`): initialize -> notifications/initialized -> tools/call, JSON or SSE bodies

### 16. Browser (Lightpanda)
- [x] License checked: Lightpanda is **AGPLv3** - same as Keirai, fully compatible (separate process, never linked)
- [x] Drive it from Python: `lightpanda fetch --dump markdown <url>` via subprocess - renders JS, prints markdown; no CDP/WebSocket client needed (CDP reserved for future interactive needs like clicks/forms)
- [x] Start/stop on demand: one subprocess per call, never resident; telemetry disabled (`LIGHTPANDA_DISABLE_TELEMETRY=true`)
- [x] Install script: `bash deploy/install_lightpanda.sh` puts the binary in `keirai/tools/lightpanda`; the `browse` tool only appears to the model after install (or when `lightpanda` is on PATH)

### 17. Reply-to-message context
- [x] Detect replies via `reply_to_message` and inject the referenced message as an annotation ("user is replying to this message (from @x): ...")
- [x] Detect quoted text (`quote` object) - "user is quoting this part of a message: ..."
- [x] Handle replies to the bot's own messages and older messages (content arrives in the reply object; capped at 1500 chars to respect context limits; no-text replies noted as media/rich message)

### 18. /stop - interrupt running work
- [x] `/stop` interrupts the reply/tool turn running in the current chat+thread
  - The bot is single-threaded, so an update-watcher thread owns getUpdates and feeds a queue; it intercepts /stop during an active turn (other messages are queued, never lost)
  - Interruption points: every streaming delta, every tool round, before/after each tool, and mid-`bash` (process killed)
  - Context marker written for the next turn: "[This run was interrupted by the user before it finished - any running command/tool did not complete.]" (says the user interrupted, without referencing the /stop command)
  - Partial live message (edit mode) is deleted; user gets "stopped - the running reply/tool was interrupted"
  - Idle `/stop` replies "nothing is running right now"

## P3 - Custom OpenAI-Compatible Providers

### 18. Custom providers
- [ ] Support custom OpenAI-compatible providers via config (base URL + API key)
- [ ] List available models per provider (`/models` should include them)
- [ ] Auto-retrieve model stats where the provider supports it (max context window, costs, ...)
- [ ] Per-provider/per-model overrides in config (e.g. manual context window, cost values for providers that don't report stats)

### 19. Interactive setup script
- [ ] `python setup.py` wizard that:
  - Prompts for bot token (validate live via `getMe` before saving)
  - Prompts for provider API keys (validate by fetching `/models`)
  - Asks for allowed users (IDs/usernames) or allow-all
  - Writes `config.json` in the project dir
- [ ] Optional keep-alive service install, per user choice at the end:
  - Detect systemd; if present, offer to install the unit with correct User/paths, then `daemon-reload` + `enable --now`
  - Skip gracefully (with a printed hint) when systemd is unavailable
- [ ] Rerunnable: detect existing config and offer to edit values instead of overwriting
