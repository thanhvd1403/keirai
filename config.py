"""Keirai configuration: TOML file + environment variable overrides.

Requires Python 3.11+ (tomllib is in the stdlib).
"""
import os

try:
    import tomllib
except ImportError:  # pragma: no cover
    raise SystemExit("Keirai requires Python 3.11+ (tomllib). Found an older Python.")

CONFIG_PATHS = ["config.toml"]
PROVIDER_ENV_KEYS = {
    "zen": ["KEIRAI_ZEN_API_KEY", "OPENCODE_API_KEY"],
    "go": ["KEIRAI_GO_API_KEY", "OPENCODE_API_KEY"],
}


class ConfigError(Exception):
    pass


class Config:
    def __init__(self, data, path=None):
        self.path = path
        self.bot_token = os.environ.get("KEIRAI_BOT_TOKEN") or data.get("bot_token") or ""
        allowed = data.get("allowed_users", "all")
        if isinstance(allowed, str) and allowed.strip().lower() == "all":
            self.allowed_users = "all"
        elif isinstance(allowed, list):
            self.allowed_users = [str(u).lstrip("@").lower() for u in allowed]
        elif isinstance(allowed, (str, int)):  # single ID/username, no list
            self.allowed_users = [str(allowed).lstrip("@").lower()]
        else:
            self.allowed_users = []
        self.thinking_default = bool(data.get("thinking_default", True))
        self.rich_messages = bool(data.get("rich_messages", True))
        self.default_model = data.get("default_model", "zen/glm-5.3-flash")
        self.max_file_mb = int(data.get("max_file_mb", 20))
        self.system_prompt = data.get("system_prompt", "You are Keirai, a lightweight AI agent.")
        providers = data.get("providers", {}) or {}
        self.providers = {}
        for name, env_keys in PROVIDER_ENV_KEYS.items():
            pconf = providers.get(name, {}) or {}
            key = pconf.get("api_key") or ""
            for env in env_keys:
                if os.environ.get(env):
                    key = os.environ[env]
                    break
            self.providers[name] = {"api_key": key}

    def is_allowed(self, user_id, username=None):
        """True if the user may talk to the bot."""
        if self.allowed_users == "all":
            return True
        uid = str(user_id)
        if uid in self.allowed_users:
            return True
        if username and username.lower() in self.allowed_users:
            return True
        return False

    def provider_key(self, name):
        p = self.providers.get(name)
        return p["api_key"] if p else ""

    def validate(self):
        if not self.bot_token:
            raise ConfigError(
                "bot_token is missing. Set it in config.toml or via the KEIRAI_BOT_TOKEN env var."
            )


def load(path=None):
    """Load config from `path`, the KEIRAI_CONFIG env var, or ./config.toml."""
    candidates = ([path] if path
                  else ([os.environ["KEIRAI_CONFIG"]] if os.environ.get("KEIRAI_CONFIG") else [])
                  + CONFIG_PATHS)
    for cand in candidates:
        if cand and os.path.isfile(cand):
            try:
                with open(cand, "rb") as f:
                    return Config(tomllib.load(f), path=cand)
            except tomllib.TOMLDecodeError as e:
                raise ConfigError("%s is not valid TOML: %s" % (cand, e))
    return Config({}, path=None)


def cache_dir(config):
    """Cache directory next to the config file (or ./cache)."""
    base = os.path.dirname(os.path.abspath(config.path)) if config.path else os.getcwd()
    d = os.path.join(base, "cache")
    os.makedirs(d, exist_ok=True)
    return d
