#!/usr/bin/env python3
"""
gemini_proxy.py - local translator: Gemini REST API  ->  OpenAI-compatible API.

The Android app speaks Google's Gemini protocol (streamGenerateContent with
function calling). This script listens on 127.0.0.1 and forwards the same
conversation to ANY OpenAI-compatible server (OpenRouter, Groq, Cerebras,
Mistral, GitHub Models, a local Ollama, ...), translating messages, tool
calls and streaming in both directions. No third-party packages needed.

Run in Termux:
    pkg install python
    python gemini_proxy.py                      # OpenRouter, free models only
    python gemini_proxy.py --upstream https://api.groq.com/openai/v1

In the app, on the login screen:
    API key        = the key of the upstream service (it stays on this phone)
    Server address = http://127.0.0.1:8787

Settings (flags or environment variables):
    --upstream / PROXY_UPSTREAM   base URL of the OpenAI-compatible API
    --port     / PROXY_PORT       default 8787
    --model    / PROXY_MODEL      pin one upstream model id (disables auto mode)
    --all-models / PROXY_ALL=1    do not limit the list to free models
    UPSTREAM_KEY                  use this key instead of the one the app sends

Model "0-auto" (default in the app) tries several suitable models in turn when
one is rate-limited or down. Unknown model names also map to auto mode.
"""
import argparse
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CFG = {
    "upstream": "https://openrouter.ai/api/v1",
    "pin": "",
    "free_only": True,
}
AUTO = "0-auto"
EXCLUDE = re.compile(
    r"embed|whisper|tts|guard|moderation|rerank|dall|flux|diffusion|ocr|audio|image",
    re.I,
)
# Models whose names contain these hints are tried first in auto mode.
PREFERRED = ["qwen3-coder", "gpt-oss", "glm", "kimi", "deepseek", "llama-3.3", "qwen3", "mistral"]

_models_lock = threading.Lock()
_models = {"ts": 0.0, "items": []}  # items: {"id", "alias", "ctx"}


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, file=sys.stderr, flush=True)


class Upstream(Exception):
    def __init__(self, status, msg):
        super().__init__(msg)
        self.status = int(status) if str(status).isdigit() else 502
        self.msg = msg


def extract_msg(raw):
    try:
        j = json.loads(raw)
        e = j.get("error", j)
        if isinstance(e, dict):
            m = e.get("message") or json.dumps(e, ensure_ascii=False)
            meta = e.get("metadata") or {}
            raw_m = meta.get("raw")
            return f"{m} ({raw_m})" if raw_m else str(m)
        return str(e)
    except Exception:
        return raw.decode("utf-8", "replace")[:500] if isinstance(raw, bytes) else str(raw)[:500]


# ---------------------------------------------------------------- models

def fetch_models(key):
    req = urllib.request.Request(CFG["upstream"] + "/models")
    if key:
        req.add_header("Authorization", "Bearer " + key)
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read())
    items = []
    for m in data.get("data", []):
        mid = m.get("id", "")
        if not mid or EXCLUDE.search(mid):
            continue
        sp = m.get("supported_parameters")
        if sp is not None and "tools" not in sp:
            continue
        pr = m.get("pricing")
        if CFG["free_only"] and isinstance(pr, dict):
            try:
                if float(pr.get("prompt", 1)) != 0 or float(pr.get("completion", 1)) != 0:
                    continue
            except (TypeError, ValueError):
                continue
        ctx = m.get("context_length") or 32768
        items.append({"id": mid, "alias": alias_of(mid), "ctx": int(ctx)})

    def score(it):
        low = it["id"].lower()
        for i, h in enumerate(PREFERRED):
            if h in low:
                return (i, -it["ctx"])
        return (len(PREFERRED), -it["ctx"])

    items.sort(key=score)
    return items


def alias_of(mid):
    return mid.replace("/", "_").replace(":", "-")


def get_models(key):
    if CFG["pin"]:
        return [{"id": CFG["pin"], "alias": alias_of(CFG["pin"]), "ctx": 32768}]
    with _models_lock:
        if time.time() - _models["ts"] < 600 and _models["items"]:
            return _models["items"]
    try:
        items = fetch_models(key)
    except Exception as e:  # network/auth problem: keep the old list if any
        log("model list failed:", e)
        items = _models["items"]
    with _models_lock:
        if items:
            _models["items"], _models["ts"] = items, time.time()
    return items


def candidates(name, key):
    items = get_models(key)
    if CFG["pin"]:
        return [CFG["pin"]]
    for it in items:
        if it["alias"] == name:
            return [it["id"]]
    return [it["id"] for it in items[:5]]  # auto mode / unknown name


# ------------------------------------------------- Gemini -> OpenAI request

def schema_fix(s):
    if isinstance(s, dict):
        return {k: (v.lower() if k == "type" and isinstance(v, str) else schema_fix(v))
                for k, v in s.items()}
    if isinstance(s, list):
        return [schema_fix(x) for x in s]
    return s


