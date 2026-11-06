import json, subprocess, os, sys, datetime as dt
def q(ds, expr, s, e, step):
    env = dict(os.environ, DS=ds)
    out = subprocess.run(["python3","prom.py","range",expr,str(s),str(e),str(step)],capture_output=True,text=True,env=env,timeout=200)
    try: return json.loads(out.stdout)
    except Exception: return []
def table(ds, sel, s, e, step, tz):
    mets = {"cpu": '100*(1-avg by (instance_id)(irate(node_cpu_seconds_total{mode="idle",%s}[5m])))' % sel}
    for m in ["subscribe","publish","kdmq","notify","options"]:
        mets[m[:4]] = 'sum by (instance_id)(rate(kamailio_core_rcv_requests_%s{%s}[5m]))' % (m, sel)
    mets["uac"] = 'sum by (instance_id)(rate(kamailio_tmx_UAC_transactions{%s}[5m]))' % sel
    mets["r2xx"] = 'sum by (instance_id)(rate(kamailio_core_rcv_replies_2xx{%s}[5m]))' % sel
    rows = {}
    for k, expr in mets.items():
        for x in q(ds, expr, s, e, step):
            i = x["metric"].get("instance_id","?")[-5:]
            for t, v in x["values"]:
                rows.setdefault(int(t), {}).setdefault(i, {})[k] = float(v)
    for t in sorted(rows):
        lt = dt.datetime.fromtimestamp(t, dt.timezone(dt.timedelta(hours=tz))).strftime("%H:%M")
        parts = []
        for i, d in sorted(rows[t].items()):
            if d.get("subs", 0) < 1 and d.get("cpu", 0) < 3: continue
            parts.append("%s cpu%4.0f sub%4.0f pub%4.0f kdmq%4.0f uac%4.0f 2xx%4.0f opt%3.0f" % (i, d.get("cpu",-1), d.get("subs",-1), d.get("publ",-1), d.get("kdmq",-1), d.get("uac",-1), d.get("r2xx",-1), d.get("opti",-1)))
        print(lt, " | ".join(parts))
which = sys.argv[1]
if which == "prod":
    s = int(dt.datetime(2026,11,3,6,20,tzinfo=dt.timezone.utc).timestamp()); e = int(dt.datetime(2026,11,3,7,40,tzinfo=dt.timezone.utc).timestamp())
    table("fdon6r4at0bnkd", 'job="n2p-callingserver-presence",node_id="27"', s, e, 300, -5)
else:
    s = int(dt.datetime(2026,11,5,23,20,tzinfo=dt.timezone.utc).timestamp()); e = int(dt.datetime.now(dt.timezone.utc).timestamp())
    table("de13w5smkidc0a", 'instance_id=~"i-04b6ea3a696584676|i-002877283156bfa07"', s, e, 300, -5)
