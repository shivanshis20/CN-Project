#!/usr/bin/env python3
"""
load_test.py - evaluates the reservation server under increasing load.

For each load level it sends random RESERVE requests (Poisson arrivals, so
requests overlap in time like real users) and records, for EVERY request:
the outcome (ACCEPTED / REJECTED / ERROR), the rejection reason and the
response time. It also samples the utilization of every link once per second.

Offered load rho = (arrival rate x mean duration x mean bandwidth x fraction of
requests that cross the trunk) / trunk capacity.  rho = 1 means the requests
would exactly fill the 10 Mbps s1-s2 link on average; rho > 1 means overload.

Usage (the server must be running):
    python3 load_test.py                                  full run (about 5 minutes)
    python3 load_test.py --quick                          short run (about 1.5 minutes)
    python3 load_test.py --repeats 3 --label with_controller
    python3 load_test.py --loads 0.5,1,2 --seconds 30 --pattern mixed

Output: results/<label>_<time>/  with  requests.csv, utilization.csv, config.json
Then make graphs:  python3 make_graphs.py results/<that folder>
"""

import argparse
import csv
import json
import os
import random
import re
import socket
import threading
import time

LEFT_HOSTS = ["h1", "h2"]        # on switch s1
RIGHT_HOSTS = ["h3", "h4"]       # on switch s2
ALL_HOSTS = LEFT_HOSTS + RIGHT_HOSTS
TRUNK_CAPACITY_MBPS = 10.0       # the s1-s2 link


# ---------------------------------------------------------------------------
# Talking to the server
# ---------------------------------------------------------------------------
def send_request(host, port, line, timeout=30):
    """Send one command on its own connection. Returns (reply or None, milliseconds)."""
    started = time.time()
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            reader = sock.makefile("r")
            sock.sendall((line + "\n").encode())
            reply = reader.readline().strip()
            sock.sendall(b"QUIT\n")
    except OSError:
        return None, (time.time() - started) * 1000
    return reply, (time.time() - started) * 1000


def classify(reply):
    """Turn the server's reply into (outcome, reason)."""
    if reply is None or reply == "":
        return "ERROR", "connection_failed"
    if reply.startswith("ACCEPTED"):
        return "ACCEPTED", ""
    if reply.startswith("REJECTED"):
        match = re.search(r"link (\S+) ", reply)
        if match:
            return "REJECTED", "insufficient_bandwidth_on_" + match.group(1)
        if "controller" in reply:
            return "REJECTED", "controller_failure"
        return "REJECTED", "other"
    return "ERROR", reply[:40]


# ---------------------------------------------------------------------------
# Utilization sampler (runs in the background for the whole test)
# ---------------------------------------------------------------------------
class UtilizationSampler(threading.Thread):
    LINE = re.compile(r"^(\S+): ([\d.]+)/([\d.]+) Mbps")

    def __init__(self, host, port):
        super().__init__(daemon=True)
        self.host, self.port = host, port
        self.state = {"load": None, "run": None, "t0": time.time()}
        self.rows = []
        self.stop_event = threading.Event()

    def run(self):
        try:
            sock = socket.create_connection((self.host, self.port), timeout=10)
        except OSError:
            return
        reader = sock.makefile("r")
        while not self.stop_event.is_set():
            try:
                sock.sendall(b"UTILIZATION\n")
                lines = []
                while True:
                    line = reader.readline().strip()
                    if line in ("END", ""):
                        break
                    lines.append(line)
            except OSError:
                return
            state = dict(self.state)
            if state["load"] is not None:
                for line in lines:
                    m = self.LINE.match(line)
                    if m:
                        reserved, capacity = float(m.group(2)), float(m.group(3))
                        self.rows.append([state["load"], state["run"],
                                          round(time.time() - state["t0"], 2), m.group(1),
                                          reserved, capacity, round(100 * reserved / capacity, 1)])
            time.sleep(1.0)
        sock.close()


# ---------------------------------------------------------------------------
# One load level
# ---------------------------------------------------------------------------
def pick_pair(rng, pattern):
    if pattern == "cross":                       # every request uses the 10 Mbps trunk
        a, b = rng.choice(LEFT_HOSTS), rng.choice(RIGHT_HOSTS)
        return (a, b) if rng.random() < 0.5 else (b, a)
    src = rng.choice(ALL_HOSTS)                  # "mixed": any two different hosts
    dst = rng.choice([h for h in ALL_HOSTS if h != src])
    return src, dst


def crosses_trunk(src, dst):
    return (src in LEFT_HOSTS) != (dst in LEFT_HOSTS)


def run_level(args, load, run_index, rng, sampler, results):
    """Send Poisson arrivals at `load` requests per second for args.seconds."""
    sampler.state.update(load=load, run=run_index, t0=time.time())
    start = time.time()
    threads = []
    clock = 0.0

    def one_request(offset, src, dst, bandwidth, duration):
        line = f"RESERVE {src} {dst} {bandwidth} {duration}"
        reply, latency_ms = send_request(args.host, args.port, line)
        outcome, reason = classify(reply)
        with results["lock"]:
            results["rows"].append([load, run_index, round(offset, 3), src, dst, bandwidth,
                                    duration, int(crosses_trunk(src, dst)), outcome, reason,
                                    round(latency_ms, 2)])

    while True:
        clock += rng.expovariate(load)           # gap until the next request
        if clock > args.seconds:
            break
        wait = start + clock - time.time()
        if wait > 0:
            time.sleep(wait)
        src, dst = pick_pair(rng, args.pattern)
        bandwidth = rng.choice(args.bandwidths)
        duration = rng.randint(args.dur_min, args.dur_max)
        thread = threading.Thread(target=one_request, args=(clock, src, dst, bandwidth, duration))
        thread.start()
        threads.append(thread)

    for thread in threads:
        thread.join()
    # Let every reservation expire so the next level starts from an empty network
    time.sleep(args.dur_max + 2)
    return len(threads)


