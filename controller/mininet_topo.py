#!/usr/bin/env python3
"""
mininet_topo.py - builds the virtual network and connects it to the Ryu controller.

    h1 (port1) --+                         +-- (port1) h3
                 s1 =====(10 Mbps)===== s2
    h2 (port2) --+   port3 <-> port3       +-- (port2) h4

  - host links: 100 Mbps, trunk s1-s2: 10 Mbps  (same as topology.py)
  - h1..h4 have IPs 10.0.0.1 .. 10.0.0.4 and MACs 00:00:00:00:00:01 .. 04
  - port numbers are fixed on purpose: reservation_controller.py relies on them
  - every host runs a message receiver (host_messenger.py), inbox: /tmp/<host>_inbox.txt

Run (needs root), from the folder that contains this file:
  sudo python3 mininet_topo.py --userspace --server-link
      --userspace    if Open vSwitch complains about the kernel module (WSL2)
      --server-link  connects the network to the reservation server (10.0.0.254) so that
                     the HOSTS can send RESERVE requests themselves (needed for --reserve)
      --no-tc        if bandwidth limits ('tc') are not supported
      --no-receivers do not start the message receivers

Clean up after a crash:  sudo mn -c
"""

import argparse
import os
import subprocess

from mininet.cli import CLI
from mininet.link import Link, TCLink
from mininet.log import info, setLogLevel
from mininet.net import Mininet
from mininet.node import Node, OVSSwitch, RemoteController

CONTROLLER_IP = "127.0.0.1"
CONTROLLER_PORT = 6653
SERVER_LINK_IP = "10.0.0.254/24"     # where the reservation server appears to the hosts
MESSENGER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "host_messenger.py")


def disable_checksum_offload(net):
    """The OVS userspace datapath cannot handle checksum offload on virtual links:
    ping works but TCP (iperf) silently stalls. Switching offload off fixes it."""
    for host in net.hosts:
        for intf in host.intfNames():
            host.cmd("ethtool -K %s tx off rx off" % intf)
    names = [intf for switch in net.switches for intf in switch.intfNames() if intf != "lo"]
    if getattr(net, "root_intf_name", None):
        names.append(net.root_intf_name)
    for intf in names:
        subprocess.run(["ethtool", "-K", intf, "tx", "off", "rx", "off"], capture_output=True)


def build_network(userspace=False, no_tc=False, server_link=False):
    """Create, wire and start the network. Returns the Mininet object."""
    net = Mininet(controller=None, switch=OVSSwitch,
                  link=Link if no_tc else TCLink, autoSetMacs=False)

    net.addController("c0", controller=RemoteController, ip=CONTROLLER_IP, port=CONTROLLER_PORT)

    switch_options = {"protocols": "OpenFlow13"}
    if userspace:
        switch_options["datapath"] = "user"   # Open vSwitch userspace datapath (no kernel module)
    s1 = net.addSwitch("s1", dpid="1", **switch_options)
    s2 = net.addSwitch("s2", dpid="2", **switch_options)

    h1 = net.addHost("h1", ip="10.0.0.1/24", mac="00:00:00:00:00:01")
    h2 = net.addHost("h2", ip="10.0.0.2/24", mac="00:00:00:00:00:02")
    h3 = net.addHost("h3", ip="10.0.0.3/24", mac="00:00:00:00:00:03")
    h4 = net.addHost("h4", ip="10.0.0.4/24", mac="00:00:00:00:00:04")

    host_link = {} if no_tc else {"bw": 100}
    trunk_link = {} if no_tc else {"bw": 10}

    # port2 / port1 = the switch-side port number (fixed, see the controller)
    net.addLink(h1, s1, port2=1, **host_link)
    net.addLink(h2, s1, port2=2, **host_link)
    net.addLink(h3, s2, port2=1, **host_link)
    net.addLink(h4, s2, port2=2, **host_link)
    net.addLink(s1, s2, port1=3, port2=3, **trunk_link)   # the 10 Mbps bottleneck

    if server_link:
        # Plug the machine we are running on ("root") into s1 (port 4), so that the
        # hosts can reach the reservation server at 10.0.0.254:5000
        root = Node("root", inNamespace=False)
        link = net.addLink(root, s1, port2=4)
        root.setIP(SERVER_LINK_IP, intf=link.intf1)
        net.root_intf_name = link.intf1.name

    net.start()
    if userspace:
        disable_checksum_offload(net)
    return net


def start_receivers(net):
    """Start a message receiver (host_messenger.py listen) on every host."""
    started = []
    for host in net.hosts:
        inbox = open("/tmp/%s_inbox.txt" % host.name, "w")
        process = host.popen(["python3", MESSENGER, "listen"], stdin=subprocess.DEVNULL,
                             stdout=inbox, stderr=subprocess.STDOUT)
        started.append((process, inbox))
    return started


def stop_receivers(started):
    for process, inbox in started:
        process.terminate()
        inbox.close()


def main():
    parser = argparse.ArgumentParser(description="Reservation system topology")
    parser.add_argument("--userspace", action="store_true", help="use the OVS userspace datapath")
    parser.add_argument("--no-tc", action="store_true", help="do not apply link bandwidth limits")
    parser.add_argument("--server-link", action="store_true",
                        help="connect the reservation server so hosts can send RESERVE requests")
    parser.add_argument("--no-receivers", action="store_true", help="do not start message receivers")
    args = parser.parse_args()

    setLogLevel("info")
    net = build_network(userspace=args.userspace, no_tc=args.no_tc, server_link=args.server_link)
    receivers = []
    try:
        info("*** Waiting for the switches to connect to the controller\n")
        net.waitConnected(timeout=20)
        if not args.no_receivers:
            receivers = start_receivers(net)
            info("*** Message receivers started on h1..h4 (inboxes: /tmp/h1_inbox.txt ... h4)\n")
        info("*** Ready. Try:  pingall   |   h1 python3 host_messenger.py send h3 \"hello\"\n")
        CLI(net)
    finally:
        stop_receivers(receivers)
        net.stop()


if __name__ == "__main__":
    main()
