# SDN controller + Mininet topology

| File | What it is |
|---|---|
| `reservation_controller.py` | Ryu (OpenFlow 1.3) controller: learning switch + REST interface (`POST /reserve`, `DELETE /reserve/<id>`, `GET /rules`) that installs flow rules and a rate-limiting meter per reservation |
| `mininet_topo.py` | Builds h1-h4, s1, s2 (10 Mbps trunk), connects to the controller on 127.0.0.1:6653, starts a message receiver on every host, and (with `--server-link`) connects the network to the reservation server |
| `host_messenger.py` | Host-to-host messaging over TCP sockets, run inside the Mininet hosts. Can reserve bandwidth first |

## One-time setup (Ubuntu in WSL2)
    sudo apt update
    sudo apt install -y mininet openvswitch-switch iperf ethtool telnet net-tools
    curl -LsSf https://astral.sh/uv/install.sh | sh      # then reopen the terminal
    uv venv --python 3.9 ~/ryuenv
    source ~/ryuenv/bin/activate
    uv pip install "setuptools<66" wheel pbr
    uv pip install --no-build-isolation ryu eventlet==0.30.2
    ryu-manager --version          # should print: ryu-manager 4.34

## Every time (4 terminals)
    # Terminal 1 - controller
    sudo service openvswitch-switch start
    source ~/ryuenv/bin/activate
    cd <repo>/controller
    ryu-manager --ofp-tcp-listen-port 6653 reservation_controller.py

    # Terminal 2 - network (start AFTER the controller; run it from the controller folder)
    cd <repo>/controller
    sudo python3 mininet_topo.py --userspace --server-link

    # Terminal 3 - reservation server
    cd <repo>/server
    python3 server.py

    # Terminal 4 - client
    cd <repo>/server
    python3 client.py

Clean up after a crash:  sudo mn -c

## Host-to-host messages (at the `mininet>` prompt)
The reservation server (Terminal 3) must be running for `--reserve`.

    h1 python3 host_messenger.py send h3 "hello from h1"          # text message
    sh cat /tmp/h3_inbox.txt                                       # what h3 received
    h1 python3 host_messenger.py send h3 --bulk 5                  # 5 MB, no reservation (~9.5 Mbit/s)
    h1 python3 host_messenger.py send h3 --bulk 5 --reserve 5 --secs 60   # reserve 5 Mbps first (~5 Mbit/s)

Options: `--reserve MBPS` asks the reservation server first and only sends if ACCEPTED,
`--secs N` sets the duration, `--keep` keeps the reservation after the transfer
(by default it is cancelled when the transfer ends).
