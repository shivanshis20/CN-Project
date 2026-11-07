#!/usr/bin/env python3
"""
mininet_topo.py - builds the virtual network and connects it to the Ryu controller.

    h1 (port1) --+                         +-- (port1) h3
                 s1 =====(10 Mbps)===== s2
    h2 (port2) --+   port3 <-> port3       +-- (port2) h4

  - host links: 100 Mbps, trunk s1-s2: 10 Mbps  (same as topology.py)
  - h1..h4 have IPs 10.0.0.1 .. 10.0.0.4 and MACs 00:00:00:00:00:01 .. 04
  - port numbers are fixed on purpose: reservation_controller.py relies on them

Run (needs root):
  sudo python3 mininet_topo.py                 normal
  sudo python3 mininet_topo.py --userspace     if Open vSwitch complains about the kernel module (WSL2)
  sudo python3 mininet_topo.py --no-tc         if bandwidth limits ('tc') are not supported

Clean up after a crash:  sudo mn -c
"""

import argparse
import subprocess

from mininet.cli import CLI
from mininet.link import Link, TCLink
from mininet.log import info, setLogLevel
from mininet.net import Mininet
from mininet.node import OVSSwitch, RemoteController

CONTROLLER_IP = "127.0.0.1"
CONTROLLER_PORT = 6653


def disable_checksum_offload(net):
    """The OVS userspace datapath cannot handle checksum offload on virtual links:
    ping works but TCP (iperf) silently stalls. Switching offload off fixes it."""
    for host in net.hosts:
        for intf in host.intfNames():
            host.cmd("ethtool -K %s tx off rx off" % intf)
    for switch in net.switches:
        for intf in switch.intfNames():
            if intf != "lo":
                subprocess.run(["ethtool", "-K", intf, "tx", "off", "rx", "off"],
                               capture_output=True)


def build_network(userspace=False, no_tc=False):
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

    net.start()
    if userspace:
        disable_checksum_offload(net)
    return net


def main():
    parser = argparse.ArgumentParser(description="Reservation system topology")
    parser.add_argument("--userspace", action="store_true", help="use the OVS userspace datapath")
    parser.add_argument("--no-tc", action="store_true", help="do not apply link bandwidth limits")
    args = parser.parse_args()

    setLogLevel("info")
    net = build_network(userspace=args.userspace, no_tc=args.no_tc)
    info("*** Waiting for the switches to connect to the controller\n")
    net.waitConnected(timeout=20)
    info("*** Ready. Try:  pingall   |   h3 iperf -s &   |   h1 iperf -c 10.0.0.3 -t 10\n")
    CLI(net)
    net.stop()


if __name__ == "__main__":
    main()
