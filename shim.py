#!/usr/bin/env python3
"""Codex web_search shim for Antigravity Tools.

Codex sends the hosted tool {"type": "web_search"} in /v1/responses requests.
Antigravity cannot serve it: Google's v1internal API rejects googleSearch when
function tools are also present, and Codex always sends function tools.

This proxy sits between Codex and Antigravity and:
  * replaces the hosted tool with a plain function web_search(query);
  * when the model calls it, runs a separate search-only Gemini request
    (tools = [googleSearch]) against Antigravity, streams a standard
    web_search_call item to Codex, feeds the results back to the model and
    continues the same response;
  * also offers web_run(query | open_url), executed against OpenAI's
    standalone search endpoint (/alpha/search) with the ChatGPT credentials
    Codex already sends, so Gemini can use OpenAI's search index too;
  * rewrites web_search_call items in later history back into
    function_call + function_call_output using a persistent result cache.

Requests are routed by model: models served by Antigravity go to --upstream,
everything else (GPT models) goes unchanged to --openai-upstream, so one Codex
provider can offer both. Requests without a web_search tool are proxied
unchanged. Standard library only.
"""

import argparse
import http.client
import json
import logging
import os
import re
import threading
import time
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger("codex-search-shim")

HOP_HEADERS = {
    "host", "content-length", "connection", "keep-alive", "transfer-encoding",
    "accept-encoding", "proxy-connection", "upgrade", "te", "trailer",
}
SEARCH_TOOL_TYPES = {"web_search", "web_search_preview", "web_search_2025_08_26",
                     "web_search_preview_2025_03_11"}
SEARCH_FN = "web_search"
SEARCH_FN_TOOL = {
    "type": "function",
    "name": SEARCH_FN,
    "description": (
        "Search the web with Google and return a summary of current results "
        "with source links. Use it for recent information, documentation, "
        "release notes, error messages, or anything that may have changed "
        "after your training data."
    ),
    "parameters": {
        "type": "object",
        "properties": {"query": {"type": "string", "description": "Search query."}},
        "required": ["query"],
    },
}
WEB_RUN_FN = "web_run"
WEB_RUN_FN_TOOL = {
    "type": "function",
    "name": WEB_RUN_FN,
    "description": (
        "OpenAI web search. Pass `query` to get ranked results (title, URL, "
        "snippet) quickly, or pass `open_url` to fetch and read a specific web "
        "page. Prefer this for fast lookups and for reading pages found by a search."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query."},
            "open_url": {"type": "string", "description": "URL of a page to open and read."},
        },
    },
}
SEARCH_FNS = {SEARCH_FN, WEB_RUN_FN}
CLIENT_CALL_TYPES = {"function_call", "custom_tool_call", "local_shell_call",
                     "computer_call", "mcp_call", "tool_search_call"}


class Config:
    upstream = "http://127.0.0.1:8045"
    # Models not served by Antigravity go here unchanged (Codex ChatGPT-auth backend).
    openai_upstream = "https://chatgpt.com/backend-api/codex"
    search_model = "gemini-3.8-flash-high"
    max_rounds = 8
    web_run = True
    google_for_web_run = False
    google_for_openai = True
    google_wait = 45
    timeout = 600
    connect_timeout = 15
    cache_file = os.path.expanduser("~/.cache/codex-search-shim/results.json")
    cache_limit = 1000


class ResultCache:
    """id/call_id/query -> search result text, persisted as JSON."""

    def __init__(self, path, limit):
        self.path, self.limit = path, limit
        self.lock = threading.Lock()
        self.data = {}
        try:
            with open(path, encoding="utf-8") as f:
                self.data = json.load(f)
        except (OSError, ValueError):
            pass

    def get(self, *keys):
        with self.lock:
            for k in keys:
                if k and k in self.data:
                    return self.data[k]["result"]
        return None

    def put(self, keys, result):
        now = time.time()
        with self.lock:
            for k in keys:
                if k:
                    self.data[k] = {"result": result, "t": now}
            if len(self.data) > self.limit:
                for k, _ in sorted(self.data.items(), key=lambda kv: kv[1]["t"])[: len(self.data) - self.limit]:
                    del self.data[k]
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False)
            os.replace(tmp, self.path)


