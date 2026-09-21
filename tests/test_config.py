import os
import unittest
from unittest import mock

import config as config_mod


class TestConfig(unittest.TestCase):
    def test_defaults_allow_all(self):
        cfg = config_mod.Config({})
        self.assertEqual(cfg.allowed_users, "all")
        self.assertTrue(cfg.is_allowed(123, "anyone"))
        self.assertFalse(cfg.thinking_default is None)

    def test_allowed_by_id(self):
        cfg = config_mod.Config({"allowed_users": [123456789, "@someone", "Admin"]})
        self.assertTrue(cfg.is_allowed(123456789))
        self.assertTrue(cfg.is_allowed(999, "someone"))
        self.assertTrue(cfg.is_allowed(1, "admin"))
        self.assertFalse(cfg.is_allowed(999, "intruder"))
        self.assertFalse(cfg.is_allowed(999))

    def test_validate_missing_token(self):
        cfg = config_mod.Config({})
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(config_mod.ConfigError):
                cfg.validate()

    def test_env_token_override(self):
        with mock.patch.dict(os.environ, {"KEIRAI_BOT_TOKEN": "env-token"}):
            cfg = config_mod.Config({"bot_token": "file-token"})
            self.assertEqual(cfg.bot_token, "env-token")

    def test_provider_env_keys(self):
        with mock.patch.dict(os.environ, {"KEIRAI_ZEN_API_KEY": "z1", "KEIRAI_GO_API_KEY": "g1"}, clear=False):
            cfg = config_mod.Config({"providers": {"zen": {"api_key": "file"}}})
            self.assertEqual(cfg.provider_key("zen"), "z1")
            self.assertEqual(cfg.provider_key("go"), "g1")

    def test_provider_file_key(self):
        cfg = config_mod.Config({"providers": {"zen": {"api_key": "file-key"}}})
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("KEIRAI_ZEN_API_KEY", None)
            os.environ.pop("OPENCODE_API_KEY", None)
            self.assertEqual(cfg.provider_key("zen"), "file-key")

    def test_load_from_file(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "config.toml")
            with open(p, "w", encoding="utf-8") as f:
                f.write('bot_token = "tok"\nallowed_users = [1, "@someone"]\n')
            with mock.patch.dict(os.environ, {"KEIRAI_CONFIG": p}, clear=False):
                cfg = config_mod.load()
            self.assertEqual(cfg.bot_token, "tok")
            self.assertEqual(cfg.allowed_users, ["1", "someone"])

    def test_invalid_toml_raises_friendly(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "config.toml")
            with open(p, "w", encoding="utf-8") as f:
                f.write("this is not = = toml {{{")
            with mock.patch.dict(os.environ, {"KEIRAI_CONFIG": p}, clear=False):
                with self.assertRaises(config_mod.ConfigError):
                    config_mod.load()

    def test_multiline_system_prompt(self):
        import tempfile
        toml = 'system_prompt = """line one\nline two"""\n'
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "config.toml")
            with open(p, "w", encoding="utf-8") as f:
                f.write(toml)
            with mock.patch.dict(os.environ, {"KEIRAI_CONFIG": p}, clear=False):
                cfg = config_mod.load()
            self.assertEqual(cfg.system_prompt, "line one\nline two")


if __name__ == "__main__":
    unittest.main()
