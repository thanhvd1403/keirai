"""OpenCode Zen / OpenCode Go providers (OpenAI-compatible chat endpoints).

- Zen:  https://opencode.ai/zen/v1
- Go:   https://opencode.ai/zen/go/v1
Both expose GET /models (model list + metadata) and POST /chat/completions.
Model stats (context window, costs) are fetched from /models and cached on disk.
"""
import json
import os
import time
import urllib.error
import urllib.request

PROVIDER_BASES = {
    "zen": "https://opencode.ai/zen/v1",
    "go": "https://opencode.ai/zen/go/v1",
}
CACHE_TTL = 24 * 60 * 60  # refresh stats daily
CHAT_TIMEOUT = 300


class ProviderError(Exception):
    pass


# ---------------------------------------------------------------- http

def _request(url, api_key, payload=None, timeout=30, session_id=None):
    headers = {"Authorization": "Bearer %s" % api_key, "User-Agent": "keirai/0.1"}
    if session_id:
        # OpenCode Go requires a stable per-conversation session id
        # (https://opencode.ai/docs/go/#where-can-i-use-it)
        headers["x-opencode-session"] = session_id
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode("utf-8")
    else:
        data = None
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        raise ProviderError("HTTP %d from %s: %s" % (e.code, url, body[:300]))
    except urllib.error.URLError as e:
        raise ProviderError("%s: %s" % (url, e.reason))


# ---------------------------------------------------------------- models

def fetch_models(name, api_key):
    """GET {base}/models, normalized. Raises ProviderError on failure."""
    base = PROVIDER_BASES.get(name)
    if not base:
        raise ProviderError("unknown provider: %s" % name)
    data = _request(base + "/models", api_key)
    return normalize_models(data)


def normalize_models(data):
    """Best-effort normalization of the /models response.

    Accepts {"data": [...]}, {"models": [...]}, or {id: {...}} dicts.
    Returns [{id, name, context, max_output, cost_in, cost_out}].
    """
    if isinstance(data, dict):
        items = data.get("data") or data.get("models") or []
        if not items and not any(k in data for k in ("data", "models")):
            items = [{"id": k, **(v if isinstance(v, dict) else {})} for k, v in data.items()]
    elif isinstance(data, list):
        items = data
    else:
        items = []
    out = []
    for item in items:
        if not isinstance(item, dict):
            continue
        mid = item.get("id") or item.get("model") or item.get("slug")
        if not mid:
            continue
        limit = item.get("limit") or {}
        cost = item.get("cost") or item.get("pricing") or item.get("price") or {}
        out.append({
            "id": mid,
            "name": item.get("name") or item.get("display_name") or mid,
            "context": _first_int(item, limit, ("context_window", "context_length", "context", "max_context")),
            "max_output": _first_int(item, limit, ("max_output", "output", "max_tokens")),
            "cost_in": _first_num(item, cost, ("input", "in", "prompt", "input_cost")),
            "cost_out": _first_num(item, cost, ("output", "out", "completion", "output_cost")),
        })
    out.sort(key=lambda m: m["id"])
    return out


def _first_int(item, nested, keys):
    for k in keys:
        v = item.get(k)
        if isinstance(v, (int, float)):
            return int(v)
    for k in keys:
        v = nested.get(k) if isinstance(nested, dict) else None
        if isinstance(v, (int, float)):
            return int(v)
    return None


def _first_num(item, nested, keys):
    for k in keys:
        v = item.get(k)
        if isinstance(v, (int, float)):
            return float(v)
    for k in keys:
        v = nested.get(k) if isinstance(nested, dict) else None
        if isinstance(v, (int, float)):
            return float(v)
    return None


# ---------------------------------------------------------------- cache

def cached_models(name, api_key, cache_dir, force_refresh=False):
    """Return (models, from_cache). Falls back to cache when the API is unreachable."""
    path = os.path.join(cache_dir, "models_%s.json" % name)
    if not force_refresh and os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                blob = json.load(f)
            if time.time() - blob.get("fetched_at", 0) < CACHE_TTL:
                return blob.get("models", []), True
        except (ValueError, OSError):
            pass
    try:
        models = fetch_models(name, api_key)
    except ProviderError:
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                blob = json.load(f)
            return blob.get("models", []), True
        raise
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"fetched_at": time.time(), "models": models}, f)
    except OSError:
        pass
    return models, False


# ---------------------------------------------------------------- chat

def chat(name, api_key, model, messages, extra=None, session_id=None):
    """One chat completion. Returns (content, reasoning).

    session_id: stable per-conversation id, required by OpenCode Go
    (sent as x-opencode-session header).
    """
    base = PROVIDER_BASES.get(name)
    if not base:
        raise ProviderError("unknown provider: %s" % name)
    payload = {"model": model, "messages": messages}
    if extra:
        payload.update(extra)
    resp = _request(base + "/chat/completions", api_key, payload=payload,
                    timeout=CHAT_TIMEOUT, session_id=session_id)
    try:
        msg = resp["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        raise ProviderError("unexpected chat response: %s" % json.dumps(resp)[:300])
    content = msg.get("content") or ""
    reasoning = msg.get("reasoning_content") or msg.get("reasoning") or ""
    if isinstance(reasoning, dict):
        reasoning = reasoning.get("content") or ""
    return content, reasoning


def image_part(mime, data_b64):
    return {"type": "image_url", "image_url": {"url": "data:%s;base64,%s" % (mime, data_b64)}}
