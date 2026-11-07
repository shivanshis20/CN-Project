"""
reservation_controller.py - the SDN controller (Ryu, OpenFlow 1.3)

It does two jobs:

  1. BASIC FORWARDING: a simple learning switch so ping/ARP and ordinary traffic work.

  2. RESERVATIONS: it exposes the REST interface our reservation server uses.
       POST   /reserve         {"id": 1, "src": "h1", "dst": "h3", "bandwidth_mbps": 5}
       DELETE /reserve/<id>
       GET    /rules           (list what is installed - handy for debugging)
     For a reservation it installs, on the switches along the path,
       - a high-priority FLOW RULE that matches "IP traffic from src to dst"
       - a METER (rate limiter) at the first switch, so that traffic cannot
         exceed the reserved bandwidth.

Run (needs the Ryu virtualenv, see the setup guide):
  ryu-manager --ofp-tcp-listen-port 6653 reservation_controller.py
Ryu serves the REST interface on port 8080 by default.

The host/port layout below MUST match mininet_topo.py:

    h1 (port1) --+                         +-- (port1) h3
                 s1 (dpid 1) ==port3==port3== s2 (dpid 2)
    h2 (port2) --+                         +-- (port2) h4
"""

import json
import os

from ryu.app.wsgi import ControllerBase, WSGIApplication, route
from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, DEAD_DISPATCHER, MAIN_DISPATCHER, set_ev_cls
from ryu.lib.packet import ether_types, ethernet, packet
from ryu.ofproto import ofproto_v1_3
from webob import Response

# ---------------------------------------------------------------------------
# The network, as the controller knows it (matches mininet_topo.py)
# ---------------------------------------------------------------------------
# host name -> (IP address, datapath id of its switch, switch port it is plugged into)
HOSTS = {
    "h1": ("10.0.0.1", 1, 1),
    "h2": ("10.0.0.2", 1, 2),
    "h3": ("10.0.0.3", 2, 1),
    "h4": ("10.0.0.4", 2, 2),
}
# datapath id -> the port that leads to the other switch
TRUNK_PORT = {1: 3, 2: 3}

LEARN_PRIORITY = 1        # ordinary learning-switch flows
RESERVE_PRIORITY = 100    # reservation flows always win over learned ones
# Meter burst size as a fraction of the rate (0.5 = about half a second of data).
# Tuned by experiment: 0.1 made TCP reach only ~70% of the reserved rate, 1.0 overshot.
BURST_FRACTION = float(os.environ.get("BURST_FRACTION", "0.5"))
MIN_BURST_KB = 64         # kilobits
ALL_COOKIE_BITS = 0xFFFFFFFFFFFFFFFF


class SwitchNotConnected(Exception):
    pass


def compute_hops(src, dst):
    """Which switch should send traffic out of which port, for traffic src -> dst.
    Returns a list [(dpid, out_port), ...] in the order the packets travel."""
    _, src_dpid, _ = HOSTS[src]
    _, dst_dpid, dst_port = HOSTS[dst]
    if src_dpid == dst_dpid:
        return [(src_dpid, dst_port)]
    return [(src_dpid, TRUNK_PORT[src_dpid]), (dst_dpid, dst_port)]


