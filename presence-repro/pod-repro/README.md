# Presence storm repro on a QA pod (6071)

`repro.py` makes the pod's SIP users subscribe through the real sipproxies like phones do,
optionally restarts both presence nodes, and reports whether the Nov 3 collapse happened.
Python 3.8+ only, nothing to install, macOS or Debian.

Files:
- `repro.py` – runner, live status every 30 s, `out/<time>/summary.md` with a verdict
- `phones_proxy.py` – the phones: digest auth, retransmissions, fail-over, new dialog after 481/408
- `users_qa_udp_n6071.secrets.csv` – credentials, one device per line:
  `sipusername,password,udp:sip.t2-n6071.qa.n2p.io:6070`. The password column is HA1
  (md5 of user:realm:password); the tool detects that (`--ha1 auto`). **Keep this file private.**

## What it does

- Every device keeps 1 MWI and 3 BLF subscriptions (BLF to extensions 101-125): 115,052 devices
  -> about 460,000 subscriptions, about twice node 27.
- All subscriptions are created at 500/s with Expires 1200, so they refresh together every
  18 minutes (node 27 refreshed in a 20-minute herd). Steady load: ~425 SUBSCRIBE/s at the proxies.
- Like Polycom/Yealink: answers the 407, reuses the nonce, retransmits 0.5 s doubling to 4 s,
  gives up after 32 s (408), sends a copy to the next proxy IP after 3.5 s, opens a new dialog
  right after 481 or 408. 2% of devices never answer NOTIFY (dead NAT).
- Several worker processes (1 per 30k devices), each on its own local UDP port (5060, 5061, ...).

No presence CPU cap is needed: the load itself is bigger than node 27's.

## Steps

1. Presence on the prod config (release v2027.3.2, `59936ef`, `subs_htable_size 18`).
2. Run it from a host that can reach `sip.t2-n6071.qa.n2p.io:6070` over UDP. NOTIFYs come back
   to the same ports through the NAT, as for real phones. A Mac on the office network or VPN works.
3. Start, let all subscriptions come up (~16 min), then restart both presence nodes at the
   same time, as the Nov 3 swap did, and watch at least 2 refresh cycles:
   ```sh
   python3 repro.py --minutes 60 --trigger-after 300 \
     --trigger-cmd "<restart both presence nodes, e.g. two ssh/ssm commands run in parallel>"
   ```
   Without `--trigger-cmd`, restart them by hand when it prints "all ... subscriptions are up".
4. Read `out/<time>/summary.md`: reply times (min, p50, p90, max) per phase, a table per 5 minutes
   and the verdict. Reproduced = alive subscriptions fall well below the total and 408 timeouts
   keep coming for more than one refresh cycle. In Splunk, presence `"took too long"` and
   `"record not found in hash_table"` per 10 minutes show the same.
5. Apply the "How to fix" tab (https://claude.ai/artifact/Ddr1TmYv7V9hganbowuze1#fix) and repeat.

If there is no collapse: lower `--expires` (minimum 300) or raise `--ramp-rate` for a tighter
herd, or as a last resort cap presence CPU
(`sudo systemctl set-property --runtime kamailio.service CPUQuota=50%` on both presence hosts).

## Options

| Option | Default | Use |
|---|---|---|
| `--creds` | `users_qa_udp_n6071.secrets.csv` | credentials file |
| `--max-users` | all | use only the first N devices (smaller test) |
| `--ramp-rate` | 500 | new subscriptions per s at start (all workers) |
| `--expires` | 1200 | Expires asked for; refresh at 90% |
| `--blf` / `--blf-range` | 3 / 101-125 | BLF subscriptions per device and their target extensions |
| `--proxies` / `--failover` | 3 / 3.5 | fail-over copies to the proxy IPs |
| `--dead-notify-pct` | 2 | devices that never answer NOTIFY |
| `--minutes` | 45 | run time |
| `--trigger-after` / `--trigger-cmd` | 300 / none | command run once after all subscriptions are up |
| `--ha1` | auto | password column is HA1 |
| `--yes` | off | skip the confirmation prompt |

It refuses any target that is not a `*.qa.n2p.io` or `*.dev.n2p.io` host.

Smoke test 2026-11-05 from a Mac: 30 s, 13,487 new subscriptions, all answered 200
(p50 196 ms including the auth round), no 408.
