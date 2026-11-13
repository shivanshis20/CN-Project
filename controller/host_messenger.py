#!/usr/bin/env python3
"""
host_messenger.py - host-to-host messaging over TCP sockets, through the SDN network.

It runs INSIDE the Mininet hosts (h1..h4). One host listens, another sends:

  receive (mininet_topo.py starts this on every host automatically):
      python3 host_messenger.py listen

  send a text message:
      python3 host_messenger.py send h3 "hello from h1"

  send a big block of data (to see the network speed), e.g. 5 MB:
      python3 host_messenger.py send h3 --bulk 5

  ask the reservation server for bandwidth FIRST, then send:
      python3 host_messenger.py send h3 --bulk 5 --reserve 5 --secs 60

With --reserve the host connects to the reservation server over TCP, sends
"RESERVE <me> <dst> <mbps> <secs>", and only sends its data if the reply is
ACCEPTED. The reservation is cancelled when the transfer ends (use --keep to
keep it). Destinations can be host names (h1..h4) or IP addresses.

Message format between hosts (application layer, one TCP connection per message):
    sender   ->  "SEND MSG <n>\n" + n bytes of text        or
                 "SEND BULK <n>\n" + n bytes of data
    receiver ->  "OK <n> <seconds>\n"
"""

import argparse
import socket
import sys
import threading
import time

PORT = 6000
CHUNK = 64 * 1024
DEFAULT_SERVER = "10.0.0.254:5000"     # the reservation server, as seen from the hosts

HOST_IPS = {"h1": "10.0.0.1", "h2": "10.0.0.2", "h3": "10.0.0.3", "h4": "10.0.0.4"}
IP_TO_HOST = {ip: name for name, ip in HOST_IPS.items()}


def name_of(ip):
    return IP_TO_HOST.get(ip, ip)


def resolve(destination):
    return HOST_IPS.get(destination, destination)


def log(text):
    print(f"[{time.strftime('%H:%M:%S')}] {text}", flush=True)


