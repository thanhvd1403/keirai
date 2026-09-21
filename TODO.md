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
- [x] Config system (bot token via env var or config file)
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

## P1 - Session Management & Context Control

### 7. Sessions via Telegram topics
- [ ] Use Telegram's forum topic API: each topic = 1 chat session with its own context
  - Requires the bot to run in a forum-enabled supergroup (topics don't exist in private chats - decide fallback behavior)
- [ ] Create sessions: `/new` creates a new topic + fresh session
- [ ] Rename sessions: `/rename <name>` renames the current topic/session
- [ ] Keep context strictly isolated per topic
- [ ] Storage for session context (in-memory first; SQLite/files when CLI management needs it)

### 8. Context window management
- [ ] Configurable limit for context size (metric TBD: chars or tokens)
- [ ] `/compact` slash command: manually compact the current session's context
- [ ] Auto-compact when context exceeds the configured limit
- [ ] Compaction strategy (TBD): summarize older messages, keep recent ones

### 9. CLI fallback for session management
- [ ] Simple CLI entry point to manage sessions without Telegram (e.g. admin/recovery when bot is down)
- [ ] List sessions (id, name, size, last active)
- [ ] Rename sessions
- [ ] Delete sessions

## P2 - Core Tools & Message Context

> First real tools for the agent. Build on the tool registry/interface design.

### 10. File & shell tools
- [ ] `read_file` - read file contents (with size limits)
- [ ] `write_file` - create/overwrite files
- [ ] `edit_file` - targeted edit (search/replace, not full rewrite)
- [ ] `bash` - execute shell commands
  - Timeout + output size limits
  - Safety measures (TBD: confirmation, blacklist, sandboxing)

### 11. Web search & fetch (Parallel API)
- [ ] `web_search` via Parallel Search API (parallel.ai, built for AI agents)
- [ ] `web_fetch` via Parallel Extract API (full/excerpted content, handles JS-heavy pages and PDFs)
- [ ] API key via config
- [ ] Check Parallel license/pricing terms before implementing

### 12. Browser (Lightpanda)
- [ ] Use Lightpanda headless browser (lightpanda.io) for pages needing full JS rendering
  - Written in Zig, ~16x less RAM than headless Chrome - fits our memory budget
- [ ] Drive it from Python via CDP (Playwright/Puppeteer-compatible) or its HTTP API
- [ ] Start/stop browser process on demand (don't keep it resident)
- [ ] Check Lightpanda license compatibility before implementing

### 13. Reply-to-message context
- [ ] Detect replies via `reply_to_message` on incoming Telegram updates
- [ ] Detect quoted text (Telegram `quote` object) and distinguish "replying to this message" vs "quoting this part"
- [ ] Inject the referenced message into the LLM context as an annotation, e.g. "user is replying to this message: ..." / "user is quoting: ..."
- [ ] Handle replies to the bot's own messages and to older messages (store/fetch referenced content, respect context limits)

## P3 - Custom OpenAI-Compatible Providers

### 14. Custom providers
- [ ] Support custom OpenAI-compatible providers via config (base URL + API key)
- [ ] List available models per provider (`/models` should include them)
- [ ] Auto-retrieve model stats where the provider supports it (max context window, costs, ...)
- [ ] Per-provider/per-model overrides in config (e.g. manual context window, cost values for providers that don't report stats)
