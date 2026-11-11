# SDN controller + Mininet topology

| File | What it is |
|---|---|
| `reservation_controller.py` | Ryu (OpenFlow 1.3) controller: learning switch + REST interface (`POST /reserve`, `DELETE /reserve/<id>`, `GET /rules`) that installs flow rules and a rate-limiting meter per reservation |
| `mininet_topo.py` | Builds h1-h4, s1, s2 (10 Mbps trunk) and connects to the controller on 127.0.0.1:6653 |

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

    # Terminal 2 - network (start AFTER the controller)
    cd <repo>/controller
    sudo python3 mininet_topo.py --userspace

    # Terminal 3 - reservation server
    cd <repo>/server
    python3 server.py

    # Terminal 4 - client
    cd <repo>/server
    python3 client.py

Clean up after a crash:  sudo mn -c
