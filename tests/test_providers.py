import json
import os
import tempfile
import unittest
from unittest import mock

import providers


class TestNormalize(unittest.TestCase):
    def test_openai_style(self):
        data = {"data": [
            {"id": "glm-5.3-flash", "name": "GLM 5.3 Flash", "context_window": 1000000,
             "max_output": 65536, "cost": {"input": 0.15, "output": 0.5}},
            {"id": "aaa", "context": 4096},
        ]}
        models = providers.normalize_models(data)
        self.assertEqual([m["id"] for m in models], ["aaa", "glm-5.3-flash"])
        glm = models[1]
        self.assertEqual(glm["context"], 1000000)
        self.assertEqual(glm["cost_in"], 0.15)
        self.assertEqual(glm["cost_out"], 0.5)

    def test_models_dev_style(self):
        data = {"models": [{"id": "m1", "limit": {"context": 200000, "output": 8192},
                            "pricing": {"input": 1.0, "output": 4.0}}]}
        m = providers.normalize_models(data)[0]
        self.assertEqual(m["context"], 200000)
        self.assertEqual(m["max_output"], 8192)
        self.assertEqual(m["cost_in"], 1.0)
        self.assertEqual(m["cost_out"], 4.0)

    def test_dict_keyed(self):
        data = {"glm-5": {"name": "GLM 5", "context_length": 131072}}
        m = providers.normalize_models(data)[0]
        self.assertEqual(m["id"], "glm-5")
        self.assertEqual(m["context"], 131072)

    def test_garbage(self):
        self.assertEqual(providers.normalize_models("nope"), [])


class TestChatParsing(unittest.TestCase):
    def _chat(self, message, **kw):
        resp = {"choices": [{"message": message}]}
        with mock.patch.object(providers, "_request", return_value=resp) as req:
            out = providers.chat("zen", "key", "m", [{"role": "user", "content": "hi"}], **kw)
        req.assert_called_once()
        args = req.call_args
        self.assertIn("/chat/completions", args[0][0])
        self.assertEqual(args[1]["payload"]["model"], "m")
        return out, args

    def test_session_header_passed(self):
        _, args = self._chat({"content": "a"}, session_id="keirai-100-77")
        self.assertEqual(args[1]["session_id"], "keirai-100-77")

    def test_reasoning_content(self):
        (content, reasoning, tcs), _ = self._chat({"content": "answer", "reasoning_content": "thinking..."})
        self.assertEqual((content, reasoning, tcs), ("answer", "thinking...", None))

    def test_reasoning_dict(self):
        (content, reasoning, _tcs), _ = self._chat({"content": "a", "reasoning": {"content": "r"}})
        self.assertEqual(reasoning, "r")

    def test_no_reasoning(self):
        (content, reasoning, _tcs), _ = self._chat({"content": "a"})
        self.assertEqual(reasoning, "")


class TestCache(unittest.TestCase):
    def test_fetch_writes_cache_and_fallback(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(providers, "fetch_models", return_value=[{"id": "m1"}]) as fm:
                models, from_cache = providers.cached_models("zen", "k", d)
            self.assertFalse(from_cache)
            self.assertEqual(models[0]["id"], "m1")
            self.assertTrue(os.path.isfile(os.path.join(d, "models_zen.json")))

            # API dies -> cached copy served
            with mock.patch.object(providers, "fetch_models", side_effect=providers.ProviderError("down")):
                models, from_cache = providers.cached_models("zen", "k", d)
            self.assertTrue(from_cache)
            self.assertEqual(models[0]["id"], "m1")

    def test_no_key_no_cache_raises(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(providers, "fetch_models", side_effect=providers.ProviderError("down")):
                with self.assertRaises(providers.ProviderError):
                    providers.cached_models("zen", "k", d)

    def test_unknown_provider(self):
        with self.assertRaises(providers.ProviderError):
            providers.fetch_models("nope", "k")


class TestMultipart(unittest.TestCase):
    def test_body_structure(self):
        import telegram as tg_mod
        with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as f:
            f.write(b"\x89PNG fake bytes")
            path = f.name
        try:
            boundary, body = tg_mod._multipart(
                {"chat_id": "1", "caption": "hi"}, "photo", "pic.png", path, "image/png")
            self.assertIn(b'form-data; name="chat_id"', body)
            self.assertIn(b'form-data; name="photo"; filename="pic.png"', body)
            self.assertIn(b"\x89PNG fake bytes", body)
            self.assertTrue(body.endswith(("--%s--\r\n" % boundary).encode()))
        finally:
            os.remove(path)

    def test_size_guard(self):
        import telegram as tg_mod
        with mock.patch.object(os.path, "getsize", return_value=tg_mod.MAX_UPLOAD + 1):
            t = tg_mod.Telegram("t")
            with self.assertRaises(tg_mod.TelegramError):
                t.send_media(1, "document", __file__)


if __name__ == "__main__":
    unittest.main()
