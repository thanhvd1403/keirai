# Keirai - TODO

> Features planned for future implementation. Write code with these in mind.
> Priorities: P0 = highest. **P0-P2 are complete.** Outstanding work is split
> into implementable phases:
> - **P3** - provider foundation: usage capture, models.dev metadata, Go-first priority, `/context` + `/cost`
> - **P4** - topic flow & session UX: `/topic` mode, `/session`, delivery rules, titles
> - **P5** - custom OpenAI-compatible providers + setup wizard (last)

## P0 - Telegram Bot with Markdown & Reasoning Display (complete)

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
  - ⚠️ **Live-probed 2026-09-25: the real `/models` endpoint returns NO stats** (every entry = `{id, object, created, owned_by}` only) -> the `limit`/`cost` normalization never populates in production; stats move to **models.dev** (see group 25, P3)

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
  - Verified live (2026-09-25): drafts work with `message_thread_id` inside private-chat topics, incl. `draft_id` reuse for animation -> streaming keeps working in topics, no fallback needed
  - Reasoning shown live as `<tg-thinking>` placeholder, then answer markdown streamed (first flush immediate, then every 1.5s)
  - Final `sendRichMessage` persists the message (draft is ephemeral, ~30s)
  - `stream_drafts` config toggle; automatic fallback to blocking call on any error
- [x] `==marked==`, footnotes, `$$formulas$$`, inline HTML - work automatically via GFM markdown passthrough
- [x] Media blocks in answers: `![alt](url)` passes through natively on the rich path; the regular path degrades it to a link (Telegram HTML has no `<img>`)
- [x] `editMessageText(rich_message=...)` for live edits after send
  - Non-private chats stream via send placeholder + rich edits (drafts are private-only); first chunk of the final answer edits the placeholder in
  - Any edit failure deletes the placeholder and sends fresh

## P1 - Session Management & Context Control (complete)

### 9. Sessions via Telegram topics
- [x] Use Telegram's forum topic API: each topic = 1 chat session with its own context
  - Bot API 9.3 (Dec 2025) brought topics to private chats with bots - enable via the **"Threaded mode" setting in the @BotFather Mini App** (t.me/BotFather?startapp, NOT the /mybots chat menu); `getMe.has_topics_enabled` confirms it - verified live (2026-09: createForumTopic -> "chat is not a forum" until enabled)
  - Works in forum supergroups the same way (message_thread_id routing)
  - Fallback when topics are disabled: `/new` clears the current session in place
- [x] Create sessions: `/new [name]` creates a new topic + fresh session (auto-names "Session N" without arg)
- [x] Rename sessions: `/rename <name>` renames the current topic/session
- [x] Keep context strictly isolated per topic (history + model choice keyed by (chat, thread))
- [x] Storage for session context: SQLite `sessions.db` (stdlib sqlite3) - history, model choice and topic registry
  - Write-through after every turn, loaded on startup - sessions survive restarts and are CLI-manageable
  - ⚠️ **Superseded by groups 20/22**: with topic flow OFF, messages in topics are inert + hinted (no per-topic sessions); the per-topic-isolation tests get rewritten around the switchable mode

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

## P2 - Core Tools & Message Context (complete)

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

## P3 - Provider Foundation: Metadata, Priority & Cost (complete)

> Everything providers.py / config / sessions.db level. No Telegram UX beyond
> two new read-only commands. Specs live in groups 25 + 21.

