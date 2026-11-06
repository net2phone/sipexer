#!/usr/bin/env python3
"""Autonomous presence-wave search on QA pod 6071.

For each sipexer config: start the herd, restart BOTH presence nodes at start+RESTART_AT s
(only `systemctl restart kamailio`), then judge from Prometheus (via grafana2) whether CPU makes
waves: >= 3 bursts in a row with avg(a,b) CPU >= 90% and a gap with CPU <= 40% between them.
Fail fast on decay (2 weak bursts), flat load (one burst >= 15 min) or timeout; then next config.
Stateless evaluation (recomputed from Prometheus every minute), so a restart of this script resumes.
Files: $WAVES_DIR/orch/{state.json,heartbeat,orch.log,SUCCESS.md,DONE} (WAVES_DIR default /tmp/waves).
A fresh start (no state.json) runs S1 = the prod pattern: background load only, steady, restart, 10 waves.
"""
import collections
import datetime
import json
import os
import re
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))          # presence-repro/
WORK = KIT
SIPEXER = os.environ.get("SIPEXER_BIN", os.path.join(os.path.dirname(KIT), "sipexer"))
CSV = os.environ.get("CREDS_CSV", KIT + "/pod-repro/users.secrets.csv")    # never commit it
CREDS_NAME = os.path.basename(CSV)                                        # same file name in ~/loadgen on the load host
LOGIN = KIT + "/ssh/presence-login.sh"
PODREPRO = KIT + "/pod-repro/repro.py"
# Load clients run on the load host (Debian arm64, no VPN / endpoint agent) since 2026-11-06 00:xx ET.
LG = KIT + "/ssh/loadgen-login.sh"
REMOTE = True
RHOME = "loadgen"                       # ~/loadgen on the load host: sipexer, repro.py, phones_proxy.py, creds csv


