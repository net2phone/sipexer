# Presence CPU waves after a restart: how to reproduce

Production, node 27, November 3, 2026: both presence servers were replaced at 01:47. After that, presence
CPU went to 100% in waves, one wave every 21-22 minutes, for hours. This folder reproduces it on QA pod
6071. All times are New York time.

## Why it was hard to reproduce

It took 20 tries. A restart alone gives one wave of 481s and new subscriptions, and then presence gets
well again. The waves came back only when the test matched production in three ways: the load per presence
server, the refresh time, and how the phones act. With less load presence had spare CPU and the waves
faded. With more load CPU stayed at 100%. With a refresh of 5-9 minutes presence got well between
refreshes. Our first tests also made many new subscriptions just before the restart: that gives one or a
few big waves that fade, and production had no such group. Last, a Mac cannot send this load: its security
program and the VPN lost up to 110,000 UDP packets a minute, so the test phones timed out by themselves.

## What we did to reproduce it

1. Measured production per presence server (node 27, before the swap): about 100 SUBSCRIBE, 300 DMQ
   messages and 100 NOTIFY per second, at 9-10% CPU. Made the same load on 6071: 240,000 subscriptions
   (60,000 phones x 1 voicemail + 3 busy-lamp), made evenly over 20 minutes.
2. Refresh every 20 minutes (Expires 1333), like production phones. This is longer than the overload
   (about 7 minutes).
3. Made the test phones act like real phones: retransmit, move to the next sipproxy after 3.5 seconds, open
   a new subscription after 481 or 408, 3% never answer NOTIFY, watch many extensions (1000-99999).
4. Sent the load from a Linux server in AWS (no VPN, bigger UDP buffers), and checked every minute that it
   lost no UDP packets.
5. Used the production trigger and nothing else: restart presence-a, then presence-b 20-21 seconds later.
6. Counted the waves with an orchestrator: 10 waves in a row in two runs, and 3 waves in a run with sipexer.

## How to reproduce

You need:

* A pod with 2 presence servers and 3 sipproxies (we used QA pod 6071).
* A Linux server in AWS for the test phones, with a public IP and no VPN (we used an m7g.2xlarge:
  8 CPU cores, 32 GB). Do not use a Mac: its security program and the VPN lose UDP packets at this load.
* The users file of the pod, one user per line: `sipusername,password-or-HA1,udp:host:port`. It is
  secret: never put it in git (`*.secrets.csv` is in `.gitignore`).
* A login on both presence servers that may run `systemctl restart kamailio` (`ssh/create-tmp-user.sh`),
  with its user and password in `ssh/credential-presence.properties` (copy the `.sample`; never commit it).

Steps:

1. Prepare the load server once: copy `ssh/setup-loadgen.sh` and `ssh/tune-loadgen.sh` to it, then run
   them there as root: `sudo bash setup-loadgen.sh`, then `sudo bash tune-loadgen.sh`. They make the
   test user and `~/loadgen`, and set bigger UDP buffers and more open files.
2. Copy `pod-repro/repro.py`, `pod-repro/phones_proxy.py` and the users file to `~/loadgen` on the load
   server, for example with `LOADGEN_HOST=<server ip> ssh/loadgen-login.sh --put <file> loadgen/`.
3. Start the test phones there: 60,000 phones x 4 subscriptions = 240,000.
   ```
   cd ~/loadgen
   python3 -u repro.py --creds <users file> --max-users 60000 --workers 4 --ramp-rate 0 --expires 1333 \
     --dead-notify-pct 3 --local-port 5160 --blf-range 1000-99999 --minutes 300 --yes --out runs/test1
   ```
4. Wait 25 minutes. Each presence server must get about 100 SUBSCRIBE per second at about 10% CPU
   (see "How to check").
5. Restart presence-a, then presence-b 21 seconds later. Do nothing else after that:
   ```
   ssh/presence-login.sh
   ssh/presence-login.sh --run presence-6071-a 'sudo -n /usr/bin/systemctl restart kamailio'
   sleep 21
   ssh/presence-login.sh --run presence-6071-b 'sudo -n /usr/bin/systemctl restart kamailio'
   ```
6. Watch presence CPU in Grafana (see "How to check"). Expected: 100% about 15 minutes after the restart,
   then a new wave to 100% every 20-23 minutes, with 5-28% CPU between waves. The phones stop by
   themselves after `--minutes`.

`orchestrator/run_orch.sh` does steps 3-6 by itself (see "Automatic mode"). sipexer can also be the test
phones (see "Reproduce with sipexer"). Every option is explained in "Parameters".

## Result

