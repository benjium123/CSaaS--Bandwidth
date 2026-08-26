# P7 media measurements — the gate before building the AI agent on this box

> **Status: NOT YET RUN.** Requires the VPS bring-up (deploy/livekit/README.md) and the
> Telnyx SIP trunk. Numbers, not adjectives — fill every cell or do not pass the gate.
> House law applies: idle probes lie; measure under call load.

## 1. VPS ↔ Telnyx SIP edge
```bash
# from the VPS
ping -c 50 sip.telnyx.com                  # RTT min/avg/max/mdev
mtr -uzbc 100 sip.telnyx.com               # per-hop UDP loss
```
| metric | value | pass bar |
|---|---|---|
| RTT avg | _ | < 40 ms |
| RTT p95 | _ | < 60 ms |
| UDP loss | _ | 0% sustained |

## 2. SFU forward latency under load (this VPS runs other tenants)
```bash
lk load-test --url ws://127.0.0.1:7880 --api-key csaas-media --api-secret $LIVEKIT_API_SECRET \
  --room load-1 --audio-publishers 8 --subscribers 8 --duration 2m
```
Run while the box is at its NORMAL daytime load, not at 3am.
| metric | value | pass bar |
|---|---|---|
| SFU forward latency p95 | _ | < 30 ms |
| dropped packets | _ | 0 |
| host CPU during test | _ | < 70% total |

## 3. End-to-end echo call (the real gate)
Dial the trunk number from a cell phone with the echo agent dispatched
(`ECHO_MODE=barge python -m agents.echo_agent start`), speak, listen.
Then run the replay harness against the same room path:
```bash
python -m agents.replay_harness --url ws://127.0.0.1:7880 --api-key csaas-media \
  --api-secret $LIVEKIT_API_SECRET --room call-replay-1 --wav caller_sample.wav \
  --report p7-replay.json --expect-echo
```
| metric | value | pass bar |
|---|---|---|
| rt_ratio | _ | ≥ 0.97 |
| underruns (5 min) | _ | 0 |
| max standing queue depth | _ | reported, < 120 ms |
| tail_energy_ratio | _ | ≥ 0.5 (no eaten sentence tails) |
| barge-in stop latency | _ | < 300 ms |
| ear test (both directions) | _ | no dead air, no one-way audio |

## Decision rule
- All pass → P8 builds on this box.
- SIP-edge numbers fail → region-pin a media host near Telnyx's US-central edge and rerun.
- SFU-load numbers fail → move livekit+livekit-sip to a dedicated small box; the backend
  stays where it is (the media plane was designed detachable — D17).
