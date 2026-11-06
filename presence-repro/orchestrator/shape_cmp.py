import json, subprocess, os, sys, datetime as dt
def q(ds, expr, s, e, step=60):
    env = dict(os.environ, DS=ds)
    out = subprocess.run(["python3","prom.py","range",expr,str(s),str(e),str(step)],capture_output=True,text=True,env=env,timeout=200)
    try: return json.loads(out.stdout)
    except Exception: return []
def series(ds, sel, proxysel, s, e):
    m = {}
    def add(key, expr, by="instance_id"):
        for x in q(ds, expr, s, e):
            i = x["metric"].get(by, "?")
            for t, v in x["values"]:
                m.setdefault(int(t), {}).setdefault(key, {})[i[-5:]] = float(v)
    add("cpu", '100*(1-avg by (instance_id)(rate(node_cpu_seconds_total{mode="idle",%s}[2m])))' % sel)
    add("sub", 'sum by (instance_id)(rate(kamailio_core_rcv_requests_subscribe{%s}[2m]))' % sel)
    add("pxsub", 'sum(rate(kamailio_core_rcv_requests_subscribe{%s}[2m]))' % proxysel, by="__none__")
    add("uac", 'sum by (instance_id)(rate(kamailio_tmx_UAC_transactions{%s}[2m]))' % sel)
    return m
def show(m, tz, label):
    print("===", label)
    for t in sorted(m):
        d = m[t]; lt = dt.datetime.fromtimestamp(t, dt.timezone(dt.timedelta(hours=tz))).strftime("%H:%M")
        cpu = " ".join("%3.0f" % v for k, v in sorted(d.get("cpu", {}).items()) if v > 3)
        sub = " ".join("%4.0f" % v for k, v in sorted(d.get("sub", {}).items()) if v > 1)
        uac = " ".join("%4.0f" % v for k, v in sorted(d.get("uac", {}).items()) if v > 1)
        px = sum(d.get("pxsub", {}).values())
        print("%s cpu %-9s sub/s %-10s notify/s %-10s proxies rcv SUBSCRIBE/s %5.0f" % (lt, cpu, sub, uac, px))
w = sys.argv[1]
if w == "prod":
    s = int(dt.datetime(2026,11,3,6,40,tzinfo=dt.timezone.utc).timestamp()); e = int(dt.datetime(2026,11,3,8,20,tzinfo=dt.timezone.utc).timestamp())
    show(series("fdon6r4at0bnkd", 'job="n2p-callingserver-presence",node_id="27"', 'node_id="27",job!="n2p-callingserver-presence"', s, e), -5, "PROD node 27 (EST), swap 01:47-01:48")
else:
    s = int(dt.datetime(2026,11,5,23,30,tzinfo=dt.timezone.utc).timestamp()); e = int(dt.datetime.now(dt.timezone.utc).timestamp())
    show(series("de13w5smkidc0a", 'instance_id=~"i-04b6ea3a696584676|i-002877283156bfa07"', 'instance_id=~"i-087d1e95e3955aec1|i-0f6134d44a9f25914|i-04f1ad26afc61826c"', s, e), -5, "QA 6071 (ET), restart 18:39:18-18:39:45")
