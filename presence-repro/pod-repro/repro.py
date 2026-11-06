#!/usr/bin/env python3
"""Reproduce the node 27 presence SUBSCRIBE storm on a QA pod (e.g. 6071), through the real
sipproxies with the pod's SIP credentials. macOS or Debian, Python 3.8+, nothing to install.

The credentials file (default: users_qa_udp_n6071.secrets.csv next to this script) has one
device per line:  sipusername,password,udp:sip.t2-n6071.qa.n2p.io:6070
Each device keeps 1 MWI + N BLF subscriptions like a real phone (digest auth, retransmissions,
fail-over to the next sipproxy IP, new dialog after 481/408). By default every device in the
file is used and all subscriptions are created in a few minutes, so they all refresh together
every 9 minutes (the herd). With ~115k devices that is ~460k subscriptions, about twice node
27, which is enough to push 2 presence nodes over the edge without any CPU cap.

The run: create all subscriptions -> wait until they are up -> trigger (restart both presence
nodes at once, as the Nov 3 swap did) -> watch -> out/<time>/summary.md with a verdict.
"""
import argparse
import csv
import glob
import json
import os
import re
import signal
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ALLOWED_HOST = re.compile(r"\.(qa|dev)\.n2p\.io$")


def pct(vals, p):
    if not vals:
        return None
    vals = sorted(vals)
    k = (len(vals) - 1) * p / 100.0
    f = int(k)
    c = min(f + 1, len(vals) - 1)
    return round(vals[f] + (vals[c] - vals[f]) * (k - f), 1)


def dist(vals):
    if not vals:
        return "n=0"
    return "n=%d, min %.1f, p50 %s, p90 %s, max %.1f ms" % (len(vals), min(vals), pct(vals, 50), pct(vals, 90),
                                                          max(vals))


def worker_stats(out):
    """Latest line of every worker -> (alive, dialogs, merged last-10-s counters)."""
    alive = dialogs = 0
    c = {}
    for p in glob.glob(os.path.join(out, "w*", "gen-stats.jsonl")):
        try:
            with open(p) as f:
                lines = [ln for ln in f if '"alive"' in ln]
        except OSError:
            continue
        if not lines:
            continue
        d = json.loads(lines[-1])
        alive += d.get("alive", 0)
        dialogs += d.get("dialogs", 0)
        for k, v in d.get("c", {}).items():
            c[k] = c.get(k, 0) + v
    return alive, dialogs, c


