"""
Network Resource Reservation System - STEP 1: TCP client

Type commands at the prompt, for example:
  RESERVE h1 h2 5 60      (reserve 5 Mbps from h1 to h2 for 60 seconds)
  STATUS
  UTILIZATION
  CANCEL 1
  QUIT

Usage:
  python3 client.py                 (connects to the server on this machine)
  python3 client.py 192.168.1.10    (connects to a server at another address)
"""

import socket
import sys

SERVER_HOST = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"  # 127.0.0.1 = this machine
SERVER_PORT = 5000


def main():
    # 1. Create a TCP socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

    # 2. Connect to the server (this performs the TCP three-way handshake)
    try:
        sock.connect((SERVER_HOST, SERVER_PORT))
    except ConnectionRefusedError:
        print(f"Could not connect to {SERVER_HOST}:{SERVER_PORT}. Is the server running?")
        return

    print(f"Connected to server at {SERVER_HOST}:{SERVER_PORT}")

    # Reader lets us read the server's replies one line at a time
    reader = sock.makefile("r", encoding="utf-8")

    try:
        while True:
            message = input("> ").strip()
            if not message:
                continue

            # 3. Send our command (add newline so the server knows it ended)
            sock.sendall((message + "\n").encode("utf-8"))

            # 4. Read the reply. STATUS replies span many lines and end with
            #    a line saying END (STATUS and UTILIZATION); every other reply is a single line.
            if message.split()[0].upper() in ("STATUS", "UTILIZATION"):
                while True:
                    line = reader.readline().strip()
                    print(line)
                    if line == "END" or line == "":
                        break
            else:
                reply = reader.readline().strip()
                if reply == "":
                    print("Server closed the connection.")
                    break
                print(reply)

            if message.split()[0].upper() == "QUIT":
                break
    except (KeyboardInterrupt, EOFError):
        print("\nExiting.")
    finally:
        reader.close()
        sock.close()  # 5. Close the connection


if __name__ == "__main__":
    main()
