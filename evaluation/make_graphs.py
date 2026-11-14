#!/usr/bin/env python3
"""
make_graphs.py - turns load_test.py results into graphs and a summary table.

Usage:
    python3 make_graphs.py results/run_20261120_101500
    python3 make_graphs.py results/no_controller_... results/with_controller_...

The graphs are saved in <first folder>/graphs/ . If you give several folders, the
first one is used for all graphs, and the response-time graph compares them all
(for example "server only" versus "server + SDN controller").

Needs matplotlib:   sudo apt install -y python3-matplotlib
"""

import csv
import json
import os
import sys

import matplotlib

matplotlib.use("Agg")                    # draw to files, no window needed
import matplotlib.pyplot as plt          # noqa: E402
import numpy as np                       # noqa: E402

GREEN, RED, GREY, BLUE = "#2e9e5b", "#d9534f", "#8a8a8a", "#2f6fb5"
TRUNK = "s1-s2"


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------
def load_dir(path):
    with open(os.path.join(path, "config.json")) as f:
        config = json.load(f)
    requests = []
    with open(os.path.join(path, "requests.csv")) as f:
        for r in csv.DictReader(f):
            requests.append({"load": float(r["load"]), "run": int(r["run"]), "t": float(r["t_offset_s"]),
                             "bw": float(r["bandwidth_mbps"]), "outcome": r["outcome"],
                             "reason": r["reason"], "latency": float(r["latency_ms"])})
    utilization = []
    with open(os.path.join(path, "utilization.csv")) as f:
        for r in csv.DictReader(f):
            utilization.append({"load": float(r["load"]), "run": int(r["run"]), "t": float(r["t_s"]),
                                "link": r["link"], "pct": float(r["utilization_pct"])})
    rho = {lvl["rate"]: lvl["rho"] for lvl in config["levels"]}
    return {"config": config, "requests": requests, "util": utilization, "rho": rho,
            "rates": sorted(rho), "path": path}


def level_rows(data, rate):
    return [r for r in data["requests"] if r["load"] == rate]


def acceptance_per_run(rows):
    values = []
    for run in sorted({r["run"] for r in rows}):
        in_run = [r for r in rows if r["run"] == run]
        values.append(100.0 * sum(r["outcome"] == "ACCEPTED" for r in in_run) / max(len(in_run), 1))
    return values


# ---------------------------------------------------------------------------
# Summary table
# ---------------------------------------------------------------------------
def summarize(data):
    rows_out = []
    seconds = data["config"]["seconds"]
    for rate in data["rates"]:
        rows = level_rows(data, rate)
        accepted = sum(r["outcome"] == "ACCEPTED" for r in rows)
        rejected = sum(r["outcome"] == "REJECTED" for r in rows)
        errors = sum(r["outcome"] == "ERROR" for r in rows)
        util = [u["pct"] for u in data["util"] if u["load"] == rate and u["link"] == TRUNK]
        util_busy = [u["pct"] for u in data["util"]
                     if u["load"] == rate and u["link"] == TRUNK and u["t"] <= seconds]
        latencies = [r["latency"] for r in rows] or [0]
        acc_lat = [r["latency"] for r in rows if r["outcome"] == "ACCEPTED"] or [0]
        rows_out.append({
            "arrival_rate_req_per_s": rate, "offered_load_rho": data["rho"][rate],
            "requests": len(rows), "accepted": accepted, "rejected": rejected, "errors": errors,
            "acceptance_pct": round(100 * accepted / max(len(rows), 1), 1),
            "mean_util_s1s2_pct": round(float(np.mean(util_busy)), 1) if util_busy else 0,
            "peak_util_s1s2_pct": round(max(util), 1) if util else 0,
            "mean_response_ms": round(float(np.mean(latencies)), 2),
            "mean_response_accepted_ms": round(float(np.mean(acc_lat)), 2),
            "p95_response_ms": round(float(np.percentile(latencies, 95)), 2)})
    return rows_out


