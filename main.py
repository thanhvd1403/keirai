"""Keirai entry point: Telegram long-polling loop, commands, AI chat.

Stdlib only. Run: python main.py  (config from ./config.toml or KEIRAI_CONFIG)
"""
import base64
import html
import logging
import logging.handlers
import mimetypes
import os
import secrets
import string
import sys
import time

import config as config_mod
import md2tg
import providers
import telegram as tg_mod

log = logging.getLogger("keirai")

HISTORY_LIMIT = 24  # messages per chat kept in memory
TEXT_EXTS = {
    ".txt", ".md", ".py", ".js", ".ts", ".json", ".yaml", ".yml", ".toml",
    ".ini", ".cfg", ".sh", ".bat", ".ps1", ".c", ".h", ".cpp", ".hpp", ".rs",
    ".go", ".java", ".rb", ".xml", ".html", ".css", ".sql", ".log", ".csv",
}
IMAGE_MIMES = ("image/jpeg", "image/png", "image/gif", "image/webp")


class State:
    """Session state: in-memory + optional SQLite write-through persistence."""

    def __init__(self, thinking_default, store=None):
        self._default = thinking_default
        self.store = store
        self.topics_enabled = False  # bot has topic mode in private chats (getMe)
        self.session_count = {}  # chat_id -> int, for auto session names
        self.topics = {}         # chat_id -> [thread_id] of topics we created
        self.pending_reset = {}  # chat_id -> (s1, s2) confirmation challenge
        self.thinking = {}   # user_id -> bool
        self.model = {}      # (chat_id, thread) -> "provider/model"
        self.history = {}    # (chat_id, thread) -> [messages]
        if store:
            self._load()

    def _load(self):
        for chat_id, thread_id, model, messages, _updated in self.store.load_sessions():
            key = (chat_id, None if thread_id == 0 else thread_id)
            self.history[key] = messages
            if model:
                self.model[key] = model
        for chat_id, thread_id, name in self.store.load_topics():
            self.topics.setdefault(chat_id, []).append(thread_id)
        for chat_id, tids in self.topics.items():
            self.session_count[chat_id] = len(tids) + 1

    def persist(self, chat_id, thread_id):
        """Write current session (history + model) through to the store."""
        if not self.store:
            return
        key = self.chat_key(chat_id, thread_id)
        self.store.save_session(chat_id, thread_id or 0, self.model.get(key),
                                self.history.get(key) or [])

    def thinking_on(self, user_id):
        return self.thinking.get(user_id, self._default)

    def set_thinking(self, user_id, on):
        self.thinking[user_id] = bool(on)

    def chat_key(self, chat_id, thread_id):
        return (chat_id, thread_id)

    def model_for(self, chat_id, thread_id, default_model):
        return self.model.get(self.chat_key(chat_id, thread_id), default_model)

    def get_history(self, chat_id, thread_id):
        return self.history.setdefault(self.chat_key(chat_id, thread_id), [])

    def next_session_num(self, chat_id):
        n = self.session_count.get(chat_id, 0) + 1
        self.session_count[chat_id] = n
        return n


# ---------------------------------------------------------------- commands
# Command registry: name -> (handler, help_text, hidden)
# handler signature: fn(cfg, tg, msg, thread_id, state)

COMMANDS = {}


def command(name, help_text, hidden=False):
    def deco(fn):
        COMMANDS[name] = (fn, help_text, hidden)
        return fn
    return deco


def build_help():
    lines = ["<b>Keirai</b> - lightweight AI agent", "", "<b>Commands</b>"]
    for name in sorted(COMMANDS):
        fn, desc, hidden = COMMANDS[name]
        if not hidden:
            lines.append("/%s - %s" % (name, desc))
    lines += [
        "",
        "Each topic = one session with its own context. Enable topics for the bot "
        "via @BotFather to use sessions in this private chat.",
        "",
        "Send a photo to talk about it. Send media with caption <code>media_test</code> "
        "to test the media round-trip.",
    ]
    return "\n".join(lines)


@command("start", "this message")
@command("help", "this message")
def cmd_start(cfg, tg, msg, thread_id, state):
    tg.send(msg["chat"]["id"], build_help(), thread_id)