CACHE: ResultCache = None


def upstream_conn(base_url=None):
    """Connected client for base_url; connecting is bounded by connect_timeout."""
    u = urllib.parse.urlsplit(base_url or Config.upstream)
    cls = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
    conn = cls(u.hostname, u.port or (443 if u.scheme == "https" else 80), timeout=Config.connect_timeout)
    conn.connect()
    conn.sock.settimeout(Config.timeout)
    return conn, u.path.rstrip("/")


class AntigravityModels:
    """Live model list of Antigravity, refreshed every few minutes."""
    ttl = 300
    lock = threading.Lock()
    models, fetched = set(), 0.0

    @classmethod
    def get(cls, auth_header):
        with cls.lock:
            if time.time() - cls.fetched < cls.ttl and cls.models:
                return cls.models
            conn = None
            try:
                conn, base = upstream_conn()
                conn.request("GET", base + "/v1/models", headers={"Authorization": auth_header} if auth_header else {})
                resp = conn.getresponse()
                if resp.status == 200:
                    cls.models = {m["id"] for m in json.loads(resp.read()).get("data") or []}
                    cls.fetched = time.time()
            except Exception as e:
                log.warning("cannot list Antigravity models: %s", e)
            finally:
                if conn:
                    conn.close()
            return cls.models


def is_antigravity_model(model, auth_header):
    if not model:
        return False
    if model in AntigravityModels.get(auth_header):
        return True
    return model.startswith(("gemini-", "claude-", "gpt-oss-"))


def iter_sse(resp):
    """Yield (event, data) tuples; data is None for comment/keepalive lines."""
    event, data = None, []
    while True:
        line = resp.readline()
        if not line:
            if data:
                yield event, "\n".join(data)
            return
        line = line.decode("utf-8", "replace").rstrip("\r\n")
        if line == "":
            if data:
                yield event, "\n".join(data)
            event, data = None, []
        elif line.startswith(":"):
            yield None, None
        elif line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].lstrip(" "))


# ---------------------------------------------------------------- search

def run_search(query, auth_header):
    """Search-only Gemini request through Antigravity; returns (text, sources)."""
    prompt = (
        "Use Google Search to research the query below and report what you find. "
        "Give concrete facts (versions, dates, numbers, names, commands, error fixes) "
        "and say which source each fact comes from. Answer in the language of the query.\n\n"
        f"Query: {query}"
    )
    body = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "tools": [{"googleSearch": {}}],
    }
    conn, base = upstream_conn()
    path = f"{base}/v1beta/models/{Config.search_model}:streamGenerateContent?alt=sse"
    headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
    if auth_header:
        headers["Authorization"] = auth_header
    texts, sources, queries = [], [], []
    try:
        conn.request("POST", path, json.dumps(body), headers)
        resp = conn.getresponse()
        if resp.status != 200:
            raise RuntimeError(f"HTTP {resp.status}: {resp.read()[:300].decode('utf-8', 'replace')}")
        for _, data in iter_sse(resp):
            if not data:
                continue
            try:
                chunk = json.loads(data)
            except ValueError:
                continue
            chunk = chunk.get("response", chunk)
            for cand in chunk.get("candidates") or []:
                for part in (cand.get("content") or {}).get("parts") or []:
                    if part.get("text") and not part.get("thought"):
                        texts.append(part["text"])
                gm = cand.get("groundingMetadata") or {}
                queries.extend(q for q in gm.get("webSearchQueries") or [] if q not in queries)
                for gc in gm.get("groundingChunks") or []:
                    web = gc.get("web") or {}
                    if web.get("uri") and all(s["url"] != web["uri"] for s in sources):
                        sources.append({"type": "url", "url": web["uri"], "title": web.get("title") or ""})
    finally:
        conn.close()
    text = "".join(texts).strip() or "(no summary returned)"
    out = [f"Google web search results (via Antigravity) for: {query}"]
    if queries:
        out.append("Google queries: " + "; ".join(queries))
    out += ["", text]
    if sources:
        out += ["", "Sources:"] + [f"{i}. {s['title']} - {s['url']}" for i, s in enumerate(sources, 1)]
    return "\n".join(out), sources


