"""
stress_test.py - proves the server handles simultaneous requests correctly.

N clients connect, wait until ALL are ready, then fire "RESERVE h1 h3 1 <secs>"
at the same instant. The s1-s2 link only has 10 Mbps, so exactly 10 requests
of 1 Mbps may be accepted. If the server had a race condition, more than 10
could slip through.

Run the server WITHOUT the controller for this test (it checks the server logic):
    python3 server.py --no-controller        (terminal 1)
    python3 stress_test.py                   (terminal 2)
    python3 stress_test.py 50 5              (optional: 50 clients, 5 s reservations)
"""

import socket
import sys
import threading

HOST, PORT = "127.0.0.1", 5000
CLIENTS = int(sys.argv[1]) if len(sys.argv) > 1 else 30
DURATION = int(sys.argv[2]) if len(sys.argv) > 2 else 10    # seconds each reservation lasts
CAPACITY_MBPS = 10                                          # the s1-s2 link

results = []
results_lock = threading.Lock()
start_line = threading.Barrier(CLIENTS)    # nobody sends until everybody is connected


def client(number):
    sock = socket.create_connection((HOST, PORT))
    reader = sock.makefile("r")
    start_line.wait()                                       # all clients fire together
    sock.sendall(f"RESERVE h1 h3 1 {DURATION}\n".encode())
    reply = reader.readline().strip()
    sock.sendall(b"QUIT\n")
    sock.close()
    with results_lock:
        results.append(reply)


threads = [threading.Thread(target=client, args=(i,)) for i in range(CLIENTS)]
for t in threads:
    t.start()
for t in threads:
    t.join()

accepted = sum(1 for r in results if r.startswith("ACCEPTED"))
rejected = sum(1 for r in results if r.startswith("REJECTED"))
print(f"Clients:   {CLIENTS} (all sent RESERVE h1 h3 1 {DURATION} at the same instant)")
print(f"Accepted:  {accepted}")
print(f"Rejected:  {rejected}")
print(f"Expected:  {CAPACITY_MBPS} accepted, {CLIENTS - CAPACITY_MBPS} rejected "
      f"(link capacity {CAPACITY_MBPS} Mbps / 1 Mbps each)")
print("RESULT:    " + ("PASS - no overbooking" if accepted == CAPACITY_MBPS else "FAIL"))