TEST_MD_BODY = """**bold** *italic* ~~strike~~ ||spoiler|| `inline code`
# Header
- list item
[link](https://opencode.ai) and bare https://example.com?a=1&b=2

```python
def hi():
    print("<hello & bye>")
```
> a blockquote
snake_case and file_name.txt stay literal"""


@command("test_md", "run markdown through the regular rendering pipeline (arg optional)")
def cmd_test_md(cfg, tg, msg, thread_id, state):
    src = _arg(msg) or TEST_MD_BODY
    try:
        for part in md2tg.split(src):
            tg.send(msg["chat"]["id"], part, thread_id)
    except Exception as e:
        _err(tg, msg, thread_id, "test_md failed: %s" % e)


TEST_RICH_BODY = """# Rich message test

Native **GFM table**:

| Provider | Model | Context |
|:---------|:-----:|--------:|
| zen | glm-5.3-flash | 1,000,000 |
| go | kimi-k3 | 1,048,576 |

- [ ] task list item
- [x] completed item
- nested
  - sub item

Inline `code`, ==marked text==, ~~strike~~, ||spoiler|| and $$E = mc^2$$"""


@command("test_rich", "test rich message rendering (tables, task lists, formulas)")
def cmd_test_rich(cfg, tg, msg, thread_id, state):
    src = _arg(msg) or TEST_RICH_BODY
    try:
        for chunk in md2tg.split_rich(src):
            tg.send_rich(msg["chat"]["id"], markdown=chunk, thread_id=thread_id)
    except Exception as e:
        _err(tg, msg, thread_id, "test_rich failed: %s" % e)


@command("thinking", "show/hide AI reasoning: /thinking on|off")
def cmd_thinking(cfg, tg, msg, thread_id, state):
    arg = _arg(msg).strip().lower()
    user_id = msg["from"]["id"]
    if arg in ("on", "off"):
        state.set_thinking(user_id, arg == "on")
    tg.send(msg["chat"]["id"], "thinking is <b>%s</b>" % ("on" if state.thinking_on(user_id) else "off"), thread_id)


@command("models", "list models from configured providers (with context/cost stats)")
def cmd_models(cfg, tg, msg, thread_id, state):
    lines = []
    for name in ("zen", "go"):
        key = cfg.provider_key(name)
        if not key:
            lines.append("<b>%s</b>: no API key configured" % name)
            continue
        try:
            models, from_cache = providers.cached_models(name, key, config_mod.cache_dir(cfg))
            lines.append("<b>%s</b> (%s, %d models)" % (name, "cached" if from_cache else "fetched", len(models)))
            lines.append(_format_models(models))
        except Exception as e:
            lines.append("<b>%s</b>: error: %s" % (name, _esc(str(e))))
    text = "\n".join(lines) or "no providers configured"
    for part in md2tg.split(text):
        tg.send(msg["chat"]["id"], part, thread_id)


@command("model", "switch model: /model provider/model-id, e.g. /model go/glm-5.3-flash")
def cmd_model(cfg, tg, msg, thread_id, state):
    arg = _arg(msg).strip()
    chat_id = msg["chat"]["id"]
    if not arg:
        current = state.model_for(chat_id, thread_id, cfg.default_model)
        tg.send(chat_id, "current model: <code>%s</code>\nusage: /model &lt;provider/model&gt;" % _esc(current), thread_id)
        return
    if "/" not in arg or arg.split("/", 1)[0] not in providers.PROVIDER_BASES:
        tg.send(chat_id, "model must look like <code>zen/&lt;model-id&gt;</code> or <code>go/&lt;model-id&gt;</code>", thread_id)
        return
    state.model[state.chat_key(chat_id, thread_id)] = arg
    state.persist(chat_id, thread_id)
    tg.send(chat_id, "model set to <code>%s</code>" % _esc(arg), thread_id)