GUI_CONFIG = os.path.expanduser("~/.antigravity_tools/gui_config.json")


def antigravity_auth():
    """Authorization header for Antigravity Tools' own API key."""
    key = os.environ.get("ANTIGRAVITY_API_KEY")
    if not key:
        try:
            with open(GUI_CONFIG, encoding="utf-8") as f:
                key = json.load(f)["proxy"]["api_key"]
        except (OSError, ValueError, KeyError):
            return None
    return "Bearer " + key


def merge_google_results(data, google):
    """Append Google results [(query, text, sources)] to an /alpha/search response."""
    data = dict(data)
    parts = [data.get("output") or ""]
    results = list(data.get("results") or [])
    for n, (query, text, sources) in enumerate(google):
        parts.append("=" * 80 + f"\nGoogle search (via Antigravity) for: {query}\n\n{text}")
        for i, s in enumerate(sources):
            results.append({"type": "text_result", "domain": urllib.parse.urlsplit(s["url"]).netloc,
                            "ref_id": f"google{n}_{i}", "title": s.get("title") or "",
                            "url": s["url"], "snippet": ""})
    data["output"] = "\n\n".join(p for p in parts if p)
    data["results"] = results
    return data


OPENAI_AUTH_HEADERS = ("authorization", "chatgpt-account-id", "originator", "user-agent")


def has_openai_auth(headers):
    keys = {k.lower() for k in headers}
    return "authorization" in keys and "chatgpt-account-id" in keys


def run_web_run(args, headers, model, user_text):
    """OpenAI standalone search (/alpha/search) with the client's ChatGPT credentials.

    Returns (text, sources, action) where action is the web_search_call action.
    """
    query = (args.get("query") or "").strip()
    url = (args.get("open_url") or "").strip()
    if url:
        commands, action = {"open": [{"ref_id": url}]}, {"type": "open_page", "url": url}
    elif query:
        commands, action = {"search_query": [{"q": query}]}, {"type": "search", "query": query}
    else:
        raise ValueError("web_run needs `query` or `open_url`")
    commands["response_length"] = "medium"
    body = {
        "id": str(uuid.uuid4()),
        "model": model,
        "input": [{"type": "message", "role": "user",
                   "content": [{"type": "input_text", "text": user_text or query or url}]}],
        "commands": commands,
        "settings": {"allowed_callers": ["direct"], "external_web_access": True},
        "max_output_tokens": 10000,
    }
    fwd = {k: v for k, v in headers.items() if k.lower() in OPENAI_AUTH_HEADERS}
    fwd["Content-Type"] = "application/json"
    conn, base = upstream_conn(Config.openai_upstream)
    try:
        conn.request("POST", base + "/alpha/search", json.dumps(body, ensure_ascii=False).encode(), fwd)
        resp = conn.getresponse()
        raw = resp.read()
        if resp.status != 200:
            raise RuntimeError(f"HTTP {resp.status}: {raw[:300].decode('utf-8', 'replace')}")
        data = json.loads(raw)
    finally:
        conn.close()
    sources = [{"type": "url", "url": r["url"], "title": r.get("title") or ""}
               for r in data.get("results") or [] if isinstance(r, dict) and r.get("url")]
    # Strip private-use citation markers that only render inside ChatGPT.
    text = re.sub(r"[\ue000-\uf8ff]", "", data.get("output") or "").strip() or "(no results)"
    head = f"OpenAI web search results for: {query}" if query else f"Contents of {url}"
    return head + "\n\n" + text, sources, action


def last_user_text(items):
    if not isinstance(items, list):
        return items if isinstance(items, str) else ""
    for it in reversed(items):
        if isinstance(it, dict) and it.get("type") == "message" and it.get("role") == "user":
            for c in it.get("content") or []:
                if isinstance(c, dict) and c.get("type") == "input_text" and not c.get("text", "").startswith("<"):
                    return c["text"]
    return ""


# ---------------------------------------------------------------- request rewriting

