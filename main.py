"""Keirai entry point: Telegram long-polling loop, commands, AI chat.

Stdlib only. Run: python main.py  (config from ./config.toml or KEIRAI_CONFIG)
"""
import base64
import html
import json
import logging
import logging.handlers
import mimetypes
import os
import queue
import secrets
import string
import sys
import threading
import time

import config as config_mod
import md2tg
import providers
import sessions
import telegram as tg_mod
import tools as tools_mod

log = logging.getLogger("keirai")

HISTORY_LIMIT = 24  # messages per chat kept in memory
SESSION_ID = "keirai"  # anonymized x-opencode-session for the whole app (no chat ids)
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
        self.meta = {}       # (chat_id, thread) -> {session_id, name, cost, tokens}
        self.topic_names = {}  # (chat_id, thread_id) -> topic name
        self._turn_lock = threading.Lock()
        self.active_turns = {}   # chat_key -> True while a reply/tool turn runs
        self.interrupts = set()  # chat_keys the user asked to /stop
        if store:
            self._load()

    # ------------------------------------------------ /stop bookkeeping
    def begin_turn(self, key):
        with self._turn_lock:
            self.active_turns[key] = True
            self.interrupts.discard(key)

    def end_turn(self, key):
        with self._turn_lock:
            self.active_turns.pop(key, None)
            self.interrupts.discard(key)

    def request_interrupt(self, key):
        """Flag a running turn to stop. True if one was running for `key`."""
        with self._turn_lock:
            if key in self.active_turns:
                self.interrupts.add(key)
                return True
            return False

    def interrupted(self, key):
        with self._turn_lock:
            return key in self.interrupts

    def _load(self):
        for chat_id, thread_id, model, messages, _updated in self.store.load_sessions():
            key = (chat_id, None if thread_id == 0 else thread_id)
            self.history[key] = messages
            if model:
                self.model[key] = model
        for (chat_id, thread_id), m in self.store.load_meta().items():
            if not m.get("session_id"):
                m["session_id"] = sessions.new_session_id()
            self.meta[(chat_id, None if thread_id == 0 else thread_id)] = m
        for key in list(self.history):
            if key not in self.meta:  # sessions predating meta (or meta-less)
                self.ensure_meta(key[0], key[1])
        for chat_id, thread_id, name in self.store.load_topics():
            self.topics.setdefault(chat_id, []).append(thread_id)
            self.topic_names[(chat_id, thread_id)] = name or ""
        for chat_id, tids in self.topics.items():
            self.session_count[chat_id] = len(tids) + 1

    def persist(self, chat_id, thread_id):
        """Write current session (history + model) through to the store."""
        if not self.store:
            return
        key = self.chat_key(chat_id, thread_id)
        self.store.save_session(chat_id, thread_id or 0, self.model.get(key),
                                self.history.get(key) or [])
        if not (self.history.get(key) or []):
            # history wiped -> the session (and its accumulators) is gone
            self.meta.pop(key, None)

    def thinking_on(self, user_id):
        return self.thinking.get(user_id, self._default)

    def set_thinking(self, user_id, on):
        self.thinking[user_id] = bool(on)

    def chat_key(self, chat_id, thread_id):
        return (chat_id, thread_id)

    def model_for(self, chat_id, thread_id, default_model):
        return self.model.get(self.chat_key(chat_id, thread_id), default_model)

    def ensure_meta(self, chat_id, thread_id):
        """{session_id, name, cost_usd, tokens_*} for this session (created lazily)."""
        key = self.chat_key(chat_id, thread_id)
        m = self.meta.get(key)
        if m is None:
            if self.store:
                m = self.store.ensure_meta(chat_id, thread_id or 0)
                if not m.get("session_id"):
                    m["session_id"] = sessions.new_session_id()
            else:
                m = {"session_id": sessions.new_session_id(), "name": None,
                     "cost_usd": 0.0, "tokens_in": 0, "tokens_out": 0,
                     "tokens_cached_read": 0, "tokens_cached_write": 0}
            self.meta[key] = m
        return m

    def add_usage(self, chat_id, thread_id, cost, tokens):
        """Accumulate one provider call's notional cost + token counts."""
        m = self.ensure_meta(chat_id, thread_id)
        m["cost_usd"] = (m.get("cost_usd") or 0.0) + float(cost)
        for k in ("tokens_in", "tokens_out", "tokens_cached_read",
                  "tokens_cached_write"):
            m[k] = (m.get(k) or 0) + int(tokens.get(k) or 0)
        if self.store:
            self.store.record_usage(chat_id, thread_id or 0, cost, tokens)

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
        "Each topic = one session with its own context. Enable Topics by turning "
        "on Threaded mode for the bot in the @BotFather Mini App "
        "(t.me/BotFather?startapp).",
        "",
        "Send a photo to talk about it. Send media with caption <code>media_test</code> "
        "to test the media round-trip.",
        "",
        "The agent can use tools: files, shell, web search/fetch - and a headless "
        "browser once <code>deploy/install_lightpanda.sh</code> has been run.",
        "While a reply or tool is running, /stop interrupts it.",
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

