"""A real server with controlled non-determinism.

Not a mock: the agent talks to it over real sockets, so capture at the HTTP
boundary is genuinely exercised.
"""
import json
import random
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CRLF = bytes((13, 10))
STATE = {"calls": 0, "flaky_seen": set(), "drift": 0}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _raw(self, code, raw, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._route(b"")

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        self._route(self.rfile.read(n))

    def _route(self, body):
        p = self.path.split("?")[0]
        STATE["calls"] += 1

        if p == "/chat":
            # temperature > 0: same request, different output
            self._send(200, {"text": f"answer-{random.randint(1, 10**9)}"})
        elif p == "/chat-stable":
            # realistic model latency — a real call takes hundreds of ms
            time.sleep(random.uniform(0.18, 0.55))
            self._send(200, {"text": "a stable answer"})
        elif p == "/drift":
            # the provider changes behaviour without saying so
            STATE["drift"] += 1
            self._send(200, {"text": f"v{1 if STATE['drift'] < 3 else 2}"})
        elif p == "/search":
            time.sleep(random.uniform(0.005, 0.09))   # unpredictable ordering
            self._send(200, {"hits": json.loads(body or b'{"q":"?"}').get("q")})
        elif p == "/flaky":
            k = body.decode()[:20]
            if k not in STATE["flaky_seen"]:
                STATE["flaky_seen"].add(k)
                self._send(503, {"error": "transient"})
            else:
                self._send(200, {"ok": True})
        elif p == "/ratelimit":
            self._send(429 if STATE["calls"] % 3 == 1 else 200, {"rl": True})
        elif p == "/send-email":
            time.sleep(0.06)
            # REAL effect: count how many times it was actually hit
            STATE.setdefault("emails", 0)
            STATE["emails"] += 1
            self._send(200, {"sent": STATE["emails"]})
        elif p == "/v1/chat/completions":
            # An OpenAI-shaped endpoint, so the execution model has a real
            # inference request to classify rather than a hand-built dict. The
            # answer is fixed: these tests are about metadata, and a random
            # answer would make every one of them a flake.
            req = json.loads(body or b"{}")
            if req.get("n_tools"):
                # tool_calls come back from this same URL, which is how OpenAI
                # does it. `arguments` is a JSON *string*, and that difference
                # is the whole reason the extractor has two paths.
                calls = []
                for i in range(int(req["n_tools"])):
                    calls.append({
                        "id": "call_%d" % i,
                        "type": "function",
                        "function": {
                            "name": ["lookup_order", "send_email",
                                     "web_search"][i % 3],
                            "arguments": json.dumps({
                                "order_id": 4471 + i,
                                "api_key": "sk-INSIDE-ARGS"}),
                        },
                    })
                if req.get("broken_args"):
                    calls[0]["function"]["arguments"] = "{not valid json"
                return self._send(200, {
                    "id": "chatcmpl-tools",
                    "model": str(req.get("model", "?")) + "-2024-07",
                    "choices": [{"index": 0, "finish_reason": "tool_calls",
                                 "message": {"role": "assistant",
                                             "content": None,
                                             "tool_calls": calls}}],
                    "usage": {"prompt_tokens": 12, "completion_tokens": 8},
                })
            if req.get("unknown_shape"):
                # A model endpoint answering in a shape nobody documented.
                # Served from an inference path on purpose, so the extractor
                # genuinely runs over it and must still find nothing.
                return self._send(200, {
                    "model": "mystery-1", "answer": "ok",
                    "actions": [{"invoke": "send_email", "with": {"to": "x"}}],
                })
            if req.get("stream"):
                # Same URL, streamed — which is how every provider does it, and
                # therefore the only shape worth testing. Usage arrives in the
                # final event or not at all.
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                served = str(req.get("model", "?")) + "-2024-07"
                events = [
                    '{"id":"c1","model":"%s",'
                    '"choices":[{"delta":{"content":"a stable"}}]}' % served,
                    '{"id":"c1","choices":[{"delta":{"content":" answer"}}]}',
                    '{"id":"c1","choices":[{"delta":{},"finish_reason":"stop"}],'
                    '"usage":{"prompt_tokens":11,"completion_tokens":7}}',
                    "[DONE]",
                ]
                for ev in events:
                    raw = ("data: " + ev + chr(10) * 2).encode()
                    self.wfile.write(b"%x" % len(raw) + CRLF + raw + CRLF)
                    self.wfile.flush()
                self.wfile.write(b"0" + CRLF + CRLF)
                return
            self._send(200, {
                "id": "chatcmpl-lab",
                "object": "chat.completion",
                "model": str(req.get("model", "?")) + "-2024-07",
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant",
                                         "content": "a stable answer"}}],
                "usage": {"prompt_tokens": 11, "completion_tokens": 7,
                          "total_tokens": 18},
            })
        elif p == "/v1/messages":
            # Anthropic shape: tool_use blocks alongside text, and arguments
            # arriving as an object rather than as a JSON string.
            req = json.loads(body or b"{}")
            content = [{"type": "text", "text": "let me look that up"}]
            for i in range(int(req.get("n_tools", 1))):
                content.append({
                    "type": "tool_use", "id": "toolu_%d" % i,
                    "name": ["lookup_order", "send_email", "web_search"][i % 3],
                    "input": {"order_id": 4471 + i},
                })
            self._send(200, {
                "id": "msg_lab", "type": "message", "role": "assistant",
                "model": str(req.get("model", "claude")) + "-20240620",
                "content": content, "stop_reason": "tool_use",
                "usage": {"input_tokens": 14, "output_tokens": 6},
            })
        elif p == "/echo":
            # echo the body as-is — this is how we test what lands in the file
            self._raw(200, body or b"{}", "application/json")
        elif p == "/redirect":
            # 302 with a secret in the Location URL: the response headers must
            # not write that secret into the file.
            self.send_response(302)
            self.send_header(
                "Location",
                "http://127.0.0.1:8731/echo?api_key=sk-IN-LOCATION-HEADER")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif p == "/bytes":
            # non-UTF-8 on purpose: it used to come out mangled by replace
            self._raw(200, bytes(range(256)), "application/octet-stream")
        elif p == "/stream":
            # SSE with real gaps: if the recorder buffers, four arrivals become
            # one — a behaviour change the agent sees.
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for i in range(4):
                piece = ("data: tok%d" + chr(10) * 2) % i
                raw = piece.encode()
                self.wfile.write(b"%x" % len(raw) + CRLF + raw + CRLF)
                self.wfile.flush()
                time.sleep(0.05)
            self.wfile.write(b"0" + CRLF + CRLF)
        elif p == "/slow":
            time.sleep(0.4)
            self._send(200, {"slow": True})
        else:
            self._send(404, {"error": "not found"})


def start(port=8731):
    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv
