#!/usr/bin/env python3
"""Phones that subscribe through the real sipproxy with digest auth (pod repro).

Credentials file (one device per line, no header):  sipusername,password,udp:host:port
Each device keeps 1 MWI subscription (From=To=username, Event: message-summary) and N BLF
subscriptions (Event: dialog) to extensions from --blf-range. Behaviour like Polycom/Yealink:
  * 401/407 -> answers the challenge once, then reuses the nonce (nc+1) on later requests
  * UDP retransmissions 0.5 s doubling to 4 s, 408 after 32 s
  * fail-over: after --failover s without a final reply, a copy goes to the next IP of the
    proxy host name (the sipproxies behind sip.<pod> DNS)
  * in-dialog refreshes follow the Record-Route set and the Contact from the 200
  * 481 / 408 -> new dialog (new Call-ID) after 0.5-3 s; other errors -> retry in 30-90 s
  * answers NOTIFY (200) and the proxies' OPTIONS pings; --dead-notify-pct never answer NOTIFY
Output in --out: requests.csv and gen-stats.jsonl (same format as phones.py).
Standard library only (Python 3.8+), macOS and Linux.
"""
import argparse
import asyncio
import hashlib
import heapq
import json
import os
import random
import re
import socket
import string
import sys
import time

CRLF = "\r\n"
BRANCH_RE = re.compile(r"branch=([^;,\s]+)")
TAG_RE = re.compile(r";\s*tag=([^;>\s]+)")
URI_RE = re.compile(r"<([^>]+)>")
COMPACT = {"v": "via", "f": "from", "t": "to", "i": "call-id", "m": "contact",
           "l": "content-length", "c": "content-type", "o": "event", "k": "supported"}


def rid(n=10):
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=n))


def md5(s):
    return hashlib.md5(s.encode()).hexdigest()


def parse(data):
    text = data.decode("utf-8", "replace")
    head, _, body = text.partition("\r\n\r\n")
    lines = head.split("\r\n")
    hdrs = {}
    for ln in lines[1:]:
        name, _, val = ln.partition(":")
        name = COMPACT.get(name.strip().lower(), name.strip().lower())
        hdrs.setdefault(name, []).append(val.strip())
    return lines[0], hdrs, body


def split_uris(values):
    """Record-Route / Route header values -> list of <uri> strings (handles comma lists)."""
    out = []
    for v in values:
        out += re.findall(r"<[^>]+>", v)
    return out


def parse_challenge(value):
    v = value.strip()
    if v.lower().startswith("digest"):
        v = v[6:]
    params = dict((k.lower(), (q if q else t)) for k, q, t in
                  re.findall(r'(\w+)\s*=\s*(?:"([^"]*)"|([^\s,]+))', v))
    return params


HA1_MODE = "auto"  # auto | yes | no: is the password column already HA1 = md5(user:realm:password)?


def digest_header(ch, user, pw, method, uri):
    """RFC 2617 Digest (MD5, qop auth or none). ch = challenge dict with a running 'nc'."""
    realm, nonce = ch.get("realm", ""), ch.get("nonce", "")
    is_ha1 = HA1_MODE == "yes" or (HA1_MODE == "auto" and re.fullmatch(r"[0-9a-f]{32}", pw or "") is not None)
    ha1 = pw if is_ha1 else md5("%s:%s:%s" % (user, realm, pw))
    ha2 = md5("%s:%s" % (method, uri))
    parts = ['username="%s"' % user, 'realm="%s"' % realm, 'nonce="%s"' % nonce, 'uri="%s"' % uri]
    qop = ch.get("qop", "")
    if "auth" in [q.strip() for q in qop.split(",")]:
        ch["nc"] = ch.get("nc", 0) + 1
        nc = "%08x" % ch["nc"]
        cnonce = rid(16)
        resp = md5("%s:%s:%s:%s:auth:%s" % (ha1, nonce, nc, cnonce, ha2))
        parts += ['response="%s"' % resp, "qop=auth", "nc=%s" % nc, 'cnonce="%s"' % cnonce]
    else:
        parts.append('response="%s"' % md5("%s:%s:%s" % (ha1, nonce, ha2)))
    parts.append("algorithm=MD5")
    if ch.get("opaque"):
        parts.append('opaque="%s"' % ch["opaque"])
    return "Digest " + ", ".join(parts)


def pct(vals, p):
    if not vals:
        return None
    vals = sorted(vals)
    k = (len(vals) - 1) * p / 100.0
    f = int(k)
    c = min(f + 1, len(vals) - 1)
    return round(vals[f] + (vals[c] - vals[f]) * (k - f), 2)


