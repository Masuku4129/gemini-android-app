#!/usr/bin/env python3
"""Self-test for tools/gemini_proxy.py against a fake OpenAI-compatible server."""
import json, os, sys, threading, urllib.request, urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
import gemini_proxy as P

SEEN = []          # payloads received by the fake upstream
FAIL = {"qwen/qwen3-coder-big:free"}   # models that answer 429

class Fake(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        models = {"data": [
            {"id": "qwen/qwen3-coder-big:free", "context_length": 999999, "pricing": {"prompt": "0", "completion": "0"},
             "supported_parameters": ["tools"]},
            {"id": "qwen/qwen3-coder:free", "context_length": 100000, "pricing": {"prompt": "0", "completion": "0"},
             "supported_parameters": ["tools", "temperature"]},
            {"id": "paid/model", "context_length": 5, "pricing": {"prompt": "0.1", "completion": "0.1"},
             "supported_parameters": ["tools"]},
            {"id": "free/no-tools:free", "context_length": 5, "pricing": {"prompt": "0", "completion": "0"},
             "supported_parameters": ["temperature"]},
            {"id": "x/embed-1:free", "pricing": {"prompt": "0", "completion": "0"}, "supported_parameters": ["tools"]},
        ]}
        raw = json.dumps(models).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw)
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        SEEN.append((self.headers.get("Authorization"), body))
        if body["model"] in FAIL:
            raw = json.dumps({"error": {"message": "rate limited", "code": 429}}).encode()
            self.send_response(429); self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw); return
        self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.end_headers()
        last = body["messages"][-1]
        if last["role"] == "tool":
            chunks = [{"choices": [{"delta": {"content": "Готово: "}}]}, {"choices": [{"delta": {"content": "файл создан"}}]},
                      {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}]
        else:
            chunks = [{"choices": [{"delta": {"content": "Создаю."}}]},
                      {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "x", "function": {"name": "write_file", "arguments": "{\"path\": \"a."}}]}}]},
                      {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "txt\", \"content\": \"hi\"}"}}]}}]},
                      {"choices": [], "usage": {"total_tokens": 42}}]
        for c in chunks:
            self.wfile.write(b": PROCESSING\n\n" + b"data: " + json.dumps(c).encode() + b"\n\n")
        self.wfile.write(b"data: [DONE]\n\n")

def serve(h, port):
    s = ThreadingHTTPServer(("127.0.0.1", port), h); threading.Thread(target=s.serve_forever, daemon=True).start(); return s

def call(port, method, path, body=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method,
                                 data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req) as r: return r.status, r.read().decode()
    except urllib.error.HTTPError as e: return e.code, e.read().decode()

def sse(text): return [json.loads(l[5:]) for l in text.splitlines() if l.startswith("data:")]

serve(Fake, 18001)
P.CFG["upstream"] = "http://127.0.0.1:18001"
serve(P.Handler, 18002)

# 1. model list: only free + tools + not excluded, auto first
st, t = call(18002, "GET", "/v1beta/models?key=K")
names = [m["name"] for m in json.loads(t)["models"]]
assert st == 200 and names[0] == "models/0-auto", names
assert "models/qwen_qwen3-coder-free" in names and "models/qwen_qwen3-coder-big-free" in names, names
assert not any("paid" in n or "no-tools" in n or "embed" in n for n in names), names
print("models ok:", names)

# 2. first request: system + tools; auto must skip the 429 model and land on qwen
body = {"systemInstruction": {"parts": [{"text": "SYS"}]},
        "contents": [{"role": "user", "parts": [{"text": "сделай файл"}]}],
        "tools": [{"functionDeclarations": [{"name": "write_file", "description": "d",
                  "parameters": {"type": "OBJECT", "properties": {"path": {"type": "STRING"}}}}]}]}
st, t = call(18002, "POST", "/v1beta/models/0-auto:streamGenerateContent?alt=sse&key=SECRET", body)
ch = sse(t); assert st == 200
text = "".join(p["text"] for c in ch for cd in c.get("candidates", []) for p in cd["content"]["parts"] if "text" in p)
fcs = [p["functionCall"] for c in ch for cd in c.get("candidates", []) for p in cd["content"]["parts"] if "functionCall" in p]
assert text == "Создаю." and fcs == [{"name": "write_file", "args": {"path": "a.txt", "content": "hi"}}], (text, fcs)
assert ch[-1]["usageMetadata"]["totalTokenCount"] == 42
auth, sent = SEEN[-1]
assert auth == "Bearer SECRET" and sent["model"] == "qwen/qwen3-coder:free", (auth, sent["model"])
assert [m["role"] for m in sent["messages"]] == ["system", "user"]
assert sent["tools"][0]["function"]["parameters"]["type"] == "object"          # lowercased
assert sent["tools"][0]["function"]["parameters"]["properties"]["path"]["type"] == "string"
assert any(s[1]["model"] == "qwen/qwen3-coder-big:free" for s in SEEN)                    # fallback happened
print("stream + tool call + fallback ok")

# 3. tool result round trip
body["contents"] += [
    {"role": "model", "parts": [{"text": "Создаю."}, {"functionCall": {"name": "write_file", "args": {"path": "a.txt"}}}]},
    {"role": "user", "parts": [{"functionResponse": {"name": "write_file", "response": {"ok": True, "output": "saved"}}}]}]
st, t = call(18002, "POST", "/v1beta/models/qwen_qwen3-coder-free:streamGenerateContent?key=K", body)
text = "".join(p["text"] for c in sse(t) for cd in c.get("candidates", []) for p in cd["content"]["parts"] if "text" in p)
assert text == "Готово: файл создан", text
msgs = SEEN[-1][1]["messages"]
assert [m["role"] for m in msgs] == ["system", "user", "assistant", "tool"], [m["role"] for m in msgs]
assert msgs[2]["tool_calls"][0]["id"] == msgs[3]["tool_call_id"] and msgs[3]["tool_call_id"] == "call_1"
print("tool round trip ok")

# 4. explicitly chosen rate-limited model -> clean Gemini-style error, no fallback
st, t = call(18002, "POST", "/v1beta/models/qwen_qwen3-coder-big-free:streamGenerateContent?key=K", body)
e = json.loads(t)["error"]; assert st == 429 and "rate limited" in e["message"], (st, t)
print("error passthrough ok")

# 5. image input and no key
body2 = {"contents": [{"role": "user", "parts": [{"inlineData": {"mimeType": "image/png", "data": "AAA"}}, {"text": "что тут?"}]}]}
call(18002, "POST", "/v1beta/models/0-auto:streamGenerateContent?key=K", body2)
c = SEEN[-1][1]["messages"][-1]["content"]
assert c[0]["type"] == "image_url" and c[0]["image_url"]["url"] == "data:image/png;base64,AAA" and c[1]["text"] == "что тут?"
st, _ = call(18002, "POST", "/v1beta/models/0-auto:streamGenerateContent", body2); assert st == 401
st, _ = call(18002, "POST", "/v1beta/models/imagen-3.0:predict?key=K", {}); assert st == 501
print("image + auth + predict ok")
print("ALL PASSED")
