"""
Network Resource Reservation System - STEP 3: SDN controller link + expiry

New in this step (compared to step 2):
  - When a reservation is accepted, the server asks the SDN controller to install
    the policy (controller_client.install). If the controller fails, the
    reservation is rolled back so we never promise something that isn't set up.
  - CANCEL also tells the controller to remove the policy.
  - A background "reaper" thread removes expired reservations and tells the
    controller to remove their policies.
  - A background "sampler" thread records link utilization over time
    (utilization_log.csv), for graphs.

Run:
  python3 server.py                  (talks to a controller on port 8080)
  python3 server.py --no-controller  (behaves like step 2, no controller needed)

Protocol (one line per message):
  RESERVE <src> <dst> <bw_mbps> <secs>   ACCEPTED id=<n> | REJECTED <reason> | ERROR <reason>
  CANCEL <id>                            OK cancelled id=<n> | ERROR <reason>
  STATUS                                 one line per active reservation, then END
  UTILIZATION                            one line per link, then END
  MSG <src> <dst> <text...>              DELIVERED ... | ERROR <reason>   (src host sends the text to dst)
  QUIT                                   BYE
"""

import csv
import os
import socket
import sys
import threading
import time

import controller_client
from topology import CAPACITY, HOSTS, HOST_IPS, find_path, path_links

HOST = "0.0.0.0"
PORT = 5000
LOG_FILE = "reservations_log.csv"
UTIL_LOG_FILE = "utilization_log.csv"
AGENT_PORT = 6000            # the message agent (host_messenger.py) listening inside every host
MAX_MESSAGE_CHARS = 500

REAPER_INTERVAL = 1.0        # how often (seconds) we look for expired reservations
SAMPLE_INTERVAL = float(os.environ.get("SAMPLE_INTERVAL", "5"))   # utilization sampling

# ---------------------------------------------------------------------------
# Shared data
# ---------------------------------------------------------------------------
reservations = {}      # id -> {src, dst, bandwidth, path, links, start, expiry}
next_id = 1
table_lock = threading.Lock()     # protects reservations and next_id
log_lock = threading.Lock()       # protects the CSV files


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
def log_request(src, dst, bandwidth, duration, outcome, reason=""):
    """One line per request: the team's agreed format."""
    with log_lock:
        new_file = not os.path.exists(LOG_FILE)
        with open(LOG_FILE, "a", newline="") as f:
            writer = csv.writer(f)
            if new_file:
                writer.writerow(["timestamp", "src", "dst", "bandwidth_mbps",
                                 "duration_s", "outcome", "reason"])
            writer.writerow([time.strftime("%Y-%m-%d %H:%M:%S"), src, dst,
                             bandwidth, duration, outcome, reason])


def log_utilization(samples):
    """samples: list of (link_name, reserved, capacity). One row per link."""
    with log_lock:
        new_file = not os.path.exists(UTIL_LOG_FILE)
        with open(UTIL_LOG_FILE, "a", newline="") as f:
            writer = csv.writer(f)
            if new_file:
                writer.writerow(["timestamp", "link", "reserved_mbps",
                                 "capacity_mbps", "utilization_pct"])
            stamp = time.strftime("%Y-%m-%d %H:%M:%S")
            for link, used, cap in samples:
                writer.writerow([stamp, link, f"{used:g}", cap, f"{100 * used / cap:.1f}"])


# ---------------------------------------------------------------------------
# Helpers (callers must already hold table_lock)
# ---------------------------------------------------------------------------
def is_active(r, now):
    return r["expiry"] > now


