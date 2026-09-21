import os
import tempfile
import unittest
from unittest import mock

import main
import providers


class FakeTG:
    def __init__(self):
        self.sent = []
        self.media = []
        self.file_bytes = b"fakeimage"

    def send(self, chat_id, text, thread_id=None):
        self.sent.append((chat_id, text))
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


def make_config(**kw):
    import config as config_mod
    base = {"bot_token": "t", "allowed_users": [42],
            "providers": {"zen": {"api_key": "zk"}, "go": {"api_key": "gk"}}}
    base.update(kw)
    return config_mod.Config(base)


def msg(text=None, user_id=42, **extra):
    m = {"chat": {"id": 100}, "from": {"id": user_id, "username": "tester"},
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
    def test_ai_flow_with_thinking(self, _):
        m = msg(text="hello agent")
        main.handle_message(self.cfg, self.tg, m, self.state)
        kinds = [t for _, t in self.tg.sent]
        self.assertTrue(kinds[0].startswith("<blockquote expandable>"))
        self.assertIn("deep thought", kinds[0])
        self.assertIn("answer <b>x</b>", kinds[-1])

    @mock.patch.object(providers, "chat", return_value=("plain answer", "hidden reasoning"))
    def test_ai_flow_thinking_off(self, _):
        main.handle_message(self.cfg, self.tg, msg(text="/thinking off"), self.state)
        self.tg.sent.clear()
        main.handle_message(self.cfg, self.tg, msg(text="hello"), self.state)
        self.assertEqual(len(self.tg.sent), 1)
        self.assertNotIn("blockquote", self.tg.sent[0][1])

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


if __name__ == "__main__":
    unittest.main()