class Device:
    __slots__ = ("user", "pw", "host", "port", "chal", "chal_hdr")

    def __init__(self, user, pw, host, port):
        self.user, self.pw, self.host, self.port = user, pw, host, port
        self.chal = None
        self.chal_hdr = None


class Dialog:
    __slots__ = ("did", "dev", "event", "ruser", "callid", "ftag", "ttag", "cseq", "rtarget",
                 "routes", "alive", "gen", "exp")

    def __init__(self, did, dev, event, ruser):
        self.did, self.dev, self.event, self.ruser = did, dev, event, ruser
        self.callid = self.ftag = self.ttag = self.rtarget = None
        self.routes = []
        self.cseq = 0
        self.alive = False
        self.gen = 0
        self.exp = None  # Expires to ask for (raised by a 423 Min-Expires)


class Tx:
    __slots__ = ("kind", "dialog", "gen", "t0", "legs", "done", "retrans", "auth_tries", "dests", "chal", "chal_hdr")

    def __init__(self, kind, dialog, dests):
        self.kind, self.dialog, self.gen = kind, dialog, dialog.gen
        self.t0 = time.time()
        self.legs = []
        self.done = False
        self.retrans = 0
        self.auth_tries = 0
        self.dests = dests
        self.chal = None      # each transaction answers its own challenge (no nonce reuse:
        self.chal_hdr = None  # the proxy checks nc order and nonce_expire is 60 s)


class Leg:
    __slots__ = ("tx", "branch", "dest", "t0", "handle", "interval", "data")

    def __init__(self, tx, branch, dest, data):
        self.tx, self.branch, self.dest, self.data = tx, branch, dest, data
        self.t0 = time.time()
        self.handle = None
        self.interval = 0.5


