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
* Run 3, with sipexer (100,000 of the 240,000 subscriptions from sipexer, see "Reproduce with sipexer"):
  presence restart at 03:13 on November 6, then 3 waves at 100% CPU at 03:27, 03:50 and 04:14 (the run was
  set to stop after 3 waves).
* The test phones lost no UDP packets (0 UDP errors on the load server in all runs).
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

## Reproduce with sipexer

sipexer can be the test phones too. The sipexer run (S3) used sipexer for 100,000 of the 240,000
subscriptions and pod-repro for the other 140,000 (35,000 devices x 4), so the total load and the
shape were the same as runs 1 and 2.

Result of the sipexer run, November 6 (New York time): pod-repro warm-up from 02:31, sipexer sessions made
from 02:51 to 03:13, presence restart at 03:13 (presence-b 21 seconds after presence-a). Then 3 waves at 100%
CPU on both presence servers, at 03:27, 03:50 and 04:14 (every 23-24 minutes), with 8-28% CPU between waves.
The run was set to stop after 3 waves. The sipexer phones took part like the others: after the restart
every refresh got 481 and sipexer opened a new dialog (about 4,800 per minute), and in the waves it saw up
to 15,852 timeouts per minute. sipexer used 9-12.6 GB of memory; the load server lost no UDP packets.
Chart: https://grafana2.aws.net2phone.com/d/kamailio-vlitvinov/kamailio-by-viktor-litvinov?orgId=1&var-datasource=de13w5smkidc0a&var-env=qa&var-node=6071&var-job=n2p-callingserver-presence&var-instance_id=i-002877283156bfa07&var-instance_id=i-04b6ea3a696584676&from=1793952000000&to=1793956800000&viewPanel=29

1. Build sipexer for the load server and copy it next to the users file in `~/loadgen`:
   `GOOS=linux GOARCH=arm64 go build -o sipexer .` (in the repo root, Go 1.24).
2. Start pod-repro for 140,000 subscriptions (as in "Steps by hand", with `--max-users 35000`)
   and wait 20 minutes.
3. Start sipexer. The exact command of the S3 run (from `~/loadgen`):
   ```
   ./sipexer --subscribe-session -sw 7562000 -ex 1333 -cb -ha1 -acsv <users file> \
     -xh 'Event: dialog' -fd sip.t2-n6071.qa.n2p.io -td sip.t2-n6071.qa.n2p.io \
     -failover 3500 -rc 100000 -rb 8 -rbi 100 -udp-sockets 16 -spread-ips -vl 1 \
     -notify-drop-pct 3 -tuser-range 1000-99999
   ```
   * `--subscribe-session`: each session keeps one SUBSCRIBE dialog and refreshes it.
   * `-ex 1333`: Expires 1333 seconds; sipexer refreshes at about 90%, so every 20 minutes.
   * `-rc 100000 -rb 8 -rbi 100`: 100,000 sessions, 8 new ones every 100 ms (80 per second), so all
     are made in about 21 minutes and their refreshes are spread evenly over the 20 minutes.
   * `-sw 7562000`: how long the sessions stay up, in milliseconds (about 2 hours).
   * `-cb -ha1 -acsv <users file>`: digest login with the HA1 column of the users file.
   * `-xh 'Event: dialog'`: busy-lamp (dialog) subscriptions.
   * `-fd` / `-td`: From and To domain of the pod.
   * `-tuser-range 1000-99999`: each session watches a random extension, spread like production.
   * `-failover 3500`: no answer in 3.5 seconds: send to the next sipproxy IP, like Polycom phones.
   * `-spread-ips`: each session uses the next IP of the sipproxy name in turn, so all 3 get traffic.
   * `-udp-sockets 16`: all sessions share 16 local UDP sockets.
   * `-notify-drop-pct 3`: 3% of the sessions never answer NOTIFY, like phones behind a broken router.
   * `-vl 1`: low log level.
4. When all sessions are made (about 21 minutes), restart presence-a, then presence-b 21 seconds later.
5. Watch presence CPU as in "Steps by hand".

Memory: sipexer needs about 100 KB per session. 100,000 sessions used 9-12 GB on the load server
(more during the waves). 240,000 sessions would need about 24 GB or more, so on a 32 GB server use
pod-repro for the rest, or use a 64 GB server (for example m7g.4xlarge) for sipexer only.

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
