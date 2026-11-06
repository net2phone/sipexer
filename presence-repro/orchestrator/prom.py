"""Prometheus via grafana2 MCP proxy. usage: prom.py range '<promql>' <start_epoch> <end_epoch> <step>
                                          prom.py instant '<promql>'"""
import json, sys, subprocess, urllib.parse, time, os
DS = os.environ.get("DS", "de13w5smkidc0a")
HERE = os.path.dirname(os.path.abspath(__file__))
def api(ep):
    out = subprocess.run(["python3", os.path.join(HERE, "gmcp.py"), "grafana_api_request",
                          json.dumps({"endpoint": ep, "method": "GET"})], capture_output=True, text=True, timeout=180).stdout
    return json.loads(out)["data"]
def rng(q, s, e, step):
    b = api(f"/api/datasources/proxy/uid/{DS}/api/v1/query_range?" + urllib.parse.urlencode({"query": q, "start": s, "end": e, "step": step}))
    if b.get("status") != "success": raise SystemExit("FAIL " + json.dumps(b)[:300])
    return b["data"]["result"]
def inst(q):
    b = api(f"/api/datasources/proxy/uid/{DS}/api/v1/query?" + urllib.parse.urlencode({"query": q, "time": int(time.time())}))
    if b.get("status") != "success": raise SystemExit("FAIL " + json.dumps(b)[:300])
    return b["data"]["result"]
if __name__ == "__main__":
    if sys.argv[1] == "range":
        print(json.dumps(rng(sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5])))
    else:
        print(json.dumps(inst(sys.argv[2])))