def to_openai(body, model_id):
    msgs = []
    si = body.get("systemInstruction") or body.get("system_instruction")
    if si:
        t = "".join(p.get("text", "") for p in si.get("parts", []))
        if t.strip():
            msgs.append({"role": "system", "content": t})
    pending = []  # (name, call_id) of the last assistant turn
    n = 0
    for turn in body.get("contents", []):
        parts = turn.get("parts", [])
        if turn.get("role") == "model":
            text = "".join(p.get("text", "") for p in parts if "text" in p and not p.get("thought"))
            calls = []
            for p in parts:
                fc = p.get("functionCall")
                if fc:
                    n += 1
                    calls.append({
                        "id": f"call_{n}", "type": "function",
                        "function": {"name": fc.get("name", ""),
                                     "arguments": json.dumps(fc.get("args") or {}, ensure_ascii=False)},
                    })
            pending = [(c["function"]["name"], c["id"]) for c in calls]
            if text or calls:
                m = {"role": "assistant", "content": text}
                if calls:
                    m["tool_calls"] = calls
                msgs.append(m)
            continue
        content = []
        for p in parts:
            if "functionResponse" in p:
                fr = p["functionResponse"]
                name = fr.get("name", "")
                payload = json.dumps(fr.get("response"), ensure_ascii=False)
                idx = next((i for i, (nm, _) in enumerate(pending) if nm == name), None)
                if idx is None:
                    msgs.append({"role": "user", "content": f"[tool result {name}] {payload}"})
                else:
                    _, cid = pending.pop(idx)
                    msgs.append({"role": "tool", "tool_call_id": cid, "content": payload})
            elif "inlineData" in p:
                d = p["inlineData"]
                mime = d.get("mimeType", "")
                if mime.startswith("image/"):
                    content.append({"type": "image_url",
                                    "image_url": {"url": f"data:{mime};base64,{d.get('data', '')}"}})
                else:
                    content.append({"type": "text", "text": f"[attachment {mime} omitted]"})
            elif "text" in p:
                content.append({"type": "text", "text": p["text"]})
        if content:
            if all(c["type"] == "text" for c in content):
                msgs.append({"role": "user", "content": "".join(c["text"] for c in content)})
            else:
                msgs.append({"role": "user", "content": content})
    payload = {"model": model_id, "messages": msgs, "stream": True,
               "stream_options": {"include_usage": True}}
    tools = []
    for t in body.get("tools", []):
        for d in t.get("functionDeclarations", []):
            tools.append({"type": "function", "function": {
                "name": d.get("name", ""), "description": d.get("description", ""),
                "parameters": schema_fix(d.get("parameters") or {"type": "object", "properties": {}}),
            }})
    if tools:
        payload["tools"] = tools
    return payload


# ------------------------------------------------- OpenAI stream -> Gemini

def open_upstream(payload, key):
    req = urllib.request.Request(
        CFG["upstream"] + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json", "Accept": "text/event-stream",
                 "Authorization": "Bearer " + key, "X-Title": "Gemini Android proxy"},
    )
    try:
        return urllib.request.urlopen(req, timeout=120)
    except urllib.error.HTTPError as e:
        raise Upstream(e.code, extract_msg(e.read()))
    except Exception as e:
        raise Upstream(502, f"нет связи с {CFG['upstream']}: {e}")


def sse_events(resp):
    with resp:
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            d = line[5:].strip()
            if d == "[DONE]":
                break
            try:
                yield json.loads(d)
            except ValueError:
                continue


def convert(events):
    acc = {}
    usage = None
    produced = False
    for ev in events:
        if ev.get("error"):
            e = ev["error"]
            raise Upstream(e.get("code") if isinstance(e, dict) else 502,
                           e.get("message", "ошибка провайдера") if isinstance(e, dict) else str(e))
        u = ev.get("usage")
        if u:
            usage = u.get("total_tokens") or (u.get("prompt_tokens", 0) + u.get("completion_tokens", 0))
        for ch in ev.get("choices") or []:
            d = ch.get("delta") or {}
            t = d.get("content")
            if t:
                produced = True
                yield {"candidates": [{"content": {"role": "model", "parts": [{"text": t}]}}]}
            for tc in d.get("tool_calls") or []:
                a = acc.setdefault(tc.get("index", 0), {"name": "", "args": ""})
                f = tc.get("function") or {}
                if f.get("name") and not a["name"]:
                    a["name"] = f["name"]
                if f.get("arguments"):
                    a["args"] += f["arguments"]
    parts = []
    for i in sorted(acc):
        a = acc[i]
        if not a["name"]:
            continue
        try:
            args = json.loads(a["args"]) if a["args"].strip() else {}
        except ValueError:
            args = {}
        if not isinstance(args, dict):
            args = {"value": args}
        parts.append({"functionCall": {"name": a["name"], "args": args}})
    if parts:
        produced = True
        yield {"candidates": [{"content": {"role": "model", "parts": parts}}]}
    if not produced:
        yield {"candidates": [{"content": {"role": "model",
                                           "parts": [{"text": "(модель вернула пустой ответ, повтори запрос)"}]}}]}
    if usage:
        yield {"usageMetadata": {"totalTokenCount": int(usage)}}