def tool_names(body):
    """Flat list of tool names, including code-mode tools nested in additional_tools."""
    out = []

    def walk(tools, prefix=""):
        for t in tools or []:
            if not isinstance(t, dict):
                continue
            name = t.get("name") or t.get("type")
            if t.get("tools"):
                walk(t["tools"], prefix + name + ".")
            else:
                out.append(prefix + str(name))

    walk(body.get("tools"))
    items = body.get("input") if isinstance(body.get("input"), list) else []
    for it in items:
        if isinstance(it, dict) and it.get("type") == "additional_tools":
            walk(it.get("tools"), "additional.")
    return out


def has_search_tool(body):
    return any(isinstance(t, dict) and t.get("type") in SEARCH_TOOL_TYPES for t in body.get("tools") or [])


def rewrite_tools(tools, with_web_run=False, add_search=False):
    out, added = [], False
    if add_search:
        out.append(SEARCH_FN_TOOL)
        added = True
    for t in tools:
        if isinstance(t, dict) and t.get("type") in SEARCH_TOOL_TYPES:
            if not added:
                out.append(SEARCH_FN_TOOL)
                if with_web_run:
                    out.append(WEB_RUN_FN_TOOL)
                added = True
        elif isinstance(t, dict) and t.get("type") == "function" and t.get("name") in SEARCH_FNS:
            continue
        else:
            out.append(t)
    return out


def rewrite_input(items, only_ours=False):
    """Turn web_search_call history items back into function_call + output.

    only_ours: leave items we did not create (e.g. OpenAI's own) untouched.
    """
    if not isinstance(items, list):
        return items
    out = []
    for it in items:
        if (isinstance(it, dict) and it.get("type") == "web_search_call"
                and not (only_ours and CACHE.get(it.get("id")) is None)):
            action = it.get("action") or {}
            query = (action.get("query") or "; ".join(action.get("queries") or [])
                     or (("open " + action["url"]) if action.get("url") else ""))
            cid = it.get("id") or ("ws_" + uuid.uuid4().hex[:24])
            result = CACHE.get(cid, "q:" + query) or (
                f"Web search for '{query}' was performed earlier; its results are no longer available. "
                "Search again if you still need them.")
            out.append({"type": "function_call", "call_id": cid, "name": SEARCH_FN,
                        "arguments": json.dumps({"query": query}, ensure_ascii=False)})
            out.append({"type": "function_call_output", "call_id": cid, "output": result})
        else:
            out.append(it)
    return out


def is_search_call(item):
    return isinstance(item, dict) and item.get("type") == "function_call" and item.get("name") in SEARCH_FNS


# ---------------------------------------------------------------- core loop

class UpstreamError(Exception):
    def __init__(self, status, body):
        super().__init__(f"upstream HTTP {status}")
        self.status, self.body = status, body