@command("new", "start a new session (new topic when topics are on): /new [name]")
def cmd_new(cfg, tg, msg, thread_id, state):
    """Start a new session: a fresh topic when topics are available,
    otherwise reset the current (implicit) session."""
    chat_id = msg["chat"]["id"]
    name = _arg(msg).strip() or "Session %d" % state.next_session_num(chat_id)
    if not state.topics_enabled:
        state.history.pop(state.chat_key(chat_id, thread_id), None)
        state.model.pop(state.chat_key(chat_id, thread_id), None)
        tg.send(chat_id, "topics are not enabled for this bot - "
                         "session cleared here instead. Enable topics via "
                         "@BotFather to get one topic per session.", thread_id)
        return
    try:
        topic = tg.create_topic(chat_id, name)
    except Exception as e:
        _err(tg, msg, thread_id, "could not create topic: %s" % e)
        return
    tid = topic["message_thread_id"]
    state.topics.setdefault(chat_id, []).append(tid)
    if state.store:
        state.store.add_topic(chat_id, tid, name)
    log.info("session created: chat=%s topic=%s name=%r", chat_id, tid, name)
    tg.send(chat_id, "new session <b>%s</b> started - type here" % _esc(name), tid)


@command("reset-all", "wipe every session and delete the bot's topics", hidden=True)
def cmd_reset_all(cfg, tg, msg, thread_id, state):
    """Wipe everything for this chat: all sessions, their context, and the
    Telegram topics we created. Hidden from /help. Requires typing two
    random confirmation strings."""
    chat_id = msg["chat"]["id"]
    args = _arg(msg).split()
    pending = state.pending_reset.get(chat_id)
    if pending and len(args) >= 2 and args[0] == pending[0] and args[1] == pending[1]:
        deleted, failed = 0, 0
        for tid in state.topics.get(chat_id, []):
            try:
                tg.delete_topic(chat_id, tid)
                deleted += 1
            except Exception as e:
                failed += 1
                log.warning("topic delete failed: chat=%s topic=%s: %s", chat_id, tid, e)
        for key in [k for k in state.history if k[0] == chat_id]:
            del state.history[key]
        for key in [k for k in state.model if k[0] == chat_id]:
            del state.model[key]
        state.session_count.pop(chat_id, None)
        state.topics.pop(chat_id, None)
        state.pending_reset.pop(chat_id, None)
        if state.store:
            state.store.delete_chat(chat_id)
        log.info("reset-all: chat=%s topics deleted=%d failed=%d", chat_id, deleted, failed)
        tg.send(chat_id, "reset done - <b>%d</b> topic(s) deleted, all sessions cleared." % deleted, thread_id)
        return
    s1, s2 = _confirmation_pair(), _confirmation_pair()
    state.pending_reset[chat_id] = (s1, s2)
    log.info("reset-all initiated: chat=%s", chat_id)
    tg.send(chat_id,
            "<b>careful:</b> this deletes ALL sessions, their context and their "
            "Telegram topics. This cannot be undone.\n\n"
            "To confirm, send exactly:\n"
            "<code>/reset-all %s %s</code>" % (s1, s2), thread_id)


def _confirmation_pair():
    alphabet = string.ascii_uppercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(4))


@command("rename", "rename the current session/topic: /rename <name>")
def cmd_rename(cfg, tg, msg, thread_id, state):
    """Rename the current topic/session."""
    chat_id = msg["chat"]["id"]
    name = _arg(msg).strip()
    if not name:
        tg.send(chat_id, "usage: /rename &lt;name&gt;", thread_id)
        return
    if thread_id is None:
        tg.send(chat_id, "open (or create with /new) a topic first - there is "
                         "nothing to rename in the main chat", thread_id)
        return
    try:
        tg.edit_topic(chat_id, thread_id, name)
    except Exception as e:
        _err(tg, msg, thread_id, "could not rename topic: %s" % e)
        return
    if state.store:
        state.store.set_topic_name(chat_id, thread_id, name)
    tg.send(chat_id, "session renamed to <b>%s</b>" % _esc(name), thread_id)


@command("delete", "delete this session's context (topic stays; delete it manually)")
def cmd_delete(cfg, tg, msg, thread_id, state):
    """Clear the conversation context of the current session. The Telegram
    topic is NOT deleted - only the memory of the conversation."""
    chat_id = msg["chat"]["id"]
    key = state.chat_key(chat_id, thread_id)
    had = bool(state.history.get(key))
    state.history.pop(key, None)
    state.model.pop(key, None)
    if state.store:
        state.store.delete_session(chat_id, thread_id or 0)
    log.info("session deleted: chat=%s thread=%s (had_history=%s)", chat_id, thread_id, had)
    tg.send(chat_id, "session deleted - conversation context cleared."
            "\nthe topic itself stays; you can delete it manually in Telegram.",
            thread_id)


