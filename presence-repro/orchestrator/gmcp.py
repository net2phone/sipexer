"""Tiny JSON-RPC client for grafana2's MCP endpoint (no auth). Usage:
   python3 gmcp.py <tool> '<json args>'   -> prints the tool's text result
"""
import json
import sys
import urllib.request

URL = "http://grafana2.aws.net2phone.com/mcp"
HDR = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}


def post(body, sid=None):
    h = dict(HDR)
    if sid:
        h["Mcp-Session-Id"] = sid
    req = urllib.request.Request(URL, data=json.dumps(body).encode(), headers=h, method="POST")
    with urllib.request.urlopen(req, timeout=120) as r:
        sid = r.headers.get("Mcp-Session-Id") or sid
        raw = r.read().decode()
    out = None
    for line in raw.splitlines():
        if line.startswith("data:"):
            out = json.loads(line[5:].strip())
    if out is None and raw.strip().startswith("{"):
        out = json.loads(raw)
    return out, sid


def call(tool, args):
    init, sid = post({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                      "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                                 "clientInfo": {"name": "gmcp", "version": "1"}}})
    post({"jsonrpc": "2.0", "method": "notifications/initialized"}, sid)
    res, _ = post({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                   "params": {"name": tool, "arguments": args}}, sid)
    if "error" in res:
        raise SystemExit("error: %s" % res["error"])
    return "".join(c.get("text", "") for c in res["result"].get("content", []))


if __name__ == "__main__":
    print(call(sys.argv[1], json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}))
