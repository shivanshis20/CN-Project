"""
mock_controller.py - a FAKE SDN controller, only for testing your server.

It speaks the agreed REST interface but does not touch any real network.
It just remembers which "rules" it was told to install and prints them.
When Teammate 2's real controller is ready, stop using this file.

Run:
  python3 mock_controller.py            (normal: accepts everything)
  python3 mock_controller.py --fail     (refuses every install, to test failure handling)

Extra endpoint for you to inspect it:
  GET /rules   -> JSON list of the rules currently "installed"
"""

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 8080
FAIL_MODE = "--fail" in sys.argv

rules = {}                       # id -> {"src":..., "dst":..., "bandwidth_mbps":...}
rules_lock = threading.Lock()


class Handler(BaseHTTPRequestHandler):

    def _reply(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path != "/reserve":
            return self._reply(404, {"error": "unknown path"})

        length = int(self.headers.get("Content-Length", 0))
        try:
            info = json.loads(self.rfile.read(length))
            rid = int(info["id"])
        except (ValueError, KeyError, json.JSONDecodeError):
            return self._reply(400, {"error": "bad request body"})

        if FAIL_MODE:
            print(f"[controller] REFUSED install id={rid} (fail mode)")
            return self._reply(500, {"error": "simulated controller failure"})

        with rules_lock:
            rules[rid] = info
        print(f"[controller] INSTALL id={rid} {info['src']}->{info['dst']} "
              f"limit {info['bandwidth_mbps']} Mbps")
        self._reply(200, {"status": "installed"})

    def do_DELETE(self):
        # path looks like /reserve/3
        parts = self.path.strip("/").split("/")
        if len(parts) != 2 or parts[0] != "reserve" or not parts[1].isdigit():
            return self._reply(404, {"error": "unknown path"})

        rid = int(parts[1])
        with rules_lock:
            existed = rules.pop(rid, None)
        if existed is None:
            print(f"[controller] REMOVE id={rid} (was not installed)")
            return self._reply(404, {"error": "no such reservation"})

        print(f"[controller] REMOVE id={rid}")
        self._reply(200, {"status": "removed"})

    def do_GET(self):
        if self.path == "/rules":
            with rules_lock:
                return self._reply(200, [dict(v, id=k) for k, v in rules.items()])
        self._reply(404, {"error": "unknown path"})

    def log_message(self, format, *args):
        pass        # silence the default per-request log lines


if __name__ == "__main__":
    mode = "FAIL MODE (refuses installs)" if FAIL_MODE else "normal"
    print(f"Mock controller listening on port {PORT} - {mode}")
    try:
        ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        print("\nMock controller stopped.")
