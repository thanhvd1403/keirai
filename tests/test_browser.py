"""Tests for the Lightpanda browser tool (binary + subprocess mocked)."""
import os
import subprocess
import tempfile
import unittest
from unittest import mock

import browser
import config as config_mod
import tools


def make_config():
    return config_mod.Config({"bot_token": "t"})


class TestFindBinary(unittest.TestCase):
    def test_not_installed(self):
        with mock.patch.object(browser.config_mod, "base_dir",
                               return_value=tempfile.gettempdir()), \
             mock.patch.object(browser.shutil, "which", return_value=None), \
             mock.patch.object(browser.os.path, "isfile", return_value=False):
            self.assertIsNone(browser.find_binary(make_config()))

    def test_project_binary(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "tools")
            os.makedirs(path)
            binary = os.path.join(path, "lightpanda")
            with open(binary, "w") as f:
                f.write("#!/bin/sh\n")
            os.chmod(binary, 0o755)
            with mock.patch.object(browser.config_mod, "base_dir", return_value=td):
                self.assertEqual(browser.find_binary(make_config()), binary)

    def test_on_path(self):
        with mock.patch.object(browser.config_mod, "base_dir",
                               return_value=tempfile.gettempdir()), \
             mock.patch.object(browser.os.path, "isfile", return_value=False), \
             mock.patch.object(browser.shutil, "which",
                               return_value="/usr/bin/lightpanda"):
            self.assertEqual(browser.find_binary(make_config()),
                             "/usr/bin/lightpanda")


class TestBrowse(unittest.TestCase):
    def test_no_binary_raises(self):
        with mock.patch.object(browser, "find_binary", return_value=None):
            with self.assertRaises(ValueError) as cm:
                browser.browse(make_config(), "https://example.com")
        self.assertIn("install_lightpanda.sh", str(cm.exception))

    def test_success_passes_flags(self):
        with mock.patch.object(browser, "find_binary",
                               return_value="/x/lightpanda"), \
             mock.patch.object(browser.subprocess, "run") as run_mock:
            run_mock.return_value = subprocess.CompletedProcess(
                [], 0, stdout="# Page\n\nhello", stderr="")
            out = browser.browse(make_config(), "https://example.com",
                                 wait_ms=2500)
        self.assertEqual(out, "# Page\n\nhello")
        cmd = run_mock.call_args[0][0]
        self.assertEqual(cmd[0], "/x/lightpanda")
        self.assertIn("fetch", cmd)
        self.assertIn("--dump", cmd)
        self.assertIn("markdown", cmd)
        self.assertIn("2500", cmd)
        self.assertIn("https://example.com", cmd)
        self.assertEqual(run_mock.call_args[1]["env"]["LIGHTPANDA_DISABLE_TELEMETRY"],
                         "true")

    def test_failure_includes_stderr(self):
        with mock.patch.object(browser, "find_binary",
                               return_value="/x/lightpanda"), \
             mock.patch.object(browser.subprocess, "run") as run_mock:
            run_mock.return_value = subprocess.CompletedProcess(
                [], 2, stdout="", stderr="bad url")
            with self.assertRaises(ValueError) as cm:
                browser.browse(make_config(), "not-a-url")
        self.assertIn("bad url", str(cm.exception))

    def test_tool_wraps_errors(self):
        cfg = make_config()
        with mock.patch.object(browser, "find_binary",
                               return_value="/x/lightpanda"), \
             mock.patch.object(browser.subprocess, "run",
                               side_effect=ValueError("nope")):
            out = tools.execute(cfg, "browse", {"url": "https://x.example"})
        self.assertTrue(out.startswith("error:"))


if __name__ == "__main__":
    unittest.main()