def search_loop(body, headers, emit, openai=False):
    """Run model rounds, executing web_search / web_run calls in between.

    openai=False: the model is served by Antigravity; hosted web_search is
    replaced by web_search (Google) + web_run (OpenAI standalone search).
    openai=True: the model is served by OpenAI and keeps its native web_run;
    web_search (Google) is added as an extra function tool.

    emit(event_dict) receives client-facing Responses stream events.
    Returns the final response object.
    """
    auth = antigravity_auth() or headers.get("Authorization")
    user_text = last_user_text(body.get("input"))
    cur_input = rewrite_input(body.get("input"), only_ours=openai)
    if isinstance(cur_input, str):
        cur_input = [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": cur_input}]}]
    with_web_run = not openai and Config.web_run and has_openai_auth(headers)
    req = dict(body, tools=rewrite_tools(body.get("tools") or [], with_web_run, add_search=openai), stream=True)
    if openai:
        upstream, resp_path = Config.openai_upstream, "/responses"
    else:
        upstream, resp_path = Config.upstream, "/v1/responses"
    meta, out_items, idx, usage = None, [], 0, None

    def finish(resp, status="completed"):
        final = dict(meta or resp or {})
        final.update({"status": status, "output": out_items, "completed_at": int(time.time())})
        if resp:
            for k in ("usage", "incomplete_details", "error"):
                if k in resp:
                    final[k] = resp[k]
        emit({"type": "response." + status if status != "completed" else "response.completed", "response": final})
        return final

    for rnd in range(Config.max_rounds):
        req["input"] = cur_input
        try:
            conn, base = upstream_conn(upstream)
        except OSError as e:
            raise UpstreamError(502, f"cannot connect to upstream {upstream}: {e}".encode())
        fwd = {k: v for k, v in headers.items()
               if k.lower() not in HOP_HEADERS and k.lower() not in ("content-type", "accept")}
        fwd["Content-Type"] = "application/json"
        fwd["Accept"] = "text/event-stream"
        try:
            conn.request("POST", base + resp_path, json.dumps(req, ensure_ascii=False).encode(), fwd)
            resp = conn.getresponse()
            if resp.status != 200:
                raise UpstreamError(resp.status, resp.read())
            round_items, searches, client_calls, idxmap, final_resp = [], [], False, {}, None
            for _, data in iter_sse(resp):
                if data is None:
                    continue
                try:
                    ev = json.loads(data)
                except ValueError:
                    continue
                t = ev.get("type", "")
                if t in ("response.created", "response.in_progress"):
                    if meta is None:
                        meta = ev.get("response") or {}
                        emit(ev)
                    continue
                if t == "response.completed":
                    final_resp = ev.get("response") or {}
                    break
                if t in ("response.failed", "response.incomplete", "error"):
                    if t == "error":
                        emit(ev)
                        return None
                    return finish(ev.get("response"), status=t.split(".", 1)[1])
                oi = ev.get("output_index")
                if t == "response.output_item.added":
                    if is_search_call(ev.get("item")):
                        idxmap[oi] = None
                        continue
                    idxmap[oi] = idx
                    idx += 1
                if oi is not None:
                    mapped = idxmap.get(oi, oi)
                    if mapped is None:
                        if t == "response.output_item.done":
                            searches.append(ev["item"])
                            round_items.append(ev["item"])
                        continue
                    ev["output_index"] = mapped
                if t == "response.output_item.done":
                    item = ev.get("item") or {}
                    round_items.append(item)
                    out_items.append(item)
                    if item.get("type") in CLIENT_CALL_TYPES:
                        client_calls = True
                emit(ev)
        finally:
            conn.close()

        if final_resp is None:
            raise UpstreamError(502, b"upstream stream ended without response.completed")
        usage = final_resp.get("usage") or usage
        if not searches:
            return finish(final_resp)

        outputs = []
        for call in searches:
            try:
                args = json.loads(call.get("arguments") or "{}") or {}
            except ValueError:
                args = {"query": call.get("arguments") or ""}
            name = call.get("name")
            query = args.get("query") or ""
            if name == WEB_RUN_FN and args.get("open_url") and not query:
                action = {"type": "open_page", "url": args["open_url"]}
                key = "open " + args["open_url"]
            else:
                action, key = {"type": "search", "query": query}, query
            ws_id = "ws_" + uuid.uuid4().hex[:24]
            item = {"id": ws_id, "type": "web_search_call", "status": "in_progress", "action": action}
            emit({"type": "response.output_item.added", "output_index": idx, "item": item})
            emit({"type": "response.web_search_call.in_progress", "output_index": idx, "item_id": ws_id})
            emit({"type": "response.web_search_call.searching", "output_index": idx, "item_id": ws_id})
            t0 = time.time()
            try:
                if name == WEB_RUN_FN:
                    result, sources, _ = run_web_run(args, headers, body.get("model"), user_text)
                else:
                    result, sources = run_search(query, auth)
                status = "completed"
            except Exception as e:  # report to the model instead of failing the turn
                log.warning("%s failed for %r: %s", name, key, e)
                result, sources, status = f"{name} for '{key}' failed: {e}", [], "failed"
            log.info("%s %r -> %d chars, %d sources, %.1fs", name, key, len(result), len(sources), time.time() - t0)
            CACHE.put([ws_id, call.get("call_id"), "q:" + key], result)
            done = dict(item, status=status)
            if sources:
                done["action"] = dict(done["action"], sources=sources)
            emit({"type": "response.web_search_call.completed", "output_index": idx, "item_id": ws_id})
            emit({"type": "response.output_item.done", "output_index": idx, "item": done})
            out_items.append(done)
            idx += 1
            outputs.append({"type": "function_call_output", "call_id": call.get("call_id"), "output": result})

        if client_calls:
            # Codex must run its own tools first; the next request carries
            # our web_search_call items, which rewrite_input() restores.
            return finish(final_resp)
        cur_input = list(cur_input) + round_items + outputs

    log.warning("max rounds (%d) reached", Config.max_rounds)
    return finish({"usage": usage} if usage else None)


# ---------------------------------------------------------------- HTTP server

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "codex-search-shim"

    def log_message(self, fmt, *args):
        log.debug("%s " + fmt, self.address_string(), *args)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def do_GET(self):
        self._proxy(b"", to_openai=True)

    def do_DELETE(self):
        self._proxy(self._body(), to_openai=True)

    def do_POST(self):
        raw = self._body()
        body = None
        if not self.headers.get("Content-Encoding"):
            try:
                body = json.loads(raw)
            except ValueError:
                pass
        model = body.get("model") if isinstance(body, dict) else None
        log.info("POST %s model=%s encoding=%s", self.path, model, self.headers.get("Content-Encoding"))
        if isinstance(body, dict) and log.isEnabledFor(logging.DEBUG):
            log.debug("tools: %s web__run=%s", tool_names(body), b"web__run" in raw)
        path = urllib.parse.urlsplit(self.path).path
        if path.rstrip("/").endswith("/alpha/search"):
            # Standalone search is executed by Codex itself, so any model may use it.
            queries = [c.get("q") for c in ((body or {}).get("commands") or {}).get("search_query") or []
                       if isinstance(c, dict) and c.get("q")] if isinstance(body, dict) else []
            if Config.google_for_web_run and queries:
                return self._alpha_search(raw, queries)
            return self._proxy(raw, to_openai=True)
        if not is_antigravity_model(model, self.headers.get("Authorization")):
            log.debug("-> openai %s model=%s", self.path, model)
            if Config.google_for_openai and isinstance(body, dict) and path.rstrip("/").endswith("/responses"):
                return self._search(body, openai=True)
            return self._proxy(raw, to_openai=True)
        log.debug("-> antigravity %s model=%s", self.path, model)
        if path.rstrip("/").endswith("/responses") and has_search_tool(body):
            return self._search(body)
        self._proxy(raw)

    # ---- plain passthrough
    def _proxy(self, raw, to_openai=False):
        path = self.path
        if to_openai and path.startswith("/v1/"):
            path = path[3:]
        try:
            conn, base = upstream_conn(Config.openai_upstream if to_openai else None)
        except OSError as e:
            log.warning("cannot connect to upstream: %s", e)
            return self._json_error(502, f"shim cannot connect to upstream: {e}")
        fwd = {k: v for k, v in self.headers.items() if k.lower() not in HOP_HEADERS}
        try:
            conn.request(self.command, base + path, raw or None, fwd)
            resp = conn.getresponse()
            self.send_response(resp.status)
            for k, v in resp.getheaders():
                if k.lower() not in HOP_HEADERS:
                    self.send_header(k, v)
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            while True:
                chunk = resp.read1(65536) if hasattr(resp, "read1") else resp.read(65536)
                if not chunk:
                    break
                self._chunk(chunk)
            self._chunk(b"")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            log.exception("proxy error")
            self._json_error(502, f"shim proxy error: {e}")
        finally:
            conn.close()

    def _alpha_search(self, raw, queries):
        """OpenAI standalone search plus a parallel Google search, merged into one response."""
        google, threads = [None] * len(queries), []

        def google_one(i, q):
            t0 = time.time()
            try:
                text, sources = run_search(q, antigravity_auth())
                google[i] = (q, text, sources)
                log.info("google for web_run %r -> %d chars, %d sources, %.1fs",
                         q, len(text), len(sources), time.time() - t0)
            except Exception as e:
                log.warning("google for web_run %r failed: %s", q, e)

        for i, q in enumerate(queries):
            th = threading.Thread(target=google_one, args=(i, q), daemon=True)
            th.start()
            threads.append(th)

        fwd = {k: v for k, v in self.headers.items() if k.lower() not in HOP_HEADERS}
        status, data, payload, conn = 502, None, b"", None
        try:
            conn, base = upstream_conn(Config.openai_upstream)
            conn.request("POST", base + "/alpha/search", raw, fwd)
            resp = conn.getresponse()
            status, payload = resp.status, resp.read()
            if status == 200:
                data = json.loads(payload)
        except Exception as e:
            log.warning("openai alpha/search failed: %s", e)
        finally:
            if conn:
                conn.close()

        for th in threads:
            th.join(Config.google_wait)
        found = [g for g in google if g]
        if data is None and not found:
            return self._json_error(status, "alpha/search failed", payload or None)
        merged = merge_google_results(data or {}, found)
        out = json.dumps(merged, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def _chunk(self, data):
        self.wfile.write(b"%x\r\n%s\r\n" % (len(data), data))
        self.wfile.flush()

    def _json_error(self, status, msg, raw=None):
        try:
            payload = raw or json.dumps({"error": {"message": msg, "type": "shim_error"}}).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except Exception:
            pass

    # ---- web_search path
    def _search(self, body, openai=False):
        stream = bool(body.get("stream"))
        headers = dict(self.headers.items())
        started = {"v": False}
        seq = {"n": 0}
        lock = threading.Lock()

        def start_stream():
            if not started["v"]:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                started["v"] = True

        def emit(ev):
            if not stream:
                return
            with lock:
                start_stream()
                ev["sequence_number"] = seq["n"]
                seq["n"] += 1
                self._chunk(f"event: {ev['type']}\ndata: {json.dumps(ev, ensure_ascii=False)}\n\n".encode())

        try:
            final = search_loop(body, headers, emit, openai=openai)
            if stream:
                start_stream()
                self._chunk(b"")
            else:
                payload = json.dumps(final or {}, ensure_ascii=False).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            log.info("client disconnected")
        except UpstreamError as e:
            log.warning("upstream error %s: %s", e.status, e.body[:300])
            if started["v"]:
                emit({"type": "error", "code": str(e.status), "message": e.body.decode("utf-8", "replace")[:2000]})
                self._chunk(b"")
            else:
                self._json_error(e.status, "", raw=e.body)
        except Exception as e:
            log.exception("search loop error")
            if started["v"]:
                emit({"type": "error", "code": "shim_error", "message": str(e)})
                self._chunk(b"")
            else:
                self._json_error(502, f"shim error: {e}")


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--listen", default="127.0.0.1:8047")
    p.add_argument("--upstream", default=Config.upstream, help="Antigravity Tools base URL")
    p.add_argument("--openai-upstream", default=Config.openai_upstream,
                   help="where non-Antigravity models are forwarded")
    p.add_argument("--search-model", default=Config.search_model)
    p.add_argument("--max-rounds", type=int, default=Config.max_rounds)
    p.add_argument("--cache-file", default=Config.cache_file)
    p.add_argument("--no-web-run", action="store_true",
                   help="don't offer Antigravity models the OpenAI-backed web_run tool")
    p.add_argument("--google-for-web-run", action="store_true",
                   help="also merge Google results into OpenAI standalone search (slower web_run)")
    p.add_argument("--no-google-for-openai", action="store_true",
                   help="don't offer OpenAI models the Google-backed web_search tool")
    p.add_argument("-v", "--verbose", action="store_true")
    a = p.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    Config.upstream = a.upstream.rstrip("/")
    Config.openai_upstream = a.openai_upstream.rstrip("/")
    Config.search_model = a.search_model
    Config.max_rounds = a.max_rounds
    Config.web_run = not a.no_web_run
    Config.google_for_web_run = a.google_for_web_run
    Config.google_for_openai = not a.no_google_for_openai
    global CACHE
    CACHE = ResultCache(a.cache_file, Config.cache_limit)
    host, port = a.listen.rsplit(":", 1)
    srv = ThreadingHTTPServer((host, int(port)), Handler)
    srv.daemon_threads = True
    log.info("listening on %s, upstream %s, search model %s", a.listen, Config.upstream, Config.search_model)
    srv.serve_forever()


if __name__ == "__main__":
    main()
