import os
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
        self.media = []
        self.file_bytes = b"fakeimage"
        self.topics = {}     # name -> thread_id
        self.renamed = []    # (chat_id, thread_id, name)
        self.deleted_topics = []  # (chat_id, thread_id)
        self._next_tid = 500

    def send(self, chat_id, text, thread_id=None):
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

    def send(self, chat_id, text, thread_id=None):
        self.sent.append((chat_id, text, thread_id))
        return {"message_id": len(self.sent)}

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
            "providers": {"zen": {"api_key": "zk"}, "go": {"api_key": "gk"}}}
    base.update(kw)
    return config_mod.Config(base)


def msg(text=None, user_id=42, **extra):
    m = {"chat": {"id": 100, "type": "private"}, "from": {"id": user_id, "username": "tester"},
         "text": text or "", "message_thread_id": None}
    m.update(extra)
    return m


class TestRouter(unittest.TestCase):
    def setUp(self):
        self.tg = FakeTG()
        self.state = main.State(True)
        self.cfg = make_config()

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

    @mock.patch.object(providers, "chat", return_value=("answer **x**", "deep thought"))
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

    @mock.patch.object(providers, "chat", return_value=("answer", "thoughts"))
    def test_ai_flow_rich_thinking_off(self, _):
        main.handle_message(self.cfg, self.tg, msg(text="/thinking off"), self.state)
        self.tg.rich.clear()
        main.handle_message(self.cfg, self.tg, msg(text="hello"), self.state)
        self.assertEqual(len(self.tg.rich), 1)
        self.assertEqual(self.tg.rich[0][1], {"markdown": "answer"})

    @mock.patch.object(providers, "chat", return_value=("answer", ""))
    def test_ai_flow_rich_fallback_on_error(self, _):
        main.handle_message(self.cfg, self.tg, msg(text="/thinking off"), self.state)
        self.tg.rich.clear()
        self.tg.sent.clear()
        with mock.patch.object(FakeTG, "send_rich", side_effect=main.tg_mod.TelegramError("no rich support")):
            main.handle_message(self.cfg, self.tg, msg(text="hello"), self.state)
        self.assertEqual(len(self.tg.sent), 1)  # fell back to regular send
        self.assertEqual(self.tg.sent[0][1], "answer")

    @mock.patch.object(providers, "chat", return_value=("answer", ""))
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

    @mock.patch.object(providers, "chat", return_value=("answer", ""))
    def test_key_fallback_prefers_go(self, chat_mock):
        self.cfg = make_config(providers={"zen": {"api_key": ""}, "go": {"api_key": "gk"}})
        main.handle_message(self.cfg, self.tg, msg(text="/thinking off"), self.state)
        self.tg.rich.clear()
        self.tg.sent.clear()
        main.handle_message(self.cfg, self.tg, msg(text="hello"), self.state)
        # default model is zen/... but zen has no key -> routed via go
        self.assertEqual(chat_mock.call_args[0][0], "go")

    @mock.patch.object(providers, "chat", return_value=("answer", ""))
    def test_no_keys_at_all_errors(self, chat_mock):
        self.cfg = make_config(providers={"zen": {"api_key": ""}, "go": {"api_key": ""}})
        main.handle_message(self.cfg, self.tg, msg(text="hello"), self.state)
        chat_mock.assert_not_called()
        self.assertTrue(any("no API key" in t for _, t, _ in self.tg.sent))

    @mock.patch.object(providers, "chat", return_value=("nice pic", ""))
    def test_photo_goes_to_vision(self, chat_mock):
        m = msg(text=None, photo=[{"file_id": "f1", "file_size": 100}])
        main.handle_message(self.cfg, self.tg, m, self.state)
        sent = chat_mock.call_args[0][3]  # messages arg
        self.assertEqual(sent[-1]["content"][1]["type"], "image_url")
        self.assertTrue(sent[-1]["content"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,"))

    @mock.patch.object(providers, "chat", return_value=("ok", ""))
    def test_media_test_echo(self, _):
        m = msg(text=None, caption="media_test", photo=[{"file_id": "f1", "file_size": 100}])
        main.handle_message(self.cfg, self.tg, m, self.state)
        self.assertEqual(self.tg.media, [(100, "photo", "round-trip test")])

    @mock.patch.object(providers, "chat", return_value=("ok", ""))
    def test_history_is_kept(self, chat_mock):
        main.handle_message(self.cfg, self.tg, msg(text="one"), self.state)
        main.handle_message(self.cfg, self.tg, msg(text="two"), self.state)
        hist = self.state.get_history(100, None)
        self.assertEqual([m2["role"] for m2 in hist], ["user", "assistant", "user", "assistant"])
        self.assertEqual(hist[-2]["content"], "two")  # last user msg
        self.assertEqual(hist[-1]["content"], "ok")   # last assistant msg


class TestSessions(unittest.TestCase):
    def setUp(self):
        self.tg = FakeTG()
        self.state = main.State(True)
        self.cfg = make_config()

    @mock.patch.object(providers, "chat", return_value=("a1", ""))
    def test_new_creates_topic_and_context_isolated(self, chat_mock):
        self.state.topics_enabled = True
        main.handle_message(self.cfg, self.tg, msg(text="/new work stuff"), self.state)
        # a topic was created and a welcome message sent INTO it
        tid = self.tg.topics["work stuff"]
        self.assertEqual(self.tg.sent[-1], (100, "new session <b>work stuff</b> started - type here", tid))

        # chat inside the new topic -> own history, isolated from main chat
        main.handle_message(self.cfg, self.tg, msg(text="hello", message_thread_id=tid), self.state)
        main.handle_message(self.cfg, self.tg, msg(text="main chat"), self.state)
        self.assertEqual([m["content"] for m in self.state.get_history(100, tid)], ["hello", "a1"])
        self.assertEqual([m["content"] for m in self.state.get_history(100, None)], ["main chat", "a1"])

    def test_new_auto_names_sessions(self):
        self.state.topics_enabled = True
        main.handle_message(self.cfg, self.tg, msg(text="/new"), self.state)
        main.handle_message(self.cfg, self.tg, msg(text="/new"), self.state)
        self.assertIn("Session 1", self.tg.topics)
        self.assertIn("Session 2", self.tg.topics)

    def test_new_without_topics_clears_history(self):
        self.state.topics_enabled = False
        self.state.get_history(100, None).append({"role": "user", "content": "old"})
        main.handle_message(self.cfg, self.tg, msg(text="/new"), self.state)
        self.assertEqual(self.state.get_history(100, None), [])
        self.assertIn("topics are not enabled", self.tg.sent[-1][1])
        self.assertEqual(self.tg.topics, {})

    def test_rename_current_topic(self):
        main.handle_message(self.cfg, self.tg, msg(text="/rename project x", message_thread_id=77), self.state)
        self.assertEqual(self.tg.renamed, [(100, 77, "project x")])
        self.assertIn("renamed", self.tg.sent[-1][1])

    def test_rename_in_main_chat_rejected(self):
        main.handle_message(self.cfg, self.tg, msg(text="/rename nope"), self.state)
        self.assertEqual(self.tg.renamed, [])
        self.assertIn("nothing to rename", self.tg.sent[-1][1])

    def test_rename_requires_name(self):
        main.handle_message(self.cfg, self.tg, msg(text="/rename", message_thread_id=77), self.state)
        self.assertEqual(self.tg.renamed, [])
        self.assertIn("usage", self.tg.sent[-1][1])

    def test_new_registers_topic_for_reset(self):
        self.state.topics_enabled = True
        main.handle_message(self.cfg, self.tg, msg(text="/new a"), self.state)
        main.handle_message(self.cfg, self.tg, msg(text="/new b"), self.state)
        self.assertEqual(len(self.state.topics[100]), 2)


class TestResetAll(unittest.TestCase):
    def setUp(self):
        self.tg = FakeTG()
        self.state = main.State(True)
        self.cfg = make_config()
        self.state.topics_enabled = True

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
                     "thinking", "models", "model", "test_md", "test_rich"):
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

    def test_delete_clears_context_keeps_topic(self):
        hist = self.state.get_history(100, 77)
        hist.append({"role": "user", "content": "x"})
        self.state.model[(100, 77)] = "go/m"
        main.handle_message(self.cfg, self.tg, msg(text="/delete", message_thread_id=77), self.state)
        self.assertEqual(self.state.history.get((100, 77)), None)
        self.assertNotIn((100, 77), self.state.model)
        self.assertIn("session deleted", self.tg.sent[-1][1])
        self.assertIn("delete it manually", self.tg.sent[-1][1])
        # topic untouched
        self.assertEqual(self.state.topics, {})

    def test_delete_without_store_ok(self):
        main.handle_message(self.cfg, self.tg, msg(text="/delete"), self.state)
        self.assertIn("session deleted", self.tg.sent[-1][1])


class TestCompact(unittest.TestCase):
    def setUp(self):
        self.tg = FakeTG()
        self.state = main.State(True)
        self.cfg = make_config()

    def _seed(self, n=10, thread=None):
        hist = self.state.get_history(100, thread)
        for i in range(n):
            hist.append({"role": "user" if i % 2 == 0 else "assistant",
                         "content": "message %d " % i + "z" * 100})
        return hist

    @mock.patch.object(providers, "chat", return_value=("THE SUMMARY", ""))
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

    @mock.patch.object(providers, "chat", side_effect=[("SUMMARY", ""), ("final answer", "")])
    def test_auto_compact_on_limit(self, chat_mock):
        self.cfg = make_config(context_limit_chars=100)
        hist = self._seed(8)
        main.handle_message(self.cfg, self.tg, msg(text="continue"), self.state)
        hist = self.state.get_history(100, None)
        self.assertIn("[Summary of earlier conversation]", hist[0]["content"])
        self.assertEqual(hist[-1]["content"], "final answer")
        self.assertEqual(chat_mock.call_count, 2)


class TestStreaming(unittest.TestCase):
    def setUp(self):
        self.tg = FakeTG()
        self.state = main.State(True)

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
        chat_mock.return_value = ("blocking answer", "")
        main.handle_message(self.cfg, self.tg, msg(text="hi"), self.state)
        chat_mock.assert_called_once()
        self.assertEqual(self.tg.rich[-1][1], {"markdown": "blocking answer"})
        hist = self.state.get_history(100, None)
        self.assertEqual([m["role"] for m in hist], ["user", "assistant"])

    @mock.patch.object(providers, "chat")
    @mock.patch.object(providers, "chat_stream")
    def test_stream_disabled_config(self, stream_mock, chat_mock):
        self.cfg = make_config(stream_drafts=False)
        chat_mock.return_value = ("answer", "")
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


if __name__ == "__main__":
    unittest.main()