Three runs on QA pod 6071, November 5-6, 2026. Nobody did anything by hand after the restart.
Load: 240,000 subscriptions, Expires 1333, made evenly, 3% never answer NOTIFY, extensions 1000-99999,
sent from the AWS server. Trigger: restart presence-a, then presence-b
20-21 seconds later.

* Run 1: steady load from 18:14, presence restart at 18:39, then 10 waves at 100% CPU on both presence
  servers from 18:54 to 22:08, one every 20-23 minutes, 5-11% CPU between waves. (Until 18:49 this run also
  had 100,000 extra subscriptions made a few minutes before the restart; the restart removed them and they
  were stopped before their first refresh.)
* Run 2, a clean repeat from the start: steady load from 22:10, presence restart at 22:35, then 10 waves
  at 100% CPU from 22:51 to 02:02, one every 20-23 minutes, 6-20% CPU between waves.
* Run 3, with sipexer (100,000 of the 240,000 subscriptions from sipexer, see "Reproduce with sipexer"):
  presence restart at 03:13 on November 6, then 3 waves at 100% CPU at 03:27, 03:50 and 04:14 (the run was
  set to stop after 3 waves).
* The test phones lost no UDP packets (0 UDP errors on the load server in all runs).
* Chart (presence CPU, November 5 18:20 to November 6 02:05): https://grafana2.aws.net2phone.com/d/kamailio-vlitvinov/kamailio-by-viktor-litvinov?orgId=1&var-datasource=de13w5smkidc0a&var-env=qa&var-node=6071&var-job=n2p-callingserver-presence&var-instance_id=i-002877283156bfa07&var-instance_id=i-04b6ea3a696584676&from=1793920800000&to=1793948700000&viewPanel=29

Production, node 27, November 3: after the new presence servers started at 01:47 and before the
first restart by hand at 04:41 (New York time): 8 waves, one every 21-22 minutes, 1-8 minutes at
100% CPU each, 8-18% CPU between waves.

## Parameters

Only the parameters of the run commands. "(default)" means it was not on the command line, the default was used.

### Python test phones (`pod-repro/repro.py`)

Command of run 2 (run 1: the same with `--minutes 274`; run 3: `--max-users 35000 --minutes 291`):
`python3 -u repro.py --creds <users file> --max-users 60000 --workers 4 --ramp-rate 0 --expires 1333 --minutes 275 --dead-notify-pct 3 --local-port 5160 --blf-range 1000-99999 --yes --out runs/<name>`

| Parameter | Value | Why it is needed | Why this value |
|---|---|---|---|
| `--creds` | users file | The phones must log in with real accounts of the pod (digest login at the sipproxy) | The pod's own test users file. It has about 115,000 accounts, enough for 60,000 phones. Secret, never in git |
| `--max-users` | 60000 (35000 in run 3) | Sets how many phones run, so it sets the load | 60,000 phones x 4 subscriptions = 240,000 subscriptions. With a 20-minute refresh that is about 100 SUBSCRIBE per second per presence server, the same as production before the swap. In run 3 sipexer made 100,000, so pod-repro made 140,000 (35,000 x 4) |
| `--workers` | 4 | One process cannot send this many packets alone | 4 processes on the 8-core load server kept up with the retransmissions in the waves (up to 25,000 packets per second) with 0 UDP errors. The default (1 per 30,000 phones) gives only 2 |
| `--ramp-rate` | 0 | Sets how fast the subscriptions are made at the start | 0 spreads them over one refresh time (240,000 in 20 minutes = 200 per second), so the refreshes come evenly, like production. A fixed fast rate makes a group that refreshes together, which production did not have |
| `--expires` | 1333 | Sets how often a phone refreshes | The phone refreshes at 90%: 1,200 seconds = 20 minutes, the wave period seen in production. It is also longer than the overload (about 7 minutes), which the loop needs |
| `--minutes` | 275 (274 in run 1, 291 in run 3) | How long the phones run | Set by the orchestrator: warm-up 20 minutes + 5 minutes steady + time for 10 waves of about 22 minutes + 15 minutes margin |
| `--dead-notify-pct` | 3 | Some real phones never answer NOTIFY. Presence deletes their subscriptions after the NOTIFY timeout, so their next refresh gets 481. This is part of the loop | 3% is the normal-day 481 rate in production (8,000-12,000 per hour on a node with about 110,000 subscriptions) |
| `--local-port` | 5160 | The phones need fixed local UDP ports for their Contact, where NOTIFY comes back | 5160-5163 for the 4 workers, away from the standard SIP port 5060 |
| `--blf-range` | 1000-99999 | Sets which extensions the busy-lamp subscriptions watch | Gives about 3 watchers per extension, like production where watchers are spread over many accounts. The default 101-125 puts thousands of watchers on 25 extensions |
| `--yes` | on | Skips the confirmation question | The runs were started by the orchestrator, with no person at the keyboard |
| `--out` | runs/<name> | Folder for the logs and the per-request data | One folder per run, so the runs do not mix |
| `--blf` | 3 (default) | Number of busy-lamp subscriptions per phone (plus 1 voicemail) | 3 busy-lamp + 1 voicemail = 4 subscriptions, like a typical desk phone |
| `--proxies` | 3 (default) | Number of sipproxy addresses a phone can move to | The pod has 3 sipproxies, like production node 27 |
| `--failover` | 3.5 (default) | Seconds without a final answer before the phone sends a copy to the next sipproxy | Polycom phones in the production capture did this after about 3.5 seconds |
| `--ha1` | auto (default) | Says if the password column is HA1 (md5 of user:realm:password) | auto finds the HA1 values in the users file |
| `--local-ip` | auto (default) | Address in Via and Contact | auto finds the public address of the load server, so NOTIFYs come back |

