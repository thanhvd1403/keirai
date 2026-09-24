"""Parallel web search/fetch via the Search MCP server (streamable HTTP).

The MCP endpoint https://search.parallel.ai/mcp is FREE and anonymous -
it exposes exactly two tools: web_search and web_fetch. An optional API key
(parallel_api_key / PARALLEL_API_KEY) raises the rate limits.

Transport: MCP streamable HTTP = JSON-RPC 2.0 POSTs; the response body is
either plain JSON or text/event-stream (data: lines). Stdlib only.
"""
import json
import urllib.error
import urllib.request

MCP_URL = "https://search.parallel.ai/mcp"
PROTOCOL_VERSION = "2025-06-18"
TIMEOUT = 60


class MCPError(Exception):
    pass


class MCPClient:
    def __init__(self, url=MCP_URL, api_key="", timeout=TIMEOUT):
        self.url = url
        self.api_key = api_key
        self.timeout = timeout

    def _post(self, payload, session=None):
        """One JSON-RPC POST. Returns (message_or_None, session_id)."""
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if session:
            headers["Mcp-Session-Id"] = session
        if self.api_key:
            headers["Authorization"] = "Bearer %s" % self.api_key
        req = urllib.request.Request(self.url, data=json.dumps(payload).encode("utf-8"),
                                     headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                ctype = resp.headers.get("Content-Type", "")
                sess = resp.headers.get("Mcp-Session-Id") or session
                body = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            raise MCPError("MCP HTTP %d: %s" % (e.code, e.read().decode("utf-8", "replace")[:300]))
        except urllib.error.URLError as e:
            raise MCPError("MCP unreachable: %s" % e.reason)
        return _parse_body(body, ctype), sess


def _parse_body(body, ctype):
    """Response body -> last JSON-RPC message (JSON or SSE framed)."""
    if not body or not body.strip():
        return None
    if "event-stream" in (ctype or ""):
        last = None
        for line in body.splitlines():
            if line.startswith("data:"):
                try:
                    last = json.loads(line[5:].strip())
                except ValueError:
                    continue
        return last
    try:
        return json.loads(body)
    except ValueError:
        raise MCPError("unparseable MCP response: %s" % body[:200])


def _text_of(result):
    parts = [b.get("text", "") for b in (result.get("content") or [])
             if isinstance(b, dict) and b.get("type") == "text"]
    return "\n".join(parts)


def call_tool(name, arguments, api_key="", url=MCP_URL, timeout=TIMEOUT):
    """initialize -> notifications/initialized -> tools/call. Returns text."""
    client = MCPClient(url, api_key, timeout)
    _msg, session = client._post({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                   "clientInfo": {"name": "keirai", "version": "0.1"}},
    })
    client._post({"jsonrpc": "2.0", "method": "notifications/initialized"}, session)
    out, _session = client._post({
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }, session)
    if out is None:
        raise MCPError("empty response for tools/call")
    if out.get("error"):
        raise MCPError(str(out["error"].get("message") or out["error"]))
    result = out.get("result") or {}
    text = _text_of(result)
    if result.get("isError"):
        raise MCPError(text or "tool call failed")
    return text


# ---------------------------------------------------------------- tools

def search(cfg, objective, search_queries, session_id=None):
    args = {"objective": objective, "search_queries": search_queries}
    if session_id:
        args["session_id"] = str(session_id)[:100]
    return call_tool("web_search", args, api_key=cfg.parallel_api_key)


def fetch(cfg, urls, objective=None, full_content=False, session_id=None):
    args = {"urls": urls}
    if objective:
        args["objective"] = str(objective)[:200]
    if full_content:
        args["full_content"] = True
    if session_id:
        args["session_id"] = str(session_id)[:100]
    return call_tool("web_fetch", args, api_key=cfg.parallel_api_key)
