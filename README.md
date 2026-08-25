# CSaaS — Communications Software as a Solution

Self-hosted, multi-tenant communications platform: SMS, MMS, voice, WebRTC softphone,
contacts, unified inbox, campaigns, auto-dialer, and AI agents on **both SMS and voice**.

**Carrier:** Bandwidth (primary) · Telnyx (failover)
**Repo:** https://github.com/benjium123/CSaaS--Bandwidth

---

## Start here

| Read | For |
|---|---|
| **`docs/PROGRESS.md`** | **Always first.** Current phase, blockers, decision log, what not to re-propose. |
| `docs/ARCHITECTURE.md` | The settled decisions and why. Do not relitigate without hitting the stated condition. |
| `docs/PHASES.md` | The 15 phases and their gates. |
| `docs/WORKSTREAMS.md` | Who owns which files, and the cross-cutting invariants. |
| `docs/SPEC.md` | Full feature scope mapped to phases, including what's explicitly out of v1. |
| `docs/DELEGATION.md` | Which model tier does what on this codebase. |
| `docs/research/` | The evidence behind the decisions. Read only the relevant one. |

## Setup

```bash
cp .env.example .env
# paste your keys into .env — it is gitignored
```

`.env` is organised by phase: `[P0]` keys are needed to boot, `[P1]` for voice, `[P2]` for
AI, `[--]` optional. **Leave anything you don't have blank** — the app boots with degraded
features and logs exactly which provider is disabled and why.

The keys you need first, in order:
1. `BANDWIDTH_ACCOUNT_ID`, `BANDWIDTH_API_USERNAME`, `BANDWIDTH_API_PASSWORD`
2. `BANDWIDTH_MESSAGING_APPLICATION_ID` + `BANDWIDTH_DEFAULT_NUMBER`
3. `JWT_SECRET`, `SESSION_SECRET`, `CREDENTIAL_ENCRYPTION_KEY` (generation commands are in the file)
4. `PUBLIC_BASE_URL` — a tunnel in dev, your domain in prod. **Carriers POST webhooks here.**

## The one-paragraph architecture

FastAPI + Postgres + Redis + S3, React/Vite console. All carrier traffic goes through a
**Carrier Abstraction Layer** modelled as *event in → async command out* (the Telnyx shape),
with the Bandwidth adapter serializing commands down into BXML documents. **AI voice agents**
consume audio over Bandwidth's bidirectional `<StartStream>` WebSocket and run a **cascaded**
Pipecat pipeline (Deepgram → LLM → ElevenLabs) — not speech-to-speech, because we need a
transcript at every hop. **Human agents** get a real WebRTC path, never TCP-carried audio.
Every send passes a central compliance gate.

## Two things that will bite you if you skip them

1. **Webhooks are at-least-once and unordered on both carriers.** Bandwidth retries any
   non-2xx for 24 hours, in parallel with in-flight retries. Every handler is idempotent and
   state-based, or your call state will corrupt on the first transfer.
2. **`rt = 1.0` does not prove low latency.** It hides standing queue depth. Never accept it
   as evidence that the audio path is healthy.

## Status

Planning complete. **P0 not started.** Blocker: confirm the Bandwidth account path to
production (`R1` in `docs/PROGRESS.md`).