def offered_load(args, rate):
    p_cross = 1.0 if args.pattern == "cross" else 8 / 12
    mean_bw = sum(args.bandwidths) / len(args.bandwidths)
    mean_dur = (args.dur_min + args.dur_max) / 2
    return rate * mean_dur * mean_bw * p_cross / TRUNK_CAPACITY_MBPS


# ---------------------------------------------------------------------------
def main():
    p = argparse.ArgumentParser(description="Reservation server load test")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=5000)
    p.add_argument("--loads", default="0.2,0.5,1,2,4",
                   help="request arrival rates to test, in requests per second")
    p.add_argument("--seconds", type=int, default=40, help="how long to send requests at each level")
    p.add_argument("--repeats", type=int, default=1, help="repeat every level this many times")
    p.add_argument("--bandwidths", default="1,2,3,4", help="possible requested Mbps")
    p.add_argument("--dur-min", type=int, default=5)
    p.add_argument("--dur-max", type=int, default=15)
    p.add_argument("--pattern", choices=["cross", "mixed"], default="cross",
                   help="cross = every request uses the trunk; mixed = any pair of hosts")
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--label", default="run", help="name for this experiment (used in graph titles)")
    p.add_argument("--outdir", default=None)
    p.add_argument("--quick", action="store_true", help="short run: 3 levels, 20 s each")
    args = p.parse_args()

    args.bandwidths = [float(b) if "." in b else int(b) for b in args.bandwidths.split(",")]
    rates = [float(x) for x in args.loads.split(",")]
    if args.quick:
        rates, args.seconds = [0.5, 2, 6], 20

    # Is the server there?
    reply, _ = send_request(args.host, args.port, "STATUS")
    if reply is None:
        print(f"Cannot reach the reservation server at {args.host}:{args.port}. Start server.py first.")
        return 1

    outdir = args.outdir or os.path.join("results", f"{args.label}_{time.strftime('%Y%m%d_%H%M%S')}")
    os.makedirs(outdir, exist_ok=True)

    total_time = len(rates) * args.repeats * (args.seconds + args.dur_max + 2)
    print(f"Evaluating '{args.label}': {len(rates)} load levels x {args.repeats} run(s), "
          f"about {total_time / 60:.1f} minutes. Output: {outdir}\n")

    rng = random.Random(args.seed)
    sampler = UtilizationSampler(args.host, args.port)
    sampler.start()
    results = {"rows": [], "lock": threading.Lock()}

    levels = []
    for rate in rates:
        for run_index in range(1, args.repeats + 1):
            rho = offered_load(args, rate)
            print(f"  rate {rate:g} req/s (offered load rho = {rho:.2f}), run {run_index} ...", flush=True)
            sent = run_level(args, rate, run_index, rng, sampler, results)
            levels.append({"rate": rate, "run": run_index, "rho": round(rho, 3), "requests": sent})
    sampler.stop_event.set()

    with open(os.path.join(outdir, "requests.csv"), "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["load", "run", "t_offset_s", "src", "dst", "bandwidth_mbps", "duration_s",
                         "crosses_trunk", "outcome", "reason", "latency_ms"])
        writer.writerows(sorted(results["rows"], key=lambda r: (r[0], r[1], r[2])))
    with open(os.path.join(outdir, "utilization.csv"), "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["load", "run", "t_s", "link", "reserved_mbps", "capacity_mbps", "utilization_pct"])
        writer.writerows(sampler.rows)
    with open(os.path.join(outdir, "config.json"), "w") as f:
        json.dump({"label": args.label, "pattern": args.pattern, "seconds": args.seconds,
                   "repeats": args.repeats, "bandwidths": args.bandwidths,
                   "dur_min": args.dur_min, "dur_max": args.dur_max, "seed": args.seed,
                   "trunk_capacity_mbps": TRUNK_CAPACITY_MBPS, "levels": levels}, f, indent=2)

    # ---- quick summary in the terminal ----
    print("\n  rate  rho   requests  accepted  rejected  errors  accept%  mean ms")
    for rate in rates:
        rows = [r for r in results["rows"] if r[0] == rate]
        acc = sum(r[8] == "ACCEPTED" for r in rows)
        rej = sum(r[8] == "REJECTED" for r in rows)
        err = sum(r[8] == "ERROR" for r in rows)
        mean_ms = sum(r[10] for r in rows) / max(len(rows), 1)
        print(f"  {rate:<5g} {offered_load(args, rate):<5.2f} {len(rows):<9} {acc:<9} {rej:<9} "
              f"{err:<7} {100 * acc / max(len(rows), 1):<8.1f} {mean_ms:.1f}")

    controller_failures = sum(r[9] == "controller_failure" for r in results["rows"])
    errors = sum(r[8] == "ERROR" for r in results["rows"])
    if controller_failures:
        print(f"\nWARNING: {controller_failures} requests were rejected because the SDN controller "
              f"failed. Start the controller and Mininet, or run server.py --no-controller.")
    if errors:
        print(f"\nWARNING: {errors} requests got an ERROR / no answer. Check the server window.")
    peak = max((row[6] for row in sampler.rows if row[3] == "s1-s2"), default=0)
    print(f"\nPeak utilization of the s1-s2 link: {peak:.0f}%  (must never exceed 100%)")
    print(f"\nNext:  python3 make_graphs.py {outdir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
