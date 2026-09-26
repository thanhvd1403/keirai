import json
import os
import queue
import tempfile
import unittest
from unittest import mock

import main
import providers


class FakeTG:
    def __init__(self):
        self.sent = []
        self.rich = []       # (chat_id, {"markdown"|"html": src}, thread_id)
        self.drafts = []     # (chat_id, draft_id, src, thread_id)
        self.edits = []      # (chat_id, message_id, src)
        self.deleted = []    # (chat_id, message_id)
        self.media = []
        self.file_bytes = b"fakeimage"
        self.topics = {}     # name -> thread_id
        self.renamed = []    # (chat_id, thread_id, name)
        self.deleted_topics = []  # (chat_id, thread_id)
        self._next_tid = 500
        self.me = {"has_topics_enabled": True,
                   "allows_users_to_create_topics": False}
        self.fail_threads = set()  # thread ids whose send raises (dead topic)

    def get_me(self):
        return dict(self.me)

    def send(self, chat_id, text, thread_id=None):
        if thread_id in self.fail_threads:
            raise Exception("Bad Request: message thread not found")
        self.sent.append((chat_id, text, thread_id))
        return {"message_id": len(self.sent)}

    def send_rich(self, chat_id, markdown=None, html=None, thread_id=None):
        src = {"markdown": markdown} if markdown is not None else {"html": html}
        self.rich.append((chat_id, src, thread_id))
        return {"message_id": 1000 + len(self.rich)}

    def send_rich_draft(self, chat_id, draft_id, markdown=None, html=None, thread_id=None):
        src = {"markdown": markdown} if markdown is not None else {"html": html}
        self.drafts.append((chat_id, draft_id, src, thread_id))
        return True

    def edit_rich(self, chat_id, message_id, markdown=None, html=None):
        src = {"markdown": markdown} if markdown is not None else {"html": html}
        self.edits.append((chat_id, message_id, src))
        return True

    def delete_message(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id))
        return True

    def typing(self, chat_id, thread_id=None):
        pass

    def get_file(self, file_id):
        return {"file_path": "photos/x.jpg", "file_size": 100}

    def download(self, tg_file, max_bytes=None):
        path = os.path.join(tempfile.gettempdir(), "keirai_test_img.jpg")
        with open(path, "wb") as f:
            f.write(self.file_bytes)
        return path

    def send_media(self, chat_id, kind, path, caption=None, thread_id=None):
        self.media.append((chat_id, kind, caption))

    def create_topic(self, chat_id, name):
        self._next_tid += 1
        self.topics[name] = self._next_tid
        return {"message_thread_id": self._next_tid, "name": name}

    def edit_topic(self, chat_id, thread_id, name):
        self.renamed.append((chat_id, thread_id, name))

    def delete_topic(self, chat_id, thread_id):
        self.deleted_topics.append((chat_id, thread_id))


def make_config(**kw):
    import config as config_mod
    base = {"bot_token": "t", "allowed_users": [42],
            "rich_messages": True, "stream_drafts": False,
            # explicit -> default tests never hit models.dev for thresholds
            "context_limit_chars": 120_000,
            "providers": {"zen": {"api_key": "zk"}, "go": {"api_key": "gk"}}}
    base.update(kw)
    return config_mod.Config(base)


def msg(text=None, user_id=42, **extra):
    m = {"chat": {"id": 100, "type": "private"}, "from": {"id": user_id, "username": "tester"},
         "text": text or "", "message_thread_id": None}
    m.update(extra)
    return m


def disable_titles(test):
    """Group-19 naming issues one extra chat call per new session; neutralize
    it so tests see exactly one call per turn (and never hit the network)."""
    p = mock.patch.object(main, "_maybe_name_session", lambda *a, **k: None)
    p.start()
    test.addCleanup(p.stop)