@command("compact", "summarize older context now to free space (also runs automatically)")
def cmd_compact(cfg, tg, msg, thread_id, state):
    chat_id = msg["chat"]["id"]
    history = state.get_history(chat_id, thread_id)
    if len(history) < 5:
        tg.send(chat_id, "nothing to compact yet (%d messages)" % len(history), thread_id)
        return
    model_full = state.model_for(chat_id, thread_id, cfg.default_model)
    provider, api_key, model_id = _resolve_provider(cfg, model_full)
    if not api_key:
        _err(tg, msg, thread_id, "no API key for provider '%s'" % provider)
        return
    tg.typing(chat_id, thread_id)
    before_n, before_chars = len(history), _est_chars(history)
    try:
        history[:] = _compact(cfg, provider, api_key, model_id, history,
                              "compact-%s-%s" % (chat_id, thread_id or "main"))
    except Exception as e:
        _err(tg, msg, thread_id, "compaction failed: %s" % e)
        return
    state.persist(chat_id, thread_id)
    log.info("manual compact: chat=%s thread=%s %d->%d msgs", chat_id, thread_id,
             before_n, len(history))
    tg.send(chat_id, "compacted: %d -> %d messages (%d -> %d chars)"
            % (before_n, len(history), before_chars, _est_chars(history)), thread_id)


def _format_models(models):
    out = []
    for m in models[:30]:
        stats = []
        if m["context"]:
            stats.append("ctx %s" % _human(m["context"]))
        if m["cost_in"] is not None and m["cost_out"] is not None:
            stats.append("$%s/$%s per 1M" % (_fmt_cost(m["cost_in"]), _fmt_cost(m["cost_out"])))
        out.append("- <code>%s</code>%s" % (_esc(m["id"]), " (%s)" % ", ".join(stats) if stats else ""))
    if len(models) > 30:
        out.append("... and %d more" % (len(models) - 30))
    return "\n".join(out)


# ---------------------------------------------------------------- AI chat

def _resolve_provider(cfg, model_full):
    """(provider_name, api_key, model_id) - falls back to the other provider
    (prefer go) when the configured one has no key; keys are endpoint-bound."""
    provider_name, model_id = model_full.split("/", 1)
    api_key = cfg.provider_key(provider_name)
    if not api_key:
        alt = "go" if provider_name != "go" else "zen"
        alt_key = cfg.provider_key(alt)
        if alt_key:
            log.info("no API key for provider '%s' - routing model '%s' via '%s'",
                     provider_name, model_id, alt)
            return alt, alt_key, model_id
    return provider_name, api_key, model_id


def _est_chars(history):
    n = 0
    for m in history:
        c = m.get("content")
        if isinstance(c, str):
            n += len(c)
        elif isinstance(c, list):
            for part in c:
                if isinstance(part, dict) and part.get("type") == "text":
                    n += len(part.get("text", ""))
    return n


def _compact(cfg, provider, api_key, model_id, history, session_id, keep=4):
    """Summarize the older part of history, keep the last `keep` messages.
    Returns the new message list."""
    tail = list(history[-keep:]) if len(history) > keep else list(history)
    older = history[:-keep] if len(history) > keep else []
    if not older:
        return history
    transcript = []
    for m in older:
        role = m.get("role", "?")
        c = m.get("content")
        if isinstance(c, list):
            c = " ".join(p.get("text", "") for p in c if isinstance(p, dict)
                         and p.get("type") == "text")
        transcript.append("%s: %s" % (role, (c or "")[:4000]))
    prompt = ("Summarize this earlier conversation concisely in markdown "
              "(facts, decisions, open tasks, user preferences), under 400 words:\n\n"
              + "\n".join(transcript))
    summary, _ = providers.chat(provider, api_key, model_id,
                                [{"role": "user", "content": prompt}],
                                session_id=session_id)
    marker = [
        {"role": "user", "content": "[Summary of earlier conversation]\n" + summary},
        {"role": "assistant", "content": "Understood. Continuing with that context."},
    ]
    return marker + tail