Inline `code`, ==marked text==, ~~strike~~, ||spoiler|| and $$E = mc^2$$

Media block (rich path renders it natively):
![Keirai](https://raw.githubusercontent.com/thanhvd1403/keirai/main/LICENSE)"""


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
    try:  # context/pricing stats come from models.dev (the /models API has none)
        meta_all, _ = providers.models_metadata(config_mod.cache_dir(cfg))
    except Exception:
        meta_all = {}
    for name in ("zen", "go"):
        key = cfg.provider_key(name)
        if not key:
            lines.append("<b>%s</b>: no API key configured" % name)
            continue
        try:
            models, from_cache = providers.cached_models(name, key, config_mod.cache_dir(cfg))
            mdata = meta_all.get(name) or {}
            for m in models:
                extra = mdata.get(m["id"])
                if extra:
                    for field in ("context", "max_output", "cost_in", "cost_out"):
                        if m.get(field) is None and extra.get(field) is not None:
                            m[field] = extra[field]
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
        key = state.chat_key(chat_id, thread_id)
        state.history.pop(key, None)
        state.model.pop(key, None)
        state.meta.pop(key, None)  # fresh session -> fresh session id
        tg.send(chat_id, "topics are not enabled for this bot - "
                         "session cleared here instead. Turn on Threaded mode "
                         "for the bot in the @BotFather Mini App to get one "
                         "topic per session.", thread_id)
        return
    try:
        topic = tg.create_topic(chat_id, name)
    except Exception as e:
        _err(tg, msg, thread_id, "could not create topic: %s" % e)
        return
    tid = topic["message_thread_id"]
    state.topics.setdefault(chat_id, []).append(tid)
    state.topic_names[(chat_id, tid)] = name
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
        for key in [k for k in state.meta if k[0] == chat_id]:
            del state.meta[key]
        for key in [k for k in state.topic_names if k[0] == chat_id]:
            del state.topic_names[key]
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
    state.topic_names[(chat_id, thread_id)] = name
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
    state.meta.pop(key, None)  # cost/token accumulators die with the session
    if state.store:
        state.store.delete_session(chat_id, thread_id or 0)
    log.info("session deleted: chat=%s thread=%s (had_history=%s)", chat_id, thread_id, had)
    tg.send(chat_id, "session deleted - conversation context cleared."
            "\nthe topic itself stays; you can delete it manually in Telegram.",
            thread_id)


@command("stop", "interrupt the reply/tool currently running in this chat")
def cmd_stop(cfg, tg, msg, thread_id, state):
    """Reaches the main loop only when no turn is running - the watcher
    consumes /stop during an active turn and interrupts it directly."""
    tg.send(msg["chat"]["id"], "nothing is running right now", thread_id)


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
    cusage = {}
    try:
        history[:] = _compact(cfg, provider, api_key, model_id, history,
                              SESSION_ID, usage_out=cusage)
    except Exception as e:
        _err(tg, msg, thread_id, "compaction failed: %s" % e)
        return
    _record_usage(cfg, state, chat_id, thread_id, cusage, provider, model_id)
    state.persist(chat_id, thread_id)
    log.info("manual compact: chat=%s thread=%s %d->%d msgs", chat_id, thread_id,
             before_n, len(history))
    tg.send(chat_id, "compacted: %d -> %d messages (%d -> %d chars)"
            % (before_n, len(history), before_chars, _est_chars(history)), thread_id)


@command("context", "context window usage of this session (name/id/model, tokens)")
def cmd_context(cfg, tg, msg, thread_id, state):
    chat_id = msg["chat"]["id"]
    tg.send(chat_id, _session_report(cfg, state, chat_id, thread_id, False),
            thread_id)


@command("cost", "cost of this session: tokens in/out/cached + total")
def cmd_cost(cfg, tg, msg, thread_id, state):
    chat_id = msg["chat"]["id"]
    tg.send(chat_id, _session_report(cfg, state, chat_id, thread_id, True),
            thread_id)


def _session_report(cfg, state, chat_id, thread_id, with_cost):
    """Shared body of /context and /cost (group 21)."""
    key = state.chat_key(chat_id, thread_id)
    history = state.history.get(key) or []
    chars = _est_chars(history)
    limit = cfg.context_limit_chars
    meta = state.ensure_meta(chat_id, thread_id)
    model = state.model_for(chat_id, thread_id, cfg.default_model)
    name = state.topic_names.get(key) or meta.get("name") or "session"
    pct = int(round(chars * 100.0 / limit)) if limit else 0
    would_compact = chars > limit and len(history) > 6
    lines = [
        "<b>session</b>: %s · <code>%s</code>"
        % (_esc(str(name)), _esc(str(meta.get("session_id") or "?"))),
        "<b>model</b>: <code>%s</code>" % _esc(model),
        "<b>context</b>: %s / %s chars (%d%%) · %d messages"
        % ("{:,}".format(chars), "{:,}".format(limit), pct, len(history)),
        "auto-compact: <b>%s</b>" % (
            "would trigger now" if would_compact
            else "not yet (triggers over %s chars)" % "{:,}".format(limit)),
        "<b>tokens</b>: in %s · out %s · cached read %s · cached write %s"
        % tuple("{:,}".format(int(meta.get(k) or 0)) for k in
                ("tokens_in", "tokens_out", "tokens_cached_read",
                 "tokens_cached_write")),
    ]
    if with_cost:
        lines.append("<b>cost</b>: $%.6f" % (meta.get("cost_usd") or 0.0))
    return "\n".join(lines)


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


def _alternate_provider(cfg, provider_name, model_id):
    """(other_provider, key) when the OTHER provider has a key AND its catalog
    serves the same model id; None otherwise (never blind-retry a model)."""
    alt = "go" if provider_name != "go" else "zen"
    key = cfg.provider_key(alt)
    if not key:
        return None
    try:
        models, _ = providers.cached_models(alt, key, config_mod.cache_dir(cfg))
    except Exception:
        return None  # unknown catalog -> no fallback (spec: iff it serves it)
    return (alt, key) if any(m.get("id") == model_id for m in models) else None


def _record_usage(cfg, state, chat_id, thread_id, usage, provider, model_id):
    """Accumulate one call's tokens + notional cost into the session (P3)."""
    if not usage:
        return
    try:
        price = providers.price_for(config_mod.cache_dir(cfg), provider, model_id)
        cost = providers.usage_cost(price, usage)
    except Exception:
        cost = 0.0
    details = usage.get("prompt_tokens_details") or {}
    tin = int(usage.get("prompt_tokens") or 0)
    cached = int(details.get("cached_tokens") or 0)
    state.add_usage(chat_id, thread_id, cost, {
        "tokens_in": tin,
        "tokens_out": int(usage.get("completion_tokens") or 0),
        "tokens_cached_read": max(0, min(cached, tin)),
        "tokens_cached_write": 0,  # never reported by the endpoints
    })


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


def _compact(cfg, provider, api_key, model_id, history, session_id, keep=4,
             usage_out=None):
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
    summary, _reasoning, _tcs = providers.chat(provider, api_key, model_id,
                                               [{"role": "user", "content": prompt}],
                                               session_id=session_id,
                                               usage_out=usage_out)
    marker = [
        {"role": "user", "content": "[Summary of earlier conversation]\n" + summary},
        {"role": "assistant", "content": "Understood. Continuing with that context."},
    ]
    return marker + tail


def _flush_live(tg, live, chat_id, thread_id, reasoning, answer, show_reasoning):
    """Push current partial state to Telegram: drafts in private chats,
    send/edit of a placeholder message elsewhere (live edits)."""
    if show_reasoning and reasoning:
        if live["mode"] == "draft":
            tg.send_rich_draft(
                chat_id, live["draft_id"],
                html="<tg-thinking>%s</tg-thinking>" % _esc(reasoning[-2000:]),
                thread_id=thread_id)
        else:
            # tg-thinking is draft-only -> collapsible <details> placeholder
            src = ("<details><summary>\U0001f914 Thinking</summary>%s</details>"
                   % _esc(reasoning[-2000:]))
            if live.get("msg_id"):
                tg.edit_rich(chat_id, live["msg_id"], html=src)
            else:
                live["msg_id"] = tg.send_rich(chat_id, html=src,
                                              thread_id=thread_id)["message_id"]
    elif answer and len(answer) <= md2tg.RICH_LIMIT:
        if live["mode"] == "draft":
            tg.send_rich_draft(chat_id, live["draft_id"], markdown=answer,
                               thread_id=thread_id)
        elif live.get("msg_id"):
            tg.edit_rich(chat_id, live["msg_id"], markdown=answer)
        else:
            live["msg_id"] = tg.send_rich(chat_id, markdown=answer,
                                          thread_id=thread_id)["message_id"]


def _stream_answer(tg, msg, live, stop, provider, api_key, model_id, messages,
                   session_id, tools=None, thread_id=None, usage_out=None):
    """Stream one provider round with live progress.
    Returns (answer, reasoning, tool_calls). Raises on provider failure.
    `stop()` is checked every delta so /stop cuts generation short."""
    chat_id = msg["chat"]["id"]
    answer, reasoning = "", ""
    show_reasoning = True  # until first content token arrives
    last_flush = 0.0
    for kind, delta in providers.chat_stream(provider, api_key, model_id, messages,
                                             session_id=session_id, tools=tools,
                                             usage_out=usage_out):
        if kind == "tool_calls":
            return answer, reasoning, delta
        if kind == "reasoning" and show_reasoning:
            reasoning += delta
        else:
            show_reasoning = False
            answer += delta
        if stop and stop():
            log.info("stream cut short by /stop")
            break
        now = time.time()
        if live.get("on") and now - last_flush > 1.5:
            last_flush = now
            try:
                _flush_live(tg, live, chat_id, thread_id, reasoning, answer,
                            show_reasoning)
            except tg_mod.TelegramError as e:
                log.warning("live updates disabled: %s", e)
                live["on"] = False
    return answer, reasoning, None


INTERRUPT_NOTE = ("[This run was interrupted by the user before it finished - "
                  "any running command/tool did not complete.]")


def ai_reply(cfg, tg, msg, thread_id, state, text, images=None):
    key = state.chat_key(msg["chat"]["id"], thread_id)
    state.begin_turn(key)
    try:
        _ai_reply(cfg, tg, msg, thread_id, state, text, images)
    finally:
        state.end_turn(key)


def _ai_reply(cfg, tg, msg, thread_id, state, text, images=None):
    chat_id = msg["chat"]["id"]
    key = state.chat_key(chat_id, thread_id)

    def stop():
        return state.interrupted(key)

    model_full = state.model_for(chat_id, thread_id, cfg.default_model)
    provider_name, api_key, model_id = _resolve_provider(cfg, model_full)
    if not api_key:
        _err(tg, msg, thread_id, "no API key for provider '%s' (or fallback 'go'/'zen')"
             % provider_name)
        return
    session_id = SESSION_ID  # anonymized app-wide constant (no chat ids)
    switched = {"on": False}

    def pcall(messages, tools=None):
        """Provider call with Go-first error fallback (group 25): on
        ProviderError, retry once via the other provider when it has a key
        AND its catalog serves this model id. Usage is recorded on success."""
        nonlocal provider_name, api_key
        while True:
            usage = {}
            try:
                result = providers.chat(provider_name, api_key, model_id,
                                        messages, session_id=session_id,
                                        tools=tools, usage_out=usage)
            except providers.ProviderError as e:
                alt = (None if switched["on"]
                       else _alternate_provider(cfg, provider_name, model_id))
                if alt is None:
                    raise
                switched["on"] = True
                log.warning("provider '%s' failed (%s) - retrying via '%s'",
                            provider_name, e, alt[0])
                provider_name, api_key = alt
                continue
            _record_usage(cfg, state, chat_id, thread_id, usage,
                          provider_name, model_id)
            return result

    content = [{"type": "text", "text": text or "Describe the image."}]
    for mime, b64 in images or []:
        content.append(providers.image_part(mime, b64))
    user_msg = {"role": "user", "content": content if images else text}

    history = state.get_history(chat_id, thread_id)
    history.append(user_msg)

    # auto-compact before the context limit is hit
    if not stop() and _est_chars(history) > cfg.context_limit_chars and len(history) > 6:
        before = len(history)
        cusage = {}
        try:
            history[:] = _compact(cfg, provider_name, api_key, model_id, history,
                                  session_id, usage_out=cusage)
            _record_usage(cfg, state, chat_id, thread_id, cusage,
                          provider_name, model_id)
            log.info("auto-compact: chat=%s thread=%s %d -> %d msgs",
                     chat_id, thread_id, before, len(history))
        except Exception as e:
            log.warning("auto-compact failed (%s) - trimming oldest instead", e)
            del history[:-6]

    messages = ([{"role": "system", "content": cfg.system_prompt}] if cfg.system_prompt else []) + history[-HISTORY_LIMIT:]
    tool_specs = tools_mod.available_specs(cfg)
    ctx = {"session_id": session_id, "interrupt": stop}
    live = None
    if cfg.rich_messages and cfg.stream_drafts:
        live = {"mode": "draft" if msg["chat"].get("type") == "private" else "edit",
                "draft_id": secrets.randbelow(10**9) + 1, "msg_id": None, "on": True}

    reasoning, answer = None, None
    interrupted = False
    for rnd in range(tools_mod.MAX_ROUNDS + 1):
        if stop():
            interrupted = True
            break
        tcs, got = None, False
        if live:
            susage = {}
            try:
                answer, reasoning, tcs = _stream_answer(
                    tg, msg, live, stop, provider_name, api_key, model_id,
                    messages, session_id, tools=tool_specs or None,
                    thread_id=thread_id, usage_out=susage)
                got = True
            except Exception as e:
                log.warning("streaming failed (%s) - falling back to blocking call", e)
            _record_usage(cfg, state, chat_id, thread_id, susage,
                          provider_name, model_id)
        if not got and not stop():
            tg.typing(chat_id, thread_id)
            try:
                answer, reasoning, tcs = pcall(messages, tools=tool_specs or None)
            except Exception as e:
                history.pop()  # don't keep failed turns
                state.persist(chat_id, thread_id)
                _err(tg, msg, thread_id, "%s error: %s" % (provider_name, e))
                return
        if stop():
            interrupted = True
            break
        if not tcs or rnd >= tools_mod.MAX_ROUNDS:
            break
        # run the tools the model asked for
        messages.append({"role": "assistant", "content": answer or "",
                         "tool_calls": [
                             {"id": t["id"], "type": "function",
                              "function": {"name": t["name"],
                                           "arguments": json.dumps(t["arguments"])}}
                             for t in tcs]})
        for t in tcs:
            if stop():
                interrupted = True
                break
            try:
                tg.send(chat_id, "\U0001f527 <code>%s</code> %s"
                        % (_esc(t["name"]),
                           _esc(tools_mod.preview(t["name"], t["arguments"]))),
                        thread_id)
            except tg_mod.TelegramError:
                pass
            result = tools_mod.execute(cfg, t["name"], t["arguments"], ctx)
            messages.append({"role": "tool", "tool_call_id": t["id"],
                             "content": result})
            log.info("tool %s -> %d chars", t["name"], len(result))
            if stop():
                interrupted = True
                break
        if interrupted:
            break
        answer = None  # next round must produce the final text

    if interrupted or stop():
        _deliver_interrupted(tg, state, msg, thread_id, answer, live)
        return

    if not (answer or "").strip():
        # round cap hit (or empty answer): final call, tools off
        tg.typing(chat_id, thread_id)
        try:
            answer, reasoning, _ = pcall(messages)
        except Exception as e:
            history.pop()
            state.persist(chat_id, thread_id)
            _err(tg, msg, thread_id, "%s error: %s" % (provider_name, e))
            return

    history.append({"role": "assistant", "content": answer})
    del history[:-HISTORY_LIMIT]
    state.persist(chat_id, thread_id)

    show = state.thinking_on(msg["from"]["id"])
    reasoning_md = reasoning if (show and reasoning and reasoning.strip()) else None
    if cfg.rich_messages:
        try:
            _send_rich_answer(tg, chat_id, thread_id, answer, reasoning_md, live=live)
            return
        except tg_mod.TelegramError as e:
            log.warning("rich send failed, falling back to regular messages: %s", e)
    parts = md2tg.send_parts(answer, reasoning_md)
    for part in parts:
        tg.send(chat_id, part, thread_id)


def _deliver_interrupted(tg, state, msg, thread_id, partial, live):
    """Record the /stop interruption in context and tell the user."""
    chat_id = msg["chat"]["id"]
    history = state.get_history(chat_id, thread_id)
    body = (((partial or "").strip() + "\n\n") if (partial or "").strip() else "")
    history.append({"role": "assistant", "content": body + INTERRUPT_NOTE})
    del history[:-HISTORY_LIMIT]
    state.persist(chat_id, thread_id)
    if live and live.get("msg_id"):  # drop the half-finished live message
        try:
            tg.delete_message(chat_id, live["msg_id"])
        except Exception:
            pass
    log.info("turn interrupted by user: chat=%s thread=%s", chat_id, thread_id)
    try:
        tg.send(chat_id, "\u23f9 stopped - the running reply/tool was interrupted.",
                thread_id)
    except tg_mod.TelegramError:
        pass


def _send_rich_answer(tg, chat_id, thread_id, answer_md, thinking_md, live=None):
    """Bot API 10.1 rich messages: thinking as a collapsible <details> first,
    then the answer as GFM markdown chunks (32k each). When a live edit-mode
    placeholder exists, the first chunk edits it in instead of sending new."""
    items = []  # (kind, src), kind: "html" | "markdown"
    if thinking_md:
        for chunk in md2tg.split(thinking_md, md2tg.RICH_LIMIT):
            items.append(("html",
                          "<details><summary>\U0001f914 Thinking</summary>%s</details>"
                          % chunk))
    for chunk in md2tg.split_rich(answer_md):
        items.append(("markdown", chunk))
    if live and live.get("msg_id"):
        kind, src = items[0]
        try:
            if kind == "html":
                tg.edit_rich(chat_id, live["msg_id"], html=src)
            else:
                tg.edit_rich(chat_id, live["msg_id"], markdown=src)
            items = items[1:]
        except tg_mod.TelegramError as e:
            log.warning("final live edit failed, sending fresh: %s", e)
            try:
                tg.delete_message(chat_id, live["msg_id"])
            except Exception:
                pass
    for kind, src in items:
        if kind == "html":
            tg.send_rich(chat_id, html=src, thread_id=thread_id)
        else:
            tg.send_rich(chat_id, markdown=src, thread_id=thread_id)


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
        reply_ctx = _reply_context(msg)
        if reply_ctx:
            text = reply_ctx + "\n\n" + text
        ai_reply(cfg, tg, msg, thread_id, state, text)


def _reply_context(msg):
    """Annotation for replies/quotes so the model sees the referenced text
    (item 17). The reply object carries the full referenced message."""
    reply = msg.get("reply_to_message")
    if not reply:
        return ""
    sender = reply.get("from") or {}
    who = sender.get("username") or sender.get("first_name") or "someone"
    body = reply.get("text") or reply.get("caption") or ""
    quote = msg.get("quote")  # user quoted a part of the message
    if quote is not None:
        q = (quote.get("text") or "") if isinstance(quote, dict) else str(quote)
        return "user is quoting this part of a message (from @%s):\n%s" % (
            who, (q[:1200] or "(empty quote)"))
    if not body:
        body = ("(the referenced message has no extractable text - "
                "it may be media or a rich message)")
    return "user is replying to this message (from @%s):\n%s" % (who, body[:1500])


# ------------------------------------------------------- /stop + update watcher
# The bot is single-threaded: while a reply/tool turn runs, no one polls
# getUpdates. A watcher thread owns polling forever, feeds updates to a queue
# the main loop drains, and intercepts /stop so an in-flight turn can be
# interrupted via a thread-safe flag (everything else is queued, never lost).

def _msg_key(msg):
    return (msg["chat"]["id"], msg.get("message_thread_id"))


def _is_stop_cmd(msg):
    text = (msg.get("text") or "").strip()
    if not text.startswith("/stop"):
        return False
    rest = text[len("/stop"):]
    return rest == "" or rest[0] in " @"  # /stop, /stop@botname


def _route_update(state, out_q, upd):
    """Watcher decision: consume /stop for a running turn, else queue.
    Returns True when the update was consumed (not queued)."""
    msg = upd.get("message")
    if msg and _is_stop_cmd(msg):
        if state.request_interrupt(_msg_key(msg)):
            log.info("stop requested: chat=%s thread=%s",
                     msg["chat"]["id"], msg.get("message_thread_id"))
            return True
    out_q.put(upd)
    return False


def _watch_updates(tg, state, out_q):
    """Long-poll getUpdates forever. Sole owner of the offset."""
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
            _route_update(state, out_q, upd)


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

    store = sessions.Store(config_mod.sessions_path(cfg))
    tg = tg_mod.Telegram(cfg.bot_token)
    state = State(cfg.thinking_default, store=store)
    try:
        me = tg.get_me()
        state.topics_enabled = bool(me.get("has_topics_enabled"))
        log.info("bot @%s ready (private-chat topics: %s)",
                 me.get("username", "?"),
                 "on" if state.topics_enabled else
                 "off - enable Threaded mode in the @BotFather Mini App")
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
    out_q = queue.Queue()
    threading.Thread(target=_watch_updates, args=(tg, state, out_q),
                     daemon=True, name="updates").start()
    while True:
        upd = out_q.get()
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