class ReservationController(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]
    _CONTEXTS = {"wsgi": WSGIApplication}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.datapaths = {}         # dpid -> datapath object of every connected switch
        self.mac_to_port = {}       # dpid -> {mac: port}  (learning switch table)
        self.reservations = {}      # id -> {"src", "dst", "bandwidth_mbps", "hops"}

        # Attach our REST API to Ryu's built-in web server
        kwargs["wsgi"].register(ReservationApi, {"controller_app": self})

    # ------------------------------------------------------------------
    # Switch connection handling
    # ------------------------------------------------------------------
    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        """A switch just connected: tell it to send unknown packets to us."""
        dp = ev.msg.datapath
        ofp, parser = dp.ofproto, dp.ofproto_parser
        match = parser.OFPMatch()   # matches everything
        actions = [parser.OFPActionOutput(ofp.OFPP_CONTROLLER, ofp.OFPCML_NO_BUFFER)]
        self._add_flow(dp, 0, match, actions)    # priority 0 = "table-miss" rule

    @set_ev_cls(ofp_event.EventOFPStateChange, [MAIN_DISPATCHER, DEAD_DISPATCHER])
    def state_change_handler(self, ev):
        dp = ev.datapath
        if ev.state == MAIN_DISPATCHER:
            self.datapaths[dp.id] = dp
            self.logger.info("switch connected: dpid=%s", dp.id)
        elif ev.state == DEAD_DISPATCHER and dp.id in self.datapaths:
            del self.datapaths[dp.id]
            self.logger.info("switch disconnected: dpid=%s", dp.id)

    # ------------------------------------------------------------------
    # Learning switch (ordinary traffic)
    # ------------------------------------------------------------------
    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in_handler(self, ev):
        msg = ev.msg
        dp = msg.datapath
        ofp, parser = dp.ofproto, dp.ofproto_parser
        in_port = msg.match["in_port"]

        eth = packet.Packet(msg.data).get_protocols(ethernet.ethernet)[0]
        if eth.ethertype == ether_types.ETH_TYPE_LLDP:
            return  # ignore switch-discovery chatter

        table = self.mac_to_port.setdefault(dp.id, {})
        table[eth.src] = in_port                       # learn where the sender is
        out_port = table.get(eth.dst, ofp.OFPP_FLOOD)  # known destination, or flood

        actions = [parser.OFPActionOutput(out_port)]
        if out_port != ofp.OFPP_FLOOD:
            match = parser.OFPMatch(in_port=in_port, eth_dst=eth.dst, eth_src=eth.src)
            self._add_flow(dp, LEARN_PRIORITY, match, actions, idle_timeout=60)

        out = parser.OFPPacketOut(datapath=dp, buffer_id=msg.buffer_id,
                                  in_port=in_port, actions=actions, data=msg.data)
        dp.send_msg(out)

    def _add_flow(self, dp, priority, match, actions, idle_timeout=0):
        ofp, parser = dp.ofproto, dp.ofproto_parser
        instructions = [parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions)]
        dp.send_msg(parser.OFPFlowMod(datapath=dp, priority=priority, match=match,
                                      instructions=instructions, idle_timeout=idle_timeout))

    # ------------------------------------------------------------------
    # Reservations
    # ------------------------------------------------------------------
    def install_reservation(self, rid, src, dst, bandwidth_mbps):
        """Install the flow rules (and the meter) for one reservation."""
        hops = compute_hops(src, dst)
        for dpid, _ in hops:
            if dpid not in self.datapaths:
                raise SwitchNotConnected(f"switch dpid={dpid} is not connected")

        # If this id already exists (e.g. the reservation server was restarted
        # and re-used the id), replace the old one instead of failing.
        if rid in self.reservations:
            self.remove_reservation(rid)

        src_ip, dst_ip = HOSTS[src][0], HOSTS[dst][0]
        rate_kbps = int(bandwidth_mbps * 1000)
        burst_kb = max(int(rate_kbps * BURST_FRACTION), MIN_BURST_KB)

        for index, (dpid, out_port) in enumerate(hops):
            dp = self.datapaths[dpid]
            ofp, parser = dp.ofproto, dp.ofproto_parser
            instructions = []

            if index == 0:
                # First switch ("ingress"): create the meter that limits the rate.
                # Policing once at the entrance is enough; later switches just forward.
                band = parser.OFPMeterBandDrop(rate=rate_kbps, burst_size=burst_kb)
                dp.send_msg(parser.OFPMeterMod(datapath=dp, command=ofp.OFPMC_ADD,
                                               flags=ofp.OFPMF_KBPS, meter_id=rid,
                                               bands=[band]))
                instructions.append(parser.OFPInstructionMeter(rid))

            instructions.append(parser.OFPInstructionActions(
                ofp.OFPIT_APPLY_ACTIONS, [parser.OFPActionOutput(out_port)]))

            match = parser.OFPMatch(eth_type=ether_types.ETH_TYPE_IP,
                                    ipv4_src=src_ip, ipv4_dst=dst_ip)
            # cookie = reservation id, so we can find and delete exactly these flows later
            dp.send_msg(parser.OFPFlowMod(datapath=dp, cookie=rid,
                                          priority=RESERVE_PRIORITY, match=match,
                                          instructions=instructions))

        self.reservations[rid] = {"src": src, "dst": dst,
                                  "bandwidth_mbps": bandwidth_mbps, "hops": hops}
        self.logger.info("INSTALLED reservation id=%s %s->%s %s Mbps hops=%s",
                         rid, src, dst, bandwidth_mbps, hops)

    def remove_reservation(self, rid):
        """Delete the flow rules and the meter of one reservation. False if unknown."""
        reservation = self.reservations.pop(rid, None)
        if reservation is None:
            return False

        for index, (dpid, _) in enumerate(reservation["hops"]):
            dp = self.datapaths.get(dpid)
            if dp is None:
                continue  # switch is gone; its rules are gone too
            ofp, parser = dp.ofproto, dp.ofproto_parser

            # Delete every flow whose cookie equals this reservation id
            dp.send_msg(parser.OFPFlowMod(datapath=dp, cookie=rid, cookie_mask=ALL_COOKIE_BITS,
                                          table_id=ofp.OFPTT_ALL, command=ofp.OFPFC_DELETE,
                                          out_port=ofp.OFPP_ANY, out_group=ofp.OFPG_ANY,
                                          match=parser.OFPMatch()))
            if index == 0:
                dp.send_msg(parser.OFPMeterMod(datapath=dp, command=ofp.OFPMC_DELETE,
                                               flags=ofp.OFPMF_KBPS, meter_id=rid))

        self.logger.info("REMOVED reservation id=%s", rid)
        return True