def write_summary(data, outdir):
    table = summarize(data)
    with open(os.path.join(outdir, "summary.csv"), "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(table[0]))
        writer.writeheader()
        writer.writerows(table)
    heads = ["Rate (req/s)", "Load rho", "Requests", "Accepted", "Rejected", "Errors",
             "Accepted %", "Mean util s1-s2 %", "Peak util s1-s2 %", "Mean resp. ms", "Mean resp. accepted ms", "p95 resp. ms"]
    with open(os.path.join(outdir, "summary.md"), "w") as f:
        f.write(f"# Evaluation summary: {data['config']['label']} "
                f"(pattern: {data['config']['pattern']}, {data['config']['seconds']} s per level, "
                f"{data['config']['repeats']} run(s) per level)\n\n")
        f.write("| " + " | ".join(heads) + " |\n|" + "---|" * len(heads) + "\n")
        for r in table:
            f.write("| " + " | ".join(str(v) for v in r.values()) + " |\n")
        peak = max(r["peak_util_s1s2_pct"] for r in table)
        f.write(f"\nPeak utilization of the s1-s2 link over the whole test: **{peak:g}%** "
                f"({'never above 100%: no overbooking' if peak <= 100 else 'ABOVE 100%: overbooking!'}).\n")
    return table


# ---------------------------------------------------------------------------
# Graphs
# ---------------------------------------------------------------------------
def style(ax, title, xlabel, ylabel):
    ax.set_title(title, fontsize=12, fontweight="bold")
    ax.set_xlabel(xlabel)
    ax.set_ylabel(ylabel)
    ax.grid(alpha=0.3)


def subtitle(data):
    c = data["config"]
    return f"{c['label']}, pattern={c['pattern']}, {c['seconds']} s per level, {c['repeats']} run(s)"


def graph_acceptance_vs_load(data, outdir):
    rhos, means, stds = [], [], []
    for rate in data["rates"]:
        values = acceptance_per_run(level_rows(data, rate))
        rhos.append(data["rho"][rate])
        means.append(np.mean(values))
        stds.append(np.std(values))
    fig, ax = plt.subplots(figsize=(7, 4.5))
    ax.errorbar(rhos, means, yerr=stds if data["config"]["repeats"] > 1 else None,
                marker="o", color=BLUE, linewidth=2, capsize=4)
    for x, y in zip(rhos, means):
        ax.annotate(f"{y:.0f}%", (x, y), textcoords="offset points", xytext=(0, 8), ha="center", fontsize=9)
    ax.axvline(1.0, color=GREY, linestyle="--", linewidth=1)
    ax.text(1.02, 5, "link fully booked\non average", color=GREY, fontsize=8)
    ax.set_ylim(0, 110)
    style(ax, "Acceptance rate vs offered load\n" + subtitle(data),
          "Offered load rho (1 = requests exactly fill the 10 Mbps link)", "Requests accepted (%)")
    fig.tight_layout()
    fig.savefig(os.path.join(outdir, "1_acceptance_vs_load.png"), dpi=150)
    plt.close(fig)


def graph_counts(data, outdir):
    labels, accepted, rejected, errors = [], [], [], []
    for rate in data["rates"]:
        rows = level_rows(data, rate)
        labels.append(f"{rate:g} req/s\n(rho={data['rho'][rate]:g})")
        accepted.append(sum(r["outcome"] == "ACCEPTED" for r in rows))
        rejected.append(sum(r["outcome"] == "REJECTED" for r in rows))
        errors.append(sum(r["outcome"] == "ERROR" for r in rows))
    x = np.arange(len(labels))
    fig, ax = plt.subplots(figsize=(7.5, 4.5))
    ax.bar(x, accepted, color=GREEN, label="Accepted")
    ax.bar(x, rejected, bottom=accepted, color=RED, label="Rejected")
    ax.bar(x, errors, bottom=np.add(accepted, rejected), color=GREY, label="Errors" if sum(errors) else None)
    ax.set_xticks(x)
    ax.set_xticklabels(labels)
    ax.legend()
    style(ax, "Accepted and rejected requests per load level\n" + subtitle(data),
          "Arrival rate (offered load)", "Number of requests")
    fig.tight_layout()
    fig.savefig(os.path.join(outdir, "2_accepted_vs_rejected.png"), dpi=150)
    plt.close(fig)


def graph_reasons(data, outdir):
    counts = {}
    for r in data["requests"]:
        if r["outcome"] != "ACCEPTED":
            key = r["reason"] or r["outcome"]
            counts[key] = counts.get(key, 0) + 1
    fig, ax = plt.subplots(figsize=(7.5, 3.2 + 0.4 * max(len(counts), 1)))
    if counts:
        names = sorted(counts, key=counts.get)
        ax.barh([n.replace("insufficient_bandwidth_on_", "no bandwidth on link ") for n in names],
                [counts[n] for n in names], color=RED)
        for i, n in enumerate(names):
            ax.text(counts[n], i, f" {counts[n]}", va="center", fontsize=9)
    else:
        ax.text(0.5, 0.5, "No rejected requests", ha="center", va="center", transform=ax.transAxes)
    style(ax, "Why requests were rejected (all load levels)\n" + subtitle(data), "Number of requests", "")
    fig.tight_layout()
    fig.savefig(os.path.join(outdir, "3_rejection_reasons.png"), dpi=150)
    plt.close(fig)


def graph_by_bandwidth(data, outdir, min_samples=5):
    bandwidths = sorted({r["bw"] for r in data["requests"]})
    fig, ax = plt.subplots(figsize=(7, 4.5))
    colors = plt.cm.viridis(np.linspace(0.05, 0.85, len(data["rates"])))
    for color, rate in zip(colors, data["rates"]):
        rows = level_rows(data, rate)
        xs, ys = [], []
        for bw in bandwidths:
            chosen = [r for r in rows if r["bw"] == bw]
            if len(chosen) >= min_samples:               # too few requests = meaningless percentage
                xs.append(bw)
                ys.append(100 * sum(r["outcome"] == "ACCEPTED" for r in chosen) / len(chosen))
        if xs:
            ax.plot(xs, ys, marker="o", color=color, label=f"rho={data['rho'][rate]:g}")
    ax.set_xticks(bandwidths)
    ax.set_ylim(0, 110)
    ax.legend(title="Offered load", fontsize=8)
    style(ax, "Acceptance rate by requested bandwidth\n" + subtitle(data),
          f"Requested bandwidth (Mbps)   [points based on fewer than {min_samples} requests are left out]",
          "Requests accepted (%)")
    fig.tight_layout()
    fig.savefig(os.path.join(outdir, "4_acceptance_by_bandwidth.png"), dpi=150)
    plt.close(fig)


def graph_utilization(data, outdir):
    rates = data["rates"]
    fig, axes = plt.subplots(len(rates), 1, figsize=(8, 1.9 * len(rates) + 1), sharex=False)
    axes = np.atleast_1d(axes)
    seconds = data["config"]["seconds"]
    for ax, rate in zip(axes, rates):
        samples = sorted((u for u in data["util"] if u["load"] == rate and u["run"] == 1 and u["link"] == TRUNK),
                         key=lambda u: u["t"])
        ax.fill_between([u["t"] for u in samples], [u["pct"] for u in samples], color=BLUE, alpha=0.25)
        ax.plot([u["t"] for u in samples], [u["pct"] for u in samples], color=BLUE, linewidth=1.5)
        ax.axhline(100, color=RED, linestyle="--", linewidth=1)
        ax.axvline(seconds, color=GREY, linestyle=":", linewidth=1)
        ax.set_ylim(0, 115)
        ax.set_ylabel("util. %")
        ax.set_title(f"{rate:g} req/s (rho = {data['rho'][rate]:g})", fontsize=9, loc="left")
        ax.grid(alpha=0.3)
    axes[-1].set_xlabel("Time since this load level started (s)   "
                        "[dotted line = requests stop, reservations drain]")
    fig.suptitle(f"Utilization of the 10 Mbps s1-s2 link over time (red = 100% capacity)\n{subtitle(data)}",
                 fontsize=11, fontweight="bold")
    fig.tight_layout(rect=(0, 0, 1, 0.94))
    fig.savefig(os.path.join(outdir, "5_utilization_over_time.png"), dpi=150)
    plt.close(fig)


def graph_response_time(all_data, outdir):
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.5), sharey=True)
    panels = [("ACCEPTED", "Accepted requests\n(server also asks the controller to install rules)"),
              ("REJECTED", "Rejected requests\n(decided by the server alone)")]
    for ax, (outcome, title) in zip(axes, panels):
        for data, color in zip(all_data, [BLUE, "#e08a1e", GREEN, RED]):
            rhos, means, p95 = [], [], []
            for rate in data["rates"]:
                lat = [r["latency"] for r in level_rows(data, rate) if r["outcome"] == outcome]
                if lat:
                    rhos.append(data["rho"][rate])
                    means.append(np.mean(lat))
                    p95.append(np.percentile(lat, 95))
            if rhos:
                ax.plot(rhos, means, marker="o", color=color, linewidth=2,
                        label=f"{data['config']['label']}: mean")
                ax.plot(rhos, p95, marker="s", color=color, linestyle="--",
                        label=f"{data['config']['label']}: 95th percentile")
        ax.legend(fontsize=8)
        style(ax, title, "Offered load rho", "Response time (ms)")
    fig.suptitle("Time to get an answer to a RESERVE request", fontsize=12, fontweight="bold")
    fig.tight_layout(rect=(0, 0, 1, 0.94))
    fig.savefig(os.path.join(outdir, "6_response_time.png"), dpi=150)
    plt.close(fig)


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    all_data = [load_dir(path) for path in sys.argv[1:]]
    data = all_data[0]
    outdir = os.path.join(data["path"], "graphs")
    os.makedirs(outdir, exist_ok=True)

    table = write_summary(data, outdir)
    graph_acceptance_vs_load(data, outdir)
    graph_counts(data, outdir)
    graph_reasons(data, outdir)
    graph_by_bandwidth(data, outdir)
    graph_utilization(data, outdir)
    graph_response_time(all_data, outdir)

    print(f"Saved to {outdir}:")
    for name in sorted(os.listdir(outdir)):
        print("  ", name)
    peak = max(r["peak_util_s1s2_pct"] for r in table)
    print(f"\nPeak s1-s2 utilization: {peak:g}%  ->  "
          f"{'OK, never above 100%' if peak <= 100 else 'PROBLEM: above 100%'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