class Phones(asyncio.DatagramProtocol):
    def __init__(self, a):
        self.a = a
        self.loop = asyncio.get_event_loop()
        self.transport = None
        self.devices, self.dialogs = [], []
        self.by_callid, self.legs = {}, {}
        self.heap = []
        self.c, self.lat = {}, {}
        self.dns = {}
        self.dead = set()
        os.makedirs(a.out, exist_ok=True)
        self.reqf = open(os.path.join(a.out, "requests.csv"), "a", buffering=1 << 16)
        if self.reqf.tell() == 0:
            self.reqf.write("t,kind,code,leg_ms,phone_ms,node,legs,retrans\n")
        self.statf = open(os.path.join(a.out, "gen-stats.jsonl"), "a", buffering=1)
        self.load()

    # ------------------------------------------------------------------ setup
    def load(self):
        a = self.a
        rnd = random.Random(a.seed)
        lo, hi = [int(x) for x in a.blf_range.split("-")]
        k, n = [int(x) for x in a.shard.split("/")]
        with open(a.creds) as f:
            for i, ln in enumerate(f):
                ln = ln.strip()
                if not ln or ln.startswith("#") or i % n != k:
                    continue
                user, pw, target = ln.split(",", 2)
                proto, host, port = target.split(":")
                self.devices.append(Device(user, pw, host, int(port)))
                if a.max_users and len(self.devices) >= a.max_users:
                    break
        did = 0
        for i, dv in enumerate(self.devices):
            if a.mwi:
                self.dialogs.append(Dialog(did, i, "message-summary", dv.user))
                did += 1
            for ext in rnd.sample(range(lo, hi + 1), min(a.blf, hi - lo + 1)):
                self.dialogs.append(Dialog(did, i, "dialog", str(ext)))
                did += 1
        self.dead = set(rnd.sample(range(len(self.devices)), int(len(self.devices) * a.dead_notify_pct / 100)))
        order = list(range(len(self.dialogs)))
        rnd.shuffle(order)
        now = time.time()
        for i, d in enumerate(order):
            heapq.heappush(self.heap, (now + 2 + i / a.ramp_rate, d, 0, "initial"))
        print("devices=%d dialogs=%d ramp=%.0fs dead_notify=%d" % (
            len(self.devices), len(self.dialogs), len(self.dialogs) / a.ramp_rate, len(self.dead)), flush=True)

    def resolve(self, host, port):
        key = (host, port)
        if key not in self.dns:
            try:
                infos = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_DGRAM)
                ips = sorted({i[4][0] for i in infos})
            except OSError:
                ips = []
            self.dns[key] = [(ip, port) for ip in ips]
            print("resolved %s:%d -> %s" % (host, port, ", ".join(ip for ip, _ in self.dns[key])), flush=True)
        return self.dns[key]

    def cnt(self, k, n=1):
        self.c[k] = self.c.get(k, 0) + n

    # --------------------------------------------------------------- messages
    def build(self, d, branch, initial, tx=None):
        dv = self.devices[d.dev]
        dom = dv.host
        ruri = ("sip:%s@%s" % (d.ruser, dom)) if initial else d.rtarget
        to = "<sip:%s@%s>" % (d.ruser, dom) + ("" if initial or not d.ttag else ";tag=" + d.ttag)
        m = ["SUBSCRIBE %s SIP/2.0" % ruri,
             "Via: SIP/2.0/UDP %s:%d;rport;branch=%s" % (self.a.ip, self.a.port, branch),
             "Max-Forwards: 70"]
        if not initial:
            m += ["Route: " + r for r in d.routes]
        m += ["From: <sip:%s@%s>;tag=%s" % (dv.user, dom, d.ftag),
              "To: " + to,
              "Call-ID: " + d.callid,
              "CSeq: %d SUBSCRIBE" % d.cseq,
              "Contact: <sip:%s@%s:%d;transport=udp>" % (dv.user, self.a.ip, self.a.port),
              "Accept: " + ("application/simple-message-summary" if d.event == "message-summary"
                            else "application/dialog-info+xml"),
              "Event: " + d.event,
              "Expires: %d" % (d.exp or self.a.expires),
              "User-Agent: presence-repro/1.0"]
        if tx is not None and tx.chal:
            m.append("%s: %s" % (tx.chal_hdr, digest_header(tx.chal, dv.user, dv.pw, "SUBSCRIBE", ruri)))
        m.append("Content-Length: 0")
        return (CRLF.join(m) + CRLF + CRLF).encode()

    def dests_for(self, d, initial):
        dv = self.devices[d.dev]
        if initial or not d.routes:
            return self.resolve(dv.host, dv.port)
        u = URI_RE.search(d.routes[0])
        uri = u.group(1) if u else d.routes[0]
        hp = uri.split(":", 1)[1].split(";")[0].split("@")[-1]
        host, _, port = hp.partition(":")
        return self.resolve(host, int(port) if port else 5060)

    # ------------------------------------------------------------ transactions
    def start(self, d, initial):
        if initial:
            d.gen += 1
            if d.callid:
                self.by_callid.pop(d.callid, None)
            d.callid = "%s@repro" % rid(18)
            d.ftag, d.ttag, d.rtarget, d.routes = rid(8), None, None, []
            d.cseq = 1
            d.alive = False
            self.by_callid[d.callid] = d
        else:
            d.cseq += 1
        dests = self.dests_for(d, initial)
        if not dests:
            self.cnt("dns_fail")
            heapq.heappush(self.heap, (time.time() + 30, d.did, d.gen, "initial"))
            return
        tx = Tx("initial" if initial else "refresh", d, dests)
        self.cnt("tx_" + tx.kind)
        self.add_leg(tx, random.randrange(len(dests)) if initial else 0)

    def add_leg(self, tx, idx):
        dest = tx.dests[idx % len(tx.dests)]
        branch = "z9hG4bK" + rid(14)
        leg = Leg(tx, branch, dest, self.build(tx.dialog, branch, tx.kind == "initial", tx))
        tx.legs.append(leg)
        self.legs[branch] = leg
        self.transport.sendto(leg.data, dest)
        self.cnt("sent")
        leg.handle = self.loop.call_later(0.5, self.retrans, leg)
        if len(tx.legs) < self.a.proxies:
            self.loop.call_later(self.a.failover, self.failover, tx, idx + 1, len(tx.legs))

    def failover(self, tx, idx, nlegs):
        if tx.done or len(tx.legs) != nlegs:
            return
        self.cnt("failover_legs")
        self.add_leg(tx, idx)

    def retrans(self, leg):
        tx = leg.tx
        if tx.done:
            return
        if time.time() - leg.t0 >= 32:
            self.legs.pop(leg.branch, None)
            if all(time.time() - lg.t0 >= 32 for lg in tx.legs):
                self.finish(tx, 408, leg, {})
            return
        self.transport.sendto(leg.data, leg.dest)
        self.cnt("retrans")
        tx.retrans += 1
        leg.interval = min(leg.interval * 2, 4.0)
        leg.handle = self.loop.call_later(leg.interval, self.retrans, leg)

    def finish(self, tx, code, leg, hdrs):
        now = time.time()
        tx.done = True
        for lg in tx.legs:
            if lg.handle:
                lg.handle.cancel()
            if lg is not leg and lg.branch in self.legs:
                self.loop.call_later(40, self.legs.pop, lg.branch, None)
        key = "%s_%d" % (tx.kind, code)
        self.cnt("rx_" + key)
        self.lat.setdefault(key, []).append((now - leg.t0) * 1000)
        self.reqf.write("%.3f,%s,%d,%.1f,%.1f,%s,%d,%d\n" % (now, tx.kind, code, (now - leg.t0) * 1000,
                                                            (now - tx.t0) * 1000, leg.dest[0], len(tx.legs), tx.retrans))
        d = tx.dialog
        if tx.gen != d.gen:
            return
        if 200 <= code < 300:
            if tx.kind == "initial":
                m = TAG_RE.search(hdrs.get("to", [""])[0])
                d.ttag = m.group(1) if m else None
                m = URI_RE.search(hdrs.get("contact", [""])[0])
                d.rtarget = m.group(1) if m else "sip:%s@%s" % (d.ruser, self.devices[d.dev].host)
                d.routes = list(reversed(split_uris(hdrs.get("record-route", []))))
            exp = self.a.expires
            try:
                exp = int(hdrs.get("expires", [exp])[0])
            except (TypeError, ValueError):
                pass
            d.alive = True
            heapq.heappush(self.heap, (now + max(30, exp * self.a.refresh_frac * random.uniform(0.97, 1.0)),
                                       d.did, d.gen, "refresh"))
        else:
            # 481 (dialog gone) / 408 (no answer): phones open a new dialog almost at once.
            # Any other 4xx/5xx/6xx: drop the dialog and try again later, after Retry-After when given.
            d.alive = False
            self.by_callid.pop(d.callid, None)
            if code in (481, 408):
                delay = random.uniform(0.5, 3.0)
            else:
                self.cnt("rx_other_error")
                delay = random.uniform(30, 90)
                ra = hdrs.get("retry-after", [""])[0].split(";")[0].strip()
                if ra.isdigit():
                    delay = max(1, int(ra)) + random.uniform(0, 2)
            heapq.heappush(self.heap, (now + delay, d.did, d.gen, "initial"))

    # ------------------------------------------------------------------ receive
    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        try:
            start, hdrs, _ = parse(data)
        except Exception:
            self.cnt("parse_error")
            return
        if start.startswith("SIP/2.0"):
            try:
                code = int(start.split(" ", 2)[1])
            except (IndexError, ValueError):
                return
            if code < 200:
                return
            m = BRANCH_RE.search(hdrs.get("via", [""])[0])
            leg = self.legs.pop(m.group(1), None) if m else None
            if leg is None:
                self.cnt("late_reply_%d" % code)
                return
            if leg.handle:
                leg.handle.cancel()
            tx = leg.tx
            if tx.done:
                self.cnt("late_leg_reply_%d" % code)
                return
            if code in (401, 407) and tx.auth_tries < 1:
                hname = "proxy-authenticate" if code == 407 else "www-authenticate"
                ch = hdrs.get(hname)
                if ch:
                    tx.chal = parse_challenge(ch[0])
                    tx.chal_hdr = "Proxy-Authorization" if code == 407 else "Authorization"
                    tx.auth_tries += 1
                    tx.dialog.cseq += 1
                    self.cnt("auth_challenge")
                    for lg in tx.legs:
                        if lg.handle:
                            lg.handle.cancel()
                    tx.legs = []
                    self.add_leg(tx, tx.dests.index(leg.dest) if leg.dest in tx.dests else 0)
                    return
            if code == 423 and tx.auth_tries < 3:
                # Interval Too Brief: ask again at once with the server's Min-Expires (RFC 6665 4.1.2.1)
                try:
                    tx.dialog.exp = int(hdrs.get("min-expires", ["0"])[0])
                except ValueError:
                    tx.dialog.exp = None
                if tx.dialog.exp:
                    self.cnt("rx_423_retry")
                    tx.auth_tries += 1
                    tx.dialog.cseq += 1
                    for lg in tx.legs:
                        if lg.handle:
                            lg.handle.cancel()
                    tx.legs = []
                    self.add_leg(tx, tx.dests.index(leg.dest) if leg.dest in tx.dests else 0)
                    return
            self.finish(tx, code, leg, hdrs)
        else:
            method = start.split(" ", 1)[0]
            if method == "NOTIFY":
                self.on_notify(hdrs, addr)
            elif method == "OPTIONS":
                self.reply(hdrs, addr, 200, "OK")
            else:
                self.cnt("rx_req_" + method)

    def on_notify(self, hdrs, addr):
        self.cnt("notify_rx")
        d = self.by_callid.get(hdrs.get("call-id", [""])[0])
        if d is None:
            self.cnt("notify_unknown_dialog")
            self.reply(hdrs, addr, 481, "Call/Transaction Does Not Exist")
            return
        if d.dev in self.dead:
            self.cnt("notify_dropped_dead_nat")
            return
        ft = TAG_RE.search(hdrs.get("from", [""])[0])
        if d.ttag and ft and ft.group(1) != d.ttag:
            self.cnt("notify_other_totag")
            self.reply(hdrs, addr, 500, "Server Internal Error")
            return
        self.reply(hdrs, addr, 200, "OK")
        if hdrs.get("subscription-state", [""])[0].startswith("terminated"):
            self.cnt("notify_terminated")
            d.alive = False
            self.by_callid.pop(d.callid, None)
            heapq.heappush(self.heap, (time.time() + random.uniform(0.5, 3), d.did, d.gen, "initial"))

    def reply(self, hdrs, addr, code, reason):
        to = hdrs.get("to", [""])[0]
        if ";tag=" not in to:
            to += ";tag=" + rid(8)
        m = ["SIP/2.0 %d %s" % (code, reason)] + ["Via: " + v for v in hdrs.get("via", [])]
        m += ["From: " + hdrs.get("from", [""])[0], "To: " + to, "Call-ID: " + hdrs.get("call-id", [""])[0],
              "CSeq: " + hdrs.get("cseq", [""])[0], "Content-Length: 0"]
        self.transport.sendto((CRLF.join(m) + CRLF + CRLF).encode(), addr)

    # -------------------------------------------------------------------- loops
    async def scheduler(self):
        while True:
            now = time.time()
            n = 0
            while self.heap and self.heap[0][0] <= now and n < 5000:
                _, did, gen, what = heapq.heappop(self.heap)
                d = self.dialogs[did]
                if gen != d.gen:
                    continue
                self.start(d, what == "initial")
                n += 1
            await asyncio.sleep(0.02)

    async def reporter(self):
        while True:
            await asyncio.sleep(10)
            c, self.c = self.c, {}
            lat, self.lat = self.lat, {}
            alive = sum(1 for d in self.dialogs if d.alive)
            ls = {k: {"n": len(v), "min": round(min(v), 1), "p50": pct(v, 50), "p90": pct(v, 90),
                      "max": round(max(v), 1)} for k, v in lat.items()}
            self.statf.write(json.dumps({"t": round(time.time(), 3), "alive": alive, "dialogs": len(self.dialogs),
                                         "pending_legs": len(self.legs), "c": c, "lat": ls}) + "\n")
            self.reqf.flush()


