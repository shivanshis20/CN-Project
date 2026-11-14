# Evaluation: accepted and rejected requests

`load_test.py` sends random reservation requests to the running server, records what happened to
every one, and samples link utilization once per second. `make_graphs.py` turns the results into
graphs and a summary table.

## What is measured
Requests arrive randomly (Poisson arrivals, so they overlap like real users). Each asks for 1-4 Mbps
for 5-15 seconds between random hosts. With `--pattern cross` (default) every request crosses the
10 Mbps s1-s2 trunk, which is the bottleneck.

**Offered load rho** = arrival rate x mean duration x mean bandwidth / 10 Mbps.
rho = 1 means the requests would exactly fill the trunk on average. rho > 1 is overload.

## One-time setup (WSL, in a plain terminal, NOT the `(ryuenv)` one)
    sudo apt install -y python3-matplotlib

## Experiment A: the reservation server alone (admission control)
    # terminal 1 (cd ~/cn/server)
    python3 server.py --no-controller
    # terminal 2 (cd ~/cn/evaluation)
    python3 load_test.py --repeats 3 --label server_only        # about 14 minutes
    python3 load_test.py --quick --label server_only            # about 2 minutes, for a first look

## Experiment B: the whole system (server + Ryu controller + Mininet)
Start Ryu, Mininet (`--userspace --server-link`) and `python3 server.py` as usual, then:
    python3 load_test.py --repeats 3 --label with_controller

Mininet and the controller must be running, otherwise requests are rejected with
"controller_failure" (the script warns you).

## Make the graphs
    python3 make_graphs.py results/server_only_<time> results/with_controller_<time>

Graphs and `summary.md` / `summary.csv` are saved in `<first folder>/graphs/`. Open the folder from
WSL with:  `explorer.exe results`

## The graphs
| File | Shows |
|---|---|
| 1_acceptance_vs_load | Percentage of requests accepted as offered load grows |
| 2_accepted_vs_rejected | Counts of accepted / rejected requests per load level |
| 3_rejection_reasons | Why requests were rejected (almost always: no bandwidth left on s1-s2) |
| 4_acceptance_by_bandwidth | Acceptance rate for 1, 2, 3 and 4 Mbps requests |
| 5_utilization_over_time | Utilization of the s1-s2 link; it must never go above 100% |
| 6_response_time | Time to answer a RESERVE, accepted vs rejected, one line per experiment |

## Things to know before you present the numbers
- Peak utilization of s1-s2 reaching 100% but never exceeding it is the key correctness result:
  it shows the server never overbooks, even with many simultaneous requests.
- The same `--seed` gives the same sequence of requests, so runs are reproducible. Acceptance
  counts can differ slightly between runs because of timing; `--repeats 3` adds error bars.
- Accepted requests are slower when a controller is attached, because the server also asks the
  controller to install rules (a REST call). Rejected requests are decided by the server alone.
  The first load level can look slower: the first connection to the controller warms up.
- The percentages are for requests the server handled, not measured network throughput.
  Throughput was checked separately with iperf (about 9.5 Mbit/s unreserved, about 5 reserved).
- The low-load levels send only a few requests, so their percentages are noisy. Use `--repeats`.