def _stream_answer(tg, cfg, msg, thread_id, provider, api_key, model_id,
                   messages, session_id):
    """Stream the completion, showing live drafts in private chats.
    Returns (answer, reasoning). Raises on provider failure."""
    draft_id = secrets.randbelow(10**9) + 1
    answer, reasoning = "", ""
    show_reasoning = True  # until first content token arrives
    drafts_on = True
    last_flush = 0.0
    for kind, delta in providers.chat_stream(provider, api_key, model_id, messages,
                                             session_id=session_id):
        if kind == "reasoning" and show_reasoning:
            reasoning += delta
        else:
            show_reasoning = False
            answer += delta
        now = time.time()
        if drafts_on and now - last_flush > 1.5:
            last_flush = now
            try:
                if show_reasoning and reasoning:
                    tg.send_rich_draft(
                        msg["chat"]["id"], draft_id,
                        html="<tg-thinking>%s</tg-thinking>" % _esc(reasoning[-2000:]),
                        thread_id=thread_id)
                elif answer and len(answer) <= md2tg.RICH_LIMIT:
                    tg.send_rich_draft(msg["chat"]["id"], draft_id,
                                       markdown=answer, thread_id=thread_id)
            except tg_mod.TelegramError as e:
                log.warning("draft updates disabled: %s", e)
                drafts_on = False
    if not answer.strip():
        raise providers.ProviderError("empty answer from stream")
    return answer, reasoning


def ai_reply(cfg, tg, msg, thread_id, state, text, images=None):
    model_full = state.model_for(msg["chat"]["id"], thread_id, cfg.default_model)
    provider_name, api_key, model_id = _resolve_provider(cfg, model_full)
    if not api_key:
        _err(tg, msg, thread_id, "no API key for provider '%s' (or fallback 'go'/'zen')"
             % provider_name)
        return
    session_id = "keirai-%s-%s" % (msg["chat"]["id"], thread_id or "main")

    content = [{"type": "text", "text": text or "Describe the image."}]
    for mime, b64 in images or []:
        content.append(providers.image_part(mime, b64))
    user_msg = {"role": "user", "content": content if images else text}

    history = state.get_history(msg["chat"]["id"], thread_id)
    history.append(user_msg)

    # auto-compact before the context limit is hit
    if _est_chars(history) > cfg.context_limit_chars and len(history) > 6:
        before = len(history)
        try:
            history[:] = _compact(cfg, provider_name, api_key, model_id, history,
                                  session_id)
            log.info("auto-compact: chat=%s thread=%s %d -> %d msgs",
                     msg["chat"]["id"], thread_id, before, len(history))
        except Exception as e:
            log.warning("auto-compact failed (%s) - trimming oldest instead", e)
            del history[:-6]

    messages = ([{"role": "system", "content": cfg.system_prompt}] if cfg.system_prompt else []) + history[-HISTORY_LIMIT:]

    reasoning = None
    answer = None
    can_stream = (cfg.rich_messages and cfg.stream_drafts
                  and msg["chat"].get("type") == "private")
    if can_stream:
        try:
            answer, reasoning = _stream_answer(tg, cfg, msg, thread_id,
                                               provider_name, api_key, model_id,
                                               messages, session_id)
        except Exception as e:
            log.warning("streaming failed (%s) - falling back to blocking call", e)
    if answer is None:
        tg.typing(msg["chat"]["id"], thread_id)
        try:
            answer, reasoning = providers.chat(provider_name, api_key, model_id,
                                               messages, session_id=session_id)
        except Exception as e:
            history.pop()  # don't keep failed turns
            state.persist(msg["chat"]["id"], thread_id)
            _err(tg, msg, thread_id, "%s error: %s" % (provider_name, e))
            return

    history.append({"role": "assistant", "content": answer})
    del history[:-HISTORY_LIMIT]
    state.persist(msg["chat"]["id"], thread_id)

    show = state.thinking_on(msg["from"]["id"])
    reasoning_md = reasoning if (show and reasoning and reasoning.strip()) else None
    if cfg.rich_messages:
        try:
            _send_rich_answer(tg, msg["chat"]["id"], thread_id, answer, reasoning_md)
            return
        except tg_mod.TelegramError as e:
            log.warning("rich send failed, falling back to regular messages: %s", e)
    parts = md2tg.send_parts(answer, reasoning_md)
    for part in parts:
        tg.send(msg["chat"]["id"], part, thread_id)


