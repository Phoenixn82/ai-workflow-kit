"""Loopback OpenAI-compatible lane shim."""

import hashlib
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .config import load
from .gate import GateError, profile_for, run
from .ledger import Ledger
from .policy import TIERS


def parse_model(model):
    parts = model.split(":")
    if len(parts) != 3 or parts[0] != "lane" or parts[2] not in TIERS:
        raise ValueError("invalid model")
    return parts[1], parts[2]


def make_server(host="127.0.0.1", port=8388, ledger=None):
    if host != "127.0.0.1":
        raise ValueError("lane serve is loopback-only")
    ledger = ledger or Ledger()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            if self.path != "/v1/chat/completions":
                self.send_error(404)
                return
            token = self.headers.get("Authorization", "").removeprefix("Bearer ")
            digest = hashlib.sha256(token.encode()).hexdigest()
            rows = ledger.db.execute(
                "SELECT * FROM callers WHERE token_hash=? AND revoked=0", (digest,)
            ).fetchall()
            if len(rows) != 1:
                self.send_error(401)
                return
            length = self.headers.get("Content-Length")
            if length is None:
                self.send_error(400, "missing Content-Length")
                return
            try:
                data = json.loads(self.rfile.read(int(length)))
                cls, tier = parse_model(data.get("model", ""))
            except (ValueError, TypeError, json.JSONDecodeError):
                self.send_error(400, "invalid request")
                return
            max_tier = rows[0]["max_tier"]
            if max_tier not in TIERS or TIERS.index(tier) > TIERS.index(max_tier):
                self.send_error(403)
                return
            state = ledger.db.execute(
                "SELECT value FROM state WHERE key='degraded'"
            ).fetchone()
            if state and state[0] == "1":
                self.send_error(503, "degraded")
                return
            chain = profile_for(cls, tier)
            admission = ledger.admission(cls, tier, chain)
            if (not admission or not admission["admitted"]) and load(
                "classes.yaml"
            ).get(cls, {}).get("volume") == "high":
                self.send_error(409, "not_admitted")
                return
            try:
                messages = data.get("messages", [])
                prompt = messages[-1].get("content", "") if messages else ""
                output = run(
                    cls,
                    tier,
                    prompt,
                    caller=rows[0]["caller"],
                    ledger=ledger,
                    tools=data.get("tools"),
                    facts=data.get("facts"),
                )
            except GateError as error:
                self.send_error(503 if str(error) == "degraded" else 409, str(error))
                return
            body = json.dumps(
                {
                    "id": "lane",
                    "object": "chat.completion",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": output},
                            "finish_reason": "stop",
                        }
                    ],
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            return

    return ThreadingHTTPServer((host, port), Handler)


def serve(host="127.0.0.1", port=8388):
    make_server(host, port).serve_forever()