def local_ip_toward(host, port):
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect((socket.gethostbyname(host), port))  # no packet is sent
        return probe.getsockname()[0]
    finally:
        probe.close()


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--creds", required=True, help="sipusername,password,udp:host:port per line")
    ap.add_argument("--out", required=True)
    ap.add_argument("--ip", default="auto", help="address put in Via/Contact")
    ap.add_argument("--port", type=int, default=5060)
    ap.add_argument("--max-users", type=int, default=0, help="use only the first N devices (0 = all)")
    ap.add_argument("--blf", type=int, default=3)
    ap.add_argument("--blf-range", default="101-125", help="BLF target extensions")
    ap.add_argument("--mwi", type=int, default=1)
    ap.add_argument("--expires", type=int, default=600)
    ap.add_argument("--refresh-frac", type=float, default=0.9)
    ap.add_argument("--ramp-rate", type=float, default=400)
    ap.add_argument("--proxies", type=int, default=3)
    ap.add_argument("--failover", type=float, default=3.5)
    ap.add_argument("--dead-notify-pct", type=float, default=2)
    ap.add_argument("--seed", type=int, default=27)
    ap.add_argument("--ha1", default="auto", choices=["auto", "yes", "no"],
                    help="password column is HA1 (auto: when it is 32 hex chars)")
    ap.add_argument("--shard", default="0/1", help="k/N: use only lines with index %% N == k")
    a = ap.parse_args()
    global HA1_MODE
    HA1_MODE = a.ha1
    ph = Phones(a)
    if a.ip == "auto":
        dv = ph.devices[0]
        a.ip = local_ip_toward(dv.host, dv.port)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 << 20)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 8 << 20)
    sock.bind(("0.0.0.0", a.port))
    print("local %s:%d" % (a.ip, a.port), flush=True)
    await asyncio.get_event_loop().create_datagram_endpoint(lambda: ph, sock=sock)

    def stop():
        ph.reqf.flush()
        ph.statf.flush()
        os._exit(0)
    import signal
    asyncio.get_event_loop().add_signal_handler(signal.SIGTERM, stop)
    await asyncio.gather(ph.scheduler(), ph.reporter())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