def _send_rich_answer(tg, chat_id, thread_id, answer_md, thinking_md):
    """Bot API 10.1 rich messages: answer as GFM markdown (native tables,
    task lists, headings, 32k chars), thinking as a collapsible <details>."""
    if thinking_md:
        for chunk in md2tg.split(thinking_md, md2tg.RICH_LIMIT):
            tg.send_rich(chat_id, html="<details><summary>\U0001f914 Thinking</summary>%s</details>" % chunk,
                         thread_id=thread_id)
    for chunk in md2tg.split_rich(answer_md):
        tg.send_rich(chat_id, markdown=chunk, thread_id=thread_id)


# ---------------------------------------------------------------- media

def handle_media(cfg, tg, msg, thread_id, state):
    user_id = msg["from"]["id"]
    caption = msg.get("caption") or ""
    if caption.strip().lstrip("/").lower() == "media_test":
        _media_echo(cfg, tg, msg, thread_id)
        return

    limit = cfg.max_file_mb * 1024 * 1024
    images = []
    doc_note = None

    if msg.get("photo"):
        tg_file = msg["photo"][-1]  # largest size
    elif msg.get("document"):
        doc = msg["document"]
        tg_file = doc
        mime = doc.get("mime_type", "")
        if mime.startswith("text/") or os.path.splitext(doc.get("file_name", ""))[1].lower() in TEXT_EXTS:
            pass  # handled below
        elif mime not in IMAGE_MIMES:
            name = doc.get("file_name", "file")
            size = doc.get("file_size", 0)
            tg.send(msg["chat"]["id"], "received <code>%s</code> (%s) - this type is not processed yet" % (_esc(name), _human(size)), thread_id)
            return
    elif msg.get("video"):
        tg_file = msg["video"]
    elif msg.get("voice"):
        tg_file = msg["voice"]
    elif msg.get("audio"):
        tg_file = msg["audio"]
    else:
        return

    size = tg_file.get("file_size") or 0
    if size > limit:
        tg.send(msg["chat"]["id"], "file too large (%s, limit %d MB)" % (_human(size), cfg.max_file_mb), thread_id)
        return
    try:
        f = tg.get_file(tg_file["file_id"])
        path = tg.download(f)
    except Exception as e:
        _err(tg, msg, thread_id, "download failed: %s" % e)
        return

    mime = tg_file.get("mime_type") or mimetypes.guess_type(path)[0] or "application/octet-stream"
    try:
        if mime in IMAGE_MIMES:
            with open(path, "rb") as fh:
                images.append((mime, base64.b64encode(fh.read()).decode("ascii")))
        elif mime.startswith("text/") or os.path.splitext(path)[1].lower() in TEXT_EXTS or mime == "application/json":
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                data = fh.read(200_000)
            doc_note = "attached file %s:\n```\n%s\n```" % (tg_file.get("file_name", path), data)
        else:
            doc_note = "user sent a %s file" % mime
    finally:
        try:
            os.remove(path)
        except OSError:
            pass

    if not images and not doc_note:
        return
    prompt = caption or ""
    if doc_note:
        prompt = (prompt + "\n\n" if prompt else "") + doc_note
    ai_reply(cfg, tg, msg, thread_id, state, prompt or None, images or None)


def _media_echo(cfg, tg, msg, thread_id):
    """Send the user's own media back - tests the upload path."""
    try:
        if msg.get("photo"):
            f = tg.get_file(msg["photo"][-1]["file_id"])
            path = tg.download(f)
            tg.send_media(msg["chat"]["id"], "photo", path, "round-trip test", thread_id)
            os.remove(path)
            return
        kind = "document" if msg.get("document") else ("video" if msg.get("video") else ("voice" if msg.get("voice") else ("audio" if msg.get("audio") else None)))
        if not kind:
            tg.send(msg["chat"]["id"], "send media with caption media_test", thread_id)
            return
        f = tg.get_file(msg[kind]["file_id"])
        path = tg.download(f)
        name = msg[kind].get("file_name") or os.path.basename(path)
        tmp = os.path.join(os.path.dirname(path), name) if kind == "document" else path
        if tmp != path:
            os.rename(path, tmp)
        tg.send_media(msg["chat"]["id"], kind, tmp, "round-trip test", thread_id)
        if tmp != path:
            os.remove(tmp)
        else:
            os.remove(path)
    except Exception as e:
        _err(tg, msg, thread_id, "media echo failed: %s" % e)


# ---------------------------------------------------------------- routing