def rsh(cmd, timeout=120):
    try:
        r = subprocess.run([LG, "--run", cmd], capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout
    except Exception as e:
        return 1, str(e)


def rstart(name, logname, cmdline):
    """start cmdline on the load host in its own process group, return 'r:<pid>'"""
    run = "%s/runs/%s" % (RHOME, name)
    c = "mkdir -p ~/%s && cd ~/%s && (setsid nohup %s > ~/%s/%s 2>&1 < /dev/null & echo PID $!)" % (run, RHOME, cmdline, run, logname)
    rc, out = rsh(c)
    m = re.search(r"PID (\d+)", out)
    if not m:
        raise RuntimeError("remote start failed: %s" % out[-300:])
    return "r:%s" % m.group(1)
DOMAIN = "sip.t2-n6071.qa.n2p.io"
HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.environ.get("WAVES_DIR", "/tmp/waves")
ORCH = BASE + "/orch"
STATE, HB, DONE = ORCH + "/state.json", ORCH + "/heartbeat", ORCH + "/DONE"
DS = "de13w5smkidc0a"
PA, PB = "i-04b6ea3a696584676", "i-002877283156bfa07"   # a = 10.131.213.239, b = 10.131.214.55
NODES = ["presence-6071-a", "presence-6071-b"]
RESTART_AT = 200          # s after sipexer start (ramp is rc/rate s)
EVAL_MAX = 3000           # s after restart, minimum


def eval_max(cfg):
    """room for the waves needed (+1.5 cycles) after the restart"""
    return max(EVAL_MAX, int((cfg.get("need", 3) + 1.5) * 0.9 * cfg["ex"]) + 300)


def restart_at(cfg):
    if cfg.get("noherd"):
        return cfg.get("settle", 300)       # background only: restart after it has run steady for this long
    return max(RESTART_AT, cfg["rc"] // cfg["rate"] + 60)
PEAK, TROUGH, WEAK = 90.0, 40.0, 75.0

# stagger = seconds between restarting node a and node b (prod Nov 3: 21 s) -> each node pulls a DMQ
# initial sync from a peer that still has all subscriptions (write-queue overflow, htable 60 KB groups,
# broken DMQ link) instead of both starting empty.
CONFIGS = [
    {"name": "E3", "rc": 40000, "rate": 400, "ex": 500, "drop": 0, "sockets": 8},
    {"name": "E4", "rc": 40000, "rate": 400, "ex": 500, "drop": 0, "sockets": 8, "stagger": 20},
    # prod scale: node 27 had ~230k subscriptions, Expires 1800, 20-min bursts; loop held only while presence
    # stayed at its CPU limit inside each burst (earlier sessions). Bigger herd + 18-min period.
    {"name": "E5", "rc": 100000, "rate": 500, "ex": 1200, "drop": 0, "sockets": 16, "stagger": 20},
    # pod-repro confirmation (user 2026-11-05): 30k devices x (1 MWI + 3 BLF) = 120k dialogs from 4 worker
    # sockets, herd at 500/s, 2% devices never answer NOTIFY (its default). rc = dialogs, sockets = workers.
    {"name": "P1", "tool": "podrepro", "devices": 30000, "rc": 120000, "rate": 500, "ex": 1200, "drop": 2, "sockets": 4, "stagger": 20},
    {"name": "E6", "rc": 100000, "rate": 500, "ex": 1200, "drop": 0, "sockets": 16},
    {"name": "E6b", "rc": 40000, "rate": 400, "ex": 500, "drop": 15, "sockets": 8, "stagger": 20},
    {"name": "E7", "rc": 40000, "rate": 400, "ex": 500, "drop": 30, "sockets": 8},
    {"name": "E8", "rc": 40000, "rate": 400, "ex": 500, "drop": 30, "sockets": 8, "stagger": 20},
    {"name": "E9", "rc": 60000, "rate": 600, "ex": 500, "drop": 30, "sockets": 12, "stagger": 20},
]
MAX_EXP = len(CONFIGS) + 10

# after the first SUCCESS: repeat it, confirm with pod-repro, and check whether the staggered restart matters
# User 2026-11-05: keep SUBSCRIBE traffic between the waves (a second sipexer, evenly spread = "base") and
# only call it a success after 10 waves in a row, so we know it is not recovering.
# prod quiet load: ~100 SUBSCRIBE/s per node normally, ~160/s on one node between storm bursts (14-16% CPU).
# Shortest Expires presence accepts (min_expires 300) -> refresh every 270 s -> 60k sessions = ~222/s for the pair.
BASE_LOAD = {"rc": 60000, "rate": 222, "rbi": 1000, "ex": 300, "sockets": 8}
# E5 (herd only) gave 4 waves and was recovering. User 2026-11-05: as close to prod as possible, no shortcuts,
# >= 10 waves, then repeat for confidence. Prod-calibrated settings:
#   dead-NAT phones (NOTIFY never answered): normal day 8-12k 481/h on a node of ~110k subscriptions -> ~3% (5% = top)
#   period: prod bursts every 20 min -> Expires 1333 (sipexer refreshes at 90%)
#   quiet load ~100 SUBSCRIBE/s per node -> BASE_LOAD 222/s for the pair
# W1 (background via sipexer, Expires 300) made the background its own 4.5-min loop -> not prod-like.
# Background now = pod-repro (light per dialog): 60k devices x (1 MWI + 3 BLF) = 240k dialogs, prod period
# (Expires 1333), spread evenly -> ~200 SUBSCRIBE/s for the pair (~100/s per node), 3% dead-NAT, own ports 5160+.
# BLF targets spread like prod (colleagues' extensions across ~20k accounts): 1000-99999 -> ~3 watchers per
# extension. 101-125 (runs up to W1b) put ~13k watchers on each of 25 extensions = 25 long hash chains in presence.
BLF_RANGE = "1000-99999"
BASE_PR = {"tool": "podrepro", "devices": 60000, "ex": 1333, "drop": 3, "workers": 4, "port": 5160, "warmup": 1200,
           "blf_range": BLF_RANGE}


# User 16:15 ET: keep the Mac, smaller scale, drop counter must stay flat (Oz extension limit). Cut packets/s,
# keep subscriptions: background with prod's Expires 1800 (refresh 27 min) holds many dialogs at a low rate.
def _w(name, rc, rate, drop, bg_devices, ex=1333, bg_ex=1800):
    return {"name": name, "rc": rc, "rate": rate, "ex": ex, "drop": drop, "sockets": 16, "stagger": 20,
            "need": 10, "active_sub": 300, "tuser_range": BLF_RANGE,
            "base": dict(BASE_PR, devices=bg_devices, ex=bg_ex, drop=drop, warmup=int(0.9 * bg_ex))}


SMALL = [
    _w("M1b", 50000, 250, 3, 45000),    # herd 50k, background 180k dialogs (~111/s for the pair)
    _w("M2", 35000, 175, 3, 30000),     # smaller still
]
# User 16:44 ET: Mac UDP buffers raised (recvspace 8 MB, maxsockbuf 16 MB) -> back to the prod-match load (W1c
# settings), drop guard on; if the client is invalid again, fall back to SMALL.
PRIMARY = [
    _w("W1e", 100000, 500, 3, 60000, bg_ex=1333),     # = W1c/W1d, now from the load host (no VPN, no endpoint agent)
    _w("W2", 115000, 575, 3, 60000, bg_ex=1333),      # all users in the herd
    _w("W3", 115000, 575, 5, 60000, bg_ex=1333),      # top of the prod dead-NAT range
]
REPEATS = 2                        # after a success, run the same settings this many more times


def extras(best):
    """The rest of the plan (artifact table), built from the first settings that reached 10 waves."""
    w4 = dict(best, name="W4", stagger=0)
    w5 = dict(best, name="W5", ex=1800)
    w6 = dict(best, name="W6", need=15)
    for c in (w4, w5, w6):
        c.pop("repeat_of", None)
        c["extra"] = True
    return [w4, w5, w6]


# pod-repro runs after W1 whatever the results (user 2026-11-05): W1 settings, its own client
P1_CFG = dict(PRIMARY[0], name="P1", tool="podrepro", devices=30000, rc=120000, rate=500, drop=2, sockets=4, extra=True)
# U1 = prod pattern without a pre-built herd: everything spread evenly over one 20-min period (sipexer 100k at
# 83/s + pod-repro 240k), restart, and see whether waves form by themselves (prod 20-min period > collapse length).
U1_CFG = dict(PRIMARY[0], name="U1", rate=83, uniform=True, extra=True)
ALWAYS = []   # 2026-11-06 user: "must!! same as in prod", "no cheating!" -> no pre-built herd runs (P1/U1/W*) any more
# Prod pattern (Prometheus Prod, node 27, Nov 3): per node ~100 SUBSCRIBE/s, ~300 KDMQ/s, ~100 NOTIFY/s, 1-2 PUBLISH/s,
# CPU ~9-10% for hours; both nodes replaced (B 21 s after A); right away ~200 SUBSCRIBE/s per node (481 + new), CPU
# 64 -> 81 -> 100% in 15 min, then 100% waves every ~20 min. The pod-repro background below gives the same per-node
# numbers on 6071 (verified 2026-11-05 18:20-18:35 ET), so S runs = background only, steady, restart, watch.
S_BASE = dict(BASE_PR, devices=60000, ex=1333, drop=3, warmup=1199)
S1_CFG = {"name": "S1", "noherd": True, "uniform": True, "extra": True, "settle": 300, "rc": 0, "rate": 1, "ex": 1333,
          "drop": 3, "sockets": 0, "stagger": 21, "need": 10, "active_sub": 400, "flat_s": 2400, "tuser_range": BLF_RANGE,
          "base": S_BASE}
S2_CFG = dict(S1_CFG, name="S2", cooldown=1500)        # repeat for confidence; 25 min pause first so the old subscriptions expire
# S3: same total load and shape, 100k of the 240k dialogs from sipexer (user: "at the end need sipexer too"), spread
S3_CFG = dict(U1_CFG, name="S3", stagger=21, need=10, active_sub=400, flat_s=2400, extra=True, uniform=True,
              base=dict(BASE_PR, devices=35000, ex=1333, drop=3, warmup=1199))
CONFIRM = PRIMARY


def config(i):
    if i < len(CONFIGS):
        return dict(CONFIGS[i])
    k = i - len(CONFIGS) + 1
    return {"name": "X%d" % k, "rc": min(40000 + 10000 * k, 80000), "rate": 400, "ex": 500,
            "drop": min(30 + 10 * k, 80), "sockets": 12, "stagger": 20}


def et(ts=None):
    ts = time.time() if ts is None else ts
    return (datetime.datetime.fromtimestamp(ts, datetime.timezone.utc) - datetime.timedelta(hours=5)).strftime("%H:%M:%S")


def log(kind, msg):
    print("%s %s %s" % (et(), kind, msg), flush=True)


def beat():
    with open(HB, "w") as f:
        f.write(str(int(time.time())))


def save(st):
    tmp = STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f, indent=1)
    os.replace(tmp, STATE)


def gapi(endpoint, method="GET", body=None, timeout=40):
    args = {"endpoint": endpoint, "method": method}
    if body is not None:
        args["body"] = json.dumps(body)
    out = subprocess.run(["python3", os.path.join(HERE, "gmcp.py"), "grafana_api_request", json.dumps(args)],
                         capture_output=True, text=True, timeout=timeout).stdout
    return json.loads(out)


def prom_range(q, s, e, step=60):
    for _ in range(3):
        try:
            ep = "/api/datasources/proxy/uid/%s/api/v1/query_range?" % DS + urllib.parse.urlencode(
                {"query": q, "start": int(s), "end": int(e), "step": step})
            return gapi(ep)["data"]["data"]["result"]
        except Exception:
            time.sleep(5)
    return None


def annotate(text, tags):
    try:
        gapi("/api/annotations", "POST", {"dashboardUID": "kamailio-vlitvinov", "time": int(time.time() * 1000),
                                          "tags": ["sipexer"] + tags, "text": text})
    except Exception:
        pass


POPEN = {}   # sipexer children started by this process: reap them, a zombie still answers kill -0


def alive(pid):
    if not pid:
        return False
    if isinstance(pid, str) and pid.startswith("r:"):
        rc, out = rsh("kill -0 %s 2>/dev/null && echo YES || echo NO" % pid[2:], timeout=60)
        return "YES" in out if rc == 0 else True     # ssh hiccup: assume alive, check again next time
    if pid in POPEN:
        return POPEN[pid].poll() is None
    return subprocess.run(["kill", "-0", str(pid)], capture_output=True).returncode == 0


def kill(pid):
    """TERM the whole process group (pod-repro has worker children), KILL after 30 s."""
    if isinstance(pid, str) and pid.startswith("r:"):
        p = pid[2:]
        rsh("kill -TERM -- -%s 2>/dev/null; kill -TERM %s 2>/dev/null; for i in $(seq 1 30); do kill -0 %s 2>/dev/null || break; sleep 1; done; "
            "kill -KILL -- -%s 2>/dev/null; true" % (p, p, p, p), timeout=90)
        return
    if not alive(pid):
        return
    subprocess.run(["kill", "-TERM", "--", "-%d" % pid], capture_output=True)
    subprocess.run(["kill", "-TERM", str(pid)], capture_output=True)
    for _ in range(30):
        if not alive(pid):
            return
        time.sleep(1)
    subprocess.run(["kill", "-KILL", "--", "-%d" % pid], capture_output=True)
    subprocess.run(["kill", "-KILL", str(pid)], capture_output=True)


def start_podrepro(cfg):
    run = os.path.join(BASE, cfg["name"])
    os.makedirs(run, exist_ok=True)
    minutes = (restart_at(cfg) + eval_max(cfg) + 600) / 60.0
    if REMOTE:
        cl = ("python3 -u repro.py --creds " + CREDS_NAME + " --max-users %d --workers %d --ramp-rate %d --expires %d "
              "--minutes %.0f --dead-notify-pct %s --blf-range %s --yes --out ~/%s/runs/%s/herd"
              % (cfg["devices"], cfg["sockets"], cfg["rate"], cfg["ex"], minutes, cfg.get("drop", 2), cfg.get("tuser_range", "101-125"), RHOME, cfg["name"]))
        with open(os.path.join(run, "cmd.txt"), "w") as f:
            f.write("(load host, ~/loadgen) " + cl + "\n")
        return rstart(cfg["name"], "podrepro.log", cl)
    cmd = ["python3", "-u", PODREPRO, "--max-users", str(cfg["devices"]), "--workers", str(cfg["sockets"]),
           "--ramp-rate", str(cfg["rate"]), "--expires", str(cfg["ex"]), "--minutes", "%.0f" % minutes,
           "--dead-notify-pct", str(cfg.get("drop", 2)), "--blf-range", cfg.get("tuser_range", "101-125"), "--yes", "--out", run]
    with open(os.path.join(run, "cmd.txt"), "w") as f:
        f.write(" ".join(cmd).replace(WORK + "/", "") + "\n")
    logf = open(os.path.join(run, "podrepro.log"), "w")
    p = subprocess.Popen(cmd, stdout=logf, stderr=subprocess.STDOUT, cwd=WORK, start_new_session=True)
    POPEN[p.pid] = p
    subprocess.Popen(["caffeinate", "-dims", "-w", str(p.pid)], start_new_session=True)
    return p.pid


def start_base(cfg):
    """Background load. pod-repro (spread over one refresh period) or a sipexer base (older runs)."""
    b = cfg["base"]
    if b.get("tool") != "podrepro":
        return start_sipexer(cfg, "base")
    run = os.path.join(BASE, cfg["name"], "base")
    os.makedirs(run, exist_ok=True)
    minutes = (b.get("warmup", 0) + restart_at(cfg) + eval_max(cfg) + 900) / 60.0
    if REMOTE:
        cl = ("python3 -u repro.py --creds " + CREDS_NAME + " --max-users %d --workers %d --ramp-rate 0 --expires %d "
              "--minutes %.0f --dead-notify-pct %s --local-port %d --blf-range %s --yes --out ~/%s/runs/%s/base"
              % (b["devices"], b["workers"], b["ex"], minutes, b.get("drop", 2), b.get("port", 5160), b.get("blf_range", "101-125"), RHOME, cfg["name"]))
        with open(os.path.join(BASE, cfg["name"], "cmd_base.txt"), "w") as f:
            f.write("(load host, ~/loadgen) " + cl + "\n")
        return rstart(cfg["name"], "base.log", cl)
    cmd = ["python3", "-u", PODREPRO, "--max-users", str(b["devices"]), "--workers", str(b["workers"]),
           "--ramp-rate", "0", "--expires", str(b["ex"]), "--minutes", "%.0f" % minutes,
           "--dead-notify-pct", str(b.get("drop", 2)), "--local-port", str(b.get("port", 5160)),
           "--blf-range", b.get("blf_range", "101-125"), "--yes", "--out", run]
    with open(os.path.join(BASE, cfg["name"], "cmd_base.txt"), "w") as f:
        f.write(" ".join(cmd).replace(WORK + "/", "") + "\n")
    logf = open(os.path.join(BASE, cfg["name"], "base.log"), "w")
    p = subprocess.Popen(cmd, stdout=logf, stderr=subprocess.STDOUT, cwd=WORK, start_new_session=True)
    POPEN[p.pid] = p
    subprocess.Popen(["caffeinate", "-dims", "-w", str(p.pid)], start_new_session=True)
    return p.pid


def start_sipexer(cfg, part="herd"):
    """part herd = the burst-making sessions; part base = the evenly spread background load (cfg["base"])."""
    if cfg.get("tool") == "podrepro" and part == "herd":
        return start_podrepro(cfg)
    run = os.path.join(BASE, cfg["name"])
    os.makedirs(run, exist_ok=True)
    sw_ms = (restart_at(cfg) + eval_max(cfg) + 600) * 1000
    c = cfg if part == "herd" else cfg["base"]
    rbi = c.get("rbi", 100)
    rb = max(1, c["rate"] * rbi // 1000)
    cmd = [SIPEXER, "--subscribe-session", "-sw", str(sw_ms), "-ex", str(c["ex"]), "-cb", "-ha1", "-acsv", CSV,
           "-xh", "Event: dialog", "-fd", DOMAIN, "-td", DOMAIN, "-failover", "3500",
           "-rc", str(c["rc"]), "-rb", str(rb), "-rbi", str(rbi),
           "-udp-sockets", str(c["sockets"]), "-spread-ips", "-vl", "1"]
    if c.get("drop"):
        cmd += ["-notify-drop-pct", str(c["drop"])]
    if c.get("tuser_range"):
        cmd += ["-tuser-range", c["tuser_range"]]
    name = "cmd.txt" if part == "herd" else "cmd_base.txt"
    if REMOTE:
        rcmd = ["./sipexer"] + cmd[1:]
        rcmd = [(CREDS_NAME if x == CSV else x) for x in rcmd]
        cl = " ".join(("'%s'" % x if (" " in x or ":" in x) else x) for x in rcmd)
        with open(os.path.join(run, name), "w") as f:
            f.write("(load host, ~/loadgen) " + cl + "\n")
        return rstart(cfg["name"], "sipexer.log" if part == "herd" else "base.log", cl)
    with open(os.path.join(run, name), "w") as f:
        f.write(" ".join(("'%s'" % x if " " in x else x) for x in cmd).replace(WORK + "/", "") + "\n")
    logf = open(os.path.join(run, "sipexer.log" if part == "herd" else "base.log"), "w")
    p = subprocess.Popen(cmd, stdout=logf, stderr=subprocess.STDOUT, cwd=WORK, start_new_session=True)
    POPEN[p.pid] = p
    subprocess.Popen(["caffeinate", "-dims", "-w", str(p.pid)], start_new_session=True)
    return p.pid


def restart_both(stagger=0):
    """Only `systemctl restart kamailio` on each node (user rule). stagger > 0: node a first, node b
    `stagger` seconds later. Returns True if both rc=0."""
    for attempt in range(3):
        subprocess.run([LOGIN], capture_output=True, text=True, timeout=90)   # (re)open shared ssh connections
        res = {}

        def one(n):
            try:
                r = subprocess.run([LOGIN, "--run", n, "sudo -n /usr/bin/systemctl restart kamailio"],
                                   capture_output=True, text=True, timeout=150)
                res[n] = r.returncode
            except Exception as e:
                res[n] = str(e)
        th = [threading.Thread(target=one, args=(n,)) for n in NODES]
        t0 = time.time()
        th[0].start()
        if stagger:
            th[0].join()
            time.sleep(max(0, stagger - (time.time() - t0)))
        th[1].start()
        for t in th:
            t.join()
        log("RESTART", "both presence nodes%s, attempt %d, %.0fs: %s" % (
            " (b %ds after a)" % stagger if stagger else "", attempt + 1, time.time() - t0, res))
        if all(v == 0 for v in res.values()):
            return True
        time.sleep(20)
    return False


class RemoteTail:
    """per-minute resubscribe counts from the herd sipexer log on the load host"""
    def __init__(self, name):
        self.path, self.off = "~/%s/runs/%s/sipexer.log" % (RHOME, name), None

    def read(self):
        codes, maxn = collections.Counter(), 0
        rc, out = rsh("f=%s; s=$(stat -c %%s $f 2>/dev/null || echo 0); echo SIZE $s; " % self.path +
                      ("tail -c +%d $f 2>/dev/null | head -c $((s-%d)) | grep -oE ' n=[0-9]+ after=-?[0-9]+' | "
                       "awk '{split($1,a,\"=\"); split($2,b,\"=\"); c[b[2]]++; if (a[2]+0>m) m=a[2]+0} END {for (k in c) print \"C\", k, c[k]; print \"M\", m+0}'"
                       % ((self.off or 0) + 1, self.off or 0)), timeout=90)
        size = None
        for line in out.splitlines():
            p = line.split()
            if p and p[0] == "SIZE":
                size = int(p[1])
            elif p and p[0] == "C" and len(p) == 3 and self.off is not None:
                codes[p[1]] += int(p[2])
            elif p and p[0] == "M" and self.off is not None:
                maxn = int(p[1])
        if size is not None:
            self.off = size
        return codes, maxn


def base_stats(path):
    """phone side of the pod-repro background: last 'alive' line of its log (counts are per 10 s)"""
    rc, out = rsh("grep ' alive ' %s 2>/dev/null | tail -n 1" % path, timeout=60)
    line = out.strip().splitlines()[-1] if out.strip() else ""
    m = re.search(r"alive (\d+)/(\d+)", line)
    if not m:
        return None
    v = {"alive": int(m.group(1)), "total": int(m.group(2))}
    for k, rx in (("new", r"NEW sent (\d+)"), ("new200", r"NEW sent \d+ -> 200 (\d+)"), ("new408", r"NEW sent \d+ -> 200 \d+, 408 (\d+)"),
                  ("ind", r"IN-DIALOG sent (\d+)"), ("ind200", r"IN-DIALOG sent \d+ -> 200 (\d+)"), ("r481", r"481 (\d+)"),
                  ("ind408", r"481 \d+, 408 (\d+)"), ("retrans", r"retrans (\d+)"), ("failover", r"failover (\d+)")):
        mm = re.search(rx, line)
        v[k] = int(mm.group(1)) if mm else 0
    return v


def phase_status(st, name, label):
    """one STATUS line per minute while waiting (warm-up, settle): CPU, SUB/s, phone side"""
    try:
        rows = series(time.time() - 240) or []
        cpu = ("CPU a %.0f%% b %.0f%% | SUB/s a %.0f b %.0f" % rows[-1][1:]) if rows else "CPU n/a"
        bst = base_stats(st["base_log"]) if (REMOTE and st.get("base_log")) else None
        ph = (" | phones alive %d/%d; last 10 s: new %d (200 %d), refresh %d (200 %d, 481 %d), retrans %d"
              % (bst["alive"], bst["total"], bst["new"], bst["new200"], bst["ind"], bst["ind200"], bst["r481"], bst["retrans"])) if bst else ""
        log("STATUS", "%s %s: %s%s" % (name, label, cpu, ph))
    except Exception as e:
        log("STATUS", "%s %s: status error %s" % (name, label, e))


def remote_client():
    """(UDP errors total = InErrors + SndbufErrors, available MB, herd RSS GB) on the load host"""
    rc, out = rsh("awk '/^Udp:/{n++; if(n==2) print \"UDP\", $4+$7}' /proc/net/snmp; free -m | awk '/^Mem:/{print \"AVAIL\", $7}'", timeout=60)
    udp = avail = None
    for line in out.splitlines():
        p = line.split()
        if p and p[0] == "UDP":
            udp = int(p[1])
        elif p and p[0] == "AVAIL":
            avail = int(p[1])
    return udp, avail


class SipexerTail:
    def __init__(self, path):
        self.path, self.f = path, None

    def read(self):
        if self.f is None:
            if not os.path.exists(self.path):
                return collections.Counter(), 0
            self.f = open(self.path, errors="replace")
        codes, maxn = collections.Counter(), 0
        for line in self.f:
            if " resubscribe n=" in line and "SSDIAG" in line:
                m = re.search(r" n=(\d+) after=(-?\d+)", line)
                if m:
                    codes[m.group(2)] += 1
                    maxn = max(maxn, int(m.group(1)))
        return codes, maxn


DROP_LIMIT = 500          # UDP full-buffer drops per minute on the Mac (Oz extension limit)
SWAP_LIMIT_GB = 10.0


def udp_drops():
    try:
        out = subprocess.run(["netstat", "-s", "-p", "udp"], capture_output=True, text=True, timeout=20).stdout
        m = re.search(r"(\d+) dropped due to full socket buffers", out)
        return int(m.group(1)) if m else None
    except Exception:
        return None


def swap_used_gb():
    try:
        out = subprocess.run(["sysctl", "-n", "vm.swapusage"], capture_output=True, text=True, timeout=10).stdout
        m = re.search(r"used = ([\d.]+)M", out)
        return float(m.group(1)) / 1024 if m else None
    except Exception:
        return None


def series(since):
    """[(t, cpu_a, cpu_b, sub_a, sub_b)] per minute since `since`."""
    now = time.time()
    cpu = prom_range('100*(1-avg by (instance_id)(irate(node_cpu_seconds_total{mode="idle",instance_id=~"%s|%s"}[3m])))'
                     % (PA, PB), since - 60, now)
    sub = prom_range('sum by (instance_id)(irate(kamailio_core_rcv_requests_subscribe{instance_id=~"%s|%s"}[3m]))'
                     % (PA, PB), since - 60, now)
    if cpu is None:
        return None
    rows = collections.defaultdict(dict)
    for r, key in ((cpu, "c"), (sub or [], "s")):
        for x in r:
            k = key + ("a" if x["metric"].get("instance_id") == PA else "b")
            for t, v in x["values"]:
                rows[int(float(t))][k] = float(v)
    return [(t, d.get("ca"), d.get("cb"), d.get("sa", 0.0), d.get("sb", 0.0)) for t, d in sorted(rows.items())
            if d.get("ca") is not None and d.get("cb") is not None and t >= since]


def bursts(rows, min_gap=3, active_sub=120):
    """Split minutes into bursts (pair SUB/s >= 120 or avg CPU >= 50). A quiet stretch only separates
    two bursts if it lasts >= min_gap minutes; shorter dips stay inside the burst."""
    segs = []                                     # [active, [rows]]
    for r in rows:
        t, ca, cb, sa, sb = r
        act = (sa + sb) >= active_sub or (ca + cb) / 2 >= 50
        if segs and segs[-1][0] == act:
            segs[-1][1].append(r)
        else:
            segs.append([act, [r]])
    merged = []
    for i, (act, rs) in enumerate(segs):
        short_gap = (not act) and len(rs) < min_gap and 0 < i < len(segs) - 1
        if (act or short_gap) and merged and merged[-1][0]:
            merged[-1][1].extend(rs)
        else:
            merged.append([act, list(rs)])
    out, gap_min = [], None
    for act, rs in merged:
        if act:
            pk = max(rs, key=lambda r: (r[1] + r[2]) / 2)
            out.append({"start": rs[0][0], "end": rs[-1][0], "peak": (pk[1] + pk[2]) / 2, "pa": pk[1], "pb": pk[2],
                        "sub": max(r[3] + r[4] for r in rs), "gap_min_before": gap_min, "open": True})
            gap_min = None
        else:
            if out:
                out[-1]["open"] = len(rs) < min_gap and rs is merged[-1][1]
            g = min((r[1] + r[2]) / 2 for r in rs)
            gap_min = g if gap_min is None else min(gap_min, g)
    if out and merged[-1][0]:
        out[-1]["open"] = True
    return out, gap_min


def herd_burst(b, cfg, start):
    """True if a burst starts in a herd refresh window: start + k*T (T = 0.9*Expires), -2 min .. +7 min."""
    if not cfg or not start or not cfg.get("base") or cfg.get("uniform"):
        return True                                  # no background load / uniform run: every burst counts
    T = 0.9 * cfg["ex"]
    # herd window k: [start + k*T - 2 min, start + k*T + 7 min]; a burst counts if it overlaps one
    # (the first herd wave can merge with the background's own 20-min storm right after the restart)
    k_lo = max(1, int((b["start"] - start - 420) // T))
    k_hi = int((b["end"] - start + 120) // T) + 1
    for k in range(k_lo, k_hi + 1):
        w0, w1 = start + k * T - 120, start + k * T + 420
        if b["start"] <= w1 and b["end"] >= w0:
            return True
    return False


def verdict(bs, restart, now, cfg=None, start=None):
    """SUCCESS / FAIL_* / None (keep going). With background load only herd-window bursts count."""
    allbs = bs
    # a closed burst that never reached 30% CPU is a SUBSCRIBE-rate blip (late re-subscribes), not a wave
    bs = [b for b in bs if herd_burst(b, cfg, start) and (b["open"] or b["peak"] >= 30)]
    for i, b in enumerate(bs):                       # gap before = min CPU since the previous herd burst
        if i > 0:
            gaps = [x["gap_min_before"] for x in allbs if bs[i - 1]["start"] < x["start"] <= b["start"] and x["gap_min_before"] is not None]
            b = dict(b, gap_min_before=min(gaps) if gaps else b["gap_min_before"])
            bs[i] = b
    streak, best = 0, 0
    for i, b in enumerate(bs):
        gap_ok = i == 0 or (b["gap_min_before"] is not None and b["gap_min_before"] <= TROUGH)
        if b["peak"] >= PEAK and gap_ok:
            streak += 1
        elif b["peak"] >= PEAK:
            streak = 1
        else:
            streak = 0 if not b["open"] else streak
        best = max(best, streak)
    if best >= ((cfg or {}).get("need", 3)):
        return "SUCCESS"
    closed = [b for b in bs if not b["open"]]
    if len(closed) >= 3 and all(b["peak"] < WEAK for b in closed[-2:]):
        return "FAIL_DECAY"
    flat_s = (cfg or {}).get("flat_s") or max(15 * 60, 1.25 * 0.9 * (cfg or {}).get("ex", 500))
    if allbs and allbs[-1]["open"] and allbs[-1]["end"] - allbs[-1]["start"] >= flat_s:
        return "FAIL_FLAT"
    if not bs and now - restart > max(900, 1.3 * 0.9 * (cfg or {}).get("ex", 500)):
        return "FAIL_NOLOAD"
    if now - restart > (eval_max(cfg) if cfg else EVAL_MAX):
        return "FAIL_TIMEOUT"
    return None


def describe(bs, cfg=None, start=None):
    out, n = [], 0
    for b in bs:
        if not b["open"] and b["peak"] < 30:
            continue                                  # rate blip, not a wave (see verdict)
        herd = herd_burst(b, cfg, start)
        n += 1 if herd else 0
        out.append("%s %s-%s peak a/b %.0f/%.0f%% SUB/s %.0f gapmin %s%s" % (
            "#%d" % n if herd else "bg", et(b["start"])[:5], et(b["end"])[:5], b["pa"], b["pb"], b["sub"],
            "-" if b["gap_min_before"] is None else "%.0f%%" % b["gap_min_before"], " (open)" if b["open"] else ""))
    return "; ".join(out)


def run_experiment(st):
    cfg = st["cfg"]
    name = cfg["name"]
    if st["phase"] == "new":
        kill(st.get("pid"))
        kill(st.get("base_pid"))
        cd = cfg.get("cooldown", 0)
        if cd:
            if "cooldown_until" not in st:          # survives an orchestrator restart
                st["cooldown_until"] = int(time.time()) + cd
                save(st)
                log("PHASE", "%s cooldown %d s: presence drops the previous run's subscriptions before the warm-up" % (name, cd))
            last_st = 0
            while time.time() < st["cooldown_until"]:
                beat()
                if time.time() - last_st >= 60:
                    last_st = time.time()
                    phase_status(dict(st, base_log=None), name, "cooldown, warm-up in %d min" % ((st["cooldown_until"] - time.time()) // 60))
                time.sleep(5)
        if REMOTE:
            rsh("mkdir -p ~/%s/runs && cd ~/%s/runs && ls -1t | tail -n +2 | xargs -r rm -rf; df -h ~ | tail -1" % (RHOME, RHOME))
        base_pid = start_base(cfg) if cfg.get("base") else None
        st.update(pid=None, base_pid=base_pid, base_start=int(time.time()), start=None, restart=None,
                  phase="warmup" if base_pid else "herd",
                  base_log=("~/%s/runs/%s/base.log" % (RHOME, name)) if (REMOTE and base_pid) else None)
        save(st)
        if base_pid:
            b = cfg["base"]
            log("PHASE", "%s background load pid %s (%s): %s, Expires %d, dead-NAT %d%%, warm-up %d s" % (
                name, base_pid, b.get("tool", "sipexer"),
                "%d devices x 4 dialogs, spread" % b["devices"] if b.get("tool") == "podrepro" else "%d sessions at %d/s" % (b["rc"], b["rate"]),
                b["ex"], b.get("drop", 0), b.get("warmup", 0)))
    if st["phase"] == "warmup":
        last_st = 0
        while time.time() < st["base_start"] + cfg["base"].get("warmup", 0):
            beat()
            if not alive(st["base_pid"]):
                log("ERROR", "%s: background load exited during warm-up" % name)
                return "FAIL_BASE_DIED"
            if time.time() - last_st >= 60:
                last_st = time.time()
                phase_status(st, name, "warm-up, restart in %d min" % ((st["base_start"] + cfg["base"].get("warmup", 0) + (restart_at(cfg) if cfg.get("noherd") else 0) - time.time()) // 60))
            time.sleep(5)
        st["phase"] = "herd"
        save(st)
    if st["phase"] == "herd" and cfg.get("noherd"):
        st.update(pid=st["base_pid"], start=int(time.time()), restart=None, phase="ramp")
        save(st)
        log("PHASE", "%s no herd (prod pattern): background only, steady, restart in %d s, b %ds after a" % (name, restart_at(cfg), cfg.get("stagger", 0)))
        annotate("%s: background only (prod pattern), steady" % name, [name])
    if st["phase"] == "herd":
        pid = start_sipexer(cfg)
        st.update(pid=pid, start=int(time.time()), restart=None, phase="ramp")
        save(st)
        log("PHASE", "%s start pid %s (%s): %d dialogs at %d/s, Expires %d, notify-drop %d%%, sockets %d, restart stagger %ds"
            % (name, pid, cfg.get("tool", "sipexer"), cfg["rc"], cfg["rate"], cfg["ex"], cfg.get("drop", 0), cfg["sockets"], cfg.get("stagger", 0)))
        annotate("%s start: %d sessions at %d/s, Expires %d, notify-drop %d%%" % (name, cfg["rc"], cfg["rate"], cfg["ex"], cfg.get("drop", 0)), [name])
    if st["phase"] == "ramp":
        last_st = 0
        while time.time() < st["start"] + restart_at(cfg):
            beat()
            if not alive(st["pid"]):
                return "FAIL_SIPEXER_DIED"
            if time.time() - last_st >= 60:
                last_st = time.time()
                phase_status(st, name, "steady, restart in %d min" % ((st["start"] + restart_at(cfg) - time.time()) // 60))
            time.sleep(5)
        annotate("%s: restart both presence nodes%s" % (name, " (b %ds after a)" % cfg.get("stagger", 0) if cfg.get("stagger") else ""), [name, "restart"])
        if not restart_both(cfg.get("stagger", 0)):
            log("ERROR", "%s: presence restart failed 3 times (VPN/ssh?) - waiting 5 min and retrying" % name)
            time.sleep(300)
            return "RETRY"
        st.update(restart=int(time.time()), phase="eval")
        save(st)
        log("PHASE", "%s eval from %s" % (name, et(st["restart"])))
    tail = RemoteTail(name) if REMOTE else SipexerTail(os.path.join(BASE, name, "sipexer.log"))
    tail.read()                                  # skip history; per-minute counts from now on
    last_n_bursts, empty, tick, minute = -1, 0, time.time(), 0
    last_drops, bad_minutes = (remote_client()[0] if REMOTE else udp_drops()), 0
    while True:
        beat()
        if not alive(st["pid"]):
            return "FAIL_SIPEXER_DIED"
        tick += 60
        time.sleep(max(0, tick - time.time()))
        beat()
        minute += 1
        codes, maxn = tail.read()
        if REMOTE:
            d_now, avail = remote_client()
            swap = None
        else:
            d_now, swap = udp_drops(), swap_used_gb()
            avail = None
        dpm = (d_now - last_drops) if (d_now is not None and last_drops is not None) else -1
        last_drops = d_now
        bad_minutes = bad_minutes + 1 if dpm > DROP_LIMIT else 0
        log("CLIENT", "%s %s UDP errors/min %d, %s" % (name, "load host" if REMOTE else "Mac", dpm,
            ("available %.1f GB" % (avail / 1024.0)) if avail is not None else ("swap %.1f GB" % (swap if swap is not None else -1))))
        if bad_minutes >= 2 or (swap is not None and swap > SWAP_LIMIT_GB) or (avail is not None and avail < 2048):
            log("WATCHDOG", "%s: client not valid (UDP errors/min %d) - stopping run" % (name, dpm))
            return "INVALID_CLIENT"
        rows = series(st["restart"])
        if rows is None or not rows:
            empty += 1
            if empty >= 3:
                log("WATCHDOG", "%s: no Prometheus data for %d min" % (name, empty))
            continue
        empty = 0
        t, ca, cb, sa, sb = rows[-1]
        try:
            if REMOTE:
                rc_, out_ = rsh("ps -o rss= -p %s" % str(st["pid"])[2:], timeout=60)
                rss = int(out_.strip() or 0) / 1048576
            else:
                rss = int(subprocess.run(["ps", "-o", "rss=", "-p", str(st["pid"])], capture_output=True, text=True).stdout.strip() or 0) / 1048576
        except Exception:
            rss = -1
        bst = base_stats(st["base_log"]) if (REMOTE and st.get("base_log")) else None
        phones = (" | phones alive %d/%d; last 10 s: new %d (200 %d, 408 %d), refresh %d (200 %d, 481 %d, 408 %d), retrans %d, failover %d"
                  % (bst["alive"], bst["total"], bst["new"], bst["new200"], bst["new408"], bst["ind"], bst["ind200"], bst["r481"],
                     bst["ind408"], bst["retrans"], bst["failover"])) if bst else ""
        if cfg.get("noherd"):
            log("STATUS", "%s CPU a %.0f%% b %.0f%% | SUB/s a %.0f b %.0f%s" % (name, ca, cb, sa, sb, phones))
        else:
            log("STATUS", "%s CPU a %.0f%% b %.0f%% | SUB/s a %.0f b %.0f | retries/min %d (481 %d, timeout %d, 408 %d, max n=%d) | sipexer %.1f GB%s"
                % (name, ca, cb, sa, sb, sum(codes.values()), codes.get("481", 0), codes.get("-1127", 0), codes.get("408", 0), maxn, rss, phones))
        if st.get("base_pid") and minute % 5 == 0:
            log("STATUS", "%s background load alive %s" % (name, alive(st["base_pid"])))
        if rss > 24:
            log("WATCHDOG", "%s: sipexer uses %.1f GB" % (name, rss))
        bs, _ = bursts(rows, active_sub=cfg.get("active_sub", 120))
        closed = len([b for b in bs if not b["open"]])
        if closed != last_n_bursts:
            last_n_bursts = closed
            log("BURSTS", "%s: %s" % (name, describe(bs, cfg, st["start"]) or "none yet"))
        if minute % 10 == 0:
            log("HEARTBEAT", "%s alive, %d min since restart, bursts: %s" % (name, (time.time() - st["restart"]) // 60, describe(bs, cfg, st["start"]) or "none"))
        v = verdict(bs, st["restart"], time.time(), cfg, st["start"])
        if v:
            log("RESULT", "%s %s | %s" % (name, v, describe(bs, cfg, st["start"])))
            return v


def success_report(st, bs_text, path=None):
    cfg, name = st["cfg"], st["cfg"]["name"]
    hfile = os.path.join(BASE, name, "cmd.txt")
    cmd = open(hfile).read().strip() if os.path.exists(hfile) else "(no herd: background load only)"
    bfile = os.path.join(BASE, name, "cmd_base.txt")
    if os.path.exists(bfile):
        cmd += "\n\nBackground load:\n\n    " + open(bfile).read().strip()
    with open(path or (ORCH + "/SUCCESS.md"), "w") as f:
        f.write("# Presence CPU waves reproduced on 6071 (%s)\n\nConfig: %s\n\nCommand:\n\n    %s\n\nRestart both presence nodes at %s ET (start + %ds).\n\nBursts: %s\n"
                % (name, json.dumps(cfg), cmd, et(st["restart"]), RESTART_AT, bs_text))


def main():
    os.makedirs(ORCH, exist_ok=True)
    beat()
    st = json.load(open(STATE)) if os.path.exists(STATE) else {"idx": 0, "post": True, "cfg": dict(S1_CFG), "queue": [], "results": [],
                                                              "phase": "new", "always_done": [], "extras_queued": True}
    log("PHASE", "orchestrator up (pid %d), state %s/%s" % (os.getpid(), st["cfg"]["name"], st["phase"]))
    while not os.path.exists(DONE):
        try:
            if st["phase"] in ("new", "warmup", "herd", "ramp", "eval"):
                if st["phase"] in ("ramp", "eval") and not alive(st.get("pid")):
                    v = "FAIL_SIPEXER_DIED"
                elif st["phase"] == "warmup" and not alive(st.get("base_pid")):
                    v = "FAIL_BASE_DIED"
                else:
                    v = run_experiment(st)
                if v == "RETRY":
                    st["phase"] = "new"
                    save(st)
                    continue
                rows = series(st["restart"]) if st.get("restart") else []
                bs_text = describe(bursts(rows or [], active_sub=st["cfg"].get("active_sub", 120))[0], st["cfg"], st.get("start"))
                annotate("%s: %s" % (st["cfg"]["name"], v), [st["cfg"]["name"], "result"])
                if st.get("post") is not None:
                    name = st["cfg"]["name"]
                    log("RESULT", "confirmation %s: %s | %s" % (name, v, bs_text))
                    kill(st.get("pid"))
                    kill(st.get("base_pid"))
                    results = st.get("results", []) + [{"name": name, "verdict": v, "bursts": bs_text}]
                    if v == "SUCCESS":
                        success_report(st, bs_text, ORCH + "/SUCCESS_%s.md" % name)
                    queue = list(st.get("queue", []))
                    if st["cfg"].get("extra"):
                        pass                       # plan rows after the repeats: just go on
                    elif not st["cfg"].get("repeat_of"):
                        if v == "SUCCESS":
                            queue = [dict(st["cfg"], name="%sr%d" % (name, k), repeat_of=name) for k in range(1, REPEATS + 1)]
                        else:
                            names = [c["name"] for c in PRIMARY]
                            i = names.index(name) if name in names else len(PRIMARY)
                            rest = [dict(c) for c in PRIMARY[i + 1:]]
                            if v == "INVALID_CLIENT":
                                rest = [dict(c) for c in SMALL]                   # the Mac cannot carry it: go smaller
                            queue = rest
                    done = set(st.get("always_done", []))
                    if name in [c["name"] for c in ALWAYS]:
                        done.add(name)
                        if name == "P1" and v == "SUCCESS":
                            queue = [dict(P1_CFG, name="P1r")] + queue
                    else:
                        missing = [dict(c) for c in ALWAYS if c["name"] not in done and not any(q.get("name") == c["name"] for q in queue)]
                        queue = missing + queue
                    st["always_done"] = sorted(done)
                    if not queue and not st.get("extras_queued"):
                        wins = [r["name"] for r in results if r["verdict"] == "SUCCESS" and r["name"] in [c["name"] for c in PRIMARY]]
                        best = next((c for c in PRIMARY if c["name"] in wins), None)
                        if best:
                            queue = extras(best)
                            st["extras_queued"] = True
                            log("PHASE", "plan: extras from %s: %s" % (best["name"], [c["name"] for c in queue]))
                    if not queue:
                        summary = "; ".join("%s %s" % (r["name"], r["verdict"]) for r in results)
                        open(DONE, "w").write(summary + "\n")
                        log("SUCCESS" if any(r["verdict"] == "SUCCESS" for r in results) else "RESULT", "all runs done: " + summary)
                        st.update(results=results, queue=[], phase="done")
                        save(st)
                        break
                    st = {"idx": st["idx"], "post": True, "queue": queue[1:], "results": results, "cfg": queue[0], "phase": "new",
                          "extras_queued": st.get("extras_queued", False), "always_done": st.get("always_done", [])}
                    save(st)
                    log("PHASE", "next %s in 60 s (queue after it: %s)" % (st["cfg"]["name"], [c["name"] for c in st["queue"]] or "none"))
                    for _ in range(12):
                        beat()
                        time.sleep(5)
                    continue
                if v == "SUCCESS":
                    success_report(st, bs_text)
                    st["phase"] = "showcase"
                    save(st)
                    log("SUCCESS", "%s - keeping sipexer running to its end to show more waves" % st["cfg"]["name"])
                    continue
                kill(st.get("pid"))
                kill(st.get("base_pid"))
                nxt = st["idx"] + 1
                if nxt >= MAX_EXP:
                    log("RESULT", "all %d configs tried without success" % MAX_EXP)
                    open(DONE, "w").write("gave up\n")
                    break
                st = {"idx": nxt, "cfg": config(nxt), "phase": "new"}
                save(st)
                log("PHASE", "next config %s in 30 s" % st["cfg"]["name"])
                for _ in range(6):
                    beat()
                    time.sleep(5)
            elif st["phase"] == "showcase":
                while alive(st.get("pid")):
                    beat()
                    rows = series(st["restart"]) or []
                    log("HEARTBEAT", "showcase %s: %s" % (st["cfg"]["name"], describe(bursts(rows)[0])))
                    for _ in range(12):
                        beat()
                        time.sleep(50)
                        if not alive(st.get("pid")):
                            break
                log("SUCCESS", "showcase of %s over: see %s/SUCCESS.md; starting confirmation runs" % (st["cfg"]["name"], ORCH))
                st = {"idx": st["idx"], "post": True, "queue": [dict(c) for c in PRIMARY[1:]], "results": [], "cfg": dict(PRIMARY[0]), "phase": "new"}
                save(st)
                continue
        except Exception as e:
            log("ERROR", "%s: %s" % (type(e).__name__, str(e)[:200]))
            traceback.print_exc()
            time.sleep(30)


if __name__ == "__main__":
    main()
