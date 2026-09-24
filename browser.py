"""Lightpanda headless browser (optional tool).

License: AGPLv3 - same as Keirai, fully compatible. Lightpanda is a separate
process (never linked), started on demand and stopped after each call.

Install: bash deploy/install_lightpanda.sh   -> <project>/tools/lightpanda
The browse tool only appears to the model when the binary is found there
(or on PATH as `lightpanda`).

We drive it via `lightpanda fetch --dump markdown`: it launches the browser,
renders the page (JS executes) and prints markdown - no CDP/WebSocket needed.
"""
import os
import shutil
import subprocess

import config as config_mod

FETCH_TIMEOUT = 60


def find_binary(cfg):
    """Path to the lightpanda binary, or None when not installed."""
    path = os.path.join(config_mod.base_dir(cfg), "tools", "lightpanda")
    if os.path.isfile(path) and os.access(path, os.X_OK):
        return path
    return shutil.which("lightpanda")


def browse(cfg, url, wait_ms=1500, timeout=FETCH_TIMEOUT):
    """Render a page and return its markdown. Raises ValueError on failure."""
    binary = find_binary(cfg)
    if not binary:
        raise ValueError("lightpanda is not installed - run deploy/install_lightpanda.sh")
    cmd = [binary, "fetch", "--obey-robots", "--dump", "markdown",
           "--wait-ms", str(int(wait_ms)),
           "--log-format", "pretty", "--log-level", "error", url]
    env = dict(os.environ)
    env["LIGHTPANDA_DISABLE_TELEMETRY"] = "true"
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout,
                              text=True, errors="replace", env=env)
    except subprocess.TimeoutExpired:
        raise ValueError("lightpanda timed out after %ds fetching %s" % (timeout, url))
    if proc.returncode != 0:
        raise ValueError("lightpanda failed (exit %d): %s"
                         % (proc.returncode, (proc.stderr or "").strip()[:500]))
    return proc.stdout
