# Presence CPU waves after a restart: how to reproduce

This folder reproduces the production presence problem of node 27 on November 3, 2026:
after both presence servers were replaced, presence CPU went to 100% in waves, one wave every
21-22 minutes, for hours. It was reproduced on QA pod 6071.

## Result

Two runs on QA pod 6071, November 5-6, 2026 (New York time). Nobody did anything by hand after the restart.

* Run 1: background load from 18:14, presence restart at 18:39, then 10 waves at 100% CPU on both presence
  servers from 18:54 to 22:08, one every 20-23 minutes, 5-11% CPU between waves. (Until 18:49 this run also
  had 100,000 extra subscriptions made a few minutes before the restart; the restart removed them and they
  were stopped before their first refresh.)
* Run 2, a clean repeat from the start: background load from 22:10, presence restart at 22:35, then 10 waves
  at 100% CPU from 22:51 to 02:02, one every 20-23 minutes, 6-20% CPU between waves.
* The test phones lost no UDP packets (0 UDP errors on the load server in both runs).
* Chart (presence CPU, November 5 18:20 to November 6 02:05): https://grafana2.aws.net2phone.com/d/kamailio-vlitvinov/kamailio-by-viktor-litvinov?orgId=1&var-datasource=de13w5smkidc0a&var-env=qa&var-node=6071&var-job=n2p-callingserver-presence&var-instance_id=i-002877283156bfa07&var-instance_id=i-04b6ea3a696584676&from=1793920800000&to=1793948700000&viewPanel=29

Production, node 27, November 3: after the new presence servers started at 01:47 and before the
first restart by hand at 04:41 (New York time): 8 waves, one every 21-22 minutes, 1-8 minutes at
100% CPU each, 8-18% CPU between waves.

## What is needed

* A pod with 2 presence servers and 3 sipproxies (QA pod 6071 was used).
* A Linux server in AWS with a public IP and no VPN, for the test phones (an m7g.2xlarge was used:
  8 CPU cores, 32 GB). A Mac is not good: its security program and the VPN program lose UDP packets
  at this load.
* A file with test SIP users, one per line: `sipusername,password-or-HA1,udp:host:port`.
  It is secret: never put it in git (`*.secrets.csv` is in `.gitignore`).
* A user on the presence servers that may run `systemctl restart kamailio` (`ssh/create-tmp-user.sh`).

## Steps by hand

1. Prepare the load server (as root): `ssh/setup-loadgen.sh`, then `ssh/tune-loadgen.sh`
   (bigger UDP buffers, more open files).
2. Copy `pod-repro/` and the users file to `~/loadgen` on the load server.
3. Start the test phones (60,000 phones, each 1 voicemail and 3 busy-lamp subscriptions = 240,000):
   ```
   python3 -u repro.py --creds <users file> --max-users 60000 --workers 4 --ramp-rate 0 \
     --expires 1333 --dead-notify-pct 3 --blf-range 1000-99999 --minutes 300 --yes --out runs/test1
   ```
   * `--ramp-rate 0`: new subscriptions are spread evenly over one refresh time (20 minutes).
   * `--expires 1333`: phones refresh every 20 minutes, like in production.
   * `--dead-notify-pct 3`: 3% of the phones never answer NOTIFY, like phones behind a broken router.
   * The phones send a request again when there is no answer, move to the next sipproxy after
     3.5 seconds, and start a new subscription after 481 or 408.
4. Wait about 25 minutes. Check: each presence server gets about 100 SUBSCRIBE, 300 DMQ messages and
   100 NOTIFY per second, at about 10% CPU (production had the same numbers before the swap).
5. Restart presence: `sudo systemctl restart kamailio` on presence-a, then on presence-b 20 seconds later.
   Do nothing else.
6. Watch presence CPU. Expected: CPU goes up at once, reaches 100% after about 15 minutes, then a new
   wave to 100% every 20-23 minutes, with 10-20% CPU between waves.

## Automatic mode

`orchestrator/run_orch.sh` starts the test phones on the load server (`ssh/loadgen-login.sh`), restarts
presence (`ssh/presence-login.sh`), reads presence CPU from Prometheus through grafana2 (`gmcp.py`) and
counts the waves. It stops after 10 waves (success), when the waves fade, or after about 4 hours.
It logs one status line per minute to `$WAVES_DIR/orch/orch.log` (default `/tmp/waves`).
Settings: `CREDS_CSV` (users file), `WAVES_DIR`; host addresses and instance ids of pod 6071 are at
the top of `wave_orch.py` and in the `ssh/*-login.sh` scripts. Logins use
`ssh/credential-presence.properties` (copy the `.sample`; never commit it).

## How to check

* Presence CPU (Prometheus Dev):
  `100*(1-avg by (instance_id)(rate(node_cpu_seconds_total{mode="idle",node_id="6071",job="n2p-callingserver-presence"}[2m])))`
* SUBSCRIBE per second per presence server:
  `sum by (instance_id)(rate(kamailio_core_rcv_requests_subscribe{node_id="6071",job="n2p-callingserver-presence"}[2m]))`
* Compare with production: `orchestrator/cmp_prod_6071.py` and `orchestrator/shape_cmp.py`.
* Grafana dashboard: grafana2 "Kamailio by Viktor Litvinov" (`kamailio-vlitvinov`), node 6071,
  job n2p-callingserver-presence, panel "CPU Utilization".

## How the loop works

* After the restart both servers have no subscriptions, so every refresh gets 481. The phones
  subscribe again. CPU goes to 100% in about 15 minutes. NOTIFY gets no answer in time, so presence
  deletes subscriptions (`timeout_rm_subs=1`). The DMQ link between the servers breaks
  (EADDRNOTAVAIL, write queue full).
* The subscriptions lost in this overload get 481 at their next refresh, 20 minutes later. All these
  phones subscribe again at the same time. This makes the next wave.

## Why it was hard to reproduce

* First tests made a big group of new subscriptions in a few minutes before the restart, so they all
  refreshed together. This gives one big wave, and then it stops. Production had no such group: the
  load was steady, and the group made itself during the overload after the restart.
* The refresh time must be longer than the overload (about 7 minutes). With Expires 300-500 seconds,
  presence gets well again between refreshes.
* A Mac cannot send this load: its security program and the VPN program lost up to 110,000 UDP
  packets per minute, so phones timed out on the test side.
* A production-size environment is not needed: the loop depends on ratios (load compared with presence
  CPU, refresh time compared with overload time, NOTIFY answer time compared with `fr_timer`), not on
  size. The same loop was reproduced locally in containers with 20,000 subscriptions and presence
  limited to a quarter of one CPU core.

## sipexer options added for these tests

`-udp-sockets N` (share N local UDP sockets between dialogs), `-spread-ips` (give each dialog the next
IP of the target name in turn), `-tuser-range A-B` (random To user in a range when `-tuser` is not set).