def alive_series(out):
    """Sum of alive per 10-s bucket over all workers."""
    buckets = {}
    for p in glob.glob(os.path.join(out, "w*", "gen-stats.jsonl")):
        with open(p) as f:
            for ln in f:
                d = json.loads(ln)
                if "alive" in d:
                    b = int(d["t"] // 10 * 10)
                    buckets.setdefault(b, {})[p] = d["alive"]
    n = len(glob.glob(os.path.join(out, "w*", "gen-stats.jsonl")))
    return sorted((b, sum(v.values())) for b, v in buckets.items() if len(v) == n)


def summarize(out, t_start, t_ready, t_trigger, t_end, dialogs):
    rows = []
    for p in glob.glob(os.path.join(out, "w*", "requests.csv")):
        with open(p) as f:
            for r in csv.DictReader(f):
                rows.append((float(r["t"]), r["kind"], r["code"], float(r["leg_ms"])))
    alive = alive_series(out)
    lines = ["# Presence storm repro, %s" % time.strftime("%Y-%m-%d %H:%M", time.localtime(t_start)), ""]
    fmt = lambda t: time.strftime("%H:%M:%S", time.localtime(t)) if t else "none"
    lines.append("Subscriptions: %d. All up: %s. Trigger: %s. End: %s." % (
        dialogs, fmt(t_ready), fmt(t_trigger), fmt(t_end)))
    lines.append("")
    phases = [("Ramp", t_start, t_ready or t_end)]
    if t_trigger:
        phases += [("Steady, before the trigger", t_ready, t_trigger), ("After the trigger", t_trigger, t_end)]
    elif t_ready:
        phases.append(("After the ramp", t_ready, t_end))
    for name, a, b in phases:
        if not a or b <= a:
            continue
        lines.append("## %s (%d s)" % (name, b - a))
        sel = [r for r in rows if a <= r[0] < b]
        for k, c in sorted({(k, c) for _, k, c, _ in sel}):
            lines.append("- %s %s: %s" % (k, c, dist([ms for _, kk, cc, ms in sel if kk == k and cc == c])))
        al = [v for t, v in alive if a <= t < b]
        if al:
            lines.append("- alive subscriptions: min %d, last %d of %d" % (min(al), al[-1], dialogs))
        lines.append("")

    lines += ["## Per 5 minutes", "",
              "| Window | Alive (min) | Refresh 200 | Refresh 481 | 408 timeouts | New dialogs | Refresh p90 ms |",
              "|---|---|---|---|---|---|---|"]
    t = t_start
    while t < t_end:
        b = t + 300
        sel = [r for r in rows if t <= r[0] < b]
        al = [v for tt, v in alive if t <= tt < b]
        r200 = [ms for _, k, c, ms in sel if k == "refresh" and c.startswith("2")]
        lines.append("| %s | %s | %d | %d | %d | %d | %s |" % (
            time.strftime("%H:%M", time.localtime(t)), min(al) if al else "-", len(r200),
            sum(1 for _, k, c, _ in sel if k == "refresh" and c == "481"),
            sum(1 for _, k, c, _ in sel if c == "408"),
            sum(1 for _, k, c, _ in sel if k == "initial"), pct(r200, 90) if r200 else "-"))
        t = b
    lines.append("")

    t0 = t_trigger or t_ready or t_start
    after = [r for r in rows if r[0] >= t0]
    n408 = sum(1 for r in after if r[2] == "408")
    al_after = [v for tt, v in alive if tt >= t0]
    lines.append("## Verdict")
    low = min(al_after) if al_after else None
    codes = {}
    for r in rows:
        codes[r[2]] = codes.get(r[2], 0) + 1
    if not t_ready:
        lines.append("Load never came up (%s of %d subscriptions alive at the end; replies by code: %s). Fix "
                     "that first: 407/401 = credentials, 408 = no answer from the proxy (network/NAT), "
                     "489 = event rejected." % (alive[-1][1] if alive else 0, dialogs,
                                                 ", ".join("%s=%d" % kv for kv in sorted(codes.items()))))
    elif (low is not None and low < 0.8 * dialogs) or (after and n408 > 0.01 * len(after)):
        lines.append("Collapse reproduced: alive fell to %s of %d, %d of %d replies were 408 timeouts."
                     % (low, dialogs, n408, len(after)))
    else:
        lines.append("No collapse: alive stayed at or above %s of %d, %d timeouts. Raise --ramp-rate, lower "
                     "--expires (min 300) or add --blf, or cap presence CPU (README)." % (low, dialogs, n408))
    text = "\n".join(lines) + "\n"
    with open(os.path.join(out, "summary.md"), "w") as f:
        f.write(text)
    return text


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--creds", default=os.path.join(HERE, "users_qa_udp_n6071.secrets.csv"),
                    help="sipusername,password,udp:host:port per line")
    ap.add_argument("--max-users", type=int, default=0, help="use only the first N devices (0 = all)")
    ap.add_argument("--workers", type=int, default=0, help="sender processes (0 = 1 per 30k devices, max 8)")
    ap.add_argument("--minutes", type=float, default=45, help="total run time")
    ap.add_argument("--ramp-rate", type=float, default=0,
                    help="new subscriptions per s at start, all workers (0 = spread: create them over one "
                         "refresh period, so refreshes come evenly as in production)")
    ap.add_argument("--expires", type=int, default=600, help="Expires asked for; refresh at 90%%")
    ap.add_argument("--ha1", default="auto", choices=["auto", "yes", "no"],
                    help="password column is HA1 = md5(user:realm:password) (auto: when 32 hex chars)")
    ap.add_argument("--blf", type=int, default=3, help="BLF subscriptions per device (+1 MWI)")
    ap.add_argument("--blf-range", default="101-125", help="BLF target extensions")
    ap.add_argument("--proxies", type=int, default=3, help="fail-over copies to N proxy IPs")
    ap.add_argument("--failover", type=float, default=3.5, help="seconds without a final reply before fail-over")
    ap.add_argument("--dead-notify-pct", type=float, default=2, help="%% of devices that never answer NOTIFY")
    ap.add_argument("--local-ip", default="auto", help="address in Via/Contact (public IP if behind NAT)")
    ap.add_argument("--local-port", type=int, default=5060, help="first local UDP port; worker k uses +k")
    ap.add_argument("--trigger-after", type=float, default=-1,
                    help="seconds after all subscriptions are up (default: one refresh period, so a full "
                         "in-dialog cycle is seen first)")
    ap.add_argument("--trigger-cmd", default="", help="shell command run once, e.g. restart both presence nodes")
    ap.add_argument("--out", default=os.path.join(HERE, "out"))
    ap.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    ap.add_argument("--stop-when-broken", action="store_true",
                    help="stop as soon as presence is broken: for 60 s >5%% of replies are 408 or 481, "
                         "or (after all are up) alive < 80%%")
    a = ap.parse_args()

    if not os.path.exists(a.creds):
        sys.exit("credentials file not found: %s" % a.creds)
    devices, hosts = 0, set()
    with open(a.creds) as f:
        for ln in f:
            ln = ln.strip()
            if not ln or ln.startswith("#"):
                continue
            devices += 1
            hosts.add(ln.rsplit(",", 1)[-1])  # only the target column is looked at
            if a.max_users and devices >= a.max_users:
                break
    for h in hosts:
        host = h.split(":")[1] if h.count(":") >= 2 else h
        if not ALLOWED_HOST.search(host):
            sys.exit("target %s is not a qa/dev n2p.io host; refusing" % h)
    workers = a.workers or min(8, max(1, (devices + 29999) // 30000))
    dialogs = devices * (1 + a.blf)
    if a.ramp_rate <= 0:
        a.ramp_rate = max(1.0, dialogs / (0.9 * a.expires))
    if a.trigger_after < 0:
        a.trigger_after = 0.9 * a.expires
    out = os.path.join(a.out, time.strftime("%Y%m%d-%H%M%S"))
    os.makedirs(out, exist_ok=True)

    print("Proxy          : %s" % ", ".join(sorted(hosts)))
    print("Devices        : %d -> %d subscriptions (1 MWI + %d BLF to %s), %d worker(s)" % (
        devices, dialogs, a.blf, a.blf_range, workers))
    print("Phases         : 1) new dialogs at %.0f/s for %.0f s  2) in-dialog refreshes, each every %.0f s "
          "(~%.0f/s)  3) restart both presence nodes %.0f s after all are up -> 481 -> new dialogs" % (
              a.ramp_rate, dialogs / a.ramp_rate, a.expires * 0.9, dialogs / (a.expires * 0.9), a.trigger_after))
    print("Phone behaviour: digest auth, retransmit 0.5..4 s, 408 after 32 s, fail-over after %.1f s to %d proxy "
          "IPs, %.1f%% dead NOTIFY" % (a.failover, a.proxies, a.dead_notify_pct))
    print("Trigger        : %s" % ("'%s' %d s after all are up" % (a.trigger_cmd, a.trigger_after)
                                   if a.trigger_cmd else "none: restart both presence nodes by hand when told"))
    print("Run time       : %.0f min, output %s" % (a.minutes, out))
    if not a.yes and input("This sends real SIP load to the pod. Type yes to start: ").strip() != "yes":
        sys.exit("not started")

    procs = []
    for k in range(workers):
        wout = os.path.join(out, "w%d" % k)
        cmd = [sys.executable, os.path.join(HERE, "phones_proxy.py"), "--creds", a.creds, "--out", wout,
               "--shard", "%d/%d" % (k, workers), "--seed", str(27 + k),
               "--ip", a.local_ip, "--port", str(a.local_port + k),
               "--max-users", str((a.max_users + workers - 1) // workers if a.max_users else 0),
               "--blf", str(a.blf), "--blf-range", a.blf_range, "--expires", str(a.expires),
               "--ramp-rate", str(a.ramp_rate / workers), "--proxies", str(a.proxies),
               "--failover", str(a.failover), "--dead-notify-pct", str(a.dead_notify_pct), "--ha1", a.ha1]
        log = open(os.path.join(out, "w%d.log" % k), "w")
        procs.append(subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT))
    t_start = time.time()
    t_end = t_start + a.minutes * 60
    t_ready = t_trigger = None
    t_broken = None
    window = []  # (t, replies, failed) per check, last 60 s

    def stop(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)
    try:
        next_print = time.time() + 30
        while time.time() < t_end:
            time.sleep(1)
            dead = [p for p in procs if p.poll() is not None]
            if dead:
                sys.exit("a worker stopped (exit %s); see %s/w*.log" % (dead[0].returncode, out))
            alive, total, c = worker_stats(out)
            total = total or dialogs
            if not t_ready and alive >= 0.995 * total:
                t_ready = time.time()
                print("%s all %d subscriptions are up" % (time.strftime("%H:%M:%S"), total), flush=True)
                if not a.trigger_cmd:
                    print("%s steady in-dialog phase now; restart both presence nodes at the same time at %s"
                          % (time.strftime("%H:%M:%S"),
                             time.strftime("%H:%M:%S", time.localtime(t_ready + a.trigger_after))), flush=True)
            if t_ready and not a.trigger_cmd and not t_trigger and time.time() >= t_ready + a.trigger_after:
                t_trigger = time.time()
                print("%s >>> RESTART BOTH PRESENCE NODES NOW <<<" % time.strftime("%H:%M:%S"), flush=True)
            if t_ready and a.trigger_cmd and not t_trigger and time.time() >= t_ready + a.trigger_after:
                t_trigger = time.time()
                print("%s trigger: %s" % (time.strftime("%H:%M:%S"), a.trigger_cmd), flush=True)
                r = subprocess.run(a.trigger_cmd, shell=True)
                print("%s trigger exit %d" % (time.strftime("%H:%M:%S"), r.returncode), flush=True)
            if a.stop_when_broken:
                rep = sum(v for k, v in c.items() if k.startswith("rx_") and not k.startswith("rx_req_"))
                bad = sum(v for k, v in c.items() if k.endswith("_408") or (k.endswith("_481")))
                window.append((time.time(), rep, bad))
                window = [w for w in window if w[0] >= time.time() - 60]
                tot = sum(w[1] for w in window)
                fail = sum(w[2] for w in window)
                span = window[-1][0] - window[0][0] if window else 0
                low_alive = t_ready and alive < 0.8 * total
                if span >= 55 and ((tot > 500 and fail > 0.05 * tot) or low_alive):
                    t_broken = time.time()
                    print("%s BROKEN: last 60 s %d of %d replies were 408/481, alive %d/%d -> stopping"
                          % (time.strftime("%H:%M:%S"), fail, tot, alive, total), flush=True)
                    break
            if time.time() >= next_print:
                next_print = time.time() + 30
                ok = sum(v for k, v in c.items() if k.startswith("rx_") and k.endswith("_200"))
                g = lambda k: c.get(k, 0)
                print("%s alive %d/%d | last 10 s: NEW sent %d -> 200 %d, 408 %d | IN-DIALOG sent %d -> 200 %d, "
                      "481 %d, 408 %d | 407 again %d, other errors %s | retrans %d, failover %d"
                      % (time.strftime("%H:%M:%S"), alive, total,
                         g("tx_initial"), g("rx_initial_200"), g("rx_initial_408"),
                         g("tx_refresh"), g("rx_refresh_200"), g("rx_refresh_481"), g("rx_refresh_408"),
                         sum(v for k, v in c.items() if k.endswith("_407") or k.endswith("_401")),
                         ",".join("%s=%d" % (k[3:], v) for k, v in sorted(c.items())
                                  if k.startswith("rx_") and k[-3:].isdigit() and k[-3] in "3456"
                                  and k[-3:] not in ("401", "407", "408", "481")) or "0",
                         g("retrans"), g("failover_legs")), flush=True)
    except KeyboardInterrupt:
        print("stopping")
    finally:
        for p in procs:
            p.terminate()
        for p in procs:
            try:
                p.wait(10)
            except subprocess.TimeoutExpired:
                p.kill()
    print(summarize(out, t_start, t_ready, t_trigger, time.time(), dialogs))


if __name__ == "__main__":
    main()