def reserved_on_link(link, now):
    total = 0.0
    for r in reservations.values():
        if is_active(r, now) and link in r["links"]:
            total += r["bandwidth"]
    return total


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------
def handle_reserve(parts):
    global next_id

    if len(parts) != 5:
        return "ERROR usage: RESERVE <src> <dst> <bw_mbps> <secs>"

    _, src, dst, bw_text, secs_text = parts

    try:
        bandwidth = float(bw_text)
        duration = int(secs_text)
    except ValueError:
        return "ERROR bandwidth and duration must be numbers"

    if bandwidth <= 0 or duration <= 0:
        return "ERROR bandwidth and duration must be positive"

    if src not in HOSTS or dst not in HOSTS:
        log_request(src, dst, bandwidth, duration, "REJECTED", "unknown_host")
        return f"REJECTED unknown host (valid hosts: {' '.join(HOSTS)})"

    if src == dst:
        log_request(src, dst, bandwidth, duration, "REJECTED", "same_host")
        return "REJECTED source and destination are the same"

    path = find_path(src, dst)
    if path is None:
        log_request(src, dst, bandwidth, duration, "REJECTED", "no_path")
        return "REJECTED no path between hosts"
    links = path_links(path)

    # ---- PHASE 1: check capacity and tentatively reserve (atomic, under lock) ----
    with table_lock:
        now = time.time()

        for link in links:
            used = reserved_on_link(link, now)
            capacity = CAPACITY[link]
            if used + bandwidth > capacity:
                free = capacity - used
                reason = f"insufficient_bandwidth_on_{link[0]}-{link[1]}"
                log_request(src, dst, bandwidth, duration, "REJECTED", reason)
                return (f"REJECTED not enough bandwidth on link {link[0]}-{link[1]} "
                        f"(free {free:g} Mbps, requested {bandwidth:g} Mbps)")

        rid = next_id
        next_id += 1
        reservations[rid] = {
            "src": src, "dst": dst, "bandwidth": bandwidth,
            "path": path, "links": links,
            "start": now, "expiry": now + duration,
        }

    # ---- PHASE 2: ask the controller to install the policy (OUTSIDE the lock) ----
    # A network call can take seconds. If we held table_lock during it, every
    # other client would freeze. The bandwidth is already held in the table, so
    # other requests correctly see it as taken while we wait.
    ok, message = controller_client.install(rid, src, dst, bandwidth)

    if not ok:
        # ROLLBACK: the controller did not set it up, so we must not keep the booking
        with table_lock:
            reservations.pop(rid, None)
        log_request(src, dst, bandwidth, duration, "REJECTED", "controller_failure")
        print(f"[!] install failed for id={rid}: {message}")
        return f"REJECTED controller could not install the policy ({message})"

    log_request(src, dst, bandwidth, duration, "ACCEPTED", "")
    return f"ACCEPTED id={rid} path={'->'.join(path)}"


def handle_cancel(parts):
    if len(parts) != 2:
        return "ERROR usage: CANCEL <id>"
    try:
        rid = int(parts[1])
    except ValueError:
        return "ERROR id must be a number"

    # Whoever removes the entry from the table "owns" the clean-up. If the
    # reaper thread got there first, pop() returns None and we do nothing more.
    with table_lock:
        r = reservations.get(rid)
        if r is None or not is_active(r, time.time()):
            return f"ERROR no active reservation with id={rid}"
        del reservations[rid]

    ok, message = controller_client.remove(rid)
    if not ok:
        print(f"[!] controller removal failed for id={rid}: {message}")
    return f"OK cancelled id={rid}"


def handle_status():
    lines = []
    with table_lock:
        now = time.time()
        for rid, r in reservations.items():
            if is_active(r, now):
                lines.append(
                    f"id={rid} {r['src']}->{r['dst']} {r['bandwidth']:g}Mbps "
                    f"path={'->'.join(r['path'])} remaining={int(r['expiry'] - now)}s"
                )
    lines.append("END")
    return "\n".join(lines)


def handle_utilization():
    lines = []
    with table_lock:
        now = time.time()
        for link in sorted(CAPACITY):
            used = reserved_on_link(link, now)
            cap = CAPACITY[link]
            lines.append(f"{link[0]}-{link[1]}: {used:g}/{cap} Mbps ({100 * used / cap:.0f}%)")
    lines.append("END")
    return "\n".join(lines)


