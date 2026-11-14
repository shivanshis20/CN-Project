# Network Resource Reservation System (SDN + Socket Programming)

A TCP-based service through which users request network resources such as bandwidth.
A reservation server validates every request against the capacity of the links on its path.
Accepted requests are enforced by an SDN controller (Ryu) that installs OpenFlow flow rules and
rate-limiting meters on virtual switches (Mininet + Open vSwitch). Reservations expire
automatically, link utilization is tracked, and hosts inside the network can exchange messages
and data over TCP sockets, optionally on a reserved path.

Team: Shivanshi, Rohan, Shekhar  |  Course: Computer Networks

## Contents
1. [Architecture](#architecture)
2. [What each requirement maps to](#what-each-requirement-maps-to)
3. [Repository layout](#repository-layout)
4. [Setup (Ubuntu in WSL2)](#setup-ubuntu-in-wsl2)
5. [Running the system](#running-the-system)
6. [Using it](#using-it)
7. [Protocols and interfaces](#protocols-and-interfaces)
8. [Evaluation](#evaluation)
9. [What was tested](#what-was-tested)
10. [Design notes and limitations](#design-notes-and-limitations)
11. [Troubleshooting](#troubleshooting)

## Architecture

```
 Users / hosts                 Reservation server              SDN controller (Ryu)             Switches (Mininet)
 ------------     TCP 5000     ------------------   REST 8080   --------------------  OpenFlow    ------------------
 client.py    ------------->   server.py           ---------->  reservation_          --------->  s1 ====== s2
 host_messenger.py             - validates input                 controller.py           6653       (Open vSwitch)
                               - finds the path (BFS)            - flow rules per
                               - checks every link               reservation
                               - expiry + logging                - meter = rate limit
```

The server makes the **decision** (admission control). The controller does the **enforcement**.

Virtual network (`controller/mininet_topo.py`, mirrored in `server/topology.py`):

```
 h1 (10.0.0.1) --+                              +-- h3 (10.0.0.3)
                 s1 =====  10 Mbps trunk  ===== s2
 h2 (10.0.0.2) --+      (the bottleneck)        +-- h4 (10.0.0.4)
```
Host links are 100 Mbps. With `--server-link` the machine running the server is also attached to
s1 as 10.0.0.254, so hosts can reach the reservation server.

## What each requirement maps to

| Requirement | Implementation |
|---|---|
| TCP reservation protocol | `server/server.py`: line-based text protocol on port 5000 |
| Handle concurrent requests | One thread per client; a lock makes "check capacity and book it" atomic |
| Maintain resource information | Reservation table in the server; link capacities in `server/topology.py` |
| Detect reservation conflicts | A request is accepted only if every link on its path has enough free bandwidth |
| Install corresponding SDN policies | `controller/reservation_controller.py`: flow rule + OpenFlow meter per reservation |
| Reservation expiry | Background thread removes expired reservations and their rules |
| Track resource utilization | `UTILIZATION` command and `utilization_log.csv` (sampled every 5 s) |
| Evaluate accepted and rejected requests | `evaluation/`: load generator, graphs, summary table |
| Host-to-host messages | `controller/host_messenger.py` and the client `MSG` command |

## Repository layout

```
CN-Project/
  README.md                       this file
  server/                         the reservation service (runs on the machine, outside Mininet)
    server.py                       TCP server, admission control, expiry, logging, MSG relay
    client.py                       interactive test client
    topology.py                     network map, link capacities, BFS path finding
    controller_client.py            server's REST client for the controller
    mock_controller.py              fake controller for testing without Ryu
    stress_test.py                  30 simultaneous clients: proves there is no overbooking
  controller/                     the SDN side
    reservation_controller.py       Ryu app: learning switch + REST API + flow rules + meters
    mininet_topo.py                 builds the virtual network
    host_messenger.py               host-to-host messaging, runs inside the Mininet hosts
    README.md                       notes for this folder
  evaluation/                     experiments
    load_test.py                    sends random requests at increasing load
    make_graphs.py                  graphs and summary table from the results
    README.md                       how to run the evaluation
```

## Setup (Ubuntu in WSL2)

```bash
sudo apt update
sudo apt install -y mininet openvswitch-switch iperf ethtool telnet net-tools python3-matplotlib
curl -LsSf https://astral.sh/uv/install.sh | sh        # then close and reopen the terminal

# Ryu needs an older Python, so it gets its own environment
uv venv --python 3.9 ~/ryuenv
source ~/ryuenv/bin/activate
uv pip install "setuptools<66" wheel pbr
uv pip install --no-build-isolation ryu eventlet==0.30.2
ryu-manager --version                                  # should print: ryu-manager 4.34
```
Clone the repository inside the Linux home folder (not under `/mnt/c`):
`git clone https://github.com/shivanshis20/CN-Project.git ~/cn`

## Running the system

Open four terminals and start them in this order. The shutdown order matters too.

**Clean up first**
```bash
sudo mn -c
sudo service openvswitch-switch start
```
**Terminal 1: controller** (wait for `wsgi starting up on http://0.0.0.0:8080`)
```bash
source ~/ryuenv/bin/activate
cd ~/cn/controller
ryu-manager --ofp-tcp-listen-port 6653 reservation_controller.py
```
**Terminal 2: network** (not inside the `(ryuenv)` environment; wait for the `mininet>` prompt)
```bash
cd ~/cn/controller
sudo python3 mininet_topo.py --userspace --server-link
```
**Terminal 3: reservation server**
```bash
cd ~/cn/server
python3 server.py
```
**Terminal 4: client**
```bash
cd ~/cn/server
python3 client.py
```
**Shutting down:** type `exit` at `mininet>`, then Ctrl+C in the controller and server windows.
Never run `sudo mn -c` while the controller is running: it kills it.

Options: `--userspace` is needed on WSL2 (no kernel Open vSwitch module); `--no-tc` skips link
bandwidth limits if `tc` is unsupported; `python3 server.py --no-controller` runs the server alone.

## Using it

**Client commands** (Terminal 4):

| Command | Meaning |
|---|---|
| `RESERVE h1 h3 5 60` | Reserve 5 Mbps from h1 to h3 for 60 seconds |
| `CANCEL 1` | Cancel reservation 1 |
| `STATUS` | List active reservations |
| `UTILIZATION` | Reserved / capacity of every link |
| `MSG h1 h3 hello there` | Host h1 sends "hello there" to host h3 across the network |
| `QUIT` | Disconnect |

**See the bandwidth limit working** (at the `mininet>` prompt, one line at a time):
```
h3 iperf -s -p 5001 > /dev/null &
h1 iperf -c 10.0.0.3 -p 5001 -t 8            # no reservation: about 9.5 Mbit/s
```
then in the client `RESERVE h1 h3 5 120`, and again at `mininet>`:
```
h1 iperf -c 10.0.0.3 -p 5001 -t 8            # about 5 Mbit/s
sh ovs-ofctl -O OpenFlow13 dump-flows s1     # shows a priority=100 flow with meter:1
sh ovs-ofctl -O OpenFlow13 dump-meters s1    # shows rate=5000
```
**Host-to-host messages** (at `mininet>`; every host runs a receiver, inbox in `/tmp/<host>_inbox.txt`):
```
h1 python3 host_messenger.py send h3 "hello from h1"
sh cat /tmp/h3_inbox.txt
h1 python3 host_messenger.py send h3 --bulk 5                          # 5 MB, no reservation
h1 python3 host_messenger.py send h3 --bulk 5 --reserve 5 --secs 60    # reserve first, then send
```
With `--reserve` the host asks the reservation server for bandwidth itself, sends only if the reply
is ACCEPTED, and cancels the reservation afterwards (`--keep` keeps it).

## Protocols and interfaces

**Client to server** (TCP port 5000, one text line per message, ends with a newline):

| Request | Reply |
|---|---|
| `RESERVE <src> <dst> <mbps> <secs>` | `ACCEPTED id=<n> path=...` or `REJECTED <reason>` or `ERROR <reason>` |
| `CANCEL <id>` | `OK cancelled id=<n>` or `ERROR <reason>` |
| `STATUS` | one line per active reservation, then `END` |
| `UTILIZATION` | one line per link, then `END` |
| `MSG <src> <dst> <text>` | `DELIVERED ...` or `ERROR <reason>` |
| `QUIT` | `BYE` |

TCP is a byte stream, so every message is one line and multi-line replies end with `END`.

**Server to controller** (REST/JSON, port 8080):

| Call | Effect |
|---|---|
| `POST /reserve` with `{"id","src","dst","bandwidth_mbps"}` | Installs flow rules and a meter, replies 200 (4xx/5xx on failure) |
| `DELETE /reserve/<id>` | Removes them |
| `GET /rules` | Lists installed reservations |

If the controller fails, the server rolls the reservation back and rejects the request, so it never
promises bandwidth that was not set up. The server gives up waiting for the controller after 3 s.

**Host to host** (TCP port 6000): `SEND MSG <n>` / `SEND BULK <n>` followed by `n` bytes; the receiver
answers `OK <n> <seconds>`. `SEND RELAY <n>` asks a host to send a message on the server's behalf.

**Log files** written by the server in its working folder:
`reservations_log.csv` (every request, outcome and reason) and `utilization_log.csv` (per link, every 5 s).

## Evaluation

See `evaluation/README.md`. In short:
```bash
cd ~/cn/evaluation
python3 load_test.py --repeats 3 --label server_only        # with: python3 server.py --no-controller
python3 load_test.py --repeats 3 --label with_controller    # with the full system running
python3 make_graphs.py results/server_only_<time> results/with_controller_<time>
```
It produces six graphs (acceptance vs load, accepted vs rejected, rejection reasons, acceptance by
requested bandwidth, utilization over time, response time) and a summary table.

_Add the final graphs and the summary table here after running the experiments._

## What was tested

- Reservation accepted, conflicting reservation rejected, cancel frees capacity, exact fit accepted.
- Invalid input: unknown host, same host, unknown command, bad numbers, over-long messages.
- Expiry: a reservation ends by itself, the controller removes its rules, capacity is freed.
- Concurrency: 30 simultaneous clients asking for 1 Mbps on a 10 Mbps link give exactly 10 accepted.
- Controller refusing or being unreachable: the reservation is rolled back and rejected.
- Enforcement with iperf: about 9.5 Mbit/s without a reservation, about 5 Mbit/s with a 5 Mbps reservation.
- Host-to-host messages in both directions, typed messages from the client, reservation before a transfer.

## Design notes and limitations

- **Admission control.** For a request, BFS finds the path, then every link on it must have
  `reserved + requested <= capacity`. Checking and booking happen under one lock, so two clients
  cannot both take the last free bandwidth.
- **Enforcement.** A meter on the first switch drops traffic above the reserved rate. It is placed
  once, at the entrance of the path. The burst size was tuned by experiment (0.1x the rate made TCP
  reach only about 70% of the reservation, 1.0x overshot, 0.5x was close).
- **Limitations.**
  - A meter caps a reserved flow but gives it no priority; unreserved traffic may still use leftover capacity.
  - Only traffic from source to destination is limited, not the replies.
  - If two reservations between the same pair of hosts are active at once, the controller keeps a single rule for that pair.
  - Reservations are kept in memory and are lost if the server restarts.
  - Links are treated as one shared pool (not separate per direction); one shortest path is used.
  - Reservations are immediate only (no booking in advance), and there is no authentication.
  - If a switch reconnects, its rules are not reinstalled. A failed rule removal is only logged.
  - The topology is written in three places (`topology.py`, `mininet_topo.py`, `reservation_controller.py`); keep them in sync.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Unable to contact the remote controller`, `pingall` shows 100% dropped | Start the controller (Terminal 1) before the network |
| `ovs-vsctl: database connection failed` | `sudo service openvswitch-switch start` |
| Errors mentioning `tc` or `RTNETLINK` | Add `--no-tc` to the Mininet command |
| Errors after a crash or an unclean exit | `exit` Mininet properly; if needed `sudo mn -c`, then restart the controller |
| `ryu-manager` prints `loading app` and returns to the prompt | The controller file is empty or incomplete; re-clone the repository |
| `RESERVE` says controller unreachable | The controller is not running or not ready yet |
| `MSG` says it could not reach the host | Start Mininet with `--server-link` |
| Pasting several lines at the `mininet>` prompt breaks commands | Type or paste one line at a time |
| `ModuleNotFoundError: controller_client` | Run `server.py` from the `server` folder, with all its files |