### sipexer test phones

Command of run 3:
`./sipexer --subscribe-session -sw 7562000 -ex 1333 -cb -ha1 -acsv <users file> -xh 'Event: dialog' -fd sip.t2-n6071.qa.n2p.io -td sip.t2-n6071.qa.n2p.io -failover 3500 -rc 100000 -rb 8 -rbi 100 -udp-sockets 16 -spread-ips -vl 1 -notify-drop-pct 3 -tuser-range 1000-99999`

| Parameter | Value | Why it is needed | Why this value |
|---|---|---|---|
| `--subscribe-session` | on | Runs sipexer as subscribing phones: keep the dialog, refresh before Expires, open a new one after a failure | The only sipexer mode with this phone behavior, which the loop needs |
| `-sw` | 7562000 | How long each session stays up, in milliseconds | Time to the restart (21 minutes) + time for 3 waves (about 95 minutes) + 10 minutes margin = 7,562 seconds |
| `-ex` | 1333 | Sets how often a session refreshes | Same as the Python phones: refresh at about 90% = 20 minutes, like production |
| `-cb` | on | Builds the Contact header from the local address | Presence sends NOTIFY to the Contact, so it must be right |
| `-ha1` | on | Says the password is HA1 | The users file has HA1 values |
| `-acsv` | users file | Accounts to log in with, one per session. The third column of each line is the target (`udp:sip.t2-n6071.qa.n2p.io:6070`), so no target is given on the command line | The same pod users file as the Python phones. Secret, never in git |
| `-xh` | `Event: dialog` | Sets the subscription type | Busy-lamp (dialog) subscriptions, the main type in production |
| `-fd`, `-td` | sip.t2-n6071.qa.n2p.io | From and To domain | The SIP domain of the pod. It resolves to its 3 sipproxies |
| `-tuser-range` | 1000-99999 | Sets which extension each session watches (when `-tuser` is not set). Added on this branch | Same spread as the Python phones: about 3 watchers per extension |
| `-rc` | 100000 | Number of sessions | 100,000 fits in memory with a margin (9-12.6 GB of 31 GB on the load server). pod-repro made the other 140,000 to reach 240,000 |
| `-rb`, `-rbi` | 8, 100 | Start 8 new sessions every 100 ms | 80 per second makes all 100,000 in about 21 minutes, about one refresh time, so the refreshes are spread evenly |
| `-failover` | 3500 | Milliseconds without a final answer before a copy goes to the next sipproxy address | Same as Polycom phones: 3.5 seconds |
| `-spread-ips` | on | Each new session uses the next address of the sipproxy name in turn. Added on this branch | Without it a cached DNS answer sends all sessions to one sipproxy (seen in early runs) |
| `-udp-sockets` | 16 | All sessions share 16 local UDP sockets. Added on this branch | 16 sockets carried 100,000 sessions with 0 UDP errors. One socket per session (100,000) slowed the machine down |
| `-notify-drop-pct` | 3 | Percent of sessions that never answer NOTIFY | Same 3% as the normal-day 481 rate in production |
| `-vl` | 1 | Log level | Small logs at this load, but still the retries that the orchestrator counts |
| `-failover-max` | 2 (default) | Maximum number of copies to the next sipproxy address per request | 2 copies reach the other 2 sipproxies, so a session tries all 3, like the Python phones |
| `-timer-t1`, `-timer-t2` | 500, 4000 (default) | Retransmission timers, in milliseconds | The standard SIP values, the same as real phones |
| `-timeout` | 32000 (default) | Milliseconds to wait for a final reply | The standard SIP 32 seconds. After it the session opens a new subscription in 0.5-3 seconds, the same as after 481 or 408 |