**Implementation points (in order):**
1. [x] `providers.py`: capture **`usage`** from every call - blocking responses (`usage` object, previously discarded) and streaming (`stream_options: {"include_usage": true}` + the usage-carrying chunk, whether `"choices": []` on Zen or attached to a choice chunk on Go) - via a `usage_out` dict handed to callers
2. [x] Swap the `x-opencode-session` header value to the **anonymized app-wide constant `keirai`** (`main.SESSION_ID`; no chat ids leave the machine, also passed to tool ctx - per-prefix cache hits unaffected)
3. [x] **models.dev daily sync** (group 25): one `api.json` fetch/24h -> extract `opencode` + `opencode-go` -> compact `cache/models_meta.json`; `/models` stats display now merges from it (the dead `/models` normalization stays as fallback)
4. [x] **Go-first priority** (group 25): built-in `default_model` -> `go/mimo-v2.6-flash`; `_alternate_provider()` + `pcall()` in `_ai_reply` - on `ProviderError` retry once via the other provider iff it has a key AND its catalog serves the model id
5. [x] sessions.db: new **`session_meta` table** (separate from `sessions` so the history REPLACE can't wipe it): `session_id` (`yyyymmdd-hhmm-4hex`, assigned at creation, **backfilled** for pre-existing rows), `name`, **`cost_usd`, `tokens_in`, `tokens_out`, `tokens_cached_read`, `tokens_cached_write`** (cleared together with the session by `/delete`/`/reset-all`)
6. [x] **`/context`** command (group 21): chars vs limit, message count, auto-compact trigger + session name/id/model + token totals
7. [x] **`/cost`** command (group 21): the context block + notional cost (cached-read discount applied) + token totals
8. [x] Tests for each (24 new: usage capture both paths, metadata parse/cache, cost math, fallback routing, command output, meta lifecycle/backfill) + docs (`config.example.toml`, AGENTS.md)

### 25. Model metadata (models.dev) & Go-first priority
*(moved here from P0 group 5 - was outstanding inside a completed phase)*

- [x] **Model metadata sync from models.dev** (the price/context source; decision 2026-09-25)
  - models.dev (`https://models.dev/api.json`) is OpenCode's own database (GitHub `anomalyco/models.dev` - "we use it internally in opencode") and is what OpenCode itself uses for pricing + context limits (V2 docs: "OpenCode includes a provider and model catalog from models.dev")
  - Verified live: providers **`opencode`** (= zen, `https://opencode.ai/zen/v1`, 113 models) and **`opencode-go`** (41 models) with `cost {input, output, cache_read}` per 1M, `limit {context, output}` and `modalities` (image input = vision detection) - e.g. `glm-5.3-flash`: $0.15/$0.50, ctx 1M, exactly matching the docs pricing table
  - Fetch: **daily** (24h TTL, same pattern as the models cache) - one `GET https://models.dev/api.json` (~4.9 MB; no per-provider endpoint exists, SPA swallows path guesses, repo per-model TOMLs would need 154 file fetches > unauth GitHub rate limit) -> extract just the two providers into a **compact derived cache** (few KB), discard the payload; transient parse spike of tens of MB, within the memory budget
  - Feeds: `/cost` price table (group 21), `/models` stats display (fixes the dead normalization), vision-capability detection (group 19 media naming)
  - Checked per request: Hermes Agent (`NousResearch/hermes-agent`) resolves **context length** via a multi-source chain ending at models.dev and only picks up pricing opportunistically from `/models` payloads - models.dev gives us both cost AND context in one place
- [x] **Go-first inference priority** (decisions 2026-09-25): infer on the **Go** key first, use **Zen** as a fallback
  - **Option (a) chosen**: built-in `default_model` flips to **`go/mimo-v2.6-flash`**; PLUS runtime error-fallback - on `ProviderError` from the primary provider, retry once on the other provider's endpoint **iff** it has a key **AND its catalog serves the same model id** (checked against the cached model list - never blind-retry `zen/gpt-5.2` on go); an explicit `/model zen/...` still starts on zen (and falls back to go on error)
  - Replaces current behavior: `_resolve_provider` routes by model prefix only; key-missing falls back to the other provider (prefer go); built-in default was `zen/glm-5.3-flash` -> with both keys present the default landed on **zen**

### 21. /context and /cost commands
- [x] `/context` shows the current context window usage of the session: chars used vs `context_limit_chars`, message count, whether an auto-compact would trigger - plus **session name, session id, active model**
- [x] `/cost` (new): same context block (used vs limit + the auto-compact trigger point) **+ total cost of this session**, plus session name, session id, active model
  - Cost research - **live-probed 2026-09-25** (service-account key; zen blocking + streaming, go, V2 inference): **no cost field anywhere** - not in the JSON body, not in response headers -> **sum locally**: capture `usage` after every API call, multiply by price
    - Blocking: standard `usage` object (returned today, currently discarded); Go additionally reports `prompt_tokens_details.cached_tokens` + `completion_tokens_details.reasoning_tokens`, zen's `prompt_tokens_details` comes back empty on cache misses
    - Streaming: `stream_options: {"include_usage": true}` is accepted; the final chunk before `[DONE]` has `"choices": []` + `usage` (chunks without `choices` are skipped today - must be captured)
    - Go requires the `x-opencode-session` header (else `400 MissingSessionID`); Zen **caches only when the header is sent** - value: anonymized app-wide constant `keirai` (decision)
    - ⚠️ **`GET /models` returns NO pricing/context metadata** (all 43 entries = `{id, object, created, owned_by}`; per-model GET 404, console pricing endpoints 404) -> the existing `/models` stats code (expects `limit`/`cost` keys) **never populates in production**
    - **Price source RESOLVED: models.dev** - daily sync spec lives in group 25 (extracts `opencode` + `opencode-go` cost/limit entries into a compact cache; also fixes the `/models` stats display)
    - ⚠️ **Free models cannot be called from the bot**: `403 FreeTierError - "OpenCode's free tier can only be used from within OpenCode"` (never point config at a `-free` model)
    - Console Usage API (`GET /console/api/v1/usage/export`, CSV) returns ground-truth `cost_micro_cents` + full token/cache/reasoning breakdown - but **no session attribution** and batch ranges only (24h/7d/30d) -> unusable for per-session `/cost`; fine as a manual cross-check (observed: Go subscription rows carry `cost_micro_cents = 0`)
    - Cache handling (verified live 2026-09-25): **cached READ is priceable on BOTH providers** - `prompt_tokens_details.cached_tokens` reports it; Zen only caches when the **`x-opencode-session` header** is sent (2 identical calls: no header -> `0`; with header -> `cached_tokens: 2048/2292` on glm-5.3-flash, `1152/1263` on kimi-k3); the header pins the routing **instance**, while the cache is keyed by **content prefix** -> one constant id keeps all traffic on one instance where every prefix warms (per-topic hits unaffected); note: Go's cache behavior is unproven (reported `cached_tokens: 0` even with a stable header); formula: `(prompt - cached) x cost_in + cached x cost_cache_read` (rates from models.dev `cost.cache_read`; model without a cache rate -> price all at input); missing/empty `prompt_tokens_details` -> cached = 0 (safe overestimate)
    - Cached WRITE still NOT priceable: chat/completions usage has no cache-creation field; the Anthropic-native `/v1/messages` endpoint reports `cache_creation_input_tokens`/`cache_read_input_tokens` natively BUT the gateway ignores explicit `cache_control` (all-zero counters probed) and **claude models are unreachable via chat/completions** (`503 Endpoint is unavailable` - yet still listed by `/models`, worth a listing filter) -> write tokens stay inside `prompt_tokens` at input price; keep models.dev `cost.cache_write` rates in the cache for future use (Console usage-export CSV `cache_write_*` columns remain provider-side ground truth: batch, no session attribution)
    - Tiered models (`context_over_200k` + `tiers` on some entries): v1 uses the base input/output rate; tier-boundary math only if a tiered model gets heavy use
  - Accumulated per session in sessions.db: **`cost_usd` (notional tokens x price) + `tokens_in`, `tokens_out`, `tokens_cached_read`, `tokens_cached_write`** (cleared together with the session by `/delete`)
  - Display decisions: `/cost` shows the **notional tokens x price** (Go's dollar limits are denominated that way even though the subscription bills $0); **`/context` and `/cost` both show the session totals: tokens in / out / cached read / cached write** (both providers report cached reads once the session header lands a cache hit; cached write stays 0 until an endpoint reports it)
- [ ] Both commands are session-scoped (work in topics, rejected in the All topic - **enforcement ships with P4**, group 20's delivery rules; the commands themselves are built and working in normal flow)

## P4 - Topic Flow & Session UX

> The switchable `/topic` mode with All-topic rules, `/session` switching,
> per-session prompts and agent titles. Specs live in groups 19, 20, 22.

**Implementation points (in order):**
1. [ ] **`/session` command** (group 22): list (name/id/timestamp/preview, last-active first, `[image]` placeholder), `page N`, `N` against current page, `<id>` immediate switch; never lists the current session
2. [ ] **Prompt machinery**: typed-choice prompts with **per-session slots** (one per topic + one for All), command-cancels-own-slot, wait forever, plain message = "do nothing" + normal handling (reuses `/reset-all` pending-confirm pattern)
3. [ ] **Topic-flow mode** (group 22): `topic_flow` flag write-through in config.toml; `/topic` enter (typed confirm, BOTH `getMe` preconditions with hints, no auto-created topic, hint `/new`) & exit (typed confirm, persist flag)
4. [ ] **Delivery rules** (group 20): All = commands only (+ hint every plain message), unbound-topic rejection hint, flow-off = topics fully inert + hint every message; rejected/allowed command lists in All
5. [ ] **Bindings & switching** (group 22): topic<->session binding table, switching matrix (normal / unbound / bound x All / in-topic), per-topic prompts, ping liveness + dead-topic recovery, `/stop` topic-only
6. [ ] **Titles** (group 19): extra naming completion after the first user message (default model, runs after the reply, media-aware via models.dev vision info); `/new` in All/topic creates topic + first-message title
7. [ ] **Mirror commands**: `/rename` renames session AND topic; `/model global <id>` surgical config.toml write (keep comments)
8. [ ] Rewrite the group-9 per-topic isolation tests around flow on/off; live dev-bot verification (entry preconditions, hints, ping recovery, drafts-in-topics already proven)

### 19. Agent-generated session titles
- [ ] New sessions get an agent-generated **very short** title (replaces the current "Session N"; `/new <name>` skips generation)
  - **Mechanism: one extra completion** after the first user message in the session, using **the session's active model - a new session always starts on the default model from config.toml, so use that** (decision)
  - Applied in **both flows**: normal flow (real names in the `/session` list) and topic flow (the topic is renamed to match - mirrors `/rename`, group 22)
  - Runs **after** the main reply so first-answer latency is unaffected; on failure keep the placeholder
  - **Media-only first message**: still named **based on the media** - the naming call includes the media itself when the model is vision-capable, else a type/filename descriptor ("photo", "report.pdf")

### 20. Topic-only delivery & All-topic rules
- [ ] All AI messages must be delivered inside a topic - **ignore every message sent to the "All" topic**
  - [x] Verified live (2026-09 dev bot): in topic flow, All-topic messages arrive with **NO `message_thread_id`** -> `thread_id is None` = All, `thread_id` set = topic session
  - ⚠️ When `allows_users_to_create_topics` (BotFather "allow user to create topics") is ON, sending from the All view makes the client **implicitly create a topic per message** (`is_name_implicit`, auto-named after the text - e.g. a topic literally named "/new"). This option must be OFF (see group 22)
  - [ ] Plain message in All during topic flow: ignore + send a **hint EVERY time** ("topic flow is on - /new to start a topic, /session to switch")
- [ ] Message lands in a topic that is **bound to no session** (residual/manual topic) during topic flow: **reject with a hint** - "this topic is bound to no session, use /session to bind a session"
- [ ] The "All" topic only accepts suitable commands, e.g.:
  - `/new` **in All** creates a new topic, then the topic title is set (very short, agent-generated, group 19) after the first user message sent in it
  - `/new` **inside an individual topic** causes the same behaviour (creates a new topic + first-message title), and the `/new` itself is **ignored in the history of the current session** (commands are never recorded)
- [ ] Session-scoped commands are unavailable in the All topic: `/context`, `/cost`, `/stop`, `/compact`, `/delete`, `/rename`, `/model` - reject with a short hint to use `/new` or open a topic
  - Includes the new `/context` and `/cost` commands (see group 21)

### 22. Topic flow - switchable mode (via /topic)

> Two per-chat modes. Normal = plain chat; Topic = sessions live in topics.
> Group 22 spec COMPLETE (2026-09-25) - no open questions remain.

**Normal flow (default):**
- [ ] Message like normal (no thread id)
- [ ] `/new` clears the current context and moves to a blank session
  - Old sessions are still saved in the DB so the user can recover them with `/session` (list + switch - same command as in topic flow)

**Entering topic flow - `/topic` command:**
- [ ] First ask the user to confirm the switch (typed reply) - explained: sessions are started afterwards with `/new`
  - The **active main-chat session is left intact, NOT cleared/deleted** - the main chat becomes the "All messages" view; the user can recover that session later via `/session` from All or from a topic
- [ ] Preconditions - **BOTH must hold**, else refuse with a hint:
  - Threaded mode ON (`has_topics_enabled` via getMe) - hint: enable Threaded mode in the @BotFather Mini App
  - **"Disallow users to create topics" toggle ENABLED** (`allows_users_to_create_topics == false` via getMe) - hint: enable that toggle (otherwise the client implicitly creates topics per message - group 20)
  - Topic flow only works when both Threaded mode and "Disallow users to create topics" are enabled (decision)
- [ ] Persist the mode flag **in config.toml** (write-through: update file + in-memory) so it survives restarts (decision)
- [ ] **No topic auto-created at entry** (decision) - just hint the user to run `/new` to create the first topic
- [ ] **No residual-topic check at entry** (decision) - liveness is only verified when a ping is called (see switching logic)
  - Constraints (verified live): the Bot API has **no "list topics" method**, and **topic deletions are NOT broadcast** to the bot (no `forum_topic_deleted` event); sending into a dead topic fails with `400 message thread not found` - the ping is the liveness probe
  - The `topics` table in sessions.db remains the registry of bot-created topics (persists across restarts); `forum_topic_created` events are registered as they stream past
- [ ] Each topic binds to a session in the database (sessions.db)

**The `/session` command (all flows):**
- [ ] `/session` (no args): list the **first 5 sessions** - each row shows **name, session id**, **timestamp** + **preview of the last user message**; **ordered by last-active (most recent first)**; if the last user message has no text, the preview is the placeholder **`[image]`**
- [ ] `/session page N` - show the Nth page (5 sessions per page)
- [ ] `/session N` - switch to the Nth session **on the current page** (remember the current page per chat so N resolves against it)
- [ ] `/session <id>` - switch immediately to that specific session
  - **Session id format: `yyyymmdd-hhmm-4hex`**, assigned when the session is created (unambiguous vs the bare-number page selector); stored in sessions.db
- [ ] The list **never includes the current active session**: not the session bound to the topic you're in, and not the main-chat session when running in normal flow; when `/session` is run in **All** (topic flow) no session is active there, so the full list is allowed

**Prompt mechanics (every typed-choice prompt in this flow):**
- [ ] Choices are made via **typed replies** (reuses the `/reset-all` pending-confirm machinery - no inline keyboards / callback_query)
- [ ] Running **any command** while a prompt is pending **cancels** that session's prompt (the command runs normally; prompts in other sessions are untouched)
- [ ] Prompt slots are **per session**: one per topic **plus one for the All view** - multiple prompts can be outstanding at the same time across different topics
- [ ] A prompt **waits forever** (no timeout); a new prompt in the same session replaces that session's pending one
- [ ] Sending a plain new message while a prompt is pending in the same session = the "do nothing" choice + the message is handled normally (per switching logic / delivery rules)

**Switching logic:**
- [ ] **Normal flow**: switch immediately to the selected session
- [ ] **Topic flow, session has no bound topic:**
  - `/session` triggered **in All**: immediately create a topic, rename it to the session name, continue from there
  - `/session` triggered **inside a topic**: ask the user **3 options** - **1. switch here** (bind session to this topic) / **2. create new topic** (as above) / **3. do nothing**
    - **Any new message in this same topic while the prompt is open counts as "do nothing"**: the prompt is cancelled and the agent answers the new message normally
- [ ] **Topic flow, session has a bound topic**:
  - `/session` triggered **in All**: ask the user **2 options** - **1. ping** the bound topic (locate it) / **2. do nothing**
  - `/session` triggered **inside a topic**: ask the user **3 options** - **1. switch here** (rebind the session to this topic) / **2. ping** / **3. do nothing** (new message = do nothing, as above)
  - **If ping fails** (`400 message thread not found` - topic was deleted): in both cases immediately **unbind the dead topic id and create a new topic for that session** (automatic recovery; ping doubles as the liveness probe)
  - **"switch here" unbinds the session's previous topic** (old topic stays, just unbound)
- [ ] **Leaving topic flow (`/topic` off)**: keep all binding ids - topics are NOT deleted when leaving topic flow
- [ ] `/unbound`: **dropped by decision** - unbinding now happens implicitly (switching elsewhere / dead-topic recovery)
- [ ] `/rename` inside topic flow renames **both the session and the topic** (they mirror each other)
- [ ] `/model` is **session-scoped** (sets the model of the current session); **`/model global <provider/model-id>`** (e.g. `/model global go/glm-5.3-flash`) sets the **default model in config.toml** (persisted by writing the file at runtime; implementation note: surgical line edit to keep config comments)
- [ ] Delivery rules of group 20 apply (AI answers only inside topics; All accepts suitable commands only, incl. `/new` and `/session`)

**Command availability in All (decision):**
- [ ] **Rejected in All** (session-scoped): `/context`, `/cost`, `/compact`, `/delete`, `/rename`, `/model` (except `/model global`), **`/stop`** - `/stop must be run inside a topic** (no global interrupt)
- [ ] **Allowed in All**: `/new`, `/session`, `/topic`, `/help`, `/start`, `/models`, `/thinking`, `/test_md`, `/test_rich`, `/reset-all` (chat-wide)

**Leaving topic flow - running `/topic` again:**
- [ ] User must confirm switching back to the normal flow (typed reply, same mechanics)
- [ ] Persist the mode flag back to config.toml (as at entry)
- [ ] After the switch: all bot messages are sent **without** a thread ID; **EVERY message that lands inside a topic is ignored - plain messages AND commands alike - and answered with a hint EVERY time** (topics are fully inert while flow is off; session recovery then works via `/session` from All only - `/session` from a topic applies while topic flow is ON)

## P5 - Custom OpenAI-Compatible Providers (last)

**Implementation points:**
1. [ ] Custom provider config + `/models` integration (group 23), using the group 25 metadata pipeline for stats where available
2. [ ] Interactive `setup.py` wizard (group 24)

### 23. Custom providers
- [ ] Support custom OpenAI-compatible providers via config (base URL + API key)
- [ ] List available models per provider (`/models` should include them)
- [ ] Auto-retrieve model stats where the provider supports it (max context window, costs, ...)
- [ ] Per-provider/per-model overrides in config (e.g. manual context window, cost values for providers that don't report stats)

### 24. Interactive setup script
- [ ] `python setup.py` wizard that:
  - Prompts for bot token (validate live via `getMe` before saving)
  - Prompts for provider API keys (validate by fetching `/models`)
  - Asks for allowed users (IDs/usernames) or allow-all
  - Writes `config.toml` in the project dir
- [ ] Optional keep-alive service install, per user choice at the end:
  - Detect systemd; if present, offer to install the unit with correct User/paths, then `daemon-reload` + `enable --now`
  - Skip gracefully (with a printed hint) when systemd is unavailable
- [ ] Rerunnable: detect existing config and offer to edit values instead of overwriting