# ---------------------------------------------------------------------------
# REST API (served by Ryu's web server, port 8080)
# ---------------------------------------------------------------------------
class ReservationApi(ControllerBase):

    def __init__(self, req, link, data, **config):
        super().__init__(req, link, data, **config)
        self.ctl = data["controller_app"]

    @staticmethod
    def _reply(status, payload):
        return Response(content_type="application/json", charset="utf-8", status=status,
                        body=json.dumps(payload).encode("utf-8"))

    @route("reserve", "/reserve", methods=["POST"])
    def create(self, req, **kwargs):
        try:
            info = json.loads(req.body.decode("utf-8"))
            rid = int(info["id"])
            src, dst = info["src"], info["dst"]
            bandwidth = float(info["bandwidth_mbps"])
        except (ValueError, KeyError, TypeError):
            return self._reply(400, {"error": "body must be JSON with id, src, dst, bandwidth_mbps"})

        # id 0 is refused on purpose: cookie 0 belongs to the table-miss rule
        if rid < 1 or bandwidth <= 0:
            return self._reply(400, {"error": "id must be >= 1 and bandwidth_mbps > 0"})
        if src not in HOSTS or dst not in HOSTS or src == dst:
            return self._reply(400, {"error": f"src/dst must be two different hosts of {sorted(HOSTS)}"})

        try:
            self.ctl.install_reservation(rid, src, dst, bandwidth)
        except SwitchNotConnected as e:
            return self._reply(503, {"error": str(e)})
        return self._reply(200, {"status": "installed"})

    @route("reserve_item", "/reserve/{rid}", methods=["DELETE"], requirements={"rid": r"\d+"})
    def delete(self, req, **kwargs):
        rid = int(kwargs["rid"])
        if not self.ctl.remove_reservation(rid):
            return self._reply(404, {"error": "no such reservation"})
        return self._reply(200, {"status": "removed"})

    @route("rules", "/rules", methods=["GET"])
    def rules(self, req, **kwargs):
        listing = [{"id": rid, "src": r["src"], "dst": r["dst"],
                    "bandwidth_mbps": r["bandwidth_mbps"]}
                   for rid, r in sorted(self.ctl.reservations.items())]
        return self._reply(200, listing)