class TestRouter(unittest.TestCase):
    def setUp(self):
        self.tg = FakeTG()
        self.state = main.State(True)
        self.cfg = make_config()
        disable_titles(self)

    def test_denied_user_ignored(self):
        main.handle_message(self.cfg, self.tg, msg(user_id=999, text="hi"), self.state)
        self.assertEqual(self.tg.sent, [])

    def test_allowed_user_help(self):
        main.handle_message(self.cfg, self.tg, msg(text="/help"), self.state)
        self.assertEqual(len(self.tg.sent), 1)
        self.assertIn("Keirai", self.tg.sent[0][1])

    def test_thinking_toggle(self):
        main.handle_message(self.cfg, self.tg, msg(text="/thinking off"), self.state)
        self.assertFalse(self.state.thinking_on(42))
        self.assertIn("off", self.tg.sent[-1][1])
        main.handle_message(self.cfg, self.tg, msg(text="/thinking on"), self.state)
        self.assertTrue(self.state.thinking_on(42))

    def test_unknown_command(self):
        main.handle_message(self.cfg, self.tg, msg(text="/nope"), self.state)
        self.assertIn("unknown command", self.tg.sent[0][1])

    def test_test_md_sends_html(self):
        main.handle_message(self.cfg, self.tg, msg(text="/test_md **hi**"), self.state)
        self.assertEqual(self.tg.sent[0][1], "hi".replace("hi", "<b>hi</b>"))

    @mock.patch.object(providers, "chat", return_value=("answer **x**", "deep thought", None))
    def test_ai_flow_rich_with_thinking(self, _):
        m = msg(text="hello agent")
        main.handle_message(self.cfg, self.tg, m, self.state)
        self.assertEqual(len(self.tg.rich), 2)
        # thinking first, as collapsible details with converted HTML inside
        chat, src, _tid = self.tg.rich[0]
        self.assertIn("html", src)
        self.assertIn("<details><summary>\U0001f914 Thinking</summary>", src["html"])
        self.assertIn("deep thought", src["html"])
        # answer as raw markdown (server parses GFM)
        chat, src, _tid = self.tg.rich[1]
        self.assertEqual(src, {"markdown": "answer **x**"})
        self.assertEqual(self.tg.sent, [])  # no regular messages

    @mock.patch.object(providers, "chat", return_value=("answer", "thoughts", None))
    def test_ai_flow_rich_thinking_off(self, _):
        main.handle_message(self.cfg, self.tg, msg(text="/thinking off"), self.state)
        self.tg.rich.clear()
        main.handle_message(self.cfg, self.tg, msg(text="hello"), self.state)
        self.assertEqual(len(self.tg.rich), 1)
        self.assertEqual(self.tg.rich[0][1], {"markdown": "answer"})

    @mock.patch.object(providers, "chat", return_value=("answer", "", None))
    def test_ai_flow_rich_fallback_on_error(self, _):
        main.handle_message(self.cfg, self.tg, msg(text="/thinking off"), self.state)
        self.tg.rich.clear()
        self.tg.sent.clear()
        with mock.patch.object(FakeTG, "send_rich", side_effect=main.tg_mod.TelegramError("no rich support")):
            main.handle_message(self.cfg, self.tg, msg(text="hello"), self.state)
        self.assertEqual(len(self.tg.sent), 1)  # fell back to regular send
        self.assertEqual(self.tg.sent[0][1], "answer")

    @mock.patch.object(providers, "chat", return_value=("answer", "", None))
    def test_ai_flow_rich_disabled_in_config(self, _):
        self.cfg = make_config(rich_messages=False)
        main.handle_message(self.cfg, self.tg, msg(text="/thinking off"), self.state)
        self.tg.rich.clear()
        self.tg.sent.clear()
        main.handle_message(self.cfg, self.tg, msg(text="hello"), self.state)
        self.assertEqual(self.tg.rich, [])
        self.assertEqual(len(self.tg.sent), 1)

    def test_test_rich_command(self):
        main.handle_message(self.cfg, self.tg, msg(text="/test_rich"), self.state)
        self.assertEqual(len(self.tg.rich), 1)
        self.assertIn("| Provider | Model |", self.tg.rich[0][1]["markdown"])

    @mock.patch.object(providers, "chat", return_value=("answer", "", None))
    def test_key_fallback_prefers_go(self, chat_mock):
        self.cfg = make_config(providers={"zen": {"api_key": ""}, "go": {"api_key": "gk"}})
        main.handle_message(self.cfg, self.tg, msg(text="/thinking off"), self.state)
        self.tg.rich.clear()
        self.tg.sent.clear()
        main.handle_message(self.cfg, self.tg, msg(text="hello"), self.state)
        # default model is zen/... but zen has no key -> routed via go
        self.assertEqual(chat_mock.call_args[0][0], "go")

    @mock.patch.object(providers, "chat", return_value=("answer", "", None))
    def test_no_keys_at_all_errors(self, chat_mock):
        self.cfg = make_config(providers={"zen": {"api_key": ""}, "go": {"api_key": ""}})
        main.handle_message(self.cfg, self.tg, msg(text="hello"), self.state)
        chat_mock.assert_not_called()
        self.assertTrue(any("no API key" in t for _, t, _ in self.tg.sent))

    @mock.patch.object(providers, "chat", return_value=("nice pic", "", None))
    def test_photo_goes_to_vision(self, chat_mock):
        m = msg(text=None, photo=[{"file_id": "f1", "file_size": 100}])
        main.handle_message(self.cfg, self.tg, m, self.state)
        sent = chat_mock.call_args[0][3]  # messages arg
        self.assertEqual(sent[-1]["content"][1]["type"], "image_url")
        self.assertTrue(sent[-1]["content"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,"))

    @mock.patch.object(providers, "chat", return_value=("ok", "", None))
    def test_media_test_echo(self, _):
        m = msg(text=None, caption="media_test", photo=[{"file_id": "f1", "file_size": 100}])
        main.handle_message(self.cfg, self.tg, m, self.state)
        self.assertEqual(self.tg.media, [(100, "photo", "round-trip test")])

    @mock.patch.object(providers, "chat", return_value=("ok", "", None))
    def test_history_is_kept(self, chat_mock):
        main.handle_message(self.cfg, self.tg, msg(text="one"), self.state)
        main.handle_message(self.cfg, self.tg, msg(text="two"), self.state)
        hist = self.state.get_history(100, None)
        self.assertEqual([m2["role"] for m2 in hist], ["user", "assistant", "user", "assistant"])
        self.assertEqual(hist[-2]["content"], "two")  # last user msg
        self.assertEqual(hist[-1]["content"], "ok")   # last assistant msg


class TestSessions(unittest.TestCase):
    """P4: sessions live in topics while topic flow is ON (groups 19/20/22)."""

    def setUp(self):
        self.tg = FakeTG()
        self.state = main.State(True)
        self.cfg = make_config()
        self.cfg.topic_flow = True
        disable_titles(self)

    @mock.patch.object(providers, "chat", return_value=("a1", "", None))
    def test_new_creates_topic_and_context_isolated(self, chat_mock):
        main.handle_message(self.cfg, self.tg, msg(text="/new work stuff"), self.state)
        # topic created, welcome sent INTO it, session bound eagerly
        tid = self.tg.topics["work stuff"]
        self.assertEqual(self.tg.sent[-1],
                         (100, "new session started in a topic - type here", tid))
        self.assertTrue(self.state.session_at(100, tid))
        # inside the topic -> own history, isolated from All
        main.handle_message(self.cfg, self.tg,
                            msg(text="hello", message_thread_id=tid), self.state)
        self.assertEqual([m["content"] for m in self.state.get_history(100, tid)],
                         ["hello", "a1"])
        # plain message in All -> hint, no answer, no history (group 20)
        main.handle_message(self.cfg, self.tg, msg(text="main chat"), self.state)
        self.assertEqual(self.state.history.get((100, None)), None)
        self.assertIn("topic flow is on", self.tg.sent[-1][1])

    @mock.patch.object(providers, "chat", return_value=("a1", "", None))
    def test_two_topics_isolated_all_hinted(self, chat_mock):
        main.handle_message(self.cfg, self.tg, msg(text="/new apples"), self.state)
        t1 = self.tg.topics["apples"]
        main.handle_message(self.cfg, self.tg, msg(text="/new bolts"), self.state)
        t2 = self.tg.topics["bolts"]
        for tid, text in ((t1, "about apples"), (t2, "about bolts")):
            main.handle_message(self.cfg, self.tg,
                                msg(text=text, message_thread_id=tid), self.state)
        main.handle_message(self.cfg, self.tg, msg(text="in all"), self.state)
        # disjoint histories; All has none
        self.assertEqual([m["content"] for m in self.state.get_history(100, t1)],
                         ["about apples", "a1"])
        self.assertEqual([m["content"] for m in self.state.get_history(100, t2)],
                         ["about bolts", "a1"])
        self.assertEqual(self.state.history.get((100, None)), None)
        self.assertEqual(chat_mock.call_count, 2)
        # every provider call saw ONLY its own session's messages
        for call, own in zip(chat_mock.call_args_list,
                             ("about apples", "about bolts")):
            user_texts = [m["content"] for m in call[0][3]
                          if m.get("role") == "user"]
            self.assertEqual(user_texts, [own])
        # one anonymized app-wide x-opencode-session; distinct session ids
        sids = {call[1]["session_id"] for call in chat_mock.call_args_list}
        self.assertEqual(sids, {main.SESSION_ID})
        self.assertNotEqual(self.state.meta[(100, t1)]["session_id"],
                            self.state.meta[(100, t2)]["session_id"])

    @mock.patch.object(providers, "chat")
    def test_manual_topic_rejected_until_bound(self, chat_mock):
        """Hand-made topics are unbound: rejected with a hint until bound
        (group 20) - and they never auto-create sessions anymore (group 9
        behavior superseded)."""
        main.handle_message(self.cfg, self.tg,
                            msg(text="hello from my own topic",
                                message_thread_id=4242), self.state)
        chat_mock.assert_not_called()
        self.assertFalse(self.state.session_at(100, 4242))
        self.assertIn("bound to no session", self.tg.sent[-1][1])
        # session-scoped commands rejected there too...
        main.handle_message(self.cfg, self.tg,
                            msg(text="/context", message_thread_id=4242), self.state)
        self.assertIn("bound to no session", self.tg.sent[-1][1])
        # ...but /session (the binder) runs
        main.handle_message(self.cfg, self.tg,
                            msg(text="/session", message_thread_id=4242), self.state)
        self.assertIn("no other sessions", self.tg.sent[-1][1])
        # not registered as a bot-created topic (/reset-all won't touch it)
        self.assertNotIn(4242, self.state.topics.get(100, []))

    @mock.patch.object(providers, "chat", return_value=("a1", "", None))
    def test_new_placeholder_until_title(self, chat_mock):
        # group 19: placeholder now, agent title after the first message
        main.handle_message(self.cfg, self.tg, msg(text="/new"), self.state)
        self.assertIn("New session", self.tg.topics)
        main.handle_message(self.cfg, self.tg, msg(text="/new named trip"), self.state)
        self.assertIn("named trip", self.tg.topics)

    @mock.patch.object(providers, "chat", return_value=("old answer", "", None))
    def test_new_in_normal_flow_parks_and_starts_blank(self, chat_mock):
        self.cfg.topic_flow = False
        main.handle_message(self.cfg, self.tg, msg(text="old conversation"),
                            self.state)
        old_sid = self.state.ensure_meta(100, None)["session_id"]
        main.handle_message(self.cfg, self.tg, msg(text="/new"), self.state)
        # blank session active in place
        self.assertEqual(self.state.get_history(100, None), [])
        self.assertNotEqual(self.state.meta[(100, None)]["session_id"], old_sid)
        # the old session is parked (kept) and recoverable via /session
        parked = [(k, m) for k, m in self.state.meta.items()
                  if m.get("session_id") == old_sid]
        self.assertEqual(len(parked), 1)
        self.assertIsNotNone(parked[0][0][1])  # no longer in the main slot
        self.assertEqual([m["content"] for m in
                          self.state.history[parked[0][0]]],
                         ["old conversation", "old answer"])
        self.assertIn("recover it with /session", self.tg.sent[-1][1])
        self.assertEqual(self.tg.topics, {})  # no topics in normal flow

    @mock.patch.object(providers, "chat", return_value=("a1", "", None))
    def test_new_registers_topic_for_reset(self, chat_mock):
        main.handle_message(self.cfg, self.tg, msg(text="/new a"), self.state)
        main.handle_message(self.cfg, self.tg, msg(text="/new b"), self.state)
        self.assertEqual(len(self.state.topics[100]), 2)

    # ------------------------------------------------ /rename (mirror, group 22)

    def test_rename_mirrors_topic_and_session(self):
        main.handle_message(self.cfg, self.tg, msg(text="/new work"), self.state)
        tid = self.tg.topics["work"]
        main.handle_message(self.cfg, self.tg,
                            msg(text="/rename project x", message_thread_id=tid),
                            self.state)
        self.assertEqual(self.tg.renamed, [(100, tid, "project x")])
        self.assertEqual(self.state.meta[(100, tid)]["name"], "project x")
        self.assertIn("renamed", self.tg.sent[-1][1])

    def test_rename_in_main_chat_without_session_rejected(self):
        self.cfg.topic_flow = False  # in All (flow on) this is gate-rejected
        main.handle_message(self.cfg, self.tg, msg(text="/rename nope"), self.state)
        self.assertEqual(self.tg.renamed, [])
        self.assertIn("no active session", self.tg.sent[-1][1])

    @mock.patch.object(providers, "chat", return_value=("hi", "", None))
    def test_rename_session_in_main_chat(self, chat_mock):
        self.cfg.topic_flow = False
        main.handle_message(self.cfg, self.tg, msg(text="hello"), self.state)
        main.handle_message(self.cfg, self.tg, msg(text="/rename my trip"),
                            self.state)
        self.assertEqual(self.tg.renamed, [])  # no topic to rename
        self.assertEqual(self.state.meta[(100, None)]["name"], "my trip")
        self.assertIn("renamed", self.tg.sent[-1][1])

    def test_rename_requires_name(self):
        main.handle_message(self.cfg, self.tg, msg(text="/new work"), self.state)
        tid = self.tg.topics["work"]
        main.handle_message(self.cfg, self.tg,
                            msg(text="/rename", message_thread_id=tid), self.state)
        self.assertEqual(self.tg.renamed, [])
        self.assertIn("usage", self.tg.sent[-1][1])


class TestResetAll(unittest.TestCase):
    def setUp(self):
        self.tg = FakeTG()
        self.state = main.State(True)
        self.cfg = make_config()
        disable_titles(self)
        self.state.topics_enabled = True
        self.cfg.topic_flow = True  # /new creates topics in topic flow

    def _initiate(self):
        main.handle_message(self.cfg, self.tg, msg(text="/reset-all"), self.state)
        text = self.tg.sent[-1][1]
        import re
        m = re.search(r"/reset-all ([A-Z0-9]+) ([A-Z0-9]+)", text)
        self.assertIsNotNone(m, text)
        return [m.group(1), m.group(2)]

    def test_not_in_help(self):
        self.assertNotIn("reset-all", main.build_help())

    def test_initiate_then_confirm(self):
        main.handle_message(self.cfg, self.tg, msg(text="/new s1"), self.state)
        tid = self.tg.topics["s1"]
        self.state.get_history(100, tid).append({"role": "user", "content": "x"})
        self.state.model[(100, tid)] = "zen/m"

        challenge = self._initiate()
        self.assertEqual(self.tg.deleted_topics, [])
        main.handle_message(self.cfg, self.tg, msg(text="/reset-all %s %s" % tuple(challenge)), self.state)

        self.assertEqual(self.tg.deleted_topics, [(100, tid)])
        self.assertNotIn(100, self.state.topics)
        self.assertEqual(self.state.history.get((100, tid)), None)
        self.assertNotIn((100, tid), self.state.model)
        self.assertIn("reset done", self.tg.sent[-1][1])
        self.assertNotIn(100, self.state.pending_reset)

    def test_wrong_strings_rechallenge(self):
        challenge = self._initiate()
        main.handle_message(self.cfg, self.tg, msg(text="/reset-all XXXX YYYY"), self.state)
        self.assertEqual(self.tg.deleted_topics, [])
        import re
        text = self.tg.sent[-1][1]
        new_challenge = re.search(r"/reset-all ([A-Z0-9]+) ([A-Z0-9]+)", text).groups()
        self.assertNotEqual(list(new_challenge), challenge)

    def test_nothing_to_delete_still_resets(self):
        challenge = self._initiate()
        main.handle_message(self.cfg, self.tg, msg(text="/reset-all %s %s" % tuple(challenge)), self.state)
        self.assertEqual(self.tg.deleted_topics, [])
        self.assertIn("reset done", self.tg.sent[-1][1])


class TestDynamicHelp(unittest.TestCase):
    def test_help_lists_all_visible_commands(self):
        h = main.build_help()
        for name in ("start", "help", "new", "rename", "delete", "compact",
                     "thinking", "models", "model", "test_md", "test_rich",
                     "stop"):
            self.assertIn("/%s -" % name, h)
        self.assertNotIn("/reset-all", h)

    def test_only_reset_all_hidden(self):
        hidden = [n for n, (_f, _d, h) in main.COMMANDS.items() if h]
        self.assertEqual(hidden, ["reset-all"])

    def test_handlers_share_signature(self):
        import inspect
        for name, (fn, _d, _h) in main.COMMANDS.items():
            params = list(inspect.signature(fn).parameters)
            self.assertEqual(params, ["cfg", "tg", "msg", "thread_id", "state"], msg=name)


class TestDeleteCommand(unittest.TestCase):
    def setUp(self):
        self.tg = FakeTG()
        self.state = main.State(True)
        self.cfg = make_config()
        disable_titles(self)

    def test_delete_clears_context_keeps_topic(self):
        self.cfg.topic_flow = True
        self.state.create_session(100, 77, name="work")
        hist = self.state.get_history(100, 77)
        hist.append({"role": "user", "content": "x"})
        self.state.model[(100, 77)] = "go/m"
        main.handle_message(self.cfg, self.tg, msg(text="/delete", message_thread_id=77), self.state)
        self.assertEqual(self.state.history.get((100, 77)), None)
        self.assertNotIn((100, 77), self.state.model)
        self.assertIn("session deleted", self.tg.sent[-1][1])
        self.assertIn("delete it manually", self.tg.sent[-1][1])
        # topic untouched, but now unbound (no session lives there)
        self.assertEqual(self.state.topics, {})
        self.assertFalse(self.state.session_at(100, 77))

    def test_delete_without_store_ok(self):
        main.handle_message(self.cfg, self.tg, msg(text="/delete"), self.state)
        self.assertIn("session deleted", self.tg.sent[-1][1])


class TestCompact(unittest.TestCase):
    def setUp(self):
        self.tg = FakeTG()
        self.state = main.State(True)
        self.cfg = make_config()
        disable_titles(self)

    def _seed(self, n=10, thread=None):
        hist = self.state.get_history(100, thread)
        for i in range(n):
            hist.append({"role": "user" if i % 2 == 0 else "assistant",
                         "content": "message %d " % i + "z" * 100})
        return hist

    @mock.patch.object(providers, "chat", return_value=("THE SUMMARY", "", None))
    def test_manual_compact(self, chat_mock):
        self._seed(10)
        main.handle_message(self.cfg, self.tg, msg(text="/compact"), self.state)
        hist = self.state.get_history(100, None)
        self.assertEqual(len(hist), 6)  # 2 summary marker + last 4
        self.assertIn("[Summary of earlier conversation]", hist[0]["content"])
        self.assertIn("THE SUMMARY", hist[0]["content"])
        self.assertEqual(hist[-1]["content"][:9], "message 9")
        self.assertIn("compacted: 10 -> 6", self.tg.sent[-1][1])

    def test_compact_nothing_to_do(self):
        self._seed(3)
        main.handle_message(self.cfg, self.tg, msg(text="/compact"), self.state)
        self.assertIn("nothing to compact", self.tg.sent[-1][1])

    @mock.patch.object(providers, "chat",
                       side_effect=[("SUMMARY", "", None), ("final answer", "", None)])
    def test_auto_compact_on_limit(self, chat_mock):
        self.cfg = make_config(context_limit_chars=100)
        hist = self._seed(8)
        main.handle_message(self.cfg, self.tg, msg(text="continue"), self.state)
        hist = self.state.get_history(100, None)
        self.assertIn("[Summary of earlier conversation]", hist[0]["content"])
        self.assertEqual(hist[-1]["content"], "final answer")
        self.assertEqual(chat_mock.call_count, 2)


    @mock.patch.object(providers, "price_for")
    @mock.patch.object(providers, "chat")
    def test_auto_compact_fires_at_full_window(self, chat_mock, price_mock):
        """Integration: history past the model's window (default threshold =
        100%) triggers compaction without any explicit config."""
        price_mock.return_value = {"context": 300}  # 300 tokens = 1,200 chars
        cfg = make_config()
        cfg.context_limit_explicit = False  # as if the key is absent
        chat_mock.side_effect = [("SUMMARY", "", None),
                                 ("final answer", "", None)]
        hist = self.state.get_history(100, None)
        for i in range(8):
            hist.append({"role": "user" if i % 2 == 0 else "assistant",
                         "content": "msg %d " % i + "z" * 300})
        main.handle_message(cfg, self.tg, msg(text="continue"), self.state)
        hist = self.state.get_history(100, None)
        self.assertIn("[Summary of earlier conversation]", hist[0]["content"])
        self.assertEqual(hist[-1]["content"], "final answer")
        self.assertEqual(chat_mock.call_count, 2)


class TestCompactThreshold(unittest.TestCase):
    """Auto-compact threshold: 100% of the model window by default;
    explicit context_limit_chars overrides; missing metadata falls back."""

    def setUp(self):
        self.tg = FakeTG()
        self.state = main.State(True)
        disable_titles(self)

    @mock.patch.object(providers, "price_for")
    def test_default_is_full_model_window(self, price_mock):
        price_mock.return_value = {"context": 1048576}
        cfg = make_config()
        cfg.context_limit_explicit = False  # as if the key is absent
        self.assertEqual(main._compact_limit_chars(cfg, "go/mimo-v2.6-flash"),
                         1048576 * main.CHARS_PER_TOKEN)

    @mock.patch.object(providers, "price_for")
    def test_explicit_config_value_wins(self, price_mock):
        cfg = make_config(context_limit_chars=100)
        self.assertEqual(main._compact_limit_chars(cfg, "go/mimo-v2.6-flash"),
                         100)
        price_mock.assert_not_called()

    @mock.patch.object(providers, "price_for", return_value=None)
    def test_no_metadata_falls_back_to_config(self, price_mock):
        cfg = make_config()
        cfg.context_limit_explicit = False
        self.assertEqual(main._compact_limit_chars(cfg, "go/unknown-model"),
                         120_000)


class TestStreaming(unittest.TestCase):
    def setUp(self):
        self.tg = FakeTG()
        self.state = main.State(True)
        disable_titles(self)

    @mock.patch.object(providers, "chat_stream")
    def test_stream_drafts_then_final(self, stream_mock):
        self.cfg = make_config(stream_drafts=True)
        stream_mock.return_value = iter([
            ("reasoning", "thinking hard..."),
            ("content", "hello "),
            ("content", "**world**"),
        ])
        main.handle_message(self.cfg, self.tg, msg(text="hi"), self.state)
        # live draft shown (first delta flushes immediately)
        self.assertTrue(self.tg.drafts)
        self.assertIn("<tg-thinking>", self.tg.drafts[0][2]["html"])
        # final rich message with full answer markdown
        self.assertEqual(self.tg.rich[-1][1], {"markdown": "hello **world**"})
        # history has exactly one user + one assistant turn
        hist = self.state.get_history(100, None)
        self.assertEqual([m["role"] for m in hist], ["user", "assistant"])

    @mock.patch.object(providers, "chat")
    @mock.patch.object(providers, "chat_stream", side_effect=providers.ProviderError("nope"))
    def test_stream_failure_falls_back(self, _stream, chat_mock):
        self.cfg = make_config(stream_drafts=True)
        chat_mock.return_value = ("blocking answer", "", None)
        main.handle_message(self.cfg, self.tg, msg(text="hi"), self.state)
        chat_mock.assert_called_once()
        self.assertEqual(self.tg.rich[-1][1], {"markdown": "blocking answer"})
        hist = self.state.get_history(100, None)
        self.assertEqual([m["role"] for m in hist], ["user", "assistant"])

    @mock.patch.object(providers, "chat")
    @mock.patch.object(providers, "chat_stream")
    def test_stream_disabled_config(self, stream_mock, chat_mock):
        self.cfg = make_config(stream_drafts=False)
        chat_mock.return_value = ("answer", "", None)
        main.handle_message(self.cfg, self.tg, msg(text="hi"), self.state)
        stream_mock.assert_not_called()
        chat_mock.assert_called_once()


class TestPersistence(unittest.TestCase):
    def setUp(self):
        import sessions
        import tempfile
        self._dir = tempfile.TemporaryDirectory()
        self.store = sessions.Store(os.path.join(self._dir.name, "test.db"))

    def tearDown(self):
        self.store.close()
        self._dir.cleanup()

    def test_roundtrip(self):
        state = main.State(True, store=self.store)
        hist = state.get_history(100, 77)
        hist.extend([{"role": "user", "content": "q"},
                     {"role": "assistant", "content": "a"}])
        state.model[(100, 77)] = "go/m"
        state.persist(100, 77)
        state.topics.setdefault(100, []).append(77)
        self.store.add_topic(100, 77, "work")

        state2 = main.State(True, store=self.store)  # simulated restart
        self.assertEqual(state2.get_history(100, 77), hist)
        self.assertEqual(state2.model_for(100, 77, "x"), "go/m")
        self.assertEqual(state2.topics, {100: [77]})
        self.assertEqual(state2.session_count[100], 2)

    def test_main_chat_uses_zero_sentinel(self):
        state = main.State(True, store=self.store)
        state.get_history(5, None).append({"role": "user", "content": "hi"})
        state.persist(5, None)
        self.assertEqual([r[:2] for r in self.store.list_sessions()], [(5, 0)])
        state2 = main.State(True, store=self.store)
        self.assertEqual(state2.get_history(5, None)[0]["content"], "hi")

    def test_delete_persists(self):
        state = main.State(True, store=self.store)
        state.get_history(9, 3).append({"role": "user", "content": "x"})
        state.persist(9, 3)
        self.store.delete_session(9, 3)
        state2 = main.State(True, store=self.store)
        self.assertEqual(state2.history.get((9, 3)), None)

    def test_delete_chat_returns_topics(self):
        self.store.add_topic(42, 11, "a")
        self.store.add_topic(42, 22, "b")
        self.store.save_session(42, 11, None, [{"role": "user", "content": "x"}])
        got = self.store.delete_chat(42)
        self.assertEqual(sorted(got), [11, 22])
        self.assertEqual(self.store.list_sessions(), [])
        self.assertEqual(self.store.load_topics(), [])

    def test_corrupt_row_skipped(self):
        self.store.conn.execute(
            "INSERT INTO sessions VALUES (1, 0, NULL, 'not-json', 0)")
        self.store.conn.commit()
        self.assertEqual(self.store.load_sessions(), [])


class TestCLI(unittest.TestCase):
    def setUp(self):
        import sessions
        import tempfile
        self._dir = tempfile.TemporaryDirectory()
        self.db = os.path.join(self._dir.name, "cli.db")
        store = sessions.Store(self.db)
        store.save_session(100, 0, "go/m", [{"role": "user", "content": "a"}] * 3)
        store.save_session(100, 77, None, [{"role": "user", "content": "b"}])
        store.add_topic(100, 77, "old name")
        store.close()

    def tearDown(self):
        self._dir.cleanup()

    def _run(self, argv):
        import cli
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with mock.patch.object(cli.config_mod, "load", return_value=make_config()), \
             mock.patch.object(cli.config_mod, "sessions_path", return_value=self.db), \
             redirect_stdout(buf):
            code = cli.run(argv)
        return code, buf.getvalue()

    def test_list(self):
        code, out = self._run(["list"])
        self.assertEqual(code, 0)
        self.assertIn("chat=100 thread=main", out)
        self.assertIn("msgs=3", out)
        self.assertIn("old name", out)

    def test_delete(self):
        code, out = self._run(["delete", "100"])
        self.assertEqual(code, 0)
        self.assertIn("deleted context", out)
        code, out = self._run(["list"])
        self.assertIn("thread=77", out)
        self.assertNotIn("thread=main", out)

    def test_delete_missing(self):
        code, out = self._run(["delete", "999"])
        self.assertEqual(code, 1)
        self.assertIn("no such session", out)

    def test_rename(self):
        code, out = self._run(["rename", "100:77", "new name"])
        self.assertEqual(code, 0)
        import sessions
        store = sessions.Store(self.db)
        names = {(c, t): n for c, t, n in store.load_topics()}
        store.close()
        self.assertEqual(names[(100, 77)], "new name")


class TestStop(unittest.TestCase):
    """The /stop command: watcher interception + turn interruption."""

    def setUp(self):
        self.tg = FakeTG()
        self.state = main.State(True)
        self.cfg = make_config(stream_drafts=True)
        self.q = queue.Queue()

    def test_stop_cmd_matching(self):
        self.assertTrue(main._is_stop_cmd(msg(text="/stop")))
        self.assertTrue(main._is_stop_cmd(msg(text="/stop@vdt_keirai_bot")))
        self.assertFalse(main._is_stop_cmd(msg(text="/stoppers")))
        self.assertFalse(main._is_stop_cmd(msg(text="/go stop")))

    def test_route_queues_when_idle(self):
        m = msg(text="/stop")
        consumed = main._route_update(self.state, self.q, {"message": m})
        self.assertFalse(consumed)
        self.assertEqual(self.q.qsize(), 1)
        self.assertFalse(self.state.interrupted((100, None)))

    def test_route_consumes_during_active_turn(self):
        self.state.begin_turn((100, None))
        consumed = main._route_update(self.state, self.q,
                                      {"message": msg(text="/stop")})
        self.assertTrue(consumed)
        self.assertTrue(self.q.empty())
        self.assertTrue(self.state.interrupted((100, None)))

    def test_stop_replies_when_idle(self):
        main.handle_message(self.cfg, self.tg, msg(text="/stop"), self.state)
        self.assertIn("nothing is running", self.tg.sent[-1][1])

    @mock.patch.object(providers, "chat_stream")
    def test_interrupt_mid_stream_records_context(self, stream_mock):
        state = self.state

        def gen():
            yield "reasoning", "thinking hard"
            state.request_interrupt((100, None))  # user sends /stop now
            yield "content", "partial answer will not finish"
            yield "content", " - rest of it"

        stream_mock.return_value = gen()
        main.handle_message(self.cfg, self.tg, msg(text="hi"), state)
        hist = state.get_history(100, None)
        # marker is in context for the model's next turn
        self.assertIn(main.INTERRUPT_NOTE, hist[-1]["content"])
        self.assertIn("partial answer", hist[-1]["content"])
        # user was told; no final full answer was delivered
        self.assertTrue(any("stopped" in t for _c, t, _th in self.tg.sent))
        self.assertEqual(self.tg.rich, [])
        # turn bookkeeping cleared
        self.assertNotIn((100, None), state.active_turns)
        self.assertNotIn((100, None), state.interrupts)

    @mock.patch.object(providers, "chat_stream")
    def test_interrupt_drops_live_placeholder(self, stream_mock):
        """Edit-mode placeholder is deleted when the turn is stopped."""
        def gen():
            yield "reasoning", "hmm"
            self.state.request_interrupt((100, None))
            yield "content", "started"

        stream_mock.return_value = gen()
        g = {"id": 100, "type": "supergroup"}
        main.handle_message(self.cfg, self.tg, msg(text="hi", chat=g), self.state)
        self.assertTrue(self.tg.rich)          # placeholder was created
        self.assertTrue(self.tg.deleted)       # ...and cleaned up on /stop
        self.assertTrue(any("stopped" in t for _c, t, _th in self.tg.sent))


class TestToolLoop(unittest.TestCase):
    """LLM tool calls: execute, feed back, then final answer."""

    def setUp(self):
        self.tg = FakeTG()
        self.state = main.State(True)
        self.cfg = make_config(stream_drafts=False)
        disable_titles(self)

    @mock.patch.object(providers, "chat")
    def test_tool_round_then_answer(self, chat_mock):
        fd, path = tempfile.mkstemp(suffix=".txt")
        os.close(fd)
        with open(path, "w") as f:
            f.write("SECRET-CONTENT-42")
        self.addCleanup(os.unlink, path)
        chat_mock.side_effect = [
            ("", "", [{"id": "c1", "name": "read_file",
                       "arguments": {"path": path}}]),
            ("the content is SECRET-CONTENT-42", "", None),
        ]
        main.handle_message(self.cfg, self.tg, msg(text="what's inside?"),
                            self.state)
        # progress line shows the tool being run
        self.assertTrue(any("read_file" in t for _c, t, _th in self.tg.sent))
        # final answer delivered
        self.assertEqual(self.tg.rich[-1][1],
                         {"markdown": "the content is SECRET-CONTENT-42"})
        # history keeps only user + final assistant (tool rounds not persisted)
        hist = self.state.get_history(100, None)
        self.assertEqual([m["role"] for m in hist], ["user", "assistant"])
        # second request carried the tool result + tool specs
        second = chat_mock.call_args_list[1]
        msgs = second[0][3]
        tool_msgs = [m for m in msgs if m.get("role") == "tool"]
        self.assertEqual(len(tool_msgs), 1)
        self.assertIn("SECRET-CONTENT-42", tool_msgs[0]["content"])
        self.assertEqual(tool_msgs[0]["tool_call_id"], "c1")
        self.assertTrue(second[1]["tools"])

    @mock.patch.object(providers, "chat")
    def test_tools_disabled_no_specs(self, chat_mock):
        self.cfg = make_config(tools_enabled=False, stream_drafts=False)
        chat_mock.return_value = ("plain", "", None)
        main.handle_message(self.cfg, self.tg, msg(text="hi"), self.state)
        self.assertIsNone(chat_mock.call_args[1]["tools"])

    @mock.patch.object(providers, "chat")
    def test_tool_rounds_run_without_cap(self, chat_mock):
        """No round cap (user directive): tool rounds continue until the model
        stops asking for tools - the old cap's tools-off final call is what
        produced text-form tool calls in chat."""
        tc = [{"id": "c1", "name": "read_file", "arguments": {"path": "missing.txt"}}]
        rounds = 9  # well past the old 6-round cap
        chat_mock.side_effect = [("", "", tc) for _ in range(rounds)] + \
            [("final answer", "", None)]
        main.handle_message(self.cfg, self.tg, msg(text="loop me"), self.state)
        self.assertEqual(self.tg.rich[-1][1], {"markdown": "final answer"})
        self.assertEqual(chat_mock.call_count, rounds + 1)
        # every round's tool result was fed back into the conversation
        last = chat_mock.call_args_list[-1][0][3]
        self.assertEqual(len([m for m in last if m.get("role") == "tool"]),
                         rounds)


class TestReplyTo(unittest.TestCase):
    """Item 17: reply/quote context injected into the prompt."""

    def test_no_reply(self):
        self.assertEqual(main._reply_context(msg(text="hi")), "")

    def test_reply_annotation(self):
        m = msg(text="and this?")
        m["reply_to_message"] = {"from": {"username": "alice"},
                                 "text": "the old answer was42"}
        ctx = main._reply_context(m)
        self.assertIn("user is replying to this message (from @alice)", ctx)
        self.assertIn("42", ctx)

    def test_quote_annotation(self):
        m = msg(text="what does this mean?")
        m["reply_to_message"] = {"from": {"first_name": "Bob"}, "text": "long text"}
        m["quote"] = {"text": "just this bit"}
        ctx = main._reply_context(m)
        self.assertIn("quoting this part", ctx)
        self.assertIn("just this bit", ctx)

    def test_reply_without_text(self):
        m = msg(text="huh?")
        m["reply_to_message"] = {"from": {}, "text": ""}
        ctx = main._reply_context(m)
        self.assertIn("no extractable text", ctx)

    def test_long_reply_capped(self):
        m = msg(text="huh?")
        m["reply_to_message"] = {"from": {}, "text": "x" * 9000}
        ctx = main._reply_context(m)
        self.assertLess(len(ctx), 1700)

    @mock.patch.object(providers, "chat", return_value=("ok", "", None))
    def test_context_reaches_prompt(self, chat_mock):
        tg = FakeTG()
        m = msg(text="what about this?")
        m["reply_to_message"] = {"from": {"username": "alice"},
                                 "text": "remember me"}
        main.handle_message(make_config(stream_drafts=False), tg, m,
                            main.State(True))
        user_texts = [msg_["content"] for msg_ in chat_mock.call_args[0][3]
                      if msg_["role"] == "user" and isinstance(msg_["content"], str)]
        self.assertTrue(any("user is replying to this message" in t
                            and "remember me" in t for t in user_texts))


class TestEditStreaming(unittest.TestCase):
    """Item 8: non-private chats stream via send + editMessageText(rich)."""

    def setUp(self):
        disable_titles(self)

    @mock.patch.object(providers, "chat_stream")
    def test_group_stream_uses_edits_not_drafts(self, stream_mock):
        stream_mock.return_value = iter([("reasoning", "hmm about that"),
                                         ("content", "hello **world**")])
        tg = FakeTG()
        state = main.State(True)
        g = {"id": 100, "type": "supergroup"}
        main.handle_message(make_config(stream_drafts=True), tg,
                            msg(text="hi", chat=g), state)
        self.assertEqual(tg.drafts, [])       # drafts are private-only
        self.assertTrue(tg.rich)              # placeholder message created
        self.assertTrue(tg.edits)             # live edits happened
        # final delivery: thinking details edited into the placeholder,
        # full answer markdown delivered
        self.assertEqual(tg.rich[-1][1], {"markdown": "hello **world**"})
        edited_ids = [mid for _c, mid, _s in tg.edits]
        self.assertIn(1000 + 1, edited_ids)   # first message was edited

    @mock.patch.object(providers, "chat_stream")
    def test_edit_failure_falls_back_to_plain_send(self, stream_mock):
        stream_mock.return_value = iter([("content", "answer text")])
        tg = FakeTG()

        def boom(chat_id, message_id, markdown=None, html=None):
            import telegram as tg_mod
            raise tg_mod.TelegramError("editMessageText not supported")

        tg.edit_rich = boom
        g = {"id": 100, "type": "supergroup"}
        main.handle_message(make_config(stream_drafts=True), tg,
                            msg(text="hi", chat=g), main.State(True))
        # live edits failed -> placeholder dropped, answer sent fresh
        self.assertTrue(tg.deleted)
        self.assertIn({"markdown": "answer text"}, [s for _c, s, _t in tg.rich])


class TestProviderPriority(unittest.TestCase):
    """P3: Go-first default + catalog-checked error fallback (group 25)."""

    def setUp(self):
        self.tg = FakeTG()
        self.state = main.State(True)
        self.cfg = make_config()
        disable_titles(self)

    def test_default_model_is_go(self):
        self.assertEqual(make_config().default_model, "go/mimo-v2.6-flash")

    @mock.patch.object(main, "_alternate_provider", return_value=("zen", "zk"))
    @mock.patch.object(providers, "chat")
    def test_provider_error_falls_back_once(self, chat_mock, alt_mock):
        chat_mock.side_effect = [providers.ProviderError("boom"),
                                 ("recovered", "", None)]
        main.handle_message(self.cfg, self.tg, msg(text="hello"), self.state)
        self.assertEqual(chat_mock.call_args_list[0][0][0], "go")   # go first
        self.assertEqual(chat_mock.call_args_list[1][0][0], "zen")  # then zen
        self.assertTrue(any(s[1].get("markdown") == "recovered"
                            for s in self.tg.rich))

    @mock.patch.object(main, "_alternate_provider", return_value=None)
    @mock.patch.object(providers, "chat",
                       side_effect=providers.ProviderError("boom"))
    def test_error_without_alternate_is_reported(self, chat_mock, alt_mock):
        main.handle_message(self.cfg, self.tg, msg(text="hello"), self.state)
        self.assertTrue(any("error" in t for _c, t, _t in self.tg.sent))

    def test_alternate_requires_key_and_catalog_entry(self):
        # catalog has the model -> usable alternate
        with mock.patch.object(providers, "cached_models",
                               return_value=([{"id": "glm-5.3-flash"}], False)):
            self.assertEqual(main._alternate_provider(self.cfg, "zen", "glm-5.3-flash"),
                             ("go", "gk"))
            self.assertIsNone(main._alternate_provider(self.cfg, "zen", "other"))
        # catalog unreachable -> never blind-retry
        with mock.patch.object(providers, "cached_models",
                               side_effect=providers.ProviderError("down")):
            self.assertIsNone(main._alternate_provider(self.cfg, "zen", "glm-5.3-flash"))
        # no key on the alternate -> no fallback
        nokey = make_config(providers={"zen": {"api_key": "zk"}, "go": {"api_key": ""}})
        self.assertIsNone(main._alternate_provider(nokey, "zen", "glm-5.3-flash"))


class TestUsageAccounting(unittest.TestCase):
    """P3: token/cost accumulation per session (group 21)."""

    def setUp(self):
        self.tg = FakeTG()
        self.state = main.State(True)
        self.cfg = make_config()
        disable_titles(self)

    @mock.patch.object(providers, "price_for")
    @mock.patch.object(providers, "chat")
    def test_usage_and_cost_accumulate(self, chat_mock, price_mock):
        price_mock.return_value = {"cost_in": 0.15, "cost_out": 0.5,
                                   "cost_cache_read": 0.03}

        def fake(*args, **kw):
            kw["usage_out"].update({
                "prompt_tokens": 1000, "completion_tokens": 100,
                "prompt_tokens_details": {"cached_tokens": 500}})
            return ("answer", "", None)

        chat_mock.side_effect = fake
        main.handle_message(self.cfg, self.tg, msg(text="hello"), self.state)
        m = self.state.meta[(100, None)]
        self.assertEqual(m["tokens_in"], 1000)
        self.assertEqual(m["tokens_out"], 100)
        self.assertEqual(m["tokens_cached_read"], 500)
        self.assertEqual(m["tokens_cached_write"], 0)
        self.assertAlmostEqual(m["cost_usd"],
                               (500 * 0.15 + 500 * 0.03 + 100 * 0.5) / 1e6)
        self.assertRegex(m["session_id"], r"^\d{8}-\d{4}-[0-9a-f]{4}$")
        # keeps accumulating across turns
        main.handle_message(self.cfg, self.tg, msg(text="again"), self.state)
        self.assertEqual(self.state.meta[(100, None)]["tokens_in"], 2000)
        self.assertAlmostEqual(self.state.meta[(100, None)]["cost_usd"],
                               2 * (500 * 0.15 + 500 * 0.03 + 100 * 0.5) / 1e6)

    @mock.patch.object(providers, "price_for", return_value=None)
    @mock.patch.object(providers, "chat")
    def test_missing_pricing_counts_tokens_only(self, chat_mock, price_mock):
        def fake(*args, **kw):
            kw["usage_out"].update({"prompt_tokens": 500,
                                    "completion_tokens": 50})
            return ("a", "", None)

        chat_mock.side_effect = fake
        main.handle_message(self.cfg, self.tg, msg(text="hello"), self.state)
        m = self.state.meta[(100, None)]
        self.assertEqual(m["tokens_in"], 500)   # tokens still counted
        self.assertEqual(m["cost_usd"], 0.0)    # but no price -> no cost


class TestContextCostCommands(unittest.TestCase):
    """P3: /context and /cost (group 21)."""

    def setUp(self):
        self.tg = FakeTG()
        self.state = main.State(True)
        self.cfg = make_config()
        disable_titles(self)

    @mock.patch.object(providers, "price_for")
    @mock.patch.object(providers, "chat", return_value=("a", "", None))
    def test_context_shows_usage_without_cost(self, _, price_mock):
        price_mock.return_value = {"context": 1048576}
        main.handle_message(self.cfg, self.tg, msg(text="hello"), self.state)
        self.tg.sent.clear()
        main.handle_message(self.cfg, self.tg, msg(text="/context"), self.state)
        text = self.tg.sent[-1][1]
        self.assertIn("<b>session</b>", text)
        self.assertIn("<b>model</b>", text)
        self.assertIn("<b>context</b>", text)
        # used/max against the model's REAL window (models.dev, tokens)
        self.assertIn("/ 1,048,576 tokens", text)
        self.assertIn("chars", text)
        self.assertIn("<b>tokens</b>", text)
        self.assertIn("auto-compact:", text)
        self.assertNotIn("<b>cost</b>", text)
        self.assertRegex(text, r"\d{8}-\d{4}-[0-9a-f]{4}")

    @mock.patch.object(providers, "price_for",
                       return_value={"context": 500000})
    def test_cost_adds_cost_line_and_context_block(self, _):
        main.handle_message(self.cfg, self.tg, msg(text="/cost"), self.state)
        text = self.tg.sent[-1][1]
        self.assertIn("<b>cost</b>: $", text)
        self.assertIn("<b>tokens</b>", text)
        self.assertIn("<b>context</b>", text)
        self.assertIn("/ 500,000 tokens", text)  # model window, not config
        self.assertIn("<b>model</b>", text)
        self.assertIn("triggers at", text)  # threshold lives on its own line

    @mock.patch.object(providers, "price_for", return_value=None)
    def test_context_max_unknown_without_metadata(self, _):
        main.handle_message(self.cfg, self.tg, msg(text="/context"), self.state)
        self.assertIn("max unknown", self.tg.sent[-1][1])


class TestModelsStats(unittest.TestCase):
    """P3: /models stats come from models.dev (the API returns ids only)."""

    @mock.patch.object(providers, "models_metadata")
    @mock.patch.object(providers, "cached_models")
    def test_models_merges_metadata(self, cm_mock, mm_mock):
        cm_mock.return_value = ([{"id": "glm-5.3-flash", "context": None,
                                  "cost_in": None, "cost_out": None,
                                  "max_output": None}], False)
        mm_mock.return_value = ({"zen": {"glm-5.3-flash": {
                                    "context": 1000000, "cost_in": 0.15,
                                    "cost_out": 0.5, "max_output": 131072}},
                                 "go": {}}, False)
        tg = FakeTG()
        main.handle_message(make_config(), tg, msg(text="/models"), main.State(True))
        text = tg.sent[0][1]
        self.assertIn("ctx ", text)
        self.assertIn("$0.15/", text)
        self.assertIn("per 1M", text)


class TestSessionMeta(unittest.TestCase):
    """sessions.db session_meta: ids + accumulators + backfill."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = os.path.join(self._dir.name, "meta.db")

    def tearDown(self):
        self._dir.cleanup()

    def test_new_session_id_format(self):
        import sessions
        self.assertRegex(sessions.new_session_id(1790000000),
                         r"^\d{8}-\d{4}-[0-9a-f]{4}$")

    def test_meta_lifecycle(self):
        import sessions
        store = sessions.Store(self.db)
        meta = store.ensure_meta(100, 0)
        self.assertRegex(meta["session_id"], r"^\d{8}-\d{4}-[0-9a-f]{4}$")
        store.record_usage(100, 0, 0.25, {"tokens_in": 10, "tokens_out": 5,
                                          "tokens_cached_read": 2,
                                          "tokens_cached_write": 0})
        store.record_usage(100, 0, 0.25, {"tokens_in": 1, "tokens_out": 0,
                                          "tokens_cached_read": 0,
                                          "tokens_cached_write": 0})
        got = store.load_meta()[(100, 0)]
        self.assertAlmostEqual(got["cost_usd"], 0.5)
        self.assertEqual(got["tokens_in"], 11)
        self.assertEqual(got["tokens_cached_read"], 2)
        # survives history REPLACE + process restart
        store.save_session(100, 0, None, [{"role": "user", "content": "x"}])
        store.close()
        store2 = sessions.Store(self.db)
        got2 = store2.load_meta()[(100, 0)]
        self.assertEqual(got2["session_id"], meta["session_id"])
        self.assertAlmostEqual(got2["cost_usd"], 0.5)
        # deleting the session clears its meta too
        store2.delete_session(100, 0)
        self.assertEqual(store2.load_meta(), {})
        store2.close()

    def test_backfill_gives_existing_sessions_ids(self):
        import sessions
        store = sessions.Store(self.db)
        store.save_session(7, 0, None, [{"role": "user", "content": "old"}])
        self.assertEqual(store.load_meta(), {})  # pre-meta session: no row yet
        store.close()
        store2 = sessions.Store(self.db)  # reopen -> backfill runs
        meta = store2.load_meta()[(7, 0)]
        self.assertRegex(meta["session_id"], r"^\d{8}-\d{4}-[0-9a-f]{4}$")
        store2.close()

    def test_delete_chat_clears_meta(self):
        import sessions
        store = sessions.Store(self.db)
        store.ensure_meta(5, 0)
        store.ensure_meta(5, 9)
        store.delete_chat(5)
        self.assertEqual(store.load_meta(), {})
        store.close()


class TestTopicFlowMode(unittest.TestCase):
    """P4 group 22: /topic enter/exit with typed confirmations + getMe gates."""

    def setUp(self):
        self.tg = FakeTG()
        self.state = main.State(True)
        self.cfg = make_config()
        disable_titles(self)

    def _confirm(self, answer):
        main.handle_message(self.cfg, self.tg, msg(text="/topic"), self.state)
        self.assertIn("type <code>yes</code>", self.tg.sent[-1][1])
        main.handle_message(self.cfg, self.tg, msg(text=answer), self.state)

    def test_enter_flow_after_confirmation(self):
        self._confirm("yes")
        self.assertTrue(self.cfg.topic_flow)
        self.assertIn("topic flow is <b>on</b>", self.tg.sent[-1][1])
        self.assertNotIn((100, None), self.state.prompts)

    def test_enter_refused_when_users_can_create_topics(self):
        self.tg.me["allows_users_to_create_topics"] = True
        self._confirm("yes")
        self.assertFalse(self.cfg.topic_flow)
        self.assertIn("Disallow users to create topics", self.tg.sent[-1][1])

    def test_enter_refused_when_threaded_mode_off(self):
        self.tg.me["has_topics_enabled"] = False
        self._confirm("yes")
        self.assertFalse(self.cfg.topic_flow)
        self.assertIn("Threaded mode is off", self.tg.sent[-1][1])

    def test_no_cancels(self):
        self._confirm("no")
        self.assertFalse(self.cfg.topic_flow)
        self.assertIn("cancelled", self.tg.sent[-1][1])

    def test_command_cancels_pending_prompt_and_runs(self):
        main.handle_message(self.cfg, self.tg, msg(text="/topic"), self.state)
        self.assertIn((100, None), self.state.prompts)
        main.handle_message(self.cfg, self.tg, msg(text="/help"), self.state)
        self.assertNotIn((100, None), self.state.prompts)
        self.assertIn("Keirai", self.tg.sent[-1][1])
        self.assertFalse(self.cfg.topic_flow)

    @mock.patch.object(providers, "chat", return_value=("answer", "", None))
    def test_plain_message_cancels_prompt_as_do_nothing(self, chat_mock):
        main.handle_message(self.cfg, self.tg, msg(text="/topic"), self.state)
        main.handle_message(self.cfg, self.tg, msg(text="hello instead"), self.state)
        self.assertFalse(self.cfg.topic_flow)  # no mode change
        self.assertNotIn((100, None), self.state.prompts)
        # the message was answered normally (the "do nothing" choice)
        self.assertTrue(any(s[1].get("markdown") == "answer"
                            for s in self.tg.rich))

    @mock.patch.object(providers, "chat", return_value=("answer", "", None))
    def test_leave_flow(self, chat_mock):
        self.cfg.topic_flow = True
        main.handle_message(self.cfg, self.tg, msg(text="/topic"), self.state)
        self.assertIn("leave topic flow", self.tg.sent[-1][1])
        main.handle_message(self.cfg, self.tg, msg(text="yes"), self.state)
        self.assertFalse(self.cfg.topic_flow)
        self.assertIn("topic flow is <b>off</b>", self.tg.sent[-1][1])


class TestDeliveryRules(unittest.TestCase):
    """P4 group 20: mode gates, per-view hints, command availability."""

    def setUp(self):
        self.tg = FakeTG()
        self.state = main.State(True)
        self.cfg = make_config()
        disable_titles(self)

    @mock.patch.object(providers, "chat")
    def test_flow_off_topics_inert_for_plain_and_commands(self, chat_mock):
        main.handle_message(self.cfg, self.tg,
                            msg(text="hello", message_thread_id=7), self.state)
        main.handle_message(self.cfg, self.tg,
                            msg(text="/help", message_thread_id=7), self.state)
        chat_mock.assert_not_called()
        hints = [t for _c, t, _th in self.tg.sent]
        self.assertEqual(len(hints), 2)
        for t in hints:
            self.assertIn("topic flow is off", t)

    @mock.patch.object(providers, "chat")
    def test_flow_on_all_gates(self, chat_mock):
        self.cfg.topic_flow = True
        # plain message -> hint, no answer
        main.handle_message(self.cfg, self.tg, msg(text="hello"), self.state)
        self.assertIn("topic flow is on", self.tg.sent[-1][1])
        chat_mock.assert_not_called()
        # session-scoped commands -> needs-session hint (incl. /stop)
        for command in ("/compact", "/stop", "/delete", "/context", "/cost"):
            main.handle_message(self.cfg, self.tg, msg(text=command), self.state)
            self.assertIn("needs a session", self.tg.sent[-1][1], command)
        # /model global is chat-wide and allowed in All
        main.handle_message(self.cfg, self.tg,
                            msg(text="/model global go/glm-5.3-flash"), self.state)
        self.assertEqual(self.cfg.default_model, "go/glm-5.3-flash")
        self.assertIn("default model set", self.tg.sent[-1][1])
        # allowed command: /session runs (empty list)
        main.handle_message(self.cfg, self.tg, msg(text="/session"), self.state)
        self.assertIn("no other sessions", self.tg.sent[-1][1])
        chat_mock.assert_not_called()

    @mock.patch.object(providers, "chat")
    def test_flow_on_media_in_all_hinted(self, chat_mock):
        self.cfg.topic_flow = True
        m = msg(text=None, photo=[{"file_id": "f1", "file_size": 100}])
        main.handle_message(self.cfg, self.tg, m, self.state)
        chat_mock.assert_not_called()
        self.assertIn("topic flow is on", self.tg.sent[-1][1])

    @mock.patch.object(providers, "chat")
    def test_forum_topic_created_registered_not_hinted(self, chat_mock):
        m = msg(text="", message_thread_id=999)
        m["forum_topic_created"] = {"name": "client topic"}
        main.handle_message(self.cfg, self.tg, m, self.state)
        self.assertIn(999, self.state.topics[100])
        self.assertEqual(self.state.topic_names[(100, 999)], "client topic")
        self.assertEqual(self.tg.sent, [])  # service messages never hinted

    @mock.patch.object(providers, "chat")
    def test_bots_own_events_witnessed_not_allowed_list_miss(self, chat_mock):
        """Telegram echoes back events the bot itself caused (log showed them
        as 'not in allowed_users'): they must be witnessed quietly instead."""
        self.state.bot_id = 8887915792
        m = msg(text="", message_thread_id=7654)
        m["from"] = {"id": 8887915792, "username": "vdt_keirai_bot"}
        m["forum_topic_created"] = {"name": "bot topic"}
        main.handle_message(self.cfg, self.tg, m, self.state)
        self.assertIn(7654, self.state.topics[100])
        self.assertEqual(self.state.topic_names[(100, 7654)], "bot topic")
        self.assertEqual(self.tg.sent, [])            # no reply, no hint
        chat_mock.assert_not_called()
        # the bot's own plain message: quiet no-op, never treated as a user
        m2 = msg(text="my own reply")
        m2["from"] = {"id": 8887915792, "username": "vdt_keirai_bot"}
        main.handle_message(self.cfg, self.tg, m2, self.state)
        self.assertEqual(self.tg.sent, [])
        chat_mock.assert_not_called()


class TestSessionCommands(unittest.TestCase):
    """P4 group 22: /session listing, switching matrix, prompts, ping."""

    def setUp(self):
        self.tg = FakeTG()
        self.state = main.State(True)
        self.cfg = make_config()
        disable_titles(self)

    def _mk(self, slot, text, name=None, updated=0.0):
        """Session at a slot with one exchange, fixed last-active time."""
        self.state.create_session(100, slot, name=name)
        hist = self.state.get_history(100, slot)
        hist.append({"role": "user", "content": text})
        hist.append({"role": "assistant", "content": "ok"})
        self.state.updated[(100, slot)] = updated
        return self.state.meta[(100, slot)]["session_id"]

    # ------------------------------------------------ listing + normal flow

    @mock.patch.object(providers, "chat")
    def test_list_excludes_current_and_formats(self, chat_mock):
        sid_main = self._mk(None, "main conversation", name="main session",
                            updated=300)
        self._mk(-1, "older topic talk", name="older", updated=100)
        sid_a = self._mk(-2, "recent talk about trains", name="recent",
                         updated=200)
        main.handle_message(self.cfg, self.tg, msg(text="/session"), self.state)
        text = self.tg.sent[-1][1]
        self.assertIn("page 1/1", text)
        self.assertIn("<b>recent</b>", text)
        self.assertIn(sid_a, text)
        self.assertIn("older", text)
        self.assertNotIn(sid_main, text)      # current session excluded
        self.assertIn("recent talk about tra", text)  # preview (truncated)
        self.assertIn("1970", text)           # timestamp rendered

    @mock.patch.object(providers, "chat")
    def test_list_paging_and_switch_by_n(self, chat_mock):
        self._mk(None, "current", name="current", updated=999)
        sids = [self._mk(-(i + 1), "talk %d" % (7 - i), name="s%d" % (7 - i),
                         updated=(7 - i) * 100) for i in range(7)]
        # page 1: 5 of 7 rows
        main.handle_message(self.cfg, self.tg, msg(text="/session"), self.state)
        self.assertIn("page 1/2", self.tg.sent[-1][1])
        # page 2: last 2
        main.handle_message(self.cfg, self.tg, msg(text="/session page 2"),
                            self.state)
        text = self.tg.sent[-1][1]
        self.assertIn("page 2/2", text)
        # /session 1 on page 2 -> row with updated=200 (second in sort order)
        main.handle_message(self.cfg, self.tg, msg(text="/session 1"),
                            self.state)
        expected = sids[5]  # sorted desc: 700,600,500,400,300,200,100 -> page2[0]=200
        self.assertEqual(self.state.meta[(100, None)]["session_id"], expected)
        self.assertIn("switched to", self.tg.sent[-1][1])
        # out-of-range N on the page
        main.handle_message(self.cfg, self.tg, msg(text="/session 9"),
                            self.state)
        self.assertIn("no session #9 on this page", self.tg.sent[-1][1])

    @mock.patch.object(providers, "chat")
    def test_switch_by_id_and_unknown_id(self, chat_mock):
        self._mk(None, "current", name="current", updated=999)
        sid = self._mk(-1, "other talk", name="other", updated=100)
        main.handle_message(self.cfg, self.tg, msg(text="/session %s" % sid),
                            self.state)
        self.assertEqual(self.state.meta[(100, None)]["session_id"], sid)
        main.handle_message(self.cfg, self.tg,
                            msg(text="/session nope-not-real"), self.state)
        self.assertIn("no session with id", self.tg.sent[-1][1])

    @mock.patch.object(providers, "chat")
    def test_already_active_is_noop(self, chat_mock):
        sid = self._mk(None, "current", name="current", updated=999)
        main.handle_message(self.cfg, self.tg, msg(text="/session %s" % sid),
                            self.state)
        self.assertIn("already active", self.tg.sent[-1][1])

    # ------------------------------------------------ topic-flow switching

    @mock.patch.object(providers, "chat")
    def test_unbound_session_from_all_creates_topic_immediately(self, chat_mock):
        self.cfg.topic_flow = True
        sid = self._mk(-1, "parked talk", name="parked", updated=100)
        main.handle_message(self.cfg, self.tg, msg(text="/session"), self.state)
        main.handle_message(self.cfg, self.tg, msg(text="/session 1"), self.state)
        self.assertIn("parked", self.tg.topics)           # topic named after it
        tid = self.tg.topics["parked"]
        self.assertEqual(self.state.sid_slot(100, sid), tid)
        self.assertEqual(self.tg.sent[-1][2], tid)         # welcome in the topic

    @mock.patch.object(providers, "chat")
    def test_bound_session_from_all_prompts_ping(self, chat_mock):
        self.cfg.topic_flow = True
        self._mk(77, "bound talk", name="bound", updated=100)
        main.handle_message(self.cfg, self.tg, msg(text="/session"), self.state)
        main.handle_message(self.cfg, self.tg, msg(text="/session 1"), self.state)
        self.assertIn((100, None), self.state.prompts)
        self.assertIn("ping", self.tg.sent[-1][1])
        main.handle_message(self.cfg, self.tg, msg(text="1"), self.state)
        self.assertNotIn((100, None), self.state.prompts)
        # ping landed in topic 77, confirmation came back in All
        self.assertIn("ping", self.tg.sent[-2][1])
        self.assertEqual(self.tg.sent[-2][2], 77)
        self.assertIn("topic found", self.tg.sent[-1][1])

    @mock.patch.object(providers, "chat")
    def test_ping_on_dead_topic_recovers(self, chat_mock):
        self.cfg.topic_flow = True
        sid = self._mk(88, "dead talk", name="dead", updated=100)
        self.state.topics[100] = [88]
        self.state.topic_names[(100, 88)] = "dead"
        self.tg.fail_threads.add(88)  # sending there -> thread not found
        main.handle_message(self.cfg, self.tg, msg(text="/session"), self.state)
        main.handle_message(self.cfg, self.tg, msg(text="/session 1"), self.state)
        main.handle_message(self.cfg, self.tg, msg(text="1"), self.state)
        # dead topic unregistered, session relocated into a fresh topic
        self.assertNotIn(88, self.state.topics[100])
        new_tid = self.tg.topics["dead"]
        self.assertNotEqual(new_tid, 88)
        self.assertEqual(self.state.sid_slot(100, sid), new_tid)

    @mock.patch.object(providers, "chat", return_value=("answer", "", None))
    def test_unbound_session_in_topic_three_option_prompt(self, chat_mock):
        # a main-chat session that becomes unbound once flow is entered
        sid_main = self._mk(None, "main talk", name="main talk", updated=999)
        self.cfg.topic_flow = True
        main.handle_message(self.cfg, self.tg, msg(text="/new here"), self.state)
        tid = self.tg.topics["here"]
        sid_here = self.state.meta[(100, tid)]["session_id"]
        # viewing from inside the topic: list excludes current, shows main
        main.handle_message(self.cfg, self.tg,
                            msg(text="/session", message_thread_id=tid), self.state)
        text = self.tg.sent[-1][1]
        self.assertIn("main talk", text)
        self.assertNotIn(sid_here, text)
        # selecting it -> 3-option prompt (switch here / create new / nothing)
        main.handle_message(self.cfg, self.tg,
                            msg(text="/session 1", message_thread_id=tid),
                            self.state)
        prompt_text = self.tg.sent[-1][1]
        for expected in ("switch here", "create a new topic", "do nothing"):
            self.assertIn(expected, prompt_text)
        # "1" = switch here: main session moves into this topic, occupant parks
        main.handle_message(self.cfg, self.tg,
                            msg(text="1", message_thread_id=tid), self.state)
        self.assertEqual(self.state.sid_slot(100, sid_main), tid)
        # old session parked somewhere negative
        parked = self.state.sid_slot(100, sid_here)
        self.assertIsNotNone(parked)
        self.assertLess(parked, 0)
        self.assertIn("switched here", self.tg.sent[-1][1])

    @mock.patch.object(providers, "chat", return_value=("answer", "", None))
    def test_prompt_do_nothing_via_plain_message(self, chat_mock):
        self.cfg.topic_flow = True
        self._mk(77, "bound talk", name="bound", updated=100)
        main.handle_message(self.cfg, self.tg, msg(text="/session"), self.state)
        main.handle_message(self.cfg, self.tg, msg(text="/session 1"), self.state)
        self.assertIn((100, None), self.state.prompts)
        # a plain message in All = do nothing + handled normally (hint)
        main.handle_message(self.cfg, self.tg, msg(text="actually hello"),
                            self.state)
        self.assertNotIn((100, None), self.state.prompts)
        self.assertIn("topic flow is on", self.tg.sent[-1][1])
        chat_mock.assert_not_called()

    @mock.patch.object(providers, "chat", return_value=("answer", "", None))
    def test_prompts_are_per_slot(self, chat_mock):
        self.cfg.topic_flow = True
        main.handle_message(self.cfg, self.tg, msg(text="/new alpha"), self.state)
        t1 = self.tg.topics["alpha"]
        main.handle_message(self.cfg, self.tg, msg(text="/new beta"), self.state)
        t2 = self.tg.topics["beta"]
        # open a prompt in t1 (the only other session is t2's)
        main.handle_message(self.cfg, self.tg,
                            msg(text="/session", message_thread_id=t1), self.state)
        main.handle_message(self.cfg, self.tg,
                            msg(text="/session 1", message_thread_id=t1), self.state)
        self.assertIn((100, t1), self.state.prompts)
        # a command in t2 does NOT cancel t1's prompt
        main.handle_message(self.cfg, self.tg,
                            msg(text="/help", message_thread_id=t2), self.state)
        self.assertIn((100, t1), self.state.prompts)
        # nor does a plain message in t2 (answered normally in t2)
        main.handle_message(self.cfg, self.tg,
                            msg(text="over here", message_thread_id=t2), self.state)
        self.assertIn((100, t1), self.state.prompts)
        self.assertTrue(any(s[1].get("markdown") == "answer"
                            for s in self.tg.rich))
        # ...but a message in t1 cancels ITS prompt
        main.handle_message(self.cfg, self.tg,
                            msg(text="back in t1", message_thread_id=t1), self.state)
        self.assertNotIn((100, t1), self.state.prompts)


class TestSessionTitles(unittest.TestCase):
    """P4 group 19: agent-generated session titles after the first message."""

    def setUp(self):
        self.tg = FakeTG()
        self.state = main.State(True)
        self.cfg = make_config()

    @mock.patch.object(providers, "price_for", return_value=None)
    @mock.patch.object(providers, "chat")
    def test_title_after_first_message_then_stops(self, chat_mock, price_mock):
        chat_mock.side_effect = [("the answer", "", None),
                                 ("  Trip Planning!  ", "", None)]
        main.handle_message(self.cfg, self.tg, msg(text="plan my trip"),
                            self.state)
        m = self.state.meta[(100, None)]
        self.assertEqual(m["name"], "Trip Planning")  # punctuation stripped
        self.assertEqual(chat_mock.call_count, 2)
        # second turn: no more naming calls
        main.handle_message(self.cfg, self.tg, msg(text="and hotels"),
                            self.state)
        self.assertEqual(chat_mock.call_count, 3)
        self.assertEqual(self.state.meta[(100, None)]["name"], "Trip Planning")

    @mock.patch.object(providers, "price_for", return_value=None)
    @mock.patch.object(providers, "chat")
    def test_new_with_explicit_name_skips_naming(self, chat_mock, price_mock):
        chat_mock.return_value = ("answer", "", None)
        main.handle_message(self.cfg, self.tg, msg(text="/new my trip"),
                            self.state)
        main.handle_message(self.cfg, self.tg, msg(text="hello"), self.state)
        self.assertEqual(chat_mock.call_count, 1)  # only the answer call
        self.assertEqual(self.state.meta[(100, None)]["name"], "my trip")

    @mock.patch.object(providers, "price_for", return_value=None)
    @mock.patch.object(providers, "chat")
    def test_title_renames_topic_in_flow(self, chat_mock, price_mock):
        self.cfg.topic_flow = True
        chat_mock.side_effect = [("answer", "", None), ("Cats", "", None)]
        main.handle_message(self.cfg, self.tg, msg(text="/new"), self.state)
        tid = self.tg.topics["New session"]
        main.handle_message(self.cfg, self.tg,
                            msg(text="tell me about cats", message_thread_id=tid),
                            self.state)
        self.assertEqual(self.state.meta[(100, tid)]["name"], "Cats")
        self.assertIn((100, tid, "Cats"), self.tg.renamed)  # topic mirrors it

    @mock.patch.object(providers, "price_for", return_value=None)
    @mock.patch.object(providers, "chat")
    def test_media_only_message_named_from_media(self, chat_mock, price_mock):
        chat_mock.side_effect = [("answer", "", None),
                                 ("Sunset Photo", "", None)]
        m = msg(text=None, photo=[{"file_id": "f1", "file_size": 100}])
        main.handle_message(self.cfg, self.tg, m, self.state)
        self.assertEqual(self.state.meta[(100, None)]["name"], "Sunset Photo")
        # the naming call got a media descriptor (vision off in this stub)
        naming_msgs = chat_mock.call_args_list[1][0][3]
        self.assertIn("media file", naming_msgs[1]["content"])

    @mock.patch.object(providers, "price_for")
    @mock.patch.object(providers, "chat")
    def test_media_only_message_with_vision_sends_image(self, chat_mock,
                                                        price_mock):
        price_mock.return_value = {"image": True}
        chat_mock.side_effect = [("answer", "", None), ("Nice Pic", "", None)]
        m = msg(text=None, photo=[{"file_id": "f1", "file_size": 100}])
        main.handle_message(self.cfg, self.tg, m, self.state)
        naming_content = chat_mock.call_args_list[1][0][3][1]["content"]
        self.assertIsInstance(naming_content, list)
        self.assertTrue(any(p.get("type") == "image_url"
                            for p in naming_content))
        self.assertEqual(self.state.meta[(100, None)]["name"], "Nice Pic")


    @mock.patch.object(providers, "price_for", return_value=None)
    @mock.patch.object(providers, "chat")
    def test_title_marker_and_quotes_stripped(self, chat_mock, price_mock):
        """Light hygiene only - word choice is left to the prompt."""
        chat_mock.side_effect = [
            ("answer", "", None),
            ("Title: \"Docker Networking\"", "", None)]
        main.handle_message(self.cfg, self.tg, msg(text="help me deploy"),
                            self.state)
        self.assertEqual(self.state.meta[(100, None)]["name"],
                         "Docker Networking")


class TestConfigPersistence(unittest.TestCase):
    """topic_flow + /model global: surgical config.toml writes (comments kept)."""

    def _cfg(self, d):
        import config as config_mod
        import tomllib
        path = os.path.join(d, "config.toml")
        with open(path, "w", encoding="utf-8") as f:
            f.write('# my comment\nbot_token = "t"\n'
                    'default_model = "zen/glm-5.3-flash"\n\n'
                    "[providers.zen]\napi_key = \"zk\"\n")
        with open(path, "rb") as f:
            return config_mod.Config(tomllib.load(f), path=path), path

    def test_set_flow_persists_and_survives_reload(self):
        import config as config_mod
        import tomllib
        with tempfile.TemporaryDirectory() as d:
            cfg, path = self._cfg(d)
            main._set_flow(cfg, True)
            self.assertTrue(cfg.topic_flow)
            with open(path, encoding="utf-8") as f:
                content = f.read()
            self.assertIn("topic_flow = true", content)
            self.assertIn("# my comment", content)   # comments preserved
            with open(path, "rb") as f:
                reloaded = config_mod.Config(tomllib.load(f), path=path)
            self.assertTrue(reloaded.topic_flow)     # survives a restart

    @mock.patch.object(providers, "chat")
    def test_model_global_writes_config(self, chat_mock):
        import config as config_mod
        import tomllib
        with tempfile.TemporaryDirectory() as d:
            cfg, path = self._cfg(d)
            tg = FakeTG()
            main.handle_message(cfg, tg,
                                msg(text="/model global go/mimo-v2.6-flash"),
                                main.State(True))
            self.assertEqual(cfg.default_model, "go/mimo-v2.6-flash")
            with open(path, encoding="utf-8") as f:
                content = f.read()
            self.assertIn('default_model = "go/mimo-v2.6-flash"', content)
            self.assertIn("# my comment", content)
            self.assertNotIn("zen/glm-5.3-flash", content)
            self.assertIn("written to", tg.sent[-1][1])
            with open(path, "rb") as f:
                reloaded = config_mod.Config(tomllib.load(f), path=path)
            self.assertEqual(reloaded.default_model, "go/mimo-v2.6-flash")

    def test_write_value_inserts_missing_key_at_top_level(self):
        import config as config_mod
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "c.toml")
            with open(path, "w", encoding="utf-8") as f:
                f.write('bot_token = "t"\n\n[providers.zen]\napi_key = "k"\n')
            cfg = config_mod.Config({}, path=path)
            config_mod.write_value(cfg, "topic_flow", True)
            with open(path, encoding="utf-8") as f:
                lines = [l for l in f.read().splitlines() if l.strip()]
            self.assertEqual(lines[1], "topic_flow = true")  # before any section
            self.assertEqual(lines[2], "[providers.zen]")


if __name__ == "__main__":
    unittest.main()
