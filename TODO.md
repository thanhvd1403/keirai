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
- [ ] Media blocks in answers (rich media needs HTTP URLs) and `editMessageText(rich_message=...)` for live edits after send

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

> First real tools for the agent. Build on the tool registry/interface design.

### 14. File & shell tools
- [ ] `read_file` - read file contents (with size limits)
- [ ] `write_file` - create/overwrite files
- [ ] `edit_file` - targeted edit (search/replace, not full rewrite)
- [ ] `bash` - execute shell commands
  - Timeout + output size limits
  - Safety measures (TBD: confirmation, blacklist, sandboxing)

### 15. Web search & fetch (Parallel API)
- [ ] `web_search` via Parallel Search API (parallel.ai, built for AI agents)
- [ ] `web_fetch` via Parallel Extract API (full/excerpted content, handles JS-heavy pages and PDFs)
- [ ] API key via config
- [ ] Check Parallel license/pricing terms before implementing

### 16. Browser (Lightpanda)
- [ ] Use Lightpanda headless browser (lightpanda.io) for pages needing full JS rendering
  - Written in Zig, ~16x less RAM than headless Chrome - fits our memory budget
- [ ] Drive it from Python via CDP (Playwright/Puppeteer-compatible) or its HTTP API
- [ ] Start/stop browser process on demand (don't keep it resident)
- [ ] Check Lightpanda license compatibility before implementing

### 17. Reply-to-message context
- [ ] Detect replies via `reply_to_message` on incoming Telegram updates
- [ ] Detect quoted text (Telegram `quote` object) and distinguish "replying to this message" vs "quoting this part"
- [ ] Inject the referenced message into the LLM context as an annotation, e.g. "user is replying to this message: ..." / "user is quoting: ..."
- [ ] Handle replies to the bot's own messages and to older messages (store/fetch referenced content, respect context limits)

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