# ---------------------------------------------------------------------------
# Receiving side
# ---------------------------------------------------------------------------
def handle_incoming(conn, addr):
    sender = name_of(addr[0])
    reader = conn.makefile("rb")
    try:
        parts = reader.readline().decode("utf-8", "replace").split()
        if len(parts) != 3 or parts[0] != "SEND" or not parts[2].isdigit():
            conn.sendall(b"ERR bad header\n")
            return
        kind, size = parts[1], int(parts[2])
        started = time.time()

        if kind == "MSG":
            text = reader.read(size).decode("utf-8", "replace")
            log(f"message from {sender} ({addr[0]}): {text}")
        elif kind == "BULK":
            remaining = size
            while remaining > 0:
                chunk = reader.read(min(CHUNK, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
            got = size - remaining
            seconds = max(time.time() - started, 1e-6)
            log(f"bulk data from {sender} ({addr[0]}): {got / 1e6:.2f} MB "
                f"in {seconds:.2f} s = {got * 8 / 1e6 / seconds:.2f} Mbit/s")
        else:
            conn.sendall(b"ERR unknown kind\n")
            return

        conn.sendall(f"OK {size} {time.time() - started:.3f}\n".encode())
    except (ConnectionResetError, BrokenPipeError):
        log(f"connection from {sender} dropped")
    finally:
        reader.close()
        conn.close()


def run_listener(port):
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("0.0.0.0", port))
    server.listen(5)
    log(f"listening for messages on port {port}")
    try:
        while True:
            conn, addr = server.accept()
            threading.Thread(target=handle_incoming, args=(conn, addr), daemon=True).start()
    except KeyboardInterrupt:
        pass
    finally:
        server.close()


# ---------------------------------------------------------------------------
# Talking to the reservation server
# ---------------------------------------------------------------------------
def ask_reservation_server(server, line):
    host, port = server.rsplit(":", 1)
    with socket.create_connection((host, int(port)), timeout=5) as sock:
        sock.sendall((line + "\n").encode())
        return sock.makefile("r").readline().strip()


def my_ip_towards(destination_ip):
    """Which of my own IP addresses is used to reach the destination?"""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect((destination_ip, 9))
        return probe.getsockname()[0]
    finally:
        probe.close()


# ---------------------------------------------------------------------------
# Sending side
# ---------------------------------------------------------------------------
def run_sender(args):
    dst_ip = resolve(args.destination)
    dst_name = name_of(dst_ip)
    text = " ".join(args.text)

    if not text and not args.bulk:
        print("nothing to send: give a message text or --bulk <MB>")
        return 1

    reservation_id = None
    if args.reserve:
        me = IP_TO_HOST.get(my_ip_towards(dst_ip))
        if me is None or dst_name not in HOST_IPS:
            print("--reserve only works between hosts h1..h4")
            return 1
        request = f"RESERVE {me} {dst_name} {args.reserve:g} {args.secs}"
        try:
            reply = ask_reservation_server(args.server, request)
        except OSError as e:
            print(f"could not reach the reservation server at {args.server}: {e}")
            return 1
        print(f"reservation: {request}  ->  {reply}")
        if not reply.startswith("ACCEPTED"):
            print("not sending: the reservation was not granted")
            return 2
        reservation_id = int(reply.split("id=")[1].split()[0])

    try:
        with socket.create_connection((dst_ip, args.port), timeout=10) as sock:
            sock.settimeout(180)
            reader = sock.makefile("r")

            if args.bulk:
                size = int(args.bulk * 1_000_000)
                sock.sendall(f"SEND BULK {size}\n".encode())
                block = b"x" * CHUNK
                started = time.time()
                sent = 0
                while sent < size:
                    piece = min(CHUNK, size - sent)
                    sock.sendall(block[:piece])
                    sent += piece
                reply = reader.readline().strip()
                seconds = max(time.time() - started, 1e-6)
                print(f"sent {size / 1e6:.2f} MB to {dst_name} ({dst_ip}) in {seconds:.2f} s "
                      f"= {size * 8 / 1e6 / seconds:.2f} Mbit/s   (receiver replied: {reply})")
            else:
                data = text.encode("utf-8")
                sock.sendall(f"SEND MSG {len(data)}\n".encode() + data)
                reply = reader.readline().strip()
                print(f"sent to {dst_name} ({dst_ip}): \"{text}\"   (receiver replied: {reply})")
    except OSError as e:
        print(f"could not send to {dst_name} ({dst_ip}:{args.port}): {e}")
        return 1
    finally:
        if reservation_id is not None and not args.keep:
            try:
                print("reservation released:",
                      ask_reservation_server(args.server, f"CANCEL {reservation_id}"))
            except OSError:
                print("could not release the reservation (it will expire by itself)")
    return 0


def main():
    parser = argparse.ArgumentParser(description="Host-to-host messenger")
    sub = parser.add_subparsers(dest="mode", required=True)

    listen = sub.add_parser("listen", help="receive messages")
    listen.add_argument("--port", type=int, default=PORT)

    send = sub.add_parser("send", help="send a message or data to another host")
    send.add_argument("destination", help="h1..h4 or an IP address")
    send.add_argument("text", nargs="*", help="the message text")
    send.add_argument("--bulk", type=float, metavar="MB", help="send this many MB of data instead")
    send.add_argument("--reserve", type=float, metavar="MBPS",
                      help="reserve this much bandwidth first")
    send.add_argument("--secs", type=int, default=60, help="reservation duration (default 60)")
    send.add_argument("--keep", action="store_true", help="do not cancel the reservation afterwards")
    send.add_argument("--port", type=int, default=PORT)
    send.add_argument("--server", default=DEFAULT_SERVER,
                      help=f"reservation server address (default {DEFAULT_SERVER})")

    args = parser.parse_args()
    if args.mode == "listen":
        run_listener(args.port)
        return 0
    return run_sender(args)


if __name__ == "__main__":
    sys.exit(main())