def start_stream(model_name, body, key):
    """Try candidate models until one starts answering. Returns (model_id, first, rest)."""
    last = Upstream(502, "нет подходящих моделей (проверь ключ и адрес)")
    for mid in candidates(model_name, key):
        try:
            gen = convert(sse_events(open_upstream(to_openai(body, mid), key)))
            first = next(gen)
            log("model", mid, "ok")
            return mid, first, gen
        except Upstream as e:
            log("model", mid, "failed:", e.status, e.msg[:160])
            last = e
            if e.status in (401, 402, 403):
                break
        except StopIteration:
            last = Upstream(502, "пустой ответ провайдера")
    raise last


# ------------------------------------------------------------------ server

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _key(self, q):
        return os.environ.get("UPSTREAM_KEY") or (q.get("key", [""])[0]) or self.headers.get("x-goog-api-key", "")

    def _json(self, code, obj):
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _err(self, code, msg):
        self._json(code, {"error": {"code": code, "message": msg, "status": "ERROR"}})

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        if u.path.rstrip("/") == "/v1beta/models":
            items = get_models(self._key(q))
            ctx = min([i["ctx"] for i in items[:5]] or [32768])
            out = [{"name": "models/" + AUTO, "supportedGenerationMethods": ["generateContent"],
                    "inputTokenLimit": ctx}] if items and not CFG["pin"] else []
            out += [{"name": "models/" + i["alias"], "supportedGenerationMethods": ["generateContent"],
                     "inputTokenLimit": i["ctx"]} for i in items]
            return self._json(200, {"models": out})
        self._json(200, {"ok": True, "upstream": CFG["upstream"]})

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        m = re.match(r"^/v1beta/models/(.+):(streamGenerateContent|generateContent|predict)$",
                     urllib.parse.unquote(u.path))
        if not m:
            return self._err(404, "неизвестный адрес: " + u.path)
        name, action = m.group(1), m.group(2)
        if action == "predict":
            return self._err(501, "Генерация картинок через этот прокси не поддерживается.")
        key = self._key(q)
        if not key:
            return self._err(401, "не передан ключ API")
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            mid, first, gen = start_stream(name, body, key)
        except Upstream as e:
            hint = " Лимит провайдера исчерпан, подожди или смени модель/провайдера." if e.status == 429 else ""
            return self._err(e.status, e.msg + hint)
        except Exception as e:
            return self._err(400, f"ошибка запроса: {e}")

        if action == "generateContent":
            parts, usage = [], None
            for ch in [first, *gen]:
                for c in ch.get("candidates", []):
                    parts += c["content"]["parts"]
                usage = ch.get("usageMetadata", usage)
            text = "".join(p["text"] for p in parts if "text" in p)
            merged = ([{"text": text}] if text else []) + [p for p in parts if "functionCall" in p]
            out = {"candidates": [{"content": {"role": "model", "parts": merged}, "finishReason": "STOP"}]}
            if usage:
                out["usageMetadata"] = usage
            return self._json(200, out)

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        try:
            for ch in _chain(first, gen):
                self.wfile.write(b"data: " + json.dumps(ch, ensure_ascii=False).encode("utf-8") + b"\r\n\r\n")
                self.wfile.flush()
        except Upstream as e:  # failed in the middle of the answer
            err = {"candidates": [{"content": {"role": "model",
                                               "parts": [{"text": f"\n[ошибка провайдера: {e.msg}]"}]}}]}
            self.wfile.write(b"data: " + json.dumps(err, ensure_ascii=False).encode("utf-8") + b"\r\n\r\n")
        except (BrokenPipeError, ConnectionResetError):
            pass


def _chain(first, rest):
    yield first
    yield from rest


def main():
    ap = argparse.ArgumentParser(description="Gemini API -> OpenAI-compatible translator")
    ap.add_argument("--upstream", default=os.environ.get("PROXY_UPSTREAM", CFG["upstream"]))
    ap.add_argument("--port", type=int, default=int(os.environ.get("PROXY_PORT", "8787")))
    ap.add_argument("--model", default=os.environ.get("PROXY_MODEL", ""))
    ap.add_argument("--all-models", action="store_true", default=os.environ.get("PROXY_ALL") == "1")
    a = ap.parse_args()
    CFG["upstream"] = a.upstream.rstrip("/")
    CFG["pin"] = a.model
    CFG["free_only"] = not a.all_models
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    log(f"listening on http://127.0.0.1:{a.port}  ->  {CFG['upstream']}"
        f"  (free only: {CFG['free_only']}, pinned: {CFG['pin'] or '-'})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
