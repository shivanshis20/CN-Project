"""
controller_client.py - how the reservation server talks to the SDN controller.

The agreed REST interface (Teammate 2 builds the receiving side):

  POST   /reserve         body: {"id": 1, "src": "h1", "dst": "h3", "bandwidth_mbps": 5}
                          -> 200 on success, 4xx/5xx on failure
  DELETE /reserve/<id>    -> 200 on success

Both functions return a pair:  (ok, message)
  ok = True  -> the controller did what we asked
  ok = False -> something went wrong; message says what

Only the Python standard library is used, so nothing needs installing.
"""

import json
import os
import socket
import urllib.error
import urllib.request

# Where the controller lives. Change with:  CONTROLLER_URL=http://10.0.0.5:8080 python3 server.py
CONTROLLER_URL = os.environ.get("CONTROLLER_URL", "http://127.0.0.1:8080")
TIMEOUT_SECONDS = 3          # never wait forever for the controller

# The server sets this to False when started with --no-controller
ENABLED = True


def _send(method, path, body=None):
    """Send one HTTP request to the controller and interpret the outcome."""
    if not ENABLED:
        return True, "controller disabled (skipped)"

    url = CONTROLLER_URL + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )

    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return True, f"HTTP {response.status}"

    except urllib.error.HTTPError as e:
        # The controller answered, but said "no" (status 4xx or 5xx)
        detail = e.read().decode("utf-8", errors="replace")[:100]
        return False, f"controller refused: HTTP {e.code} {detail}"

    except (urllib.error.URLError, socket.timeout, ConnectionError, OSError) as e:
        # We never got an answer: controller down, wrong address, or too slow
        return False, f"controller unreachable: {e}"


def install(reservation_id, src, dst, bandwidth_mbps):
    """Ask the controller to install policies (flow rules + rate limit)."""
    return _send("POST", "/reserve", {
        "id": reservation_id,
        "src": src,
        "dst": dst,
        "bandwidth_mbps": bandwidth_mbps,
    })


def remove(reservation_id):
    """Ask the controller to delete the policies of a reservation."""
    return _send("DELETE", f"/reserve/{reservation_id}")