def handle_message(cfg, tg, msg, state):
    chat_id = msg["chat"]["id"]
    thread_id = msg.get("message_thread_id")
    user = msg.get("from") or {}
    if not cfg.is_allowed(user.get("id"), user.get("username")):
        log.info("ignored message from user %s (@%s) - not in allowed_users",
                 user.get("id"), user.get("username"))
        return  # silently ignore non-allowed users
    log.info("message from user %s (@%s) chat=%s thread=%s",
             user.get("id"), user.get("username"), chat_id, thread_id)

    text = msg.get("text") or msg.get("caption") or ""
    if text.startswith("/"):
        cmd, _, rest = text[1:].partition(" ")
        msg["_arg"] = rest
        entry = COMMANDS.get(cmd.split("@", 1)[0])  # tolerate /cmd@botname
        if entry is None:
            tg.send(chat_id, "unknown command, try /help", thread_id)
        else:
            entry[0](cfg, tg, msg, thread_id, state)
        return

    if msg.get("photo") or msg.get("document") or msg.get("video") or msg.get("voice") or msg.get("audio"):
        handle_media(cfg, tg, msg, thread_id, state)
        return

    if text.strip():
        ai_reply(cfg, tg, msg, thread_id, state, text)


# ---------------------------------------------------------------- main

def setup_logging(base_dir=None):
    """Log to stderr AND to <base>/logs/keirai.log (rotating 2MB x 3)."""
    base = base_dir or os.path.dirname(os.path.abspath(__file__))
    logdir = os.path.join(base, "logs")
    os.makedirs(logdir, exist_ok=True)
    root = logging.getLogger("keirai")
    if root.handlers:
        return root
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    fh = logging.handlers.RotatingFileHandler(
        os.path.join(logdir, "keirai.log"), maxBytes=2_000_000, backupCount=2,
        encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    root.addHandler(fh)
    root.addHandler(sh)
    return root


def main():
    setup_logging()
    try:
        cfg = config_mod.load()
        cfg.validate()
    except config_mod.ConfigError as e:
        log.error("config error: %s", e)
        log.error("copy config.example.toml to config.toml and set bot_token")
        sys.exit(1)

    import sessions
    store = sessions.Store(config_mod.sessions_path(cfg))
    tg = tg_mod.Telegram(cfg.bot_token)
    state = State(cfg.thinking_default, store=store)
    try:
        me = tg.get_me()
        state.topics_enabled = bool(me.get("has_topics_enabled"))
        log.info("bot @%s ready (private-chat topics: %s)",
                 me.get("username", "?"), "on" if state.topics_enabled else "off - enable via @BotFather")
    except tg_mod.TelegramError as e:
        log.error("getMe failed: %s (continuing without topic support)", e)
    try:
        tg.call("setMyCommands", {"commands": [
            {"command": name, "description": desc}
            for name, (fn, desc, hidden) in sorted(COMMANDS.items()) if not hidden
        ]})
    except tg_mod.TelegramError as e:
        log.warning("setMyCommands failed: %s", e)
    log.info("polling... (default model: %s)", cfg.default_model)
    offset = None
    backoff = 1
    while True:
        try:
            updates = tg.get_updates(offset)
            backoff = 1
        except tg_mod.TelegramError as e:
            log.error("poll error: %s", e)
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
            continue
        for upd in updates:
            offset = upd["update_id"] + 1
            msg = upd.get("message")
            if not msg:
                continue
            try:
                handle_message(cfg, tg, msg, state)
            except Exception as e:
                log.exception("handler error")
                try:
                    tg.send(msg["chat"]["id"], "error: %s" % _esc(str(e)), msg.get("message_thread_id"))
                except Exception:
                    pass


# ---------------------------------------------------------------- helpers

def _arg(msg):
    return msg.get("_arg", "")


def _esc(s):
    return html.escape(s, quote=False)


def _human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return "%d%s" % (n, unit) if unit == "B" else "%.1f%s" % (n, unit)
        n /= 1024.0


def _fmt_cost(x):
    if x == 0:
        return "0"
    return ("%g" % x)


def _err(tg, msg, thread_id, text):
    log.error(text)
    try:
        tg.send(msg["chat"]["id"], "error: %s" % _esc(text), thread_id)
    except Exception:
        pass


if __name__ == "__main__":
    main()