### Other parameters: unused in tests

These options exist, but no test run used them.

Python test phones (`repro.py`):

| Parameter | In tests | What it does | Why it was not used |
|---|---|---|---|
| `--trigger-after`, `--trigger-cmd` | **Unused** | Runs a command once, some time after all subscriptions are up (for example a presence restart) | The orchestrator did the restart instead: presence-a, then presence-b 20-21 seconds later, like the production swap. `repro.py` would run one command at one moment |
| `--stop-when-broken` | **Unused** | Stops the run as soon as presence is broken (for 60 seconds more than 5% of replies are 408 or 481) | We needed to see the waves after the break, not stop at the first one |

`phones_proxy.py` is the phone process. `repro.py` starts one for each worker and gives it its options
(`--shard`, `--seed`, `--ip`, `--port` and the values above). Two options keep their defaults: `--mwi 1`
(1 voicemail subscription per phone) and `--refresh-frac 0.9` (refresh at 90% of Expires). Do not run it by hand.

sipexer, options close to this test:

| Parameter | In tests | What it does | Why it was not used |
|---|---|---|---|
| `-subscribe` | **Unused** | Sends one SUBSCRIBE and stops | No refresh and no new subscription after 481 or 408. `--subscribe-session` does that |
| `-tuser`, `-to-uri`, `-ruser`, `-ruri` | **Unused** | Fixed To user, To URI, R-URI user or R-URI | All sessions would watch one extension, unlike production. `-tuser-range` gives each session a random To and R-URI user from the range |
| `-set-user` (`-su`), `-set-domains` (`-sd`) | **Unused** | Copy the To user into the R-URI; take the From and To domains from the R-URI | `-tuser-range` already sets the R-URI user, and `-fd`, `-td` set the domains |
| `-fuser`, `-from-uri` | **Unused** | Fixed From user or From URI | Each session must be its own user from the users file (`-acsv`) |
| `-auth-user` (`-au`), `-auth-password` (`-ap`) | **Unused** | One login user and password | The users file gives a user and password to each session |
| `-contact-uri` (`-cu`) | **Unused** | Fixed Contact | `-cb` builds the Contact from the local address, so NOTIFY comes back to the right socket |
| `-in-dialog-dns` (`-idd`), `-in-dialog-dns-auth` (`-idda`) | **Unused** | New DNS lookup before each in-dialog request, or before the request with credentials after 401 or 407 | Real phones send refreshes on the same path. A new lookup each time is not phone behavior |
| `-in-dialog-socket` (`-ids`) | **Unused** | Sends each in-dialog request from a new local socket | Real phones keep their local port. In the test all sessions used the 16 shared sockets (`-udp-sockets`) |
| `-register-first`, `-register-party` | **Unused** | REGISTER the From user, or the To user, before the first request | The pod accepts SUBSCRIBE with digest login and no registration (the test phones never registered). The test is about presence load |
| `-local-address` (`-laddr`), `-af`, `-connect-udp` | **Unused** | Fixed local address or port; IPv4 or IPv6 only; connect first for UDP | Not needed: with `-udp-sockets` the system picks the local ports, and the default address family worked |
| `-call-id-prefix` (`-cip`), `-user-agent` (`-ua`) | **Unused** | Call-ID prefix, User-Agent value | They do not change the presence load. A Call-ID prefix can help to find test traffic in logs |
| `-publish` | **Unused** | Sends PUBLISH | Production had only 1-2 PUBLISH per second per presence server, and the waves came without PUBLISH |
| `-result-file`, `-nagios` | **Unused** | Result file and Nagios exit codes for one check | For monitoring checks, not for a long load test |

All other sipexer options were also unused. They are for other kinds of tests: other methods (INVITE,
REGISTER, MESSAGE, OPTIONS, NOTIFY, KDMQ, INFO, PRACK, ACK, CANCEL), call scenarios (`-call-self`,
`-call-user`, `-cd`, `-rt`, `-late-offer`), message body and template options, AKA login, TLS and
WebSocket, the second user (`-ua2-*`), and print or color options. `-timeout-connect` and
`-timeout-write` kept their defaults: the first is for TCP and TLS only, the second matters only when a
socket write blocks (the load server had 0 UDP send errors).

## Reproduce with sipexer

sipexer can be the test phones too. The sipexer run (run 3) used sipexer for 100,000 of the 240,000
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
2. Start pod-repro for 140,000 subscriptions (as in "How to reproduce", step 3, with `--max-users 35000`)
   and wait 20 minutes.
3. Start sipexer. The exact command of run 3 (from `~/loadgen`):
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
5. Watch presence CPU as in "How to reproduce", step 6.

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