def handle_msg(line):
    """MSG <src> <dst> <text...>: ask host <src> to send <text> to host <dst>.
    The server cannot send as h1 itself (h1 lives inside Mininet), so it asks the
    message agent running on h1, which then sends the message across the network."""
    parts = line.split(None, 3)
    if len(parts) < 4:
        return "ERROR usage: MSG <src> <dst> <text>"
    _, src, dst, text = parts
    if src not in HOST_IPS or dst not in HOST_IPS:
        return f"ERROR unknown host (valid hosts: {' '.join(HOSTS)})"
    if src == dst:
        return "ERROR source and destination are the same"
    if len(text) > MAX_MESSAGE_CHARS:
        return f"ERROR message too long (max {MAX_MESSAGE_CHARS} characters)"

    payload = f"{dst} {text}".encode("utf-8")
    try:
        with socket.create_connection((HOST_IPS[src], AGENT_PORT), timeout=5) as agent:
            agent.settimeout(15)
            agent.sendall(f"SEND RELAY {len(payload)}\n".encode() + payload)
            reply = agent.makefile("r").readline().strip()
    except OSError as e:
        return (f"ERROR could not reach host {src} ({HOST_IPS[src]}): {e} "
                f"(is Mininet running with --server-link?)")

    if reply.startswith("OK"):
        return f"DELIVERED {src}->{dst} path={'->'.join(find_path(src, dst))} {reply}"
    return f"ERROR {reply}"


# ---------------------------------------------------------------------------
# Background threads
# ---------------------------------------------------------------------------
def expiry_reaper():
    """Every REAPER_INTERVAL seconds: remove expired reservations and tell the controller."""
    while True:
        time.sleep(REAPER_INTERVAL)

        # Step 1: under the lock, take the expired ones OUT of the table
        expired = []
        with table_lock:
            now = time.time()
            for rid in [i for i, r in reservations.items() if r["expiry"] <= now]:
                expired.append((rid, reservations.pop(rid)))

        # Step 2: outside the lock, do the slow network calls
        for rid, r in expired:
            ok, message = controller_client.remove(rid)
            status = "removed from controller" if ok else f"controller removal FAILED: {message}"
            print(f"[expiry] id={rid} {r['src']}->{r['dst']} expired, {status}")


def utilization_sampler():
    """Every SAMPLE_INTERVAL seconds: record reserved/capacity of every link."""
    while True:
        time.sleep(SAMPLE_INTERVAL)
        with table_lock:
            now = time.time()
            samples = [(f"{link[0]}-{link[1]}", reserved_on_link(link, now), CAPACITY[link])
                       for link in sorted(CAPACITY)]
        log_utilization(samples)


# ---------------------------------------------------------------------------
# One thread per client
# ---------------------------------------------------------------------------
def handle_client(conn, addr):
    print(f"[+] Client connected: {addr}")
    reader = conn.makefile("r", encoding="utf-8")
    try:
        for line in reader:
            line = line.strip()
            if not line:
                continue
            print(f"[{addr}] received: {line}")
            parts = line.split()
            command = parts[0].upper()

            if command == "RESERVE":
                reply = handle_reserve(parts)
            elif command == "CANCEL":
                reply = handle_cancel(parts)
            elif command == "STATUS":
                reply = handle_status()
            elif command == "UTILIZATION":
                reply = handle_utilization()
            elif command == "MSG":
                reply = handle_msg(line)
            elif command == "QUIT":
                conn.sendall(b"BYE\n")
                break
            else:
                reply = f"ERROR unknown command '{parts[0]}'"

            conn.sendall((reply + "\n").encode("utf-8"))
    except ConnectionResetError:
        print(f"[!] Client {addr} dropped the connection abruptly")
    finally:
        reader.close()
        conn.close()
        print(f"[-] Client disconnected: {addr}")


def main():
    if "--no-controller" in sys.argv:
        controller_client.ENABLED = False
        print("Running WITHOUT an SDN controller (policies are not installed).")
    else:
        print(f"Using SDN controller at {controller_client.CONTROLLER_URL}")

    # Start the two background threads (daemon = they die when the program exits)
    threading.Thread(target=expiry_reaper, daemon=True).start()
    threading.Thread(target=utilization_sampler, daemon=True).start()

    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_socket.bind((HOST, PORT))
    server_socket.listen(5)
    print(f"Reservation server listening on port {PORT} ... (Ctrl+C to stop)")

    try:
        while True:
            conn, addr = server_socket.accept()
            threading.Thread(target=handle_client, args=(conn, addr), daemon=True).start()
    except KeyboardInterrupt:
        print("\nShutting down server.")
    finally:
        server_socket.close()


if __name__ == "__main__":
    main()
