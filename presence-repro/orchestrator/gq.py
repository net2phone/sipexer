import json, subprocess, sys
def dsq(expr, ds="de13w5smkidc0a", frm="now-30m", to="now", interval_ms=60000):
    body = {"from": frm, "to": to, "queries": [{"refId": "A", "datasource": {"type": "prometheus", "uid": ds}, "expr": expr, "intervalMs": interval_ms, "maxDataPoints": 500}]}
    out = subprocess.run(["python3", "gmcp.py", "grafana_api_request", json.dumps({"endpoint": "/api/ds/query", "method": "POST", "body": json.dumps(body)})], capture_output=True, text=True, timeout=180).stdout
    try:
        r = json.loads(out); r = r.get("data", r)
    except Exception:
        return "ERR " + out[:300]
    res = []
    for ref, x in r.get("results", {}).items():
        if x.get("error"): res.append("error: " + x["error"])
        for fr in x.get("frames", []):
            f = fr["schema"]["fields"]; vals = fr["data"]["values"]
            res.append("%s points=%d last=%s" % (f[1].get("labels") if len(f) > 1 else "?", len(vals[0]) if vals else 0, vals[1][-1] if vals and len(vals) > 1 and vals[1] else None))
    return res or ["no frames"]
if __name__ == "__main__":
    for e in sys.argv[1:]:
        print(e[:120]); [print("   ", l) for l in dsq(e)]
